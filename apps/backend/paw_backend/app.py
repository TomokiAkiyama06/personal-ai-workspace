"""Application factory."""

import asyncio
import contextlib
import logging
import os
import re
import socket
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

from fastapi import FastAPI

from paw_backend import __version__
from paw_backend.agents.cli_session import SystemdLauncher
from paw_backend.agents.codex_collect import (
    SystemdSessionUnits,
    build_codex_collection,
)
from paw_backend.agents.wiring import (
    LocalModelSetup,
    build_local_agent,
    build_preference_interpreter,
)
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
from paw_backend.connections.wiring import (
    build_connection_service,
    build_secret_store,
)
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
from paw_backend.notifications import NotificationStore
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
from paw_backend.sandbox.wiring import SandboxSetup, reap_idle_sandboxes
from paw_backend.web import WebAppMiddleware

logger = logging.getLogger(__name__)


_WORKER_ID_CHARACTER = re.compile(r"[^A-Za-z0-9._:@/-]")


def _retrieve(task: asyncio.Task) -> None:
    """Read a finished task's result (so that no "never retrieved" warning)."""
    if task.done() and not task.cancelled():
        task.exception()


def worker_ids(count: int) -> list[str]:
    """The ids of this process's orchestrator workers (Decision 0086, 3):
    ``<host>:<pid>:<n>``, the host name's other characters as ``-``, at most
    100 characters (``check_worker_id``)."""
    host = _WORKER_ID_CHARACTER.sub("-", socket.gethostname()) or "host"
    if not host[0].isalnum():
        host = "h" + host
    suffix = lambda n: f":{os.getpid()}:{n}"  # noqa: E731
    return [host[: 100 - len(suffix(n))] + suffix(n) for n in range(1, count + 1)]


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
    local_model: LocalModelSetup | None = None,
    sandbox: SandboxSetup | None = None,
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
    0058, Approved): the process's ``ComputeScheduler`` is built from it
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

    ``local_model`` (with ``compute``; issue #208, Decision 0083) is where the
    main model's OpenAI-compatible API is: the Inferred Preference interpreter
    (``app.state.preference_interpreter``, #38) then asks it, under an
    ``INTERACTIVE`` lease of the scheduler (without one, or without
    ``local_model``, the rule interpreter answers). Its client is closed at
    shutdown.
    """
    settings = settings or Settings()
    # The secret store of the shared connections (Decision 0087, 1), when one is
    # configured: a configured store that cannot be opened stops the start here.
    secret_store = build_secret_store(settings)
    database = database or Database(settings)
    event_bus = event_bus or EventBus(
        settings.event_queue_size, settings.event_max_subscribers
    )
    auth = auth or build_auth(settings, database)
    if local_runtimes is not None and compute is None:
        raise TypeError("local_runtimes need compute")
    if local_model is not None and compute is None:
        raise TypeError("local_model needs compute")
    if sandbox is not None and not isinstance(sandbox, SandboxSetup):
        raise TypeError("sandbox must be a SandboxSetup")
    compute_services: ComputeServices | None = None
    if compute is not None:
        scheduler, vram_warnings = build_compute(compute)
        compute_services = ComputeServices(compute, scheduler, vram_warnings)
    preference_interpreter = None
    local_model_client = None
    if local_model is not None:
        preference_interpreter, local_model_client = build_preference_interpreter(
            local_model, compute_services.scheduler
        )
        if sandbox is not None and orchestrator_config is not None:
            # The local agent runtime (Decision 0083, 11's stage 3): its tools are
            # the repository tools, so only with a sandbox; it plays the ladder
            # label ``local_model.agent_label``.
            local_runtimes = {
                **dict(local_runtimes or {}),
                local_model.agent_label: build_local_agent(
                    local_model,
                    local_model_client,
                    node_seconds=orchestrator_config.node_timeout_seconds,
                ),
            }

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
        collection = None
        maintenance = None
        health = app.state.system_health
        compute_stop = asyncio.Event()
        # The workers have a stop of their own, set first at shutdown: none
        # claims another entry while the rest of the shutdown runs.
        workers_stop = asyncio.Event()
        workers: set[asyncio.Task] = set()
        try:
            # The sandboxes of the repository tools (Decision 0083, 6): idle ones
            # are stopped, and every one at shutdown.
            if task_execution is not None and task_execution.sandboxes is not None:
                background.add(
                    asyncio.create_task(
                        reap_idle_sandboxes(task_execution.sandboxes, compute_stop)
                    )
                )
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
                    secret_store=secret_store,
                )
                background.add(asyncio.create_task(reaper.run()))
                health.reaper.attach(reaper)
            # Codex sessions whose slot outlived their process: taken over and
            # their login collected once their unit stopped (Decision 0096, 1 D;
            # issue #280). Every process runs it (one wins each slot).
            units = app.state.codex_session_units
            if units is not None and app.state.connections is not None:
                collection = build_codex_collection(
                    app.state.connections,
                    session_base=settings.cli_session_dir,
                    interval_seconds=settings.codex_collect_interval_seconds,
                    units=units,
                )
                background.add(asyncio.create_task(collection.run()))
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
            # The orchestrator's workers (Decision 0086, Approved): only when an
            # orchestrator was composed; cancelled first at shutdown.
            orchestrator = (
                None if task_execution is None else task_execution.orchestrator
            )
            if orchestrator is not None:
                for worker_id in worker_ids(settings.orchestrator_workers):
                    workers.add(
                        asyncio.create_task(orchestrator.serve(worker_id, workers_stop))
                    )
            # The System Health history (PAW-066): sample, roll up, purge.
            if health.sampling:
                background.add(asyncio.create_task(health.monitor.run()))
            yield
        finally:
            workers_stop.set()
            if workers:
                # The workers are cancelled first (Decision 0086, 4): each gives
                # its entry back and its running nodes become ready again, for
                # the next process (a node attempt can take far longer than the
                # shutdown timeout, and no new node or entry is started while
                # the rest of the shutdown runs). The wait is bounded: a worker
                # that ignores its cancellation is given up on.
                for worker in workers:
                    worker.cancel()
                _, ignored = await asyncio.wait(
                    workers, timeout=settings.shutdown_timeout_seconds
                )
                if ignored:
                    logger.warning(
                        "%d orchestrator worker(s) did not stop", len(ignored)
                    )
                for worker in workers:
                    if worker.done():
                        _retrieve(worker)
                    else:
                        worker.add_done_callback(_retrieve)
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
            if collection is not None:
                collection.stop()
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
            if local_model_client is not None:
                await local_model_client.aclose()
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
            sandbox=sandbox,
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
    # The quota / usage routes' connection service (issue #187): with a database.
    app.state.connections = (
        build_connection_service(
            settings,
            database,
            app.state.authorizer,
            auth.audit_sink,
            secret_store=secret_store,
        )
        if database.configured
        else None
    )
    # Whether a handle can be checked against the secret store (issue #277:
    # without one, ``/admin/connections`` attaches and replaces nothing).
    app.state.secret_store_configured = secret_store is not None
    app.state.compute = compute_services
    # The model of the Inferred Preference interpreter (#38), or ``None``: the
    # rule interpreter answers.
    app.state.preference_interpreter = preference_interpreter
    # Decision 0096 (issue #280): the session units the Codex collection asks
    # about, with a secret store to write a collected login to; System Health
    # reads the egress units left over through them too (Decision 0096, 7).
    app.state.codex_session_units = (
        SystemdSessionUnits(SystemdLauncher())
        if secret_store is not None
        and database.configured
        and settings.codex_collect_interval_seconds > 0
        else None
    )
    probe = NvidiaSmiProbe() if settings.health_gpu_probe and compute is None else None
    app.state.system_health = build_system_health(
        settings,
        database,
        compute=None if compute_services is None else compute_services.scheduler,
        probe=probe,
        event_bus=event_bus,
        secret_store=secret_store,
        egress=app.state.codex_session_units,
    )
    # A person's Notification Center (issue #188): ``/api/v1/notifications``
    # answers 503 without a database.
    app.state.notifications = (
        NotificationStore(database, timeout_seconds=settings.database_timeout_seconds)
        if database.configured
        else None
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
