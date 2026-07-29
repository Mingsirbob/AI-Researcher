from __future__ import annotations

from datetime import date

from app.market.repository import normalize_code
from app.core.primitives import canonical_hash, sha256_text
from app.research.store import ResearchStore, utc_now
from app.quant.store import QuantStore
from app.thesis.store import ThesisStore


POLICY_VERSION = "decision-policy-v1.1"
HORIZONS = {
    "20d": (20, "约1个月"),
    "60d": (60, "约1个季度"),
    "120d": (120, "约半年"),
    "250d": (250, "约1年"),
}
BENCHMARKS = {
    "000300.SH": "沪深300",
    "000905.SH": "中证500",
    "000852.SH": "中证1000",
}
RULE_STATUSES = {
    "excluded",
    "insufficient_evidence",
    "watch",
    "research_required",
    "eligible_for_review",
}
REVIEW_DECISIONS = {
    "approve_for_tracking",
    "return_for_research",
    "reject",
}

RISK_BOUNDARIES = {
    "minimum_avg_traded_value_20d": 100_000_000.0,
    "maximum_volatility_60d": 0.80,
    "maximum_drawdown_250d_abs": 0.50,
    "maximum_risk_severity_for_review": 60.0,
    "minimum_evidence_confidence_for_review": 70.0,
    "minimum_attractiveness_for_review": 60.0,
}


def _artifact_hash(payload: dict) -> str:
    return canonical_hash(payload)


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


class DecisionCaseService:
    def __init__(
        self,
        research_store: ResearchStore,
        thesis_store: ThesisStore,
        quant_store: QuantStore | None = None,
    ):
        self.research_store = research_store
        self.quant_store = quant_store or research_store
        self.thesis_store = thesis_store

    @staticmethod
    def _gate(
        gates: list[dict],
        name: str,
        passed: bool,
        observed: object,
        expected: str,
        on_fail: str,
        detail: str,
    ) -> None:
        gates.append(
            {
                "name": name,
                "passed": bool(passed),
                "observed": observed,
                "expected": expected,
                "on_fail": on_fail,
                "detail": detail,
            }
        )

    def _select_run(self, code: str, as_of: str, run_id: str | None) -> dict | None:
        if run_id:
            run = self.research_store.research_run(run_id)
            if run is None:
                raise ValueError("指定的研究运行不存在")
            if run["security_code"] != code:
                raise ValueError("研究运行与决策证券不一致")
            if run["as_of"] > as_of:
                raise ValueError("研究运行晚于决策截止日，拒绝未来数据")
            return run
        for candidate in self.research_store.list_research_runs(code, 100):
            if candidate["status"] in {"completed", "completed_with_gaps"} and candidate["as_of"] <= as_of:
                return self.research_store.research_run(candidate["run_id"])
        return None

    def _select_thesis(self, code: str, thesis_id: str | None) -> dict | None:
        if thesis_id:
            thesis = self.thesis_store.monitor_detail(thesis_id)
            if thesis is None:
                raise ValueError("指定的 Thesis 不存在")
            if thesis["thesis"]["security_code"] != code:
                raise ValueError("Thesis 与决策证券不一致")
            return thesis
        items = self.thesis_store.list(code)
        return self.thesis_store.monitor_detail(items[0]["id"]) if items else None

    def _select_shadow(self, code: str, as_of: str, snapshot_id: str | None) -> tuple[dict | None, dict | None]:
        snapshot = (
            self.quant_store.current_shadow_snapshot(snapshot_id)
            if snapshot_id
            else self.quant_store.latest_current_shadow()
        )
        if snapshot is None:
            return None, None
        if snapshot["as_of"] > as_of:
            if snapshot_id:
                raise ValueError("当前 Shadow 快照晚于决策截止日，拒绝未来数据")
            return None, None
        signal = self.quant_store.current_shadow_signal(snapshot["snapshot_id"], code)
        return snapshot, signal

    @staticmethod
    def _score_attractiveness(signal: dict | None, quant: dict | None) -> dict:
        components: list[dict] = []
        if signal is not None:
            components.append(
                {
                    "name": "shadow_percentile",
                    "value": round(float(signal["percentile"]), 4),
                    "weight": 0.60,
                    "source": "current_shadow_signal",
                }
            )
        ranks = (quant or {}).get("ranks", {})
        for name, weight in (("return_60d", 0.25), ("return_20d", 0.15)):
            rank = ranks.get(name)
            if rank and rank.get("percentile") is not None:
                components.append(
                    {
                        "name": name,
                        "value": float(rank["percentile"]),
                        "weight": weight,
                        "source": "quant_context",
                    }
                )
        weight_sum = sum(item["weight"] for item in components)
        score = sum(item["value"] * item["weight"] for item in components) / weight_sum if weight_sum else 0.0
        return {
            "score": round(_clamp(score), 2),
            "coverage": round(weight_sum, 2),
            "components": components,
            "limitation": "机器学习信号仅占吸引力的一个显式分项，不覆盖证据或风险门禁。",
        }

    @staticmethod
    def _score_evidence(
        evidence: dict | None,
        company: dict | None,
        thesis: dict | None,
    ) -> dict:
        components: list[dict] = []
        coverage = (evidence or {}).get("coverage", {})
        for name in ("market", "financials", "announcements"):
            passed = coverage.get(name, {}).get("status") == "supported"
            components.append({"name": f"{name}_coverage", "points": 20 if passed else 0, "maximum": 20})
        complete = (company or {}).get("status") == "complete"
        components.append({"name": "company_snapshot_complete", "points": 15 if complete else 0, "maximum": 15})
        evidence_ids = {item.get("id") for item in (evidence or {}).get("items", [])}
        claims = (company or {}).get("claims", [])
        claim_links_valid = bool(claims) and all(
            claim.get("evidence_ids")
            and set(claim.get("evidence_ids", [])).issubset(evidence_ids)
            for claim in claims
        )
        components.append({"name": "claim_evidence_links", "points": 15 if claim_links_valid else 0, "maximum": 15})
        baseline = ((thesis or {}).get("thesis") or {}).get("monitor", {}).get("baseline")
        components.append({"name": "thesis_baseline", "points": 10 if baseline else 0, "maximum": 10})
        return {
            "score": float(sum(item["points"] for item in components)),
            "coverage": 1.0,
            "components": components,
        }

    @staticmethod
    def _score_risk(quant: dict | None, company: dict | None) -> dict:
        factors = (quant or {}).get("factors", {})
        volatility = abs(float(factors.get("volatility_60d") or 0.0))
        drawdown = abs(min(float(factors.get("max_drawdown_250d") or 0.0), 0.0))
        return_20d = float(factors.get("return_20d") or 0.0)
        counter_count = len((company or {}).get("counter_view", []))
        components = [
            {"name": "volatility_60d", "points": round(_clamp(volatility / 0.80 * 35.0, 0, 35), 2), "maximum": 35},
            {"name": "drawdown_250d", "points": round(_clamp(drawdown / 0.60 * 35.0, 0, 35), 2), "maximum": 35},
            {"name": "negative_momentum_20d", "points": round(_clamp(max(0.0, -return_20d) / 0.20 * 15.0, 0, 15), 2), "maximum": 15},
            {"name": "counter_view", "points": min(counter_count * 5, 15), "maximum": 15},
        ]
        return {
            "score": round(sum(item["points"] for item in components), 2),
            "coverage": 1.0 if quant and company else 0.5 if quant or company else 0.0,
            "components": components,
            "direction": "higher_is_riskier",
        }

    @staticmethod
    def _rule_status(gates: list[dict], scores: dict) -> str:
        failed = [item for item in gates if not item["passed"]]
        for status in ("excluded", "insufficient_evidence", "research_required"):
            if any(item["on_fail"] == status for item in failed):
                return status
        if (
            scores["risk_severity"]["score"] >= RISK_BOUNDARIES["maximum_risk_severity_for_review"]
            or scores["attractiveness"]["score"] < RISK_BOUNDARIES["minimum_attractiveness_for_review"]
        ):
            return "watch"
        return "eligible_for_review"

    def create(
        self,
        *,
        code: str,
        as_of: str,
        decision_horizon: str,
        benchmark_code: str,
        research_run_id: str | None = None,
        thesis_id: str | None = None,
        shadow_snapshot_id: str | None = None,
    ) -> dict:
        normalized = normalize_code(code)
        try:
            date.fromisoformat(as_of)
        except ValueError as exc:
            raise ValueError("决策截止日必须是 YYYY-MM-DD") from exc
        if decision_horizon not in HORIZONS:
            raise ValueError("不支持的决策周期")
        benchmark = benchmark_code.strip().upper()
        if benchmark not in BENCHMARKS:
            raise ValueError("基准仅支持沪深300、中证500或中证1000")
        security = self.research_store.security(normalized)
        if security is None:
            raise ValueError("证券主数据不存在")

        run = self._select_run(normalized, as_of, research_run_id)
        artifacts = {item["artifact_type"]: item for item in (run or {}).get("artifacts", [])}
        evidence_artifact = artifacts.get("evidence_pack")
        quant_artifact = artifacts.get("quant_context")
        company_artifact = artifacts.get("company_snapshot")
        evidence = evidence_artifact["payload"] if evidence_artifact else None
        quant = quant_artifact["payload"] if quant_artifact else None
        company = company_artifact["payload"] if company_artifact else None
        thesis = self._select_thesis(normalized, thesis_id)
        shadow, signal = self._select_shadow(normalized, as_of, shadow_snapshot_id)
        validation = (
            self.quant_store.model_validation(shadow["validation_id"])
            if shadow else None
        )

        gates: list[dict] = []
        self._gate(gates, "research_run", run is not None, (run or {}).get("run_id"), "存在成功研究运行", "insufficient_evidence", "DecisionCase 必须绑定可回放研究运行。")
        self._gate(gates, "research_as_of", bool(run and run["as_of"] == as_of), (run or {}).get("as_of"), f"= {as_of}", "insufficient_evidence", "研究运行必须与决策截止日完全一致。")
        self._gate(gates, "research_status", bool(run and run["status"] in {"completed", "completed_with_gaps"}), (run or {}).get("status"), "completed 或 completed_with_gaps", "insufficient_evidence", "失败或运行中的研究不可进入决策层。")
        for name, artifact in (("evidence_pack", evidence_artifact), ("quant_context", quant_artifact), ("company_snapshot", company_artifact)):
            hash_valid = bool(artifact and _artifact_hash(artifact["payload"]) == artifact["snapshot_hash"])
            self._gate(gates, f"{name}_integrity", hash_valid, artifact["snapshot_hash"] if artifact else None, "SHA-256 与 payload 一致", "insufficient_evidence", f"{name} 必须存在且未被修改。")
        coverage = (evidence or {}).get("coverage", {})
        for name in ("market", "financials", "announcements"):
            status = coverage.get(name, {}).get("status")
            self._gate(gates, f"evidence_{name}", status == "supported", status, "supported", "insufficient_evidence", f"关键 {name} 证据必须覆盖。")

        quant_status = (quant or {}).get("quality_status")
        self._gate(gates, "quant_quality", quant_status == "passed", quant_status, "passed", "excluded" if quant_status else "insufficient_evidence", "行情质量异常证券不得因模型高分进入后续审查。")
        self._gate(gates, "shadow_ready", bool(shadow and shadow["status"] == "current_shadow_ready"), (shadow or {}).get("status"), "current_shadow_ready", "insufficient_evidence", "只接纳通过当前数据合同的 Shadow 快照。")
        self._gate(gates, "shadow_as_of", bool(shadow and shadow["as_of"] == as_of), (shadow or {}).get("as_of"), f"= {as_of}", "insufficient_evidence", "当前信号必须与决策截止日一致。")
        self._gate(gates, "shadow_signal", signal is not None, (signal or {}).get("security_code"), normalized, "insufficient_evidence", "证券必须存在于当前 Shadow 截面。")
        self._gate(gates, "rolling_oos", bool(validation and validation["status"] == "passed"), (validation or {}).get("status"), "passed", "insufficient_evidence", "模型滚动样本外复核必须通过。")

        thesis_item = (thesis or {}).get("thesis")
        thesis_status = (thesis_item or {}).get("status")
        baseline = (thesis_item or {}).get("monitor", {}).get("baseline")
        self._gate(gates, "thesis_present", thesis_item is not None, (thesis_item or {}).get("id"), "存在 Thesis", "research_required", "没有可证伪 Thesis 时只能返回研究。")
        self._gate(gates, "thesis_active", thesis_status not in {"已经证伪", "研究终止"} if thesis_status else True, thesis_status, "非已经证伪/研究终止", "excluded", "已证伪或终止 Thesis 不得继续准入。")
        self._gate(gates, "thesis_baseline_current", bool(baseline and run and baseline["run_id"] == run["run_id"]), (baseline or {}).get("run_id"), (run or {}).get("run_id") or "当前研究运行", "research_required", "Thesis 基线必须对应当前研究运行。")
        pending = int((thesis_item or {}).get("monitor", {}).get("pending_evaluations") or 0)
        self._gate(gates, "thesis_no_pending_review", pending == 0, pending, "= 0", "research_required", "未确认的 Claim 重评必须先由人工处理。")

        factors = (quant or {}).get("factors", {})
        liquidity = factors.get("avg_traded_value_20d")
        volatility = factors.get("volatility_60d")
        drawdown = factors.get("max_drawdown_250d")
        self._gate(gates, "liquidity_floor", liquidity is not None and float(liquidity) >= RISK_BOUNDARIES["minimum_avg_traded_value_20d"], liquidity, f">= {RISK_BOUNDARIES['minimum_avg_traded_value_20d']:.0f}", "excluded" if liquidity is not None else "insufficient_evidence", "低流动性证券不进入审查。")
        self._gate(gates, "volatility_ceiling", volatility is not None and float(volatility) <= RISK_BOUNDARIES["maximum_volatility_60d"], volatility, f"<= {RISK_BOUNDARIES['maximum_volatility_60d']}", "excluded" if volatility is not None else "insufficient_evidence", "极端波动触发硬排除。")
        self._gate(gates, "drawdown_ceiling", drawdown is not None and abs(min(float(drawdown), 0.0)) <= RISK_BOUNDARIES["maximum_drawdown_250d_abs"], drawdown, f">= -{RISK_BOUNDARIES['maximum_drawdown_250d_abs']}", "excluded" if drawdown is not None else "insufficient_evidence", "极端回撤触发硬排除。")

        scores = {
            "attractiveness": self._score_attractiveness(signal, quant),
            "evidence_confidence": self._score_evidence(evidence, company, thesis),
            "risk_severity": self._score_risk(quant, company),
        }
        self._gate(gates, "evidence_confidence", scores["evidence_confidence"]["score"] >= RISK_BOUNDARIES["minimum_evidence_confidence_for_review"], scores["evidence_confidence"]["score"], f">= {RISK_BOUNDARIES['minimum_evidence_confidence_for_review']}", "research_required", "证据可信度不足时不得进入人工准入审查。")
        rule_status = self._rule_status(gates, scores)
        horizon_days, horizon_label = HORIZONS[decision_horizon]

        source_refs = {
            "research_run_id": (run or {}).get("run_id"),
            "research_workflow_version": (run or {}).get("workflow_version"),
            "artifacts": {
                name: ({"artifact_id": artifact["artifact_id"], "snapshot_hash": artifact["snapshot_hash"], "schema_version": artifact["schema_version"]} if artifact else None)
                for name, artifact in (("evidence_pack", evidence_artifact), ("quant_context", quant_artifact), ("company_snapshot", company_artifact))
            },
            "thesis_id": (thesis_item or {}).get("id"),
            "shadow_snapshot_id": (shadow or {}).get("snapshot_id"),
            "validation_id": (shadow or {}).get("validation_id"),
        }
        snapshot = {
            "schema_version": "decision-case-v1",
            "policy_version": POLICY_VERSION,
            "security": {"code": normalized, "name": security.get("security_name") or normalized},
            "as_of": as_of,
            "decision_horizon": {"code": decision_horizon, "trading_days": horizon_days, "label": horizon_label},
            "benchmark": {"code": benchmark, "name": BENCHMARKS[benchmark]},
            "source_refs": source_refs,
            "evidence_coverage": coverage,
            "quant_context": quant,
            "company_assessment": ({"status": company.get("status"), "claims": company.get("claims", []), "counter_view": company.get("counter_view", []), "limitations": company.get("limitations", [])} if company else None),
            "thesis": thesis_item,
            "shadow_signal": signal,
            "scores": scores,
            "gates": gates,
            "risk_boundaries": RISK_BOUNDARIES,
            "rule_status": rule_status,
            "decision_boundary": "人工批准仅允许进入 M8 Shadow 跟踪，不构成交易许可。",
        }
        snapshot_hash = canonical_hash(snapshot)
        case_id = sha256_text(f"decision-case:{snapshot_hash}")
        return self.research_store.save_decision_case(
            {
                "case_id": case_id,
                "security_code": normalized,
                "as_of": as_of,
                "decision_horizon_days": horizon_days,
                "decision_horizon_label": horizon_label,
                "benchmark_code": benchmark,
                "benchmark_name": BENCHMARKS[benchmark],
                "policy_version": POLICY_VERSION,
                "rule_status": rule_status,
                "research_run_id": (run or {}).get("run_id"),
                "thesis_id": (thesis_item or {}).get("id"),
                "shadow_snapshot_id": (shadow or {}).get("snapshot_id"),
                "scores": scores,
                "gates": gates,
                "risk_boundaries": RISK_BOUNDARIES,
                "snapshot": snapshot,
                "snapshot_hash": snapshot_hash,
                "created_at": utc_now(),
            }
        )

    def review(self, case_id: str, *, decision: str, reviewer: str, note: str) -> dict:
        if decision not in REVIEW_DECISIONS:
            raise ValueError("不支持的人工审批决定")
        item = self.research_store.decision_case(case_id)
        if item is None:
            raise KeyError(case_id)
        if item["policy_version"] != POLICY_VERSION:
            raise ValueError("该案例策略版本已被替代，必须用当前策略重新建立案例")
        return self.research_store.append_decision_review(
            case_id=case_id,
            decision=decision,
            reviewer=reviewer.strip() or "human",
            note=note.strip(),
        )
