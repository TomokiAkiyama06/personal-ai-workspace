"""PostgreSQL access of the Admin's policy of the CLI agents' models
(revision ``0341``; Decision 0103, 7 A and 9 A; issue #341).

A read is one statement on ``Database.fetch_abortable``; a change is one
transaction on ``Database.transact_abortable`` (one deadline, nothing applied
late): under a transaction-level advisory lock (changes are serialised), the
database's clock read once, each kind's rows replaced, the history rows
appended and the ``admin.agent_models.update`` audit event inserted, all in
the same transaction. An audit event that cannot be written rolls the whole
change back (Decision 0103, 7 A: no record, no change). SQL is written here as
constants; values are always bound by the driver.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

import psycopg

from paw_backend.agent_models.policy import (
    ACTION_UPDATE,
    RESOURCE_KIND,
    AllowedModel,
    KindPolicy,
)
from paw_backend.authz.audit import AUDIT_INSERT_SQL, AuditEvent, audit_values
from paw_backend.connections.domain import ConnectionKind
from paw_backend.db import Database

#: The advisory lock of a change (a fixed, arbitrary bigint: "PAW341").
_LOCK_KEY = 0x504157_0341
_SELECT = (
    "SELECT kind, model, is_default, description, updated_by, updated_at"
    " FROM agent_model_policies ORDER BY kind, is_default DESC, model"
)


@dataclass(frozen=True, slots=True)
class PolicyChange:
    """What a committed change wrote: the new policies of the kinds it named,
    every kind's policy as the change's transaction left it (the answer comes
    from what was committed: no read afterwards) and each named kind's
    default before (``None``: it had no policy, so the runtime
    configuration's model: the caller knows that one)."""

    change_id: uuid.UUID
    policies: Mapping[ConnectionKind, KindPolicy]
    previous_defaults: Mapping[ConnectionKind, str | None]
    committed: Mapping[ConnectionKind, KindPolicy]


def _policies(rows: list[tuple]) -> dict[ConnectionKind, KindPolicy]:
    grouped: dict[ConnectionKind, list[tuple]] = {}
    for row in rows:
        try:
            kind = ConnectionKind(row[0])
        except ValueError:
            continue  # the CHECK allows none: never read as a policy
        grouped.setdefault(kind, []).append(row)
    found = {}
    for kind, entries in grouped.items():
        # A kind without a default (rows the service never writes) has none:
        # ``KindPolicy.default_model`` is ``None`` and nothing guesses one.
        models = tuple(
            AllowedModel(m, bool(d), desc) for _, m, d, desc, _, _ in entries
        )
        found[kind] = KindPolicy(
            kind,
            models,
            updated_by=max(entries, key=lambda e: e[5])[4],
            updated_at=max(entry[5] for entry in entries),
        )
    return found


class AgentModelPolicyStore:
    """See the module. ``timeout_seconds``: the deadline of every call."""

    def __init__(self, database: Database, *, timeout_seconds: float = 3.0) -> None:
        self._database = database
        self._timeout = float(timeout_seconds)

    async def read(self) -> dict[ConnectionKind, KindPolicy]:
        """Every kind's policy (a kind without rows is not in it)."""
        rows = await self._database.fetch_abortable(
            _SELECT, {}, timeout_seconds=self._timeout
        )
        return _policies(rows)

    async def replace(
        self,
        policies: Mapping[ConnectionKind, tuple[AllowedModel, ...]],
        *,
        actor_id: uuid.UUID,
        actor_role: str,
        change_id: uuid.UUID,
    ) -> PolicyChange:
        """Replace the policy of each kind named (checked already:
        :func:`~.policy.check_policy`) and audit it, in one transaction."""

        async def work(connection: psycopg.AsyncConnection) -> PolicyChange:
            await connection.execute(
                "SELECT pg_advisory_xact_lock(%(key)s)", {"key": _LOCK_KEY}
            )
            cursor = await connection.execute("SELECT clock_timestamp()")
            row = await cursor.fetchone()
            assert row is not None
            now: datetime = row[0]
            cursor = await connection.execute(
                "SELECT kind, model FROM agent_model_policies WHERE is_default"
            )
            before = {kind: model for kind, model in await cursor.fetchall()}
            previous: dict[ConnectionKind, str | None] = {}
            written: dict[ConnectionKind, KindPolicy] = {}
            for kind, models in policies.items():
                previous[kind] = before.get(kind.value)
                await connection.execute(
                    "DELETE FROM agent_model_policies WHERE kind = %(kind)s",
                    {"kind": kind.value},
                )
                for entry in models:
                    values = {
                        "kind": kind.value,
                        "model": entry.model,
                        "default": entry.default,
                        "description": entry.description,
                        "actor": actor_id,
                        "now": now,
                    }
                    await connection.execute(
                        "INSERT INTO agent_model_policies (kind, model, is_default,"
                        " description, updated_by, updated_at) VALUES (%(kind)s,"
                        " %(model)s, %(default)s, %(description)s, %(actor)s,"
                        " %(now)s)",
                        values,
                    )
                    await connection.execute(
                        "INSERT INTO agent_model_policy_changes (id, change_id,"
                        " kind, model, is_default, description, changed_by,"
                        " changed_at) VALUES (%(id)s, %(change)s, %(kind)s,"
                        " %(model)s, %(default)s, %(description)s, %(actor)s,"
                        " %(now)s)",
                        {**values, "id": uuid.uuid4(), "change": change_id},
                    )
                written[kind] = KindPolicy(kind, models, actor_id, now)
            event = AuditEvent(
                event_id=uuid.uuid4(),
                correlation_id=change_id,
                occurred_at=now,
                actor_id=actor_id,
                actor_role=actor_role,
                action=ACTION_UPDATE,
                resource_kind=RESOURCE_KIND,
                decision="allow",
                reason="succeeded",
            )
            await connection.execute(AUDIT_INSERT_SQL, audit_values(event))
            cursor = await connection.execute(_SELECT)
            committed = _policies(await cursor.fetchall())
            return PolicyChange(change_id, written, previous, committed)

        return await self._database.transact_abortable(
            work, timeout_seconds=self._timeout
        )


__all__ = ["AgentModelPolicyStore", "PolicyChange"]
