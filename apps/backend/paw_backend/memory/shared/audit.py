"""The completion record of a Shared Memory change (PAW-046, Decision 0009 s.13).

The Authorizer writes its ``allow`` row (the *attempt*) before the service
touches the database, and that is on purpose: no change without a record.
The price is that the row cannot say whether the change then happened. So every
operation that changes Shared Memory appends one more ``audit_events`` row, the
*completion*, **in the same database transaction as the change**:

* it exists if and only if the change committed (a rollback takes it back, and
  a completion that cannot be written takes the change back: fail-closed),
* it names the actor and the role, the operation (``action`` is the capability of
  the attempt, so the two rows of one call are found by ``correlation_id``), the
  resource and the transition time (``occurred_at``: the service clock, the same
  reading that stamps a new version; ``recorded_at`` is the database's).

It is the record of the *operation*: who managed Shared Memory, and that the
change committed. A delete or a restore, which change ``status`` and nothing else,
keep their actor and time here, and also in ``memory_metadata_changes`` (Decision
0026, which supersedes only the statements of Decision 0009 that made this row
the only such record): the database records every ``status`` change of a version
there, whatever wrote it. ``memory_versions`` gets no new column (PAW-040 lets the
application update ``status`` only).

The row is written by the service into the ``audit_events`` table itself, not
through the Authorizer's ``AuditSink``: a sink writes in a transaction of its
own, after which the change and its record could part. ``audit_events`` stays
append-only for the application role (INSERT and SELECT); this is one more
INSERT. A deployment that swaps the sink of the Authorizer still gets the
completion rows in this table (there is only ``PostgresAuditSink`` today).

The row holds ids, enum values and the fixed reason ``completed`` only (never
memory content or free text). ``decision`` is ``allow`` because the column
permits nothing else (a CHECK of PAW-025); ``reason`` tells the two rows apart.
There is no failure record: one would have to be written after the rollback,
outside the transaction, so its absence would prove nothing, while the absence
of a completion row proves that nothing committed.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.authz import AuditEvent, Capability, Principal
from paw_backend.authz.models import AuditEventRecord

COMPLETED_REASON = "completed"

_TABLE = AuditEventRecord.__table__


def completion_event(
    actor: Principal,
    capability: Capability,
    resource_kind: str,
    resource_id: UUID,
    correlation_id: UUID,
    occurred_at: datetime,
) -> AuditEvent:
    """The completion of the change the attempt ``correlation_id`` asked for."""
    return AuditEvent(
        event_id=uuid4(),
        correlation_id=correlation_id,
        occurred_at=occurred_at,
        actor_id=actor.user_id,
        actor_role=actor.system_role.value,
        action=capability.value,
        resource_kind=resource_kind,
        resource_id=resource_id,
        decision="allow",
        reason=COMPLETED_REASON,
    )


async def record_completion(session: AsyncSession, event: AuditEvent) -> None:
    """Append ``event`` to ``audit_events`` in the transaction of ``session``."""
    values = event.model_dump()
    values["id"] = values.pop("event_id")
    await session.execute(insert(_TABLE).values(**values))
