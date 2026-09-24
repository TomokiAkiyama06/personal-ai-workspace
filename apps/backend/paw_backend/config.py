"""Application settings.

Every value comes from a ``PAW_``-prefixed environment variable. Nothing is
read from files in the repository, and no secret has a default value.
"""

import re
from ipaddress import ip_address
from pathlib import Path
from typing import Annotated, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from paw_backend.security import normalize_origin

_DRIVER = "postgresql+psycopg"
# A plain (unquoted-style) PostgreSQL identifier; it is still quoted when used.
_ROLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")
_HOST = re.compile(r"\[[0-9a-f:.]+\]|[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?")


class Settings(BaseSettings):
    """Runtime configuration of the backend (see ``apps/backend/README.md``)."""

    model_config = SettingsConfigDict(
        env_prefix="PAW_",
        frozen=True,
        # Validation errors must never echo the offending value: it can be a
        # database URL that contains a password.
        hide_input_in_errors=True,
    )

    # Network / TLS. Uvicorn terminates TLS when a certificate is configured;
    # otherwise a reverse proxy in front of the loopback listener must.
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    tls_certfile: Path | None = None
    tls_keyfile: Path | None = None
    allow_plaintext_http: bool = False
    # Sent only on HTTPS responses; see SecurityHeadersMiddleware.
    hsts_max_age_seconds: int = Field(default=31_536_000, ge=0)
    # Seconds Uvicorn waits for open connections (SSE, WebSocket) on shutdown
    # before it cancels them.
    shutdown_timeout_seconds: int = Field(default=5, ge=1, le=300)

    # Host header allow-list (comma-separated in the environment). The default
    # only serves loopback names; a reverse-proxy deployment must list the
    # public host name. Without this check DNS rebinding can reach the API.
    allowed_hosts: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["localhost", "127.0.0.1", "[::1]"], min_length=1
    )
    # Extra origins that may open the event WebSocket (comma-separated). An
    # origin equal to the request's own Host is always accepted; other browser
    # origins are refused. Requests without an Origin header (non-browser
    # clients) are not affected.
    allowed_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # PostgreSQL. ``None`` keeps the app running; readiness then reports
    # ``not_configured`` instead of the process refusing to start.
    database_url: SecretStr | None = None
    database_timeout_seconds: float = Field(default=3.0, gt=0, le=60)
    database_pool_size: int = Field(default=5, ge=1, le=100)
    # Migrations run as the role that owns the schema; when this is set Alembic
    # uses it instead of ``database_url``, so the application itself can run as
    # a role that cannot alter or drop the append-only audit trail.
    migration_database_url: SecretStr | None = None
    # The role the application connects as. The audit migration grants it
    # INSERT and SELECT on ``audit_events`` (and nothing else on it).
    app_database_role: str | None = None

    # Event path (SSE / WebSocket).
    event_heartbeat_seconds: float = Field(default=15.0, gt=0)
    event_queue_size: int = Field(default=100, ge=1)
    event_max_subscribers: int = Field(default=100, ge=1)

    log_level: str = "info"

    @field_validator(
        "tls_certfile",
        "tls_keyfile",
        "database_url",
        "migration_database_url",
        "app_database_role",
        mode="before",
    )
    @classmethod
    def _empty_string_means_unset(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("database_url", "migration_database_url", mode="after")
    @classmethod
    def _normalize_database_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        try:
            url = make_url(value.get_secret_value())
        except ArgumentError:
            raise ValueError("database_url is not a valid URL") from None
        if url.drivername not in ("postgresql", _DRIVER):
            raise ValueError(f"database_url must use postgresql:// or {_DRIVER}://")
        url = url.set(drivername=_DRIVER)
        return SecretStr(url.render_as_string(hide_password=False))

    @field_validator("app_database_role")
    @classmethod
    def _valid_role_name(cls, value: str | None) -> str | None:
        if value is not None and _ROLE.fullmatch(value) is None:
            raise ValueError("app_database_role is not a valid PostgreSQL role name")
        return value

    @field_validator("allowed_hosts", "allowed_origins", mode="before")
    @classmethod
    def _split_comma_separated(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("allowed_hosts")
    @classmethod
    def _validate_hosts(cls, value: list[str]) -> list[str]:
        hosts = [host.strip().lower() for host in value]
        if not all(_HOST.fullmatch(host) for host in hosts):
            raise ValueError(
                "allowed_hosts entries must be host names or IP addresses "
                "without scheme, port or path"
            )
        return hosts

    @field_validator("allowed_origins")
    @classmethod
    def _validate_origins(cls, value: list[str]) -> list[str]:
        origins = [normalize_origin(origin) for origin in value]
        if None in origins:
            raise ValueError(
                "allowed_origins entries must look like https://host[:port]"
            )
        return [origin for origin in origins if origin is not None]

    @field_validator("log_level")
    @classmethod
    def _lower_log_level(cls, value: str) -> str:
        value = value.lower()
        if value not in {"critical", "error", "warning", "info", "debug", "trace"}:
            raise ValueError("log_level is not a Uvicorn log level")
        return value

    @model_validator(mode="after")
    def _tls_files_come_in_pairs(self) -> Self:
        if (self.tls_certfile is None) != (self.tls_keyfile is None):
            raise ValueError("tls_certfile and tls_keyfile must be set together")
        return self

    @property
    def tls_enabled(self) -> bool:
        return self.tls_certfile is not None

    @property
    def binds_loopback_only(self) -> bool:
        if self.host == "localhost":
            return True
        try:
            return ip_address(self.host).is_loopback
        except ValueError:
            return False
