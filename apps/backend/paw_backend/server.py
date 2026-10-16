"""Process entrypoint: ``python -m paw_backend`` or ``paw-backend``.

TLS: either Uvicorn terminates it (``PAW_TLS_CERTFILE`` / ``PAW_TLS_KEYFILE``),
or a reverse proxy (for example ``tailscale serve`` or Caddy) does while the
backend listens on loopback only. A listener that is reachable from other
hosts must not speak plain HTTP, so that combination is refused unless
``PAW_ALLOW_PLAINTEXT_HTTP=true`` states that the network path is protected.

The runtime configuration (Decision 0093, Approved; issue #208): when
``PAW_RUNTIME_CONFIG_FILE`` names a file, it is read once here, before the
application is built (:func:`runtime_wiring`), and what it enables (the Compute
Scheduler, the local model, the sandbox, the git runner, the orchestrator) is
passed to ``create_app``. Anything wrong in it, or anything ``create_app``
refuses of it, stops the start with exit code 2 and
``Invalid runtime configuration: <section>.<key>: <reason>`` (no value). Unset:
nothing more is built than before, and a warning says so once.
"""

import logging
import sys
from collections.abc import Callable

import uvicorn
from fastapi import FastAPI
from pydantic import ValidationError

from paw_backend.app import create_app
from paw_backend.config import Settings
from paw_backend.runtime_config import (
    RuntimeConfigError,
    RuntimeWiring,
    check_environment,
    load_runtime_config,
)

logger = logging.getLogger("paw_backend.server")


class _StartupLogHandler(logging.StreamHandler):
    """Writes to the current ``sys.stderr`` (the journal under systemd)."""

    def __init__(self) -> None:
        super().__init__()
        self.setFormatter(logging.Formatter("%(levelname)s:     %(message)s"))

    @property
    def stream(self):
        return sys.stderr

    @stream.setter
    def stream(self, _value) -> None:
        pass


def show_startup_log() -> None:
    """Make this module's start-up lines (the runtime configuration's summary
    at INFO, its warning) reach stderr: the process configures no logging of
    its own, and Uvicorn configures only its own loggers, later (Codex review
    #265). Idempotent."""
    if not any(isinstance(h, _StartupLogHandler) for h in logger.handlers):
        logger.addHandler(_StartupLogHandler())
    logger.setLevel(logging.INFO)
    logger.propagate = False


#: Logged once at start when ``PAW_RUNTIME_CONFIG_FILE`` is not set.
RUNTIME_CONFIG_NOT_SET = (
    "runtime configuration not set: compute, local model, sandbox and "
    "orchestrator are disabled"
)

# The event endpoints ignore client frames; keep inbound messages small.
WEBSOCKET_MAX_MESSAGE_BYTES = 64 * 1024


class ServerConfigurationError(Exception):
    """The settings cannot be turned into a safe server configuration."""


def build_server_config(settings: Settings, app: FastAPI) -> uvicorn.Config:
    if not settings.tls_enabled and not (
        settings.binds_loopback_only or settings.allow_plaintext_http
    ):
        raise ServerConfigurationError(
            "Refusing to serve plain HTTP on a non-loopback address. Set "
            "PAW_TLS_CERTFILE and PAW_TLS_KEYFILE, bind PAW_HOST to 127.0.0.1 behind "
            "a TLS-terminating reverse proxy, or set PAW_ALLOW_PLAINTEXT_HTTP=true."
        )
    for name, path in (
        ("PAW_TLS_CERTFILE", settings.tls_certfile),
        ("PAW_TLS_KEYFILE", settings.tls_keyfile),
    ):
        if path is not None and not path.is_file():
            raise ServerConfigurationError(f"{name} does not point to a file")
    return uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        ssl_certfile=settings.tls_certfile,
        ssl_keyfile=settings.tls_keyfile,
        log_level=settings.log_level,
        server_header=False,
        # Open SSE / WebSocket connections never end by themselves; without a
        # limit shutdown would wait for them forever and the lifespan cleanup
        # (closing the database engine) would not run.
        timeout_graceful_shutdown=settings.shutdown_timeout_seconds,
        ws_max_size=WEBSOCKET_MAX_MESSAGE_BYTES,
        # The external scheme of a request (``scope["scheme"]``, which the Origin
        # check of the state-changing routes compares) and the client address (the
        # login backoff's source) come from ``X-Forwarded-Proto`` / ``-For`` ONLY
        # when the connection is from a proxy in ``FORWARDED_ALLOW_IPS`` (default:
        # loopback). Written out so that the dependency is visible and tested.
        proxy_headers=True,
    )


def runtime_wiring(
    settings: Settings,
    *,
    load: Callable[[str, Settings], RuntimeWiring] | None = None,
) -> RuntimeWiring:
    """What ``PAW_RUNTIME_CONFIG_FILE`` enables (``RuntimeConfigError`` when it
    cannot be used); nothing, with a warning, when it is not set. ``load``:
    tests pass one."""
    if settings.runtime_config_file is None:
        logger.warning(RUNTIME_CONFIG_NOT_SET)
        return RuntimeWiring()
    load = load_runtime_config if load is None else load
    wiring = load(str(settings.runtime_config_file), settings)
    check_environment(wiring, settings)
    logger.info("Runtime configuration: %s", wiring.summary())
    return wiring


def build_app(settings: Settings, wiring: RuntimeWiring) -> FastAPI:
    """``create_app`` with what the runtime configuration enables; a
    ``TypeError`` / ``ValueError`` of it becomes ``RuntimeConfigError`` (by its
    type only: its text is not shown)."""
    arguments = wiring.create_app_arguments()
    if not arguments:
        return create_app(settings)
    try:
        return create_app(settings, **arguments)
    except ValidationError:
        raise
    except (TypeError, ValueError) as error:
        raise RuntimeConfigError(
            "create_app", f"refused the configuration ({type(error).__name__})"
        ) from None


def main() -> None:
    show_startup_log()
    try:
        settings = Settings()
        wiring = runtime_wiring(settings)
        config = build_server_config(settings, build_app(settings, wiring))
    except (ValidationError, ServerConfigurationError) as error:
        # Neither message contains setting values (see Settings.model_config).
        print(f"Invalid configuration: {error}", file=sys.stderr)
        raise SystemExit(2) from None
    except RuntimeConfigError as error:
        # Section, key and a fixed reason: no value (runtime_config.py).
        print(f"Invalid runtime configuration: {error}", file=sys.stderr)
        raise SystemExit(2) from None
    uvicorn.Server(config).run()
