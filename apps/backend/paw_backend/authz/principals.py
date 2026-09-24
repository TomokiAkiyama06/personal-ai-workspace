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
    """

    async def get_principal_by_id(self, user_id: uuid.UUID) -> Principal | None: ...


class NoPrincipalDirectory:
    """The default until users exist: nobody can be resolved."""

    async def get_principal_by_id(self, user_id: uuid.UUID) -> Principal | None:
        return None
