from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.market.updater import StockDataUpdater


PRELISTING_BLANK_CONDITION = (
    "open IS NULL AND high IS NULL AND low IS NULL AND close IS NULL "
    "AND vwap IS NULL AND volume IS NULL"
)
SUSPENDED_ROW_CONDITION = "close IS NOT NULL AND (vwap IS NULL OR volume IS NULL)"


class QfqCleanupError(RuntimeError):
    pass


@dataclass(frozen=True)
class QfqCleanupStats:
    stock_tables: int
    rows_before: int
    rows_after: int
    rows_deleted: int
    suspended_rows_before: int
    suspended_rows_after: int

    def as_dict(self) -> dict:
        return {
            "stock_tables": self.stock_tables,
            "rows_before": self.rows_before,
            "rows_after": self.rows_after,
            "rows_deleted": self.rows_deleted,
            "suspended_rows_before": self.suspended_rows_before,
            "suspended_rows_after": self.suspended_rows_after,
        }


def delete_prelisting_blank_rows(
    conn: sqlite3.Connection,
    tables: list[str],
    *,
    progress: Callable[[dict], None] | None = None,
) -> QfqCleanupStats:
    rows_before = 0
    rows_deleted = 0
    suspended_before = 0
    for index, table in enumerate(tables, 1):
        first_valid, table_rows, suspended_rows = conn.execute(
            f"""
            SELECT MIN(CASE WHEN close IS NOT NULL THEN time END), COUNT(*),
                SUM(CASE WHEN {SUSPENDED_ROW_CONDITION} THEN 1 ELSE 0 END)
            FROM "{table}"
            """
        ).fetchone()
        rows_before += int(table_rows or 0)
        suspended_before += int(suspended_rows or 0)
        if first_valid:
            before = conn.total_changes
            conn.execute(
                f'DELETE FROM "{table}" WHERE time<? AND {PRELISTING_BLANK_CONDITION}',
                (first_valid,),
            )
            rows_deleted += conn.total_changes - before
        if index % 100 == 0:
            conn.commit()
            if progress:
                progress({
                    "status": "cleaning",
                    "processed_tables": index,
                    "total_tables": len(tables),
                    "rows_deleted": rows_deleted,
                })
    conn.commit()

    rows_after = 0
    suspended_after = 0
    remaining_prelisting_blanks = 0
    for table in tables:
        first_valid, table_rows, suspended_rows = conn.execute(
            f"""
            SELECT MIN(CASE WHEN close IS NOT NULL THEN time END), COUNT(*),
                SUM(CASE WHEN {SUSPENDED_ROW_CONDITION} THEN 1 ELSE 0 END)
            FROM "{table}"
            """
        ).fetchone()
        rows_after += int(table_rows or 0)
        suspended_after += int(suspended_rows or 0)
        if first_valid:
            remaining_prelisting_blanks += conn.execute(
                f'SELECT COUNT(*) FROM "{table}" '
                f'WHERE time<? AND {PRELISTING_BLANK_CONDITION}',
                (first_valid,),
            ).fetchone()[0]
    if rows_after != rows_before - rows_deleted:
        raise QfqCleanupError("清洗前后总行数不守恒")
    if suspended_after != suspended_before:
        raise QfqCleanupError("停牌候选行数量发生变化，拒绝发布")
    if remaining_prelisting_blanks:
        raise QfqCleanupError(f"仍有 {remaining_prelisting_blanks} 条上市前全空行")
    return QfqCleanupStats(
        stock_tables=len(tables),
        rows_before=rows_before,
        rows_after=rows_after,
        rows_deleted=rows_deleted,
        suspended_rows_before=suspended_before,
        suspended_rows_after=suspended_after,
    )


class QfqDatabaseCleaner:
    def __init__(self, target_db: Path, backup_dir: Path):
        self.target_db = Path(target_db)
        self.backup_dir = Path(backup_dir)
        self.cleaning_db = self.target_db.with_name(
            f"{self.target_db.stem}.cleaning{self.target_db.suffix}"
        )

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _copy_target(self) -> None:
        if not self.target_db.exists():
            raise FileNotFoundError(self.target_db)
        if self.cleaning_db.exists():
            raise QfqCleanupError(f"清洗临时库已存在：{self.cleaning_db}")
        with closing(self._connect(self.target_db)) as source:
            with closing(self._connect(self.cleaning_db)) as destination:
                source.backup(destination)

    def clean(self, *, progress: Callable[[dict], None] | None = None) -> dict:
        self._copy_target()
        try:
            with closing(self._connect(self.cleaning_db)) as conn:
                tables = StockDataUpdater._stock_tables(conn)
                metadata = conn.execute(
                    "SELECT * FROM price_database_metadata WHERE metadata_id=1"
                ).fetchone()
                membership_count = conn.execute(
                    "SELECT COUNT(*) FROM price_universe_membership"
                ).fetchone()[0]
                if metadata is None or len(tables) != metadata["security_count"]:
                    raise QfqCleanupError("证券表数量与数据库元数据不一致")
                if membership_count != len(tables):
                    raise QfqCleanupError("证券表数量与股池成员数量不一致")
                stats = delete_prelisting_blank_rows(conn, tables, progress=progress)
                previous_dropped = int(metadata["blank_rows_dropped"] or 0)
                conn.execute(
                    """
                    UPDATE price_database_metadata SET row_count=?, blank_rows_dropped=?
                    WHERE metadata_id=1
                    """,
                    (stats.rows_after, previous_dropped + stats.rows_deleted),
                )
                conn.commit()
                if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise QfqCleanupError("清洗临时库完整性检查失败")
                conn.execute("VACUUM")
                if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise QfqCleanupError("VACUUM 后完整性检查失败")

            sidecars = [
                self.target_db.with_name(f"{self.target_db.name}-wal"),
                self.target_db.with_name(f"{self.target_db.name}-shm"),
            ]
            active_sidecars = [str(path) for path in sidecars if path.exists()]
            if active_sidecars:
                raise QfqCleanupError("目标库存在活动SQLite侧文件：" + ", ".join(active_sidecars))
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S")
            backup = self.backup_dir / f"stock_data_qfq.before-null-cleanup.{stamp}.db"
            os.replace(self.target_db, backup)
            try:
                os.replace(self.cleaning_db, self.target_db)
            except Exception:
                os.replace(backup, self.target_db)
                raise
            return {
                **stats.as_dict(),
                "status": "success",
                "target_db": str(self.target_db),
                "backup_db": str(backup),
                "file_bytes": self.target_db.stat().st_size,
            }
        except Exception:
            # Keep the cleaning copy for inspection if validation or publishing fails.
            raise
