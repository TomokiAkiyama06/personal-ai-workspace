"""System Health (PAW-066, Decision 0059 Proposed): ``/api/v1/system/health*``.

* ``GET /system/health/summary`` (``system_health.summary.read``, every human
  role): the compact state for the Global Header, the overall severity and
  whether Codex / Claude are available. Nothing else: the components, their
  numbers and the history are the detail.
* ``GET /system/health`` (``admin.system_health.view``, Owner / Admin): every
  component with its severity, status, reason codes, metrics and parts.
* ``GET /system/health/metrics/{name}`` (the same): one metric's time series,
  aggregated to at most ``MAX_SERIES_POINTS`` points.
* ``GET /system/health/events`` (the same): the latest severity changes.

Every answer holds codes and numbers only (``paw_backend.health``). The report is
reused for ``REPORT_MAX_AGE_SECONDS`` (the sources are not read once per
request). Without a database the series and the events answer 503.
"""

import logging
import math
import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import BaseModel

from paw_backend.authz import Capability, Principal, require_capability
from paw_backend.errors import ApiError
from paw_backend.health import limits
from paw_backend.health.domain import Component, HealthReport, MetricValue
from paw_backend.health.monitor import HealthMonitor
from paw_backend.health.store import HealthStore

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/system/health", tags=["system health"])

_METRIC_NAME = re.compile(limits.METRIC_NAME_PATTERN)
_Severity = Literal["info", "warning", "error", "critical"]


class ComponentResponse(BaseModel):
    component: str
    severity: _Severity
    status: str
    reasons: list[str]
    metrics: dict[str, MetricValue]
    parts: list[dict[str, MetricValue]]


class ReportResponse(BaseModel):
    severity: _Severity
    checked_at: datetime
    components: list[ComponentResponse]


class SummaryResponse(BaseModel):
    severity: _Severity
    checked_at: datetime
    # "Claude: Available / Unavailable" (docs/UI_DESIGN.md), by connection kind.
    connections: dict[str, Literal["available", "unavailable"]]


class SeriesPointResponse(BaseModel):
    bucket_start: datetime
    count: int
    mean: float
    min: float
    max: float


class SeriesResponse(BaseModel):
    metric: str
    since: datetime
    until: datetime
    step_seconds: int
    points: list[SeriesPointResponse]


class EventResponse(BaseModel):
    id: int
    occurred_at: datetime
    component: str
    severity: _Severity
    previous_severity: _Severity | None
    status: str
    reasons: list[str]


class EventsResponse(BaseModel):
    events: list[EventResponse]


def _monitor(request: Request) -> HealthMonitor:
    return request.app.state.system_health.monitor


def _store(request: Request) -> HealthStore:
    # Declared after the capability in each route: an anonymous request is
    # refused before it learns whether a database is configured.
    store = request.app.state.system_health.store
    if store is None:
        raise ApiError(503, "service_unavailable", "Service temporarily unavailable")
    return store


Monitor = Annotated[HealthMonitor, Depends(_monitor)]
Store = Annotated[HealthStore, Depends(_store)]
_view = require_capability(Capability.ADMIN_SYSTEM_HEALTH_VIEW)
_summary = require_capability(Capability.SYSTEM_HEALTH_SUMMARY_READ)


@router.get("/summary", summary="The compact System Health state")
async def summary(
    monitor: Monitor, _: Annotated[Principal, Depends(_summary)]
) -> SummaryResponse:
    report = await monitor.report()
    connections = report.component(Component.CONNECTIONS)
    available = {}
    if connections is not None:
        for part in connections.parts:
            available[str(part["kind"])] = (
                "available" if part.get("available") is True else "unavailable"
            )
    for kind in ("codex", "claude"):
        available.setdefault(kind, "unavailable")
    return SummaryResponse(
        severity=report.severity.value,
        checked_at=report.checked_at,
        connections=available,
    )


@router.get("", summary="Every System Health component (Owner / Admin)")
async def detail(
    monitor: Monitor, _: Annotated[Principal, Depends(_view)]
) -> ReportResponse:
    return _report(await monitor.report())


def _report(report: HealthReport) -> ReportResponse:
    return ReportResponse(
        severity=report.severity.value,
        checked_at=report.checked_at,
        components=[
            ComponentResponse(
                component=health.component.value,
                severity=health.severity.value,
                status=health.status.value,
                reasons=list(health.reasons),
                metrics=dict(health.metrics),
                parts=[dict(part) for part in health.parts],
            )
            for health in report.components
        ],
    )


@router.get("/metrics/{name}", summary="One metric's time series (Owner / Admin)")
async def series(
    _: Annotated[Principal, Depends(_view)],
    store: Store,
    name: Annotated[str, Path(max_length=170)],
    since: datetime | None = None,
    until: datetime | None = None,
    step_seconds: Annotated[int | None, Query(ge=10, le=86_400)] = None,
) -> SeriesResponse:
    if not _METRIC_NAME.fullmatch(name):
        raise ApiError(422, "validation_error", "Unknown metric name")
    now = datetime.now(UTC)
    until = _aware(until) if until is not None else now
    since = (
        _aware(since)
        if since is not None
        else until - timedelta(seconds=limits.DEFAULT_SERIES_SPAN_SECONDS)
    )
    span = (until - since).total_seconds()
    if span <= 0 or span > limits.MAX_SERIES_SPAN_SECONDS:
        raise ApiError(422, "validation_error", "The time range is not valid")
    # At most MAX_SERIES_POINTS buckets, never finer than 10 seconds.
    step = max(step_seconds or 0, 10, math.ceil(span / limits.MAX_SERIES_POINTS))
    try:
        points = await store.series(
            name,
            since=since,
            until=until,
            step_seconds=step,
            limit=limits.MAX_SERIES_POINTS + 1,
        )
    except Exception as error:
        raise _unavailable(error) from None
    return SeriesResponse(
        metric=name,
        since=since,
        until=until,
        step_seconds=step,
        points=[
            SeriesPointResponse(
                bucket_start=point.bucket_start,
                count=point.count,
                mean=point.mean,
                min=point.minimum,
                max=point.maximum,
            )
            for point in points[: limits.MAX_SERIES_POINTS]
        ],
    )


@router.get("/events", summary="The latest severity changes (Owner / Admin)")
async def events(
    _: Annotated[Principal, Depends(_view)],
    store: Store,
    since: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=limits.MAX_EVENTS)] = limits.DEFAULT_EVENTS,
) -> EventsResponse:
    since = (
        _aware(since) if since is not None else datetime.now(UTC) - timedelta(days=30)
    )
    try:
        found = await store.events(since=since, limit=limit)
    except Exception as error:
        raise _unavailable(error) from None
    return EventsResponse(
        events=[
            EventResponse(
                id=event.id,
                occurred_at=event.occurred_at,
                component=event.component,
                severity=event.severity,
                previous_severity=event.previous_severity,
                status=event.status,
                reasons=list(event.reasons),
            )
            for event in found
        ]
    )


def _unavailable(error: Exception) -> ApiError:
    # PostgreSQL did not answer in time or failed: named by type in the log only.
    logger.warning(
        "Reading the System Health history failed (%s)", type(error).__name__
    )
    return ApiError(503, "service_unavailable", "Service temporarily unavailable")


def _aware(value: datetime) -> datetime:
    """A time with its offset: one without is ambiguous and refused."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ApiError(422, "validation_error", "A time needs its UTC offset")
    return value
