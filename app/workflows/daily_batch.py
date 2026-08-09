from __future__ import annotations

import inspect
import uuid
from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from typing import Any

from app.core.primitives import canonical_hash
from app.core.runtime_events import RunContext, RuntimeEventStore, bind_run_context
from app.paper.run_store import PaperRunStore
from app.research.store import utc_now


DAILY_BATCH_VERSION = "daily-research-batch-v2"
DAILY_BATCH_STEPS = (
    "market_data", "factor_snapshot", "current_shadow", "candidate_research",
    "holdings_review", "order_proposals",
)
STEP_FILES = {
    "market_data": "market_data.json",
    "factor_snapshot": "factor_snapshot.json",
    "current_shadow": "current_shadow.json",
    "candidate_research": "candidate_research.json",
    "holdings_review": "holdings_review.json",
    "order_proposals": "order_proposals.json",
}


def resolve_batch_date(
    requested: str | None, *, local_latest: str | None,
    trading_dates: Callable[[date, date], list[date]],
    default_target: date | None = None,
) -> str:
    if local_latest is not None:
        date.fromisoformat(local_latest)
    target = date.fromisoformat(requested) if requested else (default_target or date.today())
    confirmed = trading_dates(target - timedelta(days=31), target)
    if not confirmed:
        raise ValueError(f"{target.isoformat()} 之前没有可用交易日")
    return confirmed[-1].isoformat()


class DailyBatchStore:
    def __init__(
        self, run_store: PaperRunStore, *, account_resolver: Callable[[str], dict],
        event_store: RuntimeEventStore,
    ):
        self.run_store = run_store
        self.account_resolver = account_resolver
        self.events = event_store

    @staticmethod
    def _as_batch(manifest: dict) -> dict:
        return {
            "batch_id": manifest.get("batch_id"), "run_key": manifest["run_key"],
            "account_id": manifest["account_id"], "as_of": manifest["trading_date"],
            "batch_version": manifest.get("batch_version", DAILY_BATCH_VERSION),
            "config": manifest.get("config", {}), "status": manifest["status"],
            "current_step": manifest.get("current_step"), "error": manifest.get("error"),
            "created_at": manifest.get("created_at"), "started_at": manifest.get("started_at"),
            "finished_at": manifest.get("finished_at"), "updated_at": manifest.get("updated_at"),
            "steps": manifest.get("steps", []),
        }

    def get(self, batch_id: str) -> dict | None:
        matches = self.run_store.find(batch_id=batch_id)
        return self._as_batch(matches[0]) if matches else None

    def latest(self, account_id: str, as_of: str | None = None) -> dict | None:
        matches = self.run_store.find(account_id=account_id, trading_date=as_of)
        matches = [item for item in matches if item.get("batch_id")]
        return self._as_batch(matches[0]) if matches else None

    def prepare(self, *, account_id: str, as_of: str, config: dict) -> tuple[dict, bool]:
        config_hash = canonical_hash(config)
        existing = [
            item for item in self.run_store.find(account_id=account_id, trading_date=as_of)
            if item.get("batch_version") == DAILY_BATCH_VERSION
            and item.get("config_hash") == config_hash
        ]
        if existing and existing[0]["status"] in {"queued", "running", "completed"}:
            return self._as_batch(existing[0]), False
        if existing:
            item = existing[0]
            for step in item.get("steps", []):
                if step["status"] in {"failed", "running"}:
                    step.update(status="pending", error=None, started_at=None, finished_at=None)
            item = self.run_store.update_manifest(
                item["run_key"], status="queued", current_step=None, error=None,
                finished_at=None, steps=item.get("steps", []), updated_at=utc_now(),
            )
            runtime = self.events.run(item["batch_id"])
            if runtime and runtime["status"] == "failed":
                self.events.requeue_run(item["batch_id"])
            return self._as_batch(item), True
        account = self.account_resolver(account_id)
        batch_id = str(uuid.uuid4())
        steps = [
            {"step_name": name, "ordinal": ordinal, "status": "pending", "result": None,
             "error": None, "started_at": None, "finished_at": None}
            for ordinal, name in enumerate(DAILY_BATCH_STEPS, 1)
        ]
        manifest = self.run_store.create(
            account=account, trading_date=as_of,
            manifest={"batch_id": batch_id, "batch_version": DAILY_BATCH_VERSION,
                      "config_hash": config_hash, "config": config, "steps": steps,
                      "current_step": None, "updated_at": utc_now()},
        )
        contract_hash = canonical_hash({"batch_version": DAILY_BATCH_VERSION, "steps": DAILY_BATCH_STEPS})
        self.events.prepare_run(
            root_run_id=batch_id, task_key="daily_research",
            contract_version=DAILY_BATCH_VERSION, contract_hash=contract_hash,
            input_payload={"account_id": account_id, "as_of": as_of, "config": config},
            domain_type="paper_daily_batch", domain_id=batch_id,
        )
        return self._as_batch(manifest), True

    def _update(self, batch_id: str, mutate: Callable[[dict], None]) -> dict:
        batch = self.get(batch_id)
        if batch is None:
            raise KeyError("每日研究批次不存在")
        manifest = self.run_store.manifest(batch["run_key"])
        mutate(manifest)
        manifest["updated_at"] = utc_now()
        manifest.pop("run_key", None)
        return self.run_store.update_manifest(batch["run_key"], **manifest)

    def start_batch(self, batch_id: str) -> None:
        def mutate(item: dict) -> None:
            item.update(status="running", started_at=item.get("started_at") or utc_now(),
                        finished_at=None, error=None)
        self._update(batch_id, mutate)

    def start_step(self, batch_id: str, step_name: str) -> None:
        def mutate(item: dict) -> None:
            item["current_step"] = step_name
            step = next(value for value in item["steps"] if value["step_name"] == step_name)
            step.update(status="running", result=None, error=None, started_at=utc_now(), finished_at=None)
        self._update(batch_id, mutate)

    def complete_step(self, batch_id: str, step_name: str, result: dict) -> None:
        batch = self.get(batch_id)
        if batch is None:
            raise KeyError("每日研究批次不存在")
        self.run_store.write_result(batch["run_key"], STEP_FILES[step_name], result)
        def mutate(item: dict) -> None:
            step = next(value for value in item["steps"] if value["step_name"] == step_name)
            step.update(status="completed", result=result, error=None, finished_at=utc_now())
        self._update(batch_id, mutate)

    def fail(self, batch_id: str, step_name: str, error: str) -> None:
        message = error[:2000]
        def mutate(item: dict) -> None:
            item.update(status="failed", current_step=step_name, error=message, finished_at=utc_now())
            step = next(value for value in item["steps"] if value["step_name"] == step_name)
            step.update(status="failed", error=message, finished_at=utc_now())
        self._update(batch_id, mutate)

    def complete_batch(self, batch_id: str) -> None:
        def mutate(item: dict) -> None:
            item.update(status="completed", current_step=None, error=None, finished_at=utc_now())
        self._update(batch_id, mutate)


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
