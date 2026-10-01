"""Version 1 of the public REST / event API, mounted under ``/api/v1``."""

from fastapi import APIRouter

from paw_backend.api.v1 import (
    accounts,
    auth,
    compute,
    events,
    health,
    notifications,
    passkeys,
    system_health,
)

router = APIRouter(prefix="/api/v1")
router.include_router(health.router)
router.include_router(events.router)
router.include_router(auth.router)
router.include_router(passkeys.router)
router.include_router(accounts.router)
router.include_router(system_health.router)
router.include_router(compute.router)
router.include_router(notifications.router)
