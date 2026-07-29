from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .api.handlers import (
    container,
    frontend_dist,
    lifespan,
)
from .api.routers import include_domain_routers
from .container import AppContainer


def create_app(app_container: AppContainer = container) -> FastAPI:
    application = FastAPI(
        title="Evidence AI Research", version="0.9.0", lifespan=lifespan
    )
    application.state.container = app_container
    if (frontend_dist / "assets").is_dir():
        application.mount(
            "/assets",
            StaticFiles(directory=frontend_dist / "assets"),
            name="frontend-assets",
        )
    application.state.domain_routers = include_domain_routers(application)
    return application


app = create_app()
domain_routers = app.state.domain_routers
