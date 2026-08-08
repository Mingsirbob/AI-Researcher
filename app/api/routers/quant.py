from .base import domain_router
from ..handlers import *  # noqa: F403

router = domain_router()


def owns(path: str) -> bool:
    return (
        path.startswith("/api/factor-snapshots")
        or path.startswith("/api/quant-research")
        or path.startswith("/api/model-runs")
        or path.startswith("/api/current-shadow")
        or path.startswith("/api/quant/")
        or path.startswith("/api/market/universe-membership")
    )


@router.get("/api/quant-research/overview")
def quant_research_overview() -> dict:
    return simple_research_catalog.overview()


@router.get("/api/quant-research/factor-sets")
def quant_research_factor_sets() -> dict:
    return {"items": simple_research_catalog.factor_sets()}


@router.get("/api/quant-research/factor-templates")
def quant_research_factor_templates() -> dict:
    return {"items": simple_research_catalog.templates()}


@router.get("/api/quant-research/factors")
def quant_research_factors(
    factor_set_id: str | None = None,
    status: str | None = Query(None, pattern="^(active|disabled)$"),
) -> dict:
    return {
        "items": simple_research_catalog.factors(
            factor_set_id=factor_set_id,
            status=status,
        )
    }


@router.post("/api/quant-research/factors", status_code=201)
def create_quant_factor(request: FactorDefinitionCreate) -> dict:
    try:
        return {"item": simple_research_catalog.create_factor(**request.model_dump())}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/api/quant-research/experiments")
def quant_research_experiments(
    experiment_type: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    return {
        "items": simple_research_catalog.experiments(
            experiment_type=experiment_type,
            target_type=target_type,
            target_id=target_id,
            limit=limit,
        )
    }


@router.get("/api/quant-research/backtests/latest")
def latest_quant_backtest() -> dict:
    return {"item": simple_research_catalog.latest_backtest()}


@router.get("/api/quant-research/experiments/{experiment_id}")
def quant_research_experiment(experiment_id: str) -> dict:
    item = simple_research_catalog.experiment(experiment_id)
    if item is None:
        raise HTTPException(status_code=404, detail="研究实验不存在")
    return item


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
