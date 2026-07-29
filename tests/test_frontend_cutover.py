from fastapi.responses import FileResponse, JSONResponse

from app.frontend_cutover import build_frontend_status, production_frontend_response


def test_vue_frontend_serves_root_when_build_exists(tmp_path):
    dist = tmp_path / "frontend" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<div id='app'></div>", encoding="utf-8")

    status = build_frontend_status(frontend_dist=dist)
    response = production_frontend_response(frontend_dist=dist)

    assert status["active_default"] == "vue"
    assert isinstance(response, FileResponse)
    assert response.path == dist / "index.html"


def test_vue_frontend_returns_service_unavailable_when_build_is_missing(tmp_path):
    dist = tmp_path / "missing-dist"
    status = build_frontend_status(frontend_dist=dist)
    response = production_frontend_response(frontend_dist=dist)

    assert status == {
        "configured_default": "vue",
        "active_default": "unavailable",
        "vue_built": False,
        "vue_entry": "/",
        "fallback_reason": "vue_build_missing",
    }
    assert isinstance(response, JSONResponse)
    assert response.status_code == 503
