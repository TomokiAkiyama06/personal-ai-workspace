"""The System Health tables (PAW-066, Alembic revision ``0066``).

* ``health_metric_samples``: the numeric time series, downsampled by age
  (``REQUIREMENTS.md`` "Observability retention / downsampling", FIXED). One row
  per metric, resolution and bucket: a raw sample (resolution 0, one row per
  sampling interval, ``sample_count = 1``) or an aggregate of 1 minute, 5
  minutes or 1 hour (count, sum, minimum, maximum; the mean is ``sum / count``).
  A row moves to the next resolution when it is old enough (``store.py``), so
  every sample is counted exactly once.
* ``health_events``: a component's severity changed. Kept apart from the series
  and never aggregated ("重要Eventは時系列Metricsと分離し、集約せず保持する").

Both hold codes and numbers only (``domain.py``): no user content, no message.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Double,
    Identity,
    Index,
    Integer,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from paw_backend.db import Base
from paw_backend.health.domain import Component, Severity, Status
from paw_backend.health.limits import (
    HOURLY_RESOLUTION,
    METRIC_NAME_PATTERN,
    TIERS,
)

TABLE_NAMES = ("health_metric_samples", "health_events")
RESOLUTIONS = (*(resolution for resolution, _ in TIERS), HOURLY_RESOLUTION)
MAX_REASONS_CHARS = 500


def _in(column: str, values, name: str) -> CheckConstraint:
    listed = ", ".join(f"'{member.value}'" for member in values)
    return CheckConstraint(f"{column} IN ({listed})", name=name)


class HealthMetricSampleRow(Base):
    __tablename__ = "health_metric_samples"
    __table_args__ = (
        CheckConstraint(f"metric ~ '{METRIC_NAME_PATTERN}'", name="metric_shape"),
        CheckConstraint(
            "resolution_seconds IN ("
            + ", ".join(str(resolution) for resolution in RESOLUTIONS)
            + ")",
            name="resolution_valid",
        ),
        CheckConstraint("sample_count >= 1", name="sample_count_positive"),
        CheckConstraint("value_min <= value_max", name="min_not_above_max"),
        # The roll-up and the purge read one resolution by age.
        Index(
            "ix_health_metric_samples_resolution_seconds_bucket_start",
            "resolution_seconds",
            "bucket_start",
        ),
    )

    metric: Mapped[str] = mapped_column(Text, primary_key=True)
    resolution_seconds: Mapped[int] = mapped_column(Integer, primary_key=True)
    bucket_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True
    )
    sample_count: Mapped[int] = mapped_column(BigInteger)
    value_sum: Mapped[float] = mapped_column(Double)
    value_min: Mapped[float] = mapped_column(Double)
    value_max: Mapped[float] = mapped_column(Double)


class HealthEventRow(Base):
    __tablename__ = "health_events"
    __table_args__ = (
        _in("component", Component, "component_valid"),
        _in("severity", Severity, "severity_valid"),
        CheckConstraint(
            "previous_severity IS NULL OR previous_severity IN ("
            + ", ".join(f"'{member.value}'" for member in Severity)
            + ")",
            name="previous_severity_valid",
        ),
        _in("status", Status, "status_valid"),
        CheckConstraint(
            f"char_length(reasons) <= {MAX_REASONS_CHARS}", name="reasons_length"
        ),
        # "The last event of this component" and the purge by age.
        Index("ix_health_events_component_id", "component", "id"),
        Index("ix_health_events_occurred_at", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    component: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(Text)
    previous_severity: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    # The component's reason codes, comma-separated (closed codes only).
    reasons: Mapped[str] = mapped_column(Text, server_default=text("''"))
