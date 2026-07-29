import pytest

from app.thesis.monitoring import build_monitor_evaluations, evidence_business_key
from app.schemas import ThesisCreate
from app.thesis.store import ThesisStore


def market_evidence(value="100.00", as_of="2026-07-20"):
    return {
        "id": "ev-price-latest",
        "category": "market_fact",
        "label": "最新收盘价",
        "value": value,
        "as_of": as_of,
        "source": "本地日线数据库",
        "method": "读取最后一个有效交易日",
    }


def financial_evidence(value="120000000", period_end="2025-12-31"):
    return {
        "id": f"ev-fin-revenue-{period_end}",
        "category": "financial_fact",
        "label": "营业收入",
        "value": value,
        "as_of": "2026-03-20",
        "source": "/api/documents/doc-1/file#page=1",
        "citation": {
            "announcement_id": "ann-1",
            "page_start": 1,
            "sha256": "a" * 64,
        },
        "financial_fact": {
            "metric_code": "operating_revenue",
            "period_end": period_end,
            "comparison_period_end": "2024-12-31",
            "current_value": value,
            "comparison_value": "100000000",
            "change_pct": "20",
            "extraction_version": "financial-regex-v1",
        },
    }


def run(run_id, as_of, evidence, statement="收盘价与营业收入已形成确定性快照", version="v1"):
    return {
        "run_id": run_id,
        "security_code": "300750.SZ",
        "as_of": as_of,
        "workflow_version": version,
        "status": "completed",
        "evidence_snapshot_hash": f"hash-{run_id}",
        "artifacts": [
            {"artifact_type": "evidence_pack", "payload": {"items": evidence}},
            {
                "artifact_type": "company_snapshot",
                "payload": {
                    "claims": [
                        {
                            "id": "cl-summary",
                            "statement": statement,
                            "claim_type": "fact",
                            "confidence": 1.0,
                            "evidence_ids": [item["id"] for item in evidence],
                        }
                    ]
                },
            },
        ],
    }


def create_thesis(store):
    return store.create(
        ThesisCreate(
            code="300750.SZ",
            title="增长论点",
            core_claim="收入增长能够支持长期竞争力",
        )
    )


def test_baseline_import_and_unchanged_run_are_idempotent(tmp_path):
    store = ThesisStore(tmp_path / "state.db")
    thesis = create_thesis(store)
    evidence = [market_evidence(), financial_evidence()]
    baseline = run("run-1", "2026-07-20", evidence)

    first = store.set_monitor_baseline(
        thesis_id=thesis["id"], run=baseline, claims=baseline["artifacts"][1]["payload"]["claims"], evidence=evidence
    )
    second = store.set_monitor_baseline(
        thesis_id=thesis["id"], run=baseline, claims=baseline["artifacts"][1]["payload"]["claims"], evidence=evidence
    )
    claims = store.active_claims(thesis["id"])
    evaluations = build_monitor_evaluations(
        thesis=store.get(thesis["id"]), baseline_run=baseline, current_run=baseline, claims=claims
    )
    saved_once = store.save_claim_evaluations(
        thesis_id=thesis["id"], current_run_id="run-1", evaluations=evaluations
    )
    saved_twice = store.save_claim_evaluations(
        thesis_id=thesis["id"], current_run_id="run-1", evaluations=evaluations
    )

    assert first["claims_imported"] == second["claims_imported"] == 1
    assert len(claims) == 1
    assert evaluations[0]["suggested_verdict"] == "维持"
    assert evaluations[0]["changes"] == []
    assert saved_once[0]["evaluation_id"] == saved_twice[0]["evaluation_id"]


def test_changed_market_and_financial_evidence_require_manual_judgment(tmp_path):
    store = ThesisStore(tmp_path / "state.db")
    thesis = create_thesis(store)
    baseline_evidence = [market_evidence(), financial_evidence()]
    baseline = run("run-1", "2026-07-20", baseline_evidence)
    store.set_monitor_baseline(
        thesis_id=thesis["id"], run=baseline, claims=baseline["artifacts"][1]["payload"]["claims"], evidence=baseline_evidence
    )
    current_evidence = [
        market_evidence("105.00", "2026-07-21"),
        financial_evidence("130000000", "2026-03-31"),
    ]
    current = run("run-2", "2026-07-21", current_evidence)

    evaluations = build_monitor_evaluations(
        thesis=store.get(thesis["id"]),
        baseline_run=baseline,
        current_run=current,
        claims=store.active_claims(thesis["id"]),
    )

    assert evaluations[0]["suggested_verdict"] == "无法判断"
    assert {change["change_type"] for change in evaluations[0]["changes"]} == {"changed"}
    assert {change["business_key"] for change in evaluations[0]["changes"]} == {
        "market:ev-price-latest",
        "financial:operating_revenue",
    }
    assert evidence_business_key(current_evidence[1]) == "financial:operating_revenue"


@pytest.mark.parametrize(
    ("current_overrides", "message"),
    [
        ({"security_code": "600519.SH"}, "证券不一致"),
        ({"workflow_version": "v2"}, "工作流版本不一致"),
        ({"as_of": "2026-07-19"}, "截止日早于基线"),
    ],
)
def test_monitor_rejects_incompatible_runs(tmp_path, current_overrides, message):
    store = ThesisStore(tmp_path / "state.db")
    thesis = create_thesis(store)
    evidence = [market_evidence()]
    baseline = run("run-1", "2026-07-20", evidence)
    store.set_monitor_baseline(
        thesis_id=thesis["id"], run=baseline, claims=baseline["artifacts"][1]["payload"]["claims"], evidence=evidence
    )
    current = {**run("run-2", "2026-07-21", evidence), **current_overrides}

    with pytest.raises(ValueError, match=message):
        build_monitor_evaluations(
            thesis=store.get(thesis["id"]), baseline_run=baseline, current_run=current, claims=store.active_claims(thesis["id"])
        )


def test_confirmation_updates_claim_without_changing_thesis_status(tmp_path):
    store = ThesisStore(tmp_path / "state.db")
    thesis = create_thesis(store)
    evidence = [market_evidence()]
    baseline = run("run-1", "2026-07-20", evidence)
    store.set_monitor_baseline(
        thesis_id=thesis["id"], run=baseline, claims=baseline["artifacts"][1]["payload"]["claims"], evidence=evidence
    )
    evaluations = build_monitor_evaluations(
        thesis=store.get(thesis["id"]), baseline_run=baseline, current_run=baseline, claims=store.active_claims(thesis["id"])
    )
    saved = store.save_claim_evaluations(
        thesis_id=thesis["id"], current_run_id="run-1", evaluations=evaluations
    )[0]

    confirmed = store.confirm_evaluation(saved["evaluation_id"], "增强")

    assert confirmed["status"] == "confirmed"
    assert confirmed["confirmed_verdict"] == "增强"
    assert store.active_claims(thesis["id"])[0]["last_verdict"] == "增强"
    assert store.get(thesis["id"])["status"] == "观察"
    assert store.get(thesis["id"])["monitor"]["pending_evaluations"] == 0


def test_empty_claim_import_does_not_leave_partial_baseline(tmp_path):
    store = ThesisStore(tmp_path / "state.db")
    thesis = create_thesis(store)
    baseline = run("run-1", "2026-07-20", [])

    with pytest.raises(ValueError, match="没有可绑定证据"):
        store.set_monitor_baseline(
            thesis_id=thesis["id"], run=baseline, claims=baseline["artifacts"][1]["payload"]["claims"], evidence=[]
        )

    assert store.get(thesis["id"])["monitor"]["baseline"] is None


def test_new_baseline_preserves_history_and_supersedes_only_pending(tmp_path):
    store = ThesisStore(tmp_path / "state.db")
    thesis = create_thesis(store)
    evidence = [market_evidence()]
    first_run = run("run-1", "2026-07-20", evidence)
    store.set_monitor_baseline(
        thesis_id=thesis["id"], run=first_run, claims=first_run["artifacts"][1]["payload"]["claims"], evidence=evidence
    )
    evaluations = build_monitor_evaluations(
        thesis=store.get(thesis["id"]), baseline_run=first_run, current_run=first_run, claims=store.active_claims(thesis["id"])
    )
    original = store.save_claim_evaluations(
        thesis_id=thesis["id"], current_run_id="run-1", evaluations=evaluations
    )[0]

    # Reapplying the same baseline leaves the pending evaluation untouched.
    store.set_monitor_baseline(
        thesis_id=thesis["id"], run=first_run, claims=first_run["artifacts"][1]["payload"]["claims"], evidence=evidence
    )
    assert store.monitor_detail(thesis["id"])["evaluations"][0]["status"] == "pending"

    second_run = run("run-2", "2026-07-21", evidence)
    store.set_monitor_baseline(
        thesis_id=thesis["id"], run=second_run, claims=second_run["artifacts"][1]["payload"]["claims"], evidence=evidence
    )
    history = store.monitor_detail(thesis["id"])["evaluations"]

    assert history[0]["evaluation_id"] == original["evaluation_id"]
    assert history[0]["status"] == "superseded"
    assert store.get(thesis["id"])["monitor"]["pending_evaluations"] == 0
