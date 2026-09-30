"""Application factory."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

from fastapi import FastAPI

from paw_backend import __version__
from paw_backend.api.v1 import router as api_v1
from paw_backend.auth.body_limit import AuthBodyLimitMiddleware
from paw_backend.auth.csrf import OriginCheckMiddleware
from paw_backend.auth.limits import AUTH_BODY_MAX_BYTES
from paw_backend.auth.wiring import AuthServices, build_auth, install_auth
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
from paw_backend.orchestrator.composition import TaskExecution, build_task_execution
from paw_backend.orchestrator.config import OrchestratorConfig
from paw_backend.orchestrator.connection_reaper import build_connection_reaper
from paw_backend.orchestrator.freshness_loop import build_freshness_loop
from paw_backend.orchestrator.project_sweep import build_project_stop_loop
from paw_backend.orchestrator.runtime import AgentRuntime
from paw_backend.projects import ProjectStateGate
from paw_backend.repositories.git import GitRunner
from paw_backend.research.scratch import ScratchJanitor, ScratchStore

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    database: Database | None = None,
    event_bus: EventBus | None = None,
    auth: AuthServices | None = None,
    agent_runtimes: Mapping[str, AgentRuntime] | None = None,
    orchestrator_config: OrchestratorConfig | None = None,
    git_runner: GitRunner | None = None,
) -> FastAPI:
    """Build the FastAPI application.

    ``database``, ``event_bus`` and ``auth`` (the authentication services, with
    their clock) can be injected (tests do); by default they are built from
    ``settings``, which itself defaults to the environment.

    With a configured database the task execution is composed here
    (``app.state.task_execution``, issue #125): the ``TaskService``, the Tool
    Broker, the production ``TaskAuthority`` and, when ``agent_runtimes`` and
    ``orchestrator_config`` are given, the ``Orchestrator`` with its worktrees
    (issue #155): git runs through ``git_runner``, the deployment's ``GitRunner``
    (default ``SubprocessGitRunner``; ``SshGitRunner`` per Decision 0029). Without
    a database it is ``None``.
    """
    settings = settings or Settings()
    database = database or Database(settings)
    event_bus = event_bus or EventBus(
        settings.event_queue_size, settings.event_max_subscribers
    )
    auth = auth or build_auth(settings, database)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await auth.start()
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
        background = {audit_check, token_check}
        stop_loop = None
        reaper = None
        maintenance = None
        try:
            # Expired Research Scratch items are only hidden until something
            # deletes them (PAW-050): purge them regularly, from the start on.
            if database.configured and settings.scratch_purge_interval_seconds > 0:
                janitor = ScratchJanitor(
                    ScratchStore(database),
                    interval_seconds=settings.scratch_purge_interval_seconds,
                )
                background.add(asyncio.create_task(janitor.run()))
            # Tasks of a project whose deletion began are stopped on a schedule,
            # also those created after the deletion request was processed (PAW-034).
            if database.configured and settings.project_task_stop_interval_seconds > 0:
                # The Project state gate is given explicitly (Issue #83, Decision
                # 0020: the task lane requires it): the loop's task service and
                # queue are built with it, never without.
                # A task the loop cancels ends through the application's own
                # task service, whose listener undoes what the task held (#125).
                stop_loop = build_project_stop_loop(
                    database,
                    project_gate=ProjectStateGate(),
                    interval_seconds=settings.project_task_stop_interval_seconds,
                    tasks=task_execution.tasks,
                )
                background.add(asyncio.create_task(stop_loop.run()))
            # Calls through a shared connection that a crashed process left
            # ``in_flight`` are settled as failed (PAW-034, Decision 0016).
            if database.configured and settings.connection_reap_interval_seconds > 0:
                reaper = build_connection_reaper(
                    database,
                    interval_seconds=settings.connection_reap_interval_seconds,
                )
                background.add(asyncio.create_task(reaper.run()))
            # The Memory freshness jobs and the sweep that finishes the cleanup of
            # ended tasks (issue #125, Decision 0047).
            if (
                task_execution is not None
                and settings.freshness_job_interval_seconds > 0
            ):
                maintenance = build_freshness_loop(
                    task_execution,
                    interval_seconds=settings.freshness_job_interval_seconds,
                )
                background.add(asyncio.create_task(maintenance.run()))
            yield
        finally:
            if stop_loop is not None:
                stop_loop.stop()
            if reaper is not None:
                reaper.stop()
            if maintenance is not None:
                maintenance.stop()
            # Cancelling aborts the connection each of them is using (a diagnostic
            # its own, the janitor the one of its purge transaction: neither waits
            # for a stalled server to answer), and the wait is bounded anyway.
            for task in background:
                task.cancel()
            _, pending = await asyncio.wait(
                background, timeout=settings.shutdown_timeout_seconds
            )
            if pending:  # a task that ignored its cancellation: given up on
                logger.warning(
                    "%d background task(s) did not stop within the shutdown timeout",
                    len(pending),
                )
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            await database.dispose()
            auth.close()

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
    install_auth(app, auth, settings=settings, database=database)
    # The task execution uses the application's Authorizer (its principal
    # directory reads the delegating user's current rights).
    task_execution: TaskExecution | None = None
    if database.configured:
        task_execution = build_task_execution(
            settings,
            database,
            app.state.authorizer,
            runtimes=agent_runtimes,
            orchestrator_config=orchestrator_config,
            git_runner=git_runner,
        )
    app.state.task_execution = task_execution

    register_error_handlers(app)
    # Added last = outermost. Request ID wraps everything, so the middleware
    # inside it can read the ID and every response carries it; the security
    # headers also cover the Host-validation error. The Origin check (CSRF) sits
    # inside the Host check: it compares Origin with a Host that is already valid.
    app.add_middleware(AuthBodyLimitMiddleware, max_bytes=AUTH_BODY_MAX_BYTES)
    app.add_middleware(OriginCheckMiddleware, allowed_origins=settings.allowed_origins)
    app.add_middleware(HostValidationMiddleware, allowed_hosts=settings.allowed_hosts)
    app.add_middleware(
        SecurityHeadersMiddleware,
        hsts_max_age_seconds=settings.hsts_max_age_seconds,
        tls_enabled=settings.tls_enabled,
    )
    app.add_middleware(RequestIdMiddleware)

    app.include_router(api_v1)
    return app
