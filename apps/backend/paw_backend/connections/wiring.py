"""The connection service of the HTTP layer (issue #187).

``create_app`` builds one ``ConnectionService`` on the application's database,
Authorizer and audit sink (``app.state.connections``) for the quota and usage routes
(``api/v1/usage.py``). It reads and changes quotas and reads usage; it never runs a
call: without the health probes no adapter is registered and its secret resolver
refuses every handle (``NoSecretStore``), so ``execute`` would fail closed; with
them (Decision 0100, 11) it probes the connections and still runs no call. The
Orchestrator builds its own service with the deployment's adapters and secret
store.
"""

import tomllib
from collections.abc import Sequence
from pathlib import Path

from paw_backend.authz import AuditSink, Authorizer
from paw_backend.config import Settings
from paw_backend.connections.adapter import AdapterRegistry, ConnectionAdapter
from paw_backend.connections.limits import DEFAULT_HEALTH_TIMEOUT_SECONDS
from paw_backend.connections.probe import PROBE_TIMEOUT_SECONDS
from paw_backend.connections.secret import Secret
from paw_backend.connections.secret_store import FileSecretStore, load_master_key
from paw_backend.connections.service import ConnectionService
from paw_backend.db import Database


class NoSecretStore:
    """A secret resolver that knows no handle (the HTTP layer resolves none)."""

    async def resolve(self, handle: str) -> Secret:
        raise LookupError("no secret store")


#: The release directory (Decision 0079): the secret store is never inside it.
RELEASE_ROOT = Path("/opt/paw")
#: The release tool's configuration and its documented restore point directory
#: (Decision 0079, 4): the secret store never overlaps the restore points.
RELEASE_CONFIG = Path("/etc/paw/release.toml")
DEFAULT_RESTORE_POINT_DIR = Path("/var/lib/paw/restore-points")


def restore_point_dirs(config: Path = RELEASE_CONFIG) -> list[Path]:
    """The documented restore point directory and the one the release tool is
    configured with (when its file can be read)."""
    found = [DEFAULT_RESTORE_POINT_DIR]
    try:
        with config.open("rb") as handle:
            value = tomllib.load(handle).get("restore_point_dir")
    except (OSError, tomllib.TOMLDecodeError):
        return found
    if isinstance(value, str) and value.startswith("/"):
        found.append(Path(value))
    return found


def build_secret_store(settings: Settings) -> FileSecretStore | None:
    """The deployment's secret store (Decision 0087, 1), or ``None`` when
    ``secret_store_dir`` is unset. Its key comes from systemd
    (``$CREDENTIALS_DIRECTORY``); a store that is configured but cannot be
    opened fails here (``SecretStoreError``), at startup."""
    if settings.secret_store_dir is None:
        return None
    forbidden = [
        path
        for path in (
            RELEASE_ROOT,
            settings.recovery_repository_dir,
            settings.memory_projection_dir,
            settings.web_dist_dir,
            *restore_point_dirs(),
        )
        if path is not None
    ]
    return FileSecretStore(
        settings.secret_store_dir, load_master_key(), forbidden=forbidden
    )


def build_connection_service(
    settings: Settings,
    database: Database,
    authorizer: Authorizer,
    audit_sink: AuditSink,
    *,
    secret_store: FileSecretStore | None = None,
    probes: Sequence[ConnectionAdapter] = (),
) -> ConnectionService:
    """The quota / usage service of the HTTP layer (calendar periods in the
    default zone, ``Asia/Tokyo``: Decision 0016, section 4). With the
    deployment's ``secret_store`` a registration checks the handle against it
    (Decision 0087, 1).

    ``probes`` (Decision 0100, 11: ``agents.cli_wiring.build_probe_adapters``)
    are the connections' health probe adapters: registered here, with the
    secret store as the resolver (the probe needs the credential) and
    ``PROBE_TIMEOUT_SECONDS`` as the health check's deadline. They need the
    secret store (``TypeError`` without it). Still no call runs here: a probe
    adapter's ``run`` fails at once, and this service has no session adapter,
    no budget and no queue lease (``execute`` and ``execute_session`` are
    refused as before)."""
    adapters = AdapterRegistry()
    for probe in probes:
        adapters.register(probe)
    if len(adapters) and secret_store is None:
        raise TypeError("the health probes need the secret store")
    return ConnectionService(
        database,
        authorizer,
        audit_sink,
        adapters,
        secret_store if len(adapters) else NoSecretStore(),
        credential_store=secret_store,
        database_timeout_seconds=settings.database_timeout_seconds,
        health_timeout_seconds=(
            PROBE_TIMEOUT_SECONDS if len(adapters) else DEFAULT_HEALTH_TIMEOUT_SECONDS
        ),
    )
