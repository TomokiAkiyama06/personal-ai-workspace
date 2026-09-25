"""Tests for benchmark metric collection and normalization."""

import unittest
from unittest.mock import patch

from benchmarks.metrics_collector import (
    GpuSample,
    MetricsCollector,
    NvidiaSmiGpuSampler,
)
from benchmarks.validate_result import load_schema, validate_document


class FakeClock:
    def __init__(self, value: float = 100.0):
        self.value = value

    def __call__(self) -> float:
        return self.value


class FakeGpuSampler:
    def __init__(self, samples):
        self.samples = list(samples)
        self.calls = 0

    def sample(self):
        self.calls += 1
        return self.samples.pop(0) if self.samples else ()


class MetricsCollectorTest(unittest.TestCase):
    def test_normalizes_lifecycle_and_runtime_metrics(self):
        clock = FakeClock()
        collector = MetricsCollector(monotonic_clock=clock, gpu_poll_interval_s=None)
        collector.start()
        collector.record_step()
        collector.record_step()
        collector.record_retry()
        collector.record_tool_call()
        collector.record_tool_call(failed=True)
        collector.record_runtime_usage(token_count=124, context_tokens=4096)
        clock.value += 1.25
        collector.stop()

        self.assertEqual(
            collector.metrics(),
            {
                "wall_clock_ms": 1250.0,
                "agent_steps": 2,
                "retries": 1,
                "tool_calls": 2,
                "tool_failures": 1,
                "token_count": 124,
                "context_tokens": 4096,
            },
        )

    def test_gpu_peaks_normalize_multiple_devices_and_samples(self):
        sampler = FakeGpuSampler(
            [
                (GpuSample(10, 20), GpuSample(30, 80)),
                (GpuSample(50, 60), GpuSample(5, 40)),
            ]
        )
        collector = MetricsCollector(gpu_sampler=sampler, gpu_poll_interval_s=None)
        collector.start()
        collector.stop()

        metrics = collector.metrics()
        self.assertEqual(metrics["peak_vram_bytes"], 55)
        self.assertEqual(metrics["peak_gpu_utilization_percent"], 80)
        self.assertEqual(sampler.calls, 2)

    def test_gpu_sampling_latency_is_excluded_from_wall_clock(self):
        clock = FakeClock()

        class SlowGpuSampler(FakeGpuSampler):
            def sample(self):
                clock.value += 5.0  # simulated nvidia-smi startup latency
                return super().sample()

        sampler = SlowGpuSampler([(GpuSample(10, 20),), (GpuSample(30, 40),)])
        collector = MetricsCollector(
            gpu_sampler=sampler, monotonic_clock=clock, gpu_poll_interval_s=None
        )
        collector.start()
        clock.value += 1.5  # the candidate's own execution time
        collector.stop()

        metrics = collector.metrics()
        self.assertEqual(sampler.calls, 2)
        self.assertEqual(metrics["wall_clock_ms"], 1500.0)
        self.assertEqual(metrics["peak_vram_bytes"], 30)
        self.assertEqual(metrics["peak_gpu_utilization_percent"], 40)

    def test_unavailable_optional_metrics_are_omitted(self):
        collector = MetricsCollector(gpu_poll_interval_s=None)
        collector.start()
        collector.stop()

        self.assertNotIn("token_count", collector.metrics())
        self.assertNotIn("context_tokens", collector.metrics())
        self.assertNotIn("peak_vram_bytes", collector.metrics())
        self.assertNotIn("peak_gpu_utilization_percent", collector.metrics())

    def test_runtime_usage_is_replaced_by_latest_cumulative_value(self):
        collector = MetricsCollector(gpu_poll_interval_s=None)
        collector.record_runtime_usage(token_count=20, context_tokens=256)
        collector.record_runtime_usage(token_count=35)

        self.assertEqual(collector.metrics()["token_count"], 35)
        self.assertEqual(collector.metrics()["context_tokens"], 256)

    def test_invalid_counts_and_gpu_samples_are_rejected(self):
        collector = MetricsCollector(gpu_poll_interval_s=None)
        with self.assertRaisesRegex(ValueError, "token_count"):
            collector.record_runtime_usage(token_count=-1)
        with self.assertRaisesRegex(ValueError, "context_tokens"):
            collector.record_runtime_usage(context_tokens=1.5)
        with self.assertRaisesRegex(ValueError, "token_count"):
            collector.record_runtime_usage(token_count=True)
        with self.assertRaisesRegex(ValueError, "vram_bytes"):
            GpuSample(-1, 0)
        with self.assertRaisesRegex(ValueError, "utilization_percent"):
            GpuSample(1, 101)

    def test_collector_output_is_accepted_by_the_result_schema(self):
        collector = MetricsCollector(gpu_poll_interval_s=None)
        collector.start()
        collector.record_step()
        collector.stop()
        document = {
            "schema_version": "1.0",
            "evaluator_version": "test",
            "task_id": "test-task",
            "candidate": {
                "model": "test-model",
                "runtime": "test-runtime",
                "quantization": "none",
            },
            "test_outcomes": {"fail_to_pass": "not_run", "pass_to_pass": "not_run"},
            "check_results": [],
            "metrics": collector.metrics(),
        }
        self.assertEqual(validate_document(document, load_schema()), [])

    def test_lifecycle_misuse_is_rejected(self):
        collector = MetricsCollector(gpu_poll_interval_s=None)
        with self.assertRaisesRegex(RuntimeError, "has not started"):
            collector.stop()
        collector.start()
        with self.assertRaisesRegex(RuntimeError, "already started"):
            collector.start()

    def test_nvidia_sampler_normalizes_mib_and_skips_malformed_rows(self):
        completed_process = type(
            "CompletedProcess", (), {"returncode": 0, "stdout": "10, 25\nbad, row\n"}
        )()
        with patch(
            "benchmarks.metrics_collector.subprocess.run",
            return_value=completed_process,
        ):
            samples = NvidiaSmiGpuSampler().sample()
        self.assertEqual(samples, (GpuSample(10 * 1024 * 1024, 25),))

    def test_nvidia_sampler_treats_a_missing_binary_as_no_telemetry(self):
        with patch(
            "benchmarks.metrics_collector.subprocess.run", side_effect=FileNotFoundError
        ):
            self.assertEqual(NvidiaSmiGpuSampler().sample(), ())


if __name__ == "__main__":
    unittest.main()
