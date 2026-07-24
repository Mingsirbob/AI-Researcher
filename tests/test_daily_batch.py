import asyncio

from app.daily_batch import DAILY_BATCH_STEPS, DailyBatchRunner, DailyBatchStore
from app.research_store import ResearchStore


def batch_store(tmp_path):
    research_store = ResearchStore(tmp_path / "state.db", tmp_path / "documents")
    return DailyBatchStore(research_store)


def handlers(calls, *, fail_step=None):
    result = {}
    for name in DAILY_BATCH_STEPS:
        async def run(step=name):
            calls.append(step)
            if step == fail_step:
                raise RuntimeError(f"{step} failed")
            return {"step": step, "artifact_id": f"artifact-{step}"}

        result[name] = run
    return result


def test_daily_batch_completes_all_steps_and_reuses_same_contract(tmp_path):
    store = batch_store(tmp_path)
    batch, should_start = store.prepare(
        account_id="account-1",
        as_of="2026-07-23",
        config={"top_n": 5},
    )
    assert should_start is True

    calls = []
    completed = asyncio.run(DailyBatchRunner(store).run(batch["batch_id"], handlers(calls)))

    assert completed["status"] == "completed"
    assert calls == list(DAILY_BATCH_STEPS)
    assert all(step["status"] == "completed" for step in completed["steps"])
    runtime = store.events.run(batch["batch_id"])
    events = store.events.events(batch["batch_id"])
    assert runtime["status"] == "completed"
    assert len(runtime["steps"]) == len(DAILY_BATCH_STEPS)
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    assert [event["event_type"] for event in events].count("step_started") == 6
    assert [event["event_type"] for event in events].count("step_completed") == 6
    assert events[0]["event_type"] == "task_queued"
    assert events[-1]["event_type"] == "task_completed"
    reused, should_restart = store.prepare(
        account_id="account-1",
        as_of="2026-07-23",
        config={"top_n": 5},
    )
    assert should_restart is False
    assert reused["batch_id"] == batch["batch_id"]


def test_daily_batch_resumes_after_failed_step_without_repeating_completed_steps(tmp_path):
    store = batch_store(tmp_path)
    batch, _ = store.prepare(
        account_id="account-1",
        as_of="2026-07-23",
        config={"top_n": 5},
    )
    first_calls = []
    failed = asyncio.run(
        DailyBatchRunner(store).run(
            batch["batch_id"], handlers(first_calls, fail_step="current_shadow")
        )
    )
    assert failed["status"] == "failed"
    assert first_calls == ["market_data", "factor_snapshot", "current_shadow"]

    resumed, should_start = store.prepare(
        account_id="account-1",
        as_of="2026-07-23",
        config={"top_n": 5},
    )
    assert should_start is True
    second_calls = []
    completed = asyncio.run(
        DailyBatchRunner(store).run(resumed["batch_id"], handlers(second_calls))
    )

    assert completed["status"] == "completed"
    assert second_calls == [
        "current_shadow", "candidate_research", "holdings_review", "order_proposals"
    ]
    events = store.events.events(batch["batch_id"])
    event_types = [event["event_type"] for event in events]
    assert "step_failed" in event_types
    assert "task_failed" in event_types
    assert event_types.count("task_queued") == 2
    assert event_types.count("step_reused") == 2
    assert events[-1]["event_type"] == "task_completed"


def test_daily_batch_recovers_running_task_after_restart(tmp_path):
    store = batch_store(tmp_path)
    batch, _ = store.prepare(
        account_id="account-1",
        as_of="2026-07-23",
        config={"top_n": 5},
    )
    store.start_batch(batch["batch_id"])
    store.events.start_task(batch["batch_id"])

    recovered = batch_store(tmp_path)

    assert recovered.get(batch["batch_id"])["status"] == "failed"
    assert recovered.events.run(batch["batch_id"])["status"] == "failed"
    assert recovered.events.events(batch["batch_id"])[-1]["event_type"] == "task_failed"
