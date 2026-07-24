from __future__ import annotations

import threading
from dataclasses import dataclass
from time import monotonic
from typing import Callable, Generic, TypeVar


T = TypeVar("T")


class CircuitOpenError(RuntimeError):
    def __init__(self, service: str, retry_after_seconds: float):
        self.service = service
        self.retry_after_seconds = max(0.0, retry_after_seconds)
        super().__init__(
            f"{service} 熔断器已打开，请在 {self.retry_after_seconds:.1f} 秒后重试"
        )


class CallTimeoutError(TimeoutError):
    pass


class CallInProgressError(CallTimeoutError):
    pass


class CircuitBreaker:
    def __init__(
        self,
        service: str,
        failure_threshold: int = 3,
        recovery_timeout_seconds: float = 60.0,
        clock: Callable[[], float] = monotonic,
    ):
        if failure_threshold < 1:
            raise ValueError("failure_threshold 必须大于 0")
        if recovery_timeout_seconds <= 0:
            raise ValueError("recovery_timeout_seconds 必须大于 0")
        self.service = service
        self.failure_threshold = failure_threshold
        self.recovery_timeout_seconds = recovery_timeout_seconds
        self.clock = clock
        self._lock = threading.Lock()
        self._state = "closed"
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._half_open_probe = False

    def before_call(self) -> None:
        with self._lock:
            if self._state == "closed":
                return
            now = self.clock()
            if self._state == "open":
                opened_at = self._opened_at if self._opened_at is not None else now
                elapsed = now - opened_at
                if elapsed < self.recovery_timeout_seconds:
                    raise CircuitOpenError(
                        self.service,
                        self.recovery_timeout_seconds - elapsed,
                    )
                self._state = "half_open"
                self._half_open_probe = False
            if self._half_open_probe:
                raise CircuitOpenError(self.service, self.recovery_timeout_seconds)
            self._half_open_probe = True

    def record_success(self) -> None:
        with self._lock:
            self._state = "closed"
            self._consecutive_failures = 0
            self._opened_at = None
            self._half_open_probe = False

    def record_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if self._state == "half_open" or self._consecutive_failures >= self.failure_threshold:
                self._state = "open"
                self._opened_at = self.clock()
            self._half_open_probe = False

    def reset(self) -> None:
        self.record_success()

    def status(self) -> dict:
        with self._lock:
            retry_after = 0.0
            if self._state == "open" and self._opened_at is not None:
                retry_after = max(
                    0.0,
                    self.recovery_timeout_seconds - (self.clock() - self._opened_at),
                )
            return {
                "service": self.service,
                "state": self._state,
                "consecutive_failures": self._consecutive_failures,
                "failure_threshold": self.failure_threshold,
                "recovery_timeout_seconds": self.recovery_timeout_seconds,
                "retry_after_seconds": round(retry_after, 3),
            }


class DaemonCallRunner(Generic[T]):
    """Applies an application deadline without pretending a native call was cancelled."""

    def __init__(self, service: str):
        self.service = service
        self._lock = threading.Lock()
        self._current_thread: threading.Thread | None = None
        self._current_completed: threading.Event | None = None

    def call(self, function: Callable[[], T], timeout_seconds: float) -> T:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        completed = threading.Event()
        outcome: dict[str, object] = {}

        with self._lock:
            call_still_running = (
                self._current_thread is not None
                and self._current_thread.is_alive()
                and self._current_completed is not None
                and not self._current_completed.is_set()
            )
            if call_still_running:
                raise CallInProgressError(
                    f"{self.service} 上一次超时调用仍在底层执行，拒绝发起重叠请求"
                )

            def target() -> None:
                try:
                    outcome["value"] = function()
                except BaseException as exc:
                    outcome["error"] = exc
                finally:
                    completed.set()

            self._current_thread = threading.Thread(
                target=target,
                name=f"{self.service}-bounded-call",
                daemon=True,
            )
            self._current_completed = completed
            self._current_thread.start()

        if not completed.wait(timeout_seconds):
            raise CallTimeoutError(
                f"{self.service} 调用超过 {timeout_seconds:g} 秒；底层调用不可安全强制终止"
            )
        error = outcome.get("error")
        if error is not None:
            raise error
        return outcome["value"]  # type: ignore[return-value]

    def in_flight(self) -> bool:
        with self._lock:
            return bool(
                self._current_thread is not None
                and self._current_thread.is_alive()
                and self._current_completed is not None
                and not self._current_completed.is_set()
            )


def backoff_seconds(attempt: int, base_seconds: float, max_seconds: float) -> float:
    if attempt < 1:
        raise ValueError("attempt 必须从 1 开始")
    return min(max_seconds, base_seconds * (2 ** (attempt - 1)))
