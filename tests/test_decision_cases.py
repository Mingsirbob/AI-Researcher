from datetime import datetime, timezone

import pytest

from app.decision_cases import DecisionCaseService
from app.research_store import ResearchStore
from app.quant_store import QuantStore
from app.schemas import ThesisCreate
from app.state import ThesisStore


AS_OF = "2026-07-20"
CODE = "300750.SZ"


def build_service(tmp_path, *, liquidity=1_500_000_000.0):
    store = ResearchStore(
        tmp_path / "state.db", tmp_path / "documents", retain_split_domains=True
    )
    theses = ThesisStore(tmp_path / "state.db")
    store.upsert_security_name(CODE, "宁德时代", "test")

    run_id = store.start_research_run(
        code=CODE, as_of=AS_OF, workflow_version="company-research-v2"
    )
    evidence = {
        "security": {"code": CODE, "name": "宁德时代"},
        "as_of": AS_OF,
        "coverage": {
            "market": {"status": "supported"},
            "financials": {"status": "supported"},
            "announcements": {"status": "supported"},
        },
        "items": [{"id": "ev-price", "kind": "market", "value": 100.0}],
    }
    quant = {
        "status": "supported",
        "quality_status": "passed",
        "factors": {
            "return_20d": 0.08,
            "return_60d": 0.18,
            "volatility_60d": 0.18,
            "avg_traded_value_20d": liquidity,
            "max_drawdown_250d": -0.12,
        },
        "ranks": {
            "return_20d": {"percentile": 82.0, "rank": 18, "universe": 100},
            "return_60d": {"percentile": 88.0, "rank": 12, "universe": 100},
        },
    }
    company = {
        "status": "complete",
        "claims": [
            {
                "id": "cl-price",
                "statement": "价格与基本面证据已覆盖。",
                "claim_type": "fact",
                "confidence": 0.95,
                "evidence_ids": ["ev-price"],
            }
        ],
        "counter_view": ["需求仍可能低于预期。"],
        "limitations": ["只使用截止日内证据。"],
    }
    evidence_artifact = store.save_research_artifact(
        run_id=run_id,
        artifact_type="evidence_pack",
        schema_version="evidence-pack-v1",
        status="complete",
        payload=evidence,
    )
    store.save_research_artifact(
        run_id=run_id,
        artifact_type="quant_context",
        schema_version="quant-context-v1",
        status="supported",
        payload=quant,
    )
    store.save_research_artifact(
        run_id=run_id,
        artifact_type="company_snapshot",
        schema_version="company-snapshot-v1",
        status="complete",
        payload=company,
    )
    store.finish_research_run(
        run_id,
        status="completed",
        evidence_snapshot_hash=evidence_artifact["snapshot_hash"],
        evidence_count=1,
    )
    run = store.research_run(run_id)

    thesis = theses.create(
        ThesisCreate(
            code=CODE,
            title="增长证据",
            core_claim="盈利增长可以持续。",
            horizon="6-12个月",
            invalidating_conditions=["需求连续两个季度下降"],
        )
    )
    theses.set_monitor_baseline(
        thesis_id=thesis["id"],
        run=run,
        claims=company["claims"],
        evidence=evidence["items"],
    )

    now = datetime.now(timezone.utc).isoformat()
    model = {
        "model_run_id": "model-run-1",
        "experiment_id": "experiment-1",
        "framework": "Qlib 0.9.7 / MLflow",
        "model_class": "LGBModel",
        "feature_set": "Alpha158",
        "universe": "csi300",
        "train_start": "2008-01-01",
        "train_end": "2014-12-31",
        "valid_start": "2015-01-01",
        "valid_end": "2016-12-31",
        "test_start": "2017-01-03",
        "test_end": "2020-07-31",
        "status": "accepted_historical_only",
        "intended_use": "shadow_evaluation_only",
        "source_path": "test/model",
        "source_fingerprint": "model-fingerprint",
        "config": {},
        "metrics": {"Rank IC": 0.05},
        "limitations": ["shadow only"],
        "imported_at": now,
    }
    store.register_model_run(
        model_run=model,
        artifacts=[
            {
                "artifact_type": "model",
                "file_path": "model.pkl",
                "sha256": "a" * 64,
                "byte_size": 10,
            }
        ],
        snapshot={
            "snapshot_id": "prediction-1",
            "start_date": "2017-01-03",
            "end_date": "2020-07-31",
            "row_count": 1,
            "instrument_count": 1,
            "status": "historical_only",
            "source_fingerprint": "prediction-fingerprint",
            "imported_at": now,
        },
        signals=[],
    )
    store.register_model_validation(
        {
            "validation_id": "validation-1",
            "model_run_id": "model-run-1",
            "validation_version": "frozen-model-rolling-oos-v1",
            "status": "passed",
            "metrics": {"window_count": 14, "mean_rank_ic": 0.05},
            "gates": [{"name": "mean_rank_ic", "passed": True}],
            "windows": [],
            "source_fingerprint": "validation-fingerprint",
            "created_at": now,
        }
    )
    store.register_current_shadow(
        {
            "snapshot_id": "current-shadow-1",
            "model_run_id": "model-run-1",
            "validation_id": "validation-1",
            "as_of": AS_OF,
            "universe": "csi300_current",
            "adjustment": "forward",
            "ifind_params": "CPS:2",
            "data_source": "test",
            "data_start": "2025-11-24",
            "data_end": AS_OF,
            "data_fingerprint": "shadow-fingerprint",
            "provider_path": "test/provider",
            "model_sha256": "a" * 64,
            "feature_count": 158,
            "universe_size": 300,
            "signal_count": 1,
            "coverage": 1.0,
            "status": "current_shadow_ready",
            "gates": [{"name": "prediction_coverage", "passed": True}],
            "provider": {"calendar_days": 158},
            "created_at": now,
        },
        [
            {
                "security_code": CODE,
                "source_instrument": "SZ300750",
                "score": 0.15,
                "cross_section_rank": 30,
                "cross_section_size": 300,
                "percentile": 90.0,
            }
        ],
    )
    return store, theses, DecisionCaseService(store, theses), run_id, thesis["id"]


def create_case(service, run_id, thesis_id, *, as_of=AS_OF):
    return service.create(
        code=CODE,
        as_of=as_of,
        decision_horizon="60d",
        benchmark_code="000300.SH",
        research_run_id=run_id,
        thesis_id=thesis_id,
        shadow_snapshot_id="current-shadow-1",
    )


def test_decision_case_is_immutable_idempotent_and_keeps_three_scores(tmp_path):
    store, _, service, run_id, thesis_id = build_service(tmp_path)

    first = create_case(service, run_id, thesis_id)
    repeated = create_case(service, run_id, thesis_id)

    assert first["rule_status"] == "eligible_for_review"
    assert first["reused"] is False
    assert repeated["reused"] is True
    assert repeated["case_id"] == first["case_id"]
    assert set(first["scores"]) == {
        "attractiveness",
        "evidence_confidence",
        "risk_severity",
    }
    assert first["scores"]["attractiveness"]["score"] >= 60
    assert first["scores"]["evidence_confidence"]["score"] == 100
    assert first["snapshot"]["source_refs"]["research_run_id"] == run_id
    assert store.list_decision_cases()[0]["snapshot_hash"] == first["snapshot_hash"]


def test_decision_case_reads_shadow_from_separate_quant_database(tmp_path):
    store, theses, _, run_id, thesis_id = build_service(tmp_path)
    quant_store = QuantStore(tmp_path / "quant_research.db", store)
    service = DecisionCaseService(store, theses, quant_store)

    case = create_case(service, run_id, thesis_id)

    assert case["rule_status"] == "eligible_for_review"
    assert case["shadow_snapshot_id"] == "current-shadow-1"
    with store.connect() as conn:
        assert "current_shadow_snapshot" not in {
            row[2] for row in conn.execute("PRAGMA foreign_key_list(decision_case)")
        }


def test_human_approval_is_append_only_and_only_allows_tracking(tmp_path):
    store, _, service, run_id, thesis_id = build_service(tmp_path)
    case = create_case(service, run_id, thesis_id)

    reviewed = service.review(
        case["case_id"],
        decision="approve_for_tracking",
        reviewer="researcher",
        note="证据与风险边界已人工复核，仅进入 Shadow 跟踪。",
    )

    assert reviewed["review_status"] == "approve_for_tracking"
    assert reviewed["reviews"][0]["sequence"] == 1
    assert store.list_decision_cases(review_status="approve_for_tracking")[0]["case_id"] == case["case_id"]
    with pytest.raises(ValueError, match="已经完成审批"):
        service.review(
            case["case_id"],
            decision="reject",
            reviewer="researcher",
            note="不得覆盖原审批。",
        )


def test_stale_sources_are_insufficient_and_cannot_be_approved(tmp_path):
    _, _, service, run_id, thesis_id = build_service(tmp_path)
    case = create_case(service, run_id, thesis_id, as_of="2026-07-21")

    assert case["rule_status"] == "insufficient_evidence"
    assert any(item["name"] == "research_as_of" and not item["passed"] for item in case["gates"])
    with pytest.raises(ValueError, match="eligible_for_review"):
        service.review(
            case["case_id"],
            decision="approve_for_tracking",
            reviewer="researcher",
            note="错误批准应被拒绝。",
        )


def test_hard_liquidity_boundary_excludes_high_scoring_signal(tmp_path):
    _, _, service, run_id, thesis_id = build_service(tmp_path, liquidity=10_000_000.0)

    case = create_case(service, run_id, thesis_id)

    assert case["scores"]["attractiveness"]["score"] >= 60
    assert case["rule_status"] == "excluded"
    assert any(item["name"] == "liquidity_floor" and not item["passed"] for item in case["gates"])
