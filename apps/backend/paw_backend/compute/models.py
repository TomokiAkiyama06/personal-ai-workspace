"""ORM model of the local usage (Alembic revision ``0189``, issue #187 item 5,
Decision 0077).

* ``local_usage``: one row per call of a local model through ``HybridRuntime`` (a
  node's attempt or a planner call that held a local lease), written when the
  call ended: the task and its creator, where the scheduler placed it
  (``local_gpu`` / ``local_cpu``), the tokens the runtime reported to the task's
  budget while it ran, and the whole seconds it held the lease. A call that did
  not stop when its node was cancelled adds the time it went on using in a row
  of its own when it ends (``calls`` 0). No model, agent, prompt, answer or text.

The usage report (``connections/store.py``) reads it next to ``connection_usage``.
The application role may SELECT and INSERT only: a row is never changed or
removed (like ``connection_usage``, it is kept).
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    SmallInteger,
    String,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from paw_backend.compute.domain import Placement
from paw_backend.db import Base

LOCAL_USAGE_TABLE = "local_usage"
# Where a local call can run (``Placement`` without the cloud: a node the
# scheduler sends to the cloud is a ``connection_usage`` row of its agent).
LOCAL_PLACEMENTS = (Placement.LOCAL_GPU, Placement.LOCAL_CPU)
# The bounds of one row (the budget's own bound of a consumption).
MAX_LOCAL_TOKENS = 10**15
MAX_LOCAL_SECONDS = 10**12


class LocalUsageRow(Base):
    """One local call (``calls`` 1) or the late time of one (``calls`` 0)."""

    __tablename__ = LOCAL_USAGE_TABLE

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="RESTRICT")
    )
    placement: Mapped[str] = mapped_column(String(16))
    calls: Mapped[int] = mapped_column(SmallInteger)
    tokens: Mapped[int] = mapped_column(BigInteger)
    seconds: Mapped[int] = mapped_column(BigInteger)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "placement IN ("
            + ", ".join(f"'{placement.value}'" for placement in LOCAL_PLACEMENTS)
            + ")",
            name="placement_valid",
        ),
        CheckConstraint("calls IN (0, 1)", name="calls_valid"),
        CheckConstraint(
            f"tokens BETWEEN 0 AND {MAX_LOCAL_TOKENS}", name="tokens_range"
        ),
        CheckConstraint(
            f"seconds BETWEEN 0 AND {MAX_LOCAL_SECONDS}", name="seconds_range"
        ),
        # The report of the workspace: the rows of a period.
        Index("ix_local_usage_started_at", "started_at"),
        # The report of one user.
        Index("ix_local_usage_user_id_started_at", "user_id", "started_at"),
    )
