"""Helpers for the authorization tests (not a test module: no ``test_`` prefix)."""

import asyncio
import uuid
from collections.abc import Mapping
from typing import Annotated

from fastapi import Depends, FastAPI, WebSocket
from starlette.requests import HTTPConnection

from paw_backend.app import create_app
from paw_backend.authz import (
    Capability,
    InMemoryAuditSink,
    Principal,
    ProjectRole,
    ProjectState,
    Resource,
    SystemRole,
    install_authz,
    require_capability,
)

from .support import FakeDatabase, make_settings

SECRET = "hunter2-secret-connection-detail"


def uid(number: int) -> uuid.UUID:
    """A readable, deterministic UUID for tests."""
    return uuid.UUID(int=number)


U1, U2, U3 = uid(1), uid(2), uid(3)
P1, P2, P3 = uid(101), uid(102), uid(103)
AGENT = uid(201)
REPO = uid(301)
CHAT = uid(401)


def principal(
    system_role: SystemRole = SystemRole.USER,
    user_id: uuid.UUID = U1,
    projects: Mapping[uuid.UUID, ProjectRole] | None = None,
) -> Principal:
    """``principal(SystemRole.USER, projects={P1: ProjectRole.MANAGER})``."""
    return Principal(user_id, system_role, projects or {})


def project(
    project_id: uuid.UUID = P1, state: ProjectState = ProjectState.ACTIVE
) -> Resource:
    return Resource.project(project_id, state)


class StaticProvider:
    """Always yields the same principal (or nobody)."""

    def __init__(self, who: Principal | None) -> None:
        self.who = who

    async def get_principal(self, connection: HTTPConnection) -> Principal | None:
        return self.who


class StaticDirectory:
    """A user store the test can change between two decisions."""

    def __init__(self, *principals: Principal) -> None:
        self.principals = {p.user_id: p for p in principals}
        self.lookups = 0

    async def get_principal_by_id(self, user_id: uuid.UUID) -> Principal | None:
        self.lookups += 1
        return self.principals.get(user_id)


class FailingSink:
    """A sink whose write fails with a message that must never leak."""

    def __init__(self) -> None:
        self.attempts = 0

    async def record(self, event) -> None:
        self.attempts += 1
        raise ConnectionError(SECRET)


class HangingSink:
    """A sink that is very slow, to exercise the write timeout.

    It sleeps for a bounded 5 seconds (not forever) so that a missing timeout
    makes a test fail instead of hanging the whole run.
    """

    async def record(self, event) -> None:
        await asyncio.sleep(5)


# The stored state of each project, as a project store would answer.
PROJECT_STATES = {P2: ProjectState.ARCHIVED, P3: ProjectState.PENDING_DELETION}


def project_resource(connection: HTTPConnection) -> Resource:
    return Resource.project(connection.path_params["project_id"], ProjectState.ACTIVE)


async def stored_project_resource(connection: HTTPConnection) -> Resource:
    """Async resolver: reads the state of the project (here from a dict)."""
    await asyncio.sleep(0)
    project_id = connection.path_params["project_id"]
    state = PROJECT_STATES.get(uuid.UUID(project_id), ProjectState.ACTIVE)
    return Resource.project(project_id, state)


def broken_resource(connection: HTTPConnection) -> Resource:
    raise RuntimeError(SECRET)


def add_test_routes(app: FastAPI) -> list[str]:
    """Add test-only protected routes; returns the handler-call log."""
    calls: list[str] = []

    @app.get(
        "/test/admin",
        dependencies=[Depends(require_capability(Capability.ADMIN_USERS_MANAGE))],
    )
    async def admin_route() -> dict[str, str]:
        calls.append("admin")
        return {"ok": "admin"}

    @app.get(
        "/test/shared-memory",
        dependencies=[Depends(require_capability(Capability.SHARED_MEMORY_READ))],
    )
    async def shared_memory_route() -> dict[str, str]:
        calls.append("shared")
        return {"ok": "shared"}

    @app.get(
        "/test/projects/{project_id}/tasks",
        dependencies=[
            Depends(require_capability(Capability.PROJECT_TASK_RUN, project_resource))
        ],
    )
    async def task_route(project_id: str) -> dict[str, str]:
        calls.append(f"task:{project_id}")
        return {"ok": project_id}

    @app.get(
        "/test/stored/{project_id}/tasks",
        dependencies=[
            Depends(
                require_capability(Capability.PROJECT_TASK_RUN, stored_project_resource)
            )
        ],
    )
    async def stored_task_route(project_id: str) -> dict[str, str]:
        calls.append(f"stored:{project_id}")
        return {"ok": project_id}

    @app.get(
        "/test/broken/{project_id}",
        dependencies=[
            Depends(require_capability(Capability.PROJECT_READ, broken_resource))
        ],
    )
    async def broken_route(project_id: str) -> dict[str, str]:
        calls.append("broken")
        return {}

    @app.get(
        "/test/two",
        dependencies=[
            Depends(require_capability(Capability.ADMIN_USAGE_VIEW)),
            Depends(require_capability(Capability.ADMIN_AUDIT_VIEW)),
        ],
    )
    async def two_route() -> dict[str, str]:
        calls.append("two")
        return {}

    @app.websocket("/test/ws")
    async def ws_route(
        websocket: WebSocket,
        _: Annotated[
            Principal, Depends(require_capability(Capability.SHARED_MEMORY_READ))
        ],
    ) -> None:
        await websocket.accept()
        await websocket.send_json({"hello": "member"})
        await websocket.close()

    @app.websocket("/test/ws-admin")
    async def ws_admin_route(
        websocket: WebSocket,
        _: Annotated[
            Principal, Depends(require_capability(Capability.ADMIN_USERS_MANAGE))
        ],
    ) -> None:
        await websocket.accept()
        await websocket.send_json({"hello": "admin"})
        await websocket.close()

    return calls


def make_test_app(
    who: Principal | None = None,
    sink=None,
    *,
    default_provider: bool = False,
    directory=None,
    **settings_overrides,
) -> tuple[FastAPI, InMemoryAuditSink, list[str]]:
    """An app with test-only protected routes; returns (app, sink, handler calls).

    ``default_provider=True`` keeps the provider ``create_app`` installs (the
    production default: nobody is authenticated).
    """
    settings = make_settings(**settings_overrides)
    database = FakeDatabase()
    app = create_app(settings, database=database)
    memory_sink = InMemoryAuditSink()
    install_authz(
        app,
        settings=settings,
        database=database,
        principal_provider=None if default_provider else StaticProvider(who),
        principal_directory=directory,
        audit_sink=sink or memory_sink,
    )
    return app, memory_sink, add_test_routes(app)
