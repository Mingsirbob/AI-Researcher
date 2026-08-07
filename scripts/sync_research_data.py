from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.research.announcements import AnnouncementPipeline
from app.core.config import settings
from app.market.repository import normalize_code
from app.data.ifind import IFindDataLayer
from app.research.store import ResearchStore


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="同步证券主数据与公告 PDF 证据")
    parser.add_argument("--bootstrap", action="store_true", help="从本地行情库初始化全量证券主数据")
    parser.add_argument("--code", type=normalize_code, help="同步单只股票的 iFinD 主数据和公告")
    parser.add_argument("--end-date", type=date.fromisoformat, default=date.today())
    parser.add_argument("--lookback-days", type=int, default=365)
    parser.add_argument("--download-limit", type=int, default=5)
    parser.add_argument("--reprocess", action="store_true", help="重新解析已下载 PDF 并重建页码切块")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.bootstrap and not args.code:
        raise SystemExit("至少指定 --bootstrap 或 --code")
    store = ResearchStore(settings.state_db, settings.document_root)
    if args.bootstrap:
        result = store.bootstrap_securities(settings.stock_db)
        print(json.dumps({"bootstrap": result}, ensure_ascii=False, indent=2))
    if not args.code:
        return 0
    if store.security(args.code) is None:
        store.bootstrap_securities(settings.stock_db)
    ifind = IFindDataLayer(settings)
    try:
        pipeline = AnnouncementPipeline(store, ifind)
        if args.reprocess:
            print(json.dumps({"reprocess": pipeline.reprocess(args.code)}, ensure_ascii=False, indent=2))
            if args.download_limit == 0:
                return 0
        result = AnnouncementPipeline(store, ifind).sync(
            args.code,
            end_date=args.end_date,
            lookback_days=args.lookback_days,
            download_limit=args.download_limit,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "success" else 2
    finally:
        ifind.close()


if __name__ == "__main__":
    raise SystemExit(main())
