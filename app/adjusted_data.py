from __future__ import annotations

import inspect
import json
import os
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import pandas as pd

from .resilience import (
    CallInProgressError,
    CallTimeoutError,
    CircuitOpenError,
    backoff_seconds,
)
from .updater import (
    ADJUSTMENT_PARAMS,
    DataContractError,
    StockDataUpdater,
    StockUpdateError,
    UpdateProcessLock,
    code_to_table,
    table_to_code,
    validate_daily_frame,
)


BUILDER_VERSION = "adjusted-daily-v1"
ADJUSTED_KINDS = {"backward", "forward"}


@dataclass(frozen=True)
class AdjustedBuildPlan:
    adjustment: str
    ifind_params: str
    source_db: Path
    target_db: Path
    start_date: date
    end_date: date
    security_count: int
    source_min_date: date
    source_max_date: date

    def as_dict(self) -> dict:
        return {
            "builder_version": BUILDER_VERSION,
            "mode": "full_rebuild",
            "adjustment": self.adjustment,
            "ifind_params": self.ifind_params,
            "source_db": str(self.source_db),
            "target_db": str(self.target_db),
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "security_count": self.security_count,
            "source_min_date": self.source_min_date.isoformat(),
            "source_max_date": self.source_max_date.isoformat(),
        }


class AdjustedStockDataBuilder:
    """Build a separately versioned adjusted-price database from iFinD."""

    def __init__(
        self,
        source_db: Path,
        target_db: Path,
        fetch: Callable[..., pd.DataFrame],
        *,
        adjustment: str,
    ):
        if adjustment not in ADJUSTED_KINDS:
            raise ValueError("复权库仅支持 backward 或 forward")
        if source_db.resolve() == target_db.resolve():
            raise ValueError("复权库不能覆盖不复权源数据库")
        self.source_db = source_db
        self.target_db = target_db
        self.fetch = fetch
        self.adjustment = adjustment
        self.fetch_supports_attempt = "attempt" in inspect.signature(fetch).parameters

    @staticmethod
    def _source_inventory(source_db: Path) -> tuple[list[str], date, date]:
        if not source_db.exists():
            raise StockUpdateError(f"不复权源数据库不存在：{source_db}")
        minimums: list[date] = []
        maximums: list[date] = []
        with sqlite3.connect(source_db) as conn:
            tables = StockDataUpdater._stock_tables(conn)
            for table in tables:
                row = conn.execute(f'SELECT MIN(time), MAX(time) FROM "{table}"').fetchone()
                if row and row[0] and row[1]:
                    minimums.append(date.fromisoformat(row[0]))
                    maximums.append(date.fromisoformat(row[1]))
        if not tables or not minimums:
            raise StockUpdateError("不复权源数据库没有可用的股票日线表")
        return tables, min(minimums), max(maximums)

    def plan(self, end_date: date, start_date: date | None = None) -> AdjustedBuildPlan:
        tables, source_min, source_max = self._source_inventory(self.source_db)
        effective_start = start_date or source_min
        if effective_start > end_date:
            raise ValueError("start_date 不能晚于 end_date")
        return AdjustedBuildPlan(
            adjustment=self.adjustment,
            ifind_params=ADJUSTMENT_PARAMS[self.adjustment],
            source_db=self.source_db,
            target_db=self.target_db,
            start_date=effective_start,
            end_date=end_date,
            security_count=len(tables),
            source_min_date=source_min,
            source_max_date=source_max,
        )

    @staticmethod
    def _create_schema(conn: sqlite3.Connection, tables: list[str]) -> None:
        for table in tables:
            conn.execute(
                f"""
                CREATE TABLE "{table}" (
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
        StockDataUpdater._initialize_journal(conn)
        conn.execute(
            """
            CREATE TABLE price_database_metadata (
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
                built_at TEXT NOT NULL
            )
            """
        )

    @staticmethod
    def _insert_frame(conn: sqlite3.Connection, frame: pd.DataFrame) -> int:
        inserted = 0
        for code, group in frame.groupby("thscode", sort=False):
            rows = [
                (
                    record["time"],
                    StockDataUpdater._number_or_none(record["open"]),
                    StockDataUpdater._number_or_none(record["high"]),
                    StockDataUpdater._number_or_none(record["low"]),
                    StockDataUpdater._number_or_none(record["close"]),
                    StockDataUpdater._number_or_none(record["vwap"]),
                    StockDataUpdater._number_or_none(record["volume"]),
                )
                for record in group.to_dict(orient="records")
            ]
            before = conn.total_changes
            conn.executemany(
                f'INSERT INTO "{code_to_table(code)}" '
                "(time, open, high, low, close, vwap, volume) VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            inserted += conn.total_changes - before
        return inserted

    def build(
        self,
        *,
        end_date: date,
        start_date: date | None = None,
        batch_size: int = 20,
        max_retries: int = 3,
        retry_delay: float = 0.5,
    ) -> dict:
        if batch_size < 1 or max_retries < 1 or retry_delay < 0:
            raise ValueError("batch_size/max_retries 必须大于 0，retry_delay 不能小于 0")
        plan = self.plan(end_date=end_date, start_date=start_date)
        tables, _, _ = self._source_inventory(self.source_db)
        codes = [table_to_code(table) for table in tables]
        run_id = str(uuid.uuid4())
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        temp_db = self.target_db.with_name(f".{self.target_db.name}.{run_id}.building")
        lock_path = self.target_db.parent / f".{self.target_db.stem}.lock"
        self.target_db.parent.mkdir(parents=True, exist_ok=True)
        rows_received = 0
        rows_inserted = 0
        failures: dict[str, str] = {}
        quality_failed_codes: set[str] = set()

        try:
            with UpdateProcessLock(lock_path), closing(sqlite3.connect(temp_db)) as conn:
                self._create_schema(conn, tables)
                conn.execute(
                    """
                    INSERT INTO data_update_runs (
                        run_id, source, adjustment, started_at, start_date, end_date, status
                    ) VALUES (?, 'iFinD', ?, ?, ?, ?, 'running')
                    """,
                    (
                        run_id,
                        self.adjustment,
                        now.isoformat(),
                        plan.start_date.isoformat(),
                        plan.end_date.isoformat(),
                    ),
                )
                conn.commit()

                def fetch_with_retry(batch: list[str]) -> pd.DataFrame:
                    error: Exception | None = None
                    for attempt in range(1, max_retries + 1):
                        try:
                            if self.fetch_supports_attempt:
                                return self.fetch(
                                    batch, plan.start_date, plan.end_date, attempt=attempt
                                )
                            return self.fetch(batch, plan.start_date, plan.end_date)
                        except (DataContractError, CircuitOpenError, CallTimeoutError, CallInProgressError):
                            raise
                        except Exception as exc:
                            error = exc
                            if attempt < max_retries:
                                time.sleep(
                                    backoff_seconds(attempt, retry_delay, retry_delay * 8)
                                )
                    raise StockUpdateError(str(error))

                def process_batch(batch: list[str]) -> None:
                    nonlocal rows_received, rows_inserted
                    try:
                        frame = fetch_with_retry(batch)
                    except (DataContractError, CircuitOpenError, CallTimeoutError, CallInProgressError):
                        raise
                    except Exception as exc:
                        if len(batch) == 1:
                            failures[batch[0]] = f"{type(exc).__name__}: {exc}"
                            return
                        midpoint = len(batch) // 2
                        process_batch(batch[:midpoint])
                        process_batch(batch[midpoint:])
                        return

                    returned_codes = set(frame["thscode"])
                    missing = set(batch).difference(returned_codes)
                    if missing:
                        if len(batch) == 1:
                            failures[batch[0]] = "iFinD 未返回该证券的复权日线"
                            return
                        midpoint = len(batch) // 2
                        process_batch(batch[:midpoint])
                        process_batch(batch[midpoint:])
                        return

                    rows_received += len(frame)
                    issues = validate_daily_frame(
                        frame,
                        requested_codes=set(batch),
                        start=plan.start_date,
                        end=plan.end_date,
                        previous_closes={code: None for code in batch},
                    )
                    invalid_codes = {issue.code for issue in issues}
                    if issues:
                        StockDataUpdater._record_quality_issues(conn, run_id, issues)
                        quality_failed_codes.update(invalid_codes)
                    valid_frame = frame.loc[~frame["thscode"].isin(invalid_codes)]
                    rows_inserted += self._insert_frame(conn, valid_frame)

                for index in range(0, len(codes), batch_size):
                    process_batch(codes[index : index + batch_size])

                status = "success" if not failures and not quality_failed_codes else "partial"
                finished_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
                conn.execute(
                    """
                    UPDATE data_update_runs SET finished_at=?, status=?, rows_received=?,
                        rows_inserted=?, failed_codes=?, failed_reasons=?, quality_issue_count=?,
                        quality_failed_codes=? WHERE run_id=?
                    """,
                    (
                        finished_at,
                        status,
                        rows_received,
                        rows_inserted,
                        json.dumps(sorted(failures), ensure_ascii=False),
                        json.dumps(failures, ensure_ascii=False),
                        conn.execute(
                            "SELECT COUNT(*) FROM data_quality_issues WHERE run_id=?", (run_id,)
                        ).fetchone()[0],
                        json.dumps(sorted(quality_failed_codes), ensure_ascii=False),
                        run_id,
                    ),
                )
                if status == "success":
                    stat = self.source_db.stat()
                    conn.execute(
                        """
                        INSERT INTO price_database_metadata VALUES
                        (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            BUILDER_VERSION,
                            self.adjustment,
                            ADJUSTMENT_PARAMS[self.adjustment],
                            str(self.source_db.resolve()),
                            stat.st_size,
                            stat.st_mtime_ns,
                            plan.start_date.isoformat(),
                            plan.end_date.isoformat(),
                            len(codes),
                            rows_inserted,
                            finished_at,
                        ),
                    )
                conn.commit()

            published = status == "success"
            if published:
                os.replace(temp_db, self.target_db)
            return {
                **plan.as_dict(),
                "run_id": run_id,
                "status": status,
                "published": published,
                "rows_received": rows_received,
                "rows_inserted": rows_inserted,
                "failed_codes": sorted(failures),
                "failed_reasons": failures,
                "quality_failed_codes": sorted(quality_failed_codes),
            }
        finally:
            temp_db.unlink(missing_ok=True)
