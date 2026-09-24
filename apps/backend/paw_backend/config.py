"""Application settings.

Every value comes from a ``PAW_``-prefixed environment variable. Nothing is
read from files in the repository, and no secret has a default value.
"""

from ipaddress import ip_address
from pathlib import Path
from typing import Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

_DRIVER = "postgresql+psycopg"


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
    hsts_max_age_seconds: int = Field(default=31_536_000, ge=0)
    # Seconds Uvicorn waits for open connections (SSE, WebSocket) on shutdown
    # before it cancels them.
    shutdown_timeout_seconds: int = Field(default=5, ge=1, le=300)

    # PostgreSQL. ``None`` keeps the app running; readiness then reports
    # ``not_configured`` instead of the process refusing to start.
    database_url: SecretStr | None = None
    database_timeout_seconds: float = Field(default=3.0, gt=0, le=60)
    database_pool_size: int = Field(default=5, ge=1, le=100)

    # Event path (SSE / WebSocket).
    event_heartbeat_seconds: float = Field(default=15.0, gt=0)
    event_queue_size: int = Field(default=100, ge=1)

    log_level: str = "info"

    @field_validator("tls_certfile", "tls_keyfile", "database_url", mode="before")
    @classmethod
    def _empty_string_means_unset(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("database_url", mode="after")
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
