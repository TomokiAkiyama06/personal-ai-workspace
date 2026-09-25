"""Turning the session cookie into a ``Principal`` (the seam of ``authz/deps.py``).

``SessionPrincipalProvider`` implements ``PrincipalProvider``: it reads the
session cookie, looks the session up (``SessionStore.authenticate``: not revoked,
not expired, the user ``active``) and builds the ``Principal`` from **stored**
data only: the user's system role from ``users`` and the project roles from the
accepted memberships of ``project_members`` (``projects.store.roles_of``, read
in the same transaction). Nothing the client sends about itself (a header, a
body, a claimed role) is read.

* A request without a session cookie, or with one that cannot be a session id,
  is anonymous **without touching the database** (an anonymous client must not
  cause database work).
* The database being unreachable is not "anonymous": the request gets 503 (a
  WebSocket, 1013), never a wrong 401 that would sign the user out in a client.
* The answer is cached on the request, so several guards of one route look the
  session up once (and touch it once).
* **A restricted session gets nothing** (PAW-023, Decision 0025): a session whose
  Passkey gate is not open (the policy requires a Passkey of this role and the
  session has not registered / used one) is refused with 403 ``passkey_required``
  by ``get_principal``, on EVERY route: default deny. Only the few routes that
  ask for it (``require_capability(..., allow_restricted=True)``: the session
  itself, sign-out, the Passkey ceremonies) reach ``get_principal_allowing_restricted``.

``DatabasePrincipalDirectory`` implements ``PrincipalDirectory``: the
authorizer asks it for the *current* principal of a user on every Agent action,
so a removed user or a demotion takes effect at the next action.
"""

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status
from starlette.exceptions import WebSocketException
from starlette.requests import HTTPConnection

from paw_backend.auth.db import run
from paw_backend.auth.errors import AuthUnavailableError
from paw_backend.auth.limits import SESSION_COOKIE_NAME
from paw_backend.auth.models import PasskeyGate
from paw_backend.auth.sessions import AuthenticatedSession, SessionStore
from paw_backend.auth.tokens import parse_session_token
from paw_backend.authz.roles import SystemRole
from paw_backend.authz.subjects import Principal
from paw_backend.db import Database
from paw_backend.errors import ApiError
from paw_backend.projects import store as project_store

logger = logging.getLogger(__name__)

_STATE_KEY = "paw_auth"


@dataclass(frozen=True, slots=True)
class AuthContext:
    """The authenticated session of a request and the principal built from it."""

    session: AuthenticatedSession
    principal: Principal


@dataclass(frozen=True, slots=True)
class _Resolved:
    """Distinguishes "looked up, nobody" from "not looked up yet" on the request."""

    context: AuthContext | None


class SessionPrincipalProvider:
    """``PrincipalProvider`` backed by the session table."""

    def __init__(
        self, database: Database, sessions: SessionStore, *, timeout_seconds: float
    ) -> None:
        if not isinstance(database, Database):
            raise TypeError("database must be a Database")
        if not isinstance(sessions, SessionStore):
            raise TypeError("sessions must be a SessionStore")
        self._database = database
        self._sessions = sessions
        self._timeout = float(timeout_seconds)

    async def authenticate(self, connection: HTTPConnection) -> AuthContext | None:
        """The session of the cookie (looked up once per request), or ``None``."""
        state = connection.state
        resolved = getattr(state, _STATE_KEY, None)
        if resolved is not None:
            return resolved.context
        # A value that cannot be a session id is anonymous before any query.
        token = parse_session_token(connection.cookies.get(SESSION_COOKIE_NAME))
        context = await self._resolve(connection, token) if token else None
        setattr(state, _STATE_KEY, _Resolved(context))
        return context

    async def get_principal(self, connection: HTTPConnection) -> Principal | None:
        context = await self.authenticate(connection)
        if context is None:
            return None
        if context.session.record.passkey_gate is not PasskeyGate.OPEN:
            # Authenticated, but not allowed to use the workspace yet: neither
            # anonymous (401 would say "sign in again") nor a principal.
            if connection.scope["type"] == "websocket":
                raise WebSocketException(status.WS_1008_POLICY_VIOLATION)
            raise ApiError(403, "passkey_required", "A passkey is required")
        return context.principal

    async def get_principal_allowing_restricted(
        self, connection: HTTPConnection
    ) -> Principal | None:
        """The principal of a session whatever its Passkey gate (the few routes that
        exist to get through the gate ask for this one)."""
        context = await self.authenticate(connection)
        return None if context is None else context.principal

    async def _resolve(
        self, connection: HTTPConnection, token: str
    ) -> AuthContext | None:
        async def work(session: AsyncSession) -> AuthContext | None:
            found = await self._sessions.authenticate(session, token)
            if found is None:
                return None
            roles = await project_store.roles_of(session, found.record.user_id)
            return AuthContext(
                found, Principal(found.record.user_id, found.system_role, roles)
            )

        try:
            return await run(self._database, work, self._timeout)
        except AuthUnavailableError:
            if connection.scope["type"] == "websocket":
                raise WebSocketException(status.WS_1013_TRY_AGAIN_LATER) from None
            raise ApiError(
                503, "service_unavailable", "Service temporarily unavailable"
            ) from None


def authenticated_context(connection: HTTPConnection) -> AuthContext | None:
    """The context the request's guard resolved (``None`` if it was not resolved)."""
    resolved = getattr(connection.state, _STATE_KEY, None)
    return None if resolved is None else resolved.context


class DatabasePrincipalDirectory:
    """``PrincipalDirectory`` backed by ``users`` and ``project_members``."""

    def __init__(self, database: Database, *, timeout_seconds: float) -> None:
        if not isinstance(database, Database):
            raise TypeError("database must be a Database")
        self._database = database
        self._timeout = float(timeout_seconds)

    async def get_principal_by_id(self, user_id: uuid.UUID) -> Principal | None:
        async def work(session: AsyncSession) -> Principal | None:
            role = (
                await session.execute(
                    text(
                        "SELECT system_role FROM users "
                        "WHERE id = :id AND status = 'active'"
                    ),
                    {"id": user_id},
                )
            ).scalar_one_or_none()
            if role is None:
                return None
            roles = await project_store.roles_of(session, user_id)
            return Principal(user_id, SystemRole(role), roles)

        try:
            return await run(self._database, work, self._timeout)
        except AuthUnavailableError:
            # The authorizer turns "no principal" into an audited denial.
            return None
