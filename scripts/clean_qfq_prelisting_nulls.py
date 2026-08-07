from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import settings
from app.market.qfq_cleanup import QfqDatabaseCleaner


def emit(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False), flush=True)


def main() -> int:
    cleaner = QfqDatabaseCleaner(settings.stock_qfq_db, ROOT / "data" / "backups")
    result = cleaner.clean(progress=emit)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
