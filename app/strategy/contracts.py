from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class StrategyArtifact:
    as_of: str
    source_hash: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class UniverseSnapshot(StrategyArtifact): ...


@dataclass(frozen=True, slots=True)
class FeatureSnapshot(StrategyArtifact): ...


@dataclass(frozen=True, slots=True)
class SignalSnapshot(StrategyArtifact): ...


@dataclass(frozen=True, slots=True)
class ResearchAssessment(StrategyArtifact): ...


@dataclass(frozen=True, slots=True)
class MacroRegimeSnapshot(StrategyArtifact): ...


@dataclass(frozen=True, slots=True)
class TargetPortfolio(StrategyArtifact): ...


@dataclass(frozen=True, slots=True)
class RiskDecision(StrategyArtifact): ...


@dataclass(frozen=True, slots=True)
class OrderPlan(StrategyArtifact): ...
