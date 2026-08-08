from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import settings
from app.quant.model_registry import QlibMLflowImporter
from app.research.store import ResearchStore
from app.quant.store import QuantStore
from app.quant.model_manifest import write_model_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="导入可信的既有 Qlib MLflow run")
    parser.add_argument("--experiment-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--trust-pickle",
        action="store_true",
        help="确认 params.pkl/pred.pkl/label.pkl 来源可信并允许反序列化",
    )
    args = parser.parse_args()
    research_store = ResearchStore(settings.state_db, settings.document_root)
    settings.runtime_temp_root.mkdir(parents=True, exist_ok=True)
    runtime = TemporaryDirectory(
        prefix="model-import-", dir=settings.runtime_temp_root,
        ignore_cleanup_errors=True,
    )
    store = QuantStore(Path(runtime.name) / "runtime.db", research_store)
    result = QlibMLflowImporter(store, settings.model_artifact_root).import_run(
        args.experiment_dir, args.run_id, trust_pickle=args.trust_pickle
    )
    result["manifest_path"] = str(
        write_model_manifest(store, args.run_id, settings.model_artifact_root)
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
