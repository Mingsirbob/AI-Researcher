from .base import domain_router
from ..handlers import *  # noqa: F403

router = domain_router()


def owns(path: str) -> bool:
    return True


@router.get("/", include_in_schema=False)
@router.get("/paper", include_in_schema=False)
@router.get("/company", include_in_schema=False)
@router.get("/theses", include_in_schema=False)
@router.get("/quant", include_in_schema=False)
@router.get("/factor-development", include_in_schema=False)
@router.get("/factor-evaluation", include_in_schema=False)
@router.get("/backtest", include_in_schema=False)
@router.get("/factor-library", include_in_schema=False)
@router.get("/decisions", include_in_schema=False)
@router.get("/acceptance", include_in_schema=False)
def index() -> Response:
    return production_frontend_response(frontend_dist=frontend_dist)


@router.get("/next", include_in_schema=False)
@router.get("/next/{path:path}", include_in_schema=False)
def next_compatibility_redirect(path: str = "") -> Response:
    frontend_paths = {
        "": "/",
        "paper": "/",
        "company": "/company",
        "theses": "/theses",
        "quant": "/quant",
        "factor-development": "/factor-development",
        "factor-evaluation": "/factor-evaluation",
        "backtest": "/backtest",
        "factor-library": "/factor-library",
        "decisions": "/decisions",
        "acceptance": "/acceptance",
    }
    target = frontend_paths.get(path.strip("/"))
    if target is None:
        raise HTTPException(status_code=404, detail="前端路由不存在")
    return RedirectResponse(url=target, status_code=307)


@router.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "frontend": frontend_cutover_status(),
        "securities": repo.security_count,
        "llm_configured": settings.llm_configured,
        "llm": {
            "configured": settings.llm_configured,
            "resilience": llm_resilience_status(settings),
        },
        "ifind": ifind_service.status(),
        "research_data": research_store.stats(),
        "quant_data": quant_store.stats(),
        "database": settings.stock_db.name,
    }


@router.get("/api/ifind/status")
def ifind_status() -> dict:
    return ifind_service.status()


@router.get("/api/runtime/runs/{root_run_id}/events")
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


@router.get("/api/runtime/runs/{root_run_id}")
def runtime_run(root_run_id: str) -> dict:
    item = daily_batch_store.events.run(root_run_id)
    if item is None:
        raise HTTPException(status_code=404, detail="运行事件根任务不存在")
    return item
