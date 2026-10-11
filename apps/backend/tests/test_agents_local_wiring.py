"""The local agent runtime and the repository tools in the application
(Decision 0083, 11's stage 3): ``create_app(compute=..., local_model=...,
sandbox=..., orchestrator_config=...)``.

With a sandbox the Tool Broker registers the repository tools next to the
Working Set's (one runner, 300 seconds a call, Decision 0083, 8), and with the
local model too the local agent plays its ladder label inside a
``HybridRuntime`` that records the main model's id (Decision 0083, 12).
"""

import unittest

from paw_backend.agents.local import LocalAgentRuntime
from paw_backend.agents.reasoning import MAIN_REASONING_POLICY
from paw_backend.agents.wiring import LocalModelSetup
from paw_backend.app import create_app
from paw_backend.compute import HybridRuntime
from paw_backend.orchestrator import OrchestratorConfig
from paw_backend.sandbox import REPO_TOOL_NAMES, SandboxPool, ToolExecutors
from paw_backend.sandbox.wiring import SandboxSetup
from paw_backend.tools import WORKING_SET_TOOL_SPECS

from .test_compute_app import fake_gpu
from .test_scratch_janitor_lifespan import configured

BASE = "http://127.0.0.1:18080/v1"


class NullRunner:
    async def start(self, account, sandbox_id, spec):
        raise AssertionError("not started in this test")

    async def exec(self, account, sandbox_id, command, *, timeout_s):
        raise AssertionError("not run in this test")

    async def stop(self, account, sandbox_id):
        return None


class WiringTest(unittest.TestCase):
    def test_the_local_agent_and_the_repository_tools(self):
        setup, *_ = fake_gpu()
        settings, database = configured()
        app = create_app(
            settings,
            database=database,
            compute=setup,
            local_model=LocalModelSetup(BASE),
            sandbox=SandboxSetup(NullRunner(), shim_base="/run/paw-sandbox"),
            orchestrator_config=OrchestratorConfig.uniform(("local",)),
        )
        execution = app.state.task_execution
        names = execution.broker._registry.names()
        self.assertTrue(REPO_TOOL_NAMES <= names)
        self.assertTrue({s.name for s in WORKING_SET_TOOL_SPECS} <= names)
        self.assertIsInstance(execution.tools._executor, ToolExecutors)
        self.assertEqual(execution.tools._execution_timeout, 300.0)
        self.assertIsInstance(execution.sandboxes, SandboxPool)
        self.assertTrue(execution.sandboxes.shims)
        hybrid = execution.orchestrator._runtimes["local"]
        self.assertIsInstance(hybrid, HybridRuntime)
        self.assertIsInstance(hybrid._local, LocalAgentRuntime)
        self.assertEqual(hybrid._local_model, "Qwen3.8-27B-FP8")
        self.assertEqual(hybrid._deployment, "main")
        self.assertIs(hybrid._local._policy, MAIN_REASONING_POLICY)

    def test_without_a_sandbox_no_repository_tool_and_no_local_agent(self):
        setup, *_ = fake_gpu()
        settings, database = configured()
        app = create_app(
            settings,
            database=database,
            compute=setup,
            local_model=LocalModelSetup(BASE),
        )
        execution = app.state.task_execution
        self.assertFalse(REPO_TOOL_NAMES & execution.broker._registry.names())
        self.assertIsNone(execution.sandboxes)
        self.assertIsNone(execution.orchestrator)

    def test_a_sandbox_setup_is_checked(self):
        with self.assertRaises(TypeError):
            SandboxSetup(object())
        with self.assertRaises(ValueError):
            SandboxSetup(NullRunner(), shim_base="relative")
        settings, database = configured()
        with self.assertRaises(TypeError):
            create_app(settings, database=database, sandbox=object())


if __name__ == "__main__":
    unittest.main()
