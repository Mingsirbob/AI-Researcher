import sqlite3

from app.data_access import StockRepository
from app.factor_backtest import FactorBacktestService
from app.factor_evaluation import FactorEvaluationService
from app.factor_lab import FactorLabService
from app.factor_release import FactorReleaseService
from app.migrations import applied_migrations
from app.quant_store import QuantStore
from app.research_store import ResearchStore


def _factor_row(code: str) -> dict:
    return {
        "security_code": code,
        "latest_trade_date": "2026-07-20",
        "observations": 300,
        "quality_status": "passed",
        "quality_reasons": [],
        "close": 10.0,
        "return_20d": 0.05,
        "return_60d": 0.12,
        "volatility_60d": 0.20,
        "avg_traded_value_20d": 200_000_000.0,
        "volume_ratio_20d": 1.1,
        "max_drawdown_250d": -0.15,
        "range_position_52w": 0.75,
    }


def test_quant_store_migrates_history_and_isolates_new_writes(tmp_path):
    price_db = tmp_path / "prices.db"
    with sqlite3.connect(price_db):
        pass
    repository = StockRepository(price_db)
    research_store = ResearchStore(
        tmp_path / "research.db", tmp_path / "documents", retain_split_domains=True
    )
    research_store.upsert_security_name("000001.SZ", "平安银行", source="test")

    legacy_lab = FactorLabService(research_store, repository)
    legacy_evaluation = FactorEvaluationService(research_store, repository, legacy_lab)
    FactorBacktestService(research_store, repository, legacy_lab, legacy_evaluation)
    FactorReleaseService(research_store)
    legacy_snapshot = research_store.start_factor_snapshot(
        as_of="2026-07-20",
        factor_version="legacy-v1",
        source_fingerprint="legacy-fingerprint",
        source_db_size=1,
        source_db_mtime_ns=1,
    )
    research_store.finish_factor_snapshot(legacy_snapshot, [_factor_row("000001.SZ")])

    quant_store = QuantStore(tmp_path / "quant_research.db", research_store)
    assert quant_store.stats()["factor_evaluations"] == 0
    assert quant_store.stats()["factor_backtests"] == 0
    quant_lab = FactorLabService(quant_store, repository)
    quant_evaluation = FactorEvaluationService(quant_store, repository, quant_lab)
    FactorBacktestService(quant_store, repository, quant_lab, quant_evaluation)
    FactorReleaseService(quant_store)

    assert quant_store.factor_snapshot(legacy_snapshot)["status"] == "completed"
    result = quant_store.list_factor_rows(legacy_snapshot)
    assert result["items"][0]["security_name"] == "平安银行"
    with research_store.connect() as source, quant_store.connect() as target:
        assert target.execute("SELECT COUNT(*) FROM factor_version").fetchone()[0] == source.execute(
            "SELECT COUNT(*) FROM factor_version"
        ).fetchone()[0]

    new_snapshot = quant_store.start_factor_snapshot(
        as_of="2026-07-21",
        factor_version="quant-v1",
        source_fingerprint="quant-only-fingerprint",
        source_db_size=2,
        source_db_mtime_ns=2,
    )
    quant_store.finish_factor_snapshot(new_snapshot, [_factor_row("000001.SZ")])
    with research_store.connect() as source, quant_store.connect() as target:
        assert source.execute(
            "SELECT COUNT(*) FROM factor_snapshot WHERE snapshot_id=?", (new_snapshot,)
        ).fetchone()[0] == 0
        assert target.execute(
            "SELECT COUNT(*) FROM factor_snapshot WHERE snapshot_id=?", (new_snapshot,)
        ).fetchone()[0] == 1
        assert "current_shadow_snapshot" not in {
            row[2] for row in source.execute("PRAGMA foreign_key_list(decision_case)")
        }

    research_store.upsert_security_name("000001.SZ", "平安银行新名称", source="test")
    quant_store.sync_security_projection()
    assert quant_store.list_factor_rows(new_snapshot)["items"][0]["security_name"] == "平安银行新名称"

    assert applied_migrations(quant_store.connect) == [
        "0001_quant_core",
        "0008_factor_lab",
        "0009_factor_evaluation",
        "0010_factor_qfq_liquidity_contract",
        "0011_factor_backtest",
        "0012_factor_release",
        "0017_split_quant_core_database",
        "0018_split_factor_lab_database",
        "0019_split_factor_evaluation_database",
        "0020_split_factor_backtest_database",
        "0021_split_factor_release_database",
    ]

    QuantStore(tmp_path / "quant_research.db", research_store)
    with quant_store.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM factor_snapshot WHERE snapshot_id=?", (new_snapshot,)
        ).fetchone()[0] == 1
