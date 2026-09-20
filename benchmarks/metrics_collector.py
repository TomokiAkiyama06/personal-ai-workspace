"""Runtime-independent benchmark metric collection.

The candidate adapter records lifecycle events through :class:`MetricsCollector`.
The collector deliberately has no knowledge of a model provider or credentials; it
only turns those events and optional runtime/GPU observations into the evaluator
result schema's ``metrics`` object.
"""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class GpuSample:
    """A point-in-time reading for one GPU."""

    vram_bytes: int
    utilization_percent: float

    def __post_init__(self) -> None:
        if isinstance(self.vram_bytes, bool) or not isinstance(self.vram_bytes, int):
            raise TypeError("vram_bytes must be a non-negative integer")
        if self.vram_bytes < 0:
            raise ValueError("vram_bytes must be non-negative")
        if (
            isinstance(self.utilization_percent, bool)
            or not isinstance(self.utilization_percent, (int, float))
            or not 0 <= self.utilization_percent <= 100
        ):
            raise ValueError("utilization_percent must be between 0 and 100")


class GpuSampler(Protocol):
    """Dependency boundary for GPU telemetry."""

    def sample(self) -> Sequence[GpuSample]: ...


class NvidiaSmiGpuSampler:
    """Read allocated VRAM and utilization without importing a CUDA library."""

    def sample(self) -> Sequence[GpuSample]:
        try:
            result = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=memory.used,utilization.gpu",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                check=False,
                text=True,
                timeout=2,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return ()
        if result.returncode != 0:
            return ()

        samples: list[GpuSample] = []
        for line in result.stdout.splitlines():
            fields = [field.strip() for field in line.split(",")]
            if len(fields) != 2:
                continue
            try:
                used_mib = int(fields[0])
                utilization = float(fields[1])
                samples.append(GpuSample(used_mib * 1024 * 1024, utilization))
            except ValueError:
                continue
        return tuple(samples)


class MetricsCollector:
    """Collect and normalize measurements for one candidate execution.

    Values which a runtime cannot expose are omitted from :meth:`metrics`; zero is
    retained for observed event counters so it remains distinguishable from unknown.
    """

    def __init__(
        self,
        *,
        gpu_sampler: GpuSampler | None = None,
        monotonic_clock: Callable[[], float] = time.monotonic,
        gpu_poll_interval_s: float | None = 0.25,
    ) -> None:
        if gpu_poll_interval_s is not None and gpu_poll_interval_s <= 0:
            raise ValueError("gpu_poll_interval_s must be positive or None")
        self._gpu_sampler = gpu_sampler
        self._clock = monotonic_clock
        self._gpu_poll_interval_s = gpu_poll_interval_s
        self._lock = threading.Lock()
        self._started_at: float | None = None
        self._stopped_at: float | None = None
        self._steps = 0
        self._retries = 0
        self._tool_calls = 0
        self._tool_failures = 0
        self._token_count: int | None = None
        self._context_tokens: int | None = None
        self._peak_vram_bytes: int | None = None
        self._peak_gpu_utilization_percent: float | None = None
        self._stop_sampling = threading.Event()
        self._sampling_thread: threading.Thread | None = None

    def start(self) -> None:
        """Start wall-clock and, when configured, periodic GPU collection."""
        with self._lock:
            if self._started_at is not None:
                raise RuntimeError("metrics collection has already started")
            self._started_at = self._clock()
        self.sample_gpu()
        if self._gpu_sampler is not None and self._gpu_poll_interval_s is not None:
            self._sampling_thread = threading.Thread(
                target=self._poll_gpu,
                name="benchmark-gpu-metrics",
                daemon=True,
            )
            self._sampling_thread.start()

    def stop(self) -> None:
        """Stop collection and capture a final GPU sample."""
        with self._lock:
            if self._started_at is None:
                raise RuntimeError("metrics collection has not started")
            if self._stopped_at is not None:
                return
            self._stopped_at = self._clock()
        self._stop_sampling.set()
        if self._sampling_thread is not None:
            self._sampling_thread.join(timeout=2)
        self.sample_gpu()

    def record_step(self) -> None:
        with self._lock:
            self._steps += 1

    def record_retry(self) -> None:
        with self._lock:
            self._retries += 1

    def record_tool_call(self, *, failed: bool = False) -> None:
        with self._lock:
            self._tool_calls += 1
            if failed:
                self._tool_failures += 1

    def record_runtime_usage(
        self, *, token_count: int | None = None, context_tokens: int | None = None
    ) -> None:
        """Record runtime-reported totals for the current execution.

        Adapters call this with their latest cumulative values; it does not add
        values because some runtimes report totals after every response.
        """
        self._validate_count("token_count", token_count)
        self._validate_count("context_tokens", context_tokens)
        with self._lock:
            if token_count is not None:
                self._token_count = token_count
            if context_tokens is not None:
                self._context_tokens = context_tokens

    def sample_gpu(self) -> None:
        """Take one optional GPU observation; sampler failures do not fail a run."""
        if self._gpu_sampler is None:
            return
        try:
            samples = self._gpu_sampler.sample()
        except Exception:  # noqa: BLE001 - telemetry must not end a candidate run.
            return
        self.record_gpu_samples(samples)

    def record_gpu_samples(self, samples: Sequence[GpuSample]) -> None:
        """Record one simultaneous multi-GPU observation."""
        if not samples:
            return
        total_vram = sum(sample.vram_bytes for sample in samples)
        peak_utilization = max(sample.utilization_percent for sample in samples)
        with self._lock:
            self._peak_vram_bytes = max(self._peak_vram_bytes or 0, total_vram)
            self._peak_gpu_utilization_percent = max(
                self._peak_gpu_utilization_percent or 0, peak_utilization
            )

    def metrics(self) -> dict[str, int | float]:
        """Return the normalized, schema-compatible evaluator metrics object."""
        with self._lock:
            values: dict[str, int | float] = {
                "agent_steps": self._steps,
                "retries": self._retries,
                "tool_calls": self._tool_calls,
                "tool_failures": self._tool_failures,
            }
            if self._started_at is not None:
                end = (
                    self._stopped_at if self._stopped_at is not None else self._clock()
                )
                values["wall_clock_ms"] = max(0, (end - self._started_at) * 1000)
            if self._token_count is not None:
                values["token_count"] = self._token_count
            if self._context_tokens is not None:
                values["context_tokens"] = self._context_tokens
            if self._peak_vram_bytes is not None:
                values["peak_vram_bytes"] = self._peak_vram_bytes
            if self._peak_gpu_utilization_percent is not None:
                values["peak_gpu_utilization_percent"] = (
                    self._peak_gpu_utilization_percent
                )
            return values

    def _poll_gpu(self) -> None:
        assert self._gpu_poll_interval_s is not None
        while not self._stop_sampling.wait(self._gpu_poll_interval_s):
            self.sample_gpu()

    @staticmethod
    def _validate_count(name: str, value: int | None) -> None:
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise ValueError(f"{name} must be a non-negative integer when provided")
