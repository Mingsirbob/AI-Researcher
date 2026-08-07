from .base import domain_router
from ..handlers import *  # noqa: F403
from app.workflows.daily_batch import resolve_batch_date

router = domain_router()


def owns(path: str) -> bool:
    return path.startswith("/api/paper/")


@router.post("/api/paper/accounts", status_code=201)
def create_paper_account(request: PaperAccountCreate) -> dict:
    try:
        return paper_trading_service.create_account(
            request.name, request.initial_cash, request.benchmark_code,
            request.strategy_id, request.strategy_version_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/paper/accounts")
def list_paper_accounts() -> dict:
    return {"items": paper_trading_service.list_accounts()}


@router.get("/api/paper/strategies")
def list_paper_strategies() -> dict:
    return {"items": paper_trading_service.list_strategies()}


@router.get("/api/paper/scheduler/status")
def paper_scheduler_status(request: Request) -> dict:
    scheduler = getattr(request.app.state, "paper_scheduler", None)
    return scheduler.last_result if scheduler else {"status": "disabled"}


@router.post("/api/paper/accounts/{account_id}/deployments", status_code=201)
def deploy_paper_strategy(account_id: str, request: PaperStrategyDeploymentCreate) -> dict:
    try:
        return paper_trading_service.deploy_strategy(account_id, request.strategy_version_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/api/paper/dashboard")
def paper_dashboard(account_id: str | None = None, as_of: str | None = None) -> dict:
    try:
        return paper_trading_service.dashboard(account_id, as_of)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/api/paper/orders")
def paper_order_ledger(
    account_id: str | None = None,
    limit: int = Query(500, ge=1, le=2000),
) -> dict:
    try:
        return paper_trading_service.order_ledger(account_id, limit)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/paper/benchmarks/refresh")
def refresh_paper_benchmark_history(request: PaperBenchmarkRefresh) -> dict:
    try:
        return refresh_paper_benchmarks(request.account_id, request.as_of)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"iFinD 指数基准更新失败：{exc}") from exc


@router.post("/api/paper/daily-runs", status_code=201)
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


@router.post("/api/paper/orders/{order_id}/approve")
def approve_paper_order(order_id: str, request: PaperOrderReview) -> dict:
    try:
        return paper_trading_service.review_order(
            order_id, "approve", request.reviewer, request.note
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/paper/orders/{order_id}/reject")
def reject_paper_order(order_id: str, request: PaperOrderReview) -> dict:
    try:
        return paper_trading_service.review_order(
            order_id, "reject", request.reviewer, request.note
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/paper/settle")
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


@router.get("/api/paper/research-targets")
def paper_research_targets(
    as_of: str | None = None,
    account_id: str | None = None,
    limit: int = Query(5, ge=1, le=5),
    hold_rank_buffer: int = Query(30, ge=5, le=100),
) -> dict:
    target_date = as_of or research_store.market_data_end()
    if target_date is None:
        raise HTTPException(status_code=409, detail="本地行情库没有可用研究日")
    try:
        return paper_trading_service.research_targets(
            as_of=target_date, account_id=account_id,
            limit=limit, hold_rank_buffer=hold_rank_buffer
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/api/paper/research-assessments")
async def build_paper_research_assessments(request: PaperResearchBatchRequest) -> dict:
    target_date = request.as_of or research_store.market_data_end()
    if target_date is None:
        raise HTTPException(status_code=409, detail="本地行情库没有可用研究日")
    try:
        target_set = paper_trading_service.research_targets(
            as_of=target_date,
            account_id=request.account_id,
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
            signal = quant_store.current_shadow_signal(target_set["shadow_snapshot_id"], code)
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


@router.post("/api/paper/daily-batches", status_code=202)
async def create_daily_research_batch(
    request: PaperDailyBatchRequest,
    background_tasks: BackgroundTasks,
) -> dict:
    try:
        as_of = await run_in_threadpool(
            resolve_batch_date,
            request.as_of,
            local_latest=research_store.market_data_end(),
            trading_dates=ifind_service.trading_dates,
            default_target=default_end_date(),
        )
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


@router.get("/api/paper/daily-batches/latest")
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
    target_date = as_of or research_store.market_data_end()
    return {"item": daily_batch_store.latest(account["account_id"], target_date)}


@router.get("/api/paper/daily-batches/{batch_id}")
def daily_research_batch(batch_id: str) -> dict:
    batch = daily_batch_store.get(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="每日研究批次不存在")
    return batch


@router.post("/api/paper/realtime-quotes/refresh")
def refresh_paper_realtime_quotes(request: PaperRealtimeRequest) -> dict:
    try:
        return paper_trading_service.refresh_realtime_quotes(request.account_id)
    except IFindDataError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except (KeyError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/paper/settle/realtime")
def settle_paper_orders_realtime(request: PaperRealtimeRequest) -> dict:
    try:
        return paper_trading_service.settle_realtime(request.account_id)
    except IFindDataError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except (KeyError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
