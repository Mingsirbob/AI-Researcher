from __future__ import annotations

import math
import statistics
import uuid
from datetime import date

from app.backtest.market_rules import ChinaAConfig, ChinaAMarketRules
from app.market.repository import StockRepository, normalize_code
from app.paper.strategies import (
    DEFAULT_PAPER_STRATEGY_ID,
    MULTIFACTOR_LINEAR_STRATEGY_ID,
    strategy_definition,
)
from app.core.primitives import canonical_hash
from app.research.assessment import (
    RESEARCH_ASSESSMENT_POLICY_VERSION,
    RESEARCH_ASSESSMENT_SCHEMA_VERSION,
    SIGNAL_MULTIPLIERS,
)
from app.research.store import ResearchStore
from app.quant.store import QuantStore
from app.strategy.code_runtime import CodeStrategyRuntime


STRATEGY_VERSION = "paper-evidence-risk-v3.2"
COMMISSION_RATE = 0.0003
MIN_COMMISSION = 5.0
SELL_STAMP_DUTY_RATE = 0.0005
SLIPPAGE_RATE = 0.0005
MIN_OBSERVATIONS = 251
MIN_AVG_TRADED_VALUE_20D = 100_000_000.0
MAX_VOLATILITY_60D = 0.80
MAX_DRAWDOWN_250D_ABS = 0.50
DEFAULT_TARGET_GROSS_EXPOSURE = 0.50
DEFAULT_MAX_POSITION_WEIGHT = 0.12
DEFAULT_MIN_POSITION_WEIGHT = 0.05
DEFAULT_MAX_INDUSTRY_WEIGHT = 0.20
DEFAULT_MAX_PAIR_CORRELATION = 0.85


class PortfolioDecisionService:
    """Builds deterministic portfolio plans without mutating paper account state."""

    def __init__(
        self,
        repository: StockRepository,
        store: QuantStore,
        research_store: ResearchStore | None = None,
        stock_pool_store=None,
        code_runtime: CodeStrategyRuntime | None = None,
    ) -> None:
        self.repository = repository
        self.store = store
        self.research_store = research_store or store
        self.stock_pool_store = stock_pool_store
        self.code_runtime = code_runtime or CodeStrategyRuntime()

    def sources(self, as_of: str, strategy: dict | None = None) -> tuple[dict, dict]:
        date.fromisoformat(as_of)
        factor = self.store.latest_compatible_factor_snapshot(as_of)
        shadow = self.store.current_shadow_for_date(as_of)
        if not factor or factor["as_of"] != as_of:
            raise ValueError(f"缺少 {as_of} 的全市场因子快照")
        if strategy and strategy.get("signal_source") in {"multifactor_linear", "python_code"}:
            shadow = {
                "snapshot_id": factor["snapshot_id"], "as_of": as_of,
                "status": "not_required", "universe_size": factor["passed_securities"],
                "signal_count": factor["passed_securities"],
            }
        elif not shadow or shadow["as_of"] != as_of or shadow["status"] != "current_shadow_ready":
            raise ValueError(f"缺少 {as_of} 且通过数据合同的 Current Shadow")
        return factor, shadow

    def _factor_metrics(self, snapshot_id: str, codes: list[str]) -> dict[str, dict]:
        if not codes:
            return {}
        placeholders = ",".join("?" for _ in codes)
        with self.store.connect() as conn:
            rows = conn.execute(
                f"""SELECT f.*, COALESCE(m.security_name, f.security_code) security_name,
                           m.industry_l1
                    FROM security_factor_snapshot f
                    JOIN security_master m ON m.security_code=f.security_code
                    WHERE f.snapshot_id=? AND f.security_code IN ({placeholders})""",
                [snapshot_id, *codes],
            ).fetchall()
        return {row["security_code"]: dict(row) for row in rows}

    def _multifactor_signals(self, snapshot_id: str, strategy: dict) -> list[dict]:
        weights = strategy["config"].get("factor_weights") or {}
        fields = tuple(weights)
        if not fields:
            raise ValueError("线性多因子策略没有配置因子权重")
        with self.store.connect() as conn:
            rows = [
                dict(row)
                for row in conn.execute(
                    f"""SELECT f.*, COALESCE(m.security_name, f.security_code) security_name
                        FROM security_factor_snapshot f
                        LEFT JOIN security_master m ON m.security_code=f.security_code
                        WHERE f.snapshot_id=? AND f.quality_status='passed'""",
                    (snapshot_id,),
                ).fetchall()
            ]
        eligible = [
            row for row in rows
            if all(
                row.get(field) is not None and math.isfinite(float(row[field]))
                for field in fields
            )
        ]
        if not eligible:
            return []
        percentiles: dict[str, dict[str, float]] = {}
        for field in fields:
            ordered = sorted(eligible, key=lambda row: (float(row[field]), row["security_code"]))
            denominator = max(1, len(ordered) - 1)
            percentiles[field] = {
                row["security_code"]: index / denominator
                for index, row in enumerate(ordered)
            }
        scored = []
        for row in eligible:
            code = row["security_code"]
            score = sum(float(weight) * percentiles[field][code] for field, weight in weights.items())
            scored.append({
                "security_code": code,
                "security_name": row["security_name"],
                "source_instrument": None,
                "score": score,
                "factor_contributions": {
                    field: float(weight) * percentiles[field][code]
                    for field, weight in weights.items()
                },
            })
        scored.sort(key=lambda item: (-item["score"], item["security_code"]))
        size = len(scored)
        for index, item in enumerate(scored, start=1):
            item["cross_section_rank"] = index
            item["cross_section_size"] = size
            item["percentile"] = 1.0 if size == 1 else 1 - (index - 1) / (size - 1)
        return scored

    def _resolve_exposure(
        self,
        *,
        strategy: dict,
        factor_snapshot_id: str,
        signals: list[dict],
        requested_exposure: float,
    ) -> dict:
        config = strategy.get("config") or {}
        mode = config.get("exposure_mode", "fixed")
        if mode != "dynamic":
            target = float(config.get("target_gross_exposure", requested_exposure))
            return {
                "mode": "fixed",
                "regime": "fixed",
                "target_gross_exposure": target,
            }

        metrics = self._factor_metrics(
            factor_snapshot_id,
            [item["security_code"] for item in signals],
        )
        valid_returns = [
            float(item["return_20d"])
            for item in metrics.values()
            if item.get("return_20d") is not None
            and math.isfinite(float(item["return_20d"]))
        ]
        valid_volatility = [
            float(item["volatility_60d"])
            for item in metrics.values()
            if item.get("volatility_60d") is not None
            and math.isfinite(float(item["volatility_60d"]))
        ]
        breadth = (
            sum(value > 0 for value in valid_returns) / len(valid_returns)
            if valid_returns else 0.0
        )
        median_volatility = statistics.median(valid_volatility) if valid_volatility else 1.0
        bullish = float(config.get("bullish_breadth_threshold", 0.6))
        bearish = float(config.get("bearish_breadth_threshold", 0.4))
        high_volatility = float(config.get("high_volatility_threshold", 0.5))
        if breadth <= bearish or median_volatility >= high_volatility:
            regime = "defensive"
            target = float(config.get("minimum_exposure", 0.2))
        elif breadth >= bullish:
            regime = "bullish"
            target = float(config.get("maximum_exposure", 0.8))
        else:
            regime = "neutral"
            target = float(config.get("neutral_exposure", 0.5))
        return {
            "mode": "dynamic",
            "regime": regime,
            "target_gross_exposure": target,
            "market_breadth_20d": breadth,
            "median_volatility_60d": median_volatility,
            "observations": min(len(valid_returns), len(valid_volatility)),
            "thresholds": {
                "bullish_breadth": bullish,
                "bearish_breadth": bearish,
                "high_volatility": high_volatility,
            },
        }

    @staticmethod
    def _market_rules(strategy: dict) -> ChinaAMarketRules:
        config = strategy.get("config") or {}
        return ChinaAMarketRules(ChinaAConfig(
            commission_rate=float(config.get("commission_rate", COMMISSION_RATE)),
            minimum_commission=float(config.get("minimum_commission", MIN_COMMISSION)),
            stamp_duty_rate=float(config.get("stamp_duty_rate", SELL_STAMP_DUTY_RATE)),
            transfer_fee_rate=float(config.get("transfer_fee_rate", 0.00001)),
            slippage_rate=float(config.get("slippage_rate", SLIPPAGE_RATE)),
            lot_size=int(config.get("lot_size", 100)),
            max_volume_participation=float(config.get("max_volume_participation", 0.1)),
        ))

    def _code_signals(
        self,
        factor: dict,
        strategy: dict,
        *,
        account: dict | None = None,
        positions: list[dict] | None = None,
    ) -> list[dict]:
        source = strategy.get("source_code")
        if not source:
            raise ValueError("代码策略版本缺少 source_code")
        config = strategy.get("config") or {}
        pool_id = str(config.get("pool_id") or "csi300")
        if self.stock_pool_store:
            pool = self.stock_pool_store.resolve(pool_id, date.fromisoformat(factor["as_of"]))
            universe = pool["members"]
        else:
            pool = {"pool_id": pool_id, "as_of": factor["as_of"]}
            universe = []
        allowed = {item["security_code"] for item in universe}
        with self.store.connect() as conn:
            rows = [
                dict(row)
                for row in conn.execute(
                    """SELECT f.*, COALESCE(m.security_name, f.security_code) security_name,
                              m.industry_l1
                       FROM security_factor_snapshot f
                       LEFT JOIN security_master m ON m.security_code=f.security_code
                       WHERE f.snapshot_id=? AND f.quality_status='passed'
                       ORDER BY f.security_code""",
                    (factor["snapshot_id"],),
                ).fetchall()
            ]
        if not universe:
            universe = [
                {"security_code": row["security_code"], "security_name": row["security_name"], "weight": None}
                for row in rows
            ]
            allowed = {item["security_code"] for item in universe}
        factors = [row for row in rows if row["security_code"] in allowed]
        raw = self.code_runtime.execute(source, {
            "as_of": factor["as_of"],
            "universe": universe,
            "factors": factors,
            "positions": positions or [],
            "cash": float((account or {}).get("cash", 0.0)),
            "params": config.get("strategy_params") or {},
            "pool_snapshot": {"pool_id": pool_id, "as_of": pool.get("as_of")},
            "filter_pipeline_id": config.get("filter_pipeline_id", "factor_quality_passed"),
        })
        names = {row["security_code"]: row["security_name"] for row in factors}
        scored = [
            {
                **item,
                "security_name": names.get(item["security_code"], item["security_code"]),
                "source_instrument": None,
                "factor_contributions": {"python_code": item["score"]},
            }
            for item in raw
        ]
        scored.sort(key=lambda item: (-item["score"], item["security_code"]))
        size = len(scored)
        for index, item in enumerate(scored, start=1):
            item["cross_section_rank"] = index
            item["cross_section_size"] = size
            item["percentile"] = 1.0 if size == 1 else 1 - (index - 1) / (size - 1)
        return scored

    def strategy_signals(
        self,
        factor: dict,
        shadow: dict,
        strategy: dict,
        *,
        account: dict | None = None,
        positions: list[dict] | None = None,
    ) -> list[dict]:
        if strategy.get("signal_source") == "python_code":
            return self._code_signals(factor, strategy, account=account, positions=positions)
        if strategy.get("signal_source") == "multifactor_linear":
            return self._multifactor_signals(factor["snapshot_id"], strategy)
        return self.store.list_current_shadow_signals(
            shadow["snapshot_id"], limit=min(500, int(shadow["signal_count"]))
        )["items"]

    def _tradability(self, code: str, as_of: str) -> dict:
        try:
            rows = self.repository.get_history(code, end=as_of, limit=30)
        except KeyError:
            return {"passed": False, "reason": "missing_local_price_table"}
        row = rows[-1] if rows else None
        if not row or row["time"] != as_of:
            return {"passed": False, "reason": "missing_as_of_quote"}
        numeric = {}
        for field in ("open", "high", "low", "close", "volume"):
            try:
                numeric[field] = float(row[field]) if row[field] is not None else None
            except (TypeError, ValueError):
                numeric[field] = None
        if any(numeric[field] is None or numeric[field] <= 0 for field in ("open", "high", "low", "close")):
            return {"passed": False, "reason": "invalid_as_of_ohlc"}
        if numeric["volume"] is None or numeric["volume"] <= 0:
            return {"passed": False, "reason": "suspended_or_no_volume"}
        return {"passed": True, "reason": None, "close": numeric["close"], "volume": numeric["volume"]}

    def _daily_returns(self, code: str, as_of: str, periods: int = 60) -> dict[str, float]:
        try:
            rows = self.repository.get_history(code, end=as_of, limit=periods + 10)
        except KeyError:
            return {}
        result: dict[str, float] = {}
        previous = None
        for row in rows:
            close = float(row["close"]) if row.get("close") is not None else None
            if close and close > 0 and previous and previous > 0:
                result[row["time"]] = close / previous - 1
            if close and close > 0:
                previous = close
        return dict(list(result.items())[-periods:])

    @staticmethod
    def _correlation(left: dict[str, float], right: dict[str, float]) -> float | None:
        dates = sorted(set(left) & set(right))
        if len(dates) < 30:
            return None
        x = [left[item] for item in dates]
        y = [right[item] for item in dates]
        sx, sy = statistics.stdev(x), statistics.stdev(y)
        if sx == 0 or sy == 0:
            return None
        return statistics.covariance(x, y) / (sx * sy)

    @staticmethod
    def _bounded_weights(items: list[dict], total: float, minimum: float, maximum: float) -> dict[str, float]:
        if not items:
            return {}
        total = min(total, maximum * len(items))
        minimum = min(minimum, total / len(items))
        inverse = {item["security_code"]: 1 / max(float(item["volatility_60d"]), 0.05) for item in items}
        scale = sum(inverse.values())
        weights = {code: min(maximum, max(minimum, total * value / scale)) for code, value in inverse.items()}
        for _ in range(20):
            difference = total - sum(weights.values())
            if abs(difference) < 1e-9:
                break
            if difference > 0:
                eligible = [code for code in weights if weights[code] < maximum - 1e-9]
                room = {code: maximum - weights[code] for code in eligible}
            else:
                eligible = [code for code in weights if weights[code] > minimum + 1e-9]
                room = {code: weights[code] - minimum for code in eligible}
            if not eligible:
                break
            denominator = sum(inverse[code] for code in eligible)
            for code in eligible:
                allocation = abs(difference) * inverse[code] / denominator
                delta = min(room[code], allocation)
                weights[code] += delta if difference > 0 else -delta
        return weights

    @staticmethod
    def _apply_industry_cap(items: list[dict], weights: dict[str, float], cap: float, maximum: float) -> dict[str, float]:
        original_total = sum(weights.values())
        for _ in range(10):
            groups: dict[str, list[str]] = {}
            for item in items:
                industry = str(item.get("industry_l1") or "").strip()
                if industry:
                    groups.setdefault(industry, []).append(item["security_code"])
            changed = False
            for codes in groups.values():
                group_weight = sum(weights[code] for code in codes)
                if group_weight > cap + 1e-9:
                    ratio = cap / group_weight
                    for code in codes:
                        weights[code] *= ratio
                    changed = True
            difference = original_total - sum(weights.values())
            if difference <= 1e-9:
                break
            eligible = []
            for item in items:
                code = item["security_code"]
                industry = str(item.get("industry_l1") or "").strip()
                group_weight = sum(
                    weights[other["security_code"]]
                    for other in items
                    if industry and str(other.get("industry_l1") or "").strip() == industry
                )
                industry_room = cap - group_weight if industry else difference
                if weights[code] < maximum - 1e-9 and industry_room > 1e-9:
                    eligible.append((code, min(maximum - weights[code], industry_room)))
            if not eligible:
                break
            share = difference / len(eligible)
            for code, room in eligible:
                weights[code] += min(share, room)
            if not changed and abs(original_total - sum(weights.values())) < 1e-9:
                break
        return weights

    def evaluate_candidates(
        self, signals: list[dict], factor_snapshot_id: str, as_of: str,
        *, research_enabled: bool = True,
    ) -> list[dict]:
        metrics = self._factor_metrics(factor_snapshot_id, [item["security_code"] for item in signals])
        evaluations = []
        for signal in signals:
            code = signal["security_code"]
            factor = metrics.get(code, {})
            tradability = self._tradability(code, as_of)
            quant_gates = [
                {"name": "factor_quality", "passed": factor.get("quality_status") == "passed", "observed": factor.get("quality_status"), "expected": "passed"},
                {"name": "observations", "passed": int(factor.get("observations") or 0) >= MIN_OBSERVATIONS, "observed": factor.get("observations"), "expected": f">= {MIN_OBSERVATIONS}"},
                {"name": "liquidity", "passed": float(factor.get("avg_traded_value_20d") or 0) >= MIN_AVG_TRADED_VALUE_20D, "observed": factor.get("avg_traded_value_20d"), "expected": f">= {MIN_AVG_TRADED_VALUE_20D}"},
                {"name": "volatility", "passed": 0 < float(factor.get("volatility_60d") or 0) <= MAX_VOLATILITY_60D, "observed": factor.get("volatility_60d"), "expected": f"0 < x <= {MAX_VOLATILITY_60D}"},
                {"name": "drawdown", "passed": float(factor.get("max_drawdown_250d") or -1) >= -MAX_DRAWDOWN_250D_ABS, "observed": factor.get("max_drawdown_250d"), "expected": f">= {-MAX_DRAWDOWN_250D_ABS}"},
                {"name": "tradability", "passed": tradability["passed"], "observed": tradability.get("reason") or "tradable", "expected": "positive OHLC and volume on as_of"},
            ]
            artifact = (
                self.research_store.latest_research_assessment(code, as_of)
                if research_enabled else None
            )
            assessment = artifact["payload"] if artifact else None
            assessment_valid = bool(
                artifact
                and artifact["as_of"] == as_of
                and artifact["schema_version"] == RESEARCH_ASSESSMENT_SCHEMA_VERSION
                and assessment.get("policy_version") == RESEARCH_ASSESSMENT_POLICY_VERSION
                and canonical_hash(assessment) == artifact["snapshot_hash"]
            )
            assessment_signal = (
                assessment.get("signal") if assessment_valid
                else "admit" if not research_enabled else "defer"
            )
            if assessment_signal not in SIGNAL_MULTIPLIERS:
                assessment_signal = "defer"
                assessment_valid = False
            research_summary = {
                "assessment_id": artifact["artifact_id"] if artifact else None,
                "research_run_id": artifact["run_id"] if artifact else None,
                "as_of": artifact["as_of"] if artifact else None,
                "schema_version": artifact["schema_version"] if artifact else None,
                "policy_version": assessment.get("policy_version") if assessment else None,
                "signal": assessment_signal,
                "evidence_confidence": assessment.get("evidence_confidence") if assessment_valid else 0.0,
                "weight_multiplier": SIGNAL_MULTIPLIERS[assessment_signal],
                "material_negative_count": len(assessment.get("material_negatives", [])) if assessment_valid else 0,
                "catalyst_count": len(assessment.get("catalysts", [])) if assessment_valid else 0,
                "invalidating_conditions": assessment.get("invalidating_conditions", []) if assessment_valid else [],
                "reasons": assessment.get("reasons", []) if assessment_valid else (
                    ["策略未启用研究过滤，按量化信号直接进入组合门禁"]
                    if not research_enabled else
                    ["缺少当日、当前策略且哈希有效的 ResearchAssessment"]
                ),
                "snapshot_hash": artifact["snapshot_hash"] if artifact else None,
                "valid": assessment_valid or not research_enabled,
            }
            research_gate = {
                "name": "research_assessment",
                "passed": (assessment_valid or not research_enabled)
                and assessment_signal in {"admit", "reduce"},
                "observed": assessment_signal,
                "expected": "admit or reduce with current policy and valid hash",
            }
            quant_passed = all(item["passed"] for item in quant_gates)
            evaluations.append({
                **signal,
                "industry_l1": factor.get("industry_l1"),
                "observations": factor.get("observations"),
                "avg_traded_value_20d": factor.get("avg_traded_value_20d"),
                "volatility_60d": factor.get("volatility_60d"),
                "max_drawdown_250d": factor.get("max_drawdown_250d"),
                "close": tradability.get("close") or factor.get("close"),
                "gates": [*quant_gates, research_gate],
                "risk_status": "passed" if quant_passed else "rejected",
                "research_status": assessment_signal,
                "decision_status": (
                    "eligible" if quant_passed and research_gate["passed"]
                    else assessment_signal if quant_passed else "risk_rejected"
                ),
                "research_assessment": research_summary,
                "rejection_reasons": [item["name"] for item in quant_gates if not item["passed"]]
                    + ([] if research_gate["passed"] else [f"research_{assessment_signal}"]),
                "returns_60d": self._daily_returns(code, as_of),
            })
        return evaluations

    def research_targets(
        self, *, as_of: str, strategy: dict, limit: int = 5, hold_rank_buffer: int = 30
    ) -> dict:
        factor, shadow = self.sources(as_of, strategy)
        signals = self.strategy_signals(factor, shadow, strategy)[:hold_rank_buffer]
        evaluations = self.evaluate_candidates(
            signals, factor["snapshot_id"], as_of,
            research_enabled=strategy.get("research_enabled", True),
        )
        targets = [item for item in evaluations if item["risk_status"] == "passed"][:limit]
        return {
            "as_of": as_of,
            "factor_snapshot_id": factor["snapshot_id"],
            "shadow_snapshot_id": shadow["snapshot_id"],
            "strategy": strategy,
            "targets": [
                {
                    "security_code": item["security_code"],
                    "security_name": item["security_name"],
                    "shadow_rank": item["cross_section_rank"],
                    "score": item["score"],
                    "current_research_status": item["research_status"],
                    "current_assessment": item["research_assessment"],
                }
                for item in targets
            ],
        }

    def review_holdings(
        self, *, account_id: str, positions: list[dict], as_of: str, strategy: dict | None = None
    ) -> dict:
        strategy = strategy or strategy_definition(DEFAULT_PAPER_STRATEGY_ID)
        factor, shadow = self.sources(as_of, strategy)
        strategy_signal_by_code = {
            item["security_code"]: item
            for item in self.strategy_signals(factor, shadow, strategy, positions=positions)
        }
        signals = []
        shadow_covered: dict[str, bool] = {}
        for position in positions:
            code = position["security_code"]
            signal = strategy_signal_by_code.get(code)
            shadow_covered[code] = signal is not None
            signals.append(signal or {
                "security_code": code,
                "security_name": position["security_name"],
                "source_instrument": None,
                "score": None,
                "cross_section_rank": None,
                "cross_section_size": shadow["universe_size"],
                "percentile": None,
            })
        evaluations = self.evaluate_candidates(
            signals, factor["snapshot_id"], as_of,
            research_enabled=strategy.get("research_enabled", True),
        )
        positions_by_code = {item["security_code"]: item for item in positions}
        items = []
        for evaluation in evaluations:
            code = evaluation["security_code"]
            research_status = evaluation["research_status"]
            if research_status == "veto" or evaluation["risk_status"] == "rejected":
                action = "exit_review"
            elif research_status == "reduce":
                action = "reduce_review"
            elif research_status == "defer" or not shadow_covered[code]:
                action = "freeze"
            else:
                action = "maintain"
            items.append({
                **evaluation,
                "position": positions_by_code[code],
                "shadow_covered": shadow_covered[code],
                "review_action": action,
            })
        counts = {
            name: sum(item["review_action"] == name for item in items)
            for name in ("maintain", "reduce_review", "freeze", "exit_review")
        }
        return {
            "account_id": account_id,
            "as_of": as_of,
            "factor_snapshot_id": factor["snapshot_id"],
            "shadow_snapshot_id": shadow["snapshot_id"],
            "strategy": strategy,
            "position_count": len(items),
            "counts": counts,
            "items": items,
        }

    def build_plan(
        self,
        *,
        account: dict,
        positions: list[dict],
        as_of: str,
        run_id: str,
        created_at: str,
        top_n: int,
        hold_rank_buffer: int,
        target_gross_exposure: float,
        max_position_weight: float,
        max_industry_weight: float,
        max_pair_correlation: float,
        strategy: dict | None = None,
    ) -> dict:
        strategy = strategy or strategy_definition(DEFAULT_PAPER_STRATEGY_ID)
        factor, shadow = self.sources(as_of, strategy)
        position_by_code = {item["security_code"]: item for item in positions}
        all_signals = self.strategy_signals(
            factor, shadow, strategy, account=account, positions=positions
        )
        exposure = self._resolve_exposure(
            strategy=strategy,
            factor_snapshot_id=factor["snapshot_id"],
            signals=all_signals,
            requested_exposure=target_gross_exposure,
        )
        target_gross_exposure = exposure["target_gross_exposure"]
        rules = self._market_rules(strategy)
        signals = all_signals[:hold_rank_buffer]
        buffered_codes = {item["security_code"] for item in signals}
        all_signal_by_code = {item["security_code"]: item for item in all_signals}
        rank_exit_codes = {
            code for code in position_by_code
            if code in all_signal_by_code and code not in buffered_codes
        }
        signal_by_code = {item["security_code"]: item for item in signals}
        holding_shadow_covered = {}
        for position in positions:
            code = position["security_code"]
            signal = all_signal_by_code.get(code)
            holding_shadow_covered[code] = signal is not None
            signal_by_code.setdefault(code, signal or {
                "security_code": code,
                "security_name": position["security_name"],
                "source_instrument": None,
                "score": None,
                "cross_section_rank": None,
                "cross_section_size": shadow["universe_size"],
                "percentile": None,
            })
        evaluations = self.evaluate_candidates(
            list(signal_by_code.values()), factor["snapshot_id"], as_of,
            research_enabled=strategy.get("research_enabled", True),
        )
        rank_by_code = {item["security_code"]: item for item in evaluations}
        market_value = sum(item["market_value"] or 0 for item in positions)
        nav = account["cash"] + market_value
        config = {
            "max_positions": top_n,
            "target_gross_exposure": target_gross_exposure,
            "exposure_mode": exposure["mode"],
            "exposure_decision": exposure,
            "min_position_weight": DEFAULT_MIN_POSITION_WEIGHT,
            "max_position_weight": max_position_weight,
            "max_industry_weight": max_industry_weight,
            "max_names_per_industry": 2,
            "max_pair_correlation": max_pair_correlation,
            "hold_rank_buffer": hold_rank_buffer,
            "minimum_observations": MIN_OBSERVATIONS,
            "minimum_avg_traded_value_20d": MIN_AVG_TRADED_VALUE_20D,
            "maximum_volatility_60d": MAX_VOLATILITY_60D,
            "maximum_drawdown_250d_abs": MAX_DRAWDOWN_250D_ABS,
            "market": "CN_A",
            "market_rules_version": "china-a-v1",
            "commission_rate": rules.config.commission_rate,
            "minimum_commission": rules.config.minimum_commission,
            "stamp_duty_rate": rules.config.stamp_duty_rate,
            "transfer_fee_rate": rules.config.transfer_fee_rate,
            "slippage_rate": rules.config.slippage_rate,
            "execution": "first_available_open_after_as_of",
            "lot_size": rules.config.lot_size,
            "max_volume_participation": rules.config.max_volume_participation,
            "approval_mode": "manual",
            "auto_run": bool(strategy.get("config", {}).get("auto_run", False)),
            "run_time": strategy.get("config", {}).get("run_time"),
            "research_assessment_policy": RESEARCH_ASSESSMENT_POLICY_VERSION,
            "strategy_id": strategy["strategy_id"],
            "strategy_version": strategy["version"],
            "signal_source": strategy["signal_source"],
        }
        selected: list[dict] = []
        industry_counts: dict[str, int] = {}
        frozen_codes = {
            code for code in position_by_code
            if code not in rank_exit_codes
            and (
                not holding_shadow_covered.get(code, False)
            or (
                rank_by_code[code]["risk_status"] == "passed"
                and rank_by_code[code]["research_status"] == "defer"
            )
            )
        }
        eligible = [
            item for item in evaluations
            if item["risk_status"] == "passed"
            and item["research_status"] in {"admit", "reduce"}
            and item["security_code"] not in frozen_codes
            and item["security_code"] not in rank_exit_codes
        ]
        existing_eligible = [item for item in eligible if item["security_code"] in position_by_code]
        new_eligible = sorted(
            [item for item in eligible if item["security_code"] not in position_by_code],
            key=lambda item: item["cross_section_rank"] if item["cross_section_rank"] is not None else 10**9,
        )
        for candidate in [*existing_eligible, *new_eligible]:
            is_existing = candidate["security_code"] in position_by_code
            if not is_existing and len(selected) + len(frozen_codes) >= top_n:
                candidate["portfolio_status"] = "rejected"
                candidate["rejection_reasons"].append("max_positions_occupied")
                continue
            industry = str(candidate.get("industry_l1") or "").strip()
            if not is_existing and industry and industry_counts.get(industry, 0) >= 2:
                candidate["portfolio_status"] = "rejected"
                candidate["rejection_reasons"].append("industry_name_limit")
                continue
            correlations = [self._correlation(candidate["returns_60d"], item["returns_60d"]) for item in selected]
            finite_correlations = [value for value in correlations if value is not None]
            candidate["maximum_selected_correlation"] = max(finite_correlations) if finite_correlations else None
            if (
                not is_existing
                and finite_correlations
                and candidate["maximum_selected_correlation"] > max_pair_correlation
            ):
                candidate["portfolio_status"] = "rejected"
                candidate["rejection_reasons"].append("pair_correlation")
                continue
            candidate["portfolio_status"] = "selected"
            selected.append(candidate)
            if industry:
                industry_counts[industry] = industry_counts.get(industry, 0) + 1
        frozen_weight = sum(
            float(position_by_code[code].get("market_value") or 0) / nav
            for code in frozen_codes
        ) if nav else 0.0
        allocatable_exposure = max(0.0, target_gross_exposure - frozen_weight)
        weights = self._bounded_weights(
            selected, allocatable_exposure, DEFAULT_MIN_POSITION_WEIGHT, max_position_weight
        )
        weights = self._apply_industry_cap(selected, weights, max_industry_weight, max_position_weight)
        for item in selected:
            item["pre_research_weight"] = weights[item["security_code"]]
            item["target_weight"] = (
                weights[item["security_code"]]
                * item["research_assessment"]["weight_multiplier"]
            )
        industry_coverage = sum(bool(str(item.get("industry_l1") or "").strip()) for item in selected)
        public_evaluations = [
            {key: value for key, value in item.items() if key != "returns_60d"}
            for item in evaluations
        ]
        public_selected = [
            {key: value for key, value in item.items() if key != "returns_60d"}
            for item in selected
        ]
        market_summary = {
            "as_of": as_of,
            "universe_size": shadow["universe_size"],
            "signal_count": shadow["signal_count"],
            "top_candidates": public_selected,
            "candidate_evaluations": public_evaluations,
            "risk_passed_count": sum(item["risk_status"] == "passed" for item in evaluations),
            "selected_count": len(selected),
            "position_count": len(positions),
            "nav_before_orders": nav,
            "holding_control": {
                "evaluated": len(positions),
                "frozen": len(frozen_codes),
                "frozen_codes": sorted(frozen_codes),
                "rank_exit_count": len(rank_exit_codes),
                "rank_exit_codes": sorted(rank_exit_codes),
                "hold_rank_buffer": hold_rank_buffer,
                "occupied_slots": len(selected) + len(frozen_codes),
                "new_entries_blocked_when_full": True,
            },
            "research_control": {
                "policy_version": RESEARCH_ASSESSMENT_POLICY_VERSION,
                "counts": {
                    status: sum(item["research_status"] == status for item in evaluations)
                    for status in ("admit", "reduce", "defer", "veto")
                },
                "missing_or_stale_is_defer": True,
                "reduce_multiplier": SIGNAL_MULTIPLIERS["reduce"],
            },
            "industry_control": {
                "status": "enforced" if selected and industry_coverage == len(selected) else "correlation_proxy",
                "covered": industry_coverage,
                "selected": len(selected),
                "note": "行业字段缺失时使用60日收益相关性上限作为降级集中度约束。",
            },
            "exposure_control": exposure,
        }
        snapshot = {
            "account_id": account["account_id"],
            "as_of": as_of,
            "factor_snapshot_id": factor["snapshot_id"],
            "shadow_snapshot_id": shadow["snapshot_id"],
            "strategy_version": f'{strategy["strategy_id"]}@{strategy["version"]}',
            "strategy_id": strategy["strategy_id"],
            "config": config,
            "market_summary": market_summary,
        }
        orders = []
        selected_by_code = {item["security_code"]: item for item in selected}
        evaluation_by_code = {item["security_code"]: item for item in evaluations}
        for position in positions:
            candidate = selected_by_code.get(position["security_code"])
            if candidate is None:
                if position["security_code"] in frozen_codes:
                    continue
                evaluation = evaluation_by_code.get(position["security_code"])
                if (
                    evaluation
                    and evaluation["risk_status"] == "passed"
                    and evaluation["research_status"] == "defer"
                ):
                    continue
                qty = int(position["quantity"])
                if qty:
                    orders.append(self._order(
                        run_id, account["account_id"], position["security_code"], "sell", qty,
                        position["close"], 0,
                        {
                            "rule": "risk_or_rank_exit",
                            "shadow_rank": (rank_by_code.get(position["security_code"]) or {}).get("cross_section_rank"),
                            "research_assessment": (evaluation or {}).get("research_assessment"),
                        },
                        created_at,
                    ))
        reserved_cash = 0.0
        for candidate in selected:
            code = candidate["security_code"]
            price = float(candidate["close"])
            target_weight = candidate["target_weight"]
            target_qty = rules.round_buy_quantity((nav * target_weight) / price)
            current_qty = int((position_by_code.get(code) or {}).get("quantity") or 0)
            if current_qty and candidate["research_status"] == "reduce":
                target_qty = min(target_qty, current_qty)
            delta = target_qty - current_qty
            if delta < 0:
                available = int((position_by_code.get(code) or {}).get("available_quantity") or 0)
                qty = min(
                    rules.round_sell_quantity(-delta, liquidating=target_qty == 0),
                    available,
                )
                if qty > 0:
                    orders.append(self._order(
                        run_id, account["account_id"], code, "sell", qty, price, target_weight,
                        {
                            "rule": "risk_budget_rebalance",
                            "shadow_rank": candidate["cross_section_rank"],
                            "current_weight": (position_by_code[code]["market_value"] or 0) / nav if nav else 0,
                            "target_weight": target_weight,
                            "volatility_60d": candidate["volatility_60d"],
                            "industry_l1": candidate.get("industry_l1"),
                            "maximum_selected_correlation": candidate.get("maximum_selected_correlation"),
                            "research_assessment": candidate["research_assessment"],
                        },
                        created_at,
                    ))
                continue
            qty = delta
            estimated_price = rules.execution_price("buy", price)
            available_cash = max(0, account["cash"] - reserved_cash)
            affordable = rules.round_buy_quantity(available_cash / estimated_price)
            while affordable > 0 and (
                affordable * estimated_price
                + rules.fee("buy", affordable, estimated_price)
                > available_cash + 1e-8
            ):
                affordable -= rules.config.lot_size
            qty = min(qty, affordable)
            if qty < rules.config.lot_size:
                continue
            reserved_cash += (
                qty * estimated_price + rules.fee("buy", qty, estimated_price)
            )
            orders.append(self._order(
                run_id, account["account_id"], code, "buy", qty, price, target_weight,
                {
                    "rule": "risk_budget_entry" if current_qty == 0 else "risk_budget_rebalance",
                    "shadow_rank": candidate["cross_section_rank"],
                    "score": candidate["score"],
                    "current_weight": ((position_by_code.get(code) or {}).get("market_value") or 0) / nav if nav else 0,
                    "target_weight": target_weight,
                    "volatility_60d": candidate["volatility_60d"],
                    "avg_traded_value_20d": candidate["avg_traded_value_20d"],
                    "max_drawdown_250d": candidate["max_drawdown_250d"],
                    "industry_l1": candidate.get("industry_l1"),
                    "maximum_selected_correlation": candidate.get("maximum_selected_correlation"),
                    "research_assessment": candidate["research_assessment"],
                },
                created_at,
            ))
        return {
            "factor_snapshot_id": factor["snapshot_id"],
            "shadow_snapshot_id": shadow["snapshot_id"],
            "strategy_id": strategy["strategy_id"],
            "strategy_version": snapshot["strategy_version"],
            "config": config,
            "market_summary": market_summary,
            "snapshot_hash": canonical_hash(snapshot, compact=False),
            "orders": orders,
        }

    @staticmethod
    def _order(
        run_id: str,
        account_id: str,
        code: str,
        side: str,
        quantity: int,
        price: float,
        target_weight: float,
        reason: dict,
        created_at: str,
    ) -> dict:
        return {
            "order_id": str(uuid.uuid4()),
            "run_id": run_id,
            "account_id": account_id,
            "security_code": normalize_code(code),
            "side": side,
            "quantity": quantity,
            "reference_price": price,
            "target_weight": target_weight,
            "reason": reason,
            "created_at": created_at,
        }
