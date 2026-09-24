"""FastAPI integration: the principal seam and ``require_capability``.

Authentication does not exist yet (PAW-022). Until it does, the default
provider yields nobody, so every endpoint protected with
:func:`require_capability` answers 401. PAW-022 replaces the provider (see
:func:`install_authz`); nothing else in this package changes.
"""

from collections.abc import Awaitable, Callable
from typing import Annotated, Protocol

from fastapi import Depends, FastAPI, Request
from starlette.requests import HTTPConnection

from paw_backend.authz.audit import AuditSink, PostgresAuditSink
from paw_backend.authz.authorizer import Authorizer
from paw_backend.authz.capabilities import Capability
from paw_backend.authz.policy import Reason
from paw_backend.authz.subjects import Principal, Resource
from paw_backend.config import Settings
from paw_backend.db import Database
from paw_backend.errors import ApiError


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


ResourceResolver = Callable[[Request], Resource]


def install_authz(
    app: FastAPI,
    *,
    settings: Settings,
    database: Database,
    principal_provider: PrincipalProvider | None = None,
    audit_sink: AuditSink | None = None,
) -> None:
    """Attach the provider and the authorizer to ``app.state``."""
    app.state.principal_provider = principal_provider or UnauthenticatedProvider()
    app.state.authorizer = Authorizer(
        audit_sink or PostgresAuditSink(database),
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

    ``resource`` is a fixed :class:`Resource` (default: the workspace itself)
    or a function of the request that builds one from the route's path
    parameters. It must return ids the backend trusts; a malformed id makes
    the resolver raise ``ValueError`` and the request is denied.

    Answers 401 (``unauthorized``) when nobody is authenticated and 403
    (``forbidden``) when the user may not do this. Both bodies are fixed, and
    neither reveals which rule denied the request. If a privileged action
    cannot be audited the answer is 503. The decision is audited either way.
    """

    async def dependency(
        request: Request,
        authorizer: Annotated[Authorizer, Depends(get_authorizer)],
        provider: Annotated[PrincipalProvider, Depends(get_principal_provider)],
    ) -> Principal:
        principal = await provider.get_principal(request)
        try:
            target = _resolve(resource, request)
        except ValueError:
            target = None
        decision = await authorizer.authorize(
            principal,
            capability,
            target,
            request_id=getattr(request.state, "request_id", None),
        )
        if decision.allowed and principal is not None:
            return principal
        if decision.reason is Reason.UNAUTHENTICATED:
            raise ApiError(401, "unauthorized", "Authentication required")
        if decision.reason is Reason.AUDIT_UNAVAILABLE:
            raise ApiError(
                503, "service_unavailable", "Service temporarily unavailable"
            )
        raise ApiError(403, "forbidden", "Permission denied")

    return dependency


def _resolve(
    resource: Resource | ResourceResolver | None, request: Request
) -> Resource:
    if resource is None:
        return Resource.system()
    if isinstance(resource, Resource):
        return resource
    return resource(request)
