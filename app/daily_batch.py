from __future__ import annotations

import inspect
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from .primitives import canonical_hash, canonical_json
from .migrations import apply_migration
from .research_store import ResearchStore, utc_now
from .runtime_events import RunContext, RuntimeEventStore, bind_run_context
from .sqlite_store import migrate_legacy_tables


DAILY_BATCH_VERSION = "daily-research-batch-v1.3"
DAILY_BATCH_STEPS = (
    "market_data",
    "factor_snapshot",
    "current_shadow",
    "candidate_research",
    "holdings_review",
    "order_proposals",
)
PAPER_DAILY_BATCH_TABLES = ("paper_daily_batch", "paper_daily_batch_step")


class DailyBatchStore:
    def __init__(
        self,
        store: Any,
        *,
        event_store: RuntimeEventStore | None = None,
        legacy_store: ResearchStore | None = None,
    ):
        self.store = store
        self.events = event_store or RuntimeEventStore(store)
        self.legacy_store = legacy_store
        self._initialize()
        self._recover_interrupted()

    def _initialize(self) -> None:
        apply_migration(self.store.connect, "0004_daily_batch", self._create_schema)
        if self.legacy_store is not None:
            migrate_legacy_tables(
                target=self.store,
                source=self.legacy_store,
                migration_id="0015_split_paper_daily_batch_database",
                tables=PAPER_DAILY_BATCH_TABLES,
            )

    def _create_schema(self) -> None:
        with self.store.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS paper_daily_batch (
                    batch_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    batch_version TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_step TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    updated_at TEXT NOT NULL,
                    UNIQUE(account_id, as_of, batch_version, config_hash)
                );

                CREATE INDEX IF NOT EXISTS idx_paper_daily_batch_latest
                ON paper_daily_batch(account_id, as_of DESC, created_at DESC);

                CREATE TABLE IF NOT EXISTS paper_daily_batch_step (
                    batch_id TEXT NOT NULL,
                    step_name TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    PRIMARY KEY (batch_id, step_name),
                    FOREIGN KEY (batch_id) REFERENCES paper_daily_batch(batch_id) ON DELETE CASCADE
                );
                """
            )

    def _recover_interrupted(self) -> None:
        now = utc_now()
        with self.store.connect() as conn:
            conn.execute(
                """UPDATE paper_daily_batch
                   SET status='failed', error='服务重启中断，可重新运行后续步骤',
                       finished_at=?, updated_at=?
                   WHERE status IN ('queued', 'running')""",
                (now, now),
            )
            conn.execute(
                """UPDATE paper_daily_batch_step
                   SET status='failed', error='服务重启中断', finished_at=?
                   WHERE status='running'""",
                (now,),
            )
        self.events.recover_interrupted(domain_type="paper_daily_batch")

    @staticmethod
    def _decode(row, steps: list[dict] | None = None) -> dict:
        item = dict(row)
        item["config"] = json.loads(item.pop("config_json"))
        item["steps"] = steps or []
        return item

    def get(self, batch_id: str) -> dict | None:
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT * FROM paper_daily_batch WHERE batch_id=?", (batch_id,)
            ).fetchone()
            if row is None:
                return None
            step_rows = conn.execute(
                """SELECT * FROM paper_daily_batch_step
                   WHERE batch_id=? ORDER BY ordinal""",
                (batch_id,),
            ).fetchall()
        steps = []
        for step_row in step_rows:
            step = dict(step_row)
            step["result"] = json.loads(step.pop("result_json")) if step["result_json"] else None
            steps.append(step)
        return self._decode(row, steps)

    def latest(self, account_id: str, as_of: str | None = None) -> dict | None:
        where = "account_id=?"
        params: list[Any] = [account_id]
        if as_of:
            where += " AND as_of=?"
            params.append(as_of)
        with self.store.connect() as conn:
            row = conn.execute(
                f"""SELECT batch_id FROM paper_daily_batch WHERE {where}
                    ORDER BY as_of DESC, created_at DESC LIMIT 1""",
                params,
            ).fetchone()
        return self.get(row[0]) if row else None

    def prepare(self, *, account_id: str, as_of: str, config: dict) -> tuple[dict, bool]:
        config_json = canonical_json(config)
        config_hash = canonical_hash(config)
        now = utc_now()
        with self.store.connect() as conn:
            row = conn.execute(
                """SELECT batch_id, status FROM paper_daily_batch
                   WHERE account_id=? AND as_of=? AND batch_version=? AND config_hash=?""",
                (account_id, as_of, DAILY_BATCH_VERSION, config_hash),
            ).fetchone()
            if row is None:
                batch_id = str(uuid.uuid4())
                conn.execute(
                    """INSERT INTO paper_daily_batch
                       (batch_id, account_id, as_of, batch_version, config_hash, config_json,
                        status, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)""",
                    (batch_id, account_id, as_of, DAILY_BATCH_VERSION, config_hash,
                     config_json, now, now),
                )
                conn.executemany(
                    """INSERT INTO paper_daily_batch_step
                       (batch_id, step_name, ordinal, status) VALUES (?, ?, ?, 'pending')""",
                    [(batch_id, name, ordinal) for ordinal, name in enumerate(DAILY_BATCH_STEPS, 1)],
                )
                should_start = True
            else:
                batch_id = row["batch_id"]
                if row["status"] in {"queued", "running", "completed"}:
                    should_start = False
                else:
                    conn.execute(
                        """UPDATE paper_daily_batch
                           SET status='queued', current_step=NULL, error=NULL,
                               finished_at=NULL, updated_at=? WHERE batch_id=?""",
                        (now, batch_id),
                    )
                    conn.execute(
                        """UPDATE paper_daily_batch_step
                           SET status='pending', error=NULL, started_at=NULL, finished_at=NULL
                           WHERE batch_id=? AND status='failed'""",
                        (batch_id,),
                    )
                    should_start = True
        contract_hash = canonical_hash({
            "batch_version": DAILY_BATCH_VERSION,
            "steps": DAILY_BATCH_STEPS,
        })
        runtime = self.events.prepare_run(
            root_run_id=batch_id,
            task_key="daily_research",
            contract_version=DAILY_BATCH_VERSION,
            contract_hash=contract_hash,
            input_payload={"account_id": account_id, "as_of": as_of, "config": config},
            domain_type="paper_daily_batch",
            domain_id=batch_id,
        )
        if should_start and runtime["status"] == "failed":
            self.events.requeue_run(batch_id)
        return self.get(batch_id), should_start

    def start_batch(self, batch_id: str) -> None:
        now = utc_now()
        with self.store.connect() as conn:
            conn.execute(
                """UPDATE paper_daily_batch SET status='running',
                   started_at=COALESCE(started_at, ?), finished_at=NULL, updated_at=?
                   WHERE batch_id=?""",
                (now, now, batch_id),
            )

    def start_step(self, batch_id: str, step_name: str) -> None:
        now = utc_now()
        with self.store.connect() as conn:
            conn.execute(
                """UPDATE paper_daily_batch_step
                   SET status='running', result_json=NULL, error=NULL,
                       started_at=?, finished_at=NULL
                   WHERE batch_id=? AND step_name=?""",
                (now, batch_id, step_name),
            )
            conn.execute(
                """UPDATE paper_daily_batch SET current_step=?, updated_at=? WHERE batch_id=?""",
                (step_name, now, batch_id),
            )

    def complete_step(self, batch_id: str, step_name: str, result: dict) -> None:
        now = utc_now()
        with self.store.connect() as conn:
            conn.execute(
                """UPDATE paper_daily_batch_step
                   SET status='completed', result_json=?, error=NULL, finished_at=?
                   WHERE batch_id=? AND step_name=?""",
                (canonical_json(result), now, batch_id, step_name),
            )
            conn.execute(
                "UPDATE paper_daily_batch SET updated_at=? WHERE batch_id=?",
                (now, batch_id),
            )

    def fail(self, batch_id: str, step_name: str, error: str) -> None:
        now = utc_now()
        message = error[:2000]
        with self.store.connect() as conn:
            conn.execute(
                """UPDATE paper_daily_batch_step
                   SET status='failed', error=?, finished_at=?
                   WHERE batch_id=? AND step_name=?""",
                (message, now, batch_id, step_name),
            )
            conn.execute(
                """UPDATE paper_daily_batch SET status='failed', current_step=?, error=?,
                   finished_at=?, updated_at=? WHERE batch_id=?""",
                (step_name, message, now, now, batch_id),
            )

    def complete_batch(self, batch_id: str) -> None:
        now = utc_now()
        with self.store.connect() as conn:
            conn.execute(
                """UPDATE paper_daily_batch SET status='completed', current_step=NULL,
                   error=NULL, finished_at=?, updated_at=? WHERE batch_id=?""",
                (now, now, batch_id),
            )


StepHandler = Callable[[], dict | Awaitable[dict]]


class DailyBatchRunner:
    def __init__(self, store: DailyBatchStore):
        self.store = store
        self.events = store.events

    async def run(self, batch_id: str, handlers: dict[str, StepHandler]) -> dict:
        self.store.start_batch(batch_id)
        self.events.start_task(batch_id)
        batch = self.store.get(batch_id)
        if batch is None:
            raise KeyError("每日研究批次不存在")
        completed = {step["step_name"] for step in batch["steps"] if step["status"] == "completed"}
        root_context = RunContext(self.events, batch_id)
        with bind_run_context(root_context):
            for ordinal, step_name in enumerate(DAILY_BATCH_STEPS, 1):
                if step_name in completed:
                    self.events.reuse_step(batch_id, step_name, ordinal)
                    continue
                self.store.start_step(batch_id, step_name)
                step_run_id = self.events.start_step(batch_id, step_name, ordinal)
                try:
                    with bind_run_context(root_context, step_run_id=step_run_id):
                        value = handlers[step_name]()
                        result = await value if inspect.isawaitable(value) else value
                    if not isinstance(result, dict):
                        raise TypeError(f"步骤 {step_name} 必须返回对象")
                    self.store.complete_step(batch_id, step_name, result)
                    self.events.complete_step(batch_id, step_run_id, result)
                except Exception as exc:
                    self.store.fail(batch_id, step_name, f"{type(exc).__name__}: {exc}")
                    self.events.fail_step(batch_id, step_run_id, exc)
                    self.events.fail_task(batch_id, exc)
                    return self.store.get(batch_id)
            self.store.complete_batch(batch_id)
            self.events.complete_task(batch_id)
        return self.store.get(batch_id)
