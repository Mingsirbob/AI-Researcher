from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings
from app.data_access import StockRepository
from app.factor_evaluation import DEFAULT_HORIZONS, FactorEvaluationService
from app.factor_lab import FactorLabService
from app.research_store import ResearchStore
from app.quant_store import QuantStore


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行 M11.2 前复权因子评价")
    parser.add_argument("--start-date", default="2021-01-01")
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--rebalance-step", type=int, default=20)
    parser.add_argument("--horizons", type=int, nargs="+", default=list(DEFAULT_HORIZONS))
    parser.add_argument("--layers", type=int, default=5)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repository = StockRepository(settings.stock_qfq_db)
    research_store = ResearchStore(settings.state_db, settings.document_root)
    store = QuantStore(settings.quant_db, research_store)
    factor_lab = FactorLabService(
        store,
        repository,
        adjustment="CPS:2",
        universe="CSI300 current",
    )
    service = FactorEvaluationService(store, repository, factor_lab)
    result = service.run(
        start_date=args.start_date,
        end_date=args.end_date,
        rebalance_step=args.rebalance_step,
        horizons=tuple(args.horizons),
        layer_count=args.layers,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
