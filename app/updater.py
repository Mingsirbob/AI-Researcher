from __future__ import annotations

import json
import inspect
import math
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Callable
from zoneinfo import ZoneInfo

import pandas as pd

from .config import Settings
from .observability import IFindCallObserver, classify_ifind_error
from .primitives import code_to_table, table_to_code
from .resilience import (
    CallInProgressError,
    CallTimeoutError,
    CircuitBreaker,
    CircuitOpenError,
    DaemonCallRunner,
    backoff_seconds,
)


INDICATORS = "open;high;low;close;vwap;volume"
ADJUSTMENT_PARAMS = {
    "unadjusted": "",
    "backward": "CPS:1",
    "forward": "CPS:2",
}
UNADJUSTED_PARAMS = ADJUSTMENT_PARAMS["unadjusted"]
TRADING_CALENDAR_EXCHANGE = "SSE"
TRADING_CALENDAR_PARAMS = "dateType:0"
DAILY_COLUMNS = ("time", "thscode", "open", "high", "low", "close", "vwap", "volume")
REQUIRED_COLUMNS = set(DAILY_COLUMNS)
MAX_ABSOLUTE_DAILY_RETURN = 0.60
PRICE_COMPARISON_REL_TOLERANCE = 1e-8
PRICE_COMPARISON_ABS_TOLERANCE = 1e-4
RETRYABLE_IFIND_CATEGORIES = {
    "timeout",
    "rate_limit",
    "network",
    "environment_or_network",
    "upstream",
}


class StockUpdateError(RuntimeError):
    def __init__(self, message: str, error_code: int | None = None):
        super().__init__(message)
        self.error_code = error_code


class DataContractError(StockUpdateError):
    pass


@dataclass(frozen=True)
class QualityIssue:
    code: str
    trading_date: str
    rule: str
    field: str | None
    observed: str
    expected: str

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "trading_date": self.trading_date,
            "rule": self.rule,
            "field": self.field,
            "observed": self.observed,
            "expected": self.expected,
        }


def default_end_date(now: datetime | None = None) -> date:
    current = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    # Before the close-to-data window, only request completed calendar days.
    return current.date() if current.hour >= 18 else current.date() - timedelta(days=1)


def normalize_daily_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None:
        raise DataContractError("iFinD 日线结果 data 为 None")
    normalized = frame.copy()
    normalized.columns = [str(column).lower() for column in normalized.columns]
    if normalized.columns.duplicated().any():
        duplicates = sorted(set(normalized.columns[normalized.columns.duplicated()]))
        raise DataContractError(f"iFinD 日线结果存在重复字段：{', '.join(duplicates)}")
    missing = REQUIRED_COLUMNS.difference(normalized.columns)
    if missing:
        raise DataContractError(f"iFinD 日线结果缺少字段：{', '.join(sorted(missing))}")
    unexpected = set(normalized.columns).difference(REQUIRED_COLUMNS)
    if unexpected:
        raise DataContractError(f"iFinD 日线结果出现未声明字段：{', '.join(sorted(unexpected))}")
    normalized = normalized[list(DAILY_COLUMNS)].copy()
    if normalized.empty:
        return normalized

    parsed_dates = pd.to_datetime(normalized["time"], errors="coerce")
    if parsed_dates.isna().any():
        bad_values = normalized.loc[parsed_dates.isna(), "time"].astype(str).head(3).tolist()
        raise DataContractError(f"iFinD 日线结果包含非法日期：{bad_values}")
    normalized["time"] = parsed_dates.dt.strftime("%Y-%m-%d")
    normalized["thscode"] = normalized["thscode"].astype(str).str.upper()
    if normalized["thscode"].isin({"", "NAN", "NONE"}).any():
        raise DataContractError("iFinD 日线结果包含空证券代码")
    for column in ("open", "high", "low", "close", "vwap", "volume"):
        original = normalized[column]
        converted = pd.to_numeric(original, errors="coerce")
        invalid = original.notna() & converted.isna()
        if invalid.any():
            values = original.loc[invalid].astype(str).head(3).tolist()
            raise DataContractError(f"iFinD 日线字段 {column} 包含非数值：{values}")
        normalized[column] = converted
    duplicated = normalized.duplicated(["thscode", "time"], keep=False)
    if duplicated.any():
        sample = normalized.loc[duplicated, ["thscode", "time"]].head(3).to_dict(orient="records")
        raise DataContractError(f"iFinD 日线结果包含重复证券日期：{sample}")
    normalized.sort_values(["thscode", "time"], inplace=True)
    return normalized


def align_adjusted_daily_fields(
    adjusted: pd.DataFrame,
    unadjusted: pd.DataFrame,
) -> tuple[pd.DataFrame, dict]:
    """Align vendor VWAP with adjusted OHLC while retaining real traded volume."""
    keys = ["time", "thscode"]
    raw = unadjusted[keys + ["close", "vwap", "volume"]].rename(
        columns={"close": "raw_close", "vwap": "raw_vwap", "volume": "raw_volume"}
    )
    merged = adjusted.merge(raw, on=keys, how="left", validate="one_to_one")
    ratio = pd.to_numeric(merged["close"], errors="coerce") / pd.to_numeric(
        merged["raw_close"], errors="coerce"
    )
    scaled_vwap = pd.to_numeric(merged["raw_vwap"], errors="coerce") * ratio
    original_vwap = pd.to_numeric(merged["vwap"], errors="coerce")
    merged["vwap"] = scaled_vwap.where(scaled_vwap.notna(), original_vwap)
    merged["volume"] = pd.to_numeric(merged["volume"], errors="coerce").where(
        pd.to_numeric(merged["volume"], errors="coerce").notna(),
        pd.to_numeric(merged["raw_volume"], errors="coerce"),
    )
    eligible = pd.to_numeric(merged["close"], errors="coerce").notna()
    scaled = eligible & scaled_vwap.notna()
    summary = {
        "method": "raw_vwap * adjusted_close / raw_close",
        "eligible_rows": int(eligible.sum()),
        "scaled_rows": int(scaled.sum()),
        "scaled_coverage": float(scaled.sum() / eligible.sum()) if eligible.any() else 0.0,
    }
    return merged[list(DAILY_COLUMNS)].copy(), summary


def validate_daily_frame(
    frame: pd.DataFrame,
    *,
    requested_codes: set[str],
    start: date,
    end: date,
    previous_closes: dict[str, float | None],
    max_absolute_return: float = MAX_ABSOLUTE_DAILY_RETURN,
) -> list[QualityIssue]:
    issues: list[QualityIssue] = []
    unknown_codes = sorted(set(frame["thscode"]).difference(requested_codes))
    if unknown_codes:
        raise DataContractError(f"iFinD 返回了请求范围外的证券代码：{unknown_codes[:5]}")
    if not frame.empty:
        returned_dates = frame["time"].map(date.fromisoformat)
        outside_range = frame.loc[(returned_dates < start) | (returned_dates > end), "time"].unique().tolist()
        if outside_range:
            raise DataContractError(
                f"iFinD 返回了请求范围外的交易日期：{outside_range[:5]}，"
                f"请求范围 {start.isoformat()}..{end.isoformat()}"
            )

    for code, group in frame.groupby("thscode", sort=False):
        previous_close = previous_closes.get(code)
        for record in group.to_dict(orient="records"):
            trading_date = record["time"]
            record_date = date.fromisoformat(trading_date)

            def add(rule: str, field: str | None, observed, expected: str) -> None:
                issues.append(
                    QualityIssue(
                        code=code,
                        trading_date=trading_date,
                        rule=rule,
                        field=field,
                        observed=str(observed),
                        expected=expected,
                    )
                )

            close = record["close"]
            if close is None or not math.isfinite(close) or close <= 0:
                add("positive_close", "close", close, "finite value > 0")
                continue

            price_fields = [record[name] for name in ("open", "high", "low")]
            volume_value = record["volume"]
            volume_missing = volume_value is None or pd.isna(volume_value)
            suspended_placeholder = (
                volume_missing
                and all(value is not None and not pd.isna(value) for value in price_fields)
                and all(
                    math.isfinite(float(value))
                    and float(value) > 0
                    and math.isclose(
                        float(value),
                        close,
                        rel_tol=PRICE_COMPARISON_REL_TOLERANCE,
                        abs_tol=PRICE_COMPARISON_ABS_TOLERANCE,
                    )
                    for value in price_fields
                )
            )
            trading_fields = [*price_fields, volume_value]
            has_trade = not suspended_placeholder and any(
                value is not None and not pd.isna(value) for value in trading_fields
            )
            if has_trade:
                missing = [
                    name
                    for name in ("open", "high", "low", "volume")
                    if record[name] is None or pd.isna(record[name])
                ]
                if missing:
                    add("complete_ohlcv", ",".join(missing), missing, "trading rows require open/high/low/volume")
                else:
                    open_price = float(record["open"])
                    high = float(record["high"])
                    low = float(record["low"])
                    volume = float(record["volume"])
                    for name, value in (("open", open_price), ("high", high), ("low", low)):
                        if not math.isfinite(value) or value <= 0:
                            add("positive_price", name, value, "finite value > 0")
                    if high < max(open_price, low, close) or low > min(open_price, high, close):
                        add(
                            "ohlc_bounds",
                            "open,high,low,close",
                            f"O={open_price},H={high},L={low},C={close}",
                            "high >= max(open, low, close) and low <= min(open, high, close)",
                        )
                    if not math.isfinite(volume) or volume < 0:
                        add("nonnegative_volume", "volume", volume, "finite value >= 0")
                    vwap = record["vwap"]
                    if vwap is not None and not pd.isna(vwap):
                        vwap = float(vwap)
                        below_low = vwap < low and not math.isclose(
                            vwap,
                            low,
                            rel_tol=PRICE_COMPARISON_REL_TOLERANCE,
                            abs_tol=PRICE_COMPARISON_ABS_TOLERANCE,
                        )
                        above_high = vwap > high and not math.isclose(
                            vwap,
                            high,
                            rel_tol=PRICE_COMPARISON_REL_TOLERANCE,
                            abs_tol=PRICE_COMPARISON_ABS_TOLERANCE,
                        )
                        if not math.isfinite(vwap) or vwap <= 0 or below_low or above_high:
                            add("vwap_bounds", "vwap", vwap, f"between low={low} and high={high}")

            if previous_close is not None and previous_close > 0:
                daily_return = close / previous_close - 1
                if abs(daily_return) > max_absolute_return:
                    add(
                        "close_jump",
                        "close",
                        f"previous={previous_close}, current={close}, return={daily_return:.6f}",
                        f"absolute close-to-close return <= {max_absolute_return:.0%}",
                    )
            previous_close = close
    return issues


class IFindDailyClient:
    def __init__(
        self,
        settings: Settings,
        observer: IFindCallObserver | None = None,
        *,
        adjustment: str = "unadjusted",
    ):
        if adjustment not in ADJUSTMENT_PARAMS:
            raise ValueError(f"不支持的复权口径：{adjustment}")
        self.settings = settings
        self.adjustment = adjustment
        self.history_params = ADJUSTMENT_PARAMS[adjustment]
        self.observer = observer or IFindCallObserver(
            settings.state_db,
            secrets=(settings.ifind_username, settings.ifind_password),
        )
        self.circuit = CircuitBreaker(
            "iFinD updater",
            failure_threshold=settings.ifind_circuit_failure_threshold,
            recovery_timeout_seconds=settings.ifind_circuit_recovery_seconds,
        )
        self.runner = DaemonCallRunner("iFinD updater SDK")
        self._sdk = None
        self._logged_in = False
        self._owns_login = False

    def _execute(
        self,
        operation: str,
        function,
        transform,
        *,
        requested_items: int = 0,
        max_attempts: int = 1,
        first_attempt: int = 1,
    ):
        try:
            self.circuit.before_call()
        except CircuitOpenError as exc:
            started_at = monotonic()
            self.observer.record(
                operation=operation,
                status="error",
                started_at=started_at,
                attempt=first_attempt,
                requested_items=requested_items,
                error=exc,
                error_category="circuit_open",
            )
            raise

        last_error: Exception | None = None
        for offset in range(max_attempts):
            attempt = first_attempt + offset
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
                retryable = category in RETRYABLE_IFIND_CATEGORIES
                if retryable:
                    self.circuit.record_failure()
                else:
                    self.circuit.record_success()
                timed_out = isinstance(exc, (CallTimeoutError, CallInProgressError))
                if (
                    not retryable
                    or timed_out
                    or self.circuit.status()["state"] == "open"
                    or offset + 1 >= max_attempts
                ):
                    break
                time.sleep(
                    backoff_seconds(
                        offset + 1,
                        self.settings.ifind_backoff_seconds,
                        self.settings.ifind_backoff_seconds * 8,
                    )
                )
                continue
            rows_returned = len(value) if isinstance(value, pd.DataFrame) else None
            self.observer.record(
                operation=operation,
                status="success",
                started_at=started_at,
                attempt=attempt,
                requested_items=requested_items,
                rows_returned=rows_returned,
            )
            self.circuit.record_success()
            return value
        if last_error is not None:
            raise last_error
        raise StockUpdateError(f"{operation} 调用失败")

    def login(self) -> None:
        if not self.settings.ifind_username or not self.settings.ifind_password:
            raise StockUpdateError("缺少 IFIND_USER 或 IFIND_PASSWORD")
        from iFinDPy import (
            THS_Date_Query,
            THS_HD,
            THS_WCQuery,
            THS_iFinDLogin,
            THS_iFinDLogout,
        )

        self._sdk = {
            "history": THS_HD,
            "calendar": THS_Date_Query,
            "universe": THS_WCQuery,
            "login": THS_iFinDLogin,
            "logout": THS_iFinDLogout,
        }
        def parse_login(value) -> int:
            result = int(value)
            if result not in {0, -201}:
                raise StockUpdateError(f"iFinD 登录失败（{result}）", error_code=result)
            return result

        result = self._execute(
            "THS_iFinDLogin",
            lambda: self._sdk["login"](
                self.settings.ifind_username,
                self.settings.ifind_password,
            ),
            parse_login,
            max_attempts=self.settings.ifind_max_attempts,
        )
        self._logged_in = True
        self._owns_login = result == 0

    def fetch_trading_dates(self, start: date, end: date) -> list[date]:
        if not self._logged_in or self._sdk is None:
            raise StockUpdateError("iFinD 尚未登录")
        if start > end:
            return []

        def query_calendar():
            return self._sdk["calendar"](
                TRADING_CALENDAR_EXCHANGE,
                TRADING_CALENDAR_PARAMS,
                start.isoformat(),
                end.isoformat(),
            )

        def parse_calendar(result):
            error_code = int(getattr(result, "errorcode", -1))
            if error_code != 0:
                raise StockUpdateError(
                    f"iFinD 交易日历查询失败（{error_code}）："
                    f"{getattr(result, 'errmsg', '未知错误')}",
                    error_code=error_code,
                )
            raw = getattr(result, "data", None)
            values = raw.split(",") if isinstance(raw, str) else getattr(result, "time", None)
            if values is None:
                raise DataContractError("iFinD 交易日历结果缺少 data/time")
            parsed: list[date] = []
            for value in values:
                text = str(value).strip()[:10]
                if not text:
                    continue
                try:
                    trading_date = date.fromisoformat(text)
                except ValueError as exc:
                    raise DataContractError(f"iFinD 交易日历包含非法日期：{text}") from exc
                if trading_date < start or trading_date > end:
                    raise DataContractError(
                        f"iFinD 交易日历返回范围外日期：{trading_date.isoformat()}"
                    )
                parsed.append(trading_date)
            if len(parsed) != len(set(parsed)):
                raise DataContractError("iFinD 交易日历包含重复日期")
            return sorted(parsed)

        return self._execute(
            "THS_Date_Query",
            query_calendar,
            parse_calendar,
            requested_items=1,
        )

    def fetch_csi300_universe(self) -> pd.DataFrame:
        if not self._logged_in or self._sdk is None:
            raise StockUpdateError("iFinD 尚未登录")

        def query_universe():
            return self._sdk["universe"]("沪深300成分股", "stock")

        def parse_universe(result):
            error_code = int(getattr(result, "errorcode", -1))
            if error_code != 0:
                raise StockUpdateError(
                    f"iFinD 沪深300成分查询失败（{error_code}）：{getattr(result, 'errmsg', '未知错误')}",
                    error_code=error_code,
                )
            frame = getattr(result, "data", None)
            required = {"股票代码", "股票简称"}
            if not isinstance(frame, pd.DataFrame) or not required.issubset(frame.columns):
                raise DataContractError("iFinD 沪深300成分结果缺少股票代码或股票简称")
            normalized = frame[["股票代码", "股票简称"]].rename(
                columns={"股票代码": "security_code", "股票简称": "security_name"}
            )
            normalized["security_code"] = normalized["security_code"].astype(str).str.upper()
            normalized["security_name"] = normalized["security_name"].astype(str).str.strip()
            if normalized["security_code"].duplicated().any():
                raise DataContractError("iFinD 沪深300成分结果包含重复证券")
            valid_codes = normalized["security_code"].str.fullmatch(r"\d{6}\.(SH|SZ)")
            if not valid_codes.all() or len(normalized) != 300:
                raise DataContractError(
                    f"iFinD 沪深300成分合同异常：rows={len(normalized)}, invalid={int((~valid_codes).sum())}"
                )
            return normalized.sort_values("security_code").reset_index(drop=True)

        return self._execute(
            "THS_WCQuery",
            query_universe,
            parse_universe,
            requested_items=1,
        )

    def fetch(
        self,
        codes: list[str],
        start: date,
        end: date,
        attempt: int = 1,
        max_attempts: int = 1,
    ) -> pd.DataFrame:
        if not self._logged_in or self._sdk is None:
            raise StockUpdateError("iFinD 尚未登录")
        def query_history():
            return self._sdk["history"](
                ",".join(codes),
                INDICATORS,
                self.history_params,
                start.isoformat(),
                end.isoformat(),
                "format:dataframe",
            )

        def parse_history(result):
            error_code = int(getattr(result, "errorcode", -1))
            if error_code != 0:
                raise StockUpdateError(
                    f"iFinD 日线查询失败（{error_code}）：{getattr(result, 'errmsg', '未知错误')}",
                    error_code=error_code,
                )
            return normalize_daily_frame(result.data)

        return self._execute(
            "THS_HD",
            query_history,
            parse_history,
            requested_items=len(codes),
            first_attempt=attempt,
            max_attempts=max_attempts,
        )

    def logout(self) -> None:
        if self._logged_in and self._owns_login and self._sdk is not None:
            started_at = monotonic()
            try:
                result = int(
                    self.runner.call(
                        self._sdk["logout"],
                        self.settings.ifind_timeout_seconds,
                    )
                )
            except Exception as exc:
                self.observer.record(operation="THS_iFinDLogout", status="error", started_at=started_at, error=exc)
            else:
                self.observer.record(
                    operation="THS_iFinDLogout",
                    status="success" if result == 0 else "error",
                    started_at=started_at,
                    error_code=result if result != 0 else None,
                    error=f"iFinD 注销失败（{result}）" if result != 0 else None,
                )
        self._logged_in = False
        self._owns_login = False


@dataclass(frozen=True)
class UpdatePlan:
    start_date: date
    end_date: date
    security_count: int
    current_min_date: date
    current_max_date: date
    pending_security_count: int
    missing_trading_dates: tuple[date, ...] = ()

    @property
    def needed(self) -> bool:
        return self.start_date <= self.end_date

    def as_dict(self) -> dict:
        return {
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "security_count": self.security_count,
            "current_min_date": self.current_min_date.isoformat(),
            "current_max_date": self.current_max_date.isoformat(),
            "pending_security_count": self.pending_security_count,
            "up_to_date_security_count": self.security_count - self.pending_security_count,
            "missing_trading_day_count": len(self.missing_trading_dates),
            "missing_trading_dates": [value.isoformat() for value in self.missing_trading_dates],
            "needed": self.needed,
            "adjustment": "unadjusted",
            "ifind_params": UNADJUSTED_PARAMS,
        }


class UpdateProcessLock:
    def __init__(self, path: Path):
        self.path = path
        self._fd: int | None = None

    def __enter__(self):
        try:
            self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(self._fd, str(os.getpid()).encode("ascii"))
        except FileExistsError as exc:
            raise StockUpdateError(f"已有更新任务或遗留锁文件：{self.path}") from exc
        return self

    def __exit__(self, exc_type, exc, traceback):
        if self._fd is not None:
            os.close(self._fd)
        self.path.unlink(missing_ok=True)


class StockDataUpdater:
    def __init__(self, db_path: Path, fetch: Callable[..., pd.DataFrame]):
        self.db_path = db_path
        self.fetch = fetch
        self.fetch_supports_attempt = "attempt" in inspect.signature(fetch).parameters

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    @staticmethod
    def _stock_tables(conn: sqlite3.Connection) -> list[str]:
        return [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = ? AND name LIKE 'stock_%' ORDER BY name",
                ("table",),
            )
        ]

    @staticmethod
    def _latest_dates(conn: sqlite3.Connection, tables: list[str]) -> dict[str, date]:
        latest: dict[str, date] = {}
        for table in tables:
            value = conn.execute(f'SELECT MAX(time) FROM "{table}"').fetchone()[0]
            if value:
                latest[table_to_code(table)] = date.fromisoformat(value)
        return latest

    @staticmethod
    def _latest_closes(conn: sqlite3.Connection, tables: list[str]) -> dict[str, float | None]:
        closes: dict[str, float | None] = {}
        for table in tables:
            row = conn.execute(
                f'SELECT close FROM "{table}" WHERE close IS NOT NULL ORDER BY time DESC LIMIT 1'
            ).fetchone()
            closes[table_to_code(table)] = float(row[0]) if row else None
        return closes

    def plan(self, end_date: date, trading_dates: list[date] | None = None) -> UpdatePlan:
        with self.connect() as conn:
            tables = self._stock_tables(conn)
            latest = self._latest_dates(conn, tables)
        if not latest:
            raise StockUpdateError("行情数据库没有可更新的股票表")
        current_min = min(latest.values())
        current_max = max(latest.values())
        missing_trading_dates = tuple(
            value for value in (trading_dates or []) if current_max < value <= end_date
        )
        return UpdatePlan(
            start_date=current_min + timedelta(days=1),
            end_date=end_date,
            security_count=len(latest),
            current_min_date=current_min,
            current_max_date=current_max,
            pending_security_count=sum(value < end_date for value in latest.values()),
            missing_trading_dates=missing_trading_dates,
        )

    def initialize_schema(self) -> None:
        with self.connect() as conn:
            self._initialize_journal(conn)

    @staticmethod
    def _initialize_journal(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS data_update_runs (
                run_id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                adjustment TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                status TEXT NOT NULL,
                rows_received INTEGER NOT NULL DEFAULT 0,
                rows_inserted INTEGER NOT NULL DEFAULT 0,
                failed_codes TEXT NOT NULL DEFAULT '[]',
                failed_reasons TEXT NOT NULL DEFAULT '{}',
                quality_issue_count INTEGER NOT NULL DEFAULT 0,
                quality_failed_codes TEXT NOT NULL DEFAULT '[]',
                error_category TEXT,
                error TEXT
            )
            """
        )
        existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(data_update_runs)")}
        migrations = {
            "failed_reasons": "TEXT NOT NULL DEFAULT '{}'",
            "quality_issue_count": "INTEGER NOT NULL DEFAULT 0",
            "quality_failed_codes": "TEXT NOT NULL DEFAULT '[]'",
            "error_category": "TEXT",
        }
        for column, definition in migrations.items():
            if column not in existing_columns:
                conn.execute(f"ALTER TABLE data_update_runs ADD COLUMN {column} {definition}")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS data_quality_issues (
                issue_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                security_code TEXT NOT NULL,
                trading_date TEXT NOT NULL,
                rule TEXT NOT NULL,
                field TEXT,
                observed TEXT NOT NULL,
                expected TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES data_update_runs(run_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_data_quality_issues_run "
            "ON data_quality_issues(run_id, security_code)"
        )

    @staticmethod
    def _record_quality_issues(conn: sqlite3.Connection, run_id: str, issues: list[QualityIssue]) -> None:
        created_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
        conn.executemany(
            """
            INSERT INTO data_quality_issues (
                issue_id, run_id, security_code, trading_date, rule, field,
                observed, expected, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    str(uuid.uuid4()),
                    run_id,
                    issue.code,
                    issue.trading_date,
                    issue.rule,
                    issue.field,
                    issue.observed,
                    issue.expected,
                    created_at,
                )
                for issue in issues
            ],
        )

    @staticmethod
    def _number_or_none(value):
        if value is None or (isinstance(value, float) and (math.isnan(value) or math.isinf(value))):
            return None
        return float(value)

    def _insert_frame(
        self,
        conn: sqlite3.Connection,
        frame: pd.DataFrame,
        latest: dict[str, date],
        valid_codes: set[str],
    ) -> int:
        inserted = 0
        for code, group in frame.groupby("thscode"):
            if code not in valid_codes:
                continue
            threshold = latest[code]
            rows = []
            newest = threshold
            for record in group.to_dict(orient="records"):
                record_date = date.fromisoformat(record["time"])
                if record_date <= threshold:
                    continue
                rows.append(
                    (
                        record["time"],
                        self._number_or_none(record["open"]),
                        self._number_or_none(record["high"]),
                        self._number_or_none(record["low"]),
                        self._number_or_none(record["close"]),
                        self._number_or_none(record["vwap"]),
                        self._number_or_none(record["volume"]),
                    )
                )
                newest = max(newest, record_date)
            if not rows:
                continue
            table = code_to_table(code)
            before = conn.total_changes
            conn.executemany(
                f'INSERT OR IGNORE INTO "{table}" '
                "(time, open, high, low, close, vwap, volume) VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            inserted += conn.total_changes - before
            latest[code] = newest
        return inserted

    def update(
        self,
        end_date: date,
        batch_size: int = 50,
        max_retries: int = 3,
        retry_delay: float = 2.0,
    ) -> dict:
        if batch_size < 1:
            raise ValueError("batch_size 必须大于 0")
        if max_retries < 1:
            raise ValueError("max_retries 必须大于 0")
        if retry_delay < 0:
            raise ValueError("retry_delay 不能小于 0")
        self.initialize_schema()
        plan = self.plan(end_date)
        if not plan.needed:
            return {
                **plan.as_dict(),
                "status": "up_to_date",
                "rows_received": 0,
                "rows_inserted": 0,
                "failed_codes": [],
                "failed_reasons": {},
                "quality_issue_count": 0,
                "quality_failed_codes": [],
            }

        run_id = str(uuid.uuid4())
        started_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
        lock_path = self.db_path.parent / ".stock_update.lock"
        rows_received = 0
        rows_inserted = 0
        failures: list[str] = []
        failed_reasons: dict[str, dict] = {}
        quality_issues: list[QualityIssue] = []
        quality_failed_codes: set[str] = set()

        with UpdateProcessLock(lock_path), self.connect() as conn:
            self._initialize_journal(conn)
            tables = self._stock_tables(conn)
            latest = self._latest_dates(conn, tables)
            previous_closes = self._latest_closes(conn, tables)
            valid_codes = set(latest)
            pending_codes = sorted(code for code, latest_date in latest.items() if latest_date < end_date)
            conn.execute(
                """
                INSERT INTO data_update_runs (
                    run_id, source, adjustment, started_at, start_date, end_date, status
                ) VALUES (?, 'iFinD', 'unadjusted', ?, ?, ?, 'running')
                """,
                (run_id, started_at, plan.start_date.isoformat(), end_date.isoformat()),
            )
            conn.commit()

            def fetch_with_retry(codes: list[str]) -> pd.DataFrame:
                error: Exception | None = None
                for attempt in range(max_retries):
                    try:
                        if self.fetch_supports_attempt:
                            return self.fetch(codes, plan.start_date, end_date, attempt=attempt + 1)
                        return self.fetch(codes, plan.start_date, end_date)
                    except (DataContractError, CircuitOpenError, CallTimeoutError, CallInProgressError):
                        raise
                    except Exception as exc:  # SDK errors vary by version.
                        error = exc
                        if attempt + 1 < max_retries:
                            time.sleep(
                                backoff_seconds(
                                    attempt + 1,
                                    retry_delay,
                                    retry_delay * 8,
                                )
                            )
                raise StockUpdateError(str(error))

            def process_batch(codes: list[str]) -> None:
                nonlocal rows_received, rows_inserted
                try:
                    frame = fetch_with_retry(codes)
                except (DataContractError, CircuitOpenError, CallTimeoutError, CallInProgressError):
                    raise
                except Exception as exc:
                    if len(codes) == 1:
                        failures.append(codes[0])
                        failed_reasons[codes[0]] = {
                            "category": classify_ifind_error(exc),
                            "message": f"{type(exc).__name__}; inspect ifind_call_events",
                        }
                        return
                    midpoint = len(codes) // 2
                    process_batch(codes[:midpoint])
                    process_batch(codes[midpoint:])
                    return
                rows_received += len(frame)
                batch_issues = validate_daily_frame(
                    frame,
                    requested_codes=set(codes),
                    start=plan.start_date,
                    end=end_date,
                    previous_closes=previous_closes,
                )
                invalid_codes = {issue.code for issue in batch_issues}
                if batch_issues:
                    quality_issues.extend(batch_issues)
                    quality_failed_codes.update(invalid_codes)
                    self._record_quality_issues(conn, run_id, batch_issues)
                valid_frame = frame.loc[~frame["thscode"].isin(invalid_codes)]
                rows_inserted += self._insert_frame(conn, valid_frame, latest, valid_codes)
                for code, group in valid_frame.groupby("thscode"):
                    close_values = group["close"].dropna()
                    if not close_values.empty:
                        previous_closes[code] = float(close_values.iloc[-1])

            try:
                for index in range(0, len(pending_codes), batch_size):
                    process_batch(pending_codes[index : index + batch_size])
                status = "partial" if failures or quality_failed_codes else "success"
                error_message = None
                error_category = None
            except Exception as exc:
                conn.rollback()
                rows_inserted = 0
                status = "failed"
                error_message = str(exc)
                error_category = "contract" if isinstance(exc, DataContractError) else classify_ifind_error(exc)
                raise
            finally:
                finished_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
                conn.execute(
                    """
                    UPDATE data_update_runs
                    SET finished_at = ?, status = ?, rows_received = ?, rows_inserted = ?,
                        failed_codes = ?, failed_reasons = ?, quality_issue_count = ?,
                        quality_failed_codes = ?, error_category = ?, error = ?
                    WHERE run_id = ?
                    """,
                    (
                        finished_at,
                        status,
                        rows_received,
                        rows_inserted,
                        json.dumps(failures, ensure_ascii=False),
                        json.dumps(failed_reasons, ensure_ascii=False),
                        len(quality_issues),
                        json.dumps(sorted(quality_failed_codes), ensure_ascii=False),
                        error_category,
                        error_message,
                        run_id,
                    ),
                )
                conn.commit()

        return {
            **plan.as_dict(),
            "run_id": run_id,
            "status": status,
            "rows_received": rows_received,
            "rows_inserted": rows_inserted,
            "failed_codes": failures,
            "failed_reasons": failed_reasons,
            "quality_issue_count": len(quality_issues),
            "quality_failed_codes": sorted(quality_failed_codes),
        }
