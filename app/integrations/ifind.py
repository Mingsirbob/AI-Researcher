from __future__ import annotations

import importlib
import math
import threading
import time
from datetime import date, datetime, timedelta
from time import monotonic
from typing import Any

from app.core.config import Settings
from app.core.observability import IFindCallObserver, classify_ifind_error
from app.core.resilience import (
    CallInProgressError,
    CallTimeoutError,
    CircuitBreaker,
    CircuitOpenError,
    DaemonCallRunner,
    backoff_seconds,
)
from app.core.runtime_events import (
    begin_tool_event,
    complete_tool_event,
    fail_tool_event,
    record_tool_retry,
)


RETRYABLE_CATEGORIES = {
    "timeout",
    "rate_limit",
    "network",
    "environment_or_network",
    "upstream",
}


class IFindError(RuntimeError):
    def __init__(self, message: str, error_code: int | None = None):
        super().__init__(message)
        self.error_code = error_code


class IFindService:
    """Thin, optional adapter around the locally installed iFinD Python SDK."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.observer = IFindCallObserver(
            settings.state_db,
            secrets=(settings.ifind_username, settings.ifind_password),
        )
        self.circuit = CircuitBreaker(
            "iFinD",
            failure_threshold=settings.ifind_circuit_failure_threshold,
            recovery_timeout_seconds=settings.ifind_circuit_recovery_seconds,
        )
        self.runner = DaemonCallRunner("iFinD SDK")
        self._lock = threading.RLock()
        self._sdk: Any | None = None
        self._logged_in = False
        self._owns_login = False
        self._cache: dict[str, tuple[float, dict]] = {}

    def status(self) -> dict:
        sdk_available = importlib.util.find_spec("iFinDPy") is not None
        if not sdk_available:
            reason = "未找到 iFinDPy SDK"
        elif not self.settings.ifind_username:
            reason = "缺少 IFIND_USER"
        elif not self.settings.ifind_password:
            reason = "缺少 IFIND_PASSWORD"
        else:
            reason = "ready"
        return {
            "configured": self.settings.ifind_credentials_configured,
            "sdk_available": sdk_available,
            "username_configured": bool(self.settings.ifind_username),
            "password_configured": bool(self.settings.ifind_password),
            "logged_in": self._logged_in,
            "reason": reason,
            "observability": self.observer.summary(),
            "resilience": {
                **self.circuit.status(),
                "timeout_seconds": self.settings.ifind_timeout_seconds,
                "max_attempts": self.settings.ifind_max_attempts,
                "backoff_seconds": self.settings.ifind_backoff_seconds,
                "native_timeout_supported": False,
                "sdk_call_in_flight": self.runner.in_flight(),
            },
        }

    def _load_sdk(self):
        if self._sdk is not None:
            return self._sdk
        try:
            self._sdk = importlib.import_module("iFinDPy")
        except ModuleNotFoundError as exc:
            raise IFindError("quant 环境未安装 iFinDAPI，请执行 pip install iFinDAPI") from exc
        return self._sdk

    def _ensure_login(self):
        if not self.settings.ifind_username:
            raise IFindError("缺少 iFinD 账号，请按示例在 .env 中设置 IFIND_USER")
        if not self.settings.ifind_password:
            raise IFindError("缺少 iFinD 密码，请在 .env 中设置 IFIND_PASSWORD")
        sdk = self._load_sdk()
        if self._logged_in:
            return sdk
        def parse_login(value) -> int:
            code = int(value)
            if code in {0, -201}:
                return code
            messages = {-2: "账号或密码错误", -201: "账号已在当前环境登录"}
            raise IFindError(
                f"iFinD 登录失败（{code}）：{messages.get(code, '请检查账号权限或网络')}",
                error_code=code,
            )

        code = self._execute(
            "THS_iFinDLogin",
            lambda: sdk.THS_iFinDLogin(
                self.settings.ifind_username,
                self.settings.ifind_password,
            ),
            parse_login,
        )
        self._logged_in = True
        self._owns_login = code == 0
        return sdk

    def logout(self) -> int:
        with self._lock:
            if not self._logged_in or self._sdk is None:
                return 0
            if self._owns_login:
                started_at = monotonic()
                try:
                    result = int(
                        self.runner.call(
                            self._sdk.THS_iFinDLogout,
                            self.settings.ifind_timeout_seconds,
                        )
                    )
                except Exception as exc:
                    self.observer.record(
                        operation="THS_iFinDLogout", status="error", started_at=started_at, error=exc
                    )
                    self._logged_in = False
                    self._owns_login = False
                    raise IFindError(f"iFinD 注销调用异常：{exc}") from exc
                self.observer.record(
                    operation="THS_iFinDLogout",
                    status="success" if result == 0 else "error",
                    started_at=started_at,
                    error_code=result if result != 0 else None,
                    error=f"iFinD 注销失败（{result}）" if result != 0 else None,
                )
            else:
                result = 0
            self._logged_in = False
            self._owns_login = False
            return result

    @staticmethod
    def _records(result: Any) -> list[dict]:
        error_code = int(getattr(result, "errorcode", -1))
        if error_code != 0:
            raise IFindError(
                f"iFinD 查询失败（{error_code}）：{getattr(result, 'errmsg', '未知错误')}",
                error_code=error_code,
            )
        data = getattr(result, "data", None)
        if data is None:
            return []
        if hasattr(data, "to_dict"):
            return data.to_dict(orient="records")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            return [data]
        return []

    def _execute(self, operation: str, function, transform, *, requested_items: int = 1):
        tool_name = f"ifind.{operation}"
        runtime_call_id = begin_tool_event(
            tool_name, {"requested_items": requested_items}
        )
        try:
            self.circuit.before_call()
        except CircuitOpenError as exc:
            started_at = monotonic()
            self.observer.record(
                operation=operation,
                status="error",
                started_at=started_at,
                requested_items=requested_items,
                error=exc,
                error_category="circuit_open",
                runtime_call_id=runtime_call_id,
            )
            fail_tool_event(
                runtime_call_id, tool_name, exc, category="circuit_open"
            )
            raise IFindError(str(exc)) from exc

        last_error: Exception | None = None
        last_category = "upstream"
        attempts_used = 0
        for attempt in range(1, self.settings.ifind_max_attempts + 1):
            attempts_used = attempt
            started_at = monotonic()
            try:
                raw = self.runner.call(function, self.settings.ifind_timeout_seconds)
                value = transform(raw)
            except Exception as exc:
                last_error = exc
                error_code = getattr(exc, "error_code", None)
                category = classify_ifind_error(exc, error_code)
                last_category = category
                self.observer.record(
                    operation=operation,
                    status="error",
                    started_at=started_at,
                    attempt=attempt,
                    requested_items=requested_items,
                    error_code=error_code,
                    error=exc,
                    error_category=category,
                    runtime_call_id=runtime_call_id,
                )
                retryable = category in RETRYABLE_CATEGORIES
                if retryable:
                    self.circuit.record_failure()
                else:
                    self.circuit.record_success()
                timed_out = isinstance(exc, (CallTimeoutError, CallInProgressError))
                circuit_open = self.circuit.status()["state"] == "open"
                if (
                    not retryable
                    or timed_out
                    or circuit_open
                    or attempt >= self.settings.ifind_max_attempts
                ):
                    break
                record_tool_retry(runtime_call_id, tool_name, attempt, category)
                time.sleep(
                    backoff_seconds(
                        attempt,
                        self.settings.ifind_backoff_seconds,
                        self.settings.ifind_backoff_seconds * 8,
                    )
                )
                continue

            rows_returned = len(value) if isinstance(value, list) else None
            self.observer.record(
                operation=operation,
                status="success",
                started_at=started_at,
                attempt=attempt,
                requested_items=requested_items,
                rows_returned=rows_returned,
                runtime_call_id=runtime_call_id,
            )
            self.circuit.record_success()
            complete_tool_event(
                runtime_call_id,
                tool_name,
                {"attempts": attempt, "rows_returned": rows_returned},
            )
            return value

        failure = last_error or IFindError(f"{operation} 调用失败")
        fail_tool_event(
            runtime_call_id, tool_name, failure,
            category=last_category, attempt=attempts_used,
        )
        if isinstance(last_error, IFindError):
            raise last_error
        raise IFindError(f"{operation} 调用失败：{last_error}") from last_error

    def _call(self, operation: str, function, *, requested_items: int = 1):
        return self._execute(
            operation,
            function,
            self._records,
            requested_items=requested_items,
        )

    @staticmethod
    def _value(row: dict, *names: str) -> Any:
        normalized = {str(key).lower(): value for key, value in row.items()}
        for name in names:
            value = normalized.get(name.lower())
            if value is not None and str(value).strip() not in {"", "nan", "None"}:
                return value
        return None

    def get_security_profiles(self, codes: list[str]) -> list[dict]:
        unique_codes = list(dict.fromkeys(code for code in codes if code))
        if not unique_codes:
            return []
        profiles: dict[str, dict] = {}
        with self._lock:
            sdk = self._ensure_login()
            for start in range(0, len(unique_codes), 100):
                batch = unique_codes[start : start + 100]
                joined = ",".join(batch)
                profile_rows = self._call(
                    "THS_BD",
                    lambda joined=joined: sdk.THS_BD(
                        joined,
                        "ths_stock_short_name_stock",
                        "",
                        "format:dataframe",
                    ),
                    requested_items=len(batch),
                )
                for index, row in enumerate(profile_rows):
                    code = self._value(row, "thscode", "ths_code", "证券代码")
                    if not code and len(batch) == 1 and index == 0:
                        code = batch[0]
                    name = self._value(
                        row,
                        "ths_stock_short_name_stock",
                        "secName",
                        "证券简称",
                    )
                    if code:
                        normalized = str(code).strip().upper()
                        profiles[normalized] = {
                            "code": normalized,
                            "name": str(name) if name else None,
                            "source": "iFinD",
                        }
        return [
            profiles.get(code, {"code": code, "name": None, "source": "iFinD"})
            for code in unique_codes
        ]

    def get_security_profile(self, code: str) -> dict:
        return self.get_security_profiles([code])[0]

    @staticmethod
    def _finite_number(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    @staticmethod
    def _quote_time(value: Any) -> str | None:
        if value is None or not str(value).strip():
            return None
        raw = str(value).strip().replace("/", "-")
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.isoformat(sep=" ") + "+08:00"
        return parsed.isoformat(sep=" ")

    def get_realtime_quotes(self, codes: list[str]) -> list[dict]:
        """Return normalized point-in-time snapshots from THS_RQ only."""
        unique_codes = list(dict.fromkeys(str(code).strip().upper() for code in codes if code))
        if not unique_codes:
            return []
        quotes: dict[str, dict] = {}
        indicators = "open;latest;high;low;volume;amount;preClose"
        with self._lock:
            sdk = self._ensure_login()
            for start in range(0, len(unique_codes), 50):
                batch = unique_codes[start : start + 50]
                joined = ",".join(batch)
                rows = self._call(
                    "THS_RQ",
                    lambda joined=joined: sdk.THS_RQ(
                        joined, indicators, "", "format:dataframe"
                    ),
                    requested_items=len(batch),
                )
                for index, row in enumerate(rows):
                    code = self._value(row, "thscode", "ths_code", "证券代码")
                    if not code and len(batch) == 1 and index == 0:
                        code = batch[0]
                    if not code:
                        continue
                    normalized = str(code).strip().upper()
                    quote_time = self._quote_time(self._value(row, "time", "时间"))
                    quotes[normalized] = {
                        "security_code": normalized,
                        "quote_time": quote_time,
                        "open": self._finite_number(self._value(row, "open", "开盘价")),
                        "latest": self._finite_number(self._value(row, "latest", "最新价")),
                        "high": self._finite_number(self._value(row, "high", "最高价")),
                        "low": self._finite_number(self._value(row, "low", "最低价")),
                        "volume": self._finite_number(self._value(row, "volume", "成交量")),
                        "amount": self._finite_number(self._value(row, "amount", "成交额")),
                        "previous_close": self._finite_number(
                            self._value(row, "preClose", "pre_close", "昨收盘")
                        ),
                        "source": "iFinD THS_RQ",
                    }
        return [quotes[code] for code in unique_codes if code in quotes]

    def query_announcements(self, code: str, start_date: str, end_date: str) -> list[dict]:
        with self._lock:
            sdk = self._ensure_login()
            params = f"beginrDate:{start_date};endrDate:{end_date}"
            output = "reportDate:Y,thscode:Y,secName:Y,ctime:Y,reportTitle:Y,pdfURL:Y,seq:Y"
            report_rows = self._call(
                "THS_ReportQuery",
                lambda: sdk.THS_ReportQuery(code, params, output, "format:dataframe"),
            )
        announcements = []
        for row in report_rows:
            title = self._value(row, "reportTitle", "公告标题")
            if not title:
                continue
            announcements.append(
                {
                    "date": str(self._value(row, "reportDate", "ctime", "公告日期") or ""),
                    "published_at": str(self._value(row, "ctime", "发布时间") or ""),
                    "title": str(title),
                    "url": str(self._value(row, "pdfURL", "公告链接") or ""),
                    "sequence": str(self._value(row, "seq", "唯一标号") or ""),
                    "source": "iFinD",
                }
            )
        announcements.sort(key=lambda item: (item["date"], item["published_at"]), reverse=True)
        return announcements

    def get_context(self, code: str, as_of: str, lookback_days: int = 365) -> dict:
        cache_key = f"{code}:{as_of}:{lookback_days}"
        cached = self._cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < 600:
            return cached[1]
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached and time.monotonic() - cached[0] < 600:
                return cached[1]
            end = date.fromisoformat(as_of)
            start = end - timedelta(days=lookback_days)
            profile = self.get_security_profile(code)
            announcements = self.query_announcements(code, start.isoformat(), end.isoformat())
            context = {
                "security": {"code": code, "name": profile["name"]},
                "as_of": as_of,
                "lookback_start": start.isoformat(),
                "announcements": announcements[:20],
                "source": "iFinD QuantAPI",
            }
            self._cache[cache_key] = (time.monotonic(), context)
            return context
