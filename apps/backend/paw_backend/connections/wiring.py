"""The connection service of the HTTP layer (issue #187).

``create_app`` builds one ``ConnectionService`` on the application's database,
Authorizer and audit sink (``app.state.connections``) for the quota and usage routes
(``api/v1/usage.py``). It reads and changes quotas and reads usage; it never runs a
call: no adapter is registered and its secret resolver refuses every handle
(``NoSecretStore``), so ``execute`` would fail closed. The Orchestrator builds its
own service with the deployment's adapters and secret store.
"""

from pathlib import Path

from paw_backend.authz import AuditSink, Authorizer
from paw_backend.config import Settings
from paw_backend.connections.adapter import AdapterRegistry
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
) -> ConnectionService:
    """The quota / usage service of the HTTP layer (calendar periods in the
    default zone, ``Asia/Tokyo``: Decision 0016, section 4). With the
    deployment's ``secret_store`` a registration checks the handle against it
    (Decision 0087, 1); it still runs no call (no adapter is registered)."""
    return ConnectionService(
        database,
        authorizer,
        audit_sink,
        AdapterRegistry(),
        NoSecretStore(),
        credential_store=secret_store,
        database_timeout_seconds=settings.database_timeout_seconds,
    )
