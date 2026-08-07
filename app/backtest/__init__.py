"""Shared backtest building blocks for factor, model and strategy research."""

from .contracts import BacktestResult, PositionLot, ScoreSignal, TargetPortfolio
from .engine import DailyBacktestEngine
from .market_rules import ChinaAConfig, ChinaAMarketRules

__all__ = [
    "BacktestResult",
    "ChinaAConfig",
    "ChinaAMarketRules",
    "DailyBacktestEngine",
    "PositionLot",
    "ScoreSignal",
    "TargetPortfolio",
]
