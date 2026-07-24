from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
from pathlib import Path
from collections.abc import Iterable
from typing import Any, Callable


SECURITY_CODE_RE = re.compile(r"^(\d{6})\.(SZ|SH|BJ)$", re.IGNORECASE)
QLIB_INSTRUMENT_RE = re.compile(r"(SH|SZ|BJ)(\d{6})", re.IGNORECASE)


def canonical_json(
    value: Any,
    *,
    compact: bool = True,
    default: Callable[[Any], Any] | None = None,
) -> str:
    options: dict[str, Any] = {
        "ensure_ascii": False,
        "sort_keys": True,
    }
    if compact:
        options["separators"] = (",", ":")
    if default is not None:
        options["default"] = default
    return json.dumps(value, **options)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str, *, encoding: str = "utf-8") -> str:
    return sha256_bytes(value.encode(encoding))


def sha256_parts(parts: Iterable[bytes]) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part)
    return digest.hexdigest()


def canonical_hash(
    value: Any,
    *,
    compact: bool = True,
    default: Callable[[Any], Any] | None = None,
) -> str:
    return sha256_text(canonical_json(value, compact=compact, default=default))


def sha256_file(path: Path, *, block_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(block_size), b""):
            digest.update(block)
    return digest.hexdigest()


def period_return(
    values: list[float],
    periods: int,
    *,
    require_positive_base: bool = True,
) -> float | None:
    if periods < 1:
        raise ValueError("periods 必须为正整数")
    if len(values) <= periods:
        return None
    base = values[-periods - 1]
    invalid_base = base <= 0 if require_positive_base else base == 0
    if invalid_base:
        return None
    return values[-1] / base - 1


def annualized_volatility(
    values: list[float],
    periods: int = 60,
    *,
    require_full_window: bool = True,
    ignore_zero_bases: bool = False,
    trading_days: int = 252,
) -> float | None:
    if periods < 1:
        raise ValueError("periods 必须为正整数")
    recent = values[-(periods + 1) :]
    if require_full_window and len(recent) < periods + 1:
        return None
    if len(recent) < 3:
        return None
    returns = [
        recent[index] / recent[index - 1] - 1
        for index in range(1, len(recent))
        if not ignore_zero_bases or recent[index - 1]
    ]
    if len(returns) < 2:
        return None
    return statistics.stdev(returns) * math.sqrt(trading_days)


def maximum_drawdown(
    values: list[float],
    periods: int = 250,
    *,
    require_full_window: bool = True,
) -> float | None:
    if periods < 1:
        raise ValueError("periods 必须为正整数")
    recent = values[-periods:]
    if require_full_window and len(recent) < periods:
        return None
    if not recent:
        return None
    peak = recent[0]
    worst = 0.0
    for value in recent:
        peak = max(peak, value)
        if peak:
            worst = min(worst, value / peak - 1)
    return worst


def normalize_code(value: str) -> str:
    cleaned = value.strip().upper().replace("_", ".")
    if cleaned.startswith("STOCK."):
        cleaned = cleaned[6:]
    if re.fullmatch(r"\d{6}", cleaned):
        prefix = cleaned[0]
        market = "SH" if prefix in {"5", "6", "9"} else "BJ" if prefix in {"4", "8"} else "SZ"
        cleaned = f"{cleaned}.{market}"
    if not SECURITY_CODE_RE.fullmatch(cleaned):
        raise ValueError("股票代码格式应为 000001.SZ、600000.SH 或 920000.BJ")
    return cleaned


def code_to_table(code: str) -> str:
    return f"stock_{normalize_code(code).replace('.', '_')}"


def table_to_code(table: str) -> str:
    return normalize_code(table.removeprefix("stock_").replace("_", "."))


def code_to_qlib_instrument(code: str) -> str:
    normalized = normalize_code(code)
    number, market = normalized.split(".")
    return f"{market}{number}"


def qlib_instrument_to_code(value: str) -> str:
    match = QLIB_INSTRUMENT_RE.fullmatch(str(value).upper())
    if not match:
        raise ValueError(f"不支持的 Qlib 证券代码：{value}")
    return f"{match.group(2)}.{match.group(1).upper()}"
