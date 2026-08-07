from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ResearchRequest(BaseModel):
    code: str
    as_of: str | None = None
    depth: Literal["quick", "deep"] = "quick"


class ResearchRunRequest(BaseModel):
    code: str
    as_of: str | None = None
    depth: Literal["quick", "deep"] = "quick"
    factor_snapshot_id: str | None = None


class DocumentAssistantRequest(BaseModel):
    code: str
    question: str = Field(min_length=2, max_length=500)
    as_of: str | None = None
    scope: Literal["all", "financial_reports", "announcements"] = "all"


class FinancialChangeTemplateRequest(BaseModel):
    code: str
    as_of: str | None = None


class ThesisCreate(BaseModel):
    code: str
    title: str = Field(min_length=2, max_length=120)
    core_claim: str = Field(min_length=4, max_length=2000)
    horizon: str = Field(default="6-12个月", max_length=50)
    status: Literal["观察", "验证中", "基本成立", "证据减弱", "已经证伪", "研究终止"] = "观察"
    invalidating_conditions: list[str] = Field(default_factory=list, max_length=12)


class ThesisUpdate(BaseModel):
    status: Literal["观察", "验证中", "基本成立", "证据减弱", "已经证伪", "研究终止"]


class ThesisMonitorBaselineRequest(BaseModel):
    run_id: str | None = None


class ThesisMonitorCheckRequest(BaseModel):
    current_run_id: str | None = None


class ClaimEvaluationConfirm(BaseModel):
    verdict: Literal["增强", "维持", "减弱", "证伪", "无法判断"]


class FactorSnapshotRequest(BaseModel):
    as_of: str | None = None


class FactorDefinitionCreate(BaseModel):
    factor_id: str = Field(min_length=2, max_length=80, pattern=r"^[a-z0-9_]+$")
    name: str = Field(min_length=2, max_length=80)
    description: str = Field(min_length=4, max_length=500)
    template_id: str = Field(min_length=2, max_length=80)
    window: int = Field(ge=1, le=500)
    direction: Literal["positive", "negative"] | None = None
    owner: str = Field(default="human", min_length=1, max_length=80)


class FactorLifecycleChange(BaseModel):
    to_status: Literal["draft", "testing", "shadow", "approved", "deprecated"]
    reviewer: str = Field(default="human", min_length=1, max_length=80)
    note: str = Field(min_length=2, max_length=500)


class FactorLabSnapshotRequest(BaseModel):
    as_of: str | None = None


class FactorEvaluationRequest(BaseModel):
    start_date: str = "2021-01-01"
    end_date: str | None = None
    rebalance_step: int = Field(default=20, ge=1, le=120)
    horizons: list[int] = Field(default_factory=lambda: [1, 5, 20], min_length=1, max_length=8)
    layer_count: int = Field(default=5, ge=3, le=10)


class FactorBacktestRequest(BaseModel):
    evaluation_id: str | None = None
    factor_id: str
    start_date: str | None = None
    end_date: str | None = None
    top_n: int = Field(default=30, ge=5, le=100)
    rebalance_step: int = Field(default=20, ge=5, le=120)
    initial_capital: float = Field(default=1_000_000, gt=0, le=1_000_000_000)
    commission_rate: float = Field(default=0.0003, ge=0, le=0.02)
    stamp_duty_rate: float = Field(default=0.0005, ge=0, le=0.02)
    slippage_rate: float = Field(default=0.001, ge=0, le=0.02)


class FactorReleaseCreate(BaseModel):
    factor_id: str = Field(min_length=2, max_length=80)
    factor_version: int = Field(ge=1)
    evaluation_id: str = Field(min_length=8, max_length=128)
    backtest_id: str = Field(min_length=8, max_length=128)
    limitations_acknowledged: bool = False
    created_by: str = Field(default="human", min_length=1, max_length=80)


class FactorReleaseDecision(BaseModel):
    reviewer: str = Field(default="human", min_length=1, max_length=80)
    note: str = Field(min_length=2, max_length=1000)


class ResearchCandidateCreate(BaseModel):
    code: str
    snapshot_id: str
    note: str = Field(default="", max_length=500)


class DecisionCaseCreate(BaseModel):
    code: str
    as_of: str | None = None
    decision_horizon: Literal["20d", "60d", "120d", "250d"] = "60d"
    benchmark_code: Literal["000300.SH", "000905.SH", "000852.SH"] = "000300.SH"
    research_run_id: str | None = None
    thesis_id: str | None = None
    shadow_snapshot_id: str | None = None


class DecisionCaseReview(BaseModel):
    decision: Literal["approve_for_tracking", "return_for_research", "reject"]
    reviewer: str = Field(default="human", min_length=1, max_length=80)
    note: str = Field(min_length=2, max_length=1000)


class DecisionOutcomeEvaluate(BaseModel):
    end_date: str | None = None


class DecisionOutcomeBatchEvaluate(BaseModel):
    case_ids: list[str] = Field(min_length=1, max_length=100)
    end_date: str | None = None


class FactorNeutralizationRequest(BaseModel):
    rows: list[dict] = Field(min_length=1, max_length=5000)
    factor_key: str = Field(min_length=1, max_length=80)
    industry_key: str = Field(default="industry_l1", min_length=1, max_length=80)
    market_cap_key: str = Field(default="market_cap", min_length=1, max_length=80)


class PaperAccountCreate(BaseModel):
    name: str = Field(default="每日模拟组合", min_length=1, max_length=80)
    initial_cash: float = Field(default=1_000_000, gt=0, le=1_000_000_000)
    benchmark_code: Literal["000300.SH", "000905.SH", "000852.SH"] = "000300.SH"
    strategy_id: str = Field(default="lightgbm_shadow_v1", min_length=2, max_length=128)
    strategy_version_id: str | None = Field(default=None, min_length=8, max_length=128)


class StrategyDraftCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    description: str = Field(default="", max_length=1000)
    template_id: str = Field(min_length=2, max_length=80)


class StrategyDraftUpdate(BaseModel):
    definition: dict[str, Any]


class StrategyCodeUpdate(BaseModel):
    source_code: str = Field(min_length=1, max_length=50_000)


class StrategyGenerateRequest(BaseModel):
    requirement: str = Field(min_length=8, max_length=4000)
    name: str = Field(min_length=2, max_length=120)
    pool_id: str = Field(default="csi300", min_length=2, max_length=80)
    filter_pipeline_id: Literal["factor_quality_passed"] = "factor_quality_passed"
    params: dict[str, Any] = Field(default_factory=dict)


class PaperStrategyDeploymentCreate(BaseModel):
    strategy_version_id: str = Field(min_length=8, max_length=128)


class PaperBenchmarkRefresh(BaseModel):
    account_id: str | None = None
    as_of: str | None = None


class PaperDailyRunCreate(BaseModel):
    as_of: str | None = None
    account_id: str | None = None
    top_n: int = Field(default=5, ge=1, le=20)
    hold_rank_buffer: int = Field(default=30, ge=5, le=200)
    target_gross_exposure: float | None = Field(default=None, gt=0, le=1)
    max_position_weight: float | None = Field(default=None, gt=0, le=0.25)
    max_industry_weight: float | None = Field(default=None, gt=0, le=0.50)
    max_pair_correlation: float | None = Field(default=None, ge=0, le=1)


class PaperOrderReview(BaseModel):
    reviewer: str = Field(default="human", min_length=1, max_length=80)
    note: str = Field(default="人工确认模拟订单", min_length=2, max_length=500)


class PaperSettleRequest(BaseModel):
    execution_date: str | None = None
    account_id: str | None = None


class PaperRealtimeRequest(BaseModel):
    account_id: str | None = None


class PaperResearchBatchRequest(BaseModel):
    as_of: str | None = None
    account_id: str | None = None
    include_holdings: bool = False
    limit: int = Field(default=5, ge=1, le=5)
    hold_rank_buffer: int = Field(default=30, ge=5, le=100)
    lookback_days: int = Field(default=730, ge=30, le=3650)
    financial_download_limit: int = Field(default=2, ge=0, le=10)
    announcement_download_limit: int = Field(default=3, ge=0, le=10)
    depth: Literal["quick", "deep"] = "quick"


class PaperDailyBatchRequest(BaseModel):
    as_of: str | None = None
    account_id: str | None = None
    model_run_id: str | None = None
    shadow_lookback_days: int = Field(default=240, ge=120, le=730)
    shadow_batch_size: int = Field(default=20, ge=1, le=100)
    top_n: int = Field(default=5, ge=1, le=5)
    hold_rank_buffer: int = Field(default=30, ge=5, le=100)
    research_lookback_days: int = Field(default=730, ge=30, le=3650)
    financial_download_limit: int = Field(default=2, ge=0, le=10)
    announcement_download_limit: int = Field(default=3, ge=0, le=10)
    depth: Literal["quick", "deep"] = "quick"
    target_gross_exposure: float | None = Field(default=None, gt=0, le=1)
    max_position_weight: float | None = Field(default=None, gt=0, le=0.25)
    max_industry_weight: float | None = Field(default=None, gt=0, le=0.50)
    max_pair_correlation: float | None = Field(default=None, ge=0, le=1)


class AIClaim(BaseModel):
    statement: str
    claim_type: Literal["fact", "inference"]
    confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str]
    counter_evidence: list[str] = Field(default_factory=list)
    invalidating_conditions: list[str] = Field(default_factory=list)


class AIReport(BaseModel):
    executive_summary: str
    observed_changes: list[str]
    claims: list[AIClaim]
    counter_view: list[str]
    uncertainties: list[str]
    next_checks: list[str]


class AssessmentEvidenceItem(BaseModel):
    statement: str = Field(min_length=2, max_length=1000)
    evidence_ids: list[str] = Field(min_length=1)


class AssessmentNegativeItem(AssessmentEvidenceItem):
    category: Literal[
        "earnings_deterioration", "cash_flow", "leverage", "governance",
        "regulatory_investigation", "fraud_or_restatement", "major_litigation",
        "default_or_insolvency", "delisting_or_listing_status", "operation_disruption",
        "shareholder_reduction", "other",
    ]
    severity: Literal["low", "medium", "high", "critical"]


class AssessmentCatalystItem(AssessmentEvidenceItem):
    category: Literal[
        "earnings_improvement", "contract_or_order", "capacity_or_product",
        "regulatory_approval", "buyback_or_increase", "dividend", "restructuring", "other",
    ]
    status: Literal["confirmed", "potential", "expired"]


class ResearchAssessmentDraft(BaseModel):
    fundamental_outlook: Literal["positive", "neutral", "negative", "insufficient"]
    evidence_confidence: float = Field(ge=0, le=1)
    fundamental_evidence: list[AssessmentEvidenceItem] = Field(default_factory=list)
    material_negatives: list[AssessmentNegativeItem] = Field(default_factory=list)
    catalysts: list[AssessmentCatalystItem] = Field(default_factory=list)
    invalidating_conditions: list[str] = Field(default_factory=list, max_length=12)
    limitations: list[str] = Field(default_factory=list, max_length=12)


class DocumentAnswerClaim(BaseModel):
    statement: str
    evidence_ids: list[str]


class DocumentAnswer(BaseModel):
    status: Literal["answered", "insufficient_evidence"]
    answer: str
    claims: list[DocumentAnswerClaim] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    follow_up_questions: list[str] = Field(default_factory=list)


class FinancialChangeSection(BaseModel):
    key: Literal[
        "operating_scale",
        "profitability",
        "cash_and_capex",
        "balance_and_working_capital",
        "shareholder_returns",
    ]
    label: str
    status: Literal["supported", "not_covered"]
    direction: Literal["improved", "weakened", "mixed", "stable", "unknown"]
    summary: str
    evidence_ids: list[str] = Field(default_factory=list)


class FinancialChangeTemplate(BaseModel):
    status: Literal["answered", "insufficient_evidence"]
    period_summary: str
    overall_assessment: str
    sections: list[FinancialChangeSection]
    limitations: list[str] = Field(default_factory=list)
    next_checks: list[str] = Field(default_factory=list)
