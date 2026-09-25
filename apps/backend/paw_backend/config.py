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

from paw_backend.db_roles import validate_role_name
from paw_backend.security import normalize_origin

_DRIVER = "postgresql+psycopg"
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
    # INSERT and SELECT on ``audit_events`` (and nothing else on it). Migration
    # ``0021`` grants it only what redeeming an Owner token needs (SELECT, and
    # UPDATE of a few columns), so it cannot create tokens or change roles.
    app_database_role: str | None = None
    # The server-local management commands (Owner setup / recovery, PAW-021)
    # connect with this URL instead: a role that may create Owner tokens, which
    # the web application's role may not. Unset: they use ``database_url``
    # (a single-role setup, where the application could create tokens too).
    operator_database_url: SecretStr | None = None
    # The role named by ``operator_database_url``; migration ``0021`` grants it
    # the privileges of the management commands.
    operator_database_role: str | None = None
    # How long a readiness result (also a failure) is reused. Together with
    # single flight it caps the probe connections an unauthenticated
    # /health/ready can cause at one per interval; 0 turns the reuse off.
    database_readiness_cache_seconds: float = Field(default=1.0, ge=0, le=60)

    # Event path (SSE / WebSocket).
    event_heartbeat_seconds: float = Field(default=15.0, gt=0)
    event_queue_size: int = Field(default=100, ge=1)
    event_max_subscribers: int = Field(default=100, ge=1)

    # Initial Owner setup / recovery tokens (PAW-021). A token is single-use and
    # expires after this many seconds; a token that was tried this many times
    # is locked out for good.
    setup_token_ttl_seconds: int = Field(default=1_800, ge=60, le=14_400)
    setup_token_max_attempts: int = Field(default=5, ge=1, le=20)

    # How often the Research Scratch Store's janitor deletes expired items
    # (PAW-050). 0 turns it off: expired research would then stay in PostgreSQL.
    scratch_purge_interval_seconds: int = Field(default=3_600, ge=0, le=86_400)

    # Repository registration (PAW-027, Decision 0017). Where a user's checkouts
    # live below their home; the roots (per user: ``{home}`` and ``{user}``) an
    # existing repository may be registered from; the hosts a repository may be
    # cloned from; the lowest uid that counts as a person (system accounts never
    # get a checkout); and how long git may run. ``RepositoryPolicy.from_settings``
    # validates the values strictly.
    repository_workspace_subdir: str = Field(
        default="workspaces", pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
    )
    repository_existing_roots: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["{home}"], min_length=1, max_length=8
    )
    repository_clone_hosts: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["github.com"], min_length=1, max_length=8
    )
    repository_min_linux_uid: int = Field(default=1000, ge=1, le=4_294_967_295)
    repository_git_timeout_seconds: float = Field(default=30.0, gt=0, le=7_200)
    repository_clone_timeout_seconds: float = Field(default=900.0, gt=0, le=7_200)

    log_level: str = "info"

    @field_validator(
        "tls_certfile",
        "tls_keyfile",
        "database_url",
        "migration_database_url",
        "operator_database_url",
        "app_database_role",
        "operator_database_role",
        mode="before",
    )
    @classmethod
    def _empty_string_means_unset(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator(
        "database_url",
        "migration_database_url",
        "operator_database_url",
        mode="after",
    )
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

    @field_validator("app_database_role", "operator_database_role")
    @classmethod
    def _valid_role_name(cls, value: str | None) -> str | None:
        # The same rules the migrations apply (see paw_backend.db_roles).
        return None if value is None else validate_role_name(value)

    @field_validator(
        "allowed_hosts",
        "allowed_origins",
        "repository_existing_roots",
        "repository_clone_hosts",
        mode="before",
    )
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

    @field_validator("scratch_purge_interval_seconds")
    @classmethod
    def _purge_interval_is_off_or_at_least_a_minute(cls, value: int) -> int:
        # 1..59 would hit the database every few seconds for no benefit.
        if 0 < value < 60:
            raise ValueError(
                "scratch_purge_interval_seconds must be 0 (off) or 60 to 86400"
            )
        return value

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
