import importlib

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


def test_legacy_module_paths_alias_domain_implementations():
    mappings = {
        "app.config": "app.core.config",
        "app.ifind": "app.integrations.ifind",
        "app.data_access": "app.market.repository",
        "app.research_store": "app.research.store",
        "app.quant_store": "app.quant.store",
        "app.paper_trading": "app.paper.service",
        "app.daily_batch": "app.workflows.daily_batch",
    }
    for legacy, current in mappings.items():
        assert importlib.import_module(legacy) is importlib.import_module(current)
