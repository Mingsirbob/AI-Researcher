from __future__ import annotations

import importlib
import math
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from time import monotonic
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from app.core.config import Settings
from app.core.observability import IFindCallObserver, classify_ifind_error
from app.core.resilience import CallInProgressError, CallTimeoutError, DaemonCallRunner
from app.market.updater import (
    ADJUSTMENT_PARAMS,
    DataContractError,
    StockDataUpdater,
    default_end_date,
    normalize_daily_frame,
)


DAILY_INDICATORS = "open;high;low;close;vwap;volume"
REALTIME_INDICATORS = "open;latest;high;low;volume;amount;preClose"
INDEX_REPORT_ID = "p03473"
INDEX_REPORT_FIELDS = "p03473_f001:Y,p03473_f002:Y,p03473_f003:Y"
INDEX_UNIVERSES = {
    "csi300": "000300.CSI",
    "csi_a500": "000510.CSI",
}


class IFindDataError(RuntimeError):
    def __init__(self, message: str, error_code: int | None = None):
        super().__init__(message)
        self.error_code = error_code


class IFindDataLayer:
    """Single-process iFinD session and the project's four data capabilities."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.observer = IFindCallObserver(
            settings.state_db,
            secrets=(settings.ifind_username, settings.ifind_password),
        )
        self.runner = DaemonCallRunner("iFinD SDK")
        self._lock = threading.RLock()
        self._sdk: Any | None = None
        self._logged_in = False
        self._owns_login = False
        self._sync_status: dict = {"status": "not_started"}

    def status(self) -> dict:
        sdk_available = importlib.util.find_spec("iFinDPy") is not None
        if not sdk_available:
            reason = "未找到 iFinDPy SDK"
        elif not self.settings.ifind_credentials_configured:
            reason = "缺少 IFIND_USER 或 IFIND_PASSWORD"
        else:
            reason = "ready"
        return {
            "configured": self.settings.ifind_credentials_configured,
            "sdk_available": sdk_available,
            "username_configured": bool(self.settings.ifind_username),
            "password_configured": bool(self.settings.ifind_password),
            "logged_in": self._logged_in,
            "reason": reason,
            "market_sync": dict(self._sync_status),
            "observability": self.observer.summary(),
            "resilience": {
                "state": "single_session",
                "timeout_seconds": self.settings.ifind_timeout_seconds,
                "max_attempts": min(self.settings.ifind_max_attempts, 2),
                "sdk_call_in_flight": self.runner.in_flight(),
            },
        }

    def _load_sdk(self):
        if self._sdk is not None:
            return self._sdk
        try:
            self._sdk = importlib.import_module("iFinDPy")
        except ModuleNotFoundError as exc:
            raise IFindDataError("quant 环境未安装 iFinDAPI") from exc
        return self._sdk

    def _execute(
        self,
        operation: str,
        function,
        transform=lambda value: value,
        *,
        requested_items: int = 1,
        attempts: int = 1,
    ):
        last_error: Exception | None = None
        for attempt in range(1, max(1, min(attempts, 2)) + 1):
            started_at = monotonic()
            try:
                raw = self.runner.call(function, self.settings.ifind_timeout_seconds)
                value = transform(raw)
            except Exception as exc:
                last_error = exc
                error_code = getattr(exc, "error_code", None)
                category = classify_ifind_error(exc, error_code)
                self.observer.record(
                    operation=operation,
                    status="error",
                    started_at=started_at,
                    attempt=attempt,
                    requested_items=requested_items,
                    error_code=error_code,
                    error=exc,
                    error_category=category,
                )
                if (
                    attempt >= attempts
                    or category not in {"rate_limit", "network", "environment_or_network", "upstream"}
                    or isinstance(exc, (CallTimeoutError, CallInProgressError))
                ):
                    break
                time.sleep(self.settings.ifind_backoff_seconds)
                continue
            rows = len(value) if isinstance(value, (list, pd.DataFrame)) else None
            self.observer.record(
                operation=operation,
                status="success",
                started_at=started_at,
                attempt=attempt,
                requested_items=requested_items,
                rows_returned=rows,
            )
            return value
        if isinstance(last_error, IFindDataError):
            raise last_error
        raise IFindDataError(f"{operation} 调用失败：{last_error}") from last_error

    @staticmethod
    def _check_result(result: Any) -> Any:
        error_code = int(getattr(result, "errorcode", -1))
        if error_code != 0:
            raise IFindDataError(
                f"iFinD 查询失败（{error_code}）：{getattr(result, 'errmsg', '未知错误')}",
                error_code=error_code,
            )
        return getattr(result, "data", None)

    @classmethod
    def _records(cls, result: Any) -> list[dict]:
        data = cls._check_result(result)
        if data is None:
            return []
        if hasattr(data, "to_dict"):
            return data.to_dict(orient="records")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            return [data]
        return []

    def start(self) -> None:
        with self._lock:
            if self._logged_in:
                return
            if not self.settings.ifind_credentials_configured:
                raise IFindDataError("缺少 IFIND_USER 或 IFIND_PASSWORD")
            sdk = self._load_sdk()

            def parse_login(value: Any) -> int:
                code = int(value)
                if code not in {0, -201}:
                    raise IFindDataError(f"iFinD 登录失败（{code}）", error_code=code)
                return code

            code = self._execute(
                "THS_iFinDLogin",
                lambda: sdk.THS_iFinDLogin(
                    self.settings.ifind_username,
                    self.settings.ifind_password,
                ),
                parse_login,
                attempts=min(self.settings.ifind_max_attempts, 2),
            )
            self._logged_in = True
            self._owns_login = code == 0

    def close(self) -> None:
        with self._lock:
            if not self._logged_in or self._sdk is None:
                return
            if self._owns_login:
                self._execute(
                    "THS_iFinDLogout",
                    self._sdk.THS_iFinDLogout,
                    lambda value: int(value),
                )
            self._logged_in = False
            self._owns_login = False

    def _ready(self):
        self.start()
        return self._sdk

    def _trading_dates(self, start: date, end: date) -> list[date]:
        if start > end:
            return []
        with self._lock:
            sdk = self._ready()

            def parse(result: Any) -> list[date]:
                data = self._check_result(result)
                values = data.split(",") if isinstance(data, str) else getattr(result, "time", None)
                if values is None:
                    raise DataContractError("iFinD 交易日历结果缺少 data/time")
                parsed = [date.fromisoformat(str(value).strip()[:10]) for value in values if str(value).strip()]
                if len(parsed) != len(set(parsed)):
                    raise DataContractError("iFinD 交易日历包含重复日期")
                if any(value < start or value > end for value in parsed):
                    raise DataContractError("iFinD 交易日历返回范围外日期")
                return sorted(parsed)

            return self._execute(
                "THS_Date_Query",
                lambda: sdk.THS_Date_Query("SSE", "dateType:0", start.isoformat(), end.isoformat()),
                parse,
            )

    def trading_dates(self, start: date, end: date) -> list[date]:
        """Return confirmed SSE trading dates for application workflows."""
        return self._trading_dates(start, end)

    def is_trading_day(self, value: date) -> bool:
        return value in self._trading_dates(value, value)

    def get_daily_prices(
        self,
        codes: list[str],
        start: date,
        end: date,
        *,
        adjustment: str = "unadjusted",
        attempt: int = 1,
        max_attempts: int = 1,
    ) -> pd.DataFrame:
        if adjustment not in ADJUSTMENT_PARAMS:
            raise ValueError(f"不支持的复权口径：{adjustment}")
        normalized_codes = list(dict.fromkeys(str(code).strip().upper() for code in codes if code))
        if not normalized_codes or start > end:
            return pd.DataFrame(columns=("time", "thscode", "open", "high", "low", "close", "vwap", "volume"))
        with self._lock:
            sdk = self._ready()

            def parse(result: Any) -> pd.DataFrame:
                data = self._check_result(result)
                return normalize_daily_frame(data)

            return self._execute(
                "THS_HD",
                lambda: sdk.THS_HD(
                    ",".join(normalized_codes),
                    DAILY_INDICATORS,
                    ADJUSTMENT_PARAMS[adjustment],
                    start.isoformat(),
                    end.isoformat(),
                    "format:dataframe",
                ),
                parse,
                requested_items=len(normalized_codes),
                attempts=max_attempts,
            )

    def sync_daily_prices(
        self,
        now: datetime | None = None,
        *,
        target_date: date | None = None,
    ) -> dict:
        current = now or datetime.now(ZoneInfo("Asia/Shanghai"))
        candidate = target_date or default_end_date(current)
        self._sync_status = {"status": "running", "candidate_date": candidate.isoformat()}
        try:
            dates = self._trading_dates(candidate - timedelta(days=31), candidate)
            if not dates:
                raise IFindDataError("iFinD 未返回最近交易日")
            target = dates[-1]
            if target_date is not None and target != target_date:
                raise IFindDataError(
                    f"{target_date.isoformat()} 不是已确认交易日；最近交易日为 {target.isoformat()}"
                )
            updater = StockDataUpdater(
                self.settings.stock_db,
                lambda codes, start, end, attempt=1: self.get_daily_prices(
                    codes, start, end, adjustment="unadjusted", attempt=attempt
                ),
            )
            result = updater.update(
                end_date=target,
                batch_size=50,
                max_retries=min(self.settings.ifind_max_attempts, 2),
                retry_delay=self.settings.ifind_backoff_seconds,
            )
            self._sync_status = {
                "status": result["status"],
                "latest_completed_trading_date": target.isoformat(),
                "rows_inserted": result["rows_inserted"],
                "failed_codes": len(result["failed_codes"]),
            }
            return {**result, "latest_completed_trading_date": target.isoformat()}
        except Exception as exc:
            self._sync_status = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
            raise

    @staticmethod
    def _column(frame: pd.DataFrame, *names: str) -> str | None:
        by_lower = {str(column).lower(): str(column) for column in frame.columns}
        for name in names:
            if name.lower() in by_lower:
                return by_lower[name.lower()]
        return None

    def get_index_members(self, index_code: str, as_of: date) -> pd.DataFrame:
        normalized_index = str(index_code).strip().upper()
        with self._lock:
            sdk = self._ready()

            def parse(result: Any) -> pd.DataFrame:
                data = self._check_result(result)
                if not isinstance(data, pd.DataFrame) or data.empty:
                    raise DataContractError("iFinD THS_DR 未返回指数成分")
                code_col = self._column(data, "p03473_f001", "股票代码", "证券代码", "成分券代码")
                name_col = self._column(data, "p03473_f002", "股票简称", "证券简称", "成分券名称")
                weight_col = self._column(data, "p03473_f003", "权重", "成分券权重")
                if code_col is None:
                    raise DataContractError(f"iFinD THS_DR 结果缺少成分代码字段：{list(data.columns)}")
                normalized = pd.DataFrame({
                    "security_code": data[code_col].astype(str).str.strip().str.upper(),
                    "security_name": data[name_col].astype(str).str.strip() if name_col else None,
                    "weight": pd.to_numeric(data[weight_col], errors="coerce") if weight_col else None,
                })
                if normalized["security_code"].duplicated().any():
                    raise DataContractError("iFinD THS_DR 指数成分包含重复证券")
                return normalized.sort_values("security_code").reset_index(drop=True)

            members = self._execute(
                "THS_DR",
                lambda: sdk.THS_DR(
                    INDEX_REPORT_ID,
                    f"iv_date={as_of:%Y%m%d};iv_zsdm={normalized_index}",
                    INDEX_REPORT_FIELDS,
                    "format:dataframe",
                ),
                parse,
            )
        self._save_universe_snapshot(normalized_index, as_of, members)
        return members

    def get_universe_by_query(self, query_text: str) -> pd.DataFrame:
        """Return the current security universe produced by iFinD Smart Selection."""
        normalized_query = str(query_text).strip()
        if not normalized_query:
            raise ValueError("股池查询条件不能为空")
        with self._lock:
            sdk = self._ready()

            def parse(result: Any) -> pd.DataFrame:
                data = self._check_result(result)
                if not isinstance(data, pd.DataFrame) or data.empty:
                    raise DataContractError("iFinD THS_WCQuery 未返回成分股")
                code_col = self._column(data, "股票代码", "证券代码", "成分券代码", "thscode")
                name_col = self._column(data, "股票简称", "证券简称", "成分券名称", "ths_stock_short_name_stock")
                weight_col = self._column(data, "权重", "成分券权重")
                if code_col is None:
                    raise DataContractError(
                        f"iFinD THS_WCQuery 结果缺少证券代码字段：{list(data.columns)}"
                    )
                normalized = pd.DataFrame({
                    "security_code": data[code_col].astype(str).str.strip().str.upper(),
                    "security_name": (
                        data[name_col].astype(str).str.strip() if name_col else None
                    ),
                    "weight": (
                        pd.to_numeric(data[weight_col], errors="coerce")
                        if weight_col else None
                    ),
                })
                normalized = normalized[
                    normalized["security_code"].str.fullmatch(
                        r"\d{6}\.(SH|SZ|BJ)", na=False
                    )
                ].copy()
                if normalized.empty:
                    raise DataContractError("iFinD THS_WCQuery 未返回有效 A 股证券代码")
                if normalized["security_code"].duplicated().any():
                    raise DataContractError("iFinD THS_WCQuery 成分股包含重复证券")
                return normalized.sort_values("security_code").reset_index(drop=True)

            return self._execute(
                "THS_WCQuery",
                lambda: sdk.THS_WCQuery(normalized_query, "stock"),
                parse,
            )

    def _save_universe_snapshot(self, index_code: str, as_of: date, frame: pd.DataFrame) -> None:
        with sqlite3.connect(self.settings.stock_db) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS universe_member (
                    universe_code TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    security_code TEXT NOT NULL,
                    security_name TEXT,
                    weight REAL,
                    PRIMARY KEY (universe_code, as_of, security_code)
                )
                """
            )
            conn.execute(
                "DELETE FROM universe_member WHERE universe_code=? AND as_of=?",
                (index_code, as_of.isoformat()),
            )
            conn.executemany(
                "INSERT INTO universe_member VALUES (?, ?, ?, ?, ?)",
                [
                    (index_code, as_of.isoformat(), row.security_code, row.security_name, row.weight)
                    for row in frame.itertuples(index=False)
                ],
            )

    @staticmethod
    def _value(row: dict, *names: str) -> Any:
        normalized = {str(key).lower(): value for key, value in row.items()}
        for name in names:
            value = normalized.get(name.lower())
            if value is not None and str(value).strip() not in {"", "nan", "None"}:
                return value
        return None

    def _profiles(self, codes: list[str]) -> list[dict]:
        sdk = self._ready()
        profiles: dict[str, dict] = {}
        for offset in range(0, len(codes), 100):
            batch = codes[offset : offset + 100]
            rows = self._execute(
                "THS_BD",
                lambda batch=batch: sdk.THS_BD(
                    ",".join(batch),
                    "ths_stock_short_name_stock",
                    "",
                    "format:dataframe",
                ),
                self._records,
                requested_items=len(batch),
            )
            for index, row in enumerate(rows):
                code = self._value(row, "thscode", "ths_code", "证券代码")
                if not code and len(batch) == 1 and index == 0:
                    code = batch[0]
                name = self._value(row, "ths_stock_short_name_stock", "secName", "证券简称")
                if code:
                    normalized = str(code).strip().upper()
                    profiles[normalized] = {"code": normalized, "name": str(name) if name else None, "source": "iFinD"}
        return [profiles.get(code, {"code": code, "name": None, "source": "iFinD"}) for code in codes]

    def _announcements(self, code: str, start_date: date, end_date: date) -> list[dict]:
        sdk = self._ready()
        rows = self._execute(
            "THS_ReportQuery",
            lambda: sdk.THS_ReportQuery(
                code,
                f"beginrDate:{start_date.isoformat()};endrDate:{end_date.isoformat()}",
                "reportDate:Y,thscode:Y,secName:Y,ctime:Y,reportTitle:Y,pdfURL:Y,seq:Y",
                "format:dataframe",
            ),
            self._records,
        )
        result = []
        for row in rows:
            title = self._value(row, "reportTitle", "公告标题")
            if title:
                result.append({
                    "date": str(self._value(row, "reportDate", "ctime", "公告日期") or ""),
                    "published_at": str(self._value(row, "ctime", "发布时间") or ""),
                    "title": str(title),
                    "url": str(self._value(row, "pdfURL", "公告链接") or ""),
                    "sequence": str(self._value(row, "seq", "唯一标号") or ""),
                    "source": "iFinD",
                })
        return sorted(result, key=lambda item: (item["date"], item["published_at"]), reverse=True)

    def get_company_data(
        self,
        codes: list[str],
        *,
        as_of: date,
        lookback_days: int = 365,
        include_announcements: bool = True,
    ) -> dict:
        normalized_codes = list(dict.fromkeys(str(code).strip().upper() for code in codes if code))
        with self._lock:
            self._ready()
            profiles = self._profiles(normalized_codes)
            announcements = {
                code: self._announcements(code, as_of - timedelta(days=lookback_days), as_of)
                for code in normalized_codes
            } if include_announcements else {}
        return {
            "as_of": as_of.isoformat(),
            "profiles": profiles,
            "announcements": announcements,
            "source": "iFinD QuantAPI",
        }

    @staticmethod
    def _finite(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    def get_realtime_quotes(self, codes: list[str]) -> list[dict]:
        normalized_codes = list(dict.fromkeys(str(code).strip().upper() for code in codes if code))
        quotes: dict[str, dict] = {}
        with self._lock:
            sdk = self._ready()
            for offset in range(0, len(normalized_codes), 50):
                batch = normalized_codes[offset : offset + 50]
                rows = self._execute(
                    "THS_RQ",
                    lambda batch=batch: sdk.THS_RQ(",".join(batch), REALTIME_INDICATORS, "", "format:dataframe"),
                    self._records,
                    requested_items=len(batch),
                )
                for index, row in enumerate(rows):
                    code = self._value(row, "thscode", "ths_code", "证券代码")
                    if not code and len(batch) == 1 and index == 0:
                        code = batch[0]
                    if not code:
                        continue
                    normalized = str(code).strip().upper()
                    raw_time = self._value(row, "time", "时间")
                    quote_time = None
                    if raw_time:
                        try:
                            parsed = datetime.fromisoformat(str(raw_time).strip().replace("/", "-"))
                            quote_time = parsed.isoformat(sep=" ") + ("+08:00" if parsed.tzinfo is None else "")
                        except ValueError:
                            pass
                    quotes[normalized] = {
                        "security_code": normalized,
                        "quote_time": quote_time,
                        "open": self._finite(self._value(row, "open", "开盘价")),
                        "latest": self._finite(self._value(row, "latest", "最新价")),
                        "high": self._finite(self._value(row, "high", "最高价")),
                        "low": self._finite(self._value(row, "low", "最低价")),
                        "volume": self._finite(self._value(row, "volume", "成交量")),
                        "amount": self._finite(self._value(row, "amount", "成交额")),
                        "previous_close": self._finite(self._value(row, "preClose", "pre_close", "昨收盘")),
                        "source": "iFinD THS_RQ",
                    }
        return [quotes[code] for code in normalized_codes if code in quotes]
