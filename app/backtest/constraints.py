from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PortfolioConstraints:
    max_weight: float | None = None
    min_weight: float | None = None
    max_group_weight: float | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("max_weight", self.max_weight),
            ("min_weight", self.min_weight),
            ("max_group_weight", self.max_group_weight),
        ):
            if value is not None and not 0 < value <= 1:
                raise ValueError(f"{name} 必须在 (0, 1] 范围内")


def apply_constraints(
    weights: dict[str, float],
    constraints: PortfolioConstraints,
    *,
    groups: dict[str, str] | None = None,
) -> dict[str, float]:
    result = {code: max(0.0, float(weight)) for code, weight in weights.items() if weight > 0}
    if not result:
        return {}
    total = sum(result.values())
    if total > 1 + 1e-9:
        result = {code: weight / total for code, weight in result.items()}

    if constraints.min_weight is not None:
        result = {
            code: weight for code, weight in result.items()
            if weight + 1e-12 >= constraints.min_weight
        }
    if not result:
        return {}

    if constraints.max_weight is not None:
        result = _cap_and_redistribute(result, constraints.max_weight)

    if constraints.max_group_weight is not None and groups:
        for group in sorted(set(groups.values())):
            members = [code for code in result if groups.get(code) == group]
            exposure = sum(result[code] for code in members)
            if exposure > constraints.max_group_weight and exposure > 0:
                scale = constraints.max_group_weight / exposure
                for code in members:
                    result[code] *= scale
    return result


def _cap_and_redistribute(weights: dict[str, float], cap: float) -> dict[str, float]:
    result = dict(weights)
    for _ in range(50):
        excess = sum(max(0.0, weight - cap) for weight in result.values())
        for code in result:
            result[code] = min(result[code], cap)
        if excess <= 1e-12:
            break
        room = {code: cap - weight for code, weight in result.items() if weight < cap - 1e-12}
        capacity = sum(room.values())
        if capacity <= 1e-12:
            break
        for code, available in room.items():
            result[code] += min(available, excess * available / capacity)
    return result
