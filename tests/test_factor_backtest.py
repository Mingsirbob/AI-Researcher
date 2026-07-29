import sqlite3
from datetime import date, timedelta

from app.market.repository import StockRepository
from app.quant.factor_backtest import FactorBacktestService
from app.quant.factor_evaluation import FactorEvaluationService
from app.quant.factor_lab import FactorLabService
from app.research.store import ResearchStore


def create_price_db(path):
    with sqlite3.connect(path) as conn:
        for security_index in range(40):
            code = f"{security_index + 1:06d}_SZ"
            table = f"stock_{code}"
            conn.execute(
                f"""
                CREATE TABLE "{table}" (
                    time TEXT PRIMARY KEY, open REAL, high REAL, low REAL,
                    close REAL, vwap REAL, volume REAL
                )
                """
            )
            growth = 0.0002 + security_index * 0.00005
            price = 10.0
            rows = []
            start = date(2025, 1, 1)
            for day_index in range(430):
                open_price = price
                price *= 1 + growth
                rows.append(
                    (
                        (start + timedelta(days=day_index)).isoformat(),
                        open_price, max(open_price, price) * 1.002,
                        min(open_price, price) * 0.998, price, price,
                        1_000_000 + day_index * 100,
                    )
                )
            conn.executemany(f'INSERT INTO "{table}" VALUES (?, ?, ?, ?, ?, ?, ?)', rows)


def create_services(tmp_path):
    price_db = tmp_path / "stock_data_qfq.db"
    create_price_db(price_db)
    repository = StockRepository(price_db)
    store = ResearchStore(
        tmp_path / "state.db", tmp_path / "documents", retain_split_domains=True
    )
    store.bootstrap_securities(price_db)
    factor_lab = FactorLabService(
        store, repository, adjustment="CPS:2", universe="CSI300 current"
    )
    evaluation = FactorEvaluationService(store, repository, factor_lab)
    backtest = FactorBacktestService(store, repository, factor_lab, evaluation)
    return store, evaluation, backtest


def test_factor_backtest_delays_execution_applies_costs_and_reuses_input(tmp_path):
    store, evaluation, backtest = create_services(tmp_path)
    evaluated = evaluation.run(
        start_date="2025-10-01",
        end_date="2026-02-28",
        rebalance_step=20,
        horizons=(1, 5, 20),
        layer_count=5,
    )

    result = backtest.run(
        evaluation_id=evaluated["run"]["evaluation_id"],
        factor_id="momentum_20d",
        top_n=10,
        rebalance_step=20,
    )
    repeated = backtest.run(
        evaluation_id=evaluated["run"]["evaluation_id"],
        factor_id="momentum_20d",
        top_n=10,
        rebalance_step=20,
    )
    no_cost = backtest.run(
        evaluation_id=evaluated["run"]["evaluation_id"],
        factor_id="momentum_20d",
        top_n=10,
        rebalance_step=20,
        commission_rate=0,
        stamp_duty_rate=0,
        slippage_rate=0,
    )

    run = result["run"]
    first_rebalance = result["rebalances"][0]
    assert run["status"] == "completed"
    assert run["factor_version"] == 2
    assert run["benchmark_code"] == "CSI300_CURRENT_EQUAL_WEIGHT"
    assert first_rebalance["execution_date"] > first_rebalance["signal_date"]
    assert run["metrics"]["total_cost"] > 0
    assert run["metrics"]["cumulative_return"] > run["metrics"]["benchmark_cumulative_return"]
    assert run["metrics"]["final_nav"] < no_cost["run"]["metrics"]["final_nav"]
    assert result["nav"][0]["cumulative_return"] == 0
    assert result["nav"][-1]["holdings_count"] == 10
    assert repeated["run"]["backtest_id"] == run["backtest_id"]
    assert repeated["reused"] is True
    with store.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM factor_backtest_trade WHERE backtest_id=?",
            (run["backtest_id"],),
        ).fetchone()[0] == run["trade_count"]
