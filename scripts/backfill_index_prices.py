from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import settings
from app.data.ifind import IFindDataLayer
from app.market.stock_pool import MAJOR_INDEX_BENCHMARKS, StockPoolStore


DEFAULT_START = date(2020, 1, 1)
DEFAULT_END = date(2026, 7, 31)
SOURCE = "iFinD THS_HD (unadjusted)"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="回填六大指数的不复权日线到 stock_pool.db"
    )
    parser.add_argument("--start-date", type=date.fromisoformat, default=DEFAULT_START)
    parser.add_argument("--end-date", type=date.fromisoformat, default=DEFAULT_END)
    parser.add_argument(
        "--index-code",
        action="append",
        choices=tuple(MAJOR_INDEX_BENCHMARKS),
        help="只回填指定指数；可重复传入。默认回填全部六个指数",
    )
    parser.add_argument("--dry-run", action="store_true", help="只显示计划，不调用 iFinD")
    return parser.parse_args()


def backfill(
    data: IFindDataLayer,
    store: StockPoolStore,
    *,
    index_codes: list[str],
    start_date: date,
    end_date: date,
) -> list[dict]:
    if start_date > end_date:
        raise ValueError("开始日期不能晚于结束日期")

    results: list[dict] = []
    for code in index_codes:
        name = MAJOR_INDEX_BENCHMARKS[code]
        try:
            frame = data.get_daily_prices(
                [code],
                start_date,
                end_date,
                adjustment="unadjusted",
                max_attempts=min(settings.ifind_max_attempts, 2),
            )
            if frame.empty:
                raise RuntimeError("iFinD 未返回行情数据")

            write_result = store.save_index_prices(
                frame.to_dict(orient="records"),
                source=SOURCE,
            )
            dates = frame["time"].astype(str)
            results.append({
                "index_code": code,
                "name": name,
                "status": "success",
                "rows_received": len(frame),
                "rows_written": write_result["rows_written"],
                "first_date": dates.min(),
                "last_date": dates.max(),
            })
        except Exception as exc:
            results.append({
                "index_code": code,
                "name": name,
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            })
    return results


def main() -> int:
    args = parse_args()
    if args.start_date > args.end_date:
        raise SystemExit("开始日期不能晚于结束日期")

    index_codes = list(dict.fromkeys(args.index_code or MAJOR_INDEX_BENCHMARKS))
    plan = {
        "database": str(settings.stock_pool_db),
        "start_date": args.start_date.isoformat(),
        "end_date": args.end_date.isoformat(),
        "adjustment": "unadjusted",
        "indices": [
            {"index_code": code, "name": MAJOR_INDEX_BENCHMARKS[code]}
            for code in index_codes
        ],
    }
    if args.dry_run:
        print(json.dumps({**plan, "status": "dry_run"}, ensure_ascii=False, indent=2))
        return 0

    store = StockPoolStore(settings.stock_pool_db)
    data = IFindDataLayer(settings)
    try:
        results = backfill(
            data,
            store,
            index_codes=index_codes,
            start_date=args.start_date,
            end_date=args.end_date,
        )
    finally:
        data.close()

    failed_count = sum(item["status"] == "failed" for item in results)
    summary = {
        **plan,
        "status": "success" if failed_count == 0 else "partial",
        "success_count": len(results) - failed_count,
        "failed_count": failed_count,
        "items": results,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if failed_count == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
