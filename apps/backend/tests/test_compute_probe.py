"""The read-only GPU probe (``nvidia-smi --query-*``) and its parsing (PAW-036).

The probe only ever runs the two query commands below; the tests pin them. The
command runner is a fake: no test starts ``nvidia-smi``, except the one opt-in
test at the end (``PAW_TEST_REAL_GPU_PROBE=1``, skipped by default and on CI),
which runs the same two read-only queries on the real GPU.
"""

import asyncio
import os
import pathlib
import re
import unittest

from paw_backend.compute import (
    GpuDevice,
    GpuProcess,
    InvalidComputeArgumentError,
    NvidiaSmiProbe,
    ProbeUnavailableError,
)
from paw_backend.compute.probe import (
    QUERY_APPS_ARGV,
    QUERY_GPU_ARGV,
    CommandResult,
    parse_gpu_rows,
    parse_process_rows,
)
from tests.compute_support import MIB

GPU_LINE = (
    "0, GPU-cc88c531-26fd-ae57-a930-d1b8d1a0389c, "
    "NVIDIA RTX PRO 6000 Blackwell Workstation Edition, 97887, 74302, 0"
)
APPS_LINE = "GPU-cc88c531-26fd-ae57-a930-d1b8d1a0389c, 1039135, 74274"


class RecordingRunner:
    def __init__(self, outputs: dict[tuple[str, ...], CommandResult]) -> None:
        self.outputs = outputs
        self.calls: list[tuple[tuple[str, ...], float]] = []
        self.error: BaseException | None = None

    async def run(self, argv, *, timeout_seconds):
        self.calls.append((tuple(argv), timeout_seconds))
        if self.error is not None:
            raise self.error
        return self.outputs[tuple(argv)]


def runner_with(gpu: str = GPU_LINE + "\n", apps: str = APPS_LINE + "\n", code=0):
    return RecordingRunner(
        {
            QUERY_GPU_ARGV: CommandResult(code, gpu),
            QUERY_APPS_ARGV: CommandResult(0, apps),
        }
    )


class QueryCommandTest(unittest.TestCase):
    def test_the_probe_runs_only_the_two_read_only_queries(self):
        self.assertEqual(
            QUERY_GPU_ARGV,
            (
                "/usr/bin/nvidia-smi",
                "--query-gpu=index,uuid,name,memory.total,memory.used,utilization.gpu",
                "--format=csv,noheader,nounits",
            ),
        )
        self.assertEqual(
            QUERY_APPS_ARGV,
            (
                "/usr/bin/nvidia-smi",
                "--query-compute-apps=gpu_uuid,pid,used_memory",
                "--format=csv,noheader,nounits",
            ),
        )

    def test_nvidia_smi_is_run_by_an_absolute_path(self):
        # Not looked up in PATH, where another user's directory could plant one.
        runner = RecordingRunner(
            {
                ("/opt/nvidia/bin/nvidia-smi", *QUERY_GPU_ARGV[1:]): CommandResult(
                    0, GPU_LINE + "\n"
                ),
                ("/opt/nvidia/bin/nvidia-smi", *QUERY_APPS_ARGV[1:]): CommandResult(
                    0, APPS_LINE + "\n"
                ),
            }
        )
        probe = NvidiaSmiProbe(runner=runner, executable="/opt/nvidia/bin/nvidia-smi")
        asyncio.run(probe.sample())
        self.assertEqual(
            [argv[0] for argv, _ in runner.calls], ["/opt/nvidia/bin/nvidia-smi"] * 2
        )
        for executable in ("nvidia-smi", "bin/nvidia-smi", "", "/usr/bin/\x00x", 7):
            with self.subTest(executable=executable):
                with self.assertRaises(InvalidComputeArgumentError):
                    NvidiaSmiProbe(runner=runner, executable=executable)

    def test_no_compute_module_names_a_command_that_changes_the_gpu(self):
        # Clocks, persistence, MIG, power limits, compute mode, resets, killing a
        # process by pid: none of these may appear in the scheduler's code (the
        # probe's runner may stop the nvidia-smi child it started itself).
        forbidden = re.compile(
            r"--(?:gpu-reset|persistence-mode|lock-gpu-clocks|lock-memory-clocks|"
            r"reset-gpu-clocks|power-limit|compute-mode|multi-instance-gpu|"
            r"applications-clocks)|\"-(?:pm|lgc|lmc|rgc|pl|c|mig|r|ac|rac)\"|"
            r"os\.kill|os\.killpg|signal\.SIGKILL|pkill|killall|nvidia-smi\s+mig"
        )
        package = pathlib.Path(__file__).resolve().parents[1] / "paw_backend/compute"
        for path in sorted(package.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            with self.subTest(module=path.name):
                self.assertIsNone(forbidden.search(source))


class ParseTest(unittest.TestCase):
    def test_gpu_rows_are_read_in_bytes(self):
        (device,) = parse_gpu_rows(GPU_LINE + "\n")
        self.assertEqual(
            device,
            GpuDevice(
                index=0,
                uuid="GPU-cc88c531-26fd-ae57-a930-d1b8d1a0389c",
                name="NVIDIA RTX PRO 6000 Blackwell Workstation Edition",
                total_bytes=97_887 * MIB,
                used_bytes=74_302 * MIB,
                utilization_percent=0,
            ),
        )

    def test_utilization_that_is_not_available_is_none(self):
        (device,) = parse_gpu_rows("1, GPU-x, Some GPU, 1000, 10, [N/A]\n")
        self.assertIsNone(device.utilization_percent)
        (device,) = parse_gpu_rows("1, GPU-x, Some GPU, 1000, 10, [Not Supported]\n")
        self.assertIsNone(device.utilization_percent)

    def test_several_gpus_are_read_in_order(self):
        devices = parse_gpu_rows("0, GPU-a, A, 100, 1, 5\n1, GPU-b, B, 200, 2, 6\n\n")
        self.assertEqual([d.uuid for d in devices], ["GPU-a", "GPU-b"])

    def test_a_row_the_accounting_cannot_use_makes_the_probe_unavailable(self):
        for text in (
            "",
            "garbage\n",
            "0, GPU-a, A, [N/A], 1, 5\n",  # no total
            "0, GPU-a, A, 100, [N/A], 5\n",  # no used memory
            "0, GPU-a, A, -1, 1, 5\n",
            "0, GPU-a, A, 100, 101, 5\n",  # more used than there is
            "x, GPU-a, A, 100, 1, 5\n",
            "0, GPU-a, A, 100, 1, 5, extra\n",
            "0, GPU-a, A, 100, 1, 500\n",  # a percentage above 100
            "0, GPU-a, A, 100, 1, 5\n0, GPU-b, B, 100, 1, 5\n",  # duplicate index
        ):
            with self.subTest(text=text), self.assertRaises(ProbeUnavailableError):
                parse_gpu_rows(text)

    def test_process_rows(self):
        self.assertEqual(
            parse_process_rows(APPS_LINE + "\nGPU-b, 42, [N/A]\n"),
            (
                GpuProcess(
                    "GPU-cc88c531-26fd-ae57-a930-d1b8d1a0389c", 1_039_135, 74_274 * MIB
                ),
                GpuProcess("GPU-b", 42, None),
            ),
        )
        self.assertEqual(parse_process_rows(""), ())
        # nvidia-smi says this when it has nothing to list.
        self.assertEqual(parse_process_rows("No running processes found\n"), ())

    def test_a_broken_process_row_makes_the_probe_unavailable(self):
        for text in (
            "GPU-a, x, 10\n",
            "GPU-a, 1\n",
            "GPU-a, 0, 10\n",
            "GPU-a, 1, -5\n",
        ):
            with self.subTest(text=text), self.assertRaises(ProbeUnavailableError):
                parse_process_rows(text)

    def test_a_process_on_a_gpu_that_was_not_listed_is_refused(self):
        runner = runner_with(apps="GPU-other, 1, 10\n")
        with self.assertRaises(ProbeUnavailableError):
            asyncio.run(NvidiaSmiProbe(runner=runner).sample())


class ProbeTest(unittest.TestCase):
    def test_sample_runs_both_queries_with_the_timeout(self):
        runner = runner_with()
        sample = asyncio.run(NvidiaSmiProbe(runner=runner, timeout=3.0).sample())
        self.assertEqual(runner.calls, [(QUERY_GPU_ARGV, 3.0), (QUERY_APPS_ARGV, 3.0)])
        self.assertEqual(len(sample.devices), 1)
        self.assertEqual(sample.processes[0].pid, 1_039_135)
        self.assertEqual(sample.device(0).total_bytes, 97_887 * MIB)
        self.assertIsNone(sample.device(1))

    def test_a_failed_command_makes_the_probe_unavailable(self):
        with self.assertRaises(ProbeUnavailableError):
            asyncio.run(NvidiaSmiProbe(runner=runner_with(code=9)).sample())

    def test_a_runner_error_makes_the_probe_unavailable(self):
        for error in (FileNotFoundError(), TimeoutError(), OSError(), RuntimeError()):
            runner = runner_with()
            runner.error = error
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(ProbeUnavailableError) as raised:
                    asyncio.run(NvidiaSmiProbe(runner=runner).sample())
                # The message never repeats what the command said.
                self.assertEqual(str(raised.exception), "GPU probe is unavailable")

    def test_invalid_arguments(self):
        for timeout in (0, -1, True, "1", 601):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                NvidiaSmiProbe(timeout=timeout)


@unittest.skipUnless(
    os.environ.get("PAW_TEST_REAL_GPU_PROBE") == "1" and not os.environ.get("CI"),
    "opt-in: set PAW_TEST_REAL_GPU_PROBE=1 on a server with an NVIDIA GPU",
)
class RealProbeTest(unittest.TestCase):
    """Runs the two read-only queries on the real GPU (nothing else)."""

    def test_the_real_gpu_can_be_sampled(self):
        sample = asyncio.run(NvidiaSmiProbe().sample())
        self.assertGreaterEqual(len(sample.devices), 1)
        for device in sample.devices:
            self.assertGreater(device.total_bytes, 0)
            self.assertLessEqual(device.used_bytes, device.total_bytes)


if __name__ == "__main__":
    unittest.main()
