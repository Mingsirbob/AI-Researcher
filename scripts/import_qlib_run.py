from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings
from app.model_registry import QlibMLflowImporter
from app.research_store import ResearchStore


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
    store = ResearchStore(settings.state_db, settings.document_root)
    result = QlibMLflowImporter(store, settings.model_artifact_root).import_run(
        args.experiment_dir, args.run_id, trust_pickle=args.trust_pickle
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
