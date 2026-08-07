from __future__ import annotations

from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute
from starlette.routing import BaseRoute

from . import acceptance, decisions, paper, quant, research, securities, strategy, system, theses


DOMAIN_MODULES = (
    ("securities", securities),
    ("research", research),
    ("quant", quant),
    ("decisions", decisions),
    ("paper", paper),
    ("strategy", strategy),
    ("theses", theses),
    ("acceptance", acceptance),
    ("system", system),
)


def _domain_for(path: str) -> str:
    return next(name for name, module in DOMAIN_MODULES if module.owns(path))


def include_domain_routers(app: FastAPI) -> dict[str, APIRouter]:
    """Compose domain routers and retain any routes registered by extensions."""
    routers = {name: module.router for name, module in DOMAIN_MODULES}
    retained: list[BaseRoute] = []
    for route in app.router.routes:
        if isinstance(route, APIRoute):
            routers[_domain_for(route.path)].routes.append(route)
        else:
            retained.append(route)
    app.router.routes = retained
    for name, _ in DOMAIN_MODULES:
        app.include_router(routers[name])
    return routers
