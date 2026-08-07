import hashlib
import json

import app.main as main_module
from app.api.routers.registry import DOMAIN_MODULES
from app.container import AppContainer
from app.main import app, domain_routers


OPENAPI_BASELINE_SHA256 = (
    "2d878254c8c0bc7d93a905107c8ba2b0aa11435141627a6fe8b512e377c85725"
)


def test_openapi_contract_matches_p0_baseline():
    schema = app.openapi()
    canonical = json.dumps(
        schema, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")

    assert len(schema["paths"]) == 104
    assert hashlib.sha256(canonical).hexdigest() == OPENAPI_BASELINE_SHA256


def test_every_route_is_owned_by_exactly_one_domain_router():
    registered = [route for router in domain_routers.values() for route in router.routes]
    assert len(registered) == len({id(route) for route in registered})
    assert set(domain_routers) == {name for name, _ in DOMAIN_MODULES}
    assert all(domain_routers[name].routes for name, _ in DOMAIN_MODULES)
    assert all(route.endpoint.__name__ not in vars(main_module) for route in registered)


def test_application_exposes_container_through_fastapi_state():
    assert isinstance(app.state.container, AppContainer)
