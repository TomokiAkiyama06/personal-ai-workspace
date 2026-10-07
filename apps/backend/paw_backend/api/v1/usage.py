"""Usage and quotas over HTTP (issue #187, Decision 0069, Proposed; the rules of
quotas are Decision 0016, Approved).

* ``GET /usage?scope=self|workspace&range=last14|last30|month``: the Usage screen's
  report (``connections/report.py``). ``self`` needs ``agent.use`` (one's own),
  ``workspace`` ``admin.usage.view`` (Owner / Admin).
* ``GET /quotas/me``: one's own quotas with what their current windows used
  (``agent.use``).
* ``GET /users/{user_id}/quotas``: a user's quotas (one's own: ``agent.use``;
  another user's: ``admin.usage.view``). 404 for a user that does not exist.
* ``PUT`` / ``DELETE /users/{user_id}/quotas/{kind}/{metric}/{period}``: set
  (a number, ``0`` blocks new tasks, or ``"unlimited"``) or remove one limit
  (``admin.quota.manage``; an Owner's quota by the Owner only). Both need a recent
  **Passkey Step-up** of the session (403 ``step_up_required`` /
  ``step_up_method_insufficient``). A quota that is not set is unlimited
  (Decision 0016, section 2): removing it makes the user unlimited for it.
* ``GET /admin/users``: the workspace's users that are not deleted
  (``admin.users.manage``). Not ``/users``: that path is one a first-user setup
  page would be guessed at, and it stays 404 (PAW-021,
  ``tests/test_owner_no_web_path.py``; Decision 0069).

The routes are guarded by ``require_capability`` (``account.read``: every human
role, for the reads the service authorizes itself; the administrator's capability
for the rest), and the ``ConnectionService`` authorizes again and audits. The
answers hold counts, enums, ids and login names: never a prompt, an answer, a model
name or a credential. The local models' calls, their GPU time and the escalated
tasks are in the report (issue #187 item 5, Decision 0077).

Without a database the routes answer 503 ``service_unavailable``.
"""

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Annotated, Literal

import psycopg
from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from starlette import status

from paw_backend.api.v1.auth import _session_of, get_auth
from paw_backend.auth.errors import (
    AuthUnavailableError,
    InvalidAuthInputError,
    StepUpMethodInsufficientError,
    StepUpRequiredError,
)
from paw_backend.auth.user_directory import list_users, user_exists
from paw_backend.authz import (
    AuditEvent,
    Capability,
    Principal,
    Reason,
    require_capability,
)
from paw_backend.connections import (
    UNLIMITED,
    ConnectionBusyError,
    ConnectionKind,
    ConnectionPermissionDeniedError,
    ConnectionService,
    InvalidConnectionInputError,
    QuotaMetric,
    QuotaPeriod,
    QuotaUsage,
    TargetUserNotFoundError,
    Unlimited,
)
from paw_backend.connections.limits import MAX_QUOTA_LIMIT
from paw_backend.connections.report import UsageRange, UsageReport
from paw_backend.connections.service import ACTION_QUOTA_REMOVE, ACTION_QUOTA_SET
from paw_backend.db import DatabaseDisposedError
from paw_backend.errors import ApiError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["usage"])

_READER = Annotated[Principal, Depends(require_capability(Capability.ACCOUNT_READ))]
_QUOTA_MANAGER = Annotated[
    Principal, Depends(require_capability(Capability.ADMIN_QUOTA_MANAGE))
]
_USER_MANAGER = Annotated[
    Principal, Depends(require_capability(Capability.ADMIN_USERS_MANAGE))
]

# The resource kind of the step-up refusal's audit row (the service's own).
_RESOURCE_QUOTA = "connection_quota"
_AUDIT_TIMEOUT_SECONDS = 3.0


# -- models ---------------------------------------------------------------------------


class QuotaUsageOut(BaseModel):
    kind: Literal["codex", "claude"]
    metric: Literal["requests", "tasks", "tokens", "runtime_seconds"]
    period: Literal["rolling_5h", "day", "week", "month"]
    # A number, or "unlimited" (Decision 0016, section 2).
    limit: int | Literal["unlimited"]
    # In the metric's unit (runtime_seconds: whole seconds).
    used: int
    window_start: datetime
    # When the window resets; null for the rolling 5 hours.
    window_end: datetime | None


class QuotasResponse(BaseModel):
    user_id: uuid.UUID
    # Only the quotas that are set: one that is not set is unlimited.
    quotas: list[QuotaUsageOut]


class TokensOut(BaseModel):
    # The tokens the local runtimes reported (Decision 0077). Nullable in the
    # contract of the screen (null: not recorded); always a number now.
    local: int | None
    external: int


class EscalationsOut(BaseModel):
    # Escalated tasks by cause (Decision 0077, point 5): every escalation of the
    # orchestrator comes from the loop detector, so ``failed`` is 0 for now.
    failed: int
    loop_detected: int


class DailyOut(BaseModel):
    # A calendar day (YYYY-MM-DD) in the time zone of the quotas (Asia/Tokyo).
    date: date
    local: int
    codex: int
    claude: int


class AgentOut(BaseModel):
    agent: Literal["local", "codex", "claude"]
    tasks: int
    tokens: int


class PurposeOut(BaseModel):
    # A category of Decision 0016, section 6 (never free text).
    purpose: Literal["chat", "coding", "review", "research", "evaluation", "other"]
    tasks: int
    tokens: int


class UserUsageOut(BaseModel):
    user_id: uuid.UUID
    login_name: str
    system_role: str
    status: str
    tasks: int
    tokens: int
    quotas: list[QuotaUsageOut]


class UsageResponse(BaseModel):
    scope: Literal["self", "workspace"]
    range: Literal["last14", "last30", "month"]
    # The period: [window_start, window_end), whole calendar days.
    window_start: datetime
    window_end: datetime
    tasks: int
    # The tasks of the period of the same length just before (Decision 0069).
    previous_tasks: int | None
    tokens: TokensOut
    # The GPU time of the local models' calls, in seconds (Decision 0077). Nullable
    # in the contract of the screen (null: not recorded); always a number now.
    gpu_seconds: int | None = None
    escalations: EscalationsOut | None = None
    # Every day of the period, in order.
    daily: list[DailyOut]
    agents: list[AgentOut]
    purposes: list[PurposeOut]
    # The quotas of the user the report is of (workspace: the viewer's own).
    quotas: list[QuotaUsageOut]
    # Every user (workspace); empty for self.
    users: list[UserUsageOut]


class SetQuotaRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # A number (0 blocks new tasks) or "unlimited".
    limit: Annotated[StrictInt, Field(ge=0, le=MAX_QUOTA_LIMIT)] | Literal["unlimited"]


class QuotaOut(BaseModel):
    user_id: uuid.UUID
    kind: Literal["codex", "claude"]
    metric: Literal["requests", "tasks", "tokens", "runtime_seconds"]
    period: Literal["rolling_5h", "day", "week", "month"]
    limit: int | Literal["unlimited"]
    updated_at: datetime


class UserOut(BaseModel):
    user_id: uuid.UUID
    login_name: str
    system_role: str
    status: str
    created_at: datetime


class UsersResponse(BaseModel):
    users: list[UserOut]


# -- helpers --------------------------------------------------------------------------


def _service(request: Request) -> ConnectionService:
    service: ConnectionService | None = getattr(request.app.state, "connections", None)
    if service is None:
        raise ApiError(503, "service_unavailable", "Service temporarily unavailable")
    return service


@contextlib.contextmanager
def _errors() -> Iterator[None]:
    """The connection service's errors as the API's (fixed messages)."""
    try:
        yield
    except ConnectionPermissionDeniedError as error:
        if error.reason is Reason.AUDIT_UNAVAILABLE:
            raise ApiError(
                503, "service_unavailable", "Service temporarily unavailable"
            ) from None
        raise ApiError(403, "forbidden", "Permission denied") from None
    except TargetUserNotFoundError:
        raise ApiError(404, "not_found", "Not Found") from None
    except InvalidConnectionInputError:
        raise ApiError(422, "validation_error", "Invalid request") from None
    except (
        ConnectionBusyError,
        AuthUnavailableError,
        TimeoutError,
        # The user directory's read (``identity/directory.py``).
        psycopg.Error,
        OSError,
        DatabaseDisposedError,
    ):
        raise ApiError(
            503, "service_unavailable", "Service temporarily unavailable"
        ) from None


def _limit_out(limit: int | Unlimited) -> int | Literal["unlimited"]:
    return "unlimited" if isinstance(limit, Unlimited) else limit


def _quota_out(quota: QuotaUsage) -> QuotaUsageOut:
    return QuotaUsageOut(
        kind=quota.kind.value,
        metric=quota.metric.value,
        period=quota.period.value,
        limit=_limit_out(quota.limit),
        used=quota.used,
        window_start=quota.window_start,
        window_end=quota.window_end,
    )


def _report_out(
    report: UsageReport, scope: Literal["self", "workspace"]
) -> UsageResponse:
    by_day = {day: {"local": 0, "codex": 0, "claude": 0} for day in report.days}
    for item in report.daily:
        by_day[item.day][item.kind.value] = item.tasks
    for local in report.local.daily:
        by_day[local.day]["local"] = local.tasks
    agents = [
        AgentOut(agent=item.kind.value, tasks=item.tasks, tokens=item.tokens)
        for item in report.kinds
    ]
    if report.local.tasks:
        agents.insert(
            0,
            AgentOut(
                agent="local", tasks=report.local.tasks, tokens=report.local.tokens
            ),
        )
    return UsageResponse(
        scope=scope,
        range=report.range.value,
        window_start=report.window_start,
        window_end=report.window_end,
        tasks=report.tasks,
        previous_tasks=report.previous_tasks,
        tokens=TokensOut(local=report.local.tokens, external=report.tokens),
        gpu_seconds=report.local.gpu_seconds,
        escalations=EscalationsOut(
            failed=report.escalations.failed,
            loop_detected=report.escalations.loop_detected,
        ),
        daily=[DailyOut(date=day, **counts) for day, counts in by_day.items()],
        agents=agents,
        purposes=[
            PurposeOut(purpose=item.purpose.value, tasks=item.tasks, tokens=item.tokens)
            for item in report.purposes
        ],
        quotas=[_quota_out(quota) for quota in report.quotas],
        users=[
            UserUsageOut(
                user_id=user.user_id,
                login_name=user.login_name,
                system_role=user.system_role,
                status=user.status,
                tasks=user.tasks,
                tokens=user.tokens,
                quotas=[_quota_out(quota) for quota in user.quotas],
            )
            for user in report.users or ()
        ],
    )


async def _require_step_up(
    request: Request, principal: Principal, action: str, user_id: uuid.UUID
) -> None:
    """The session's Passkey Step-up (Decision 0069); a refusal is audited as a
    denial of ``action`` with the step-up reason, then answered 403."""
    auth = get_auth(request)
    session_id = _session_of(request).session.record.id
    try:
        await auth.service.require_passkey_step_up(principal, session_id)
    except StepUpMethodInsufficientError:
        await _audit_refusal(
            request, principal, action, "step_up_method_insufficient", user_id
        )
        raise ApiError(
            403,
            "step_up_method_insufficient",
            "This operation needs a Passkey step-up",
        ) from None
    except StepUpRequiredError:
        await _audit_refusal(request, principal, action, "step_up_required", user_id)
        raise ApiError(
            403, "step_up_required", "A recent step-up authentication is required"
        ) from None
    except InvalidAuthInputError:
        raise ApiError(422, "validation_error", "Invalid request") from None
    except AuthUnavailableError:
        raise ApiError(
            503, "service_unavailable", "Service temporarily unavailable"
        ) from None


async def _audit_refusal(
    request: Request,
    principal: Principal,
    action: str,
    reason: str,
    user_id: uuid.UUID,
) -> None:
    """Best effort, like the service's own events: a failure is logged by type."""
    try:
        event = AuditEvent(
            event_id=uuid.uuid4(),
            correlation_id=uuid.uuid4(),
            occurred_at=datetime.now(UTC),
            actor_id=principal.user_id,
            actor_role=principal.system_role.value,
            action=action,
            resource_kind=_RESOURCE_QUOTA,
            resource_id=user_id,
            project_id=None,
            decision="deny",
            reason=reason,
        )
        async with asyncio.timeout(_AUDIT_TIMEOUT_SECONDS):
            await get_auth(request).audit_sink.record(event)
    except Exception as error:
        logger.error("quota step-up audit write failed (%s)", type(error).__name__)


# -- routes ---------------------------------------------------------------------------


@router.get(
    "/usage",
    response_model=UsageResponse,
    summary="The usage of one's own (self) or of the workspace, over a period",
)
async def usage(
    request: Request,
    principal: _READER,
    scope: Annotated[Literal["self", "workspace"], Query()] = "self",
    range_: Annotated[
        Literal["last14", "last30", "month"], Query(alias="range")
    ] = "last14",
) -> UsageResponse:
    service = _service(request)
    with _errors():
        if scope == "workspace":
            report = await service.workspace_usage_report(principal, UsageRange(range_))
        else:
            report = await service.usage_report(
                principal, UsageRange(range_), principal.user_id
            )
    return _report_out(report, scope)


@router.get(
    "/quotas/me",
    response_model=QuotasResponse,
    summary="One's own quotas and what their current windows used",
)
async def my_quotas(request: Request, principal: _READER) -> QuotasResponse:
    service = _service(request)
    with _errors():
        quotas = await service.quota_status(principal, principal.user_id)
    return QuotasResponse(
        user_id=principal.user_id, quotas=[_quota_out(quota) for quota in quotas]
    )


@router.get(
    "/admin/users",
    response_model=UsersResponse,
    summary="The workspace's users that are not deleted",
)
async def users(request: Request, _: _USER_MANAGER) -> UsersResponse:
    database = request.app.state.database
    if not database.configured:
        raise ApiError(503, "service_unavailable", "Service temporarily unavailable")
    with _errors():
        found = await list_users(
            database,
            timeout_seconds=request.app.state.settings.database_timeout_seconds,
        )
    return UsersResponse(
        users=[
            UserOut(
                user_id=user.user_id,
                login_name=user.login_name,
                system_role=user.system_role,
                status=user.status,
                created_at=user.created_at,
            )
            for user in found
        ]
    )


@router.get(
    "/users/{user_id}/quotas",
    response_model=QuotasResponse,
    summary="A user's quotas (another user's: admin.usage.view)",
)
async def user_quotas(
    user_id: uuid.UUID, request: Request, principal: _READER
) -> QuotasResponse:
    service = _service(request)
    with _errors():
        quotas = await service.quota_status(principal, user_id)
        # Allowed to see them: an unknown user is told apart from one without
        # quotas (a user who may not see them never gets here).
        if not quotas and not await user_exists(
            request.app.state.database,
            user_id,
            timeout_seconds=request.app.state.settings.database_timeout_seconds,
        ):
            raise TargetUserNotFoundError()
    return QuotasResponse(
        user_id=user_id, quotas=[_quota_out(quota) for quota in quotas]
    )


@router.put(
    "/users/{user_id}/quotas/{kind}/{metric}/{period}",
    response_model=QuotaOut,
    summary=(
        "Set a user's limit for a kind, metric and period: a number or "
        '"unlimited" (needs a recent Passkey step-up)'
    ),
)
async def set_quota(
    user_id: uuid.UUID,
    kind: ConnectionKind,
    metric: QuotaMetric,
    period: QuotaPeriod,
    body: SetQuotaRequest,
    request: Request,
    principal: _QUOTA_MANAGER,
) -> QuotaOut:
    service = _service(request)
    await _require_step_up(request, principal, ACTION_QUOTA_SET, user_id)
    with _errors():
        quota = await service.set_quota(
            principal,
            user_id,
            kind,
            metric,
            period,
            UNLIMITED if body.limit == "unlimited" else body.limit,
        )
    return QuotaOut(
        user_id=quota.user_id,
        kind=quota.kind.value,
        metric=quota.metric.value,
        period=quota.period.value,
        limit=_limit_out(quota.limit),
        updated_at=quota.updated_at,
    )


@router.delete(
    "/users/{user_id}/quotas/{kind}/{metric}/{period}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary=("Remove one limit (not set = unlimited; needs a recent Passkey step-up)"),
)
async def remove_quota(
    user_id: uuid.UUID,
    kind: ConnectionKind,
    metric: QuotaMetric,
    period: QuotaPeriod,
    request: Request,
    principal: _QUOTA_MANAGER,
) -> Response:
    service = _service(request)
    await _require_step_up(request, principal, ACTION_QUOTA_REMOVE, user_id)
    with _errors():
        removed = await service.remove_quota(principal, user_id, kind, metric, period)
    if not removed:
        raise ApiError(404, "not_found", "Not Found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
