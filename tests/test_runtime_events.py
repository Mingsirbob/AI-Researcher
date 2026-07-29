import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from fastapi import HTTPException

from app.api.routers import system as system_router
from app.runtime_events import MAX_EVENT_PAYLOAD_BYTES, RuntimeEventStore


def prepared_store(tmp_path):
    store = RuntimeEventStore(tmp_path / "state.db")
    store.prepare_run(
        root_run_id="run-1",
        task_key="test_task",
        contract_version="v1",
        contract_hash="contract-hash",
        input_payload={"as_of": "2026-07-23"},
    )
    return store


def test_runtime_events_are_incremental_contiguous_and_hashed(tmp_path):
    store = prepared_store(tmp_path)
    store.start_task("run-1")
    store.append_event("run-1", "artifact_created", payload={"value": 1})
    store.complete_task("run-1")

    events = store.events("run-1")
    assert [event["seq"] for event in events] == [1, 2, 3, 4]
    assert [event["seq"] for event in store.events("run-1", after_seq=2)] == [3, 4]
    for event in events:
        canonical = json.dumps(
            event["payload"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        assert event["payload_hash"] == hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_runtime_event_payload_is_recursively_redacted_and_bounded(tmp_path):
    store = prepared_store(tmp_path)
    event = store.append_event(
        "run-1",
        "gate_evaluated",
        payload={
            "password": "plain-password",
            "nested": {"api_key": "plain-key", "note": "x" * (MAX_EVENT_PAYLOAD_BYTES + 1)},
        },
    )

    serialized = json.dumps(event["payload"], ensure_ascii=False)
    assert "plain-password" not in serialized
    assert "plain-key" not in serialized
    assert len(serialized.encode("utf-8")) <= MAX_EVENT_PAYLOAD_BYTES


def test_runtime_event_rejects_unknown_type(tmp_path):
    store = prepared_store(tmp_path)
    with pytest.raises(ValueError, match="未知运行事件类型"):
        store.append_event("run-1", "uncontracted_event")


def test_runtime_event_sequence_is_contiguous_under_concurrent_writes(tmp_path):
    store = prepared_store(tmp_path)

    def append(index):
        return store.append_event("run-1", "artifact_created", payload={"index": index})

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(append, range(24)))

    events = store.events("run-1", limit=100)
    assert [event["seq"] for event in events] == list(range(1, 26))
    assert len({event["event_id"] for event in events}) == 25


def test_runtime_event_api_is_incremental_and_reports_exact_has_more(tmp_path, monkeypatch):
    store = prepared_store(tmp_path)
    for index in range(3):
        store.append_event("run-1", "artifact_created", payload={"index": index})
    monkeypatch.setattr(system_router, "daily_batch_store", SimpleNamespace(events=store))

    first = system_router.runtime_run_events("run-1", after_seq=0, limit=2)
    assert [item["seq"] for item in first["items"]] == [1, 2]
    assert first["last_seq"] == 2
    assert first["has_more"] is True

    second = system_router.runtime_run_events("run-1", after_seq=2, limit=2)
    assert [item["seq"] for item in second["items"]] == [3, 4]
    assert second["has_more"] is False

    with pytest.raises(HTTPException) as exc_info:
        system_router.runtime_run_events("missing", after_seq=0, limit=2)
    assert exc_info.value.status_code == 404
