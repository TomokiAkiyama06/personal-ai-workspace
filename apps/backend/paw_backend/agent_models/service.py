"""The Admin's policy of the CLI agents' models and the checks before a send
(Decision 0103, 2 A, 3 A, 4 A, 5 A, 9 A and 10; approved 2026-11-08; issue
#341).

:class:`AgentModelService` reads and changes the policy for the Admin API
(``api/v1/agent_models.py``): both need ``admin.config.manage`` (Owner /
Admin, no new capability: Decision 0103, 2 A), asked of the Authorizer
(audited, fail closed); a change is checked (:func:`~.policy.check_policy`:
verified models only, exactly one default per kind) and then written with its
``admin.agent_models.update`` audit event in one transaction
(:class:`~.store.AgentModelPolicyStore`).

:class:`AgentModelGate` is what a CLI runtime and a health probe ask before
anything is sent (Decision 0103, 4 A, 2 and 3; 5 A):

* :meth:`AgentModelGate.allows`: the model is one the repository verified for
  the kind (on some CLI version: the exact pair is checked by the session's
  adapter once the CLI said its version) and the current policy allows it;
  without a policy, the runtime configuration's model is the only allowed one
  (Decision 0103, 9 A);
* :meth:`AgentModelGate.default_model`: the model a health probe checks (the
  policy's default, else the runtime configuration's), or ``None`` when it is
  not a verified one (nothing is sent then);
* :meth:`AgentModelGate.configured_verified`: whether the runtime
  configuration's model of a kind is verified at all (Decision 0103, 10 A,
  2: the backend still starts; that kind's stages and probes send nothing,
  and the start-up line says ``model: unverified``).

The policy is read from the database on every call (no cache: a change is
seen by the next session and the next probe without a restart).
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from paw_backend.agent_models.policy import (
    RESOURCE_KIND,
    AllowedModel,
    InvalidModelPolicyError,
    KindPolicy,
    check_policy,
)
from paw_backend.agent_models.store import AgentModelPolicyStore, PolicyChange
from paw_backend.agent_models.verified import verified_models
from paw_backend.authz import Authorizer, Capability, Principal, Resource
from paw_backend.authz.policy import Reason
from paw_backend.connections.domain import ConnectionKind


class AgentModelPermissionDeniedError(Exception):
    """The Authorizer denied (``reason``; ``AUDIT_UNAVAILABLE``: 503)."""

    def __init__(self, reason: Reason) -> None:
        super().__init__("permission denied")
        self.reason = reason


@dataclass(frozen=True, slots=True)
class KindModels:
    """What the Admin sees of one kind: the stored policy (``None``: none, the
    runtime configuration's model is the only allowed one), that model
    (``None``: no ``[agents.<kind>]``) and the verified candidates."""

    kind: ConnectionKind
    policy: KindPolicy | None
    configured: str | None
    candidates: frozenset[str]

    @property
    def effective_default(self) -> str | None:
        if self.policy is not None:
            return self.policy.default_model
        return self.configured


def _configured(configured: Mapping[ConnectionKind, str] | None) -> Mapping:
    found = dict(configured or {})
    if not all(
        type(kind) is ConnectionKind and type(model) is str
        for kind, model in found.items()
    ):
        raise TypeError("configured must map a ConnectionKind to its model")
    return MappingProxyType(found)


class AgentModelService:
    """See the module. ``configured``: the runtime configuration's model of
    each kind that has an ``[agents.<kind>]``."""

    def __init__(
        self,
        store: AgentModelPolicyStore,
        authorizer: Authorizer,
        *,
        configured: Mapping[ConnectionKind, str] | None = None,
    ) -> None:
        self._store = store
        self._authorizer = authorizer
        self._configured = _configured(configured)

    async def _authorize(self, principal: object, correlation_id: uuid.UUID) -> None:
        if type(principal) is not Principal:
            raise AgentModelPermissionDeniedError(Reason.UNAUTHENTICATED)
        decision = await self._authorizer.authorize(
            principal,
            Capability.ADMIN_CONFIG_MANAGE,
            Resource(kind=RESOURCE_KIND),
            correlation_id=correlation_id,
        )
        if not decision.allowed:
            raise AgentModelPermissionDeniedError(decision.reason)

    def _view(self, policies: Mapping[ConnectionKind, KindPolicy]) -> list[KindModels]:
        return [
            KindModels(
                kind, policies.get(kind), self._configured.get(kind),
                verified_models(kind),
            )
            for kind in ConnectionKind
        ]  # fmt: skip

    async def read(self, principal: Principal) -> list[KindModels]:
        """Every kind's policy, configured model and candidates."""
        await self._authorize(principal, uuid.uuid4())
        return self._view(await self._store.read())

    async def update(
        self,
        principal: Principal,
        policies: Mapping[ConnectionKind, tuple[AllowedModel, ...] | list],
    ) -> tuple[list[KindModels], list[ConnectionKind]]:
        """Replace the policy of each kind named; return every kind's view and
        the kinds whose default model changed (their probe runs next:
        Decision 0103, 5 A). The rules are checked before the database is
        touched; ``InvalidModelPolicyError`` / ``ModelNotVerifiedError``."""
        change_id = uuid.uuid4()
        if type(principal) is not Principal:
            raise AgentModelPermissionDeniedError(Reason.UNAUTHENTICATED)
        if not isinstance(policies, Mapping) or not policies:
            raise InvalidModelPolicyError("policies")
        checked = {
            kind: check_policy(kind, models) for kind, models in policies.items()
        }
        await self._authorize(principal, change_id)
        change: PolicyChange = await self._store.replace(
            checked,
            actor_id=principal.user_id,
            actor_role=principal.system_role.value,
            change_id=change_id,
        )
        changed = [
            kind
            for kind, policy in change.policies.items()
            if policy.default_model
            != (change.previous_defaults.get(kind) or self._configured.get(kind))
        ]
        return self._view(change.committed), changed


class AgentModelGate:
    """See the module. ``store``: ``None`` reads no policy (a deployment
    without a database; tests): the runtime configuration's model is then
    the only allowed one of each kind."""

    def __init__(
        self,
        configured: Mapping[ConnectionKind, str] | None = None,
        store: AgentModelPolicyStore | None = None,
    ) -> None:
        self._configured = _configured(configured)
        self._store = store

    def __repr__(self) -> str:
        return "AgentModelGate()"

    def configured_verified(self, kind: ConnectionKind) -> bool:
        return self._configured.get(kind) in verified_models(kind)

    async def policy(self, kind: ConnectionKind) -> KindPolicy | None:
        if self._store is None:
            return None
        return (await self._store.read()).get(kind)

    async def allows(self, kind: ConnectionKind, model: object) -> bool:
        """May ``model`` be sent on ``kind`` now (see the module)? A policy
        that cannot be read raises (the caller fails closed)."""
        if type(model) is not str or model not in verified_models(kind):
            return False
        policy = await self.policy(kind)
        if policy is None:
            return model == self._configured.get(kind)
        return policy.allows(model)

    async def default_model(self, kind: ConnectionKind) -> str | None:
        """The model a health probe checks, or ``None`` (nothing is sent)."""
        policy = await self.policy(kind)
        model = self._configured.get(kind) if policy is None else policy.default_model
        return model if model in verified_models(kind) else None


__all__ = [
    "AgentModelGate",
    "AgentModelPermissionDeniedError",
    "AgentModelService",
    "KindModels",
]
