from pathlib import Path

import app
from app.paper.benchmarks import PaperBenchmarkMixin
from app.paper.dashboard import PaperDashboardMixin
from app.paper.execution import PaperExecutionMixin
from app.paper.service import PaperExecutionService
from app.paper.store import PaperStoreMixin
from app.quant.store import QuantStore
from app.research.store import ResearchStore


def test_quant_store_is_independent_from_research_store():
    assert not issubclass(QuantStore, ResearchStore)
    quant_only_methods = {
        "register_model_run",
        "latest_current_shadow",
        "start_factor_snapshot",
        "list_factor_rows",
        "list_research_candidates",
    }
    assert quant_only_methods.issubset(QuantStore.__dict__)
    assert quant_only_methods.isdisjoint(ResearchStore.__dict__)


def test_paper_facade_composes_focused_capabilities():
    assert PaperExecutionService.__mro__[1:5] == (
        PaperDashboardMixin,
        PaperExecutionMixin,
        PaperBenchmarkMixin,
        PaperStoreMixin,
    )


def test_legacy_root_modules_are_retired():
    app_root = Path(app.__file__).parent
    retired = {
        "adjusted_data.py",
        "analysis.py",
        "announcement_pipeline.py",
        "company_research.py",
        "config.py",
        "current_shadow.py",
        "current_shadow_service.py",
        "daily_batch.py",
        "data_access.py",
        "decision_cases.py",
        "decision_outcomes.py",
        "document_pipeline.py",
        "evidence_acceptance.py",
        "factor_backtest.py",
        "factor_evaluation.py",
        "factor_lab.py",
        "factor_release.py",
        "financial_extraction.py",
        "frontend_cutover.py",
        "ifind.py",
        "llm.py",
        "migrations.py",
        "model_registry.py",
        "monitoring.py",
        "observability.py",
        "paper_strategies.py",
        "paper_trading.py",
        "portfolio_decision.py",
        "primitives.py",
        "quant_research.py",
        "quant_store.py",
        "research_assessment.py",
        "research_store.py",
        "research_workflow.py",
        "resilience.py",
        "runtime_events.py",
        "sqlite_store.py",
        "state.py",
        "updater.py",
    }
    root_modules = {path.name for path in app_root.glob("*.py")}

    assert retired.isdisjoint(root_modules)
