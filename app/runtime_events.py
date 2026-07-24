from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterator

from .primitives import canonical_json, sha256_text
from .migrations import apply_migration
from .research_store import utc_now


EVENT_SCHEMA_VERSION = "research.event.v1"
MAX_EVENT_PAYLOAD_BYTES = 64 * 1024
SENSITIVE_KEYS = ("password", "passwd", "token", "api_key", "apikey", "authorization", "secret")
EVENT_TYPES = frozenset({
    "task_queued", "task_started", "task_completed", "task_failed",
    "step_started", "step_reused", "step_completed", "step_failed",
    "tool_started", "tool_retrying", "tool_completed", "tool_failed",
    "artifact_created", "gate_evaluated", "resource_changed",
    "human_reviewed", "order_proposed", "order_filled", "order_skipped",
})


def _canonical_json(value: Any) -> str:
    return canonical_json(value, default=str)


def _redact(value: Any, key: str = "") -> Any:
    lowered = key.lower()
    if any(item in lowered for item in SENSITIVE_KEYS):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(item_key): _redact(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return value[:2000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:2000]


def _safe_payload(payload: dict | None) -> tuple[str, str]:
    cleaned = _redact(payload or {})
    raw = _canonical_json(cleaned)
    if len(raw.encode("utf-8")) > MAX_EVENT_PAYLOAD_BYTES:
        full_hash = sha256_text(raw)
        cleaned = {
            "truncated": True,
            "original_sha256": full_hash,
            "keys": sorted(cleaned) if isinstance(cleaned, dict) else [],
        }
        raw = _canonical_json(cleaned)
    return raw, sha256_text(raw)


@dataclass(frozen=True)
class RunContext:
    event_store: "RuntimeEventStore"
    root_run_id: str
    step_run_id: str | None = None
    domain_run_id: str | None = None


_RUN_CONTEXT: ContextVar[RunContext | None] = ContextVar("research_run_context", default=None)


def current_run_context() -> RunContext | None:
    return _RUN_CONTEXT.get()


@contextmanager
def bind_run_context(context: RunContext, **changes: str | None) -> Iterator[RunContext]:
    active = replace(context, **changes) if changes else context
    token = _RUN_CONTEXT.set(active)
    try:
        yield active
    finally:
        _RUN_CONTEXT.reset(token)


class RuntimeEventStore:
    def __init__(self, store_or_path: Any):
        self.db_path = Path(getattr(store_or_path, "db_path", store_or_path))
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def _initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        apply_migration(self.connect, "0005_runtime_events", self._create_schema)

    def _create_schema(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runtime_run (
                    root_run_id TEXT PRIMARY KEY,
                    task_key TEXT NOT NULL,
                    contract_version TEXT NOT NULL,
                    contract_hash TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    domain_type TEXT,
                    domain_id TEXT,
                    next_seq INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    UNIQUE(task_key, contract_version, contract_hash, input_hash)
                );

                CREATE TABLE IF NOT EXISTS runtime_step_run (
                    step_run_id TEXT PRIMARY KEY,
                    root_run_id TEXT NOT NULL,
                    step_key TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    attempt INTEGER NOT NULL,
                    tool_name TEXT NOT NULL,
                    tool_version TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_hash TEXT,
                    error_code TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    UNIQUE(root_run_id, step_key, attempt),
                    FOREIGN KEY(root_run_id) REFERENCES runtime_run(root_run_id)
                );

                CREATE TABLE IF NOT EXISTS runtime_event (
                    event_id TEXT PRIMARY KEY,
                    schema_version TEXT NOT NULL,
                    root_run_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    parent_run_id TEXT,
                    domain_run_id TEXT,
                    step_run_id TEXT,
                    call_id TEXT,
                    event_type TEXT NOT NULL,
                    status TEXT,
                    occurred_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    UNIQUE(root_run_id, seq),
                    FOREIGN KEY(root_run_id) REFERENCES runtime_run(root_run_id)
                );

                CREATE INDEX IF NOT EXISTS idx_runtime_event_run_seq
                ON runtime_event(root_run_id, seq);
                CREATE INDEX IF NOT EXISTS idx_runtime_event_call
                ON runtime_event(call_id) WHERE call_id IS NOT NULL;
                """
            )

    def prepare_run(
        self,
        *,
        root_run_id: str,
        task_key: str,
        contract_version: str,
        contract_hash: str,
        input_payload: dict,
        domain_type: str | None = None,
        domain_id: str | None = None,
    ) -> dict:
        input_json, input_hash = _safe_payload(input_payload)
        created = False
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM runtime_run WHERE root_run_id=?", (root_run_id,)
            ).fetchone()
            if row is None:
                now = utc_now()
                conn.execute(
                    """INSERT INTO runtime_run
                       (root_run_id, task_key, contract_version, contract_hash, input_json,
                        input_hash, status, domain_type, domain_id, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)""",
                    (root_run_id, task_key, contract_version, contract_hash, input_json,
                     input_hash, domain_type, domain_id, now),
                )
                created = True
        if created:
            self.append_event(
                root_run_id,
                "task_queued",
                status="queued",
                payload={"task_key": task_key, "contract_version": contract_version},
            )
        return self.run(root_run_id)

    def run(self, root_run_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM runtime_run WHERE root_run_id=?", (root_run_id,)
            ).fetchone()
            if row is None:
                return None
            steps = conn.execute(
                """SELECT * FROM runtime_step_run WHERE root_run_id=?
                   ORDER BY ordinal, attempt""",
                (root_run_id,),
            ).fetchall()
        item = dict(row)
        item["input"] = json.loads(item.pop("input_json"))
        item["steps"] = [dict(step) for step in steps]
        return item

    @staticmethod
    def _append_in_conn(
        conn: sqlite3.Connection,
        root_run_id: str,
        event_type: str,
        *,
        status: str | None = None,
        payload: dict | None = None,
        parent_run_id: str | None = None,
        domain_run_id: str | None = None,
        step_run_id: str | None = None,
        call_id: str | None = None,
    ) -> dict:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"未知运行事件类型: {event_type}")
        payload_json, payload_hash = _safe_payload(payload)
        row = conn.execute(
            "SELECT next_seq FROM runtime_run WHERE root_run_id=?", (root_run_id,)
        ).fetchone()
        if row is None:
            raise KeyError("运行事件根任务不存在")
        seq = int(row["next_seq"])
        conn.execute(
            "UPDATE runtime_run SET next_seq=? WHERE root_run_id=?",
            (seq + 1, root_run_id),
        )
        event = {
            "event_id": str(uuid.uuid4()),
            "schema_version": EVENT_SCHEMA_VERSION,
            "root_run_id": root_run_id,
            "seq": seq,
            "parent_run_id": parent_run_id,
            "domain_run_id": domain_run_id,
            "step_run_id": step_run_id,
            "call_id": call_id,
            "event_type": event_type,
            "status": status,
            "occurred_at": utc_now(),
            "payload_json": payload_json,
            "payload_hash": payload_hash,
        }
        conn.execute(
            """INSERT INTO runtime_event
               (event_id, schema_version, root_run_id, seq, parent_run_id, domain_run_id,
                step_run_id, call_id, event_type, status, occurred_at, payload_json, payload_hash)
               VALUES (:event_id, :schema_version, :root_run_id, :seq, :parent_run_id,
                       :domain_run_id, :step_run_id, :call_id, :event_type, :status,
                       :occurred_at, :payload_json, :payload_hash)""",
            event,
        )
        return event

    def append_event(self, root_run_id: str, event_type: str, **kwargs: Any) -> dict:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            event = self._append_in_conn(conn, root_run_id, event_type, **kwargs)
        result = dict(event)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    def events(self, root_run_id: str, *, after_seq: int = 0, limit: int = 200) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM runtime_event WHERE root_run_id=? AND seq>?
                   ORDER BY seq LIMIT ?""",
                (root_run_id, max(0, after_seq), min(max(1, limit), 1001)),
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            items.append(item)
        return items

    def requeue_run(self, root_run_id: str) -> None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE runtime_run SET status='queued', finished_at=NULL WHERE root_run_id=?",
                (root_run_id,),
            )
            self._append_in_conn(conn, root_run_id, "task_queued", status="queued",
                                 payload={"resumed": True})

    def recover_interrupted(self, *, domain_type: str | None = None) -> int:
        conditions = "status='running'"
        params: list[Any] = []
        if domain_type:
            conditions += " AND domain_type=?"
            params.append(domain_type)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT root_run_id FROM runtime_run WHERE {conditions}", params
            ).fetchall()
        for row in rows:
            self.fail_task(row["root_run_id"], "服务重启中断，可重新运行后续步骤")
        return len(rows)

    def start_task(self, root_run_id: str) -> None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = utc_now()
            conn.execute(
                """UPDATE runtime_run SET status='running', started_at=COALESCE(started_at, ?),
                   finished_at=NULL WHERE root_run_id=?""",
                (now, root_run_id),
            )
            self._append_in_conn(conn, root_run_id, "task_started", status="running")

    def complete_task(self, root_run_id: str) -> None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE runtime_run SET status='completed', finished_at=? WHERE root_run_id=?",
                (utc_now(), root_run_id),
            )
            self._append_in_conn(conn, root_run_id, "task_completed", status="completed")

    def fail_task(self, root_run_id: str, error: BaseException | str) -> None:
        message = str(error)[:2000]
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE runtime_run SET status='failed', finished_at=? WHERE root_run_id=?",
                (utc_now(), root_run_id),
            )
            self._append_in_conn(
                conn, root_run_id, "task_failed", status="failed",
                payload={"error_type": type(error).__name__, "message": message},
            )

    def start_step(self, root_run_id: str, step_key: str, ordinal: int) -> str:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            attempt = conn.execute(
                "SELECT COALESCE(MAX(attempt), 0) + 1 FROM runtime_step_run "
                "WHERE root_run_id=? AND step_key=?",
                (root_run_id, step_key),
            ).fetchone()[0]
            step_run_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO runtime_step_run
                   (step_run_id, root_run_id, step_key, ordinal, attempt, tool_name,
                    tool_version, input_hash, status, started_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'legacy-handler-v1', '', 'running', ?)""",
                (step_run_id, root_run_id, step_key, ordinal, attempt, step_key, utc_now()),
            )
            self._append_in_conn(
                conn, root_run_id, "step_started", status="running",
                step_run_id=step_run_id,
                payload={"step_key": step_key, "ordinal": ordinal, "attempt": attempt},
            )
        return step_run_id

    def reuse_step(self, root_run_id: str, step_key: str, ordinal: int) -> None:
        self.append_event(
            root_run_id, "step_reused", status="completed",
            payload={"step_key": step_key, "ordinal": ordinal},
        )

    def complete_step(self, root_run_id: str, step_run_id: str, result: dict) -> None:
        result_json = _canonical_json(_redact(result))
        result_hash = sha256_text(result_json)
        summary = {
            "step_key": None,
            "result_hash": result_hash,
            "result_keys": sorted(result),
            "identifiers": {
                key: value for key, value in result.items()
                if key.endswith("_id") or key in {"status", "as_of", "order_count", "completed", "failed"}
            },
        }
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT step_key FROM runtime_step_run WHERE step_run_id=?", (step_run_id,)
            ).fetchone()
            if row is None:
                raise KeyError("运行步骤不存在")
            summary["step_key"] = row["step_key"]
            conn.execute(
                """UPDATE runtime_step_run SET status='completed', result_hash=?, finished_at=?
                   WHERE step_run_id=?""",
                (result_hash, utc_now(), step_run_id),
            )
            self._append_in_conn(
                conn, root_run_id, "step_completed", status="completed",
                step_run_id=step_run_id, payload=summary,
            )

    def fail_step(self, root_run_id: str, step_run_id: str, error: BaseException) -> None:
        error_code = type(error).__name__
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT step_key FROM runtime_step_run WHERE step_run_id=?", (step_run_id,)
            ).fetchone()
            conn.execute(
                """UPDATE runtime_step_run SET status='failed', error_code=?, finished_at=?
                   WHERE step_run_id=?""",
                (error_code, utc_now(), step_run_id),
            )
            self._append_in_conn(
                conn, root_run_id, "step_failed", status="failed",
                step_run_id=step_run_id,
                payload={"step_key": row["step_key"] if row else None,
                         "error_type": error_code, "message": str(error)[:2000]},
            )


def begin_tool_event(tool_name: str, payload: dict | None = None) -> str | None:
    context = current_run_context()
    if context is None:
        return None
    call_id = str(uuid.uuid4())
    context.event_store.append_event(
        context.root_run_id, "tool_started", status="running",
        step_run_id=context.step_run_id, domain_run_id=context.domain_run_id,
        call_id=call_id, payload={"tool": tool_name, **(payload or {})},
    )
    return call_id


def record_tool_retry(call_id: str | None, tool_name: str, attempt: int, category: str) -> None:
    context = current_run_context()
    if context is None or call_id is None:
        return
    context.event_store.append_event(
        context.root_run_id, "tool_retrying", status="retrying",
        step_run_id=context.step_run_id, domain_run_id=context.domain_run_id,
        call_id=call_id,
        payload={"tool": tool_name, "attempt": attempt, "error_category": category},
    )


def complete_tool_event(call_id: str | None, tool_name: str, payload: dict | None = None) -> None:
    context = current_run_context()
    if context is None or call_id is None:
        return
    context.event_store.append_event(
        context.root_run_id, "tool_completed", status="completed",
        step_run_id=context.step_run_id, domain_run_id=context.domain_run_id,
        call_id=call_id, payload={"tool": tool_name, **(payload or {})},
    )


def fail_tool_event(
    call_id: str | None,
    tool_name: str,
    error: BaseException,
    *,
    category: str,
    attempt: int = 1,
) -> None:
    context = current_run_context()
    if context is None or call_id is None:
        return
    context.event_store.append_event(
        context.root_run_id, "tool_failed", status="failed",
        step_run_id=context.step_run_id, domain_run_id=context.domain_run_id,
        call_id=call_id,
        payload={"tool": tool_name, "attempt": attempt, "error_category": category,
                 "error_type": type(error).__name__, "message": str(error)[:2000]},
    )
