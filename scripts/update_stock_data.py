from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import settings
from app.market.updater import IFindDailyClient, StockDataUpdater, default_end_date


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="通过 iFinD 增量更新本地 A 股不复权日线")
    parser.add_argument("--end-date", type=date.fromisoformat, default=default_end_date())
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--max-retries", type=int, default=settings.ifind_max_attempts)
    parser.add_argument("--retry-delay", type=float, default=settings.ifind_backoff_seconds)
    parser.add_argument("--dry-run", action="store_true", help="只打印更新计划，不登录或写数据库")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    client = IFindDailyClient(settings)
    updater = StockDataUpdater(settings.stock_db, client.fetch)
    preliminary_plan = updater.plan(args.end_date)
    if args.dry_run:
        print(json.dumps({
            **preliminary_plan.as_dict(),
            "today": today.isoformat(),
            "calendar_verified": False,
            "note": "离线计划未登录 iFinD；正式执行会用官方交易日历修正截止日。",
        }, ensure_ascii=False, indent=2))
        return 0

    client.login()
    try:
        calendar_start = min(
            preliminary_plan.current_max_date + timedelta(days=1),
            args.end_date - timedelta(days=31),
        )
        trading_dates = client.fetch_trading_dates(calendar_start, args.end_date)
        if not trading_dates:
            raise RuntimeError(
                f"iFinD 在 {calendar_start.isoformat()}..{args.end_date.isoformat()} 未返回交易日"
            )
        target_end_date = trading_dates[-1]
        plan = updater.plan(target_end_date, trading_dates=trading_dates)
        print(json.dumps({
            **plan.as_dict(),
            "status": "update_required" if plan.needed else "up_to_date",
            "today": today.isoformat(),
            "requested_end_date": args.end_date.isoformat(),
            "latest_completed_trading_date": target_end_date.isoformat(),
            "calendar_verified": True,
            "calendar_source": "iFinD THS_Date_Query/SSE/dateType:0",
        }, ensure_ascii=False, indent=2))
        updater.initialize_schema()
        if not plan.needed:
            return 0
        result = updater.update(
            end_date=target_end_date,
            batch_size=args.batch_size,
            max_retries=args.max_retries,
            retry_delay=args.retry_delay,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "success" else 2
    finally:
        client.logout()


if __name__ == "__main__":
    raise SystemExit(main())
