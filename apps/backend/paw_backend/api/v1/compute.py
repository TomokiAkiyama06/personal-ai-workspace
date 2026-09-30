"""Kaggle / Full GPU Mode over HTTP (PAW-037, Decision 0055 8; issue #165,
Decision 0058, Proposed).

``/api/v1/admin/compute/full-gpu``: ``GET`` the state, ``POST`` to start (the GPU
for one exclusive job), ``DELETE`` to end. Every operation needs
``admin.compute.full_gpu`` (Owner / Admin, not delegable, audited; no Passkey
Step-up, Decision 0055 1), and ``FullGpuMode`` authorizes the principal again for
a start and an end (its own audited decision). The behaviour is in
``paw_backend.compute`` (``full_gpu.py``, ``wiring.py``); this module only
translates.

* ``POST`` answers ``202`` at once: holding the tasks and draining the local GPU
  work take up to ``drain_seconds`` (default 600), so the start runs in the
  background and its end is read with ``GET`` (``state`` ``on``, or
  ``last_failure``). ``409`` while a start is pending or the mode is on.
* ``DELETE`` ends the mode (the models are loaded again and the held tasks
  resume once the main LLM is back), or abandons a start that is still pending.
  ``409`` when there is nothing to end.
* ``503`` (``compute_not_configured``) when the deployment runs no scheduler
  (``create_app(compute=...)``) or has no database (holding tasks needs one).

The answers name no task, pid, process or command: counts, byte sizes and
states only (the models' names are the administrator's configuration).
"""

from contextlib import contextmanager
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt
from starlette import status

from paw_backend.authz import Capability, Principal, Reason, require_capability
from paw_backend.compute import (
    FullGpuModeStateError,
    FullGpuPermissionDeniedError,
    FullGpuState,
    InvalidComputeArgumentError,
)
from paw_backend.compute.wiring import ComputeServices, FullGpuController
from paw_backend.errors import ApiError

router = APIRouter(prefix="/admin/compute", tags=["admin", "compute"])

# The bounds FullGpuMode checks (``check_seconds``), and a byte count far above
# any GPU (``compute/config.py``).
_MAX_SECONDS = 86_400
_MAX_BYTES = 1 << 50

_FULL_GPU = Annotated[
    Principal, Depends(require_capability(Capability.ADMIN_COMPUTE_FULL_GPU))
]


class StartFullGpuRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Ask the local GPU work still running after the drain time to stop (its
    # lease is revoked; nothing is killed). Default: give up instead.
    preempt: StrictBool = False
    # How long the running local GPU work may take to end (default: 600 s).
    drain_seconds: float | None = Field(default=None, ge=0, le=_MAX_SECONDS)
    # What the job needs free (default: the GPU but the safety headroom and the
    # other workloads' memory).
    vram_bytes: StrictInt | None = Field(default=None, ge=1, le=_MAX_BYTES)


class VramOut(BaseModel):
    total_bytes: int
    observed_free_bytes: int
    external_bytes: int
    headroom_bytes: int
    available_bytes: int


class GpuOut(BaseModel):
    # The scheduler's mode (``SchedulerMode``).
    mode: str
    probe_ok: bool
    vram: VramOut | None
    vram_waiting: int
    exclusive_waiting_for_vram: bool


class VramWarningOut(BaseModel):
    occurred_at: datetime
    work: str
    resource_class: str | None
    requested_bytes: int
    observed_free_bytes: int
    external_bytes: int
    headroom_bytes: int
    gave_up: bool


class FullGpuResponse(BaseModel):
    state: Literal["off", "starting", "on", "resuming"]
    # A start was accepted and has not finished (``state`` is ``starting``).
    start_pending: bool
    # The tasks this process held since the mode was last started.
    held_tasks: int
    preempted: bool
    on_seconds: float | None
    last_failure: str | None
    # The main LLM is not back after the end: the held tasks keep waiting.
    needs_human: bool
    gpu: GpuOut
    # The latest warnings that work waits for VRAM another workload holds
    # (Decision 0042), newest last, and how many this process had in all.
    vram_warnings: list[VramWarningOut]
    vram_warnings_total: int


def _services(request: Request) -> tuple[ComputeServices, FullGpuController]:
    services: ComputeServices | None = getattr(request.app.state, "compute", None)
    if services is None or services.full_gpu is None:
        raise ApiError(
            503, "compute_not_configured", "The compute scheduler is not configured"
        )
    return services, services.full_gpu


def _answer(
    services: ComputeServices, controller: FullGpuController
) -> FullGpuResponse:
    mode = controller.status()
    pending = controller.start_pending
    state = mode.state
    if pending and state is not FullGpuState.ON:
        # Accepted, still being authorized or holding and draining.
        state = FullGpuState.STARTING
    scheduler = services.scheduler.status()
    vram = scheduler.vram
    return FullGpuResponse(
        state=state.value,
        start_pending=pending,
        held_tasks=mode.held_tasks,
        preempted=mode.preempted,
        on_seconds=mode.on_seconds,
        last_failure=None if mode.last_failure is None else mode.last_failure.value,
        needs_human=mode.needs_human,
        gpu=GpuOut(
            mode=scheduler.mode.value,
            probe_ok=scheduler.probe_ok,
            vram=None
            if vram is None
            else VramOut(
                total_bytes=vram.total,
                observed_free_bytes=max(0, vram.observed_free),
                external_bytes=vram.external,
                headroom_bytes=vram.headroom,
                available_bytes=vram.available,
            ),
            vram_waiting=scheduler.vram_waiting,
            exclusive_waiting_for_vram=scheduler.exclusive_waiting_for_vram,
        ),
        vram_warnings=[
            VramWarningOut(
                occurred_at=item.occurred_at,
                work=item.event.work.value,
                resource_class=None
                if item.event.resource_class is None
                else item.event.resource_class.value,
                requested_bytes=item.event.requested_bytes,
                observed_free_bytes=max(0, item.event.observed_free_bytes),
                external_bytes=item.event.external_bytes,
                headroom_bytes=item.event.headroom_bytes,
                gave_up=item.event.gave_up,
            )
            for item in services.warnings.recent()
        ],
        vram_warnings_total=services.warnings.total,
    )


@contextmanager
def _compute_errors():
    try:
        yield
    except FullGpuModeStateError as error:
        raise ApiError(
            409, "full_gpu_mode_state", f"Full GPU Mode is {error.state}"
        ) from None
    except FullGpuPermissionDeniedError as error:
        if error.reason is Reason.AUDIT_UNAVAILABLE:
            raise ApiError(
                503, "service_unavailable", "Service temporarily unavailable"
            ) from None
        raise ApiError(403, "forbidden", "Permission denied") from None
    except InvalidComputeArgumentError:
        raise ApiError(422, "validation_error", "Invalid request") from None


@router.get(
    "/full-gpu",
    response_model=FullGpuResponse,
    summary="The state of Kaggle / Full GPU Mode and of the GPU",
)
async def full_gpu_status(request: Request, _: _FULL_GPU) -> FullGpuResponse:
    services, controller = _services(request)
    return _answer(services, controller)


@router.post(
    "/full-gpu",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=FullGpuResponse,
    summary=(
        "Start Kaggle / Full GPU Mode: hold the local GPU tasks, drain, unload "
        "(runs in the background; read the state with GET)"
    ),
)
async def start_full_gpu(
    request: Request, principal: _FULL_GPU, body: StartFullGpuRequest | None = None
) -> FullGpuResponse:
    services, controller = _services(request)
    body = body or StartFullGpuRequest()
    with _compute_errors():
        controller.start(
            principal,
            vram_bytes=body.vram_bytes,
            drain_seconds=body.drain_seconds,
            preempt=body.preempt,
        )
    return _answer(services, controller)


@router.delete(
    "/full-gpu",
    response_model=FullGpuResponse,
    summary=(
        "End Kaggle / Full GPU Mode (the models are loaded again and the held "
        "tasks resume), or abandon a start that is still pending"
    ),
)
async def end_full_gpu(request: Request, principal: _FULL_GPU) -> FullGpuResponse:
    services, controller = _services(request)
    with _compute_errors():
        await controller.end(principal)
    return _answer(services, controller)
