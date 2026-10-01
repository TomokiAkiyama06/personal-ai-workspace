"""A person's Notification Center (issue #188, Decision 0070 Approved):
``/api/v1/notifications``.

* ``GET /notifications`` (``notification.read``): the newest notifications the
  user receives that they have not dismissed (``limit``, default 100, at most
  200), newest first, and how many are unread in all.
* ``POST /notifications/read`` (``notification.manage``): ``{"ids": [...]}``
  (at most 200) or ``{"all": true}`` become read for this user, on every device.
* ``POST /notifications/{id}/dismiss`` (``notification.manage``): the
  notification's entry (it and the earlier notifications of its key) is
  dismissed for this user. ``404`` when the user does not receive it.

Which notifications a user receives is the store's (``paw_backend.notifications``):
their own and those of the audiences their role holds now; another user's
notification is "not found", never "forbidden". A notification holds codes and
numbers (``kind``, ``params``): the Web App words it. A change is announced to
the user's other streams (``notification.changed``). No Passkey Step-up: these
change only the user's own view. ``503`` without a database or when it does not
answer.
"""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator
from starlette import status

from paw_backend.authz import Capability, Principal, require_capability
from paw_backend.errors import ApiError
from paw_backend.events import EventBus, notification_changed
from paw_backend.notifications import (
    NotificationStore,
    NotificationsUnavailableError,
    audience_capabilities,
)
from paw_backend.notifications.store import DEFAULT_PAGE, MAX_PAGE, MAX_READ_IDS

router = APIRouter(prefix="/notifications", tags=["notifications"])

_Severity = Literal["info", "warning", "error", "critical"]
_READ = Annotated[Principal, Depends(require_capability(Capability.NOTIFICATION_READ))]
_MANAGE = Annotated[
    Principal, Depends(require_capability(Capability.NOTIFICATION_MANAGE))
]


class NotificationOut(BaseModel):
    id: uuid.UUID
    # Notifications with the same key are one entry of the Notification Center.
    key: str
    kind: str
    severity: _Severity
    category: Literal["task", "system"]
    project_id: uuid.UUID | None
    params: dict[str, str | int | float | bool | None | list[str]]
    created_at: datetime
    read: bool


class NotificationsResponse(BaseModel):
    notifications: list[NotificationOut]
    unread: int


class MarkReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: list[uuid.UUID] | None = Field(default=None, max_length=MAX_READ_IDS)
    all: StrictBool = False

    @model_validator(mode="after")
    def _one_of(self) -> "MarkReadRequest":
        if (self.ids is None) == (not self.all):
            raise ValueError("give either ids or all: true")
        return self


class MarkReadResponse(BaseModel):
    updated: int
    unread: int


def _store(request: Request) -> NotificationStore:
    # Declared after the capability in each route: an anonymous request is
    # refused before it learns whether a database is configured.
    store = getattr(request.app.state, "notifications", None)
    if store is None:
        raise _unavailable()
    return store


Store = Annotated[NotificationStore, Depends(_store)]


def _unavailable() -> ApiError:
    return ApiError(503, "service_unavailable", "Service temporarily unavailable")


def _audiences(request: Request, principal: Principal) -> tuple[str, ...]:
    return audience_capabilities(principal, request.app.state.authorizer.policy)


def _announce(request: Request, principal: Principal) -> None:
    """Tell the user's other streams (another device) to read the list again."""
    bus: EventBus = request.app.state.event_bus
    bus.publish(notification_changed(user_ids=frozenset({principal.user_id})))


@router.get("", summary="The user's notifications, newest first, and the unread count")
async def list_notifications(
    request: Request,
    principal: _READ,
    store: Store,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
) -> NotificationsResponse:
    try:
        page = await store.page(
            principal.user_id, _audiences(request, principal), limit=limit
        )
    except NotificationsUnavailableError:
        raise _unavailable() from None
    return NotificationsResponse(
        notifications=[
            NotificationOut(
                id=item.id,
                key=item.key,
                kind=item.kind,
                severity=item.severity.value,
                category=item.category.value,
                project_id=item.project_id,
                params=dict(item.params),
                created_at=item.created_at,
                read=item.read,
            )
            for item in page.items
        ],
        unread=page.unread,
    )


@router.post("/read", summary="Mark notifications (or all of them) read")
async def mark_read(
    body: MarkReadRequest, request: Request, principal: _MANAGE, store: Store
) -> MarkReadResponse:
    audiences = _audiences(request, principal)
    try:
        result = await store.mark_read(
            principal.user_id, audiences, None if body.all else body.ids
        )
    except NotificationsUnavailableError:
        raise _unavailable() from None
    if result.updated:
        _announce(request, principal)
    return MarkReadResponse(updated=result.updated, unread=result.unread)


@router.post(
    "/{notification_id}/dismiss",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Dismiss a notification's entry (it and the earlier ones of its key)",
)
async def dismiss(
    notification_id: uuid.UUID, request: Request, principal: _MANAGE, store: Store
) -> Response:
    try:
        found = await store.dismiss(
            principal.user_id, _audiences(request, principal), notification_id
        )
    except NotificationsUnavailableError:
        raise _unavailable() from None
    if not found:
        raise ApiError(404, "not_found", "Not Found")
    _announce(request, principal)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
