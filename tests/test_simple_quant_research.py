from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routers import quant as quant_router
from app.core.sqlite_store import SQLiteStore
from app.quant.simple_research import SimpleResearchCatalog


def _catalog(tmp_path):
    return SimpleResearchCatalog(SQLiteStore(tmp_path / "quant_research.db"))


def _seed(catalog):
    factor = catalog.create_factor(
        factor_id="momentum_20d", name="20日动量", description="区间动量因子",
        template_id="momentum", window=20, direction="positive", owner="test",
    )
    catalog.save_backtest({
        "run": {
            "backtest_id": "backtest-1", "factor_id": factor["factor_id"],
            "universe": "all_a", "requested_start_date": "2025-10-01",
            "requested_end_date": "2026-02-28", "top_n": 10,
            "rebalance_step": 20, "metrics": {"annualized_return": 0.12},
            "status": "completed", "started_at": "2026-03-01T00:00:00Z",
            "finished_at": "2026-03-01T00:01:00Z",
        },
        "nav": [
            {"trading_date": "2025-10-01", "nav": 1.0, "benchmark_nav": 1.0, "drawdown": 0.0},
            {"trading_date": "2026-02-28", "nav": 1.12, "benchmark_nav": 1.05, "drawdown": -0.03},
        ],
    })
    return factor


def test_simple_catalog_saves_factor_and_backtest(tmp_path):
    catalog = _catalog(tmp_path)
    factor = _seed(catalog)
    store = catalog.store
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM quant_factor").fetchone()[0] == 1
        experiment = conn.execute(
            "SELECT * FROM quant_backtest WHERE backtest_id=?",
            ("backtest-1",),
        ).fetchone()
        assert experiment["target_type"] == "factor"

    assert factor["expression"] == "close / Ref(close, 20) - 1"
    assert catalog.overview()["counts"]["experiments"] == 1
    assert {item["factor_set_id"] for item in catalog.factor_sets()} == {"default"}
    assert catalog.factors(factor_set_id="default")
    assert catalog.experiments(experiment_type="backtest", limit=1)[0][
        "experiment_id"
    ] == "backtest-1"
    detail = catalog.experiment("backtest-1")
    assert detail is not None
    assert len(detail["series"]) == 6


def test_simple_catalog_read_api(tmp_path, monkeypatch):
    catalog = _catalog(tmp_path)
    _seed(catalog)
    monkeypatch.setattr(
        quant_router, "simple_research_catalog", catalog
    )
    test_app = FastAPI()
    test_app.state.container = object()
    test_app.include_router(quant_router.router)
    client = TestClient(test_app)

    assert client.get("/api/quant-research/overview").status_code == 200
    assert client.get("/api/quant-research/factor-sets").json()["items"]
    assert client.get(
        "/api/quant-research/factors", params={"factor_set_id": "default"}
    ).json()["items"]
    experiments = client.get(
        "/api/quant-research/experiments",
        params={"experiment_type": "backtest", "limit": 1},
    ).json()["items"]
    assert experiments[0]["experiment_id"] == "backtest-1"
    detail = client.get(
        "/api/quant-research/experiments/backtest-1"
    )
    assert detail.status_code == 200
    assert detail.json()["series"]
    assert client.get("/api/quant-research/experiments/missing").status_code == 404
