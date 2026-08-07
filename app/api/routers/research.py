from .base import domain_router
from ..handlers import *  # noqa: F403

router = domain_router()


def owns(path: str) -> bool:
    return (
        path.startswith("/api/research")
        or path.startswith("/api/stocks/")
        or path.startswith("/api/document")
        or path.startswith("/api/financial-change-template")
    )


@router.get("/api/stocks/{code}/context")
async def stock_context(code: str, as_of: str | None = None) -> dict:
    analysis = build_analysis(code, as_of)
    try:
        context = await run_in_threadpool(
            load_ifind_context,
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
    except IFindDataError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/api/research")
async def research(request: ResearchRequest) -> dict:
    analysis = build_analysis(request.code, request.as_of)
    if ifind_service.status()["configured"]:
        try:
            context = await run_in_threadpool(
                load_ifind_context,
                analysis["security"]["code"],
                analysis["as_of"],
            )
            enrich_with_ifind(analysis, context)
        except IFindDataError as exc:
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


@router.get("/api/stocks/{code}/announcements")
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


@router.get("/api/stocks/{code}/announcement-evidence")
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


@router.get("/api/stocks/{code}/evidence-search")
def evidence_search(
    code: str,
    q: str = Query(min_length=1, max_length=500),
    as_of: str | None = None,
    limit: int = Query(20, ge=1, le=100),
) -> dict:
    return research_store.search_document_chunks(code, q, as_of=as_of, limit=limit)


@router.post("/api/document-assistant")
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


@router.post("/api/financial-change-template")
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


@router.post("/api/research-runs", status_code=201)
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


@router.get("/api/stocks/{code}/research-runs")
def list_research_runs(code: str, limit: int = Query(20, ge=1, le=100)) -> dict:
    normalized = normalize_code(code)
    return {"items": research_store.list_research_runs(normalized, limit)}


@router.get("/api/research-runs/{run_id}")
def research_run(run_id: str) -> dict:
    item = research_store.research_run(run_id)
    if item is None:
        raise HTTPException(status_code=404, detail="研究任务不存在")
    return item


@router.get("/api/stocks/{code}/research-assessment")
def latest_research_assessment(code: str, as_of: str | None = None) -> dict:
    item = research_store.latest_research_assessment(normalize_code(code), as_of)
    if item is None:
        raise HTTPException(status_code=404, detail="该证券尚无可用 ResearchAssessment")
    return item


@router.get("/api/research-candidates")
async def research_candidates() -> dict:
    items = quant_store.list_research_candidates()
    warning = await hydrate_security_names(items)
    if any(item.get("security_name") in {None, item["security_code"]} for item in items):
        items = quant_store.list_research_candidates()
    return {"items": items, "name_sync_warning": warning}


@router.post("/api/research-candidates", status_code=201)
async def add_research_candidate(request: ResearchCandidateCreate) -> dict:
    try:
        candidate = quant_store.add_research_candidate(
            request.code, request.snapshot_id, request.note
        )
        await hydrate_security_names([candidate])
        return next(
            item
            for item in quant_store.list_research_candidates()
            if item["security_code"] == candidate["security_code"]
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete(
    "/api/research-candidates/{code}",
    status_code=204,
    response_class=Response,
    response_model=None,
)
def remove_research_candidate(code: str) -> None:
    if not quant_store.remove_research_candidate(code):
        raise HTTPException(status_code=404, detail="候选证券不存在")


@router.post("/api/stocks/{code}/financial-facts/refresh")
def refresh_financial_facts(code: str, as_of: str | None = None) -> dict:
    analysis = build_analysis(code, as_of)
    result = research_store.refresh_financial_facts(
        analysis["security"]["code"], analysis["as_of"]
    )
    result["items"] = research_store.financial_fact_evidence(
        analysis["security"]["code"], analysis["as_of"]
    )
    return result


@router.get("/api/stocks/{code}/financial-facts")
def financial_facts(code: str, as_of: str | None = None) -> dict:
    normalized = normalize_code(code)
    return {"items": research_store.financial_fact_evidence(normalized, as_of)}


@router.get("/api/stocks/{code}/assistant-history")
def assistant_history(code: str, limit: int = Query(20, ge=1, le=100)) -> dict:
    normalized = normalize_code(code)
    return {"items": research_store.list_interactions(normalized, limit)}


@router.get("/api/research-interactions/{interaction_id}")
def research_interaction(interaction_id: str) -> dict:
    item = research_store.interaction(interaction_id)
    if item is None:
        raise HTTPException(status_code=404, detail="研究记录不存在")
    return item


@router.post("/api/stocks/{code}/announcements/sync")
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
    except IFindDataError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/api/documents/{document_id}/file", include_in_schema=False)
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
