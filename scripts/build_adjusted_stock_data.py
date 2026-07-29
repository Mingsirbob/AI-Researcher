from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.adjusted_data import AdjustedStockDataBuilder
from app.config import settings
from app.updater import IFindDailyClient, default_end_date


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="通过 iFinD 全量构建独立的前复权或后复权 A 股日线数据库"
    )
    parser.add_argument("--adjustment", required=True, choices=("forward", "backward"))
    parser.add_argument(
        "--universe",
        choices=("source", "csi300"),
        default="source",
        help="source=源库全部股票；csi300=iFinD 查询时点的沪深300成分股",
    )
    parser.add_argument("--start-date", type=date.fromisoformat)
    parser.add_argument("--end-date", type=date.fromisoformat, default=default_end_date())
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--max-retries", type=int, default=settings.ifind_max_attempts)
    parser.add_argument("--retry-delay", type=float, default=settings.ifind_backoff_seconds)
    parser.add_argument("--dry-run", action="store_true", help="仅检查源库并打印构建计划")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    target_db = settings.stock_qfq_db if args.adjustment == "forward" else settings.stock_hfq_db
    client = IFindDailyClient(settings, adjustment=args.adjustment)
    raw_client = IFindDailyClient(settings, adjustment="unadjusted")
    needs_login = args.universe == "csi300" or not args.dry_run
    if needs_login:
        client.login()
        raw_client.login()
    try:
        universe = client.fetch_csi300_universe() if args.universe == "csi300" else None
        builder = AdjustedStockDataBuilder(
            settings.stock_db,
            target_db,
            client.fetch,
            adjustment=args.adjustment,
            universe=universe,
            universe_name="csi300_current" if universe is not None else "source_database",
            universe_as_of=date.today().isoformat(),
            universe_source="iFinD THS_WCQuery" if universe is not None else "stock_data.db tables",
            raw_fetch=raw_client.fetch,
        )
        plan = builder.plan(end_date=args.end_date, start_date=args.start_date)
        print(json.dumps(plan.as_dict(), ensure_ascii=False, indent=2))
        if args.dry_run:
            return 0
        result = builder.build(
            start_date=args.start_date,
            end_date=args.end_date,
            batch_size=args.batch_size,
            max_retries=args.max_retries,
            retry_delay=args.retry_delay,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["published"] else 2
    finally:
        if needs_login:
            raw_client.logout()
            client.logout()


if __name__ == "__main__":
    raise SystemExit(main())
