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
from paw_backend.compute import FullGpuMode, PostgresTaskHolds
from paw_backend.compute.probe import NvidiaSmiProbe
from paw_backend.compute.wiring import (
    ComputeServices,
    ComputeSetup,
    FullGpuController,
    LocalRuntime,
    build_compute,
)
from paw_backend.config import Settings
from paw_backend.db import Database
from paw_backend.errors import ERROR_RESPONSES, register_error_handlers
from paw_backend.events import EventBus, publish_heartbeats
from paw_backend.health.wiring import build_system_health
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
from paw_backend.orchestrator.user_sweep import build_user_stop_loop
from paw_backend.projects import ProjectService, ProjectStateGate
from paw_backend.repositories import (
    RepositoryPolicy,
    RepositoryService,
    SubprocessGitRunner,
)
from paw_backend.repositories.git import GitRunner
from paw_backend.repositories.github_connection import GhRunner
from paw_backend.research.scratch import ScratchJanitor, ScratchStore
from paw_backend.web import WebAppMiddleware

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
    compute: ComputeSetup | None = None,
    local_runtimes: Mapping[str, LocalRuntime] | None = None,
    gh_runner: GhRunner | None = None,
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

    ``compute`` turns on the Compute Resource Scheduler (issue #165, Decision
    0058, Proposed): the process's ``ComputeScheduler`` is built from it
    (``app.state.compute``), the lifespan runs its ``serve`` and, with a
    database, Kaggle / Full GPU Mode (``FullGpuMode.serve``; the HTTP routes of
    ``api/v1/compute.py``). ``local_runtimes`` (with ``compute`` and
    ``orchestrator_config``) are the orchestrator runtimes on a local model; the
    composition wraps them in a ``HybridRuntime`` on that scheduler. Without
    ``compute`` there is no scheduler and the routes answer 503.

    With a database, the project and repository services of the HTTP routes
    (issue #184, ``api/v1/projects.py``) are ``app.state.projects`` and
    ``app.state.repositories``; the repository service runs git through
    ``git_runner`` (as above) and creates GitHub repositories through ``gh_runner``
    (``SubprocessGhRunner``; without one, creating a GitHub repository answers
    ``github_unavailable``). Without a database both are ``None`` (503).

    System Health (PAW-066) is ``app.state.system_health``: its monitor serves
    ``/api/v1/system/health*`` and, with a database, samples the metrics in the
    lifespan. It reports the Compute Scheduler built from ``compute`` and, while
    the lifespan runs it, Full GPU Mode; without ``compute``, the read-only GPU
    probe when ``PAW_HEALTH_GPU_PROBE`` is set.
    """
    settings = settings or Settings()
    database = database or Database(settings)
    event_bus = event_bus or EventBus(
        settings.event_queue_size, settings.event_max_subscribers
    )
    auth = auth or build_auth(settings, database)
    if local_runtimes is not None and compute is None:
        raise TypeError("local_runtimes need compute")
    compute_services: ComputeServices | None = None
    if compute is not None:
        scheduler, vram_warnings = build_compute(compute)
        compute_services = ComputeServices(compute, scheduler, vram_warnings)

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
        user_stop_loop = None
        reaper = None
        maintenance = None
        health = app.state.system_health
        compute_stop = asyncio.Event()
        try:
            # The Compute Resource Scheduler, when the deployment configured one
            # (issue #165): its refresh loop, and Kaggle / Full GPU Mode next to
            # it (holding tasks needs the task lifecycle, so a database).
            if compute_services is not None:
                background.add(
                    asyncio.create_task(
                        compute_services.scheduler.serve(
                            compute_stop,
                            interval=compute_services.setup.refresh_seconds,
                        )
                    )
                )
                if task_execution is not None:
                    # The authorizer of the application, read now (tests swap it).
                    mode = FullGpuMode(
                        compute_services.scheduler,
                        PostgresTaskHolds(task_execution.tasks, task_execution.queue),
                        app.state.authorizer,
                        clock=compute_services.setup.clock,
                    )
                    compute_services.full_gpu = FullGpuController(
                        mode, compute_services.scheduler
                    )
                    background.add(asyncio.create_task(mode.serve(compute_stop)))
                    # System Health reports Full GPU Mode while it runs.
                    health.compute.attach(
                        compute_services.scheduler, compute_services.full_gpu
                    )
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
            # Tasks of a user whose deletion began are stopped the same way
            # (Issue #127, Decision 0043), also through the application's own
            # task service (#125).
            if database.configured and settings.user_task_stop_interval_seconds > 0:
                user_stop_loop = build_user_stop_loop(
                    database,
                    project_gate=ProjectStateGate(),
                    interval_seconds=settings.user_task_stop_interval_seconds,
                    tasks=task_execution.tasks,
                )
                background.add(asyncio.create_task(user_stop_loop.run()))
            # Calls through a shared connection that a crashed process left
            # ``in_flight`` are settled as failed (PAW-034, Decision 0016).
            if database.configured and settings.connection_reap_interval_seconds > 0:
                reaper = build_connection_reaper(
                    database,
                    interval_seconds=settings.connection_reap_interval_seconds,
                )
                background.add(asyncio.create_task(reaper.run()))
                health.reaper.attach(reaper)
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
            # The System Health history (PAW-066): sample, roll up, purge.
            if health.sampling:
                background.add(asyncio.create_task(health.monitor.run()))
            yield
        finally:
            if compute_services is not None and compute_services.full_gpu is not None:
                # A start in progress is abandoned: the scheduler goes back to
                # normal, the models it unloaded are loaded again and the held
                # tasks resume, while the loops still run (Codex review #168).
                # A Full GPU Mode that is on ends with the process.
                try:
                    await asyncio.wait_for(
                        compute_services.full_gpu.close(),
                        settings.shutdown_timeout_seconds,
                    )
                except TimeoutError:
                    logger.warning(
                        "The models were not back after an abandoned Full GPU Mode "
                        "start within the shutdown timeout: the next process "
                        "resumes the held tasks"
                    )
                except Exception as error:
                    logger.warning(
                        "Full GPU Mode could not be closed (%s)", type(error).__name__
                    )
                compute_services.full_gpu = None
                health.compute.attach(compute_services.scheduler)
            compute_stop.set()
            health.monitor.stop()
            health.reaper.attach(None)
            if stop_loop is not None:
                stop_loop.stop()
            if user_stop_loop is not None:
                user_stop_loop.stop()
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
            scheduler=None if compute_services is None else compute_services.scheduler,
            local_runtimes=local_runtimes,
        )
    elif local_runtimes is not None:
        raise TypeError("local_runtimes need a database")
    app.state.task_execution = task_execution
    # The project / repository services of the HTTP routes (issue #184).
    app.state.projects = None
    app.state.repositories = None
    if database.configured:
        app.state.projects = ProjectService(database, app.state.authorizer)
        app.state.repositories = RepositoryService.from_policy(
            database,
            app.state.authorizer,
            git_runner if git_runner is not None else SubprocessGitRunner(),
            RepositoryPolicy.from_settings(settings),
            gh_runner=gh_runner,
        )
    elif gh_runner is not None:
        raise TypeError("gh_runner needs a database")
    app.state.compute = compute_services
    probe = NvidiaSmiProbe() if settings.health_gpu_probe and compute is None else None
    app.state.system_health = build_system_health(
        settings,
        database,
        compute=None if compute_services is None else compute_services.scheduler,
        probe=probe,
    )

    register_error_handlers(app)
    # Innermost: the built Web App (Decision 0044), only when configured. It answers
    # GET / HEAD outside /api only, inside every check and header below.
    if settings.web_dist_dir is not None:
        app.add_middleware(WebAppMiddleware, dist_dir=settings.web_dist_dir)
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
