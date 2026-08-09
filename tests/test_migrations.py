import sqlite3

from app.market.repository import StockRepository
from app.workflows.daily_batch import DailyBatchStore
from app.core.migrations import applied_migrations, apply_migration
from app.core.observability import IFindCallObserver
from app.paper.service import PaperExecutionService
from app.paper.run_store import PaperRunStore
from app.research.store import RETIRED_SPLIT_DOMAIN_TABLES, ResearchStore
from app.core.runtime_events import RuntimeEventStore
from app.core.sqlite_store import SQLiteStore
from app.thesis.store import ThesisStore

def test_shared_migration_journal_contains_all_schema_entries(tmp_path):
    state_db = tmp_path / "state.db"
    price_db = tmp_path / "prices.db"
    with sqlite3.connect(price_db):
        pass
    store = ResearchStore(
        state_db, tmp_path / "documents", retain_split_domains=True
    )
    ThesisStore(state_db)
    PaperExecutionService(StockRepository(price_db), store)
    DailyBatchStore(
        PaperRunStore(tmp_path / "paper"),
        account_resolver=lambda account_id: {"account_id": account_id, "name": account_id},
        event_store=RuntimeEventStore(store),
    )
    RuntimeEventStore(state_db)
    IFindCallObserver(state_db)
    migrations = applied_migrations(store.connect)
    assert {"0001_research_core", "0002_thesis_monitoring", "0005_runtime_events",
            "0006_ifind_observability", "0016_quant_reference_boundary"}.issubset(migrations)
    assert not any(item.startswith(("0003_", "0004_", "0007_", "0013_", "0023_", "0025_", "0028_", "0029_", "0030_", "0031_")) for item in migrations)


def test_apply_migration_is_idempotent(tmp_path):
    store = ResearchStore(tmp_path / "state.db", tmp_path / "documents")
    calls = []

    def create_probe_table():
        calls.append("called")
        with store.connect() as conn:
            conn.execute("CREATE TABLE migration_probe (value TEXT)")

    assert apply_migration(store.connect, "test_probe", create_probe_table) is True
    assert apply_migration(store.connect, "test_probe", create_probe_table) is False
    assert calls == ["called"]
    assert "test_probe" in applied_migrations(store.connect)


def test_research_store_retires_split_domain_tables_by_default(tmp_path):
    store = ResearchStore(tmp_path / "state.db", tmp_path / "documents")

    with store.connect() as conn:
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert not tables.intersection(RETIRED_SPLIT_DOMAIN_TABLES)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert "0022_retire_split_domain_tables" in applied_migrations(store.connect)
    assert store.stats()["factor_snapshots"] == 0
    assert store.stats()["model_runs"] == 0


def test_paper_v2_has_no_migration_journal(tmp_path):
    state_db = tmp_path / "state.db"
    price_db = tmp_path / "prices.db"
    with sqlite3.connect(price_db):
        pass
    research_store = ResearchStore(state_db, tmp_path / "documents")
    paper_store = SQLiteStore(tmp_path / "paper_trading.db")

    PaperExecutionService(
        StockRepository(price_db), research_store, paper_store=paper_store
    )
    with paper_store.connect() as conn:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )}
    assert tables == {"paper_account", "paper_proposal", "paper_trade",
                      "paper_position", "paper_nav_snapshot"}
