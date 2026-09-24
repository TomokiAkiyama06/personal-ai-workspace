"""Process entrypoint: ``python -m paw_backend`` or ``paw-backend``.

TLS: either Uvicorn terminates it (``PAW_TLS_CERTFILE`` / ``PAW_TLS_KEYFILE``),
or a reverse proxy (for example ``tailscale serve`` or Caddy) does while the
backend listens on loopback only. A listener that is reachable from other
hosts must not speak plain HTTP, so that combination is refused unless
``PAW_ALLOW_PLAINTEXT_HTTP=true`` states that the network path is protected.
"""

import sys

import uvicorn
from fastapi import FastAPI
from pydantic import ValidationError

from paw_backend.app import create_app
from paw_backend.config import Settings

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
    )


def main() -> None:
    try:
        settings = Settings()
        config = build_server_config(settings, create_app(settings))
    except (ValidationError, ServerConfigurationError) as error:
        # Neither message contains setting values (see Settings.model_config).
        print(f"Invalid configuration: {error}", file=sys.stderr)
        raise SystemExit(2) from None
    uvicorn.Server(config).run()
