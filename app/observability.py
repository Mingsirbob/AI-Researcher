from __future__ import annotations

import json
import logging
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic

from .runtime_events import current_run_context
from .migrations import apply_migration


logger = logging.getLogger("ai_researcher.ifind")


ERROR_CODE_CATEGORIES = {
    -2: "authentication",
    -340: "environment_or_network",
}


def classify_ifind_error(error: BaseException | str, error_code: int | None = None) -> str:
    if error_code in ERROR_CODE_CATEGORIES:
        return ERROR_CODE_CATEGORIES[error_code]
    message = str(error).lower()
    if any(token in message for token in ("timeout", "timed out", "超时")):
        return "timeout"
    if any(token in message for token in ("限频", "频率", "rate limit", "too many requests")):
        return "rate_limit"
    if any(token in message for token in ("账号", "密码", "login", "认证", "authentication")):
        return "authentication"
    if any(token in message for token in ("权限", "permission", "未授权", "unauthorized")):
        return "permission"
    if any(token in message for token in ("network", "connection", "连接", "dns", "socket")):
        return "network"
    if any(token in message for token in ("字段", "schema", "contract", "dataframe")):
        return "contract"
    if any(token in message for token in ("quality", "ohlc", "异常行情", "数据质量")):
        return "data_quality"
    return "upstream"


class IFindCallObserver:
    def __init__(self, db_path: Path, secrets: tuple[str, ...] = ()):
        self.db_path = db_path
        self.secrets = tuple(secret for secret in secrets if secret)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def _initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        apply_migration(self.connect, "0006_ifind_observability", self._create_schema)

    def _create_schema(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ifind_call_events (
                    call_id TEXT PRIMARY KEY,
                    occurred_at TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    status TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    attempt INTEGER NOT NULL,
                    requested_items INTEGER NOT NULL,
                    rows_returned INTEGER,
                    error_code INTEGER,
                    error_category TEXT,
                    error_message TEXT
                )
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(ifind_call_events)")}
            for name in ("root_run_id", "step_run_id", "runtime_call_id"):
                if name not in columns:
                    conn.execute(f"ALTER TABLE ifind_call_events ADD COLUMN {name} TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ifind_call_events_time "
                "ON ifind_call_events(occurred_at DESC)"
            )

    def _redact(self, value: str | None) -> str | None:
        if value is None:
            return None
        redacted = value
        for secret in self.secrets:
            redacted = redacted.replace(secret, "[REDACTED]")
        redacted = re.sub(
            r"(?i)(password|passwd|token|api[_-]?key)\s*[:=]\s*[^\s,;]+",
            r"\1=[REDACTED]",
            redacted,
        )
        return redacted[:500]

    def record(
        self,
        *,
        operation: str,
        status: str,
        started_at: float,
        attempt: int = 1,
        requested_items: int = 0,
        rows_returned: int | None = None,
        error_code: int | None = None,
        error: BaseException | str | None = None,
        error_category: str | None = None,
        runtime_call_id: str | None = None,
    ) -> None:
        duration_ms = max(0, round((monotonic() - started_at) * 1000))
        message = self._redact(str(error)) if error is not None else None
        category = error_category or (
            classify_ifind_error(error or "", error_code) if status == "error" else None
        )
        context = current_run_context()
        event = {
            "call_id": str(uuid.uuid4()),
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "operation": operation,
            "status": status,
            "duration_ms": duration_ms,
            "attempt": attempt,
            "requested_items": requested_items,
            "rows_returned": rows_returned,
            "error_code": error_code,
            "error_category": category,
            "error_message": message,
            "root_run_id": context.root_run_id if context else None,
            "step_run_id": context.step_run_id if context else None,
            "runtime_call_id": runtime_call_id,
        }
        try:
            with self.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO ifind_call_events (
                        call_id, occurred_at, operation, status, duration_ms, attempt,
                        requested_items, rows_returned, error_code, error_category, error_message,
                        root_run_id, step_run_id, runtime_call_id
                    ) VALUES (
                        :call_id, :occurred_at, :operation, :status, :duration_ms, :attempt,
                        :requested_items, :rows_returned, :error_code, :error_category, :error_message,
                        :root_run_id, :step_run_id, :runtime_call_id
                    )
                    """,
                    event,
                )
        except sqlite3.Error:
            logger.exception("failed to persist iFinD call event")
        log_payload = {key: value for key, value in event.items() if key != "error_message" or value}
        if status == "error":
            logger.warning("ifind_call %s", json.dumps(log_payload, ensure_ascii=False))
        else:
            logger.info("ifind_call %s", json.dumps(log_payload, ensure_ascii=False))

    def summary(self, hours: int = 24) -> dict:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        try:
            with self.connect() as conn:
                totals = conn.execute(
                    """
                    SELECT COUNT(*) AS calls,
                           SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS successes,
                           SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS errors,
                           COALESCE(ROUND(AVG(duration_ms)), 0) AS average_duration_ms
                    FROM ifind_call_events WHERE occurred_at >= ?
                    """,
                    (since,),
                ).fetchone()
                last = conn.execute(
                    """
                    SELECT occurred_at, operation, status, duration_ms, rows_returned,
                           error_code, error_category
                    FROM ifind_call_events ORDER BY occurred_at DESC LIMIT 1
                    """
                ).fetchone()
                categories = {
                    row[0]: row[1]
                    for row in conn.execute(
                        """
                        SELECT error_category, COUNT(*) FROM ifind_call_events
                        WHERE occurred_at >= ? AND status = 'error'
                        GROUP BY error_category
                        """,
                        (since,),
                    )
                }
        except sqlite3.Error as exc:
            logger.exception("failed to read iFinD observability summary")
            return {"available": False, "reason": type(exc).__name__}
        return {
            "available": True,
            "window_hours": hours,
            "calls": int(totals["calls"] or 0),
            "successes": int(totals["successes"] or 0),
            "errors": int(totals["errors"] or 0),
            "average_duration_ms": int(totals["average_duration_ms"] or 0),
            "error_categories": categories,
            "last_call": dict(last) if last else None,
        }
