import sqlite3

from app.data_access import StockRepository
from app.daily_batch import DailyBatchStore
from app.factor_lab import FactorLabService
from app.factor_evaluation import FactorEvaluationService
from app.factor_backtest import FactorBacktestService
from app.factor_release import FactorReleaseService
from app.migrations import applied_migrations, apply_migration
from app.observability import IFindCallObserver
from app.paper_trading import PaperExecutionService
from app.research_store import ResearchStore
from app.runtime_events import RuntimeEventStore
from app.state import ThesisStore

def test_shared_migration_journal_contains_all_schema_entries(tmp_path):
    state_db = tmp_path / "state.db"
    price_db = tmp_path / "prices.db"
    with sqlite3.connect(price_db):
        pass
    store = ResearchStore(state_db, tmp_path / "documents")
    ThesisStore(state_db)
    PaperExecutionService(StockRepository(price_db), store)
    DailyBatchStore(store)
    RuntimeEventStore(state_db)
    IFindCallObserver(state_db)
    repository = StockRepository(price_db)
    factor_lab = FactorLabService(store, repository)
    factor_evaluation = FactorEvaluationService(store, repository, factor_lab)
    FactorBacktestService(store, repository, factor_lab, factor_evaluation)
    FactorReleaseService(store)

    assert applied_migrations(store.connect) == [
        "0001_research_core",
        "0002_thesis_monitoring",
        "0003_paper_trading",
        "0004_daily_batch",
        "0005_runtime_events",
        "0006_ifind_observability",
        "0007_paper_benchmarks",
        "0008_factor_lab",
        "0009_factor_evaluation",
        "0010_factor_qfq_liquidity_contract",
        "0011_factor_backtest",
        "0012_factor_release",
        "0013_paper_strategies",
    ]


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
