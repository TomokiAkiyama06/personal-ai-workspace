"""FastAPI integration: the principal seam and ``require_capability``.

Authentication does not exist yet (PAW-022). Until it does, the default
provider yields nobody, so every endpoint protected with
:func:`require_capability` answers 401. PAW-022 replaces the provider (see
:func:`install_authz`); nothing else in this package changes.
"""

import inspect
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Protocol

from fastapi import Depends, FastAPI
from starlette import status
from starlette.exceptions import WebSocketException
from starlette.requests import HTTPConnection

from paw_backend.authz.audit import AuditSink, PostgresAuditSink
from paw_backend.authz.authorizer import Authorizer
from paw_backend.authz.capabilities import Capability
from paw_backend.authz.policy import Reason
from paw_backend.authz.principals import NoPrincipalDirectory, PrincipalDirectory
from paw_backend.authz.subjects import Principal, Resource
from paw_backend.config import Settings
from paw_backend.db import Database
from paw_backend.errors import ApiError

logger = logging.getLogger(__name__)


class PrincipalProvider(Protocol):
    """Turns a request into the authenticated user, or ``None``.

    The implementation (PAW-022: session cookie, PAW-021 / PAW-026: user and
    membership lookup) must return only *active* users, and build the
    principal from stored data, never from anything the client or a model
    supplies about its own role.
    """

    async def get_principal(self, connection: HTTPConnection) -> Principal | None: ...


class UnauthenticatedProvider:
    """The default: nobody is authenticated."""

    async def get_principal(self, connection: HTTPConnection) -> Principal | None:
        return None


# Builds the Resource of a request, from ids the backend trusts. May be sync or
# async (a project's state has to be read from the database).
ResourceResolver = Callable[[HTTPConnection], Resource | Awaitable[Resource]]


def install_authz(
    app: FastAPI,
    *,
    settings: Settings,
    database: Database,
    principal_provider: PrincipalProvider | None = None,
    principal_directory: PrincipalDirectory | None = None,
    audit_sink: AuditSink | None = None,
) -> None:
    """Attach the provider and the authorizer to ``app.state``."""
    app.state.principal_provider = principal_provider or UnauthenticatedProvider()
    app.state.authorizer = Authorizer(
        audit_sink or PostgresAuditSink(database),
        directory=principal_directory or NoPrincipalDirectory(),
        timeout_seconds=settings.database_timeout_seconds,
    )


def get_authorizer(connection: HTTPConnection) -> Authorizer:
    return connection.app.state.authorizer


def get_principal_provider(connection: HTTPConnection) -> PrincipalProvider:
    return connection.app.state.principal_provider


def require_capability(
    capability: Capability,
    resource: Resource | ResourceResolver | None = None,
) -> Callable[..., Awaitable[Principal]]:
    """Dependency that lets a request through only if the backend allows it.

    Works for HTTP and WebSocket routes (a WebSocket is refused before it is
    accepted, with close code 1008, or 1013 when auditing is unavailable).

    ``resource`` is a fixed :class:`Resource` (default: the workspace itself)
    or a function of the connection (sync or async) that builds one from the
    route's path parameters and stored state. If the resolver raises, the
    request is denied (and audited) rather than turned into an unaudited 500.

    HTTP answers: 401 (``unauthorized``) when nobody is authenticated, 403
    (``forbidden``) when the user may not do this, 503 when an action that
    must be audited cannot be. The bodies are fixed and never say which rule
    denied the request.
    """
    if not isinstance(capability, Capability):
        raise TypeError("require_capability needs a Capability member")

    async def dependency(
        connection: HTTPConnection,
        authorizer: Annotated[Authorizer, Depends(get_authorizer)],
        provider: Annotated[PrincipalProvider, Depends(get_principal_provider)],
    ) -> Principal:
        principal = await provider.get_principal(connection)
        try:
            target = await _resolve(resource, connection)
        except Exception as error:  # a broken resolver must not skip the audit
            # A malformed id (ValueError) is a client mistake; anything else is a bug.
            logger.log(
                logging.INFO if isinstance(error, ValueError) else logging.WARNING,
                "Resource resolver failed (%s)",
                type(error).__name__,
            )
            target = None
        decision = await authorizer.authorize(
            principal,
            capability,
            target,
            correlation_id=_correlation_id(connection),
            client_request_id=getattr(connection.state, "request_id", None),
        )
        if decision.allowed and principal is not None:
            return principal
        raise _refusal(connection, decision.reason)

    # Lets tests (and reviewers) find every guarded route.
    dependency.paw_capability = capability  # type: ignore[attr-defined]
    return dependency


def _correlation_id(connection: HTTPConnection) -> uuid.UUID:
    """One server-generated id per request, shared by all its decisions."""
    existing = getattr(connection.state, "audit_correlation_id", None)
    if isinstance(existing, uuid.UUID):
        return existing
    created = uuid.uuid4()
    connection.state.audit_correlation_id = created
    return created


async def _resolve(
    resource: Resource | ResourceResolver | None, connection: HTTPConnection
) -> Resource:
    if resource is None:
        return Resource.system()
    if isinstance(resource, Resource):
        return resource
    result = resource(connection)
    if inspect.isawaitable(result):
        result = await result
    return result


def _refusal(connection: HTTPConnection, reason: Reason) -> Exception:
    unauthenticated = reason is Reason.UNAUTHENTICATED
    unavailable = reason is Reason.AUDIT_UNAVAILABLE
    if connection.scope["type"] == "websocket":
        if unavailable:
            return WebSocketException(code=status.WS_1013_TRY_AGAIN_LATER)
        return WebSocketException(code=status.WS_1008_POLICY_VIOLATION)
    if unauthenticated:
        return ApiError(401, "unauthorized", "Authentication required")
    if unavailable:
        return ApiError(503, "service_unavailable", "Service temporarily unavailable")
    return ApiError(403, "forbidden", "Permission denied")
