from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


SignalSource = Literal["factor", "factor_set", "model", "strategy"]


@dataclass(frozen=True, slots=True)
class ScoreSignal:
    source_type: SignalSource
    source_id: str
    as_of: str
    security_code: str
    score: float
    rank: int


@dataclass(frozen=True, slots=True)
class TargetPortfolio:
    source_type: SignalSource
    source_id: str
    signal_date: str
    execution_date: str
    weights: dict[str, float]


@dataclass(slots=True)
class PositionLot:
    security_code: str
    acquired_date: str
    quantity: int
    cost_price: float


@dataclass(frozen=True, slots=True)
class BacktestResult:
    effective_start_date: str
    effective_end_date: str
    nav_rows: list[tuple]
    rebalance_rows: list[tuple]
    trade_rows: list[tuple]
    metrics: dict[str, Any]
    blocked_orders: list[dict[str, Any]] = field(default_factory=list)


def equal_weight_top_n(
    signals: list[ScoreSignal],
    *,
    execution_date: str,
    top_n: int,
) -> TargetPortfolio:
    if top_n < 1:
        raise ValueError("Top-N 必须为正数")
    if not signals:
        raise ValueError("没有可用于生成目标组合的信号")
    ordered = sorted(signals, key=lambda item: (item.rank, -item.score, item.security_code))
    selected = ordered[:top_n]
    weight = 1.0 / len(selected)
    return TargetPortfolio(
        source_type=selected[0].source_type,
        source_id=selected[0].source_id,
        signal_date=selected[0].as_of,
        execution_date=execution_date,
        weights={item.security_code: weight for item in selected},
    )
