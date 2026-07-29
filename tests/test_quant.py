import sqlite3
from datetime import date, timedelta

import pytest

from app.market.repository import StockRepository
from app.research.analysis import analyze_stock
from app.integrations.llm import deterministic_report
from app.thesis.monitoring import build_monitor_evaluations, run_artifacts
from app.quant import FactorSnapshotService, compute_security_factors
from app.research.store import ResearchStore
from app.quant.store import QuantStore
from app.research.workflow import (
    COMPANY_SNAPSHOT_SCHEMA_VERSION,
    EVIDENCE_PACK_SCHEMA_VERSION,
    QUANT_CONTEXT_SCHEMA_VERSION,
    WORKFLOW_VERSION,
    build_company_snapshot,
    build_evidence_pack,
)
from app.schemas import ThesisCreate
from app.thesis.store import ThesisStore


def price_rows(count=300, *, end=date(2026, 7, 20), daily_growth=0.001):
    start = end - timedelta(days=count - 1)
    rows = []
    price = 10.0
    for index in range(count):
        price *= 1 + daily_growth
        rows.append(
            {
                "time": (start + timedelta(days=index)).isoformat(),
                "open": price * 0.995,
                "high": price * 1.01,
                "low": price * 0.99,
                "close": price,
                "vwap": price,
                "volume": 1_000_000 + index * 100,
            }
        )
    return rows


def create_factor_db(path):
    datasets = {
        "stock_000001_SZ": price_rows(),
        "stock_000002_SZ": price_rows(120),
        "stock_600000_SH": price_rows(end=date(2026, 6, 30)),
        "stock_300750_SZ": price_rows(),
    }
    datasets["stock_300750_SZ"][280]["close"] *= 2
    datasets["stock_300750_SZ"][280]["high"] = datasets["stock_300750_SZ"][280]["close"] * 1.01
    datasets["stock_300750_SZ"][280]["open"] = datasets["stock_300750_SZ"][280]["close"] * 0.995
    datasets["stock_300750_SZ"][280]["low"] = datasets["stock_300750_SZ"][280]["close"] * 0.99
    datasets["stock_300750_SZ"][280]["vwap"] = datasets["stock_300750_SZ"][280]["close"]
    with sqlite3.connect(path) as conn:
        for table, rows in datasets.items():
            conn.execute(
                f"""
                CREATE TABLE "{table}" (
                    time TEXT PRIMARY KEY, open REAL, high REAL, low REAL,
                    close REAL, vwap REAL, volume REAL
                )
                """
            )
            conn.executemany(
                f'INSERT INTO "{table}" VALUES (:time, :open, :high, :low, :close, :vwap, :volume)',
                rows,
            )


def test_factor_quality_gate_and_metrics():
    passed = compute_security_factors("000001.SZ", price_rows(), "2026-07-20")
    insufficient = compute_security_factors("000002.SZ", price_rows(120), "2026-07-20")
    stale = compute_security_factors(
        "600000.SH", price_rows(end=date(2026, 6, 30)), "2026-07-20"
    )
    jumped_rows = price_rows()
    jumped_rows[280]["close"] *= 2
    jumped_rows[280]["high"] = jumped_rows[280]["close"] * 1.01
    jumped_rows[280]["open"] = jumped_rows[280]["close"] * 0.995
    jumped_rows[280]["low"] = jumped_rows[280]["close"] * 0.99
    jumped_rows[280]["vwap"] = jumped_rows[280]["close"]
    jumped = compute_security_factors("300750.SZ", jumped_rows, "2026-07-20")

    assert passed["quality_status"] == "passed"
    assert passed["return_20d"] > 0
    assert passed["avg_traded_value_20d"] > 0
    assert insufficient["quality_status"] == "excluded"
    assert "insufficient_observations" in insufficient["quality_reasons"]
    assert "stale_latest_trade" in stale["quality_reasons"]
    assert "unadjusted_price_jump" in jumped["quality_reasons"]


def test_snapshot_generation_filtering_idempotency_and_candidate_pool(tmp_path):
    stock_db = tmp_path / "stocks.db"
    create_factor_db(stock_db)
    repository = StockRepository(stock_db)
    research_store = ResearchStore(tmp_path / "state.db", tmp_path / "documents")
    research_store.bootstrap_securities(stock_db)
    store = QuantStore(tmp_path / "quant.db", research_store)
    service = FactorSnapshotService(repository, store)

    first = service.generate("2026-07-20")
    second = service.generate("2026-07-20")
    ranked = store.list_factor_rows(first["snapshot_id"], quality_status="passed")
    excluded = store.list_factor_rows(first["snapshot_id"], quality_status="excluded")

    assert first["status"] == "completed"
    assert first["total_securities"] == 4
    assert first["passed_securities"] == 1
    assert first["excluded_securities"] == 3
    assert second["snapshot_id"] == first["snapshot_id"]
    assert second["reused"] is True
    assert [item["security_code"] for item in ranked["items"]] == ["000001.SZ"]
    assert excluded["total"] == 3

    candidate = store.add_research_candidate("000001.SZ", first["snapshot_id"], "动量观察")
    assert candidate["security_code"] == "000001.SZ"
    assert candidate["note"] == "动量观察"
    assert store.list_factor_rows(first["snapshot_id"])["items"][0]["in_candidate_pool"] is True
    with pytest.raises(ValueError, match="正常排名范围"):
        store.add_research_candidate("300750.SZ", first["snapshot_id"])
    assert store.remove_research_candidate("000001.SZ") is True
    assert store.list_research_candidates() == []


def test_factor_filters_are_applied_before_pagination(tmp_path):
    stock_db = tmp_path / "stocks.db"
    create_factor_db(stock_db)
    repository = StockRepository(stock_db)
    research_store = ResearchStore(tmp_path / "state.db", tmp_path / "documents")
    research_store.bootstrap_securities(stock_db)
    store = QuantStore(tmp_path / "quant.db", research_store)
    snapshot = FactorSnapshotService(repository, store).generate("2026-07-20")

    included = store.list_factor_rows(
        snapshot["snapshot_id"], min_return_20d=0, max_volatility_60d=1
    )
    excluded = store.list_factor_rows(
        snapshot["snapshot_id"], min_return_20d=0.5
    )

    assert included["total"] == 1
    assert excluded["total"] == 0


def test_quant_context_ranks_passed_and_excludes_bad_quality_rows(tmp_path):
    stock_db = tmp_path / "stocks.db"
    create_factor_db(stock_db)
    repository = StockRepository(stock_db)
    research_store = ResearchStore(tmp_path / "state.db", tmp_path / "documents")
    research_store.bootstrap_securities(stock_db)
    store = QuantStore(tmp_path / "quant.db", research_store)
    snapshot = FactorSnapshotService(repository, store).generate("2026-07-20")

    passed = store.quant_context_for_security(
        "000001.SZ", as_of="2026-07-20", snapshot_id=snapshot["snapshot_id"]
    )
    excluded = store.quant_context_for_security(
        "300750.SZ", as_of="2026-07-20", snapshot_id=snapshot["snapshot_id"]
    )

    assert passed["status"] == "supported"
    assert passed["source"] == "candidate_pool"
    assert passed["ranks"]["return_60d"] == {
        "rank": 1,
        "universe": 1,
        "percentile": 100.0,
    }
    assert excluded["status"] == "excluded"
    assert "unadjusted_price_jump" in excluded["quality_reasons"]
    assert excluded["ranks"] == {}


def test_quant_context_respects_research_cutoff(tmp_path):
    stock_db = tmp_path / "stocks.db"
    create_factor_db(stock_db)
    repository = StockRepository(stock_db)
    research_store = ResearchStore(tmp_path / "state.db", tmp_path / "documents")
    research_store.bootstrap_securities(stock_db)
    store = QuantStore(tmp_path / "quant.db", research_store)
    snapshot = FactorSnapshotService(repository, store).generate("2026-07-20")

    uncovered = store.quant_context_for_security("000001.SZ", as_of="2026-07-19")
    assert uncovered["status"] == "not_covered"
    assert uncovered["snapshot_id"] is None
    with pytest.raises(ValueError, match="晚于研究截止日"):
        store.quant_context_for_security(
            "000001.SZ", as_of="2026-07-19", snapshot_id=snapshot["snapshot_id"]
        )


def test_m4_candidate_research_snapshot_and_thesis_monitor_chain(tmp_path):
    stock_db = tmp_path / "stocks.db"
    create_factor_db(stock_db)
    repository = StockRepository(stock_db)
    state_db = tmp_path / "state.db"
    store = ResearchStore(state_db, tmp_path / "documents")
    store.bootstrap_securities(stock_db)
    quant_store = QuantStore(tmp_path / "quant.db", store)
    factor_snapshot = FactorSnapshotService(repository, quant_store).generate("2026-07-20")
    candidate = quant_store.add_research_candidate(
        "000001.SZ", factor_snapshot["snapshot_id"], "M4 闭环回归"
    )
    quant_context = quant_store.quant_context_for_security(
        candidate["security_code"],
        as_of="2026-07-20",
        snapshot_id=candidate["source_snapshot_id"],
    )
    analysis = analyze_stock(
        candidate["security_code"],
        candidate["security_name"],
        repository.get_history(candidate["security_code"], end="2026-07-20"),
    )
    empty_financial = {
        "status": "insufficient_evidence",
        "period_summary": "未形成可比较期间",
        "overall_assessment": "没有财务事实",
        "sections": [],
        "limitations": ["财务证据不足"],
        "next_checks": [],
    }
    evidence_pack = build_evidence_pack(
        analysis=analysis,
        financial_evidence=[],
        document_evidence=[],
        announcements=[],
        extraction={"documents_seen": 0, "reports_identified": 0},
    )
    company_snapshot = build_company_snapshot(
        analysis=analysis,
        evidence_pack=evidence_pack,
        announcements=[],
        financial_template=empty_financial,
        narrative=deterministic_report(analysis),
        generation_mode="deterministic_fallback",
        generation_meta={},
        quant_context=quant_context,
    )

    def persist_run():
        run_id = store.start_research_run(
            code=candidate["security_code"],
            as_of="2026-07-20",
            workflow_version=WORKFLOW_VERSION,
        )
        evidence_artifact = store.save_research_artifact(
            run_id=run_id,
            artifact_type="evidence_pack",
            schema_version=EVIDENCE_PACK_SCHEMA_VERSION,
            status="partial",
            payload=evidence_pack,
        )
        store.save_research_artifact(
            run_id=run_id,
            artifact_type="quant_context",
            schema_version=QUANT_CONTEXT_SCHEMA_VERSION,
            status=quant_context["status"],
            payload=quant_context,
        )
        store.save_research_artifact(
            run_id=run_id,
            artifact_type="company_snapshot",
            schema_version=COMPANY_SNAPSHOT_SCHEMA_VERSION,
            status=company_snapshot["status"],
            payload=company_snapshot,
        )
        store.finish_research_run(
            run_id,
            status="completed_with_gaps",
            evidence_snapshot_hash=evidence_artifact["snapshot_hash"],
            evidence_count=len(evidence_pack["items"]),
        )
        return store.research_run(run_id)

    baseline_run = persist_run()
    replay_types = {item["artifact_type"] for item in baseline_run["artifacts"]}
    assert replay_types == {"evidence_pack", "quant_context", "company_snapshot"}
    assert baseline_run["artifacts"][1]["payload"]["source"] == "candidate_pool"
    replay_pack, replay_snapshot = run_artifacts(baseline_run)

    thesis_store = ThesisStore(state_db)
    thesis = thesis_store.create(
        ThesisCreate(
            code="000001.SZ",
            title="M4 候选研究闭环",
            core_claim="当前确定性行情事实值得持续跟踪",
        )
    )
    thesis_store.set_monitor_baseline(
        thesis_id=thesis["id"],
        run=baseline_run,
        claims=replay_snapshot["claims"],
        evidence=replay_pack["items"],
    )
    current_run = persist_run()
    evaluations = build_monitor_evaluations(
        thesis=thesis_store.get(thesis["id"]),
        baseline_run=baseline_run,
        current_run=current_run,
        claims=thesis_store.active_claims(thesis["id"]),
    )
    saved = thesis_store.save_claim_evaluations(
        thesis_id=thesis["id"],
        current_run_id=current_run["run_id"],
        evaluations=evaluations,
    )

    assert saved
    assert all(item["suggested_verdict"] == "维持" for item in saved)
    assert thesis_store.get(thesis["id"])["monitor"]["pending_evaluations"] == len(saved)
