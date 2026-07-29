from .base import domain_router
from ..handlers import *  # noqa: F403
from ..handlers import _completed_run

router = domain_router()


def owns(path: str) -> bool:
    return path.startswith("/api/theses") or path.startswith("/api/claim-evaluations")


@router.get("/api/theses")
def list_theses(code: str | None = None) -> dict:
    return {"items": thesis_store.list(code)}


@router.post("/api/theses", status_code=201)
def create_thesis(data: ThesisCreate) -> dict:
    try:
        if not repo.exists(data.code):
            raise HTTPException(status_code=404, detail="股票不在本地数据库中")
        return thesis_store.create(data)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.patch("/api/theses/{thesis_id}")
def update_thesis(thesis_id: str, data: ThesisUpdate) -> dict:
    item = thesis_store.update_status(thesis_id, data.status)
    if not item:
        raise HTTPException(status_code=404, detail="研究论点不存在")
    return item


@router.get("/api/theses/{thesis_id}/monitor")
def thesis_monitor(thesis_id: str) -> dict:
    detail = thesis_store.monitor_detail(thesis_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="研究论点不存在")
    return detail


@router.post("/api/theses/{thesis_id}/monitor/baseline")
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


@router.post("/api/theses/{thesis_id}/monitor/check")
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


@router.patch("/api/claim-evaluations/{evaluation_id}")
def confirm_claim_evaluation(
    evaluation_id: str, request: ClaimEvaluationConfirm
) -> dict:
    item = thesis_store.confirm_evaluation(evaluation_id, request.verdict)
    if item is None:
        raise HTTPException(status_code=404, detail="Claim 评估不存在")
    return item


@router.delete(
    "/api/theses/{thesis_id}",
    status_code=204,
    response_class=Response,
    response_model=None,
)
def delete_thesis(thesis_id: str) -> None:
    if not thesis_store.delete(thesis_id):
        raise HTTPException(status_code=404, detail="研究论点不存在")
