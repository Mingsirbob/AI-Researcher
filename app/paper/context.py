from __future__ import annotations

import json
import math
import uuid
from datetime import date, datetime, timedelta
from typing import Any

from app.backtest.market_rules import ChinaAConfig, ChinaAMarketRules
from app.market.repository import StockRepository, normalize_code
from app.market.stock_pool import MAJOR_INDEX_BENCHMARKS, StockPoolStore
from app.core.migrations import apply_migration
from app.paper.strategies import (
    DEFAULT_PAPER_STRATEGY_ID,
    LIGHTGBM_SHADOW_STRATEGY_ID,
    PAPER_STRATEGIES,
)
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


PAPER_BENCHMARKS = MAJOR_INDEX_BENCHMARKS

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
