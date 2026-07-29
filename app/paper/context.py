from __future__ import annotations

import json
import math
import uuid
from datetime import date, datetime, timedelta
from typing import Any

from app.market.repository import StockRepository, normalize_code
from app.core.migrations import apply_migration
from app.paper.strategies import DEFAULT_PAPER_STRATEGY_ID, PAPER_STRATEGIES
from app.core.primitives import canonical_hash
from app.decision.portfolio import (
    COMMISSION_RATE,
    DEFAULT_MAX_INDUSTRY_WEIGHT,
    DEFAULT_MAX_PAIR_CORRELATION,
    DEFAULT_MAX_POSITION_WEIGHT,
    DEFAULT_TARGET_GROSS_EXPOSURE,
    MIN_COMMISSION,
    SELL_STAMP_DUTY_RATE,
    SLIPPAGE_RATE,
    PortfolioDecisionService,
)
from app.research.store import ResearchStore, utc_now
from app.quant.store import QuantStore
from app.core.sqlite_store import SQLiteStore, migrate_legacy_tables


PAPER_BENCHMARKS = {
    "000001.SH": "上证指数",
    "000300.SH": "沪深300",
    "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
    "000688.SH": "科创50",
    "000510.CSI": "中证A500",
}

PAPER_TRADING_TABLES = (
    "paper_strategy",
    "paper_account",
    "paper_daily_run",
    "paper_order",
    "paper_position",
    "paper_nav_snapshot",
    "paper_realtime_quote",
    "paper_benchmark_price",
)


def _normalize_paper_quote_code(value: str) -> str:
    cleaned = value.strip().upper().replace("_", ".")
    if len(cleaned) == 10 and cleaned[:6].isdigit() and cleaned[6:] == ".CSI":
        return cleaned
    return normalize_code(cleaned)
