"""Helpers for the authorization tests (not a test module: no ``test_`` prefix)."""

import asyncio

from fastapi import Depends, FastAPI, Request
from starlette.requests import HTTPConnection

from paw_backend.app import create_app
from paw_backend.authz import (
    Capability,
    InMemoryAuditSink,
    Principal,
    ProjectRole,
    Resource,
    SystemRole,
    install_authz,
    require_capability,
)

from .support import FakeDatabase, make_settings

SECRET = "hunter2-secret-connection-detail"


def principal(
    system_role: SystemRole = SystemRole.USER,
    user_id: str = "u1",
    **project_roles: ProjectRole,
) -> Principal:
    """``principal(SystemRole.USER, p1=ProjectRole.MANAGER)`` is a Manager of p1."""
    return Principal(user_id, system_role, project_roles)


class StaticProvider:
    """Always yields the same principal (or nobody)."""

    def __init__(self, who: Principal | None) -> None:
        self.who = who

    async def get_principal(self, connection: HTTPConnection) -> Principal | None:
        return self.who


class FailingSink:
    """A sink whose write fails with a message that must never leak."""

    def __init__(self) -> None:
        self.attempts = 0

    async def record(self, event) -> None:
        self.attempts += 1
        raise ConnectionError(SECRET)


class HangingSink:
    """A sink that never returns, to exercise the write timeout."""

    async def record(self, event) -> None:
        await asyncio.sleep(3600)


def project_resource(request: Request) -> Resource:
    return Resource.project(request.path_params["project_id"])


def add_test_routes(app: FastAPI) -> list[str]:
    """Add three test-only protected routes; returns the handler-call log."""
    calls: list[str] = []

    @app.get(
        "/test/admin",
        dependencies=[Depends(require_capability(Capability.ADMIN_USERS_MANAGE))],
    )
    async def admin_route() -> dict[str, str]:
        calls.append("admin")
        return {"ok": "admin"}

    @app.get(
        "/test/chat",
        dependencies=[Depends(require_capability(Capability.SHARED_MEMORY_READ))],
    )
    async def chat_route() -> dict[str, str]:
        calls.append("chat")
        return {"ok": "chat"}

    @app.get(
        "/test/projects/{project_id}/tasks",
        dependencies=[
            Depends(require_capability(Capability.PROJECT_TASK_RUN, project_resource))
        ],
    )
    async def task_route(project_id: str) -> dict[str, str]:
        calls.append(f"task:{project_id}")
        return {"ok": project_id}

    return calls


def make_test_app(
    who: Principal | None = None,
    sink=None,
    *,
    default_provider: bool = False,
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
        audit_sink=sink or memory_sink,
    )
    return app, memory_sink, add_test_routes(app)
