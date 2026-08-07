from __future__ import annotations

from collections import defaultdict

from .contracts import ScoreSignal, SignalSource, TargetPortfolio, equal_weight_top_n


def ranked_rows_to_target(
    rows: list[dict],
    *,
    source_type: SignalSource,
    source_id: str,
    signal_date: str,
    execution_date: str,
    top_n: int,
    code_key: str = "security_code",
    score_key: str = "score",
    rank_key: str = "rank",
) -> TargetPortfolio:
    signals = [
        ScoreSignal(
            source_type=source_type,
            source_id=source_id,
            as_of=signal_date,
            security_code=str(row[code_key]),
            score=float(row[score_key]),
            rank=int(row.get(rank_key, index + 1)),
        )
        for index, row in enumerate(rows)
    ]
    return equal_weight_top_n(signals, execution_date=execution_date, top_n=top_n)


def combine_factor_signals(
    groups: dict[str, list[ScoreSignal]],
    *,
    factor_weights: dict[str, float] | None,
    source_id: str,
    execution_date: str,
    top_n: int,
) -> TargetPortfolio:
    if not groups:
        raise ValueError("多因子组合不能为空")
    resolved = factor_weights or {factor_id: 1.0 for factor_id in groups}
    if any(factor_id not in groups for factor_id in resolved):
        raise ValueError("权重引用了不存在的因子")
    denominator = sum(abs(float(value)) for value in resolved.values())
    if denominator <= 0:
        raise ValueError("因子权重不能全部为零")
    scores: dict[str, float] = defaultdict(float)
    as_of: str | None = None
    for factor_id, signals in groups.items():
        if not signals:
            continue
        as_of = as_of or signals[0].as_of
        size = max(item.rank for item in signals)
        for item in signals:
            percentile = (size - item.rank) / max(1, size - 1)
            scores[item.security_code] += percentile * float(resolved.get(factor_id, 0.0)) / denominator
    combined = [
        ScoreSignal("factor_set", source_id, as_of or "", code, score, rank)
        for rank, (code, score) in enumerate(
            sorted(scores.items(), key=lambda item: (-item[1], item[0])), 1
        )
    ]
    return equal_weight_top_n(combined, execution_date=execution_date, top_n=top_n)


def strategy_weights_to_target(
    weights: dict[str, float],
    *,
    strategy_id: str,
    signal_date: str,
    execution_date: str,
) -> TargetPortfolio:
    normalized = {code: float(weight) for code, weight in weights.items() if weight > 0}
    if not normalized:
        raise ValueError("策略没有生成正目标权重")
    if sum(normalized.values()) > 1 + 1e-9:
        total = sum(normalized.values())
        normalized = {code: weight / total for code, weight in normalized.items()}
    return TargetPortfolio("strategy", strategy_id, signal_date, execution_date, normalized)
