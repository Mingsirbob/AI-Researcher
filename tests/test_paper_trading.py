import hashlib
import json
import sqlite3
from pathlib import Path

from app.data_access import StockRepository
from app.paper_trading import PAPER_BENCHMARKS, PaperExecutionService, PaperTradingService
from app.research_assessment import (
    RESEARCH_ASSESSMENT_POLICY_VERSION,
    RESEARCH_ASSESSMENT_SCHEMA_VERSION,
)
from app.research_store import ResearchStore


def _prices(path: Path) -> StockRepository:
    with sqlite3.connect(path) as conn:
        for code, base in (("000001_SZ", 10.0), ("000002_SZ", 20.0)):
            conn.execute(
                f'CREATE TABLE "stock_{code}" (time TEXT PRIMARY KEY, open REAL, high REAL, low REAL, close REAL, vwap REAL, volume REAL)'
            )
            for day, price in (("2026-07-20", base), ("2026-07-21", base + 1), ("2026-07-22", base + 2)):
                conn.execute(
                    f'INSERT INTO "stock_{code}" VALUES (?, ?, ?, ?, ?, ?, ?)',
                    (day, price, price + 0.2, price - 0.2, price, price, 100000),
                )
    return StockRepository(path)


def _service(tmp_path: Path, quote_provider=None) -> tuple[PaperTradingService, ResearchStore]:
    repo = _prices(tmp_path / "prices.db")
    store = ResearchStore(tmp_path / "state.db", tmp_path / "documents")
    store.bootstrap_securities(repo.db_path)
    now = "2026-07-20T08:00:00+00:00"
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO factor_snapshot VALUES
            ('factor-1','2026-07-20','test','fp',1,1,'completed',2,2,0,?,?,NULL)""",
            (now, now),
        )
        conn.execute(
            """INSERT INTO model_run VALUES
            ('model-1','e','qlib','LightGBM','f','a','2020','2023','2024','2024','2025','2025','accepted','shadow','x','fp','{}','{}','[]',?)""", (now,)
        )
        conn.execute(
            """INSERT INTO model_validation_run VALUES
            ('validation-1','model-1','v','passed','{}','[]','[]','fp',?)""", (now,)
        )
        conn.execute(
            """INSERT INTO current_shadow_snapshot VALUES
            ('shadow-1','model-1','validation-1','2026-07-20','a','none','','test','2020','2026-07-20','fp','p','sha',1,2,2,1,'current_shadow_ready','[]','{}',?)""", (now,)
        )
        conn.executemany(
            "INSERT INTO current_shadow_signal VALUES ('shadow-1',?,?,?,?,?,?)",
            [("000001.SZ", "SZ000001", 0.8, 1, 2, 1.0), ("000002.SZ", "SZ000002", 0.7, 2, 2, 0.0)],
        )
        conn.executemany(
            """INSERT INTO security_factor_snapshot
            (snapshot_id, security_code, latest_trade_date, observations, quality_status,
             quality_reasons_json, close, return_20d, return_60d, volatility_60d,
             avg_traded_value_20d, volume_ratio_20d, max_drawdown_250d, range_position_52w)
            VALUES ('factor-1', ?, '2026-07-20', 300, 'passed', '[]', ?, .05, .1, ?,
                    200000000, 1, -.2, .7)""",
            [("000001.SZ", 10, .20), ("000002.SZ", 20, .40)],
        )
    for code in ("000001.SZ", "000002.SZ"):
        run_id = store.start_research_run(
            code=code, as_of="2026-07-20", workflow_version="test-research"
        )
        store.save_research_artifact(
            run_id=run_id,
            artifact_type="research_assessment",
            schema_version=RESEARCH_ASSESSMENT_SCHEMA_VERSION,
            status="admit",
            payload={
                "schema_version": RESEARCH_ASSESSMENT_SCHEMA_VERSION,
                "policy_version": RESEARCH_ASSESSMENT_POLICY_VERSION,
                "security": {"code": code, "name": code},
                "as_of": "2026-07-20",
                "research_run_id": run_id,
                "signal": "admit",
                "weight_multiplier": 1.0,
                "evidence_confidence": 0.9,
                "material_negatives": [],
                "catalysts": [],
                "invalidating_conditions": ["后续财务事实恶化"],
                "reasons": ["测试评估准入"],
            },
        )
        store.finish_research_run(run_id, status="completed")
    return PaperTradingService(repo, store, quote_provider), store


def _set_assessment_signal(store: ResearchStore, code: str, signal: str) -> None:
    with store.connect() as conn:
        row = conn.execute(
            """SELECT a.artifact_id, a.payload_json FROM research_artifact a
               JOIN research_run r ON r.run_id=a.run_id
               WHERE r.security_code=? AND a.artifact_type='research_assessment'""",
            (code,),
        ).fetchone()
        payload = json.loads(row["payload_json"])
        payload["signal"] = signal
        payload["weight_multiplier"] = {
            "admit": 1.0, "reduce": 0.5, "defer": 0.0, "veto": 0.0,
        }[signal]
        payload_json = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        snapshot_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        conn.execute(
            "UPDATE research_artifact SET status=?, payload_json=?, snapshot_hash=? WHERE artifact_id=?",
            (signal, payload_json, snapshot_hash, row["artifact_id"]),
        )


def _insert_position(
    service: PaperTradingService,
    store: ResearchStore,
    code: str = "000002.SZ",
    *,
    available_quantity: int = 0,
) -> dict:
    account = service.default_account()
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO paper_position
               (account_id, security_code, quantity, available_quantity, average_cost,
                realized_pnl, last_buy_date, updated_at)
               VALUES (?, ?, 100, ?, 20, 0, '2026-07-20', '2026-07-20T08:00:00+00:00')""",
            (account["account_id"], code, available_quantity),
        )
    return account


class RealtimeProvider:
    def __init__(self, quote_time="2026-07-23 10:01:02+08:00"):
        self.quote_time = quote_time
        self.requested = []

    def get_realtime_quotes(self, codes):
        self.requested = list(codes)
        prices = {"000001.SZ": 13.0, "000300.SH": 4100.0}
        return [{
            "security_code": code, "quote_time": self.quote_time,
            "open": prices[code], "latest": prices[code] + 0.1,
            "high": prices[code] + 0.2, "low": prices[code] - 0.1,
            "volume": 100000, "amount": 1000000, "previous_close": prices[code] - 0.2,
            "source": "iFinD THS_RQ",
        } for code in codes if code in prices]


def test_portfolio_planning_does_not_mutate_paper_account_state(tmp_path):
    service, store = _service(tmp_path)
    account = service.default_account()
    _, shadow = service.portfolio_decision.sources("2026-07-20")
    positions = service._positions(account["account_id"], "2026-07-20", shadow["snapshot_id"])
    tables = (
        "paper_account",
        "paper_daily_run",
        "paper_order",
        "paper_position",
        "paper_nav_snapshot",
        "paper_realtime_quote",
    )
    with store.connect() as conn:
        before = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}

    plan = service.portfolio_decision.build_plan(
        account=account,
        positions=positions,
        as_of="2026-07-20",
        run_id="planning-only-run",
        created_at="2026-07-20T08:00:00+00:00",
        top_n=1,
        hold_rank_buffer=30,
        target_gross_exposure=0.50,
        max_position_weight=0.12,
        max_industry_weight=0.20,
        max_pair_correlation=0.85,
    )

    with store.connect() as conn:
        after = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}
    assert plan["orders"]
    assert after == before


def test_execution_service_persists_supplied_portfolio_plan(tmp_path):
    service, store = _service(tmp_path)

    class StubPortfolioDecision:
        def sources(self, as_of):
            assert as_of == "2026-07-20"
            return {"snapshot_id": "factor-1"}, {"snapshot_id": "shadow-1"}

        def build_plan(self, **kwargs):
            assert kwargs["account"]["account_id"]
            assert kwargs["positions"] == []
            return {
                "factor_snapshot_id": "factor-1",
                "shadow_snapshot_id": "shadow-1",
                "config": {"source": "stub-plan"},
                "market_summary": {"planned": True},
                "snapshot_hash": "stub-plan-hash",
                "orders": [{
                    "order_id": "stub-order",
                    "security_code": "000001.SZ",
                    "side": "buy",
                    "quantity": 100,
                    "reference_price": 10.0,
                    "target_weight": 0.10,
                    "reason": {"rule": "stubbed_decision"},
                }],
            }

    service.portfolio_decision = StubPortfolioDecision()
    assert isinstance(service, PaperExecutionService)
    run = service.create_daily_run(
        as_of="2026-07-20", account_id=None, top_n=1, hold_rank_buffer=30
    )

    assert run["config"] == {"source": "stub-plan"}
    assert run["market_summary"] == {"planned": True}
    assert run["orders"][0]["order_id"] == "stub-order"
    assert run["orders"][0]["reason"] == {"rule": "stubbed_decision"}
    with store.connect() as conn:
        stored = conn.execute(
            "SELECT snapshot_hash FROM paper_daily_run WHERE run_id=?", (run["run_id"],)
        ).fetchone()
    assert stored["snapshot_hash"] == "stub-plan-hash"


def test_local_price_uses_latest_row_at_or_before_as_of(tmp_path):
    service, _ = _service(tmp_path)

    assert service._price("000001.SZ", "2026-07-22") == ("2026-07-22", 12.0)


def test_daily_run_requires_review_and_fills_only_after_as_of(tmp_path):
    service, _ = _service(tmp_path)
    run = service.create_daily_run(
        as_of="2026-07-20", account_id=None, top_n=1, hold_rank_buffer=30
    )
    assert run["status"] == "awaiting_review"
    assert len(run["orders"]) == 1
    order = run["orders"][0]
    assert order["status"] == "proposed"
    assert order["quantity"] % 100 == 0

    service.review_order(order["order_id"], "approve", "tester", "批准模拟订单")
    same_day = service.settle(execution_date="2026-07-20")
    assert same_day["filled"] == []
    result = service.settle(execution_date="2026-07-21")
    assert len(result["filled"]) == 1
    assert result["filled"][0]["fill_date"] == "2026-07-21"
    assert result["filled"][0]["fill_price"] > 11.0
    dashboard = service.dashboard(as_of="2026-07-21")
    assert dashboard["positions"][0]["quantity"] == order["quantity"]
    assert dashboard["account"]["cash"] < dashboard["account"]["initial_cash"]


def test_daily_run_is_idempotent(tmp_path):
    service, _ = _service(tmp_path)
    first = service.create_daily_run(as_of="2026-07-20", account_id=None, top_n=1, hold_rank_buffer=30)
    second = service.create_daily_run(as_of="2026-07-20", account_id=first["account_id"], top_n=1, hold_rank_buffer=30)
    assert second["run_id"] == first["run_id"]
    assert second["reused"] is True


def test_benchmark_comparison_uses_common_inception_and_aligned_nav_dates(tmp_path):
    service, store = _service(tmp_path)
    account = service.default_account()
    service._mark_nav(account, "2026-07-20")
    run = service.create_daily_run(as_of="2026-07-20", account_id=None, top_n=1, hold_rank_buffer=30)
    with store.connect() as conn:
        conn.execute(
            """UPDATE paper_order SET status='filled', fill_date='2026-07-21', fill_price=11
               WHERE order_id=?""",
            (run["orders"][0]["order_id"],),
        )
        conn.execute(
            "UPDATE paper_account SET cash=990000 WHERE account_id=?",
            (account["account_id"],),
        )
    service._mark_nav(service.account(account["account_id"]), "2026-07-21")
    with store.connect() as conn:
        conn.execute(
            "UPDATE paper_account SET cash=1020000 WHERE account_id=?",
            (account["account_id"],),
        )
    service._mark_nav(service.account(account["account_id"]), "2026-07-22")
    latest_closes = {
        "000001.SH": 101.0,
        "399001.SZ": 102.0,
        "399006.SZ": 98.0,
        "000300.SH": 100.5,
    }
    rows = []
    for code in PAPER_BENCHMARKS:
        rows.extend([
            {"thscode": code, "time": "2026-07-20", "close": 100.0},
            {"thscode": code, "time": "2026-07-21", "close": 100.0},
            {"thscode": code, "time": "2026-07-22", "close": latest_closes[code]},
        ])
    saved = service.save_benchmark_prices(account["account_id"], rows)
    comparison = service.dashboard(account["account_id"], "2026-07-20")["benchmark_comparison"]

    assert saved["rows_written"] == 12
    assert comparison["status"] == "complete"
    assert comparison["inception_date"] == "2026-07-21"
    assert comparison["as_of"] == "2026-07-22"
    assert round(comparison["portfolio"]["points"][0]["cumulative_return"], 10) == -0.01
    assert round(comparison["portfolio"]["latest_return"], 10) == 0.02
    shanghai = next(item for item in comparison["benchmarks"] if item["code"] == "000001.SH")
    assert round(shanghai["latest_return"], 10) == 0.01
    assert round(shanghai["excess_return"], 10) == 0.01
    assert shanghai["coverage"] == 1.0


def test_benchmark_comparison_rejects_missing_inception_as_partial(tmp_path):
    service, store = _service(tmp_path)
    account = service.default_account()
    run = service.create_daily_run(as_of="2026-07-20", account_id=None, top_n=1, hold_rank_buffer=30)
    with store.connect() as conn:
        conn.execute(
            """UPDATE paper_order SET status='filled', fill_date='2026-07-21', fill_price=11
               WHERE order_id=?""",
            (run["orders"][0]["order_id"],),
        )
    service._mark_nav(account, "2026-07-21")
    service.save_benchmark_prices(account["account_id"], [
        {"thscode": code, "time": "2026-07-21", "close": 100.0}
        for code in PAPER_BENCHMARKS
    ])

    comparison = service.dashboard(account["account_id"], "2026-07-21")["benchmark_comparison"]

    assert comparison["status"] == "partial"
    assert all(item["status"] == "missing_baseline" for item in comparison["benchmarks"])
    assert any("之前的最近收盘价" in item for item in comparison["limitations"])


def test_benchmark_comparison_waits_for_first_filled_order(tmp_path):
    service, _ = _service(tmp_path)
    account = service.default_account()
    service._mark_nav(account, "2026-07-20")

    comparison = service.dashboard(account["account_id"], "2026-07-20")["benchmark_comparison"]

    assert comparison["status"] == "not_available"
    assert comparison["inception_date"] is None
    assert "首笔模拟成交后开始" in comparison["limitations"][0]


def test_risk_gate_rejects_illiquid_candidate_and_inverse_volatility_allocates_weight(tmp_path):
    service, store = _service(tmp_path)
    with store.connect() as conn:
        conn.execute(
            """UPDATE security_factor_snapshot SET avg_traded_value_20d=1000000
               WHERE snapshot_id='factor-1' AND security_code='000001.SZ'"""
        )
    run = service.create_daily_run(
        as_of="2026-07-20", account_id=None, top_n=2, hold_rank_buffer=30
    )
    summary = run["market_summary"]
    rejected = next(item for item in summary["candidate_evaluations"] if item["security_code"] == "000001.SZ")
    assert rejected["risk_status"] == "rejected"
    assert "liquidity" in rejected["rejection_reasons"]
    assert [item["security_code"] for item in summary["top_candidates"]] == ["000002.SZ"]
    assert run["orders"][0]["target_weight"] <= 0.12


def test_known_industry_weight_is_capped(tmp_path):
    service, store = _service(tmp_path)
    with store.connect() as conn:
        conn.execute("UPDATE security_master SET industry_l1='银行'")
    run = service.create_daily_run(
        as_of="2026-07-20", account_id=None, top_n=2, hold_rank_buffer=30,
        target_gross_exposure=0.24, max_industry_weight=0.20,
    )
    selected = run["market_summary"]["top_candidates"]
    assert run["market_summary"]["industry_control"]["status"] == "enforced"
    assert sum(item["target_weight"] for item in selected) <= 0.2000001
    assert all(item["target_weight"] <= 0.12 for item in selected)


def test_execution_skips_suspended_day_and_waits_for_next_tradable_open(tmp_path):
    service, _ = _service(tmp_path)
    run = service.create_daily_run(
        as_of="2026-07-20", account_id=None, top_n=1, hold_rank_buffer=30
    )
    service.review_order(run["orders"][0]["order_id"], "approve", "tester", "批准模拟订单")
    with sqlite3.connect(service.repository.db_path) as conn:
        conn.execute(
            "UPDATE stock_000001_SZ SET volume=NULL WHERE time='2026-07-21'"
        )
    suspended = service.settle(execution_date="2026-07-21")
    assert suspended["filled"] == []
    assert suspended["skipped"][0]["reason"] == "suspended_or_incomplete_quote"
    resumed = service.settle(execution_date="2026-07-22")
    assert resumed["filled"][0]["fill_date"] == "2026-07-22"


def test_current_strategy_cancels_approved_unfilled_prior_version(tmp_path):
    service, store = _service(tmp_path)
    current = service.create_daily_run(
        as_of="2026-07-20", account_id=None, top_n=1, hold_rank_buffer=30
    )
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO paper_daily_run VALUES
            ('legacy-run', ?, '2026-07-20', 'factor-1', 'shadow-1', 'legacy-v1',
             'awaiting_review', '{}', '{}', 'legacy-hash', '2026-07-20T09:00:00+00:00')""",
            (current["account_id"],),
        )
        conn.execute(
            """INSERT INTO paper_order
            (order_id, run_id, account_id, security_code, side, quantity, reference_price,
             target_weight, reason_json, status, reviewer, review_note, reviewed_at, created_at)
            VALUES ('legacy-order','legacy-run',?,'000001.SZ','buy',100,10,.1,'{}',
                    'approved','human','原人工批准','2026-07-20T10:00:00+00:00','2026-07-20T09:00:00+00:00')""",
            (current["account_id"],),
        )
    service.create_daily_run(
        as_of="2026-07-20", account_id=current["account_id"], top_n=1, hold_rank_buffer=30
    )
    with store.connect() as conn:
        order = conn.execute("SELECT status, reviewer, review_note FROM paper_order WHERE order_id='legacy-order'").fetchone()
        run_status = conn.execute("SELECT status FROM paper_daily_run WHERE run_id='legacy-run'").fetchone()[0]
    assert order["status"] == "cancelled"
    assert order["reviewer"] == "human"
    assert "原人工批准" in order["review_note"]
    assert "替代" in order["review_note"]
    assert run_status == "superseded"


def test_realtime_refresh_persists_quote_and_settles_at_official_open(tmp_path):
    provider = RealtimeProvider()
    service, store = _service(tmp_path, provider)
    run = service.create_daily_run(
        as_of="2026-07-20", account_id=None, top_n=1, hold_rank_buffer=30
    )
    order = run["orders"][0]
    service.review_order(order["order_id"], "approve", "tester", "批准模拟订单")
    with store.connect() as conn:
        conn.execute(
            "UPDATE paper_order SET reviewed_at='2026-07-23T08:00:00+08:00' WHERE order_id=?",
            (order["order_id"],),
        )

    result = service.settle_realtime()

    assert set(provider.requested) == {"000001.SZ", "000300.SH"}
    assert result["filled"][0]["fill_date"] == "2026-07-23"
    assert result["filled"][0]["fill_price"] > 13.0
    assert result["filled"][0]["execution_quote_id"]
    assert service.run(run["run_id"])["status"] == "completed"
    dashboard = service.dashboard(as_of="2026-07-22")
    assert dashboard["positions"][0]["close"] == 13.1
    assert dashboard["positions"][0]["price_source"] == "iFinD THS_RQ"
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM paper_realtime_quote").fetchone()[0] == 2


def test_realtime_settlement_rejects_order_approved_after_market_open(tmp_path):
    provider = RealtimeProvider()
    service, store = _service(tmp_path, provider)
    run = service.create_daily_run(
        as_of="2026-07-20", account_id=None, top_n=1, hold_rank_buffer=30
    )
    order = run["orders"][0]
    service.review_order(order["order_id"], "approve", "tester", "批准模拟订单")
    with store.connect() as conn:
        conn.execute(
            "UPDATE paper_order SET reviewed_at='2026-07-23T10:00:00+08:00' WHERE order_id=?",
            (order["order_id"],),
        )

    result = service.settle_realtime()

    assert result["filled"] == []
    assert result["skipped"][0]["reason"] == "approved_after_market_open"
    assert service.run(run["run_id"])["orders"][0]["status"] == "approved"


def test_missing_research_assessment_defers_new_position(tmp_path):
    service, store = _service(tmp_path)
    with store.connect() as conn:
        conn.execute(
            """DELETE FROM research_artifact WHERE run_id IN
               (SELECT run_id FROM research_run WHERE security_code='000001.SZ')"""
        )
    run = service.create_daily_run(
        as_of="2026-07-20", account_id=None, top_n=1, hold_rank_buffer=30
    )
    first = next(
        item for item in run["market_summary"]["candidate_evaluations"]
        if item["security_code"] == "000001.SZ"
    )
    assert first["research_status"] == "defer"
    assert first["decision_status"] == "defer"
    assert run["orders"][0]["security_code"] == "000002.SZ"


def test_reduce_assessment_halves_pre_research_weight(tmp_path):
    service, store = _service(tmp_path)
    with store.connect() as conn:
        row = conn.execute(
            """SELECT a.artifact_id, a.payload_json FROM research_artifact a
               JOIN research_run r ON r.run_id=a.run_id
               WHERE r.security_code='000001.SZ' AND a.artifact_type='research_assessment'"""
        ).fetchone()
        payload = json.loads(row["payload_json"])
        payload["signal"] = "reduce"
        payload["weight_multiplier"] = 0.5
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        snapshot_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        conn.execute(
            "UPDATE research_artifact SET status='reduce', payload_json=?, snapshot_hash=? WHERE artifact_id=?",
            (payload_json, snapshot_hash, row["artifact_id"]),
        )
    run = service.create_daily_run(
        as_of="2026-07-20", account_id=None, top_n=1, hold_rank_buffer=30
    )
    candidate = run["market_summary"]["top_candidates"][0]
    assert candidate["target_weight"] == candidate["pre_research_weight"] * 0.5


def test_holding_outside_rank_buffer_is_evaluated_and_preserved(tmp_path):
    service, store = _service(tmp_path)
    account = _insert_position(service, store)

    run = service.create_daily_run(
        as_of="2026-07-20", account_id=account["account_id"], top_n=1, hold_rank_buffer=1
    )

    selected = run["market_summary"]["top_candidates"]
    assert [item["security_code"] for item in selected] == ["000002.SZ"]
    assert run["market_summary"]["holding_control"]["evaluated"] == 1
    assert not any(
        order["side"] == "buy" and order["security_code"] == "000001.SZ"
        for order in run["orders"]
    )


def test_deferred_holding_is_frozen_and_blocks_new_entry_slot(tmp_path):
    service, store = _service(tmp_path)
    account = _insert_position(service, store)
    _set_assessment_signal(store, "000002.SZ", "defer")

    run = service.create_daily_run(
        as_of="2026-07-20", account_id=account["account_id"], top_n=1, hold_rank_buffer=1
    )

    control = run["market_summary"]["holding_control"]
    assert control["frozen_codes"] == ["000002.SZ"]
    assert control["occupied_slots"] == 1
    assert run["orders"] == []
    assert run["status"] == "completed"


def test_t1_holding_exit_is_proposed_and_releases_slot_before_buy(tmp_path):
    service, store = _service(tmp_path)
    account = _insert_position(service, store, available_quantity=0)
    _set_assessment_signal(store, "000002.SZ", "veto")

    run = service.create_daily_run(
        as_of="2026-07-20", account_id=account["account_id"], top_n=1, hold_rank_buffer=1
    )
    assert [(item["side"], item["security_code"]) for item in run["orders"]] == [
        ("sell", "000002.SZ"), ("buy", "000001.SZ")
    ]
    for order in run["orders"]:
        service.review_order(order["order_id"], "approve", "tester", "批准模拟订单")

    result = service.settle(execution_date="2026-07-21", account_id=account["account_id"])

    assert [(item["side"], item["security_code"]) for item in service.run(run["run_id"])["orders"]] == [
        ("sell", "000002.SZ"), ("buy", "000001.SZ")
    ]
    assert len(result["filled"]) == 2
    positions = [item for item in service.dashboard(account["account_id"], "2026-07-21")["positions"] if item["quantity"] > 0]
    assert [item["security_code"] for item in positions] == ["000001.SZ"]


def test_execution_blocks_new_name_when_exit_did_not_release_slot(tmp_path):
    service, store = _service(tmp_path)
    run = service.create_daily_run(
        as_of="2026-07-20", account_id=None, top_n=1, hold_rank_buffer=1
    )
    order = run["orders"][0]
    service.review_order(order["order_id"], "approve", "tester", "批准模拟订单")
    _insert_position(service, store, available_quantity=100)

    result = service.settle(execution_date="2026-07-21", account_id=run["account_id"])

    assert result["filled"] == []
    assert result["skipped"][0]["reason"] == "持仓数量上限尚未通过卖出释放"


def test_daily_run_cannot_backdate_after_account_has_fills(tmp_path):
    service, _ = _service(tmp_path)
    run = service.create_daily_run(
        as_of="2026-07-20", account_id=None, top_n=1, hold_rank_buffer=30
    )
    service.review_order(run["orders"][0]["order_id"], "approve", "tester", "批准模拟订单")
    service.settle(execution_date="2026-07-21")
    try:
        service.create_daily_run(
            as_of="2026-07-20", account_id=run["account_id"], top_n=1, hold_rank_buffer=30
        )
    except ValueError as exc:
        assert "禁止回溯" in str(exc)
    else:
        raise AssertionError("成交后的账户不得生成回溯研究批次")
