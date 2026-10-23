"""The production ``CloudPolicy`` (Decision 0099, 2, Approved; issue #297).

:class:`ConfiguredCloudPolicy` answers ``CloudPolicy.allows(assignment)`` for
``CloudGate`` (Decision 0083, 1) from the ``[cloud]`` section of the runtime
configuration (Decision 0099, 7) and the database. It allows a send only when
all of these hold:

1. ``[cloud].enabled = true`` (the deployment's switch, ``false`` by default);
2. the assignment's agent is a CLI runtime (``claude`` / ``codex``) that the
   file configures (``[agents.<name>]``) and that is a rung of the role's
   ladder (``[orchestrator.ladders]``: the escalation targets included, wider
   than the planner's ``allowed`` candidates);
3. the deployment has exactly one ``active`` user (``single_user_only``,
   Decision 0099's point 5 A). This is asked again in the transaction that
   records the send (:meth:`ConfiguredCloudPolicy.recheck_in`, under the
   single-user advisory lock: ``paw_backend.identity.single_user``);
4. the task belongs to a project that is not being deleted (``pending_deletion``
   or ``deleted``: the projects ``stop_project_tasks`` stops). There is no
   per-project allow list: every project is allowed (the Human's answer 3).

Anything it cannot tell (an unknown task, a project row that is missing, a
database error, an unexpected value) is ``False``: fail closed. ``CloudGate``
itself treats only an exact ``True`` as an allowance.

The policy decides whether this content may leave the backend, not how much:
the quota and the task's budget stay the admission's (Decision 0016, 0083, 5).
"""

import logging
import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

from sqlalchemy import text

from paw_backend.db import Database
from paw_backend.identity.single_user import recheck_single_user_in
from paw_backend.orchestrator.domain import NodeRole
from paw_backend.orchestrator.runtime import NodeAssignment

logger = logging.getLogger(__name__)

#: The labels of the CLI runtimes (Decision 0083): the only agents that send a
#: task's content out of the backend.
CLI_AGENT_LABELS = ("claude", "codex")

# The project statuses whose tasks ``stop_project_tasks`` stops.
_STOPPING_PROJECT_STATUSES = frozenset({"pending_deletion", "deleted"})
_KNOWN_PROJECT_STATUSES = frozenset({"active", "archived"}) | (
    _STOPPING_PROJECT_STATUSES
)

_FACTS = text(
    "SELECT (SELECT count(*) FROM users WHERE status = 'active') AS active_users,"
    " p.status AS project_status"
    " FROM tasks t LEFT JOIN projects p ON p.id = t.project_id"
    " WHERE t.id = :task_id"
)


@dataclass(frozen=True, slots=True)
class CloudSettings:
    """The ``[cloud]`` section (Decision 0099, 7). ``single_user_only`` is
    ``True`` only: the Decision approved its point 5 A, and a deployment that
    wants more users needs a new Decision."""

    enabled: bool = False
    single_user_only: bool = True

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise TypeError("enabled")
        if self.single_user_only is not True:
            raise ValueError("single_user_only")


@dataclass(frozen=True, slots=True)
class CloudFacts:
    """What the policy reads of the database for one task."""

    active_users: int
    # ``None``: the task has no project row.
    project_status: str | None


class CloudFactsReader(Protocol):
    async def read(self, task_id: uuid.UUID) -> CloudFacts | None:
        """The facts of the task, ``None`` when there is no such task."""
        ...


class PostgresCloudFacts:
    """:class:`CloudFactsReader` on the database (one read-only statement)."""

    def __init__(self, database: Database) -> None:
        if not isinstance(database, Database):
            raise TypeError("database must be a Database")
        self._database = database

    async def read(self, task_id: uuid.UUID) -> CloudFacts | None:
        async with self._database.session() as session:
            row = (await session.execute(_FACTS, {"task_id": task_id})).one_or_none()
        if row is None:
            return None
        return CloudFacts(int(row.active_users), row.project_status)


class ConfiguredCloudPolicy:
    """See the module. ``ladders``: the orchestrator's ladders by role;
    ``agents``: the labels of the CLI runtimes the file configures
    (``[agents.<name>]``); ``facts``: where the users and the project are read
    (:class:`PostgresCloudFacts` in production)."""

    def __init__(
        self,
        settings: CloudSettings,
        *,
        ladders: Mapping[NodeRole, Sequence[str]],
        agents: Collection[str],
        facts: CloudFactsReader,
    ) -> None:
        if not isinstance(settings, CloudSettings):
            raise TypeError("settings must be CloudSettings")
        if not isinstance(ladders, Mapping) or not all(
            isinstance(role, NodeRole) for role in ladders
        ):
            raise TypeError("ladders must map a NodeRole to its labels")
        if getattr(facts, "read", None) is None or not callable(facts.read):
            raise TypeError("facts must be a CloudFactsReader")
        self._settings = settings
        self._ladders = MappingProxyType(
            {role: tuple(labels) for role, labels in ladders.items()}
        )
        self._agents = frozenset(label for label in agents if label in CLI_AGENT_LABELS)
        self._facts = facts

    @property
    def settings(self) -> CloudSettings:
        return self._settings

    def __repr__(self) -> str:
        return (
            f"ConfiguredCloudPolicy(enabled={self._settings.enabled}, "
            f"agents={sorted(self._agents)})"
        )

    async def allows(self, assignment: NodeAssignment) -> bool:
        try:
            return await self._allows(assignment)
        except Exception:
            # Fail closed; the error is not logged (it may quote a value).
            logger.warning("The cloud policy could not decide: the send is refused")
            return False

    async def _allows(self, assignment: NodeAssignment) -> bool:
        # 1. The deployment's switch.
        if self._settings.enabled is not True:
            return False
        # 2. A configured CLI runtime that is a rung of the role's ladder.
        if not isinstance(assignment, NodeAssignment):
            return False
        role = assignment.role
        agent = assignment.agent
        if not isinstance(role, NodeRole) or type(agent) is not str:
            return False
        if agent not in CLI_AGENT_LABELS or agent not in self._agents:
            return False
        if agent not in self._ladders.get(role, ()):
            return False
        facts = await self._facts.read(assignment.task_id)
        if not isinstance(facts, CloudFacts):
            return False
        # 3. Exactly one active user (asked again when the send is recorded).
        if self._settings.single_user_only and (
            type(facts.active_users) is not int or facts.active_users != 1
        ):
            return False
        # 4. A project that is not being deleted.
        status = facts.project_status
        if status not in _KNOWN_PROJECT_STATUSES:
            return False
        return status not in _STOPPING_PROJECT_STATUSES

    async def recheck_in(self, session) -> bool:
        """The check of the transaction that records a send (a
        ``DagStore`` ``cloud_send_check``; the planner call's record calls it
        too): the switch, then the single-user recount under the single-user
        lock taken shared in ``session``'s transaction. ``False``: the caller
        writes nothing and sends nothing."""
        if self._settings.enabled is not True:
            return False
        if self._settings.single_user_only:
            return await recheck_single_user_in(session)
        return True


__all__ = [
    "CLI_AGENT_LABELS",
    "CloudFacts",
    "CloudFactsReader",
    "CloudSettings",
    "ConfiguredCloudPolicy",
    "PostgresCloudFacts",
    "recheck_single_user_in",
]
