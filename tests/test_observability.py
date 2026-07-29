from time import monotonic

from app.core.observability import IFindCallObserver, classify_ifind_error


def test_ifind_observer_persists_metrics_and_redacts_secrets(tmp_path):
    db_path = tmp_path / "state.db"
    observer = IFindCallObserver(db_path, secrets=("secret-password", "research-user"))
    observer.record(
        operation="THS_HD",
        status="success",
        started_at=monotonic(),
        requested_items=50,
        rows_returned=50,
    )
    observer.record(
        operation="THS_HD",
        status="error",
        started_at=monotonic(),
        requested_items=1,
        error_code=-2,
        error="research-user password=secret-password",
    )

    summary = observer.summary()
    assert summary["calls"] == 2
    assert summary["successes"] == 1
    assert summary["errors"] == 1
    assert summary["error_categories"] == {"authentication": 1}
    with observer.connect() as conn:
        message = conn.execute(
            "SELECT error_message FROM ifind_call_events WHERE status = 'error'"
        ).fetchone()[0]
    assert "secret-password" not in message
    assert "research-user" not in message


def test_ifind_error_classification():
    assert classify_ifind_error("request timeout") == "timeout"
    assert classify_ifind_error("调用频率超过限制") == "rate_limit"
    assert classify_ifind_error("anything", -340) == "environment_or_network"
