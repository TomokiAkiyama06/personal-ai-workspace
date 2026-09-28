"""Typed errors of the Compute Resource Scheduler (PAW-036).

Messages are fixed strings: they never contain what a command printed, a model
name from a configuration, a pid or a caller's value. ``code`` is the stable
machine-readable identifier.
"""

from typing import ClassVar

from paw_backend.compute.domain import ExclusiveFailure, Refusal


class ComputeError(Exception):
    """Base class of every error raised by ``paw_backend.compute``."""

    code: ClassVar[str] = "compute_error"


class InvalidComputeArgumentError(ComputeError, ValueError):
    """A caller passed an argument of the wrong type or outside its bounds.
    ``parameter`` is a constant of this package, never the value."""

    code = "invalid_compute_argument"

    def __init__(self, parameter: str) -> None:
        self.parameter = parameter
        super().__init__(f"Invalid value for {parameter}")


class ProbeUnavailableError(ComputeError):
    """The GPU could not be read (no ``nvidia-smi``, a timeout, an output the
    accounting cannot use). The scheduler then admits no new local GPU work."""

    code = "gpu_probe_unavailable"

    def __init__(self) -> None:
        super().__init__("GPU probe is unavailable")


class ModelControlError(ComputeError):
    """A model could not be loaded, unloaded or inspected (the action is not
    configured, the command failed or timed out)."""

    code = "model_control_failed"

    def __init__(self) -> None:
        super().__init__("Model control action failed")


class ComputeUnavailableError(ComputeError):
    """No capacity was granted in time. ``reason`` is the last refusal."""

    code = "compute_unavailable"

    def __init__(self, reason: Refusal) -> None:
        self.reason = reason
        super().__init__(f"Compute is unavailable ({reason.value})")


class ExclusiveUnavailableError(ComputeError):
    """An Exclusive lease was not granted; the scheduler is back to normal."""

    code = "exclusive_unavailable"

    def __init__(self, failure: ExclusiveFailure) -> None:
        self.failure = failure
        super().__init__(f"The GPU could not be given exclusively ({failure.value})")
