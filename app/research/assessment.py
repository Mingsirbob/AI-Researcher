from __future__ import annotations

from app.core.primitives import canonical_hash


RESEARCH_ASSESSMENT_SCHEMA_VERSION = "research-assessment-v1"
RESEARCH_ASSESSMENT_POLICY_VERSION = "evidence-admission-v1"

SIGNAL_MULTIPLIERS = {
    "admit": 1.0,
    "reduce": 0.5,
    "defer": 0.0,
    "veto": 0.0,
}

CRITICAL_NEGATIVE_CATEGORIES = {
    "fraud_or_restatement",
    "default_or_insolvency",
    "delisting_or_listing_status",
}


def deterministic_assessment_fallback(reason: str) -> dict:
    return {
        "fundamental_outlook": "insufficient",
        "evidence_confidence": 0.0,
        "fundamental_evidence": [],
        "material_negatives": [],
        "catalysts": [],
        "invalidating_conditions": [],
        "limitations": [f"结构化研究评估未由模型完成：{reason}"],
    }


def build_research_assessment(
    *,
    run_id: str,
    evidence_pack: dict,
    evidence_artifact: dict,
    company_snapshot: dict,
    company_artifact: dict,
    draft: dict,
    generation_mode: str,
    generation_meta: dict,
) -> dict:
    valid_ids = {item["id"] for item in evidence_pack.get("items", [])}
    cited_groups = [
        *draft.get("fundamental_evidence", []),
        *draft.get("material_negatives", []),
        *draft.get("catalysts", []),
    ]
    cited_ids = {
        evidence_id
        for item in cited_groups
        for evidence_id in item.get("evidence_ids", [])
    }
    citations_valid = bool(cited_ids) and cited_ids.issubset(valid_ids)
    evidence_integrity = canonical_hash(evidence_pack) == evidence_artifact["snapshot_hash"]
    company_integrity = canonical_hash(company_snapshot) == company_artifact["snapshot_hash"]

    coverage = company_snapshot.get("coverage", {})
    announcements_supported = coverage.get("announcements", {}).get("status") == "supported"
    financials_supported = coverage.get("financials", {}).get("status") == "supported"
    coverage_count = int(announcements_supported) + int(financials_supported)
    confidence_cap = {0: 0.35, 1: 0.65, 2: 0.95}[coverage_count]
    model_confidence = float(draft.get("evidence_confidence") or 0)
    evidence_confidence = min(max(model_confidence, 0.0), confidence_cap)

    negatives = draft.get("material_negatives", [])
    critical_negatives = [
        item for item in negatives
        if item.get("severity") == "critical"
        and item.get("category") in CRITICAL_NEGATIVE_CATEGORIES
    ]
    elevated_negatives = [
        item for item in negatives if item.get("severity") in {"high", "critical"}
    ]
    has_fundamental_evidence = bool(draft.get("fundamental_evidence"))
    has_invalidating_conditions = bool(draft.get("invalidating_conditions"))

    gates = [
        {"name": "evidence_artifact_integrity", "passed": evidence_integrity,
         "observed": evidence_artifact["snapshot_hash"], "expected": "payload SHA-256 matches"},
        {"name": "company_artifact_integrity", "passed": company_integrity,
         "observed": company_artifact["snapshot_hash"], "expected": "payload SHA-256 matches"},
        {"name": "evidence_links", "passed": citations_valid,
         "observed": len(cited_ids), "expected": ">= 1 valid Evidence ID"},
        {"name": "announcement_coverage", "passed": announcements_supported,
         "observed": coverage.get("announcements", {}).get("status"), "expected": "supported"},
        {"name": "financial_coverage", "passed": financials_supported,
         "observed": coverage.get("financials", {}).get("status"), "expected": "supported"},
        {"name": "fundamental_evidence", "passed": has_fundamental_evidence,
         "observed": len(draft.get("fundamental_evidence", [])), "expected": ">= 1"},
        {"name": "invalidating_conditions", "passed": has_invalidating_conditions,
         "observed": len(draft.get("invalidating_conditions", [])), "expected": ">= 1"},
        {"name": "minimum_evidence_confidence", "passed": evidence_confidence >= 0.55,
         "observed": evidence_confidence, "expected": ">= 0.55"},
    ]

    reasons = []
    if critical_negatives and citations_valid and evidence_integrity and company_integrity:
        signal = "veto"
        reasons.append("存在有原文证据支持的关键类别重大负面事项")
    elif not all(item["passed"] for item in gates):
        signal = "defer"
        reasons.extend(item["name"] for item in gates if not item["passed"])
    elif (
        elevated_negatives
        or draft.get("fundamental_outlook") in {"neutral", "negative"}
        or evidence_confidence < 0.75
    ):
        signal = "reduce"
        if elevated_negatives:
            reasons.append("存在高严重度负面证据")
        if draft.get("fundamental_outlook") in {"neutral", "negative"}:
            reasons.append(f"基本面方向为 {draft.get('fundamental_outlook')}")
        if evidence_confidence < 0.75:
            reasons.append("证据置信度不足以全额准入")
    else:
        signal = "admit"
        reasons.append("公告与财务证据完整，未发现触发降级的重大负面事项")

    return {
        "schema_version": RESEARCH_ASSESSMENT_SCHEMA_VERSION,
        "policy_version": RESEARCH_ASSESSMENT_POLICY_VERSION,
        "security": company_snapshot["security"],
        "as_of": company_snapshot["as_of"],
        "research_run_id": run_id,
        "source_refs": {
            "evidence_artifact_id": evidence_artifact["artifact_id"],
            "evidence_snapshot_hash": evidence_artifact["snapshot_hash"],
            "company_artifact_id": company_artifact["artifact_id"],
            "company_snapshot_hash": company_artifact["snapshot_hash"],
        },
        "generation": {"mode": generation_mode, "meta": generation_meta},
        "fundamental_outlook": draft.get("fundamental_outlook", "insufficient"),
        "fundamental_evidence": draft.get("fundamental_evidence", []),
        "material_negatives": negatives,
        "catalysts": draft.get("catalysts", []),
        "evidence_confidence": evidence_confidence,
        "model_confidence": model_confidence,
        "confidence_cap": confidence_cap,
        "invalidating_conditions": draft.get("invalidating_conditions", []),
        "limitations": draft.get("limitations", []),
        "gates": gates,
        "signal": signal,
        "weight_multiplier": SIGNAL_MULTIPLIERS[signal],
        "reasons": reasons,
        "decision_boundary": "该信号仅约束确定性风险引擎；模型不能直接设置仓位或覆盖硬风险门禁。",
    }
