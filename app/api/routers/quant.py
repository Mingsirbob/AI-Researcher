from .base import domain_router
from ..handlers import *  # noqa: F403

router = domain_router()


def owns(path: str) -> bool:
    return (
        path.startswith("/api/factor-")
        or path.startswith("/api/model-runs")
        or path.startswith("/api/current-shadow")
        or path.startswith("/api/quant/")
        or path.startswith("/api/market/universe-membership")
    )


@router.get("/api/market/universe-membership")
def market_universe_membership(as_of: str, universe: str | None = None) -> dict:
    try:
        date.fromisoformat(as_of)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="as_of 必须是 YYYY-MM-DD") from exc
    return universe_membership(settings.stock_qfq_db, as_of=as_of, universe=universe)


@router.post("/api/quant/neutralize")
def factor_neutralization(request: FactorNeutralizationRequest) -> dict:
    return neutralize_factor(
        request.rows,
        factor_key=request.factor_key,
        industry_key=request.industry_key,
        market_cap_key=request.market_cap_key,
    )


@router.post("/api/factor-snapshots")
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


@router.get("/api/factor-snapshots/latest")
def latest_factor_snapshot() -> dict:
    snapshot = quant_store.latest_factor_snapshot()
    if snapshot is None:
        raise HTTPException(status_code=404, detail="尚未生成全市场因子快照")
    return snapshot


@router.get("/api/factor-snapshots/{snapshot_id}/securities")
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
    snapshot = quant_store.factor_snapshot(snapshot_id)
    if snapshot is None or snapshot["status"] != "completed":
        raise HTTPException(status_code=404, detail="因子快照不存在或尚未完成")
    result = quant_store.list_factor_rows(
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


@router.get("/api/factor-lab/overview")
def factor_lab_overview() -> dict:
    return factor_lab_service.overview()


@router.get("/api/factor-lab/templates")
def factor_lab_templates() -> dict:
    return {"items": factor_lab_service.templates()}


@router.get("/api/factor-lab/factors")
def factor_lab_factors(
    status: str | None = Query(
        None, pattern="^(draft|testing|shadow|approved|deprecated)$"
    ),
) -> dict:
    return {"items": factor_lab_service.list_factors(status)}


@router.post("/api/factor-lab/factors")
def create_factor_definition(request: FactorDefinitionCreate) -> dict:
    try:
        return {
            "item": factor_lab_service.create_factor(
                factor_id=request.factor_id,
                name=request.name,
                description=request.description,
                template_id=request.template_id,
                window=request.window,
                direction=request.direction,
                owner=request.owner,
            )
        }
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/api/factor-lab/factors/{factor_id}/versions/{version}/status")
def change_factor_lifecycle(
    factor_id: str, version: int, request: FactorLifecycleChange
) -> dict:
    try:
        return {
            "item": factor_lab_service.change_status(
                factor_id, version, request.to_status, request.reviewer, request.note
            )
        }
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/api/factor-lab/snapshots")
async def generate_factor_lab_snapshot(request: FactorLabSnapshotRequest) -> dict:
    as_of = request.as_of or research_store.market_data_end()
    if as_of is None:
        raise HTTPException(status_code=409, detail="本地行情库没有可用截止日")
    try:
        return await run_in_threadpool(factor_lab_service.generate_snapshot, as_of)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"实验室因子快照生成失败：{exc}") from exc


@router.get("/api/factor-lab/snapshots/latest")
def latest_factor_lab_snapshot() -> dict:
    return {"item": factor_lab_service.latest_snapshot()}


@router.get("/api/factor-lab/snapshots/{snapshot_id}/values")
def factor_lab_snapshot_values(
    snapshot_id: str,
    factor_id: str,
    limit: int = Query(100, ge=1, le=500),
) -> dict:
    try:
        return factor_lab_service.snapshot_values(snapshot_id, factor_id, limit)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/factor-lab/evaluations")
async def run_factor_evaluation(request: FactorEvaluationRequest) -> dict:
    if factor_evaluation_service is None:
        raise HTTPException(status_code=409, detail="缺少 stock_data_qfq.db，无法运行正式因子评价")
    end_date = request.end_date or research_store.market_data_end()
    if end_date is None:
        raise HTTPException(status_code=409, detail="没有可用行情截止日")
    try:
        return await run_in_threadpool(
            factor_evaluation_service.run,
            start_date=request.start_date,
            end_date=end_date,
            rebalance_step=request.rebalance_step,
            horizons=tuple(request.horizons),
            layer_count=request.layer_count,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"因子评价失败：{exc}") from exc


@router.get("/api/factor-lab/evaluations/latest")
def latest_factor_evaluation() -> dict:
    return {"item": factor_evaluation_service.latest() if factor_evaluation_service else None}


@router.get("/api/factor-lab/evaluations/{evaluation_id}")
def factor_evaluation(evaluation_id: str) -> dict:
    if factor_evaluation_service is None:
        raise HTTPException(status_code=409, detail="缺少 stock_data_qfq.db")
    item = factor_evaluation_service.evaluation(evaluation_id)
    if item is None:
        raise HTTPException(status_code=404, detail="因子评价不存在")
    return item


@router.post("/api/factor-lab/backtests")
async def run_factor_backtest(request: FactorBacktestRequest) -> dict:
    if factor_backtest_service is None or factor_evaluation_service is None:
        raise HTTPException(status_code=409, detail="缺少 stock_data_qfq.db，无法运行策略回测")
    evaluation_id = request.evaluation_id
    if not evaluation_id:
        latest = factor_evaluation_service.latest()
        evaluation_id = latest["run"]["evaluation_id"] if latest else None
    if not evaluation_id:
        raise HTTPException(status_code=409, detail="请先完成 M11.2 因子评价")
    try:
        return await run_in_threadpool(
            factor_backtest_service.run,
            evaluation_id=evaluation_id,
            factor_id=request.factor_id,
            start_date=request.start_date,
            end_date=request.end_date,
            top_n=request.top_n,
            rebalance_step=request.rebalance_step,
            initial_capital=request.initial_capital,
            commission_rate=request.commission_rate,
            stamp_duty_rate=request.stamp_duty_rate,
            slippage_rate=request.slippage_rate,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"策略回测失败：{exc}") from exc


@router.get("/api/factor-lab/backtests/latest")
def latest_factor_backtest() -> dict:
    return {"item": factor_backtest_service.latest() if factor_backtest_service else None}


@router.get("/api/factor-lab/backtests/{backtest_id}")
def factor_backtest(backtest_id: str) -> dict:
    if factor_backtest_service is None:
        raise HTTPException(status_code=409, detail="缺少 stock_data_qfq.db")
    item = factor_backtest_service.backtest(backtest_id)
    if item is None:
        raise HTTPException(status_code=404, detail="策略回测不存在")
    return item


@router.post("/api/factor-lab/releases")
def create_factor_release(request: FactorReleaseCreate) -> dict:
    try:
        return factor_release_service.create_candidate(
            factor_id=request.factor_id,
            factor_version=request.factor_version,
            evaluation_id=request.evaluation_id,
            backtest_id=request.backtest_id,
            limitations_acknowledged=request.limitations_acknowledged,
            created_by=request.created_by,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/api/factor-lab/releases/latest")
def latest_factor_releases(limit: int = Query(20, ge=1, le=100)) -> dict:
    return factor_release_service.latest(limit)


@router.get("/api/factor-lab/releases/{release_id}")
def factor_release(release_id: str) -> dict:
    item = factor_release_service.release(release_id)
    if item is None:
        raise HTTPException(status_code=404, detail="发布候选不存在")
    return item


@router.post("/api/factor-lab/releases/{release_id}/approve")
def approve_factor_release(release_id: str, request: FactorReleaseDecision) -> dict:
    try:
        return factor_release_service.decide(
            release_id, "approve", request.reviewer, request.note
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/api/factor-lab/releases/{release_id}/reject")
def reject_factor_release(release_id: str, request: FactorReleaseDecision) -> dict:
    try:
        return factor_release_service.decide(
            release_id, "reject", request.reviewer, request.note
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/api/model-runs/latest")
def latest_model_run() -> dict:
    return {"item": quant_store.latest_model_run()}


@router.get("/api/model-runs/{model_run_id}")
def model_run(model_run_id: str) -> dict:
    item = quant_store.model_run(model_run_id)
    if item is None:
        raise HTTPException(status_code=404, detail="模型运行不存在")
    return item


@router.get("/api/model-runs/{model_run_id}/signals")
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
        return quant_store.list_shadow_signals(
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


@router.get("/api/model-runs/{model_run_id}/validation")
def model_run_validation(model_run_id: str) -> dict:
    if quant_store.model_run(model_run_id) is None:
        raise HTTPException(status_code=404, detail="模型运行不存在")
    return {"item": quant_store.latest_model_validation(model_run_id)}


@router.get("/api/current-shadow/latest")
def latest_current_shadow() -> dict:
    return {"item": quant_store.latest_current_shadow()}


@router.get("/api/current-shadow/{snapshot_id}/signals")
def current_shadow_signals(
    snapshot_id: str,
    q: str = "",
    sort: str = Query("rank", pattern="^(rank|score|security_code)$"),
    direction: str = Query("asc", pattern="^(asc|desc)$"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    try:
        return quant_store.list_current_shadow_signals(
            snapshot_id,
            query=q,
            sort=sort,
            direction=direction,
            limit=limit,
            offset=offset,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
