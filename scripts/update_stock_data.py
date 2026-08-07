from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import settings
from app.data.ifind import IFindDataLayer
from app.market.updater import StockDataUpdater


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="通过 iFinD 增量更新本地 A 股不复权日线")
    parser.add_argument("--end-date", type=date.fromisoformat)
    parser.add_argument("--dry-run", action="store_true", help="只打印更新计划，不登录或写数据库")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    current = datetime.now(ZoneInfo("Asia/Shanghai"))
    today = current.date()
    requested_end = args.end_date or today
    updater = StockDataUpdater(settings.stock_db, lambda *_args, **_kwargs: None)
    preliminary_plan = updater.plan(requested_end)
    if args.dry_run:
        print(json.dumps({
            **preliminary_plan.as_dict(),
            "today": today.isoformat(),
            "calendar_verified": False,
            "note": "离线计划未登录 iFinD；正式执行会用官方交易日历修正截止日。",
        }, ensure_ascii=False, indent=2))
        return 0

    data = IFindDataLayer(settings)
    try:
        result = data.sync_daily_prices(
            current,
            target_date=args.end_date,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] in {"success", "up_to_date"} else 2
    finally:
        data.close()


if __name__ == "__main__":
    raise SystemExit(main())
