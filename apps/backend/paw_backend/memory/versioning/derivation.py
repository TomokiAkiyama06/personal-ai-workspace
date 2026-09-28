"""Which memory versions came from a conversation or a Task (Issue #128).

Decision 0045: the deletion of a conversation or a Task (a later issue) must find
EVERY version whose content came from it, including the versions a person wrote
from such a version by an edit, a restore or a revalidation. What the deletion
then does with them is decided in that issue, not here; this module only finds.

A version is found when

* it has a ``memory_sources`` row naming the conversation (``conversation_id``;
  a message source names its conversation too) or the Task (``source_type =
  'task'`` and ``source_ref`` the canonical text of the id, ``str(task_id)``, as
  ``FreshnessMaintenance.end_task`` matches it), or
* it is a person's version (``actor_type = 'user'``) of the same memory written
  from a found version: its ``attributes`` name that version's number as
  ``edited_from_version``, ``revalidated_from_version`` or
  ``restored_from_version`` (``MemoryVersioningService`` writes exactly these),
  transitively.

Since Decision 0045 such a version carries a copy of the sources as well
(``MemoryVersioningService._carry_sources``), so the first rule finds it alone. The
second rule also finds the versions written before the copies existed, and any
version whose copy was skipped (a source that named nothing any more), without a
backfill.

Backend-internal, like the freshness jobs: there is no caller to authorize (the
deletion flow runs as the ``system`` actor, after its own authorization of the
deletion), and only ids and numbers are returned, never a version's content. A
database error leaves as
:class:`~paw_backend.memory.versioning.errors.MemoryDatabaseError`, detached from
the driver's error.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, StatementError

from paw_backend.db import Database
from paw_backend.memory.versioning.errors import (
    InputProblem,
    MemoryDatabaseError,
    raise_detached,
)
from paw_backend.memory.versioning.validation import reject, validate_uuid

# The ``attributes`` keys by which a person's version names the version it was
# written from (``service.py``: edit, revalidate, restore).
LINEAGE_KEYS = (
    "edited_from_version",
    "revalidated_from_version",
    "restored_from_version",
)

_LINEAGE = " OR ".join(
    f"v.attributes @> jsonb_build_object('{key}', d.version_number)"
    for key in LINEAGE_KEYS
)


def _derived_sql(source_condition: str) -> Any:
    return text(
        "WITH RECURSIVE derived(id, memory_id, version_number) AS ("
        " SELECT v.id, v.memory_id, v.version_number FROM memory_versions v"
        " WHERE v.id IN (SELECT s.memory_version_id FROM memory_sources s"
        f" WHERE {source_condition})"
        " UNION"
        " SELECT v.id, v.memory_id, v.version_number FROM memory_versions v"
        " JOIN derived d ON v.memory_id = d.memory_id"
        f" WHERE v.actor_type = 'user' AND ({_LINEAGE})"
        ") SELECT d.id, d.memory_id, d.version_number,"
        " EXISTS (SELECT 1 FROM memory_sources s"
        f" WHERE s.memory_version_id = d.id AND {source_condition}) AS has_source"
        " FROM derived d ORDER BY d.memory_id, d.version_number"
    )


_FROM_CONVERSATION = _derived_sql("s.conversation_id = CAST(:ref AS uuid)")
_FROM_TASK = _derived_sql("s.source_type = 'task' AND s.source_ref = :ref")


@dataclass(frozen=True, slots=True)
class DerivedVersion:
    """A version whose content came from the conversation or Task looked up.

    ``has_source`` says whether the version itself has a source naming it (else
    it was found through a person's edit, restore or revalidation of one that
    has).
    """

    memory_id: UUID
    version_id: UUID
    version_number: int
    has_source: bool


class MemoryDerivation:
    """The lookup of the versions derived from a conversation or a Task."""

    def __init__(self, database: Database) -> None:
        if not isinstance(database, Database):
            raise reject("database", InputProblem.WRONG_TYPE)
        self._database = database

    async def versions_from_conversation(
        self, conversation_id: UUID
    ) -> tuple[DerivedVersion, ...]:
        """Every version derived from ``conversation_id`` (any status, any scope)."""
        conversation_id = validate_uuid("conversation_id", conversation_id)
        return await self._run(_FROM_CONVERSATION, str(conversation_id))

    async def versions_from_task(self, task_id: UUID) -> tuple[DerivedVersion, ...]:
        """Every version derived from the Task ``task_id`` (any status, any scope)."""
        task_id = validate_uuid("task_id", task_id)
        return await self._run(_FROM_TASK, str(task_id))

    async def _run(self, statement: Any, ref: str) -> tuple[DerivedVersion, ...]:
        failure: MemoryDatabaseError | None = None
        found: tuple[DerivedVersion, ...] = ()
        try:
            async with self._database.session() as session:
                rows = await session.execute(statement, {"ref": ref})
                found = tuple(
                    DerivedVersion(
                        row.memory_id, row.id, row.version_number, row.has_source
                    )
                    for row in rows
                )
        except StatementError as error:
            orig = error.orig if isinstance(error, DBAPIError) else None
            sqlstate = getattr(orig, "sqlstate", None)
            failure = MemoryDatabaseError(
                sqlstate if isinstance(sqlstate, str) else None
            )
        if failure is not None:
            raise_detached(failure)
        return found
