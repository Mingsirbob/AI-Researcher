from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from .analysis import analyze_stock
from .config import ROOT, settings
from .data_access import StockRepository, normalize_code
from .llm import (
    deterministic_report,
    generate_ai_report,
    generate_document_answer,
    llm_resilience_status,
)
from .monitoring import build_monitor_evaluations, run_artifacts
from .quant import FactorSnapshotService
from .ifind import IFindError, IFindService
from .announcement_pipeline import AnnouncementPipeline
from .financial_extraction import build_financial_change_template
from .evidence_acceptance import EvidenceAcceptanceService
from .decision_cases import DecisionCaseService, POLICY_VERSION
from .decision_outcomes import DecisionOutcomeService
from .paper_trading import PAPER_BENCHMARKS, PaperTradingService
from .current_shadow_service import CurrentShadowService
from .daily_batch import DailyBatchRunner, DailyBatchStore
from .company_research import (
    CompanyResearchContextError,
    CompanyResearchNotFoundError,
    CompanyResearchRunError,
    CompanyResearchService,
)
from .research_assessment import (
    RESEARCH_ASSESSMENT_POLICY_VERSION,
    RESEARCH_ASSESSMENT_SCHEMA_VERSION,
)
from .updater import IFindDailyClient, StockDataUpdater, default_end_date
from .research_store import ResearchStore
from .research_workflow import (
    empty_financial_template,
)
from .schemas import (
    DocumentAssistantRequest,
    DecisionCaseCreate,
    DecisionCaseReview,
    DecisionOutcomeEvaluate,
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
    FinancialChangeTemplateRequest,
    ResearchRequest,
    ResearchRunRequest,
    ResearchCandidateCreate,
    ThesisCreate,
    ThesisMonitorBaselineRequest,
    ThesisMonitorCheckRequest,
    ThesisUpdate,
)
from .state import ThesisStore


repo = StockRepository(settings.stock_db)
thesis_store = ThesisStore(settings.state_db)
ifind_service = IFindService(settings)
research_store = ResearchStore(settings.state_db, settings.document_root)
company_research_service = CompanyResearchService(repo, research_store, settings)
announcement_pipeline = AnnouncementPipeline(research_store, ifind_service)
factor_snapshot_service = FactorSnapshotService(repo, research_store)
evidence_acceptance_service = EvidenceAcceptanceService(
    research_store, ROOT / "data" / "acceptance" / "evidence_acceptance_v1.json"
)
decision_case_service = DecisionCaseService(research_store, thesis_store)
decision_outcome_service = DecisionOutcomeService(research_store)
paper_trading_service = PaperTradingService(repo, research_store, ifind_service)
current_shadow_service = CurrentShadowService(settings, research_store)
daily_batch_store = DailyBatchStore(research_store)
daily_batch_runner = DailyBatchRunner(daily_batch_store)


def ensure_market_data(as_of: str) -> dict:
    target = date.fromisoformat(as_of)
    client = IFindDailyClient(settings, adjustment="unadjusted")
    updater = StockDataUpdater(settings.stock_db, client.fetch)
    client.login()
    try:
        trading_dates = client.fetch_trading_dates(target - timedelta(days=31), target)
        if not trading_dates or trading_dates[-1] != target:
            latest = trading_dates[-1].isoformat() if trading_dates else "none"
            raise ValueError(f"{as_of} 不是已确认交易日；最近交易日为 {latest}")
        result = updater.update(
            end_date=target,
            batch_size=50,
            max_retries=settings.ifind_max_attempts,
            retry_delay=settings.ifind_backoff_seconds,
        )
    finally:
        client.logout()
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
    client = IFindDailyClient(settings, adjustment="unadjusted")
    client.login()
    try:
        frame = client.fetch(
            sync_range["benchmark_codes"],
            date.fromisoformat(sync_range["start_date"]),
            date.fromisoformat(sync_range["end_date"]),
            max_attempts=settings.ifind_max_attempts,
        )
    finally:
        client.logout()
    sync = paper_trading_service.save_benchmark_prices(
        sync_range["account_id"],
        frame.to_dict(orient="records"),
        source="iFinD THS_HD",
    )
    comparison = paper_trading_service.dashboard(
        sync_range["account_id"], sync_range["end_date"]
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


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await run_in_threadpool(ifind_service.logout)


app = FastAPI(title="Evidence AI Research", version="0.9.0", lifespan=lifespan)
static_dir = ROOT / "app" / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


def build_analysis(code: str, as_of: str | None = None) -> dict:
    try:
        normalized = normalize_code(code)
        rows = repo.get_history(normalized, end=as_of, limit=1800)
        master = research_store.security(normalized)
        display_name = master.get("security_name") if master else None
        return analyze_stock(normalized, display_name or repo.display_name(normalized), rows)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(static_dir / "index.html")


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "securities": repo.security_count,
        "llm_configured": settings.llm_configured,
        "llm": {
            "configured": settings.llm_configured,
            "resilience": llm_resilience_status(settings),
        },
        "ifind": ifind_service.status(),
        "research_data": research_store.stats(),
        "database": settings.stock_db.name,
    }


@app.get("/api/securities")
def securities(q: str = "", limit: int = Query(12, ge=1, le=50)) -> dict:
    items = research_store.list_securities(q, limit)
    return {"items": items or repo.list_securities(q, limit), "total_covered": repo.security_count}


@app.get("/api/stocks/{code}/analysis")
def stock_analysis(code: str, as_of: str | None = None) -> dict:
    return build_analysis(code, as_of)


@app.get("/api/ifind/status")
def ifind_status() -> dict:
    return ifind_service.status()


@app.post("/api/evidence-acceptance-runs")
async def run_evidence_acceptance() -> dict:
    return await run_in_threadpool(evidence_acceptance_service.run)


@app.get("/api/evidence-acceptance-runs/latest")
def latest_evidence_acceptance_run() -> dict:
    return {"item": research_store.latest_evidence_acceptance_run()}


@app.get("/api/evidence-acceptance-runs")
def evidence_acceptance_runs(limit: int = Query(20, ge=1, le=100)) -> dict:
    return {"items": research_store.list_evidence_acceptance_runs(limit)}


@app.get("/api/evidence-acceptance-runs/{run_id}")
def evidence_acceptance_run(run_id: str) -> dict:
    item = research_store.evidence_acceptance_run(run_id)
    if item is None:
        raise HTTPException(status_code=404, detail="证据验收记录不存在")
    return item


@app.get("/api/stocks/{code}/context")
async def stock_context(code: str, as_of: str | None = None) -> dict:
    analysis = build_analysis(code, as_of)
    try:
        context = await run_in_threadpool(
            ifind_service.get_context,
            analysis["security"]["code"],
            analysis["as_of"],
        )
        if context.get("security", {}).get("name"):
            research_store.upsert_security_name(
                analysis["security"]["code"], context["security"]["name"]
            )
        context = public_ifind_context(context)
        context["persisted_announcements"] = public_announcements(
            research_store.list_announcements(
                analysis["security"]["code"], as_of=analysis["as_of"], limit=20
            )
        )
        return context
    except IFindError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/research")
async def research(request: ResearchRequest) -> dict:
    analysis = build_analysis(request.code, request.as_of)
    if ifind_service.status()["configured"]:
        try:
            context = await run_in_threadpool(
                ifind_service.get_context,
                analysis["security"]["code"],
                analysis["as_of"],
            )
            enrich_with_ifind(analysis, context)
        except IFindError as exc:
            analysis["uncertainties"].append(f"iFinD 补充数据不可用：{exc}")
    document_evidence = research_store.evidence_for_security(
        analysis["security"]["code"], as_of=analysis["as_of"], limit=5
    )
    if document_evidence:
        analysis["evidence"] = [
            item for item in analysis["evidence"] if not item["id"].startswith("ev-announcement-")
        ] + document_evidence
        analysis["uncertainties"] = [
            item
            for item in analysis["uncertainties"]
            if "当前结论不包含财务、公告" not in item
        ]
        analysis["uncertainties"].append(
            "公告证据仅覆盖已持久化并成功提取文本的文件；未下载或需要 OCR 的公告不在内容结论范围内。"
        )
    fallback = deterministic_report(analysis)
    try:
        report, meta = await generate_ai_report(analysis, settings, request.depth)
        return {"mode": "ai", "report": report, "meta": meta, "analysis": analysis}
    except Exception as exc:
        return {
            "mode": "deterministic_fallback",
            "report": fallback,
            "meta": {"warning": str(exc)},
            "analysis": analysis,
        }


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


@app.get("/api/stocks/{code}/announcements")
def persisted_announcements(
    code: str,
    as_of: str | None = None,
    limit: int = Query(20, ge=1, le=100),
) -> dict:
    normalized = normalize_code(code)
    return {
        "security": research_store.security(normalized),
        "items": public_announcements(
            research_store.list_announcements(normalized, as_of=as_of, limit=limit)
        ),
    }


@app.get("/api/stocks/{code}/announcement-evidence")
def announcement_evidence(
    code: str,
    q: str = "",
    as_of: str | None = None,
    limit: int = Query(5, ge=1, le=20),
) -> dict:
    normalized = normalize_code(code)
    return {
        "items": research_store.evidence_for_security(
            normalized, as_of=as_of, query=q, limit=limit
        )
    }


@app.post("/api/document-assistant")
async def document_assistant(request: DocumentAssistantRequest) -> dict:
    analysis = build_analysis(request.code, request.as_of)
    security = {
        "code": analysis["security"]["code"],
        "name": analysis["security"]["name"],
    }
    evidence = research_store.assistant_evidence(
        security["code"],
        request.question,
        as_of=analysis["as_of"],
        scope=request.scope,
        limit=8,
    )
    if not evidence:
        mode = "insufficient_evidence"
        answer = {
            "status": "insufficient_evidence",
            "answer": "当前已归档并成功解析的公告中，没有检索到足以回答该问题的原文证据。",
            "claims": [],
            "limitations": ["未归档、解析失败或需要 OCR 的公告不在本次检索范围内。"],
            "follow_up_questions": [],
        }
        meta = {}
    else:
        try:
            answer, meta = await generate_document_answer(
                security=security,
                question=request.question,
                as_of=analysis["as_of"],
                evidence=evidence,
                settings=settings,
            )
            mode = "ai"
        except Exception as exc:
            mode = "evidence_only"
            meta = {}
            answer = {
                "status": "insufficient_evidence",
                "answer": "已找到相关原文，但模型当前不可用或回答未通过引用校验，因此没有生成内容结论。",
                "claims": [],
                "limitations": [str(exc)],
                "follow_up_questions": [],
            }
    interaction = research_store.save_interaction(
        code=security["code"],
        interaction_type="document_qa",
        question=request.question,
        scope=request.scope,
        as_of=analysis["as_of"],
        mode=mode,
        status=answer["status"],
        result=answer,
        evidence=evidence,
        meta=meta,
    )
    return {
        "mode": mode,
        "answer": answer,
        "evidence": evidence,
        "security": security,
        "as_of": analysis["as_of"],
        "meta": meta,
        **interaction,
    }


@app.post("/api/financial-change-template")
async def financial_change_template(request: FinancialChangeTemplateRequest) -> dict:
    analysis = build_analysis(request.code, request.as_of)
    security = {
        "code": analysis["security"]["code"],
        "name": analysis["security"]["name"],
    }
    extraction = research_store.refresh_financial_facts(security["code"], analysis["as_of"])
    fact_evidence = research_store.financial_fact_evidence(security["code"], analysis["as_of"])
    if fact_evidence:
        mode = "deterministic"
        template = build_financial_change_template(fact_evidence)
        evidence = fact_evidence
        meta = {
            "provider": "deterministic_financial_extraction",
            "extraction": extraction,
        }
    else:
        evidence = research_store.financial_template_evidence(
            security["code"], as_of=analysis["as_of"], limit=16
        )
        mode = "insufficient_evidence"
        template = empty_financial_template(
            "当前没有通过严格规则提取出可比较的财务数字。",
            (
                "请先归档财报；若已归档，则当前文档可能缺少可识别单位、同行双值，"
                "或包含调整前后多列口径，需要人工核验。"
            ),
        )
        meta = {"provider": "deterministic_financial_extraction", "extraction": extraction}
    interaction = research_store.save_interaction(
        code=security["code"],
        interaction_type="financial_change_template",
        question="财报变化模板",
        scope="financial_reports",
        as_of=analysis["as_of"],
        mode=mode,
        status=template["status"],
        result=template,
        evidence=evidence,
        meta=meta,
    )
    return {
        "mode": mode,
        "template": template,
        "evidence": evidence,
        "security": security,
        "as_of": analysis["as_of"],
        "meta": meta,
        "financial_facts": [item["financial_fact"] for item in fact_evidence],
        **interaction,
    }


@app.post("/api/research-runs", status_code=201)
async def create_research_run(request: ResearchRunRequest) -> dict:
    try:
        return await company_research_service.run(
            code=request.code,
            as_of=request.as_of,
            depth=request.depth,
            factor_snapshot_id=request.factor_snapshot_id,
        )
    except CompanyResearchNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CompanyResearchContextError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except CompanyResearchRunError as exc:
        raise HTTPException(
            status_code=500,
            detail={"message": "统一研究任务执行失败", "run_id": exc.run_id, "error": str(exc)},
        ) from exc


@app.get("/api/stocks/{code}/research-runs")
def list_research_runs(code: str, limit: int = Query(20, ge=1, le=100)) -> dict:
    normalized = normalize_code(code)
    return {"items": research_store.list_research_runs(normalized, limit)}


@app.get("/api/research-runs/{run_id}")
def research_run(run_id: str) -> dict:
    item = research_store.research_run(run_id)
    if item is None:
        raise HTTPException(status_code=404, detail="研究任务不存在")
    return item


@app.get("/api/stocks/{code}/research-assessment")
def latest_research_assessment(code: str, as_of: str | None = None) -> dict:
    item = research_store.latest_research_assessment(normalize_code(code), as_of)
    if item is None:
        raise HTTPException(status_code=404, detail="该证券尚无可用 ResearchAssessment")
    return item


@app.post("/api/factor-snapshots")
async def generate_factor_snapshot(request: FactorSnapshotRequest) -> dict:
    as_of = request.as_of or research_store.market_data_end()
    if as_of is None:
        raise HTTPException(status_code=409, detail="本地行情库没有可用截止日")
    try:
        return await run_in_threadpool(factor_snapshot_service.generate, as_of)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"全市场因子快照生成失败：{exc}") from exc


@app.get("/api/factor-snapshots/latest")
def latest_factor_snapshot() -> dict:
    snapshot = research_store.latest_factor_snapshot()
    if snapshot is None:
        raise HTTPException(status_code=404, detail="尚未生成全市场因子快照")
    return snapshot


@app.get("/api/factor-snapshots/{snapshot_id}/securities")
def factor_snapshot_securities(
    snapshot_id: str,
    quality_status: str = Query("passed", pattern="^(passed|excluded|all)$"),
    q: str = "",
    exchange: str = Query("all", pattern="^(all|SZ|SH|BJ)$"),
    sort: str = Query(
        "return_60d",
        pattern="^(security_code|return_20d|return_60d|volatility_60d|avg_traded_value_20d|max_drawdown_250d|range_position_52w)$",
    ),
    direction: str = Query("desc", pattern="^(asc|desc)$"),
    min_return_20d: float | None = None,
    min_return_60d: float | None = None,
    max_volatility_60d: float | None = None,
    min_avg_traded_value_20d: float | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    snapshot = research_store.factor_snapshot(snapshot_id)
    if snapshot is None or snapshot["status"] != "completed":
        raise HTTPException(status_code=404, detail="因子快照不存在或尚未完成")
    result = research_store.list_factor_rows(
        snapshot_id,
        quality_status=quality_status,
        query=q,
        exchange=exchange,
        sort=sort,
        direction=direction,
        min_return_20d=min_return_20d,
        min_return_60d=min_return_60d,
        max_volatility_60d=max_volatility_60d,
        min_avg_traded_value_20d=min_avg_traded_value_20d,
        limit=limit,
        offset=offset,
    )
    result["snapshot"] = snapshot
    return result


@app.get("/api/model-runs/latest")
def latest_model_run() -> dict:
    return {"item": research_store.latest_model_run()}


@app.get("/api/model-runs/{model_run_id}")
def model_run(model_run_id: str) -> dict:
    item = research_store.model_run(model_run_id)
    if item is None:
        raise HTTPException(status_code=404, detail="模型运行不存在")
    return item


@app.get("/api/model-runs/{model_run_id}/signals")
def model_run_signals(
    model_run_id: str,
    as_of: date | None = None,
    q: str = "",
    sort: str = Query("rank", pattern="^(rank|score|label|security_code)$"),
    direction: str = Query("asc", pattern="^(asc|desc)$"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    try:
        return research_store.list_shadow_signals(
            model_run_id,
            as_of=as_of.isoformat() if as_of else None,
            query=q,
            sort=sort,
            direction=direction,
            limit=limit,
            offset=offset,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/model-runs/{model_run_id}/validation")
def model_run_validation(model_run_id: str) -> dict:
    if research_store.model_run(model_run_id) is None:
        raise HTTPException(status_code=404, detail="模型运行不存在")
    return {"item": research_store.latest_model_validation(model_run_id)}


@app.get("/api/current-shadow/latest")
def latest_current_shadow() -> dict:
    return {"item": research_store.latest_current_shadow()}


@app.get("/api/current-shadow/{snapshot_id}/signals")
def current_shadow_signals(
    snapshot_id: str,
    q: str = "",
    sort: str = Query("rank", pattern="^(rank|score|security_code)$"),
    direction: str = Query("asc", pattern="^(asc|desc)$"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    try:
        return research_store.list_current_shadow_signals(
            snapshot_id,
            query=q,
            sort=sort,
            direction=direction,
            limit=limit,
            offset=offset,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


async def hydrate_security_names(items: list[dict]) -> str | None:
    missing_codes = [
        item["security_code"]
        for item in items
        if item.get("security_name") in {None, item["security_code"]}
    ]
    if not missing_codes or not ifind_service.status()["configured"]:
        return None
    try:
        profiles = await run_in_threadpool(
            ifind_service.get_security_profiles, missing_codes
        )
        for profile in profiles:
            if profile.get("name"):
                research_store.upsert_security_name(
                    profile["code"], profile["name"], profile["source"]
                )
        return None
    except IFindError as exc:
        return str(exc)


@app.get("/api/research-candidates")
async def research_candidates() -> dict:
    items = research_store.list_research_candidates()
    warning = await hydrate_security_names(items)
    if any(item.get("security_name") in {None, item["security_code"]} for item in items):
        items = research_store.list_research_candidates()
    return {"items": items, "name_sync_warning": warning}


@app.post("/api/research-candidates", status_code=201)
async def add_research_candidate(request: ResearchCandidateCreate) -> dict:
    try:
        candidate = research_store.add_research_candidate(
            request.code, request.snapshot_id, request.note
        )
        await hydrate_security_names([candidate])
        return next(
            item
            for item in research_store.list_research_candidates()
            if item["security_code"] == candidate["security_code"]
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.delete("/api/research-candidates/{code}", status_code=204)
def remove_research_candidate(code: str) -> None:
    if not research_store.remove_research_candidate(code):
        raise HTTPException(status_code=404, detail="候选证券不存在")


@app.post("/api/decision-cases", status_code=201)
def create_decision_case(request: DecisionCaseCreate) -> dict:
    as_of = request.as_of or research_store.market_data_end()
    if as_of is None:
        raise HTTPException(status_code=409, detail="本地行情库没有可用截止日")
    try:
        return decision_case_service.create(
            code=request.code,
            as_of=as_of,
            decision_horizon=request.decision_horizon,
            benchmark_code=request.benchmark_code,
            research_run_id=request.research_run_id,
            thesis_id=request.thesis_id,
            shadow_snapshot_id=request.shadow_snapshot_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/decision-cases")
def list_decision_cases(
    code: str | None = None,
    rule_status: str | None = Query(
        None,
        pattern="^(excluded|insufficient_evidence|watch|research_required|eligible_for_review)$",
    ),
    review_status: str | None = Query(
        None,
        pattern="^(pending|approve_for_tracking|return_for_research|reject)$",
    ),
    limit: int = Query(100, ge=1, le=100),
) -> dict:
    items = research_store.list_decision_cases(
            code=code,
            rule_status=rule_status,
            review_status=review_status,
            limit=limit,
        )
    return {"items": [public_decision_case(item) for item in items]}


@app.get("/api/decision-cases/{case_id}")
def decision_case(case_id: str) -> dict:
    item = research_store.decision_case(case_id)
    if item is None:
        raise HTTPException(status_code=404, detail="决策案例不存在")
    return public_decision_case(item)


@app.post("/api/decision-cases/{case_id}/reviews", status_code=201)
def review_decision_case(case_id: str, request: DecisionCaseReview) -> dict:
    try:
        return decision_case_service.review(
            case_id,
            decision=request.decision,
            reviewer=request.reviewer,
            note=request.note,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="决策案例不存在") from exc
    except ValueError as exc:
        status_code = 409 if "已经完成审批" in str(exc) else 422
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@app.get("/api/decision-outcomes/attribution")
def decision_outcome_attribution() -> dict:
    return decision_outcome_service.attribution()


@app.get("/api/decision-outcomes")
def list_decision_outcomes(
    status: str | None = Query(None, pattern="^(pending|completed|data_rejected)$"),
    limit: int = Query(100, ge=1, le=500),
) -> dict:
    return {"items": research_store.list_decision_outcomes(status=status, limit=limit)}


@app.get("/api/decision-outcomes/{outcome_id}")
def decision_outcome(outcome_id: str) -> dict:
    item = research_store.decision_outcome(outcome_id)
    if item is None:
        raise HTTPException(status_code=404, detail="决策结果不存在")
    return item


@app.get("/api/decision-cases/{case_id}/outcome")
def decision_case_outcome(case_id: str) -> dict:
    if research_store.decision_case(case_id) is None:
        raise HTTPException(status_code=404, detail="决策案例不存在")
    return {"item": research_store.decision_case_outcome(case_id)}


@app.post("/api/decision-cases/{case_id}/outcome", status_code=201)
def evaluate_decision_case(case_id: str, request: DecisionOutcomeEvaluate) -> dict:
    case = research_store.decision_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="决策案例不存在")
    try:
        end = date.fromisoformat(request.end_date) if request.end_date else default_end_date()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="评价截止日必须是 YYYY-MM-DD") from exc
    start = date.fromisoformat(case["as_of"])
    if end < start:
        raise HTTPException(status_code=422, detail="评价截止日不得早于案例截止日")
    client = IFindDailyClient(settings, adjustment="forward")
    try:
        client.login()
        frame = client.fetch(
            [case["security_code"], case["benchmark_code"]], start, end,
            max_attempts=settings.ifind_max_attempts,
        )
        rows = frame.to_dict(orient="records")
        security_rows = [row for row in rows if row["thscode"] == case["security_code"]]
        benchmark_rows = [row for row in rows if row["thscode"] == case["benchmark_code"]]
        return decision_outcome_service.evaluate(
            case_id,
            security_rows=security_rows,
            benchmark_rows=benchmark_rows,
            data_source="iFinD THS_HD",
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"iFinD 结果评价失败：{exc}") from exc
    finally:
        client.logout()


@app.post("/api/paper/accounts", status_code=201)
def create_paper_account(request: PaperAccountCreate) -> dict:
    return paper_trading_service.create_account(
        request.name, request.initial_cash, request.benchmark_code
    )


@app.get("/api/paper/dashboard")
def paper_dashboard(account_id: str | None = None, as_of: str | None = None) -> dict:
    try:
        return paper_trading_service.dashboard(account_id, as_of)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/paper/benchmarks/refresh")
def refresh_paper_benchmark_history(request: PaperBenchmarkRefresh) -> dict:
    try:
        return refresh_paper_benchmarks(request.account_id, request.as_of)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"iFinD 指数基准更新失败：{exc}") from exc


@app.post("/api/paper/daily-runs", status_code=201)
def create_paper_daily_run(request: PaperDailyRunCreate) -> dict:
    as_of = request.as_of or research_store.market_data_end()
    if as_of is None:
        raise HTTPException(status_code=409, detail="本地行情库没有可用截止日")
    try:
        return paper_trading_service.create_daily_run(
            as_of=as_of,
            account_id=request.account_id,
            top_n=request.top_n,
            hold_rank_buffer=request.hold_rank_buffer,
            target_gross_exposure=request.target_gross_exposure,
            max_position_weight=request.max_position_weight,
            max_industry_weight=request.max_industry_weight,
            max_pair_correlation=request.max_pair_correlation,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/paper/orders/{order_id}/approve")
def approve_paper_order(order_id: str, request: PaperOrderReview) -> dict:
    try:
        return paper_trading_service.review_order(
            order_id, "approve", request.reviewer, request.note
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/paper/orders/{order_id}/reject")
def reject_paper_order(order_id: str, request: PaperOrderReview) -> dict:
    try:
        return paper_trading_service.review_order(
            order_id, "reject", request.reviewer, request.note
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/paper/settle")
def settle_paper_orders(request: PaperSettleRequest) -> dict:
    execution_date = request.execution_date or research_store.market_data_end()
    if execution_date is None:
        raise HTTPException(status_code=409, detail="本地行情库没有可用执行日")
    try:
        return paper_trading_service.settle(
            execution_date=execution_date, account_id=request.account_id
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/paper/research-targets")
def paper_research_targets(
    as_of: str | None = None,
    limit: int = Query(5, ge=1, le=5),
    hold_rank_buffer: int = Query(30, ge=5, le=100),
) -> dict:
    target_date = as_of or research_store.market_data_end()
    if target_date is None:
        raise HTTPException(status_code=409, detail="本地行情库没有可用研究日")
    try:
        return paper_trading_service.research_targets(
            as_of=target_date, limit=limit, hold_rank_buffer=hold_rank_buffer
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/paper/research-assessments")
async def build_paper_research_assessments(request: PaperResearchBatchRequest) -> dict:
    target_date = request.as_of or research_store.market_data_end()
    if target_date is None:
        raise HTTPException(status_code=409, detail="本地行情库没有可用研究日")
    try:
        target_set = paper_trading_service.research_targets(
            as_of=target_date,
            limit=request.limit,
            hold_rank_buffer=request.hold_rank_buffer,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if request.include_holdings:
        existing_codes = {item["security_code"] for item in target_set["targets"]}
        for code in paper_trading_service.position_codes(request.account_id):
            if code in existing_codes:
                continue
            master = research_store.security(code) or {}
            signal = research_store.current_shadow_signal(target_set["shadow_snapshot_id"], code)
            assessment_artifact = research_store.latest_research_assessment(code, target_date)
            assessment = assessment_artifact["payload"] if assessment_artifact else None
            target_set["targets"].append({
                "security_code": code,
                "security_name": master.get("security_name") or code,
                "shadow_rank": signal.get("cross_section_rank") if signal else None,
                "score": signal.get("score") if signal else None,
                "current_research_status": assessment.get("signal") if assessment else "defer",
                "current_assessment": assessment,
                "target_reason": "existing_position",
            })
            existing_codes.add(code)
    results = []
    for target in target_set["targets"]:
        code = target["security_code"]
        existing = research_store.latest_research_assessment(code, target_date)
        if (
            existing
            and existing["as_of"] == target_date
            and existing["schema_version"] == RESEARCH_ASSESSMENT_SCHEMA_VERSION
            and existing["payload"].get("policy_version") == RESEARCH_ASSESSMENT_POLICY_VERSION
        ):
            results.append({
                **target,
                "status": "reused",
                "assessment": existing["payload"],
                "run_id": existing["run_id"],
            })
            continue
        sync_results = []
        sync_error = None
        try:
            if request.financial_download_limit:
                sync_results.append(await run_in_threadpool(
                    lambda code=code: announcement_pipeline.sync(
                        code,
                        end_date=date.fromisoformat(target_date),
                        lookback_days=request.lookback_days,
                        download_limit=request.financial_download_limit,
                        document_scope="financial_reports",
                    )
                ))
            if request.announcement_download_limit:
                sync_results.append(await run_in_threadpool(
                    lambda code=code: announcement_pipeline.sync(
                        code,
                        end_date=date.fromisoformat(target_date),
                        lookback_days=request.lookback_days,
                        download_limit=request.announcement_download_limit,
                        document_scope="all",
                    )
                ))
        except Exception as exc:
            sync_error = str(exc)
        try:
            research = await company_research_service.run(
                code=code,
                as_of=target_date,
                depth=request.depth,
                factor_snapshot_id=target_set["factor_snapshot_id"],
            )
            results.append({
                **target,
                "status": "completed",
                "run_id": research["run_id"],
                "assessment": research["assessment"],
                "sync": sync_results,
                "sync_error": sync_error,
            })
        except Exception as exc:
            detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
            results.append({
                **target,
                "status": "failed",
                "error": detail,
                "sync": sync_results,
                "sync_error": sync_error,
            })
    return {
        **target_set,
        "policy_version": RESEARCH_ASSESSMENT_POLICY_VERSION,
        "results": results,
        "completed": sum(item["status"] in {"completed", "reused"} for item in results),
        "failed": sum(item["status"] == "failed" for item in results),
    }


async def run_daily_research_batch(
    batch_id: str,
    request: PaperDailyBatchRequest,
    as_of: str,
    account_id: str,
) -> None:
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
        snapshot = await run_in_threadpool(
            lambda: current_shadow_service.generate(
                as_of=date.fromisoformat(as_of),
                model_run_id=request.model_run_id,
                lookback_days=request.shadow_lookback_days,
                batch_size=request.shadow_batch_size,
                trust_pickle=True,
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
        }

    async def candidate_research_step() -> dict:
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
            )
        )
        return {
            "run_id": run["run_id"],
            "as_of": run["as_of"],
            "status": run["status"],
            "strategy_version": run["strategy_version"],
            "order_count": len(run.get("orders", [])),
            "reused": run.get("reused", False),
        }

    await daily_batch_runner.run(batch_id, {
        "market_data": market_data_step,
        "factor_snapshot": factor_snapshot_step,
        "current_shadow": current_shadow_step,
        "candidate_research": candidate_research_step,
        "holdings_review": holdings_review_step,
        "order_proposals": order_proposals_step,
    })


@app.post("/api/paper/daily-batches", status_code=202)
async def create_daily_research_batch(
    request: PaperDailyBatchRequest,
    background_tasks: BackgroundTasks,
) -> dict:
    as_of = request.as_of or default_end_date().isoformat()
    try:
        date.fromisoformat(as_of)
        account = (
            paper_trading_service.account(request.account_id)
            if request.account_id else paper_trading_service.default_account()
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    config = request.model_dump(exclude={"as_of", "account_id"})
    batch, should_start = daily_batch_store.prepare(
        account_id=account["account_id"],
        as_of=as_of,
        config=config,
    )
    if should_start:
        background_tasks.add_task(
            run_daily_research_batch,
            batch["batch_id"],
            request,
            as_of,
            account["account_id"],
        )
    return {**batch, "started": should_start, "reused": not should_start}


@app.get("/api/paper/daily-batches/latest")
def latest_daily_research_batch(
    account_id: str | None = None,
    as_of: str | None = None,
) -> dict:
    try:
        account = (
            paper_trading_service.account(account_id)
            if account_id else paper_trading_service.default_account()
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"item": daily_batch_store.latest(account["account_id"], as_of)}


@app.get("/api/paper/daily-batches/{batch_id}")
def daily_research_batch(batch_id: str) -> dict:
    batch = daily_batch_store.get(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="每日研究批次不存在")
    return batch


@app.get("/api/runtime/runs/{root_run_id}/events")
def runtime_run_events(
    root_run_id: str,
    after_seq: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=1000),
) -> dict:
    run = daily_batch_store.events.run(root_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="运行事件根任务不存在")
    fetched = daily_batch_store.events.events(
        root_run_id, after_seq=after_seq, limit=limit + 1
    )
    has_more = len(fetched) > limit
    items = fetched[:limit]
    return {
        "root_run_id": root_run_id,
        "after_seq": after_seq,
        "items": items,
        "last_seq": items[-1]["seq"] if items else after_seq,
        "has_more": has_more,
    }


@app.get("/api/runtime/runs/{root_run_id}")
def runtime_run(root_run_id: str) -> dict:
    item = daily_batch_store.events.run(root_run_id)
    if item is None:
        raise HTTPException(status_code=404, detail="运行事件根任务不存在")
    return item


@app.post("/api/paper/realtime-quotes/refresh")
def refresh_paper_realtime_quotes(request: PaperRealtimeRequest) -> dict:
    try:
        return paper_trading_service.refresh_realtime_quotes(request.account_id)
    except IFindError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except (KeyError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/paper/settle/realtime")
def settle_paper_orders_realtime(request: PaperRealtimeRequest) -> dict:
    try:
        return paper_trading_service.settle_realtime(request.account_id)
    except IFindError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except (KeyError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/stocks/{code}/financial-facts/refresh")
def refresh_financial_facts(code: str, as_of: str | None = None) -> dict:
    analysis = build_analysis(code, as_of)
    result = research_store.refresh_financial_facts(
        analysis["security"]["code"], analysis["as_of"]
    )
    result["items"] = research_store.financial_fact_evidence(
        analysis["security"]["code"], analysis["as_of"]
    )
    return result


@app.get("/api/stocks/{code}/financial-facts")
def financial_facts(code: str, as_of: str | None = None) -> dict:
    normalized = normalize_code(code)
    return {"items": research_store.financial_fact_evidence(normalized, as_of)}


@app.get("/api/stocks/{code}/assistant-history")
def assistant_history(code: str, limit: int = Query(20, ge=1, le=100)) -> dict:
    normalized = normalize_code(code)
    return {"items": research_store.list_interactions(normalized, limit)}


@app.get("/api/research-interactions/{interaction_id}")
def research_interaction(interaction_id: str) -> dict:
    item = research_store.interaction(interaction_id)
    if item is None:
        raise HTTPException(status_code=404, detail="研究记录不存在")
    return item


@app.post("/api/stocks/{code}/announcements/sync")
async def sync_announcements(
    code: str,
    as_of: str | None = None,
    lookback_days: int = Query(365, ge=1, le=3650),
    download_limit: int = Query(5, ge=0, le=50),
    document_scope: str = Query("all", pattern="^(all|financial_reports)$"),
) -> dict:
    analysis = build_analysis(code, as_of)
    try:
        return await run_in_threadpool(
            lambda: announcement_pipeline.sync(
                analysis["security"]["code"],
                end_date=date.fromisoformat(analysis["as_of"]),
                lookback_days=lookback_days,
                download_limit=download_limit,
                document_scope=document_scope,
            )
        )
    except IFindError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/documents/{document_id}/file", include_in_schema=False)
def document_file(document_id: str) -> FileResponse:
    path = research_store.resolve_document_path(document_id)
    if path is None:
        raise HTTPException(status_code=404, detail="公告文档不存在")
    item = research_store.document(document_id)
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=f"{item['security_code']}-{item['title']}.pdf",
        content_disposition_type="inline",
    )


@app.get("/api/theses")
def list_theses(code: str | None = None) -> dict:
    return {"items": thesis_store.list(code)}


@app.post("/api/theses", status_code=201)
def create_thesis(data: ThesisCreate) -> dict:
    try:
        if not repo.exists(data.code):
            raise HTTPException(status_code=404, detail="股票不在本地数据库中")
        return thesis_store.create(data)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.patch("/api/theses/{thesis_id}")
def update_thesis(thesis_id: str, data: ThesisUpdate) -> dict:
    item = thesis_store.update_status(thesis_id, data.status)
    if not item:
        raise HTTPException(status_code=404, detail="研究论点不存在")
    return item


def _completed_run(run_id: str, security_code: str) -> dict:
    run = research_store.research_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="研究运行不存在")
    if run["security_code"] != security_code:
        raise HTTPException(status_code=422, detail="研究运行与 Thesis 证券不一致")
    if run["status"] not in {"completed", "completed_with_gaps"}:
        raise HTTPException(status_code=422, detail="研究运行尚未成功完成")
    return run


@app.get("/api/theses/{thesis_id}/monitor")
def thesis_monitor(thesis_id: str) -> dict:
    detail = thesis_store.monitor_detail(thesis_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="研究论点不存在")
    return detail


@app.post("/api/theses/{thesis_id}/monitor/baseline")
def establish_thesis_baseline(
    thesis_id: str, request: ThesisMonitorBaselineRequest
) -> dict:
    thesis = thesis_store.get(thesis_id)
    if thesis is None:
        raise HTTPException(status_code=404, detail="研究论点不存在")
    run_id = request.run_id
    if run_id is None:
        run_id = next(
            (
                item["run_id"]
                for item in research_store.list_research_runs(thesis["security_code"], 100)
                if item["status"] in {"completed", "completed_with_gaps"}
            ),
            None,
        )
    if run_id is None:
        raise HTTPException(status_code=409, detail="该证券没有可用的公司研究运行")
    run = _completed_run(run_id, thesis["security_code"])
    evidence_pack, snapshot = run_artifacts(run)
    try:
        result = thesis_store.set_monitor_baseline(
            thesis_id=thesis_id,
            run=run,
            claims=snapshot["claims"],
            evidence=evidence_pack["items"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    result["monitor"] = thesis_store.monitor_detail(thesis_id)
    return result


@app.post("/api/theses/{thesis_id}/monitor/check")
def check_thesis_monitor(thesis_id: str, request: ThesisMonitorCheckRequest) -> dict:
    thesis = thesis_store.get(thesis_id)
    if thesis is None:
        raise HTTPException(status_code=404, detail="研究论点不存在")
    baseline_info = thesis["monitor"]["baseline"]
    if baseline_info is None:
        raise HTTPException(status_code=409, detail="请先设置监控基线")
    baseline_run = _completed_run(baseline_info["run_id"], thesis["security_code"])
    current_run_id = request.current_run_id
    if current_run_id is None:
        current_run_id = next(
            (
                item["run_id"]
                for item in research_store.list_research_runs(thesis["security_code"], 100)
                if item["status"] in {"completed", "completed_with_gaps"}
                and item["run_id"] != baseline_run["run_id"]
                and item["started_at"] > baseline_run["started_at"]
            ),
            None,
        )
    if current_run_id is None:
        raise HTTPException(status_code=409, detail="基线之后没有新的公司研究运行")
    current_run = _completed_run(current_run_id, thesis["security_code"])
    claims = thesis_store.active_claims(thesis_id)
    try:
        evaluations = build_monitor_evaluations(
            thesis=thesis,
            baseline_run=baseline_run,
            current_run=current_run,
            claims=claims,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    saved = thesis_store.save_claim_evaluations(
        thesis_id=thesis_id,
        current_run_id=current_run["run_id"],
        evaluations=evaluations,
    )
    return {
        "thesis_id": thesis_id,
        "baseline_run_id": baseline_run["run_id"],
        "current_run_id": current_run["run_id"],
        "evaluations": saved,
    }


@app.patch("/api/claim-evaluations/{evaluation_id}")
def confirm_claim_evaluation(
    evaluation_id: str, request: ClaimEvaluationConfirm
) -> dict:
    item = thesis_store.confirm_evaluation(evaluation_id, request.verdict)
    if item is None:
        raise HTTPException(status_code=404, detail="Claim 评估不存在")
    return item


@app.delete("/api/theses/{thesis_id}", status_code=204)
def delete_thesis(thesis_id: str) -> None:
    if not thesis_store.delete(thesis_id):
        raise HTTPException(status_code=404, detail="研究论点不存在")
