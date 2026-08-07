from fastapi import HTTPException

from app.schemas import (
    StrategyCodeUpdate,
    StrategyDraftCreate,
    StrategyDraftUpdate,
    StrategyGenerateRequest,
)
from .base import domain_router
from ..handlers import strategy_service

router = domain_router()


def owns(path: str) -> bool:
    return path.startswith("/api/strategy-")


@router.get("/api/strategy-components")
def strategy_components() -> dict:
    return strategy_service.components()


@router.get("/api/strategy-drafts")
def strategy_drafts() -> dict:
    return {"items": strategy_service.drafts()}


@router.post("/api/strategy-drafts", status_code=201)
def create_strategy_draft(request: StrategyDraftCreate) -> dict:
    try:
        return strategy_service.create_draft(**request.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/strategy-generate", status_code=201)
async def generate_strategy(request: StrategyGenerateRequest) -> dict:
    try:
        return await strategy_service.generate_draft(**request.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/api/strategy-drafts/{draft_id}")
def strategy_draft(draft_id: str) -> dict:
    try:
        return strategy_service.draft(draft_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.patch("/api/strategy-drafts/{draft_id}")
def update_strategy_draft(draft_id: str, request: StrategyDraftUpdate) -> dict:
    try:
        return strategy_service.update_draft(draft_id, request.definition)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.put("/api/strategy-drafts/{draft_id}/code")
def update_strategy_code(draft_id: str, request: StrategyCodeUpdate) -> dict:
    try:
        return strategy_service.update_code(draft_id, request.source_code)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/strategy-drafts/{draft_id}/compile")
def compile_strategy_draft(draft_id: str) -> dict:
    try:
        return strategy_service.compile(draft_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/strategy-drafts/{draft_id}/publish", status_code=201)
def publish_strategy_draft(draft_id: str) -> dict:
    try:
        return strategy_service.publish(draft_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/api/strategy-versions")
def strategy_versions() -> dict:
    return {"items": strategy_service.versions()}
