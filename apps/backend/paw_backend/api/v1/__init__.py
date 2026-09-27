"""Version 1 of the public REST / event API, mounted under ``/api/v1``."""

from fastapi import APIRouter

from paw_backend.api.v1 import auth, events, health

router = APIRouter(prefix="/api/v1")
router.include_router(health.router)
router.include_router(events.router)
router.include_router(auth.router)
