from datetime import date, timedelta

from app.decision.outcomes import DecisionOutcomeService
from tests.test_decision_cases import build_service, create_case


def price_rows(count: int, *, start="2026-07-20", base=100.0, step=1.0):
    first = date.fromisoformat(start)
    return [
        {"time": (first + timedelta(days=index)).isoformat(), "close": base + step * index}
        for index in range(count)
    ]


def setup_outcome(tmp_path):
    store, _, case_service, run_id, thesis_id = build_service(tmp_path)
    case = create_case(case_service, run_id, thesis_id)
    return store, case, DecisionOutcomeService(store)


def test_outcome_stays_pending_until_full_horizon(tmp_path):
    store, case, service = setup_outcome(tmp_path)
    result = service.evaluate(
        case["case_id"],
        security_rows=price_rows(11),
        benchmark_rows=price_rows(11, base=200.0, step=0.5),
        data_source="test",
    )

    assert result["status"] == "pending"
    assert result["metrics"]["observed_trading_days"] == 10
    assert result["metrics"]["remaining_trading_days"] == 50
    assert result["security_return"] is None
    assert result["target_date"] is None
    assert store.decision_case_outcome(case["case_id"])["outcome_id"] == result["outcome_id"]


def test_completed_outcome_calculates_returns_excess_and_mae(tmp_path):
    _, case, service = setup_outcome(tmp_path)
    security = price_rows(61, base=100.0, step=1.0)
    security[20]["close"] = 80.0
    benchmark = price_rows(61, base=200.0, step=1.0)

    result = service.evaluate(
        case["case_id"], security_rows=security, benchmark_rows=benchmark,
        data_source="test",
    )

    assert result["status"] == "completed"
    assert result["entry_date"] == "2026-07-20"
    assert result["exit_date"] == "2026-09-18"
    assert result["security_return"] == 0.6
    assert result["benchmark_return"] == 0.3
    assert result["excess_return"] == 0.3
    assert result["maximum_adverse_excursion"] == -0.2
    assert result["adjustment"] == "forward"
    assert result["ifind_params"] == "CPS:2"


def test_misaligned_benchmark_or_wrong_adjustment_is_rejected(tmp_path):
    _, case, service = setup_outcome(tmp_path)
    result = service.evaluate(
        case["case_id"],
        security_rows=price_rows(61),
        benchmark_rows=price_rows(60, base=200.0),
        data_source="test",
        adjustment="unadjusted",
        ifind_params="",
    )

    assert result["status"] == "data_rejected"
    failed = {gate["name"] for gate in result["gates"] if not gate["passed"]}
    assert failed == {"adjustment", "benchmark_alignment"}
    assert result["security_return"] is None


def test_evaluation_is_idempotent_and_completed_snapshot_is_not_overwritten(tmp_path):
    store, case, service = setup_outcome(tmp_path)
    kwargs = {
        "security_rows": price_rows(61),
        "benchmark_rows": price_rows(61, base=200.0),
        "data_source": "test",
    }
    first = service.evaluate(case["case_id"], **kwargs)
    repeated = service.evaluate(case["case_id"], **kwargs)
    later = service.evaluate(
        case["case_id"],
        security_rows=price_rows(62),
        benchmark_rows=price_rows(62, base=200.0),
        data_source="test",
    )

    assert repeated["reused"] is True
    assert repeated["outcome_id"] == first["outcome_id"]
    assert later["outcome_id"] != first["outcome_id"]
    assert store.decision_outcome(first["outcome_id"])["snapshot_hash"] == first["snapshot_hash"]


def test_attribution_uses_only_latest_completed_outcome_per_case(tmp_path):
    _, case, service = setup_outcome(tmp_path)
    service.evaluate(
        case["case_id"], security_rows=price_rows(10),
        benchmark_rows=price_rows(10, base=200.0), data_source="test",
    )
    service.evaluate(
        case["case_id"], security_rows=price_rows(61),
        benchmark_rows=price_rows(61, base=200.0), data_source="test",
    )

    result = service.attribution()

    assert result["counts"] == {"cases": 1, "completed": 1, "pending": 0, "data_rejected": 0}
    assert result["overall"]["count"] == 1
    assert result["overall"]["reliable"] is False
    assert result["groups"]["decision_horizon_days"][0]["key"] == "60"
