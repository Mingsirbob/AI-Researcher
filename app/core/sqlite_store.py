from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from app.core.migrations import apply_migration


_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")


class SQLiteStore:
    """Small connection owner for a bounded SQLite state database."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn


def migrate_legacy_tables(
    *,
    target: Any,
    source: Any,
    migration_id: str,
    tables: Iterable[str],
    replace_tables: Iterable[str] = (),
) -> bool:
    """Copy legacy tables once while leaving the source intact for rollback."""
    if Path(target.db_path).resolve() == Path(source.db_path).resolve():
        return False
    table_names = tuple(tables)
    replace = set(replace_tables)
    if any(not _IDENTIFIER.fullmatch(name) for name in (*table_names, *replace)):
        raise ValueError("SQLite migration contains an invalid table identifier")

    def copy() -> None:
        with source.connect() as source_conn, target.connect() as target_conn:
            source_tables = {
                row[0] for row in source_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            target_tables = {
                row[0] for row in target_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            for table in table_names:
                if table not in source_tables or table not in target_tables:
                    continue
                source_columns = [
                    row[1] for row in source_conn.execute(f'PRAGMA table_info("{table}")')
                ]
                target_columns = {
                    row[1] for row in target_conn.execute(f'PRAGMA table_info("{table}")')
                }
                columns = [name for name in source_columns if name in target_columns]
                if not columns:
                    continue
                quoted = ", ".join(f'"{name}"' for name in columns)
                placeholders = ", ".join("?" for _ in columns)
                conflict = "OR REPLACE" if table in replace else "OR IGNORE"
                cursor = source_conn.execute(f'SELECT {quoted} FROM "{table}"')
                while rows := cursor.fetchmany(1000):
                    target_conn.executemany(
                        f'INSERT {conflict} INTO "{table}" ({quoted}) VALUES ({placeholders})',
                        [tuple(row[name] for name in columns) for row in rows],
                    )

    return apply_migration(target.connect, migration_id, copy)
