from app.research.assessment import (
    RESEARCH_ASSESSMENT_POLICY_VERSION,
    build_research_assessment,
    canonical_hash,
)


def _inputs(*, financial_status="supported", negatives=None, confidence=0.9):
    evidence_pack = {
        "security": {"code": "000001.SZ", "name": "测试公司"},
        "as_of": "2026-07-23",
        "coverage": {},
        "items": [{"id": "ev-fin", "category": "financial_fact", "value": "收入增长"}],
    }
    company = {
        "security": evidence_pack["security"],
        "as_of": "2026-07-23",
        "coverage": {
            "announcements": {"status": "supported"},
            "financials": {"status": financial_status},
        },
    }
    evidence_artifact = {
        "artifact_id": "ev-artifact",
        "snapshot_hash": canonical_hash(evidence_pack),
    }
    company_artifact = {
        "artifact_id": "company-artifact",
        "snapshot_hash": canonical_hash(company),
    }
    draft = {
        "fundamental_outlook": "positive",
        "evidence_confidence": confidence,
        "fundamental_evidence": [{"statement": "收入增长", "evidence_ids": ["ev-fin"]}],
        "material_negatives": negatives or [],
        "catalysts": [],
        "invalidating_conditions": ["后续收入转为下降"],
        "limitations": [],
    }
    return evidence_pack, company, evidence_artifact, company_artifact, draft


def _build(**kwargs):
    evidence, company, evidence_artifact, company_artifact, draft = _inputs(**kwargs)
    return build_research_assessment(
        run_id="run-1",
        evidence_pack=evidence,
        evidence_artifact=evidence_artifact,
        company_snapshot=company,
        company_artifact=company_artifact,
        draft=draft,
        generation_mode="ai",
        generation_meta={},
    )


def test_complete_supported_assessment_is_admitted():
    result = _build()
    assert result["policy_version"] == RESEARCH_ASSESSMENT_POLICY_VERSION
    assert result["signal"] == "admit"
    assert result["weight_multiplier"] == 1.0
    assert all(item["passed"] for item in result["gates"])


def test_partial_coverage_is_deferred_and_confidence_is_capped():
    result = _build(financial_status="not_covered")
    assert result["signal"] == "defer"
    assert result["evidence_confidence"] == 0.65
    assert "financial_coverage" in result["reasons"]


def test_critical_supported_negative_is_vetoed():
    result = _build(negatives=[{
        "statement": "存在退市风险事项",
        "category": "delisting_or_listing_status",
        "severity": "critical",
        "evidence_ids": ["ev-fin"],
    }])
    assert result["signal"] == "veto"
    assert result["weight_multiplier"] == 0.0


def test_high_negative_reduces_weight_without_overriding_hard_gates():
    result = _build(negatives=[{
        "statement": "现金流显著恶化",
        "category": "cash_flow",
        "severity": "high",
        "evidence_ids": ["ev-fin"],
    }])
    assert result["signal"] == "reduce"
    assert result["weight_multiplier"] == 0.5
