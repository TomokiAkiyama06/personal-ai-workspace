"""The host's side of starting a model runtime (issue #182, Decision 0039-4;
the values are Decision 0072, Proposed).

On 2026-09-30 a FlashInfer JIT build that vLLM started on its first load ran
``ninja`` at its default parallelism (CPU count + 2): 27 ``cicc`` processes took
about 75 GiB of host RAM, the kernel ran out of memory and the machine rebooted.
So ``CommandModelControl``

* refuses to start a GPU runtime while the host's ``MemAvailable`` is below a
  minimum (or cannot be read), runs no command and logs a warning, and
* runs its commands with ``MAX_JOBS`` / ``FLASHINFER_NVCC_THREADS`` capped.

No test starts a runtime: the runner records the argv, and ``/proc/meminfo`` is
replaced by a function (or a file the test writes).
"""

import asyncio
import os
import pathlib
import sys
import tempfile
import unittest

from paw_backend.compute import (
    CommandModelControl,
    DeploymentCommands,
    HostMemoryLowError,
    InvalidComputeArgumentError,
    ModelControlError,
    Placement,
)
from paw_backend.compute.host import (
    MEMINFO_PATH,
    parse_mem_available,
    read_mem_available_bytes,
)
from paw_backend.compute.limits import (
    DEFAULT_CONTROL_TIMEOUT_SECONDS,
    DEFAULT_JIT_BUILD_ENV,
    DEFAULT_MIN_HOST_AVAILABLE_BYTES,
    MAX_CONTROL_TIMEOUT_SECONDS,
)
from paw_backend.compute.probe import CommandResult, SubprocessRunner
from tests.compute_support import GIB

MEMINFO = (
    "MemTotal:       127035912 kB\n"
    "MemFree:         12582912 kB\n"
    "MemAvailable:    33554432 kB\n"
    "Buffers:           123456 kB\n"
)


class RecordingRunner:
    def __init__(self, stdout=""):
        self.calls = []
        self.result = CommandResult(0, stdout)

    async def run(self, argv, *, timeout_seconds):
        self.calls.append(tuple(argv))
        return self.result


def commands():
    return DeploymentCommands(
        gpu=("systemctl", "start", "paw-llm-main.service"),
        unload=("systemctl", "stop", "paw-llm-main.service"),
        cpu=("systemctl", "start", "paw-embed-cpu.service"),
        pids=("cat", "/sys/fs/cgroup/system.slice/paw-llm-main.service/cgroup.procs"),
    )


def available(value):
    def read():
        if isinstance(value, BaseException):
            raise value
        return value

    return read


class MemInfoTest(unittest.TestCase):
    def test_mem_available_is_read_in_bytes(self):
        self.assertEqual(parse_mem_available(MEMINFO), 33_554_432 * 1024)

    def test_an_output_without_a_usable_mem_available_is_refused(self):
        for text in (
            "",
            "MemTotal: 1 kB\n",
            "MemAvailable: x kB\n",
            "MemAvailable: -1 kB\n",
            "MemAvailable: 12 MB\n",
            "MemAvailable: 1 kB\nMemAvailable: 2 kB\n",
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_mem_available(text)

    def test_the_file_is_read(self):
        self.assertEqual(MEMINFO_PATH, "/proc/meminfo")
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory, "meminfo")
            path.write_text(MEMINFO)
            self.assertEqual(read_mem_available_bytes(path), 32 * GIB)
            with self.assertRaises(OSError):
                read_mem_available_bytes(pathlib.Path(directory, "missing"))


class HostMemoryGateTest(unittest.IsolatedAsyncioTestCase):
    def control(self, runner, **options):
        return CommandModelControl({"main": commands()}, runner=runner, **options)

    async def test_a_gpu_runtime_starts_when_enough_memory_is_available(self):
        runner = RecordingRunner()
        control = self.control(runner, host_memory=available(32 * GIB))
        await control.place("main", Placement.LOCAL_GPU)
        self.assertEqual(runner.calls, [("systemctl", "start", "paw-llm-main.service")])

    async def test_a_gpu_runtime_is_not_started_when_memory_is_low(self):
        runner = RecordingRunner()
        control = self.control(runner, host_memory=available(32 * GIB - 1))
        with self.assertLogs("paw_backend.compute", level="WARNING") as logs:
            with self.assertRaises(HostMemoryLowError) as raised:
                await control.place("main", Placement.LOCAL_GPU)
        self.assertEqual(runner.calls, [])  # nothing was started
        # The scheduler treats it as any failed load (FAILED, tried again later).
        self.assertIsInstance(raised.exception, ModelControlError)
        self.assertEqual(raised.exception.code, "host_memory_low")
        self.assertIn("main", logs.output[0])
        self.assertIn("32767 MiB", logs.output[0])  # what was available
        self.assertIn("32768 MiB", logs.output[0])  # the minimum

    async def test_an_unreadable_meminfo_starts_nothing(self):
        for error in (OSError(), ValueError(), RuntimeError()):
            runner = RecordingRunner()
            control = self.control(runner, host_memory=available(error))
            with self.subTest(error=type(error).__name__):
                with self.assertLogs("paw_backend.compute", level="WARNING"):
                    with self.assertRaises(HostMemoryLowError):
                        await control.place("main", Placement.LOCAL_GPU)
                self.assertEqual(runner.calls, [])
        for value in (-1, True, 1.5, None, "1"):
            runner = RecordingRunner()
            control = self.control(runner, host_memory=available(value))
            with self.subTest(value=value):
                with self.assertLogs("paw_backend.compute", level="WARNING"):
                    with self.assertRaises(HostMemoryLowError):
                        await control.place("main", Placement.LOCAL_GPU)
                self.assertEqual(runner.calls, [])

    async def test_unloading_and_the_cpu_copy_are_not_gated(self):
        # Stopping a runtime frees memory; the CPU copy (an Embedding /
        # Reranker model, the relief under VRAM pressure) builds no GPU kernels.
        runner = RecordingRunner()
        control = self.control(runner, host_memory=available(0))
        await control.unload("main")
        await control.place("main", Placement.LOCAL_CPU)
        self.assertEqual(len(runner.calls), 2)

    async def test_the_minimum_can_be_configured_and_zero_turns_the_check_off(self):
        runner = RecordingRunner()
        control = self.control(
            runner, host_memory=available(8 * GIB), min_host_available_bytes=8 * GIB
        )
        await control.place("main", Placement.LOCAL_GPU)
        control = self.control(
            runner, host_memory=available(OSError()), min_host_available_bytes=0
        )
        await control.place("main", Placement.LOCAL_GPU)  # not even read
        self.assertEqual(len(runner.calls), 2)
        for bad in (-1, True, 1.5, "1", None, 1 << 51):
            with self.subTest(bad=bad), self.assertRaises(InvalidComputeArgumentError):
                self.control(RecordingRunner(), min_host_available_bytes=bad)
        with self.assertRaises(InvalidComputeArgumentError):
            self.control(RecordingRunner(), host_memory="/proc/meminfo")

    def test_the_default_minimum_is_32_gib(self):
        self.assertEqual(DEFAULT_MIN_HOST_AVAILABLE_BYTES, 32 * GIB)


class JitBuildEnvTest(unittest.TestCase):
    """The commands see ``MAX_JOBS`` / ``FLASHINFER_NVCC_THREADS``: a command
    that starts the runtime itself (a script, not ``systemctl``) builds its JIT
    kernels with at most that many jobs."""

    def test_the_defaults(self):
        self.assertEqual(
            dict(DEFAULT_JIT_BUILD_ENV),
            {"MAX_JOBS": "4", "FLASHINFER_NVCC_THREADS": "1"},
        )

    def test_the_runner_passes_its_extra_environment(self):
        runner = SubprocessRunner(extra_env=DEFAULT_JIT_BUILD_ENV)
        code = (
            "import os\n"
            "get = os.environ.get\n"
            "print(get('MAX_JOBS'), get('FLASHINFER_NVCC_THREADS'), get('LC_ALL'))\n"
        )
        result = asyncio.run(
            runner.run((sys.executable, "-c", code), timeout_seconds=30)
        )
        self.assertEqual(result.stdout.split(), ["4", "1", "C"])

    def test_the_backends_own_environment_does_not_leak(self):
        os.environ["PAW_TEST_JIT_LEAK"] = "1"
        try:
            runner = SubprocessRunner(extra_env={"MAX_JOBS": "2"})
            code = "import os\nprint(os.environ.get('PAW_TEST_JIT_LEAK'))\n"
            result = asyncio.run(
                runner.run((sys.executable, "-c", code), timeout_seconds=30)
            )
        finally:
            del os.environ["PAW_TEST_JIT_LEAK"]
        self.assertEqual(result.stdout.strip(), "None")

    def test_invalid_extra_environments(self):
        for bad in (
            {"PATH": "/tmp"},  # the runner's own variables stay its own
            {"LC_ALL": "en_US.UTF-8"},
            {"max_jobs": "4"},
            {"MAX_JOBS": 4},
            {"MAX_JOBS": "4\x00"},
            {"": "1"},
            "MAX_JOBS=4",
        ):
            with self.subTest(bad=bad), self.assertRaises(InvalidComputeArgumentError):
                SubprocessRunner(extra_env=bad)

    def test_the_control_runs_its_commands_with_the_caps_by_default(self):
        control = CommandModelControl({"main": commands()})
        runner = control._runner
        self.assertIsInstance(runner, SubprocessRunner)
        self.assertEqual(runner.extra_env, dict(DEFAULT_JIT_BUILD_ENV))


class ControlTimeoutTest(unittest.IsolatedAsyncioTestCase):
    """Decision 0039-2: a first load from the HDD took 412 s, so one load /
    unload command may take 900 s; the ceiling of the setting is raised with it
    (Codex P2 on #179), only for the model commands, not the probe."""

    async def test_the_default_is_900_seconds(self):
        self.assertEqual(DEFAULT_CONTROL_TIMEOUT_SECONDS, 900.0)
        seen = []

        class Runner(RecordingRunner):
            async def run(self, argv, *, timeout_seconds):
                seen.append(timeout_seconds)
                return await super().run(argv, timeout_seconds=timeout_seconds)

        control = CommandModelControl(
            {"main": commands()}, runner=Runner(), host_memory=available(64 * GIB)
        )
        await control.place("main", Placement.LOCAL_GPU)
        self.assertEqual(seen, [900.0])

    def test_the_ceiling_is_twice_the_default(self):
        self.assertEqual(MAX_CONTROL_TIMEOUT_SECONDS, 1_800.0)
        CommandModelControl({"main": commands()}, timeout=1_800)
        for bad in (0, -1, 1_800.5, True, "900"):
            with self.subTest(bad=bad), self.assertRaises(InvalidComputeArgumentError):
                CommandModelControl({"main": commands()}, timeout=bad)


if __name__ == "__main__":
    unittest.main()
