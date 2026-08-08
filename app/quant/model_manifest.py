from __future__ import annotations

import json
from pathlib import Path


def load_model_manifest(store, artifact_root: Path) -> dict:
    manifests = sorted(artifact_root.glob("*/manifest.json"))
    if not manifests:
        raise FileNotFoundError(f"模型目录中没有 manifest.json：{artifact_root}")
    manifest_path = manifests[-1]
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    model = payload["model"]
    model_id = model["model_run_id"]
    model_dir = manifest_path.parent
    artifacts = []
    for item in payload["artifacts"]:
        path = model_dir / item["filename"]
        if not path.is_file():
            raise FileNotFoundError(f"模型产物不存在：{path}")
        artifacts.append({**item, "file_path": str(path.resolve())})

    with store.connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO model_run (
                model_run_id, experiment_id, framework, model_class, feature_set,
                universe, train_start, train_end, valid_start, valid_end,
                test_start, test_end, status, intended_use, source_path,
                source_fingerprint, config_json, metrics_json, limitations_json,
                imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                model_id, model["experiment_id"], model["framework"],
                model["model_class"], model["feature_set"], model["universe"],
                model["train_start"], model["train_end"], model["valid_start"],
                model["valid_end"], model["test_start"], model["test_end"],
                model["status"], model["intended_use"], str(model_dir.resolve()),
                model["source_fingerprint"],
                json.dumps(model.get("config") or {}, ensure_ascii=False, sort_keys=True),
                json.dumps(model.get("metrics") or {}, ensure_ascii=False, sort_keys=True),
                json.dumps(model.get("limitations") or [], ensure_ascii=False),
                model["imported_at"],
            ),
        )
        conn.execute("DELETE FROM model_artifact WHERE model_run_id=?", (model_id,))
        conn.executemany(
            "INSERT INTO model_artifact VALUES (?, ?, ?, ?, ?)",
            [
                (
                    model_id, item["artifact_type"], item["file_path"],
                    item["sha256"], int(item["byte_size"]),
                )
                for item in artifacts
            ],
        )
    return store.model_run(model_id)


def write_model_manifest(store, model_run_id: str, artifact_root: Path) -> Path:
    model = store.model_run(model_run_id)
    if model is None:
        raise KeyError("模型运行不存在")
    model_dir = artifact_root / model_run_id
    payload = {
        "schema_version": 1,
        "model": {
            key: value for key, value in model.items()
            if key not in {"artifacts", "prediction_snapshot", "source_path"}
        },
        "artifacts": [
            {
                "artifact_type": item["artifact_type"],
                "filename": Path(item["file_path"]).name,
                "sha256": item["sha256"],
                "byte_size": item["byte_size"],
            }
            for item in model["artifacts"]
        ],
    }
    target = model_dir / "manifest.json"
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target
