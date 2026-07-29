from __future__ import annotations

from dataclasses import dataclass

from app.research.announcements import AnnouncementPipeline
from app.research.company import CompanyResearchService
from app.core.config import ROOT, Settings, settings
from app.quant.current_shadow_service import CurrentShadowService
from app.workflows.daily_batch import DailyBatchRunner, DailyBatchStore
from app.market.repository import StockRepository
from app.decision.cases import DecisionCaseService
from app.decision.outcomes import DecisionOutcomeService
from app.research.acceptance import EvidenceAcceptanceService
from app.quant.factor_backtest import FactorBacktestService
from app.quant.factor_evaluation import FactorEvaluationService
from app.quant.factor_lab import FactorLabService
from app.quant.factor_release import FactorReleaseService
from app.integrations.ifind import IFindService
from app.paper.service import PaperTradingService
from app.quant.factors import FactorSnapshotService
from app.quant.store import QuantStore
from app.research.store import ResearchStore
from app.core.runtime_events import RuntimeEventStore
from app.core.sqlite_store import SQLiteStore
from app.thesis.store import ThesisStore


@dataclass(slots=True)
class AppContainer:
    settings: Settings
    repo: StockRepository
    thesis_store: ThesisStore
    ifind_service: IFindService
    research_store: ResearchStore
    paper_store: SQLiteStore
    quant_store: QuantStore
    company_research_service: CompanyResearchService
    announcement_pipeline: AnnouncementPipeline
    factor_snapshot_service: FactorSnapshotService
    factor_lab_repository: StockRepository
    factor_lab_service: FactorLabService
    factor_evaluation_service: FactorEvaluationService | None
    factor_backtest_service: FactorBacktestService | None
    factor_release_service: FactorReleaseService
    evidence_acceptance_service: EvidenceAcceptanceService
    decision_case_service: DecisionCaseService
    decision_outcome_service: DecisionOutcomeService
    paper_trading_service: PaperTradingService
    current_shadow_service: CurrentShadowService
    daily_batch_store: DailyBatchStore
    daily_batch_runner: DailyBatchRunner

    def close(self) -> None:
        self.ifind_service.logout()


def create_app_container(app_settings: Settings = settings) -> AppContainer:
    repo = StockRepository(app_settings.stock_db)
    thesis_store = ThesisStore(app_settings.state_db)
    ifind_service = IFindService(app_settings)
    research_store = ResearchStore(app_settings.state_db, app_settings.document_root)
    paper_store = SQLiteStore(app_settings.paper_db)
    quant_store = QuantStore(app_settings.quant_db, research_store)
    factor_lab_repository = (
        StockRepository(app_settings.stock_qfq_db)
        if app_settings.stock_qfq_db.exists()
        else repo
    )
    factor_lab_service = FactorLabService(
        quant_store,
        factor_lab_repository,
        adjustment="CPS:2" if app_settings.stock_qfq_db.exists() else "unadjusted",
        universe=(
            "CSI300 current"
            if app_settings.stock_qfq_db.exists()
            else "A-share local coverage"
        ),
    )
    factor_evaluation_service = (
        FactorEvaluationService(quant_store, factor_lab_repository, factor_lab_service)
        if app_settings.stock_qfq_db.exists()
        else None
    )
    factor_backtest_service = (
        FactorBacktestService(
            quant_store,
            factor_lab_repository,
            factor_lab_service,
            factor_evaluation_service,
        )
        if factor_evaluation_service
        else None
    )
    paper_trading_service = PaperTradingService(
        repo,
        research_store,
        ifind_service,
        paper_store=paper_store,
        quant_store=quant_store,
    )
    daily_batch_store = DailyBatchStore(
        paper_store,
        event_store=RuntimeEventStore(research_store),
        legacy_store=research_store,
    )
    return AppContainer(
        settings=app_settings,
        repo=repo,
        thesis_store=thesis_store,
        ifind_service=ifind_service,
        research_store=research_store,
        paper_store=paper_store,
        quant_store=quant_store,
        company_research_service=CompanyResearchService(
            repo, research_store, app_settings, quant_store=quant_store
        ),
        announcement_pipeline=AnnouncementPipeline(research_store, ifind_service),
        factor_snapshot_service=FactorSnapshotService(repo, quant_store),
        factor_lab_repository=factor_lab_repository,
        factor_lab_service=factor_lab_service,
        factor_evaluation_service=factor_evaluation_service,
        factor_backtest_service=factor_backtest_service,
        factor_release_service=FactorReleaseService(quant_store),
        evidence_acceptance_service=EvidenceAcceptanceService(
            research_store, ROOT / "data" / "acceptance" / "evidence_acceptance_v1.json"
        ),
        decision_case_service=DecisionCaseService(
            research_store, thesis_store, quant_store
        ),
        decision_outcome_service=DecisionOutcomeService(research_store),
        paper_trading_service=paper_trading_service,
        current_shadow_service=CurrentShadowService(
            app_settings, quant_store, research_store
        ),
        daily_batch_store=daily_batch_store,
        daily_batch_runner=DailyBatchRunner(daily_batch_store),
    )


container = create_app_container()
