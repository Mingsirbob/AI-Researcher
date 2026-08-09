from __future__ import annotations

import asyncio
import shutil
from contextlib import suppress
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from app.research.analysis import analyze_stock
from app.core.config import ROOT, settings
from app.market.repository import StockRepository, normalize_code
from app.core.frontend_cutover import build_frontend_status, production_frontend_response
from app.integrations.llm import (
    deterministic_report,
    generate_ai_report,
    generate_document_answer,
    llm_resilience_status,
)
from app.thesis.monitoring import build_monitor_evaluations, run_artifacts
from app.quant.factors import FactorSnapshotService
from app.quant.research import neutralize_factor, universe_membership
from app.data.ifind import IFindDataError
from app.research.announcements import AnnouncementPipeline
from app.research.financials import build_financial_change_template
from app.research.acceptance import EvidenceAcceptanceService
from app.decision.cases import DecisionCaseService, POLICY_VERSION
from app.decision.outcomes import DecisionOutcomeService
from app.paper.service import PAPER_BENCHMARKS, PaperTradingService
from app.paper.scheduler import PaperDailyScheduler
from app.market.updater import default_end_date
from app.quant.current_shadow_service import CurrentShadowService
from app.workflows.daily_batch import DailyBatchRunner, DailyBatchStore
from app.research.company import (
    CompanyResearchContextError,
    CompanyResearchNotFoundError,
    CompanyResearchRunError,
    CompanyResearchService,
)
from app.research.assessment import (
    RESEARCH_ASSESSMENT_POLICY_VERSION,
    RESEARCH_ASSESSMENT_SCHEMA_VERSION,
)
from app.research.store import ResearchStore
from app.core.runtime_events import RuntimeEventStore
from app.core.sqlite_store import SQLiteStore
from app.quant.store import QuantStore
from app.research.workflow import (
    empty_financial_template,
)
from app.schemas import (
    DocumentAssistantRequest,
    DecisionCaseCreate,
    DecisionCaseReview,
    DecisionOutcomeEvaluate,
    DecisionOutcomeBatchEvaluate,
    FactorNeutralizationRequest,
    PaperAccountCreate,
    PaperBenchmarkRefresh,
    PaperDailyBatchRequest,
    PaperDailyRunCreate,
    PaperOrderReview,
    PaperRealtimeRequest,
    PaperResearchBatchRequest,
    PaperSettleRequest,
    ClaimEvaluationConfirm,
    FactorSnapshotRequest,
    FactorDefinitionCreate,
    FinancialChangeTemplateRequest,
    ResearchRequest,
    ResearchRunRequest,
    ResearchCandidateCreate,
    ThesisCreate,
    ThesisMonitorBaselineRequest,
    ThesisMonitorCheckRequest,
    ThesisUpdate,
    StrategyDraftCreate,
    StrategyDraftUpdate,
    StrategyCodeUpdate,
    StrategyGenerateRequest,
)
from app.thesis.store import ThesisStore
from ..container import container


# Shared service instances used by the route handlers below.
repo = container.repo
thesis_store = container.thesis_store
ifind_service = container.ifind_service
research_store = container.research_store
paper_store = container.paper_store
quant_store = container.quant_store
strategy_service = container.strategy_service
company_research_service = container.company_research_service
announcement_pipeline = container.announcement_pipeline
factor_snapshot_service = container.factor_snapshot_service
simple_research_catalog = container.simple_research_catalog
evidence_acceptance_service = container.evidence_acceptance_service
decision_case_service = container.decision_case_service
decision_outcome_service = container.decision_outcome_service
paper_trading_service = container.paper_trading_service
current_shadow_service = container.current_shadow_service
daily_batch_store = container.daily_batch_store
daily_batch_runner = container.daily_batch_runner


def ensure_market_data(as_of: str) -> dict:
    target = date.fromisoformat(as_of)
    result = ifind_service.sync_daily_prices(target_date=target)
    if result["status"] not in {"success", "up_to_date"}:
        raise RuntimeError(
            f"日线补齐未完全通过：status={result['status']}, "
            f"failed={len(result['failed_codes'])}, quality={result['quality_issue_count']}"
        )
    master_refresh = research_store.bootstrap_securities(settings.stock_db)
    return {
        **result,
        "security_master_refresh": master_refresh,
        "latest_completed_trading_date": target.isoformat(),
        "calendar_source": "iFinD THS_Date_Query/SSE/dateType:0",
    }


def refresh_paper_benchmarks(
    account_id: str | None = None, as_of: str | None = None
) -> dict:
    sync_range = paper_trading_service.benchmark_sync_range(account_id, as_of)
    frame = ifind_service.get_daily_prices(
        sync_range["benchmark_codes"],
        date.fromisoformat(sync_range["start_date"]),
        date.fromisoformat(sync_range["end_date"]),
        adjustment="unadjusted",
        max_attempts=min(settings.ifind_max_attempts, 2),
    )
    sync = paper_trading_service.save_benchmark_prices(
        frame.to_dict(orient="records"),
        source="iFinD THS_HD",
    )
    comparison = paper_trading_service.dashboard(
        account_id, sync_range["end_date"]
    )["benchmark_comparison"]
    return {
        **sync,
        "start_date": sync_range["start_date"],
        "end_date": sync_range["end_date"],
        "benchmark_codes": PAPER_BENCHMARKS,
        "status": comparison["status"],
        "comparison": comparison,
    }


def public_decision_case(item: dict | None) -> dict | None:
    if item is None:
        return None
    result = dict(item)
    result["policy_current"] = result["policy_version"] == POLICY_VERSION
    if not result["policy_current"] and result["review_status"] == "pending":
        result["review_status"] = "policy_superseded"
    return result


def public_announcements(items: list[dict]) -> list[dict]:
    public = []
    for item in items:
        cleaned = dict(item)
        cleaned["has_source_url"] = bool(cleaned.pop("source_url", None))
        cleaned["has_error"] = bool(cleaned.pop("error", None))
        public.append(cleaned)
    return public


def public_ifind_context(context: dict) -> dict:
    cleaned = dict(context)
    cleaned["announcements"] = []
    for item in context.get("announcements", []):
        announcement = dict(item)
        announcement["has_source_url"] = bool(announcement.pop("url", None))
        cleaned["announcements"].append(announcement)
    return cleaned


def load_ifind_context(code: str, as_of: str, lookback_days: int = 365) -> dict:
    company_data = ifind_service.get_company_data(
        [code],
        as_of=date.fromisoformat(as_of),
        lookback_days=lookback_days,
    )
    profile = company_data["profiles"][0]
    return {
        "security": {"code": code, "name": profile.get("name")},
        "as_of": as_of,
        "lookback_start": (
            date.fromisoformat(as_of) - timedelta(days=lookback_days)
        ).isoformat(),
        "announcements": company_data["announcements"].get(code, [])[:20],
        "source": company_data["source"],
    }


@asynccontextmanager
async def lifespan(application: FastAPI):
    app_container = application.state.container
    sync_task = None
    scheduler_task = None
    if app_container.settings.ifind_credentials_configured:
        sync_task = asyncio.create_task(
            run_in_threadpool(app_container.ifind_service.sync_daily_prices)
        )
        application.state.market_sync_task = sync_task
        scheduler = PaperDailyScheduler(
            app_container.paper_trading_service,
            app_container.ifind_service,
        )
        application.state.paper_scheduler = scheduler
        scheduler_task = asyncio.create_task(_paper_scheduler_loop(scheduler))
        application.state.paper_scheduler_task = scheduler_task
    try:
        yield
    finally:
        if scheduler_task is not None:
            scheduler_task.cancel()
            with suppress(asyncio.CancelledError):
                await scheduler_task
        if sync_task is not None and not sync_task.done():
            try:
                await sync_task
            except Exception:
                pass
        await run_in_threadpool(app_container.close)


async def _scheduled_account_run(account: dict, as_of: str) -> dict:
    config = account["strategy"].get("config") or {}
    request = PaperDailyBatchRequest(
        as_of=as_of,
        account_id=account["account_id"],
        model_run_id=config.get("model_run_id"),
        top_n=int(config.get("top_n", 5)),
        hold_rank_buffer=int(config.get("hold_rank_buffer", 30)),
        target_gross_exposure=float(config.get("target_gross_exposure", 0.5)),
        max_position_weight=float(config.get("max_position_weight", 0.12)),
        max_industry_weight=float(config.get("max_industry_weight", 0.2)),
        max_pair_correlation=float(config.get("max_pair_correlation", 0.85)),
    )
    batch_config = request.model_dump(exclude={"as_of", "account_id"})
    batch, should_start = daily_batch_store.prepare(
        account_id=account["account_id"],
        as_of=as_of,
        config=batch_config,
    )
    if should_start:
        await run_daily_research_batch(
            batch["batch_id"], request, as_of, account["account_id"]
        )
        batch = daily_batch_store.get(batch["batch_id"])
    else:
        batch = daily_batch_store.get(batch["batch_id"])
    if not batch or batch["status"] != "completed":
        return {
            "status": "failed",
            "batch_id": batch["batch_id"] if batch else None,
            "error": (batch or {}).get("error") or "每日批次未完成",
        }
    order_step = next(
        item for item in batch["steps"] if item["step_name"] == "order_proposals"
    )
    run_id = order_step["result"]["run_id"]
    run = await run_in_threadpool(paper_trading_service.run, run_id)
    return {
        "status": "completed",
        "batch_id": batch["batch_id"],
        "run_id": run_id,
        "approval_mode": "manual",
        "proposed_count": sum(order["status"] == "proposed" for order in run["orders"]),
        "filled_count": 0,
        "skipped_count": 0,
    }


async def _paper_scheduler_loop(scheduler: PaperDailyScheduler) -> None:
    while True:
        try:
            await scheduler.run_once(
                datetime.now(ZoneInfo("Asia/Shanghai")),
                _scheduled_account_run,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            scheduler.last_result = {
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
                "retrying": True,
            }
        await asyncio.sleep(60)


frontend_dist = ROOT / "frontend" / "dist"


def build_analysis(code: str, as_of: str | None = None) -> dict:
    try:
        normalized = normalize_code(code)
        rows = repo.get_history(normalized, end=as_of, limit=1800)
        master = research_store.security(normalized)
        display_name = master.get("security_name") if master else None
        return analyze_stock(normalized, display_name or repo.display_name(normalized), rows)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def frontend_cutover_status() -> dict:
    return build_frontend_status(frontend_dist=frontend_dist)


def enrich_with_ifind(analysis: dict, context: dict) -> None:
    name = context.get("security", {}).get("name")
    if name:
        analysis["security"]["name"] = name
    analysis["ifind_context"] = public_ifind_context(context)
    for index, item in enumerate(context.get("announcements", [])[:5], start=1):
        evidence_id = f"ev-announcement-{index}"
        analysis["evidence"].append(
            {
                "id": evidence_id,
                "category": "external_document",
                "label": "公开公告",
                "value": item["title"],
                "as_of": item["published_at"] or item["date"],
                "source": f"ifind://report/{item['sequence']}",
                "method": "iFinD QuantAPI 公告查询；仅证明公告已发布，不代表已解析或支持任何投资结论",
            }
        )


async def hydrate_security_names(items: list[dict]) -> str | None:
    missing_codes = [
        item["security_code"]
        for item in items
        if item.get("security_name") in {None, item["security_code"]}
    ]
    if not missing_codes or not ifind_service.status()["configured"]:
        return None
    try:
        company_data = await run_in_threadpool(
            lambda: ifind_service.get_company_data(
                missing_codes,
                as_of=date.today(),
                include_announcements=False,
            )
        )
        for profile in company_data["profiles"]:
            if profile.get("name"):
                research_store.upsert_security_name(
                    profile["code"], profile["name"], profile["source"]
                )
        return None
    except IFindDataError as exc:
        return str(exc)


async def run_daily_research_batch(
    batch_id: str,
    request: PaperDailyBatchRequest,
    as_of: str,
    account_id: str,
) -> None:
    account = paper_trading_service.account(account_id)
    strategy = account["strategy"]
    workspace = container.runtime_root / "batches" / batch_id
    if workspace.exists():
        shutil.rmtree(workspace, ignore_errors=True)
    workspace.mkdir(parents=True, exist_ok=True)
    quant_store.clear_daily_runtime(as_of)
    async def market_data_step() -> dict:
        result = await run_in_threadpool(ensure_market_data, as_of)
        try:
            benchmark = await run_in_threadpool(
                refresh_paper_benchmarks, account_id, as_of
            )
            benchmark_result = {
                "status": benchmark["status"],
                "rows_written": benchmark["rows_written"],
                "benchmark_count": benchmark["benchmark_count"],
                "start_date": benchmark["start_date"],
                "end_date": benchmark["end_date"],
            }
        except Exception as exc:
            benchmark_result = {"status": "failed", "error": str(exc)}
        return {**result, "benchmark_refresh": benchmark_result}

    async def factor_snapshot_step() -> dict:
        snapshot = await run_in_threadpool(factor_snapshot_service.generate, as_of)
        if snapshot["status"] != "completed" or snapshot["as_of"] != as_of:
            raise RuntimeError("全市场因子快照未完成同日数据合同")
        return {
            "snapshot_id": snapshot["snapshot_id"],
            "as_of": snapshot["as_of"],
            "status": snapshot["status"],
            "total_securities": snapshot["total_securities"],
            "passed_securities": snapshot["passed_securities"],
            "excluded_securities": snapshot["excluded_securities"],
            "reused": snapshot.get("reused", False),
        }

    async def current_shadow_step() -> dict:
        if strategy.get("signal_source") == "multifactor_linear":
            return {"status": "skipped", "reason": "线性多因子策略直接使用因子快照"}
        snapshot = await run_in_threadpool(
            lambda: current_shadow_service.generate(
                as_of=date.fromisoformat(as_of),
                model_run_id=request.model_run_id,
                lookback_days=request.shadow_lookback_days,
                batch_size=request.shadow_batch_size,
                trust_pickle=True,
                workspace=workspace,
            )
        )
        if snapshot["status"] != "current_shadow_ready" or snapshot["as_of"] != as_of:
            raise RuntimeError("Current Shadow 未通过同日数据合同")
        return {
            "snapshot_id": snapshot["snapshot_id"],
            "model_run_id": snapshot["model_run_id"],
            "validation_id": snapshot["validation_id"],
            "as_of": snapshot["as_of"],
            "status": snapshot["status"],
            "universe_size": snapshot["universe_size"],
            "signal_count": snapshot["signal_count"],
            "coverage": snapshot["coverage"],
            "reused": snapshot.get("reused", False),
            "score_storage": "temporary_csv",
            "score_file": "lightgbm_scores.csv",
        }

    async def candidate_research_step() -> dict:
        if not strategy.get("research_enabled", True):
            return {"status": "skipped", "reason": "当前策略未启用个股研究过滤"}
        from app.api.routers.paper import build_paper_research_assessments

        result = await build_paper_research_assessments(PaperResearchBatchRequest(
            as_of=as_of,
            account_id=account_id,
            include_holdings=True,
            limit=request.top_n,
            hold_rank_buffer=request.hold_rank_buffer,
            lookback_days=request.research_lookback_days,
            financial_download_limit=request.financial_download_limit,
            announcement_download_limit=request.announcement_download_limit,
            depth=request.depth,
        ))
        if result["failed"]:
            failed = [item["security_code"] for item in result["results"] if item["status"] == "failed"]
            raise RuntimeError(f"候选研究失败：{', '.join(failed)}")
        return {
            "factor_snapshot_id": result["factor_snapshot_id"],
            "shadow_snapshot_id": result["shadow_snapshot_id"],
            "policy_version": result["policy_version"],
            "completed": result["completed"],
            "failed": result["failed"],
            "items": [
                {
                    "security_code": item["security_code"],
                    "security_name": item["security_name"],
                    "shadow_rank": item["shadow_rank"],
                    "status": item["status"],
                    "run_id": item.get("run_id"),
                    "signal": (item.get("assessment") or {}).get("signal"),
                    "assessment_id": (item.get("assessment") or {}).get("assessment_id"),
                }
                for item in result["results"]
            ],
        }

    async def holdings_review_step() -> dict:
        review = await run_in_threadpool(
            lambda: paper_trading_service.review_holdings(
                account_id=account_id,
                as_of=as_of,
            )
        )
        return {
            "account_id": review["account_id"],
            "as_of": review["as_of"],
            "factor_snapshot_id": review["factor_snapshot_id"],
            "shadow_snapshot_id": review["shadow_snapshot_id"],
            "position_count": review["position_count"],
            "counts": review["counts"],
            "items": [
                {
                    "security_code": item["security_code"],
                    "security_name": item["security_name"],
                    "quantity": item["position"]["quantity"],
                    "close": item["position"]["close"],
                    "market_value": item["position"]["market_value"],
                    "shadow_rank": item.get("cross_section_rank"),
                    "shadow_covered": item["shadow_covered"],
                    "risk_status": item["risk_status"],
                    "research_status": item["research_status"],
                    "review_action": item["review_action"],
                    "rejection_reasons": item["rejection_reasons"],
                    "research_assessment": item["research_assessment"],
                }
                for item in review["items"]
            ],
        }

    async def order_proposals_step() -> dict:
        prepared = daily_batch_store.get(batch_id)
        if prepared is None:
            raise RuntimeError("完整批次运行目录不存在")
        run = await run_in_threadpool(
            lambda: paper_trading_service.create_daily_run(
                as_of=as_of,
                account_id=account_id,
                top_n=request.top_n,
                hold_rank_buffer=request.hold_rank_buffer,
                target_gross_exposure=request.target_gross_exposure,
                max_position_weight=request.max_position_weight,
                max_industry_weight=request.max_industry_weight,
                max_pair_correlation=request.max_pair_correlation,
                run_key=prepared["run_key"],
            )
        )
        return {
            "run_id": run["run_id"],
            "as_of": run["as_of"],
            "status": run["status"],
            "strategy_version": run["strategy_version"],
            "order_count": len(run.get("orders", [])),
            "orders": run.get("orders", []),
            "reused": run.get("reused", False),
        }

    try:
        await daily_batch_runner.run(batch_id, {
            "market_data": market_data_step,
            "factor_snapshot": factor_snapshot_step,
            "current_shadow": current_shadow_step,
            "candidate_research": candidate_research_step,
            "holdings_review": holdings_review_step,
            "order_proposals": order_proposals_step,
        })
    finally:
        quant_store.clear_daily_runtime(as_of)
        (workspace / "lightgbm_scores.csv").unlink(missing_ok=True)
        shutil.rmtree(workspace, ignore_errors=True)


def _completed_run(run_id: str, security_code: str) -> dict:
    run = research_store.research_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="研究运行不存在")
    if run["security_code"] != security_code:
        raise HTTPException(status_code=422, detail="研究运行与 Thesis 证券不一致")
    if run["status"] not in {"completed", "completed_with_gaps"}:
        raise HTTPException(status_code=422, detail="研究运行尚未成功完成")
    return run
