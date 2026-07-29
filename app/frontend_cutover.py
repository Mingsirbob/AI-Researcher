from pathlib import Path

from fastapi.responses import JSONResponse, RedirectResponse, Response


def build_frontend_status(*, frontend_dist: Path) -> dict:
    vue_built = (frontend_dist / "index.html").is_file()
    return {
        "configured_default": "vue",
        "active_default": "vue" if vue_built else "unavailable",
        "vue_built": vue_built,
        "vue_entry": "/next/paper",
        "fallback_reason": "vue_build_missing" if not vue_built else None,
    }


def production_frontend_response(*, frontend_dist: Path) -> Response:
    status = build_frontend_status(frontend_dist=frontend_dist)
    if status["active_default"] == "vue":
        return RedirectResponse(url="/next/paper", status_code=307)
    return JSONResponse(
        status_code=503,
        content={
            "detail": "Vue 前端尚未构建，请先在 frontend 目录运行 npm ci && npm run build",
        },
    )
