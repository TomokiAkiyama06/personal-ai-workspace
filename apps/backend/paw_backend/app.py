"""Application factory."""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from paw_backend import __version__
from paw_backend.api.v1 import router as api_v1
from paw_backend.authz import install_authz
from paw_backend.authz.diagnostics import warn_about_loose_privileges
from paw_backend.config import Settings
from paw_backend.db import Database
from paw_backend.errors import ERROR_RESPONSES, register_error_handlers
from paw_backend.events import EventBus, publish_heartbeats
from paw_backend.identity.diagnostics import warn_if_tokens_can_be_minted
from paw_backend.middleware import (
    HostValidationMiddleware,
    RequestIdMiddleware,
    SecurityHeadersMiddleware,
)


def create_app(
    settings: Settings | None = None,
    *,
    database: Database | None = None,
    event_bus: EventBus | None = None,
) -> FastAPI:
    """Build the FastAPI application.

    ``database`` and ``event_bus`` can be injected (tests do); by default they
    are built from ``settings``, which itself defaults to the environment.
    """
    settings = settings or Settings()
    database = database or Database(settings)
    event_bus = event_bus or EventBus(
        settings.event_queue_size, settings.event_max_subscribers
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        heartbeat = asyncio.create_task(
            publish_heartbeats(event_bus, settings.event_heartbeat_seconds)
        )
        # One background check (PostgreSQL may be down at startup): warn if the
        # application's database user could rewrite the audit trail or the tool
        # approvals.
        audit_check = asyncio.create_task(
            warn_about_loose_privileges(database, settings.database_timeout_seconds)
        )
        # Likewise: warn if that user could mint an Owner token (PAW-021).
        token_check = asyncio.create_task(
            warn_if_tokens_can_be_minted(database, settings.database_timeout_seconds)
        )
        try:
            yield
        finally:
            # Cancelling aborts each diagnostic's own connection (it does not wait
            # for a stalled server to answer), and the wait is bounded anyway.
            for check in (audit_check, token_check):
                check.cancel()
            await asyncio.wait(
                {audit_check, token_check}, timeout=settings.shutdown_timeout_seconds
            )
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            await database.dispose()

    app = FastAPI(
        title="Personal AI Workspace Backend",
        version=__version__,
        lifespan=lifespan,
        # Only the machine-readable schema is served (Web / CLI clients use
        # it). Swagger UI / ReDoc would load scripts from a public CDN.
        openapi_url="/api/v1/openapi.json",
        docs_url=None,
        redoc_url=None,
        responses=ERROR_RESPONSES,
    )
    app.state.settings = settings
    app.state.database = database
    app.state.event_bus = event_bus
    install_authz(app, settings=settings, database=database)

    register_error_handlers(app)
    # Added last = outermost. Request ID wraps everything, so the middleware
    # inside it can read the ID and every response carries it; the security
    # headers also cover the Host-validation error.
    app.add_middleware(HostValidationMiddleware, allowed_hosts=settings.allowed_hosts)
    app.add_middleware(
        SecurityHeadersMiddleware,
        hsts_max_age_seconds=settings.hsts_max_age_seconds,
        tls_enabled=settings.tls_enabled,
    )
    app.add_middleware(RequestIdMiddleware)

    app.include_router(api_v1)
    return app
