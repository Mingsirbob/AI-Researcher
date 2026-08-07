from __future__ import annotations

import json
import math
import os
import sqlite3
import uuid
from collections import defaultdict
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from app.core.primitives import code_to_table
from app.market.updater import StockDataUpdater, UpdateProcessLock


BUILDER_VERSION = "qfq-stock-pool-resumable-v1"
PRICE_COLUMNS = ("time", "open", "high", "low", "close", "vwap", "volume")
BUILD_ONLY_TABLES = (
    "data_quality_issues",
    "data_update_runs",
    "market_data_status",
    "price_quality_exceptions",
    "qfq_build_state",
    "qfq_security_progress",
    "qfq_sync_failure",
)


class QfqSyncError(RuntimeError):
    pass


@dataclass(frozen=True)
class QfqBuildPlan:
    target_db: Path
    work_db: Path
    pool_id: str
    pool_as_of: str
    start_date: date
    end_date: date
    security_count: int
    target_exists: bool
    resumable_work_exists: bool

    def as_dict(self) -> dict:
        return {
            "builder_version": BUILDER_VERSION,
            "mode": "resumable_staging_build",
            "adjustment": "forward",
            "ifind_params": "CPS:2",
            "target_db": str(self.target_db),
            "work_db": str(self.work_db),
            "pool_id": self.pool_id,
            "pool_as_of": self.pool_as_of,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "security_count": self.security_count,
            "target_exists": self.target_exists,
            "resumable_work_exists": self.resumable_work_exists,
        }


class ResumableQfqBuilder:
    """Build a forward-adjusted database in a persistent staging file."""

    def __init__(
        self,
        *,
        target_db: Path,
        pool_db: Path,
        universe: pd.DataFrame,
        pool_id: str,
        pool_as_of: str,
        fetch: Callable[[list[str], date, date], pd.DataFrame],
        work_db: Path | None = None,
    ):
        required = {"security_code", "security_name"}
        if not isinstance(universe, pd.DataFrame) or not required.issubset(universe.columns):
            raise ValueError("股池必须包含 security_code 和 security_name")
        normalized = universe[["security_code", "security_name"]].copy()
        normalized["security_code"] = normalized["security_code"].astype(str).str.upper()
        normalized["security_name"] = normalized["security_name"].fillna("").astype(str).str.strip()
        valid = normalized["security_code"].str.fullmatch(r"\d{6}\.(SH|SZ|BJ)", na=False)
        if normalized.empty or not valid.all() or normalized["security_code"].duplicated().any():
            raise ValueError("股池为空，或包含非法、重复的A股代码")
        self.target_db = Path(target_db)
        self.pool_db = Path(pool_db)
        self.work_db = Path(work_db) if work_db else self.target_db.with_name(
            f"{self.target_db.stem}.building{self.target_db.suffix}"
        )
        if self.target_db.resolve() == self.work_db.resolve():
            raise ValueError("构建库不能与目标库相同")
        self.universe = normalized.sort_values("security_code").reset_index(drop=True)
        self.pool_id = pool_id
        self.pool_as_of = pool_as_of
        self.fetch = fetch

    def plan(self, *, start_date: date, end_date: date) -> QfqBuildPlan:
        if start_date > end_date:
            raise ValueError("start_date 不能晚于 end_date")
        return QfqBuildPlan(
            target_db=self.target_db,
            work_db=self.work_db,
            pool_id=self.pool_id,
            pool_as_of=self.pool_as_of,
            start_date=start_date,
            end_date=end_date,
            security_count=len(self.universe),
            target_exists=self.target_db.exists(),
            resumable_work_exists=self.work_db.exists(),
        )

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @staticmethod
    def _create_price_table(conn: sqlite3.Connection, code: str) -> None:
        table = code_to_table(code)
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS "{table}" (
                time TEXT PRIMARY KEY,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                vwap REAL,
                volume REAL
            )
            """
        )

    @staticmethod
    def _create_metadata_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS price_database_metadata (
                metadata_id INTEGER PRIMARY KEY CHECK (metadata_id = 1),
                builder_version TEXT NOT NULL,
                adjustment TEXT NOT NULL,
                ifind_params TEXT NOT NULL,
                source_db TEXT NOT NULL,
                source_db_size INTEGER NOT NULL,
                source_db_mtime_ns INTEGER NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                security_count INTEGER NOT NULL,
                row_count INTEGER NOT NULL,
                built_at TEXT NOT NULL,
                blank_rows_dropped INTEGER NOT NULL,
                accepted_quality_exception_count INTEGER NOT NULL,
                vwap_adjustment_method TEXT NOT NULL,
                universe_name TEXT NOT NULL,
                universe_as_of TEXT NOT NULL,
                universe_source TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS price_universe_membership (
                security_code TEXT PRIMARY KEY,
                security_name TEXT NOT NULL,
                universe_name TEXT NOT NULL,
                universe_as_of TEXT NOT NULL,
                universe_source TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS qfq_security_progress (
                security_code TEXT PRIMARY KEY,
                checked_through TEXT NOT NULL,
                last_data_date TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS qfq_sync_failure (
                security_code TEXT PRIMARY KEY,
                error TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS qfq_build_state (
                state_id INTEGER PRIMARY KEY CHECK (state_id = 1),
                builder_version TEXT NOT NULL,
                pool_id TEXT NOT NULL,
                pool_as_of TEXT NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                status TEXT NOT NULL,
                security_count INTEGER NOT NULL,
                completed_count INTEGER NOT NULL,
                rows_inserted INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        StockDataUpdater._initialize_journal(conn)

    def _initialize_work_db(self, plan: QfqBuildPlan, now: str) -> None:
        if self.work_db.exists():
            return
        self.work_db.parent.mkdir(parents=True, exist_ok=True)
        initializing = self.work_db.with_name(
            f".{self.work_db.name}.{uuid.uuid4().hex}.initializing"
        )
        try:
            with closing(self._connect(initializing)) as destination:
                if self.target_db.exists():
                    with closing(self._connect(self.target_db)) as source:
                        source.backup(destination)
                self._create_metadata_schema(destination)
                previous = destination.execute(
                    "SELECT * FROM qfq_build_state WHERE state_id=1"
                ).fetchone()
                expected = (
                    self.pool_id,
                    self.pool_as_of,
                    plan.start_date.isoformat(),
                    plan.end_date.isoformat(),
                )
                if previous is not None:
                    actual = (
                        previous["pool_id"], previous["pool_as_of"],
                        previous["start_date"], previous["end_date"],
                    )
                    if actual != expected:
                        raise QfqSyncError(
                            "现有构建状态与本次参数不一致，请保留或移走 building 数据库后重试"
                        )
                for code in self.universe["security_code"]:
                    self._create_price_table(destination, code)
                destination.execute("DELETE FROM price_universe_membership")
                destination.executemany(
                    """
                    INSERT INTO price_universe_membership (
                        security_code, security_name, universe_name,
                        universe_as_of, universe_source
                    ) VALUES (?, ?, ?, ?, 'stock_pool.db')
                    """,
                    [
                        (row.security_code, row.security_name, self.pool_id, self.pool_as_of)
                        for row in self.universe.itertuples(index=False)
                    ],
                )
                destination.execute(
                    """
                    DELETE FROM qfq_security_progress
                    WHERE security_code NOT IN (
                        SELECT security_code FROM price_universe_membership
                    )
                    """
                )
                completed = destination.execute(
                    "SELECT COUNT(*) FROM qfq_security_progress WHERE checked_through>=?",
                    (plan.end_date.isoformat(),),
                ).fetchone()[0]
                previous_rows = previous["rows_inserted"] if previous else 0
                destination.execute(
                    """
                    INSERT OR REPLACE INTO qfq_build_state VALUES (
                        1, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?
                    )
                    """,
                    (
                        BUILDER_VERSION, self.pool_id, self.pool_as_of,
                        plan.start_date.isoformat(), plan.end_date.isoformat(),
                        len(self.universe), completed, previous_rows, now,
                    ),
                )
                destination.commit()
            os.replace(initializing, self.work_db)
        finally:
            initializing.unlink(missing_ok=True)

    def _pending_groups(
        self, conn: sqlite3.Connection, plan: QfqBuildPlan, now: str
    ) -> dict[date, list[str]]:
        groups: dict[date, list[str]] = defaultdict(list)
        progress = {
            row["security_code"]: date.fromisoformat(row["checked_through"])
            for row in conn.execute("SELECT security_code, checked_through FROM qfq_security_progress")
        }
        for code in self.universe["security_code"]:
            if progress.get(code, date.min) >= plan.end_date:
                continue
            table = code_to_table(code)
            latest_text = conn.execute(f'SELECT MAX(time) FROM "{table}"').fetchone()[0]
            latest = date.fromisoformat(latest_text) if latest_text else None
            if latest is not None and latest >= plan.end_date:
                conn.execute(
                    "INSERT OR REPLACE INTO qfq_security_progress VALUES (?, ?, ?, ?)",
                    (code, plan.end_date.isoformat(), latest.isoformat(), now),
                )
                continue
            request_start = max(plan.start_date, latest + timedelta(days=1)) if latest else plan.start_date
            groups[request_start].append(code)
        conn.commit()
        return groups

    def _reconcile_universe(self, conn: sqlite3.Connection, plan: QfqBuildPlan, now: str) -> None:
        current_codes = set(self.universe["security_code"])
        current_tables = {code_to_table(code) for code in current_codes}
        for table in StockDataUpdater._stock_tables(conn):
            if table not in current_tables:
                conn.execute(f'DROP TABLE "{table}"')
        for code in current_codes:
            self._create_price_table(conn, code)
        conn.execute("DELETE FROM price_universe_membership")
        conn.executemany(
            """
            INSERT INTO price_universe_membership (
                security_code, security_name, universe_name,
                universe_as_of, universe_source
            ) VALUES (?, ?, ?, ?, 'stock_pool.db')
            """,
            [
                (row.security_code, row.security_name, self.pool_id, self.pool_as_of)
                for row in self.universe.itertuples(index=False)
            ],
        )
        conn.execute(
            """
            DELETE FROM qfq_security_progress
            WHERE security_code NOT IN (SELECT security_code FROM price_universe_membership)
            """
        )
        conn.execute(
            """
            DELETE FROM qfq_sync_failure
            WHERE security_code NOT IN (SELECT security_code FROM price_universe_membership)
            """
        )
        conn.execute(
            """
            DELETE FROM market_data_status
            WHERE security_code NOT IN (SELECT security_code FROM price_universe_membership)
            """
        )
        completed = conn.execute(
            """
            SELECT COUNT(*) FROM qfq_security_progress p
            JOIN price_universe_membership m USING (security_code)
            WHERE p.checked_through>=?
            """,
            (plan.end_date.isoformat(),),
        ).fetchone()[0]
        conn.execute(
            """
            UPDATE qfq_build_state SET security_count=?, completed_count=?,
                status=CASE WHEN status='complete' AND ?=? THEN 'complete' ELSE 'running' END,
                updated_at=? WHERE state_id=1
            """,
            (len(current_codes), completed, completed, len(current_codes), now),
        )
        conn.commit()

    @staticmethod
    def _number(value: Any) -> float | None:
        if value is None or pd.isna(value):
            return None
        number = float(value)
        return number if math.isfinite(number) else None

    def _save_frame(
        self,
        conn: sqlite3.Connection,
        frame: pd.DataFrame,
        requested_codes: list[str],
        plan: QfqBuildPlan,
        now: str,
    ) -> tuple[int, list[str]]:
        blank_mask = frame[
            ["open", "high", "low", "close", "vwap", "volume"]
        ].isna().all(axis=1)
        frame = frame.loc[~blank_mask].copy()
        returned = set(frame["thscode"].astype(str))
        unknown = returned.difference(requested_codes)
        if unknown:
            raise QfqSyncError(f"iFinD 返回请求范围外证券：{sorted(unknown)[:5]}")
        inserted = 0
        completed: list[str] = []
        for code, group in frame.groupby("thscode", sort=False):
            rows = [
                (
                    row.time,
                    self._number(row.open), self._number(row.high), self._number(row.low),
                    self._number(row.close), self._number(row.vwap), self._number(row.volume),
                )
                for row in group.itertuples(index=False)
                if plan.start_date <= date.fromisoformat(row.time) <= plan.end_date
            ]
            table = code_to_table(code)
            before = conn.total_changes
            conn.executemany(
                f'INSERT OR REPLACE INTO "{table}" '
                "(time, open, high, low, close, vwap, volume) VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            inserted += conn.total_changes - before
            latest = conn.execute(f'SELECT MAX(time) FROM "{table}"').fetchone()[0]
            conn.execute(
                "INSERT OR REPLACE INTO qfq_security_progress VALUES (?, ?, ?, ?)",
                (code, plan.end_date.isoformat(), latest, now),
            )
            if latest:
                conn.execute(
                    """
                    INSERT INTO market_data_status VALUES (?, ?, ?, 'ready')
                    ON CONFLICT(security_code) DO UPDATE SET
                        latest_date=excluded.latest_date,
                        updated_at=excluded.updated_at,
                        status='ready'
                    """,
                    (code, latest, now),
                )
            conn.execute("DELETE FROM qfq_sync_failure WHERE security_code=?", (code,))
            completed.append(code)
        return inserted, completed

    @staticmethod
    def _record_failures(
        conn: sqlite3.Connection, codes: list[str], error: Exception | str, now: str
    ) -> None:
        message = str(error)
        conn.executemany(
            """
            INSERT INTO qfq_sync_failure VALUES (?, ?, ?)
            ON CONFLICT(security_code) DO UPDATE SET
                error=excluded.error, updated_at=excluded.updated_at
            """,
            [(code, message, now) for code in codes],
        )

    def _finalize(self, conn: sqlite3.Connection, plan: QfqBuildPlan, now: str) -> int:
        row_count = 0
        for code in self.universe["security_code"]:
            row_count += conn.execute(
                f'SELECT COUNT(*) FROM "{code_to_table(code)}"'
            ).fetchone()[0]
        stat = self.pool_db.stat()
        conn.execute(
            """
            INSERT OR REPLACE INTO price_database_metadata VALUES (
                1, ?, 'forward', 'CPS:2', ?, ?, ?, ?, ?, ?, ?, ?,
                0, 0, 'vendor_value', ?, ?, 'stock_pool.db'
            )
            """,
            (
                BUILDER_VERSION, str(self.pool_db.resolve()), stat.st_size, stat.st_mtime_ns,
                plan.start_date.isoformat(), plan.end_date.isoformat(), len(self.universe),
                row_count, now, self.pool_id, self.pool_as_of,
            ),
        )
        conn.execute(
            """
            UPDATE qfq_build_state SET status='complete', completed_count=?,
                rows_inserted=?, updated_at=? WHERE state_id=1
            """,
            (len(self.universe), row_count, now),
        )
        conn.commit()
        check = conn.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            raise QfqSyncError(f"构建数据库完整性检查失败：{check}")
        return row_count

    @staticmethod
    def _strip_build_tables(conn: sqlite3.Connection) -> None:
        for table in BUILD_ONLY_TABLES:
            conn.execute(f'DROP TABLE IF EXISTS "{table}"')
        conn.commit()
        check = conn.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            raise QfqSyncError(f"精简数据库完整性检查失败：{check}")

    def _publish_ready_row_count(
        self, conn: sqlite3.Connection, plan: QfqBuildPlan
    ) -> int | None:
        metadata_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='price_database_metadata'"
        ).fetchone()
        if not metadata_table:
            return None
        metadata = conn.execute("SELECT * FROM price_database_metadata WHERE metadata_id=1").fetchone()
        if metadata is None:
            return None
        matches = (
            metadata["builder_version"] == BUILDER_VERSION
            and metadata["adjustment"] == "forward"
            and metadata["ifind_params"] == "CPS:2"
            and metadata["start_date"] == plan.start_date.isoformat()
            and metadata["end_date"] == plan.end_date.isoformat()
            and metadata["universe_name"] == self.pool_id
            and metadata["universe_as_of"] == self.pool_as_of
            and metadata["security_count"] == len(self.universe)
        )
        membership_count = conn.execute(
            "SELECT COUNT(*) FROM price_universe_membership"
        ).fetchone()[0]
        stock_table_count = len(StockDataUpdater._stock_tables(conn))
        if not matches or membership_count != len(self.universe) or stock_table_count != len(self.universe):
            return None
        return int(metadata["row_count"])

    def _publish(self) -> None:
        sidecars = [
            self.target_db.with_name(f"{self.target_db.name}-wal"),
            self.target_db.with_name(f"{self.target_db.name}-shm"),
        ]
        active_sidecars = [str(path) for path in sidecars if path.exists()]
        if active_sidecars:
            raise QfqSyncError(
                "目标前复权库仍有SQLite WAL连接，停止占用进程后重新运行以发布："
                + ", ".join(active_sidecars)
            )
        os.replace(self.work_db, self.target_db)

    def build(
        self,
        *,
        start_date: date,
        end_date: date,
        batch_size: int = 10,
        max_failures: int = 50,
        progress: Callable[[dict], None] | None = None,
    ) -> dict:
        if batch_size < 1 or max_failures < 1:
            raise ValueError("batch_size 和 max_failures 必须大于0")
        plan = self.plan(start_date=start_date, end_date=end_date)
        from datetime import datetime
        from zoneinfo import ZoneInfo

        def current_time() -> str:
            return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")

        lock_path = self.work_db.with_name(f".{self.work_db.stem}.lock")
        with UpdateProcessLock(lock_path):
            self._initialize_work_db(plan, current_time())
            with closing(self._connect(self.work_db)) as conn:
                state_table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='qfq_build_state'"
                ).fetchone()
                state = (
                    conn.execute("SELECT * FROM qfq_build_state WHERE state_id=1").fetchone()
                    if state_table else None
                )
                if state is None:
                    ready_row_count = self._publish_ready_row_count(conn, plan)
                    if ready_row_count is None:
                        raise QfqSyncError("构建数据库缺少有效构建状态")
                    row_count = ready_row_count
                    publish_ready = True
                else:
                    publish_ready = False
                if publish_ready:
                    pass
                else:
                    expected_state = (
                        BUILDER_VERSION,
                        self.pool_id,
                        self.pool_as_of,
                        start_date.isoformat(),
                        end_date.isoformat(),
                    )
                    actual_state = (
                        state["builder_version"],
                        state["pool_id"],
                        state["pool_as_of"],
                        state["start_date"],
                        state["end_date"],
                    )
                    if actual_state != expected_state:
                        raise QfqSyncError(
                            "building 数据库的股池或日期参数与本次运行不一致"
                        )
                    self._reconcile_universe(conn, plan, current_time())
                    state = conn.execute("SELECT * FROM qfq_build_state WHERE state_id=1").fetchone()
                    if state and state["status"] == "complete":
                        row_count = state["rows_inserted"]
                    else:
                        groups = self._pending_groups(conn, plan, current_time())
                        completed_count = conn.execute(
                            "SELECT COUNT(*) FROM qfq_security_progress WHERE checked_through>=?",
                            (end_date.isoformat(),),
                        ).fetchone()[0]
                        inserted_total = 0
                        failure_count = 0
                        for request_start in sorted(groups):
                            codes = groups[request_start]
                            for offset in range(0, len(codes), batch_size):
                                batch = codes[offset : offset + batch_size]
                                now = current_time()
                                batch_inserted = 0
                                try:
                                    frame = self.fetch(batch, request_start, end_date)
                                    inserted, completed = self._save_frame(
                                        conn, frame, batch, plan, now
                                    )
                                    batch_inserted = inserted
                                    missing = sorted(set(batch).difference(completed))
                                    if missing:
                                        self._record_failures(conn, missing, "iFinD 未返回该证券", now)
                                        failure_count += len(missing)
                                    completed_count += len(completed)
                                    inserted_total += inserted
                                except Exception as exc:
                                    self._record_failures(conn, batch, exc, now)
                                    failure_count += len(batch)
                                conn.execute(
                                    """
                                    UPDATE qfq_build_state SET status='running', completed_count=?,
                                        rows_inserted=rows_inserted+?, updated_at=? WHERE state_id=1
                                    """,
                                    (completed_count, batch_inserted, now),
                                )
                                conn.commit()
                                if progress:
                                    progress({
                                        "status": "running",
                                        "completed": completed_count,
                                        "total": len(self.universe),
                                        "rows_inserted_this_run": inserted_total,
                                        "failures_this_run": failure_count,
                                        "last_batch": batch,
                                    })
                                if failure_count >= max_failures:
                                    break
                            if failure_count >= max_failures:
                                break
                        remaining = len(self.universe) - conn.execute(
                            "SELECT COUNT(*) FROM qfq_security_progress WHERE checked_through>=?",
                            (end_date.isoformat(),),
                        ).fetchone()[0]
                        if remaining:
                            conn.execute(
                                "UPDATE qfq_build_state SET status='partial', updated_at=? WHERE state_id=1",
                                (current_time(),),
                            )
                            conn.commit()
                            return {
                                **plan.as_dict(),
                                "status": "partial",
                                "published": False,
                                "remaining_security_count": remaining,
                                "failures_this_run": failure_count,
                                "rows_inserted_this_run": inserted_total,
                            }
                        row_count = self._finalize(conn, plan, current_time())
                    self._strip_build_tables(conn)
            try:
                self._publish()
            except (OSError, QfqSyncError) as exc:
                return {
                    **plan.as_dict(),
                    "status": "complete_not_published",
                    "published": False,
                    "row_count": row_count,
                    "error": f"{type(exc).__name__}: {exc}",
                }
        return {
            **plan.as_dict(),
            "status": "success",
            "published": True,
            "row_count": row_count,
            "remaining_security_count": 0,
        }
