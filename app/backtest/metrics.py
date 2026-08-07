from __future__ import annotations

import math
import statistics
from typing import Any


def annualized_return(total_return: float, periods: int, periods_per_year: int = 252) -> float | None:
    if periods < 2 or total_return <= -1:
        return None
    return (1 + total_return) ** (periods_per_year / (periods - 1)) - 1


def annualized_ratio(values: list[float], periods_per_year: int = 252) -> float | None:
    if len(values) < 2:
        return None
    deviation = statistics.stdev(values)
    return statistics.fmean(values) / deviation * math.sqrt(periods_per_year) if deviation else None


def calculate_metrics(
    *,
    nav_rows: list[tuple],
    rebalance_rows: list[tuple],
    trade_rows: list[tuple],
    initial_capital: float,
    realized_pnls: list[float] | None = None,
) -> dict[str, Any]:
    daily_returns = [float(row[5]) for row in nav_rows if row[5] is not None]
    paired_excess = [
        float(row[5]) - float(row[9])
        for row in nav_rows if row[5] is not None and row[9] is not None
    ]
    cumulative_return = float(nav_rows[-1][6])
    benchmark_return = float(nav_rows[-1][10])
    explicit_cost = sum(float(row[9]) for row in rebalance_rows)
    slippage_cost = sum(float(row[10]) for row in rebalance_rows)
    total_turnover = sum(float(row[8]) for row in rebalance_rows)
    realized = list(realized_pnls or [])
    wins = [value for value in realized if value > 0]
    losses = [value for value in realized if value < 0]
    volatility = statistics.stdev(daily_returns) * math.sqrt(252) if len(daily_returns) > 1 else None
    tracking_error = statistics.stdev(paired_excess) * math.sqrt(252) if len(paired_excess) > 1 else None
    return {
        "cumulative_return": cumulative_return,
        "annualized_return": annualized_return(cumulative_return, len(nav_rows)),
        "annualized_volatility": volatility,
        "sharpe_ratio": annualized_ratio(daily_returns),
        "sortino_ratio": _sortino(daily_returns),
        "calmar_ratio": _calmar(cumulative_return, min(float(row[7]) for row in nav_rows), len(nav_rows)),
        "max_drawdown": min(float(row[7]) for row in nav_rows),
        "benchmark_cumulative_return": benchmark_return,
        "benchmark_annualized_return": annualized_return(benchmark_return, len(nav_rows)),
        "excess_cumulative_return": (1 + cumulative_return) / (1 + benchmark_return) - 1,
        "tracking_error": tracking_error,
        "information_ratio": annualized_ratio(paired_excess),
        "excess_positive_day_ratio": sum(value > 0 for value in paired_excess) / len(paired_excess) if paired_excess else None,
        "average_turnover": total_turnover / len(rebalance_rows) if rebalance_rows else 0.0,
        "total_turnover": total_turnover,
        "explicit_cost": explicit_cost,
        "slippage_cost": slippage_cost,
        "total_cost": explicit_cost + slippage_cost,
        "total_cost_rate": (explicit_cost + slippage_cost) / initial_capital,
        "final_nav": float(nav_rows[-1][2]),
        "trade_count": len(trade_rows),
        "win_rate": len(wins) / len(realized) if realized else 0.0,
        "profit_factor": sum(wins) / abs(sum(losses)) if losses else (float("inf") if wins else 0.0),
    }


def _sortino(values: list[float]) -> float | None:
    downside = [value for value in values if value < 0]
    if not values or len(downside) < 2:
        return None
    deviation = statistics.stdev(downside)
    return statistics.fmean(values) / deviation * math.sqrt(252) if deviation else None


def _calmar(total_return: float, max_drawdown: float, periods: int) -> float | None:
    annual = annualized_return(total_return, periods)
    return annual / abs(max_drawdown) if annual is not None and max_drawdown < 0 else None
