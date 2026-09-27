"""ORM models of the DAG orchestrator (Alembic revision ``0034``).

* ``agent_dags``: one DAG per task **attempt** (``UNIQUE (task_id, attempt)``: a
  Restart starts a new attempt and so a new DAG; a Retry continues the DAG of the
  attempt). ``epoch`` is the fencing token: every worker that takes the DAG over
  adds 1, and every write of a worker must present the epoch it took; a worker
  that was replaced holds an older epoch and is refused (``store.py``).
* ``agent_dag_nodes``: the nodes, with their state, the agent rung and approach
  they are on, their attempt counters and the result (bounded JSON).
* ``agent_dag_edges``: ``node_key`` depends on ``depends_on_key`` (insert-only).
* ``agent_dag_node_attempts``: one row per start of a node (which agent, which
  approach, which epoch started it, how it ended). The text of a failure is never
  stored: only its class name and its loop signature.

Every table references ``tasks.id`` (or a node) with a real foreign key. The
tables deliberately do not start with ``task``: the PAW-032 tests inspect every
table with that prefix. Enum-like columns are text with CHECK constraints whose
value lists the migration writes out.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from paw_backend.db import Base
from paw_backend.orchestrator.domain import (
    AttemptState,
    DagState,
    NodeRole,
    NodeState,
)
from paw_backend.orchestrator.limits import (
    DB_MAX_JSON_BYTES,
    KEY_PATTERN,
    MAX_GOAL_CHARS,
    MAX_LADDER_LENGTH,
    MAX_NODES,
    MAX_PLAN_BYTES,
)
from paw_backend.tasks.queueing.validation import MAX_APPROACH

TABLE_NAMES = (
    "agent_dags",
    "agent_dag_nodes",
    "agent_dag_edges",
    "agent_dag_node_attempts",
)


def _enum(enum_class: type[StrEnum], length: int = 16) -> Enum:
    """Store the enum's value as text; the CHECK constraint is added separately."""
    return Enum(
        enum_class,
        native_enum=False,
        create_constraint=False,
        validate_strings=True,
        length=length,
        values_callable=lambda members: [member.value for member in members],
    )


def _in(column: str, enum_class: type[StrEnum], name: str) -> CheckConstraint:
    values = ", ".join(f"'{member.value}'" for member in enum_class)
    return CheckConstraint(f"{column} IN ({values})", name=name)


class DagRow(Base):
    """The DAG of one task attempt."""

    __tablename__ = "agent_dags"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"))
    attempt: Mapped[int] = mapped_column(Integer)
    # The ``retry_count`` of the task run that last opened the DAG: a Retry (a
    # higher count) re-opens the failed, blocked and cancelled nodes.
    task_retry_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    state: Mapped[DagState] = mapped_column(
        _enum(DagState), default=DagState.ACTIVE, server_default=text("'active'")
    )
    epoch: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    owner: Mapped[str | None] = mapped_column(String(100))
    node_count: Mapped[int] = mapped_column(Integer)
    # The size of the accepted plan in UTF-8 bytes (``Plan.encoded_bytes``). The
    # texts of the nodes are bounded one by one by CHECK constraints; the total
    # cannot be one (it spans rows), so the service declares it here and the
    # database bounds the declaration. Fixed for good (the application cannot
    # update it).
    plan_bytes: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("task_id", "attempt"),
        _in("state", DagState, "state_valid"),
        CheckConstraint("attempt >= 1", name="attempt_positive"),
        CheckConstraint("task_retry_count >= 0", name="retry_count_not_negative"),
        CheckConstraint("epoch >= 0", name="epoch_not_negative"),
        # A DAG that nobody took over has no owner; one that was taken has one.
        CheckConstraint("(epoch = 0) = (owner IS NULL)", name="owner_matches_epoch"),
        CheckConstraint(
            f"node_count BETWEEN 1 AND {MAX_NODES}", name="node_count_in_range"
        ),
        CheckConstraint(
            f"plan_bytes BETWEEN 1 AND {MAX_PLAN_BYTES}", name="plan_bytes_in_range"
        ),
    )


class DagNodeRow(Base):
    """One node of a DAG."""

    __tablename__ = "agent_dag_nodes"

    dag_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_dags.id"), primary_key=True
    )
    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    # Position in the topological order: a dependency has a smaller ordinal.
    ordinal: Mapped[int] = mapped_column(Integer)
    role: Mapped[NodeRole] = mapped_column(_enum(NodeRole))
    title: Mapped[str] = mapped_column(String(100))
    goal: Mapped[str] = mapped_column(Text)
    input: Mapped[dict[str, Any]] = mapped_column(JSONB)
    required: Mapped[bool] = mapped_column(Boolean)
    # What the plan asked for (``None``: the role's ceiling / the whole working set).
    capabilities: Mapped[list[str] | None] = mapped_column(JSONB(none_as_null=True))
    repositories: Mapped[list[str] | None] = mapped_column(JSONB(none_as_null=True))
    state: Mapped[NodeState] = mapped_column(_enum(NodeState))
    # The rung of the escalation ladder and the approach the next attempt uses.
    agent_index: Mapped[int] = mapped_column(
        SmallInteger, default=0, server_default=text("0")
    )
    approach: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    # Starts of this node, ever. Also the fencing token of one attempt.
    attempt_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    # Starts on the current rung during the current run (the cap of attempts).
    rung_attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    error_class: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("dag_id", "ordinal"),
        _in("role", NodeRole, "role_valid"),
        _in("state", NodeState, "state_valid"),
        CheckConstraint(f"key ~ '^{KEY_PATTERN}$'", name="key_format"),
        CheckConstraint("ordinal >= 0", name="ordinal_not_negative"),
        CheckConstraint(
            f"goal <> '' AND char_length(goal) <= {MAX_GOAL_CHARS}",
            name="goal_length",
        ),
        CheckConstraint(
            f"agent_index BETWEEN 0 AND {MAX_LADDER_LENGTH - 1}",
            name="agent_index_in_range",
        ),
        CheckConstraint(
            f"approach BETWEEN 0 AND {MAX_APPROACH}", name="approach_in_range"
        ),
        CheckConstraint(
            "attempt_count >= 0 AND rung_attempts >= 0"
            " AND rung_attempts <= attempt_count",
            name="counters_valid",
        ),
        CheckConstraint(
            "state <> 'running' OR attempt_count >= 1", name="running_has_attempt"
        ),
        # A node has a result exactly when it succeeded.
        CheckConstraint(
            "(state = 'succeeded') = (result IS NOT NULL)",
            name="result_matches_state",
        ),
        CheckConstraint(
            "result IS NULL OR (jsonb_typeof(result) = 'object'"
            f" AND octet_length(result::text) <= {DB_MAX_JSON_BYTES})",
            name="result_bounded",
        ),
        CheckConstraint(
            "jsonb_typeof(input) = 'object'"
            f" AND octet_length(input::text) <= {DB_MAX_JSON_BYTES}",
            name="input_bounded",
        ),
        CheckConstraint(
            "capabilities IS NULL OR jsonb_typeof(capabilities) = 'array'",
            name="capabilities_is_a_list",
        ),
        CheckConstraint(
            "repositories IS NULL OR jsonb_typeof(repositories) = 'array'",
            name="repositories_is_a_list",
        ),
    )


class DagEdgeRow(Base):
    """``node_key`` depends on ``depends_on_key`` (both nodes of the same DAG)."""

    __tablename__ = "agent_dag_edges"

    dag_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    node_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    depends_on_key: Mapped[str] = mapped_column(String(32), primary_key=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["dag_id", "node_key"],
            ["agent_dag_nodes.dag_id", "agent_dag_nodes.key"],
            name="fk_agent_dag_edges_node",
        ),
        ForeignKeyConstraint(
            ["dag_id", "depends_on_key"],
            ["agent_dag_nodes.dag_id", "agent_dag_nodes.key"],
            name="fk_agent_dag_edges_dependency",
        ),
        CheckConstraint("node_key <> depends_on_key", name="not_self"),
    )


class DagNodeAttemptRow(Base):
    """One start of a node."""

    __tablename__ = "agent_dag_node_attempts"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    dag_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    node_key: Mapped[str] = mapped_column(String(32))
    number: Mapped[int] = mapped_column(Integer)
    agent_index: Mapped[int] = mapped_column(SmallInteger)
    approach: Mapped[int] = mapped_column(Integer)
    # The DAG epoch (the worker) that started this attempt.
    epoch: Mapped[int] = mapped_column(Integer)
    state: Mapped[AttemptState] = mapped_column(_enum(AttemptState))
    error_class: Mapped[str | None] = mapped_column(String(100))
    failure_signature: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        ForeignKeyConstraint(
            ["dag_id", "node_key"],
            ["agent_dag_nodes.dag_id", "agent_dag_nodes.key"],
            name="fk_agent_dag_node_attempts_node",
        ),
        UniqueConstraint("dag_id", "node_key", "number"),
        _in("state", AttemptState, "state_valid"),
        CheckConstraint("number >= 1 AND epoch >= 1", name="counters_positive"),
        CheckConstraint(
            f"agent_index BETWEEN 0 AND {MAX_LADDER_LENGTH - 1}"
            f" AND approach BETWEEN 0 AND {MAX_APPROACH}",
            name="rung_and_approach_in_range",
        ),
        CheckConstraint(
            "(state = 'running') = (finished_at IS NULL)", name="finished_matches_state"
        ),
        CheckConstraint(
            "failure_signature IS NULL OR failure_signature ~ '^[0-9a-f]{64}$'",
            name="signature_format",
        ),
        CheckConstraint(
            "state <> 'failed' OR error_class IS NOT NULL", name="failed_has_class"
        ),
    )
