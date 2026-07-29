from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Protocol


class ConnectionFactory(Protocol):
    def __call__(self) -> sqlite3.Connection: ...


_migration_lock = threading.RLock()


def apply_migration(
    connect: ConnectionFactory,
    migration_id: str,
    apply: Callable[[], None],
) -> bool:
    """Apply one idempotent schema migration and record it in the shared journal."""
    with _migration_lock:
        with connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migration (
                    migration_id TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            exists = conn.execute(
                "SELECT 1 FROM schema_migration WHERE migration_id=?",
                (migration_id,),
            ).fetchone()
        if exists:
            return False

        apply()

        with connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO schema_migration(migration_id, applied_at) VALUES (?, ?)",
                (migration_id, datetime.now(timezone.utc).isoformat()),
            )
        return True


def applied_migrations(connect: ConnectionFactory) -> list[str]:
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migration (
                migration_id TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        return [
            row[0]
            for row in conn.execute(
                "SELECT migration_id FROM schema_migration ORDER BY migration_id"
            ).fetchall()
        ]
