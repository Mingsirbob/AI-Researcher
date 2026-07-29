from .base import domain_router
from ..handlers import *  # noqa: F403

router = domain_router()


def owns(path: str) -> bool:
    return (
        path.startswith("/api/securities")
        or path.endswith("/analysis")
        or path.endswith("/master")
    )


@router.get("/api/securities")
def securities(q: str = "", limit: int = Query(12, ge=1, le=50)) -> dict:
    items = research_store.list_securities(q, limit)
    return {"items": items or repo.list_securities(q, limit), "total_covered": repo.security_count}


@router.get("/api/securities/{code}/master")
def security_master(code: str, as_of: str | None = None) -> dict:
    item = research_store.security_at(code, as_of) if as_of else research_store.security(code)
    if item is None:
        raise HTTPException(status_code=404, detail="证券主数据不存在")
    return item


@router.get("/api/stocks/{code}/analysis")
def stock_analysis(code: str, as_of: str | None = None) -> dict:
    return build_analysis(code, as_of)
