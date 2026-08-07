from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routers import quant as quant_router
from app.quant.simple_research import SimpleResearchCatalog
from tests.test_factor_backtest import create_services


def test_simple_catalog_migrates_factors_and_saved_experiment(tmp_path):
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

    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM research_factor_set").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM research_factor").fetchone()[0] >= 10
        experiment = conn.execute(
            "SELECT * FROM research_experiment WHERE experiment_id=?",
            (result["run"]["backtest_id"],),
        ).fetchone()
        assert experiment["experiment_type"] == "backtest"
        assert conn.execute(
            "SELECT COUNT(*) FROM research_experiment_series WHERE experiment_id=?",
            (result["run"]["backtest_id"],),
        ).fetchone()[0] == len(result["nav"]) * 3

    catalog = SimpleResearchCatalog(store)
    assert catalog.overview()["counts"]["experiments"] >= 2
    assert {item["factor_set_id"] for item in catalog.factor_sets()} == {
        "alpha158",
        "custom",
    }
    assert catalog.factors(factor_set_id="custom")
    assert catalog.experiments(experiment_type="backtest", limit=1)[0][
        "experiment_id"
    ] == result["run"]["backtest_id"]
    detail = catalog.experiment(result["run"]["backtest_id"])
    assert detail is not None
    assert len(detail["series"]) == len(result["nav"]) * 3


def test_simple_catalog_read_api(tmp_path, monkeypatch):
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
    monkeypatch.setattr(
        quant_router, "simple_research_catalog", SimpleResearchCatalog(store)
    )
    test_app = FastAPI()
    test_app.state.container = object()
    test_app.include_router(quant_router.router)
    client = TestClient(test_app)

    assert client.get("/api/quant-research/overview").status_code == 200
    assert client.get("/api/quant-research/factor-sets").json()["items"]
    assert client.get(
        "/api/quant-research/factors", params={"factor_set_id": "custom"}
    ).json()["items"]
    experiments = client.get(
        "/api/quant-research/experiments",
        params={"experiment_type": "backtest", "limit": 1},
    ).json()["items"]
    assert experiments[0]["experiment_id"] == result["run"]["backtest_id"]
    detail = client.get(
        f"/api/quant-research/experiments/{result['run']['backtest_id']}"
    )
    assert detail.status_code == 200
    assert detail.json()["series"]
    assert client.get("/api/quant-research/experiments/missing").status_code == 404
