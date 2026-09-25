"""Resolving a user id to a current :class:`Principal`."""

import uuid
from typing import Protocol

from paw_backend.authz.subjects import Principal


class PrincipalDirectory(Protocol):
    """Looks up the *current* principal of a user by id.

    ``Authorizer.authorize_agent_action`` calls this on every decision, so a
    removed user, a deactivated account or a demotion takes effect on the next
    action of every agent working for that user; an agent never keeps the
    authority its user had when the task started. Return ``None`` for a user
    that does not exist or is not active. Implemented by the user store
    (PAW-021 / PAW-026).

    The lookup is bounded by the ``Authorizer`` timeout
    (``PAW_DATABASE_TIMEOUT_SECONDS``), and the authorizer does not depend on
    how an implementation reacts to cancellation: at the deadline it cancels
    the lookup, stops waiting for it and denies (``delegator_not_active``,
    audited); whatever the lookup returns or raises afterwards is discarded.
    An implementation should still stop its own work when cancelled. One that
    reads PostgreSQL must use ``Database.fetch_abortable`` (the connection's
    socket is shut down at the deadline), not a pooled SQLAlchemy/psycopg call:
    cancelling that waits for a stalled server (about ten seconds, or for
    good) and the abandoned lookup keeps its connection meanwhile. At most 32
    lookups (in flight or abandoned) exist at a time: a request waits for a
    free slot within its deadline, and is refused with the same audited denial
    when none frees up.
    """

    async def get_principal_by_id(self, user_id: uuid.UUID) -> Principal | None: ...


class NoPrincipalDirectory:
    """The default until users exist: nobody can be resolved."""

    async def get_principal_by_id(self, user_id: uuid.UUID) -> Principal | None:
        return None
