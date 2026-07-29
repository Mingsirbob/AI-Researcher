import threading
import time

from app.core.resilience import (
    CallInProgressError,
    CallTimeoutError,
    CircuitBreaker,
    CircuitOpenError,
    DaemonCallRunner,
    backoff_seconds,
)


def test_circuit_breaker_opens_and_allows_one_half_open_probe():
    now = [100.0]
    circuit = CircuitBreaker(
        "test-service",
        failure_threshold=2,
        recovery_timeout_seconds=10,
        clock=lambda: now[0],
    )

    circuit.before_call()
    circuit.record_failure()
    circuit.before_call()
    circuit.record_failure()
    assert circuit.status()["state"] == "open"

    try:
        circuit.before_call()
    except CircuitOpenError as exc:
        assert exc.retry_after_seconds == 10
    else:
        raise AssertionError("恢复窗口内必须快速失败")

    now[0] += 10
    circuit.before_call()
    assert circuit.status()["state"] == "half_open"
    try:
        circuit.before_call()
    except CircuitOpenError:
        pass
    else:
        raise AssertionError("半开状态只能有一个探测请求")
    circuit.record_success()
    assert circuit.status()["state"] == "closed"


def test_daemon_runner_times_out_and_rejects_overlapping_call():
    release = threading.Event()
    runner = DaemonCallRunner("native-sdk")

    try:
        runner.call(lambda: release.wait(1), timeout_seconds=0.01)
    except CallTimeoutError:
        pass
    else:
        raise AssertionError("超时必须返回")

    try:
        runner.call(lambda: "overlap", timeout_seconds=0.01)
    except CallInProgressError:
        pass
    else:
        raise AssertionError("底层调用未结束时不得重叠调用")
    release.set()
    time.sleep(0.02)
    assert runner.call(lambda: "ok", timeout_seconds=0.1) == "ok"


def test_exponential_backoff_is_bounded():
    assert [backoff_seconds(i, 0.5, 2.0) for i in range(1, 5)] == [0.5, 1.0, 2.0, 2.0]
