"""Warnings that work waits for VRAM another workload holds (Decision 0042).

Processes the scheduler does not manage (another user's vLLM, a notebook) are
invisible to its leases; the probe sees them. When work that needs VRAM of its
own, an Exclusive job or a model load has to wait because the probe does not
show enough free, the scheduler logs a warning and tells the injected
:class:`VramWarningSink` (the hook a notification or System Health (PAW-066)
implements; the scheduler itself writes no audit record, Decision 0037 1).

The same kind of warning (what waits, its class, and whether it gave up) is
repeated at most once per ``vram_warning_interval_seconds``. Nothing in a
warning names a process, a pid or a command's output: only byte counts of the
whole GPU and the kind of work.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from paw_backend.compute.domain import ResourceClass

logger = logging.getLogger("paw_backend.compute")

MIB = 1024**2


class DeferredWork(StrEnum):
    REQUEST = "request"  # shared-class work with ``vram_bytes`` of its own
    EXCLUSIVE = "exclusive"  # an Exclusive job
    MODEL_LOAD = "model_load"  # a model the residency policy wants on the GPU


@dataclass(frozen=True, slots=True)
class VramDeferral:
    """One warning. ``gave_up``: the work stopped waiting (its wait ran out)."""

    work: DeferredWork
    resource_class: ResourceClass | None
    requested_bytes: int
    observed_free_bytes: int
    external_bytes: int
    headroom_bytes: int
    gave_up: bool = False


class VramWarningSink(Protocol):
    def vram_deferred(self, event: VramDeferral) -> None:
        """Called on the event loop; must not block. An exception is logged by
        its type and otherwise ignored."""
        ...


class VramWarnings:
    """Rate-limits the warnings and hands them to the log and the sink."""

    def __init__(
        self,
        clock: Callable[[], float],
        *,
        interval_seconds: float,
        sink: VramWarningSink | None = None,
    ) -> None:
        if sink is not None and not callable(getattr(sink, "vram_deferred", None)):
            raise TypeError("vram warning sink must have vram_deferred()")
        self._clock = clock
        self._interval = interval_seconds
        self._sink = sink
        self._last: dict[tuple[DeferredWork, ResourceClass | None, bool], float] = {}

    def emit(self, event: VramDeferral) -> bool:
        """Warn about ``event`` unless the same kind was warned about less than
        the interval ago. ``True`` when it was emitted."""
        key = (event.work, event.resource_class, event.gave_up)
        now = self._clock()
        last = self._last.get(key)
        if last is not None and now - last < self._interval:
            return False
        self._last[key] = now
        logger.warning(
            "Not enough free VRAM for %s%s: %d MiB requested, %d MiB free, "
            "%d MiB used by other workloads, %d MiB headroom%s",
            event.work.value,
            "" if event.resource_class is None else f" ({event.resource_class.value})",
            event.requested_bytes // MIB,
            max(0, event.observed_free_bytes) // MIB,
            event.external_bytes // MIB,
            event.headroom_bytes // MIB,
            ": gave up waiting" if event.gave_up else ": waiting",
        )
        if self._sink is not None:
            try:
                self._sink.vram_deferred(event)
            except Exception as error:  # a broken hook must not stop admission
                logger.error("VRAM warning sink failed (%s)", type(error).__name__)
        return True
