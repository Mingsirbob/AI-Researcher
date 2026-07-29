from .base import domain_router
from ..handlers import *  # noqa: F403

router = domain_router()


def owns(path: str) -> bool:
    return path.startswith("/api/decision-")


@router.post("/api/decision-cases", status_code=201)
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


@router.get("/api/decision-cases")
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


@router.get("/api/decision-cases/{case_id}")
def decision_case(case_id: str) -> dict:
    item = research_store.decision_case(case_id)
    if item is None:
        raise HTTPException(status_code=404, detail="决策案例不存在")
    return public_decision_case(item)


@router.post("/api/decision-cases/{case_id}/reviews", status_code=201)
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


@router.get("/api/decision-outcomes/attribution")
def decision_outcome_attribution() -> dict:
    return decision_outcome_service.attribution()


@router.get("/api/decision-outcomes")
def list_decision_outcomes(
    status: str | None = Query(None, pattern="^(pending|completed|data_rejected)$"),
    limit: int = Query(100, ge=1, le=500),
) -> dict:
    return {"items": research_store.list_decision_outcomes(status=status, limit=limit)}


@router.get("/api/decision-outcomes/maturity-scan")
def decision_outcome_maturity_scan(as_of: str | None = None) -> dict:
    target = date.fromisoformat(as_of) if as_of else default_end_date()
    items = []
    for case in research_store.list_decision_cases(limit=500):
        start = date.fromisoformat(case["as_of"])
        observed = sum(1 for offset in range(1, max((target - start).days, 0) + 1) if (start + timedelta(days=offset)).weekday() < 5)
        outcome = research_store.decision_case_outcome(case["case_id"])
        status = "completed" if outcome and outcome["status"] == "completed" else "ready" if observed >= case["decision_horizon_days"] else "pending"
        items.append({"case_id": case["case_id"], "security_code": case["security_code"], "as_of": case["as_of"], "horizon_trading_days": case["decision_horizon_days"], "estimated_observed_trading_days": observed, "estimated_remaining_trading_days": max(case["decision_horizon_days"] - observed, 0), "status": status, "latest_outcome_id": outcome["outcome_id"] if outcome else None})
    return {"as_of": target.isoformat(), "items": items, "counts": {name: sum(item["status"] == name for item in items) for name in ("ready", "pending", "completed")}, "boundary": "成熟度按工作日估算；正式评价仍以证券与基准对齐后的真实交易日为准。"}


@router.post("/api/decision-outcomes/batch-evaluate")
def batch_evaluate_decision_outcomes(request: DecisionOutcomeBatchEvaluate) -> dict:
    results = []
    for case_id in dict.fromkeys(request.case_ids):
        try:
            results.append({"case_id": case_id, "status": "ok", "outcome": evaluate_decision_case(case_id, DecisionOutcomeEvaluate(end_date=request.end_date))})
        except HTTPException as exc:
            results.append({"case_id": case_id, "status": "failed", "error": exc.detail, "http_status": exc.status_code})
    return {"results": results, "completed": sum(item["status"] == "ok" for item in results), "failed": sum(item["status"] == "failed" for item in results)}


@router.get("/api/decision-outcomes/{outcome_id}")
def decision_outcome(outcome_id: str) -> dict:
    item = research_store.decision_outcome(outcome_id)
    if item is None:
        raise HTTPException(status_code=404, detail="决策结果不存在")
    return item


@router.get("/api/decision-cases/{case_id}/outcome")
def decision_case_outcome(case_id: str) -> dict:
    if research_store.decision_case(case_id) is None:
        raise HTTPException(status_code=404, detail="决策案例不存在")
    return {"item": research_store.decision_case_outcome(case_id)}


@router.post("/api/decision-cases/{case_id}/outcome", status_code=201)
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
