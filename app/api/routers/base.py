from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends

from ..dependencies import get_container


def domain_router() -> APIRouter:
    return APIRouter(dependencies=[Depends(get_container)])


PathMatcher = Callable[[str], bool]
