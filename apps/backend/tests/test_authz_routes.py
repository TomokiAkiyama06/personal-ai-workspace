"""Every ``/api/v1`` route operation is guarded by ``require_capability`` or listed.

An operation is one HTTP method on a path (``("POST", "/api/v1/items")``) or one
WebSocket endpoint (``(WEBSOCKET, path)``). The guards of each operation are
tracked on their own, so a guarded ``GET`` never vouches for an unguarded
``POST`` of the same path, and a public ``GET`` does not make the path's other
methods public. A new endpoint that forgets its guard fails this test. Adding an
operation to ``PUBLIC_ROUTES`` is a deliberate, reviewable act: say why next to
it.
"""

import unittest

from fastapi import APIRouter, Depends, FastAPI
from starlette.routing import WebSocketRoute

from paw_backend.app import create_app
from paw_backend.authz import Capability, require_capability

from .support import FakeDatabase, make_settings

WEBSOCKET = "WEBSOCKET"

# (method, path); WEBSOCKET stands for the protocol of a WebSocket route.
PUBLIC_ROUTES = {
    # Liveness / readiness probes and the schema: no user data.
    ("GET", "/api/v1/health"),
    ("GET", "/api/v1/health/ready"),
    ("GET", "/api/v1/openapi.json"),
    # TODO(PAW-022): unauthenticated until Login / Session exists; they carry
    # system events only (see apps/backend/README.md). PAW-022 must guard
    # them with require_capability and remove them from this list.
    ("GET", "/api/v1/events/stream"),
    (WEBSOCKET, "/api/v1/events/ws"),
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


def operations(route) -> set[str]:
    """The HTTP methods a route answers, or ``{WEBSOCKET}`` for a WebSocket route.

    Starlette adds ``HEAD`` next to ``GET`` on its own (same handler, so the
    same guards): it is not a separate operation. A route without a method list
    (a ``Mount``, say) is one wildcard operation, never silently skipped.
    """
    if isinstance(route, WebSocketRoute):
        return {WEBSOCKET}
    methods = getattr(route, "methods", None)
    if not methods:
        return {"*"}
    methods = {method.upper() for method in methods}
    return methods - {"HEAD"} if "GET" in methods else methods


def route_guards(app: FastAPI) -> list[tuple[str, str, frozenset[Capability]]]:
    """Every route operation of the app as ``(method, path, guards)``.

    One entry per route AND method, in registration order and never merged:
    two routes for the same operation (the second is shadowed) and two methods
    of one path are each checked on their own.

    FastAPI (pinned at an exact version) nests routers added with
    ``include_router`` in a wrapper whose ``effective_route_contexts()`` lists
    them with their full path and inherited dependencies; a WebSocket route
    keeps its full path on ``starlette_route``.
    """
    result: list[tuple[str, str, frozenset[Capability]]] = []
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
                for method in sorted(operations(context.original_route)):
                    result.append((method, path, frozenset(found)))
        else:
            dependant = getattr(route, "dependant", None)
            found = guards(dependant) if dependant is not None else set()
            for method in sorted(operations(route)):
                result.append((method, getattr(route, "path", ""), frozenset(found)))
    return result


def unprotected_routes(app: FastAPI, prefix: str = "/api/v1") -> set[tuple[str, str]]:
    """Operations ``(method, path)`` under ``prefix`` with no guard and not public."""
    return {
        (method, path)
        for method, path, found in route_guards(app)
        if path.startswith(prefix) and (method, path) not in PUBLIC_ROUTES and not found
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

        self.assertEqual(
            unprotected_routes(app),
            {("GET", "/api/v1/oops"), (WEBSOCKET, "/api/v1/oops-ws")},
        )

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
        found = {(method, path) for method, path, _ in route_guards(build_app())}
        self.assertEqual(PUBLIC_ROUTES - found, set())

    def test_a_guarded_method_does_not_vouch_for_an_unguarded_one_on_the_path(self):
        app = build_app()
        guard = Depends(require_capability(Capability.ADMIN_AUDIT_VIEW))
        router = APIRouter(prefix="/api/v1/nested")

        @app.get("/api/v1/items", dependencies=[guard])
        async def read_items() -> dict[str, str]:
            return {}

        @app.post("/api/v1/items")
        async def create_item() -> dict[str, str]:
            return {}

        @router.put("/thing", dependencies=[guard])
        async def put_thing() -> dict[str, str]:
            return {}

        @router.delete("/thing")
        async def delete_thing() -> dict[str, str]:
            return {}

        app.include_router(router)
        self.assertEqual(
            unprotected_routes(app),
            {("POST", "/api/v1/items"), ("DELETE", "/api/v1/nested/thing")},
        )

    def test_one_route_for_several_methods_is_checked_for_each_method(self):
        app = build_app()
        guard = Depends(require_capability(Capability.ADMIN_AUDIT_VIEW))
        app.add_api_route("/api/v1/both", lambda: {}, methods=["GET", "POST"])
        app.add_api_route(
            "/api/v1/both-guarded",
            lambda: {},
            methods=["GET", "POST"],
            dependencies=[guard],
        )
        self.assertEqual(
            unprotected_routes(app),
            {("GET", "/api/v1/both"), ("POST", "/api/v1/both")},
        )

    def test_a_public_operation_does_not_make_the_path_public_for_other_methods(self):
        app = build_app()

        @app.post("/api/v1/health")  # GET /api/v1/health is public, this is not
        async def post_health() -> dict[str, str]:
            return {}

        @app.websocket("/api/v1/health")
        async def health_socket(websocket) -> None:
            await websocket.close()

        self.assertEqual(
            unprotected_routes(app),
            {("POST", "/api/v1/health"), (WEBSOCKET, "/api/v1/health")},
        )

    def test_a_shadowed_duplicate_operation_is_checked_on_its_own(self):
        app = build_app()
        guard = Depends(require_capability(Capability.ADMIN_AUDIT_VIEW))

        @app.get("/api/v1/twice", dependencies=[guard])
        async def first() -> dict[str, str]:
            return {}

        @app.get("/api/v1/twice")
        async def second() -> dict[str, str]:
            return {}

        self.assertEqual(unprotected_routes(app), {("GET", "/api/v1/twice")})

    def test_a_mounted_app_is_reported_not_silently_skipped(self):
        app = build_app()

        async def sub_app(scope, receive, send) -> None:
            raise AssertionError("not called")

        app.mount("/api/v1/mounted", sub_app)
        self.assertEqual(unprotected_routes(app), {("*", "/api/v1/mounted")})

    def test_head_is_not_a_separate_operation_of_a_get_route(self):
        methods = {method for method, _, _ in route_guards(build_app())}
        self.assertEqual(methods, {"GET", WEBSOCKET})


if __name__ == "__main__":
    unittest.main()
