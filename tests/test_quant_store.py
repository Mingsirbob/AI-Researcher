import sqlite3

from app.market.repository import StockRepository
from app.core.migrations import applied_migrations
from app.quant.store import QuantStore
from app.research.store import ResearchStore


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


def test_quant_store_isolates_writes_and_syncs_security_projection(tmp_path):
    price_db = tmp_path / "prices.db"
    with sqlite3.connect(price_db):
        pass
    repository = StockRepository(price_db)
    research_store = ResearchStore(tmp_path / "research.db", tmp_path / "documents")
    research_store.upsert_security_name("000001.SZ", "平安银行", source="test")

    quant_store = QuantStore(tmp_path / "quant_research.db", research_store)
    initial_snapshot = quant_store.start_factor_snapshot(
        as_of="2026-07-20",
        factor_version="quant-v1",
        source_fingerprint="initial-fingerprint",
        source_db_size=1,
        source_db_mtime_ns=1,
    )
    quant_store.finish_factor_snapshot(initial_snapshot, [_factor_row("000001.SZ")])

    assert quant_store.stats()["factor_evaluations"] == 0
    assert quant_store.stats()["factor_backtests"] == 0

    assert quant_store.factor_snapshot(initial_snapshot)["status"] == "completed"
    result = quant_store.list_factor_rows(initial_snapshot)
    assert result["items"][0]["security_name"] == "平安银行"

    new_snapshot = quant_store.start_factor_snapshot(
        as_of="2026-07-21",
        factor_version="quant-v1",
        source_fingerprint="quant-only-fingerprint",
        source_db_size=2,
        source_db_mtime_ns=2,
    )
    quant_store.finish_factor_snapshot(new_snapshot, [_factor_row("000001.SZ")])
    with research_store.connect() as source, quant_store.connect() as target:
        assert "factor_snapshot" not in {
            row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
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
        "0017_split_quant_core_database",
    ]

    QuantStore(tmp_path / "quant_research.db", research_store)
    with quant_store.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM factor_snapshot WHERE snapshot_id=?", (new_snapshot,)
        ).fetchone()[0] == 1
