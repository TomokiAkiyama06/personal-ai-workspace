"""The configuration of the Compute Resource Scheduler and the command-based
model control (PAW-036).

``CommandModelControl`` only turns the scheduler's decisions into the argv an
administrator configured; the tests give it a recording runner, so no command is
ever executed.
"""

import asyncio
import unittest

from paw_backend.compute import (
    CommandModelControl,
    ComputeConfig,
    ComputeScheduler,
    DeploymentCommands,
    DeploymentSpec,
    DeploymentState,
    InvalidComputeArgumentError,
    ModelControlError,
    ModelRole,
    Placement,
    ResidencyPolicy,
    ResourceClass,
)
from paw_backend.compute.limits import (
    DEFAULT_CLASS_KV_CEILINGS,
    DEFAULT_HEADROOM_FRACTION,
    DEFAULT_HEADROOM_MIN_BYTES,
)
from paw_backend.compute.probe import CommandResult
from tests.compute_support import GIB, FakeProbe, embedding_spec, main_spec, memory_spec


class SpecTest(unittest.TestCase):
    def test_the_footprint_is_the_sum_of_its_parts(self):
        spec = main_spec()
        self.assertEqual(spec.gpu_bytes, 66 * GIB)
        self.assertEqual(spec.kv_capacity_tokens, 131_072)
        self.assertEqual(embedding_spec().kv_capacity_tokens, 0)

    def test_invalid_specs(self):
        cases = [
            dict(name="Main"),
            dict(name=""),
            dict(name="a" * 33),
            dict(role="main"),
            dict(weights_bytes=0),
            dict(weights_bytes=-1),
            dict(weights_bytes=True),
            dict(kv_pool_bytes=-1),
            dict(kv_pool_bytes=GIB, kv_bytes_per_token=0),
            dict(runtime_bytes=-1),
            dict(workspace_bytes=-1),
            dict(max_sequences=0),
            dict(max_context_tokens=0),
            dict(residency="always"),
            dict(initial="gpu"),
            dict(cpu_fallback=True),  # the main model has no CPU fallback
            dict(initial=DeploymentState.FAILED),
            dict(initial=DeploymentState.CPU),  # a CPU copy needs cpu_fallback
        ]
        for case in cases:
            with (
                self.subTest(case=case),
                self.assertRaises(InvalidComputeArgumentError),
            ):
                main_spec(**case)
        with self.assertRaises(InvalidComputeArgumentError):
            memory_spec(cpu_fallback=True)  # CPU fallback is for Embedding / Reranker


class ConfigTest(unittest.TestCase):
    def test_defaults_are_the_provisional_values(self):
        config = ComputeConfig(deployments=(main_spec(),))
        self.assertEqual(config.headroom_min_bytes, DEFAULT_HEADROOM_MIN_BYTES)
        self.assertEqual(config.headroom_fraction, DEFAULT_HEADROOM_FRACTION)
        self.assertEqual(DEFAULT_HEADROOM_MIN_BYTES, 4 * GIB)
        self.assertEqual(DEFAULT_HEADROOM_FRACTION, 0.05)
        self.assertEqual(
            DEFAULT_CLASS_KV_CEILINGS,
            {
                ResourceClass.INTERACTIVE: 1.0,
                ResourceClass.CODING: 0.95,
                ResourceClass.SUPPORT: 0.85,
                ResourceClass.BACKGROUND: 0.70,
            },
        )
        self.assertEqual(config.spec("main"), main_spec())
        self.assertIsNone(config.spec("nope"))

    def test_invalid_configs(self):
        cases = [
            dict(deployments=()),
            dict(deployments=[main_spec()]),  # a tuple, so it cannot change
            dict(deployments=(main_spec(), main_spec())),  # duplicate name
            dict(deployments=("main",)),
            dict(gpu_index=-1),
            dict(headroom_min_bytes=-1),
            dict(headroom_fraction=0.6),
            dict(headroom_fraction=-0.1),
            dict(kv_safety=0),
            dict(kv_safety=1.1),
            dict(class_ceilings={ResourceClass.INTERACTIVE: 1.0}),
            dict(
                class_ceilings={
                    **DEFAULT_CLASS_KV_CEILINGS,
                    ResourceClass.BACKGROUND: 0.99,  # above Support
                }
            ),
            dict(probe_max_age_seconds=0),
            dict(restore_margin_bytes=-1),
            dict(pressure_context_fraction=0),
            dict(pressure_context_fraction=1.5),
            dict(max_waiters=0),
            dict(verify_timeout_seconds=0),
            dict(verify_poll_seconds=0),
            dict(failed_retry_seconds=0),
        ]
        for case in cases:
            values = {"deployments": (main_spec(),)} | case
            with (
                self.subTest(case=case),
                self.assertRaises(InvalidComputeArgumentError),
            ):
                ComputeConfig(**values)

    def test_the_scheduler_checks_its_collaborators(self):
        config = ComputeConfig(deployments=(main_spec(),))
        with self.assertRaises(TypeError):
            ComputeScheduler(config, object())
        with self.assertRaises(TypeError):
            ComputeScheduler(object(), FakeProbe())
        with self.assertRaises(TypeError):
            ComputeScheduler(config, FakeProbe(), control=object())


class RecordingRunner:
    def __init__(self, stdout="", code=0):
        self.calls = []
        self.result = CommandResult(code, stdout)
        self.error = None

    async def run(self, argv, *, timeout_seconds):
        self.calls.append((tuple(argv), timeout_seconds))
        if self.error is not None:
            raise self.error
        return self.result


def commands(**overrides):
    values = dict(
        gpu=("systemctl", "start", "paw-llm-main.service"),
        unload=("systemctl", "stop", "paw-llm-main.service"),
        pids=(
            "systemctl",
            "show",
            "--property=MainPID",
            "--value",
            "paw-llm-main.service",
        ),
    )
    values.update(overrides)
    return DeploymentCommands(**values)


class CommandModelControlTest(unittest.IsolatedAsyncioTestCase):
    async def test_each_action_runs_the_configured_argv(self):
        runner = RecordingRunner(stdout="4242\n")
        control = CommandModelControl(
            {"main": commands()},
            runner=runner,
            timeout=30,
            host_memory=lambda: 64 * GIB,  # not this machine's /proc/meminfo
        )
        await control.place("main", Placement.LOCAL_GPU)
        await control.unload("main")
        self.assertEqual(await control.processes("main"), frozenset({4242}))
        self.assertIsNone(await control.kv_usage("main"))
        self.assertEqual(
            [call for call, _ in runner.calls],
            [
                ("systemctl", "start", "paw-llm-main.service"),
                ("systemctl", "stop", "paw-llm-main.service"),
                (
                    "systemctl",
                    "show",
                    "--property=MainPID",
                    "--value",
                    "paw-llm-main.service",
                ),
            ],
        )
        self.assertEqual({timeout for _, timeout in runner.calls}, {30})

    async def test_pid_output(self):
        control = CommandModelControl(
            {"main": commands()}, runner=RecordingRunner("0\n")
        )
        self.assertEqual(await control.processes("main"), frozenset())  # not running
        control = CommandModelControl(
            {"main": commands()}, runner=RecordingRunner("12 13\n14\n")
        )
        self.assertEqual(await control.processes("main"), frozenset({12, 13, 14}))
        for stdout in ("x\n", "-1\n"):
            control = CommandModelControl(
                {"main": commands()}, runner=RecordingRunner(stdout)
            )
            with self.subTest(stdout=stdout), self.assertRaises(ModelControlError):
                await control.processes("main")

    async def test_an_action_that_is_not_configured_is_refused(self):
        runner = RecordingRunner()
        control = CommandModelControl({"main": commands()}, runner=runner)
        with self.assertRaises(ModelControlError):
            await control.place("main", Placement.LOCAL_CPU)
        with self.assertRaises(ModelControlError):
            await control.place("other", Placement.LOCAL_GPU)
        with self.assertRaises(ModelControlError):
            await control.place("main", Placement.CLOUD)
        control = CommandModelControl({"main": commands(pids=None)}, runner=runner)
        with self.assertRaises(ModelControlError):
            await control.processes("main")
        self.assertEqual(runner.calls, [])

    async def test_a_failed_command_raises_without_its_output(self):
        runner = RecordingRunner(stdout="secret-ish output", code=1)
        control = CommandModelControl({"main": commands()}, runner=runner)
        with self.assertRaises(ModelControlError) as raised:
            await control.unload("main")
        self.assertNotIn("secret", str(raised.exception))
        runner = RecordingRunner()
        runner.error = TimeoutError()
        control = CommandModelControl({"main": commands()}, runner=runner)
        with self.assertRaises(ModelControlError):
            await control.unload("main")

    def test_invalid_commands(self):
        for bad in ((), ("",), "systemctl stop x", ("a", 1), ("a\x00",)):
            with self.subTest(bad=bad), self.assertRaises(InvalidComputeArgumentError):
                commands(unload=bad)
        with self.assertRaises(InvalidComputeArgumentError):
            CommandModelControl({"Main": commands()})
        with self.assertRaises(InvalidComputeArgumentError):
            CommandModelControl({"main": commands()}, timeout=0)

    def test_nothing_is_executed_by_constructing_it(self):
        runner = RecordingRunner()
        CommandModelControl({"main": commands()}, runner=runner)
        asyncio.run(asyncio.sleep(0))
        self.assertEqual(runner.calls, [])

    def test_it_is_accepted_as_the_schedulers_control(self):
        config = ComputeConfig(
            deployments=(
                DeploymentSpec(
                    name="main",
                    role=ModelRole.MAIN,
                    weights_bytes=GIB,
                    residency=ResidencyPolicy.ALWAYS,
                ),
            )
        )
        ComputeScheduler(
            config,
            FakeProbe(),
            control=CommandModelControl({"main": commands()}, runner=RecordingRunner()),
        )


if __name__ == "__main__":
    unittest.main()
