import json

import pytest

from app.factor_release import FactorReleaseService
from tests.test_factor_backtest import create_services


def test_release_requires_gates_and_human_approval_before_shadow(tmp_path):
    store, evaluation, backtest = create_services(tmp_path)
    evaluated = evaluation.run(
        start_date="2025-04-01",
        end_date="2026-02-28",
        rebalance_step=20,
        horizons=(1, 5, 20),
        layer_count=5,
    )
    tested = backtest.run(
        evaluation_id=evaluated["run"]["evaluation_id"],
        factor_id="momentum_20d",
        top_n=10,
        rebalance_step=20,
    )
    service = FactorReleaseService(store)

    with store.connect() as conn:
        metrics = tested["run"]["metrics"]
        metrics["average_turnover"] = 1.82
        metrics["total_cost_rate"] = 0.32
        conn.execute(
            "UPDATE factor_backtest_run SET metrics_json=?, result_hash=? WHERE backtest_id=?",
            (json.dumps(metrics), "high-cost-result", tested["run"]["backtest_id"]),
        )
    failed = service.create_candidate(
        factor_id="momentum_20d",
        factor_version=2,
        evaluation_id=evaluated["run"]["evaluation_id"],
        backtest_id=tested["run"]["backtest_id"],
        limitations_acknowledged=True,
        created_by="tester",
    )
    assert failed["candidate"]["status"] == "gate_failed"
    assert {gate["gate_key"] for gate in failed["gates"] if not gate["passed"]} >= {
        "average_turnover", "cost_rate"
    }
    with pytest.raises(ValueError, match="阻断门禁"):
        service.decide(
            failed["candidate"]["release_id"], "approve", "tester", "不能绕过门禁"
        )

    with store.connect() as conn:
        metrics["average_turnover"] = 0.50
        metrics["total_cost_rate"] = 0.05
        conn.execute(
            "UPDATE factor_backtest_run SET metrics_json=?, result_hash=? WHERE backtest_id=?",
            (json.dumps(metrics), "controlled-cost-result", tested["run"]["backtest_id"]),
        )
    passed = service.create_candidate(
        factor_id="momentum_20d",
        factor_version=2,
        evaluation_id=evaluated["run"]["evaluation_id"],
        backtest_id=tested["run"]["backtest_id"],
        limitations_acknowledged=True,
        created_by="tester",
    )
    assert passed["candidate"]["status"] == "gate_passed"
    assert passed["candidate"]["blocking_failure_count"] == 0

    approved = service.decide(
        passed["candidate"]["release_id"], "approve", "tester", "进入前向观察"
    )
    assert approved["candidate"]["status"] == "approved"
    with store.connect() as conn:
        status = conn.execute(
            "SELECT lifecycle_status FROM factor_version WHERE factor_id='momentum_20d' AND version=2"
        ).fetchone()[0]
        model_binding_count = conn.execute("SELECT COUNT(*) FROM model_factor_binding").fetchone()[0]
    assert status == "shadow"
    assert model_binding_count == 0


def test_release_rejects_mismatched_evidence_chain(tmp_path):
    store, evaluation, backtest = create_services(tmp_path)
    evaluated = evaluation.run(
        start_date="2025-04-01", end_date="2026-02-28", rebalance_step=20,
        horizons=(1, 5, 20), layer_count=5,
    )
    tested = backtest.run(
        evaluation_id=evaluated["run"]["evaluation_id"],
        factor_id="momentum_20d", top_n=10, rebalance_step=20,
    )
    service = FactorReleaseService(store)

    with pytest.raises(ValueError, match="同一证据链"):
        service.create_candidate(
            factor_id="momentum_60d",
            factor_version=2,
            evaluation_id=evaluated["run"]["evaluation_id"],
            backtest_id=tested["run"]["backtest_id"],
            limitations_acknowledged=True,
            created_by="tester",
        )
