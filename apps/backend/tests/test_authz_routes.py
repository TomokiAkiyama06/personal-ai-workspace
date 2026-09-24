"""Every ``/api/v1`` route must be guarded by ``require_capability`` or listed here.

A new endpoint that forgets its guard fails this test. Adding a path to
``PUBLIC_ROUTES`` is a deliberate, reviewable act: say why next to it.
"""

import unittest

from fastapi import APIRouter, Depends, FastAPI

from paw_backend.app import create_app
from paw_backend.authz import Capability, require_capability

from .support import FakeDatabase, make_settings

PUBLIC_ROUTES = {
    # Liveness / readiness probes and the schema: no user data.
    "/api/v1/health",
    "/api/v1/health/ready",
    "/api/v1/openapi.json",
    # TODO(PAW-022): unauthenticated until Login / Session exists; they carry
    # system events only (see apps/backend/README.md). PAW-022 must guard
    # them with require_capability and remove them from this list.
    "/api/v1/events/stream",
    "/api/v1/events/ws",
}


def guards(dependant) -> set[Capability]:
    """The capabilities required by a route's dependency tree."""
    found: set[Capability] = set()
    for dependency in dependant.dependencies:
        capability = getattr(dependency.call, "paw_capability", None)
        if capability is not None:
            found.add(capability)
        found |= guards(dependency)
    return found


def route_guards(app: FastAPI) -> dict[str, set[Capability]]:
    """Every route path of the app mapped to the capabilities that guard it.

    FastAPI (pinned at an exact version) nests routers added with
    ``include_router`` in a wrapper whose ``effective_route_contexts()`` lists
    them with their full path and inherited dependencies; a WebSocket route
    keeps its full path on ``starlette_route``.
    """
    result: dict[str, set[Capability]] = {}
    for route in app.routes:
        if hasattr(route, "effective_route_contexts"):
            for context in route.effective_route_contexts():
                path = context.path or getattr(context.starlette_route, "path", "")
                found: set[Capability] = set()
                for dependant in (
                    context.dependant,
                    getattr(context.original_route, "dependant", None),
                ):
                    if dependant is not None:
                        found |= guards(dependant)
                for depends in context.dependencies or []:
                    capability = getattr(depends.dependency, "paw_capability", None)
                    if capability is not None:
                        found.add(capability)
                result.setdefault(path, set()).update(found)
        else:
            dependant = getattr(route, "dependant", None)
            found = guards(dependant) if dependant is not None else set()
            result.setdefault(getattr(route, "path", ""), set()).update(found)
    return result


def unprotected_routes(app: FastAPI, prefix: str = "/api/v1") -> set[str]:
    """Paths under ``prefix`` that have no guard and are not public."""
    return {
        path
        for path, found in route_guards(app).items()
        if path.startswith(prefix) and path not in PUBLIC_ROUTES and not found
    }


def build_app() -> FastAPI:
    return create_app(make_settings(), database=FakeDatabase())


class RouteInventoryTest(unittest.TestCase):
    def test_every_api_route_is_guarded_or_explicitly_public(self):
        self.assertEqual(unprotected_routes(build_app()), set())

    def test_the_inventory_finds_a_route_that_forgot_its_guard(self):
        app = build_app()

        @app.get("/api/v1/oops")
        async def oops() -> dict[str, str]:
            return {}

        @app.websocket("/api/v1/oops-ws")
        async def oops_ws(websocket) -> None:
            await websocket.close()

        self.assertEqual(unprotected_routes(app), {"/api/v1/oops", "/api/v1/oops-ws"})

    def test_the_inventory_recognises_guards_however_they_are_attached(self):
        app = build_app()
        guard = Depends(require_capability(Capability.ADMIN_AUDIT_VIEW))
        router = APIRouter(prefix="/api/v1/guarded", dependencies=[guard])

        @router.get("/by-router")
        async def by_router() -> dict[str, str]:
            return {}

        @app.get("/api/v1/by-route", dependencies=[guard])
        async def by_route() -> dict[str, str]:
            return {}

        @app.get("/api/v1/by-parameter")
        async def by_parameter(_=guard) -> dict[str, str]:  # noqa: B008
            return {}

        app.include_router(router)
        self.assertEqual(unprotected_routes(app), set())

    def test_the_public_list_has_no_stale_entries(self):
        # Also proves the traversal really sees the routers included by the app.
        self.assertEqual(PUBLIC_ROUTES - set(route_guards(build_app())), set())


if __name__ == "__main__":
    unittest.main()
