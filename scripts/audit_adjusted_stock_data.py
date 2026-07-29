from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="只读审计复权日线数据库")
    parser.add_argument("--db", type=Path, default=settings.stock_qfq_db)
    return parser.parse_args()


def main() -> int:
    path = parse_args().db.resolve()
    if not path.exists():
        raise FileNotFoundError(path)
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name LIKE 'stock_%' ORDER BY name"
            )
        ]
        stats = [
            conn.execute(
                f'SELECT MIN(time), MAX(time), COUNT(*), '
                f'SUM(CASE WHEN close IS NULL THEN 1 ELSE 0 END) FROM "{table}"'
            ).fetchone()
            for table in tables
        ]
        metadata = dict(conn.execute("SELECT * FROM price_database_metadata").fetchone())
        result = {
            "path": str(path),
            "file_bytes": path.stat().st_size,
            "integrity": conn.execute("PRAGMA integrity_check").fetchone()[0],
            "stock_tables": len(tables),
            "membership": conn.execute(
                "SELECT COUNT(*) FROM price_universe_membership"
            ).fetchone()[0],
            "rows_sum": sum(row[2] for row in stats),
            "global_min": min(row[0] for row in stats if row[0]),
            "global_max": max(row[1] for row in stats if row[1]),
            "null_close_rows": sum(row[3] or 0 for row in stats),
            "quality_issues": conn.execute(
                "SELECT COUNT(*) FROM data_quality_issues"
            ).fetchone()[0],
            "quality_exceptions": [
                dict(row)
                for row in conn.execute(
                    "SELECT security_code, trading_date, rule, verification "
                    "FROM price_quality_exceptions ORDER BY trading_date, security_code"
                )
            ],
            "metadata": metadata,
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    valid = (
        result["integrity"] == "ok"
        and result["stock_tables"] == metadata["security_count"]
        and result["membership"] == metadata["security_count"]
        and result["rows_sum"] == metadata["row_count"]
        and result["null_close_rows"] == 0
        and result["quality_issues"] == 0
    )
    return 0 if valid else 2


if __name__ == "__main__":
    raise SystemExit(main())
