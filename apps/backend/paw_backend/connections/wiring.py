"""The connection service of the HTTP layer (issue #187).

``create_app`` builds one ``ConnectionService`` on the application's database,
Authorizer and audit sink (``app.state.connections``) for the quota and usage routes
(``api/v1/usage.py``). It reads and changes quotas and reads usage; it never runs a
call: no adapter is registered and its secret resolver refuses every handle
(``NoSecretStore``), so ``execute`` would fail closed. The Orchestrator builds its
own service with the deployment's adapters and secret store.
"""

from paw_backend.authz import AuditSink, Authorizer
from paw_backend.config import Settings
from paw_backend.connections.adapter import AdapterRegistry
from paw_backend.connections.secret import Secret
from paw_backend.connections.service import ConnectionService
from paw_backend.db import Database


class NoSecretStore:
    """A secret resolver that knows no handle (the HTTP layer resolves none)."""

    async def resolve(self, handle: str) -> Secret:
        raise LookupError("no secret store")


def build_connection_service(
    settings: Settings,
    database: Database,
    authorizer: Authorizer,
    audit_sink: AuditSink,
) -> ConnectionService:
    """The quota / usage service of the HTTP layer (calendar periods in the
    default zone, ``Asia/Tokyo``: Decision 0016, section 4)."""
    return ConnectionService(
        database,
        authorizer,
        audit_sink,
        AdapterRegistry(),
        NoSecretStore(),
        database_timeout_seconds=settings.database_timeout_seconds,
    )
