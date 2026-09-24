"""Health endpoints for process supervisors and load balancers."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel

from paw_backend.api.deps import get_database
from paw_backend.db import Database, DatabaseStatus

router = APIRouter(tags=["health"])


class Liveness(BaseModel):
    status: Literal["ok"] = "ok"


class Readiness(BaseModel):
    """Stable body of ``/health/ready``: same shape for 200 and 503."""

    status: Literal["ok", "unavailable"]
    checks: dict[str, DatabaseStatus]


@router.get("/health", summary="Liveness (no dependencies checked)")
async def liveness() -> Liveness:
    return Liveness()


@router.get(
    "/health/ready",
    summary="Readiness (checks PostgreSQL)",
    responses={503: {"model": Readiness, "description": "A dependency is unavailable"}},
)
async def readiness(
    response: Response, database: Annotated[Database, Depends(get_database)]
) -> Readiness:
    database_status = await database.check()
    ready = database_status is DatabaseStatus.OK
    if not ready:
        response.status_code = 503
    return Readiness(
        status="ok" if ready else "unavailable", checks={"database": database_status}
    )
