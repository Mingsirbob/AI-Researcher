from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import settings
from app.data.ifind import IFindDataLayer
from app.market.qfq_sync import ResumableQfqBuilder
from app.market.stock_pool import StockPoolStore


DEFAULT_START = date(2020, 1, 1)
DEFAULT_END = date(2026, 7, 30)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="断点续跑构建全部非ST A股前复权日线数据库"
    )
    parser.add_argument("--start-date", type=date.fromisoformat, default=DEFAULT_START)
    parser.add_argument("--end-date", type=date.fromisoformat, default=DEFAULT_END)
    parser.add_argument("--pool-as-of", type=date.fromisoformat)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--max-failures", type=int, default=50)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def emit(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False), flush=True)


def main() -> int:
    args = parse_args()
    pool_store = StockPoolStore(settings.stock_pool_db)
    snapshot = pool_store.resolve("all_a_non_st", args.pool_as_of or date.max)
    universe = pd.DataFrame(snapshot["members"])[["security_code", "security_name"]]
    data = IFindDataLayer(settings)
    builder = ResumableQfqBuilder(
        target_db=settings.stock_qfq_db,
        pool_db=settings.stock_pool_db,
        universe=universe,
        pool_id=snapshot["pool_id"],
        pool_as_of=snapshot["as_of"],
        fetch=lambda codes, start, end: data.get_daily_prices(
            codes,
            start,
            end,
            adjustment="forward",
            max_attempts=min(settings.ifind_max_attempts, 2),
        ),
    )
    plan = builder.plan(start_date=args.start_date, end_date=args.end_date)
    print(json.dumps(plan.as_dict(), ensure_ascii=False, indent=2), flush=True)
    if args.dry_run:
        return 0
    try:
        result = builder.build(
            start_date=args.start_date,
            end_date=args.end_date,
            batch_size=args.batch_size,
            max_failures=args.max_failures,
            progress=emit,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        return 0 if result["published"] else 2
    finally:
        data.close()


if __name__ == "__main__":
    raise SystemExit(main())
