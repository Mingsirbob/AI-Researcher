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
from app.market.stock_pool import STOCK_POOLS, StockPoolStore


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="通过 iFinD 更新股票池快照")
    parser.add_argument("--as-of", type=date.fromisoformat)
    parser.add_argument("--pool-id", choices=[item["pool_id"] for item in STOCK_POOLS])
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    as_of = args.as_of or datetime.now(ZoneInfo("Asia/Shanghai")).date()
    store = StockPoolStore(settings.stock_pool_db)
    data = IFindDataLayer(settings)
    results = []
    try:
        for definition in STOCK_POOLS:
            if args.pool_id and definition["pool_id"] != args.pool_id:
                continue
            store.register({**definition, "source": "iFinD THS_WCQuery"})
            try:
                members = data.get_universe_by_query(definition["query_text"])
                expected_count = definition.get("expected_member_count")
                if expected_count is not None and len(members) != expected_count:
                    raise ValueError(
                        f"{definition['name']} 成分数量异常："
                        f"expected={expected_count}, actual={len(members)}"
                    )
                minimum_count = definition.get("minimum_member_count")
                maximum_count = definition.get("maximum_member_count")
                if minimum_count is not None and len(members) < minimum_count:
                    raise ValueError(
                        f"{definition['name']} 成分数量过少："
                        f"minimum={minimum_count}, actual={len(members)}"
                    )
                if maximum_count is not None and len(members) > maximum_count:
                    raise ValueError(
                        f"{definition['name']} 成分数量过多："
                        f"maximum={maximum_count}, actual={len(members)}"
                    )
                result = store.save_snapshot(definition["pool_id"], as_of, members)
                results.append({**result, "name": definition["name"], "status": "success"})
            except Exception as exc:
                store.record_failure(definition["pool_id"], exc)
                results.append({
                    "pool_id": definition["pool_id"],
                    "name": definition["name"],
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                })
    finally:
        data.close()
    summary = {
        "database": str(settings.stock_pool_db),
        "as_of": as_of.isoformat(),
        "success_count": sum(item["status"] == "success" for item in results),
        "failed_count": sum(item["status"] == "failed" for item in results),
        "items": results,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["failed_count"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
