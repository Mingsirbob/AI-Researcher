import sqlite3
from datetime import date, timedelta

from app.data_access import StockRepository
from app.factor_evaluation import FactorEvaluationService
from app.factor_lab import FactorLabService
from app.research_store import ResearchStore


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
                price *= 1 + growth
                rows.append(
                    (
                        (start + timedelta(days=day_index)).isoformat(),
                        price * 0.995, price * 1.005, price * 0.99, price,
                        price, 1_000_000 + day_index * 100,
                    )
                )
            conn.executemany(f'INSERT INTO "{table}" VALUES (?, ?, ?, ?, ?, ?, ?)', rows)


def create_service(tmp_path):
    price_db = tmp_path / "stock_data_qfq.db"
    create_price_db(price_db)
    repository = StockRepository(price_db)
    store = ResearchStore(tmp_path / "state.db", tmp_path / "documents")
    store.bootstrap_securities(price_db)
    factor_lab = FactorLabService(
        store, repository, adjustment="CPS:2", universe="CSI300 current"
    )
    return FactorEvaluationService(store, repository, factor_lab)


def test_factor_evaluation_computes_ic_layers_decay_turnover_and_correlation(tmp_path):
    service = create_service(tmp_path)

    result = service.run(
        start_date="2025-10-01",
        end_date="2026-02-28",
        rebalance_step=20,
        horizons=(1, 5, 20),
        layer_count=5,
    )
    repeated = service.run(
        start_date="2025-10-01",
        end_date="2026-02-28",
        rebalance_step=20,
        horizons=(1, 5, 20),
        layer_count=5,
    )

    run = result["run"]
    momentum = next(
        item for item in result["metrics"]
        if item["factor_id"] == "momentum_20d" and item["horizon"] == 20
    )
    assert run["status"] == "completed"
    assert run["adjustment"] == "CPS:2"
    assert run["factor_count"] == 9
    assert momentum["factor_version"] == 2
    assert momentum["mean_rank_ic"] > 0.99
    assert momentum["mean_layer_spread"] > 0
    assert momentum["layer_monotonicity"] > 0.99
    assert momentum["top_layer_turnover"] == 0
    assert result["correlations"]
    assert repeated["run"]["evaluation_id"] == run["evaluation_id"]
    assert repeated["reused"] is True
