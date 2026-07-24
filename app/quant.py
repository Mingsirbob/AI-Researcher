from __future__ import annotations

import math
import statistics
import threading
from datetime import date
from pathlib import Path

from .data_access import StockRepository
from .primitives import annualized_volatility, maximum_drawdown, period_return, sha256_text
from .research_store import ResearchStore


FACTOR_VERSION = "price-liquidity-v1"
MIN_OBSERVATIONS = 251
MAX_STALE_DAYS = 10
MAX_ABSOLUTE_DAILY_RETURN = 0.60


def _finite(value) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _round(value: float | None, digits: int = 4) -> float | None:
    return round(value, digits) if value is not None and math.isfinite(value) else None


def _range_position(values: list[float], periods: int = 250) -> float | None:
    recent = values[-periods:]
    if len(recent) < periods:
        return None
    low = min(recent)
    high = max(recent)
    return 0.5 if high == low else (recent[-1] - low) / (high - low)


def compute_security_factors(code: str, rows: list[dict], as_of: str) -> dict:
    cutoff = date.fromisoformat(as_of)
    reasons: list[str] = []
    if not rows:
        return {
            "security_code": code,
            "latest_trade_date": None,
            "observations": 0,
            "quality_status": "excluded",
            "quality_reasons": ["no_history"],
        }

    valid_rows = []
    for row in rows:
        close = _finite(row.get("close"))
        if close is not None and close > 0:
            valid_rows.append((row, close))
    if not valid_rows:
        return {
            "security_code": code,
            "latest_trade_date": None,
            "observations": 0,
            "quality_status": "excluded",
            "quality_reasons": ["no_valid_close"],
        }

    latest_row, latest_close = valid_rows[-1]
    latest_date = latest_row["time"]
    if rows[-1]["time"] != latest_date:
        reasons.append("latest_close_missing")
    if (cutoff - date.fromisoformat(latest_date)).days > MAX_STALE_DAYS:
        reasons.append("stale_latest_trade")
    if len(valid_rows) < MIN_OBSERVATIONS:
        reasons.append("insufficient_observations")

    recent_rows = [item[0] for item in valid_rows[-MIN_OBSERVATIONS:]]
    closes = [item[1] for item in valid_rows]
    previous_close = None
    for row in recent_rows:
        close = _finite(row.get("close"))
        open_price = _finite(row.get("open"))
        high = _finite(row.get("high"))
        low = _finite(row.get("low"))
        volume = _finite(row.get("volume"))
        vwap = _finite(row.get("vwap"))
        trading_fields = (open_price, high, low, volume)
        if any(value is not None for value in trading_fields):
            if any(value is None for value in trading_fields):
                reasons.append("incomplete_ohlcv")
            elif (
                open_price <= 0
                or high <= 0
                or low <= 0
                or volume < 0
                or high < max(open_price, low, close)
                or low > min(open_price, high, close)
            ):
                reasons.append("invalid_ohlcv")
            elif vwap is not None and (vwap <= 0 or vwap < low or vwap > high):
                reasons.append("invalid_vwap")
        if previous_close and abs(close / previous_close - 1) > MAX_ABSOLUTE_DAILY_RETURN:
            reasons.append("unadjusted_price_jump")
        previous_close = close

    latest_volume = _finite(latest_row.get("volume"))
    latest_vwap = _finite(latest_row.get("vwap"))
    if latest_volume is None or latest_vwap is None:
        reasons.append("latest_suspended_or_incomplete")

    traded = []
    for row, _ in valid_rows[-20:]:
        volume = _finite(row.get("volume"))
        vwap = _finite(row.get("vwap"))
        if volume is not None and volume >= 0 and vwap is not None and vwap > 0:
            traded.append((volume, vwap))
    if len(traded) < 15:
        reasons.append("insufficient_liquidity_samples")
    avg_volume = statistics.fmean(item[0] for item in traded) if traded else None
    avg_traded_value = statistics.fmean(item[0] * item[1] for item in traded) if traded else None
    volume_ratio = latest_volume / avg_volume if latest_volume is not None and avg_volume else None

    required_metrics = {
        "return_20d": period_return(closes, 20),
        "return_60d": period_return(closes, 60),
        "volatility_60d": annualized_volatility(closes, periods=60),
        "max_drawdown_250d": maximum_drawdown(closes, periods=250),
        "range_position_52w": _range_position(closes),
    }
    if any(value is None for value in required_metrics.values()):
        reasons.append("incomplete_factor_set")

    unique_reasons = list(dict.fromkeys(reasons))
    return {
        "security_code": code,
        "latest_trade_date": latest_date,
        "observations": len(valid_rows),
        "quality_status": "excluded" if unique_reasons else "passed",
        "quality_reasons": unique_reasons,
        "close": _round(latest_close),
        "return_20d": _round(required_metrics["return_20d"]),
        "return_60d": _round(required_metrics["return_60d"]),
        "volatility_60d": _round(required_metrics["volatility_60d"]),
        "avg_traded_value_20d": _round(avg_traded_value, 2),
        "volume_ratio_20d": _round(volume_ratio),
        "max_drawdown_250d": _round(required_metrics["max_drawdown_250d"]),
        "range_position_52w": _round(required_metrics["range_position_52w"]),
    }


def source_fingerprint(path: Path, security_count: int) -> tuple[str, int, int]:
    stat = path.stat()
    payload = f"{stat.st_size}:{stat.st_mtime_ns}:{security_count}"
    return sha256_text(payload, encoding="ascii"), stat.st_size, stat.st_mtime_ns


class FactorSnapshotService:
    def __init__(self, repository: StockRepository, store: ResearchStore):
        self.repository = repository
        self.store = store
        self._lock = threading.Lock()

    def generate(self, as_of: str) -> dict:
        date.fromisoformat(as_of)
        with self._lock:
            fingerprint, size, mtime_ns = source_fingerprint(
                self.repository.db_path, self.repository.security_count
            )
            existing = self.store.factor_snapshot_by_input(
                as_of=as_of,
                factor_version=FACTOR_VERSION,
                source_fingerprint=fingerprint,
            )
            if existing and existing["status"] == "completed":
                return {**existing, "reused": True}

            snapshot_id = self.store.start_factor_snapshot(
                as_of=as_of,
                factor_version=FACTOR_VERSION,
                source_fingerprint=fingerprint,
                source_db_size=size,
                source_db_mtime_ns=mtime_ns,
            )
            try:
                rows = [
                    compute_security_factors(code, history, as_of)
                    for code, history in self.repository.iter_histories(end=as_of, limit=320)
                ]
                self.store.finish_factor_snapshot(snapshot_id, rows)
            except Exception as exc:
                self.store.fail_factor_snapshot(snapshot_id, str(exc))
                raise
            result = self.store.factor_snapshot(snapshot_id)
            return {**result, "reused": False}
