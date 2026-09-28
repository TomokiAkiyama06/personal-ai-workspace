"""What the scheduler is told (PAW-036): the deployed models and the policy
values. Everything is checked when it is built, so a wrong configuration fails at
start-up, not at the first request.

A :class:`DeploymentSpec` declares one model runtime and its footprint on the GPU
in the parts the requirements name (weights, KV cache pool, CUDA graphs and
runtime buffers, temporary workspace). The numbers come from the Model / Runtime
Benchmark (PAW-017 / PAW-019) and the runtime's own settings; the scheduler does
not measure them, it checks them against what the probe sees.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from paw_backend.compute.domain import (
    SHARED_CLASSES,
    SUPPORT_ROLES,
    DeploymentState,
    ModelRole,
    ResidencyPolicy,
    ResourceClass,
)
from paw_backend.compute.errors import InvalidComputeArgumentError
from paw_backend.compute.limits import (
    DEFAULT_CLASS_KV_CEILINGS,
    DEFAULT_FAILED_RETRY_SECONDS,
    DEFAULT_HEADROOM_FRACTION,
    DEFAULT_HEADROOM_MIN_BYTES,
    DEFAULT_KV_SAFETY,
    DEFAULT_MAX_CONTEXT_TOKENS,
    DEFAULT_MAX_WAITERS,
    DEFAULT_PRESSURE_CONTEXT_FRACTION,
    DEFAULT_PROBE_MAX_AGE_SECONDS,
    DEFAULT_VERIFY_POLL_SECONDS,
    DEFAULT_VERIFY_TIMEOUT_SECONDS,
    MAX_HEADROOM_FRACTION,
)

NAME_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_MAX_BYTES = 1 << 50  # 1 PiB: far above any GPU, below any overflow
_MAX_TOKENS = 1 << 24
_MAX_SEQUENCES = 4_096


def check_name(parameter: str, value: object) -> str:
    if not isinstance(value, str) or NAME_PATTERN.fullmatch(value) is None:
        raise InvalidComputeArgumentError(parameter)
    return value


def check_int(parameter: str, value: object, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidComputeArgumentError(parameter)
    if not minimum <= value <= maximum:
        raise InvalidComputeArgumentError(parameter)
    return value


def check_number(
    parameter: str,
    value: object,
    *,
    minimum: float,
    maximum: float,
    exclusive_minimum: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise InvalidComputeArgumentError(parameter)
    if value != value or value > maximum or value < minimum:  # NaN too
        raise InvalidComputeArgumentError(parameter)
    if exclusive_minimum and value == minimum:
        raise InvalidComputeArgumentError(parameter)
    return float(value)


def check_member(parameter: str, value: object, enum: type):
    if not isinstance(value, enum):
        raise InvalidComputeArgumentError(parameter)
    return value


@dataclass(frozen=True, slots=True)
class DeploymentSpec:
    """One model runtime the scheduler manages.

    ``kv_pool_bytes`` / ``kv_bytes_per_token``: the KV cache the runtime allocates
    when it loads and what one token of context takes in it (both 0 for a model
    without a KV cache, such as an embedding model). ``max_sequences``: requests
    the runtime runs at once. ``max_context_tokens``: the longest context
    (prompt and answer) it takes. ``cpu_fallback``: an Embedding / Reranker model
    that can serve from the CPU. ``initial``: where the model is when the
    scheduler starts (the scheduler does not load or unload anything to find out).
    """

    name: str
    role: ModelRole
    weights_bytes: int
    kv_pool_bytes: int = 0
    runtime_bytes: int = 0  # CUDA graphs, runtime buffers
    workspace_bytes: int = 0  # temporary workspace
    kv_bytes_per_token: int = 0
    max_sequences: int = 1
    max_context_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS
    residency: ResidencyPolicy = ResidencyPolicy.IF_ROOM
    cpu_fallback: bool = False
    initial: DeploymentState = DeploymentState.UNLOADED

    def __post_init__(self) -> None:
        check_name("name", self.name)
        check_member("role", self.role, ModelRole)
        check_int("weights_bytes", self.weights_bytes, minimum=1, maximum=_MAX_BYTES)
        for parameter in ("kv_pool_bytes", "runtime_bytes", "workspace_bytes"):
            check_int(
                parameter, getattr(self, parameter), minimum=0, maximum=_MAX_BYTES
            )
        check_int(
            "kv_bytes_per_token", self.kv_bytes_per_token, minimum=0, maximum=_MAX_BYTES
        )
        if self.kv_pool_bytes and not self.kv_bytes_per_token:
            raise InvalidComputeArgumentError("kv_bytes_per_token")
        check_int(
            "max_sequences", self.max_sequences, minimum=1, maximum=_MAX_SEQUENCES
        )
        check_int(
            "max_context_tokens",
            self.max_context_tokens,
            minimum=1,
            maximum=_MAX_TOKENS,
        )
        check_member("residency", self.residency, ResidencyPolicy)
        if not isinstance(self.cpu_fallback, bool):
            raise InvalidComputeArgumentError("cpu_fallback")
        # CPU fallback is for Embedding / Reranker (the requirements' step 3).
        if self.cpu_fallback and self.role not in SUPPORT_ROLES:
            raise InvalidComputeArgumentError("cpu_fallback")
        check_member("initial", self.initial, DeploymentState)
        if self.initial is DeploymentState.FAILED or (
            self.initial is DeploymentState.CPU and not self.cpu_fallback
        ):
            raise InvalidComputeArgumentError("initial")

    @property
    def gpu_bytes(self) -> int:
        """The whole footprint on the GPU."""
        return (
            self.weights_bytes
            + self.kv_pool_bytes
            + self.runtime_bytes
            + self.workspace_bytes
        )

    @property
    def kv_capacity_tokens(self) -> int:
        if not self.kv_pool_bytes:
            return 0
        return self.kv_pool_bytes // self.kv_bytes_per_token


@dataclass(frozen=True, slots=True)
class ComputeConfig:
    """The deployments and the policy values (``limits.py`` has the defaults).

    ``gpu_index``: the GPU the scheduler manages (V1 manages one GPU; MIG is not
    used). ``restore_margin_bytes``: the room that must stay free after a model
    is brought back or loaded ``IF_ROOM`` (``None``: the headroom again).
    """

    deployments: tuple[DeploymentSpec, ...]
    gpu_index: int = 0
    headroom_min_bytes: int = DEFAULT_HEADROOM_MIN_BYTES
    headroom_fraction: float = DEFAULT_HEADROOM_FRACTION
    kv_safety: float = DEFAULT_KV_SAFETY
    class_ceilings: Mapping[ResourceClass, float] = field(
        default_factory=lambda: dict(DEFAULT_CLASS_KV_CEILINGS)
    )
    probe_max_age_seconds: float = DEFAULT_PROBE_MAX_AGE_SECONDS
    restore_margin_bytes: int | None = None
    pressure_context_fraction: float = DEFAULT_PRESSURE_CONTEXT_FRACTION
    max_waiters: int = DEFAULT_MAX_WAITERS
    verify_timeout_seconds: float = DEFAULT_VERIFY_TIMEOUT_SECONDS
    verify_poll_seconds: float = DEFAULT_VERIFY_POLL_SECONDS
    failed_retry_seconds: float = DEFAULT_FAILED_RETRY_SECONDS

    def __post_init__(self) -> None:
        if not isinstance(self.deployments, tuple) or not self.deployments:
            raise InvalidComputeArgumentError("deployments")
        if not all(isinstance(spec, DeploymentSpec) for spec in self.deployments):
            raise InvalidComputeArgumentError("deployments")
        if len({spec.name for spec in self.deployments}) != len(self.deployments):
            raise InvalidComputeArgumentError("deployments")
        check_int("gpu_index", self.gpu_index, minimum=0, maximum=63)
        check_int(
            "headroom_min_bytes", self.headroom_min_bytes, minimum=0, maximum=_MAX_BYTES
        )
        check_number(
            "headroom_fraction",
            self.headroom_fraction,
            minimum=0,
            maximum=MAX_HEADROOM_FRACTION,
        )
        check_number(
            "kv_safety", self.kv_safety, minimum=0, maximum=1, exclusive_minimum=True
        )
        ceilings = self.class_ceilings
        if not isinstance(ceilings, Mapping) or set(ceilings) != set(SHARED_CLASSES):
            raise InvalidComputeArgumentError("class_ceilings")
        values = {
            cls: check_number(
                "class_ceilings",
                ceilings[cls],
                minimum=0,
                maximum=1,
                exclusive_minimum=True,
            )
            for cls in SHARED_CLASSES
        }
        # A lower class never gets more of the pool than a higher one.
        ordered = [values[cls] for cls in SHARED_CLASSES]
        if any(
            later > earlier
            for earlier, later in zip(ordered, ordered[1:], strict=False)
        ):
            raise InvalidComputeArgumentError("class_ceilings")
        object.__setattr__(self, "class_ceilings", values)
        check_number(
            "probe_max_age_seconds",
            self.probe_max_age_seconds,
            minimum=0,
            maximum=3_600,
            exclusive_minimum=True,
        )
        if self.restore_margin_bytes is not None:
            check_int(
                "restore_margin_bytes",
                self.restore_margin_bytes,
                minimum=0,
                maximum=_MAX_BYTES,
            )
        check_number(
            "pressure_context_fraction",
            self.pressure_context_fraction,
            minimum=0,
            maximum=1,
            exclusive_minimum=True,
        )
        check_int("max_waiters", self.max_waiters, minimum=1, maximum=100_000)
        for parameter in (
            "verify_timeout_seconds",
            "verify_poll_seconds",
            "failed_retry_seconds",
        ):
            check_number(
                parameter,
                getattr(self, parameter),
                minimum=0,
                maximum=86_400,
                exclusive_minimum=True,
            )

    def spec(self, name: str) -> DeploymentSpec | None:
        for spec in self.deployments:
            if spec.name == name:
                return spec
        return None
