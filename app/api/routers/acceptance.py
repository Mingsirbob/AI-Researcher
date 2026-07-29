from .base import domain_router
from ..handlers import *  # noqa: F403

router = domain_router()


def owns(path: str) -> bool:
    return path.startswith("/api/evidence-acceptance")


@router.post("/api/evidence-acceptance-runs")
async def run_evidence_acceptance() -> dict:
    return await run_in_threadpool(evidence_acceptance_service.run)


@router.get("/api/evidence-acceptance-runs/latest")
def latest_evidence_acceptance_run() -> dict:
    return {"item": research_store.latest_evidence_acceptance_run()}


@router.get("/api/evidence-acceptance-runs")
def evidence_acceptance_runs(limit: int = Query(20, ge=1, le=100)) -> dict:
    return {"items": research_store.list_evidence_acceptance_runs(limit)}


@router.get("/api/evidence-acceptance-runs/{run_id}")
def evidence_acceptance_run(run_id: str) -> dict:
    item = research_store.evidence_acceptance_run(run_id)
    if item is None:
        raise HTTPException(status_code=404, detail="证据验收记录不存在")
    return item
