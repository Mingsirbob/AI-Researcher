from __future__ import annotations

import json
import pickle
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yaml

from app.quant.store import QuantStore
from app.core.primitives import qlib_instrument_to_code, sha256_file, sha256_text


MODEL_REGISTRY_VERSION = "qlib-mlflow-import-v1"
REQUIRED_ARTIFACTS = {
    "model": "params.pkl",
    "predictions": "pred.pkl",
    "labels": "label.pkl",
}


def _metric_value(path: Path) -> float:
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        raise ValueError(f"指标文件为空：{path.name}")
    return float(lines[-1].split()[1])


def _read_params(path: Path) -> dict[str, str]:
    return {
        item.name: item.read_text(encoding="utf-8").strip()
        for item in path.iterdir()
        if item.is_file()
    }


def prepare_shadow_signals(predictions: pd.DataFrame, labels: pd.DataFrame) -> tuple[dict, list[dict]]:
    if predictions.shape[1] != 1 or labels.shape[1] != 1:
        raise ValueError("M6 仅接受单目标预测和单标签")
    if predictions.index.names != ["datetime", "instrument"]:
        raise ValueError("预测索引必须为 datetime + instrument")
    if not predictions.index.equals(labels.index):
        raise ValueError("预测与标签索引不一致")
    if predictions.index.has_duplicates:
        raise ValueError("预测包含重复证券日期")
    if predictions.isna().any().any():
        raise ValueError("预测包含空值")

    frame = pd.DataFrame(
        {
            "score": pd.to_numeric(predictions.iloc[:, 0], errors="raise"),
            "realized_label": pd.to_numeric(labels.iloc[:, 0], errors="coerce"),
        }
    ).reset_index()
    frame["datetime"] = pd.to_datetime(frame["datetime"], errors="raise")
    frame["source_instrument"] = frame["instrument"].astype(str).str.upper()
    frame["security_code"] = frame["source_instrument"].map(qlib_instrument_to_code)
    grouped = frame.groupby("datetime", sort=False)["score"]
    frame["cross_section_rank"] = grouped.rank(method="first", ascending=False).astype(int)
    frame["cross_section_size"] = grouped.transform("size").astype(int)
    denominator = (frame["cross_section_size"] - 1).clip(lower=1)
    frame["percentile"] = (
        100.0 * (frame["cross_section_size"] - frame["cross_section_rank"]) / denominator
    ).round(4)
    rows = [
        {
            "trading_date": row.datetime.date().isoformat(),
            "security_code": row.security_code,
            "source_instrument": row.source_instrument,
            "score": float(row.score),
            "realized_label": None if pd.isna(row.realized_label) else float(row.realized_label),
            "cross_section_rank": int(row.cross_section_rank),
            "cross_section_size": int(row.cross_section_size),
            "percentile": float(row.percentile),
        }
        for row in frame.itertuples(index=False)
    ]
    summary = {
        "start_date": frame["datetime"].min().date().isoformat(),
        "end_date": frame["datetime"].max().date().isoformat(),
        "row_count": len(frame),
        "instrument_count": int(frame["security_code"].nunique()),
        "trading_days": int(frame["datetime"].nunique()),
    }
    return summary, rows


class QlibMLflowImporter:
    def __init__(self, store: QuantStore, artifact_root: Path):
        self.store = store
        self.artifact_root = artifact_root

    def import_run(self, experiment_dir: Path, run_id: str, *, trust_pickle: bool) -> dict:
        if not trust_pickle:
            raise ValueError("必须显式确认信任 pickle 才能导入模型和预测")
        run_dir = experiment_dir.resolve() / run_id
        meta_path = run_dir / "meta.yaml"
        artifacts_dir = run_dir / "artifacts"
        if not meta_path.is_file() or not artifacts_dir.is_dir():
            raise FileNotFoundError("MLflow run 目录缺少 meta.yaml 或 artifacts")
        meta = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
        if str(meta.get("run_id")) != run_id or int(meta.get("status", -1)) != 3:
            raise ValueError("MLflow run ID 不匹配或运行未成功完成")

        source_artifacts = {
            kind: artifacts_dir / filename for kind, filename in REQUIRED_ARTIFACTS.items()
        }
        missing = [str(path) for path in source_artifacts.values() if not path.is_file()]
        if missing:
            raise FileNotFoundError(f"缺少模型产物：{missing}")

        with source_artifacts["model"].open("rb") as handle:
            model = pickle.load(handle)
        model_type = f"{type(model).__module__}.{type(model).__name__}"
        if model_type != "qlib.contrib.model.gbdt.LGBModel" or getattr(model, "model", None) is None:
            raise ValueError(f"模型不是已拟合的 Qlib LGBModel：{model_type}")
        predictions = pd.read_pickle(source_artifacts["predictions"])
        labels = pd.read_pickle(source_artifacts["labels"])
        signal_summary, signals = prepare_shadow_signals(predictions, labels)

        params = _read_params(run_dir / "params")
        metrics = {
            item.name: _metric_value(item)
            for item in (run_dir / "metrics").iterdir()
            if item.is_file() and item.name not in {"l2.train", "l2.valid"}
        }
        hashes = {kind: sha256_file(path) for kind, path in source_artifacts.items()}
        fingerprint_payload = json.dumps(
            {"version": MODEL_REGISTRY_VERSION, "run_id": run_id, "hashes": hashes},
            sort_keys=True,
        )
        source_fingerprint = sha256_text(fingerprint_payload)
        imported_at = datetime.now(timezone.utc).isoformat()
        destination = self.artifact_root / run_id
        destination.mkdir(parents=True, exist_ok=True)
        archived_artifacts = []
        for kind, source in source_artifacts.items():
            target = destination / source.name
            if target.exists() and sha256_file(target) != hashes[kind]:
                raise ValueError(f"项目内模型归档哈希冲突：{target}")
            if not target.exists():
                shutil.copy2(source, target)
            archived_artifacts.append(
                {
                    "artifact_type": kind,
                    "file_path": str(target.resolve()),
                    "sha256": hashes[kind],
                    "byte_size": target.stat().st_size,
                }
            )

        config = {
            "registry_version": MODEL_REGISTRY_VERSION,
            "provider_uri": "~/.qlib/qlib_data/cn_data",
            "model_params": {
                key.removeprefix("model.kwargs."): value
                for key, value in params.items()
                if key.startswith("model.kwargs.")
            },
            "prediction_trading_days": signal_summary["trading_days"],
            "prediction_column": str(predictions.columns[0]),
            "label_column": str(labels.columns[0]),
        }
        model_run = {
            "model_run_id": run_id,
            "experiment_id": str(meta["experiment_id"]),
            "framework": "Qlib 0.9.7 / MLflow",
            "model_class": "LGBModel",
            "feature_set": "Alpha158",
            "universe": params.get("dataset.kwargs.handler.kwargs.instruments", "csi300"),
            "train_start": "2008-01-01",
            "train_end": "2014-12-31",
            "valid_start": "2015-01-01",
            "valid_end": "2016-12-31",
            "test_start": signal_summary["start_date"],
            "test_end": signal_summary["end_date"],
            "status": "accepted_historical_only",
            "intended_use": "shadow_evaluation_only",
            "source_path": str(run_dir),
            "source_fingerprint": source_fingerprint,
            "config": config,
            "metrics": metrics,
            "limitations": [
                "预测和标签仅覆盖 2017-01-03 至 2020-07-31，不能视为当前信号。",
                "模型训练使用 Qlib 下载的 CSI300 历史数据，不是本项目 stock_data.db。",
                "现有 Qlib 数据日历截至 2020-09-25，尚不能执行 2026 年推理。",
                "该实验是单次时间切分结果，尚未完成滚动样本外和多市场阶段稳定性验证。",
                "Shadow Signal 不进入候选池准入，也不构成交易建议。",
            ],
            "imported_at": imported_at,
        }
        snapshot = {
            "snapshot_id": sha256_text(
                f"{run_id}:{hashes['predictions']}:{hashes['labels']}", encoding="ascii"
            ),
            **{key: signal_summary[key] for key in ("start_date", "end_date", "row_count", "instrument_count")},
            "status": "historical_only",
            "source_fingerprint": source_fingerprint,
            "imported_at": imported_at,
        }
        return self.store.register_model_run(
            model_run=model_run,
            artifacts=archived_artifacts,
            snapshot=snapshot,
            signals=signals,
        )
