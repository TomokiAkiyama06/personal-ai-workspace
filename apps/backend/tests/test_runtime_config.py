"""The runtime configuration file ``PAW_RUNTIME_CONFIG_FILE`` (Decision 0093).

The parser turns one root-owned TOML file into the arguments of ``create_app``
(``compute``, ``local_model``, ``sandbox``, ``git_runner``,
``orchestrator_config``). Every check of the Decision's section 2 is tested
here against a fake file system (``runtime_config_support``): the file and its
directories, every command's ``argv[0]`` with each hop of its symbolic links and
its script interpreter, the loopback URLs, the frozen values, the sections'
dependencies, and that no error message carries a value.
"""

import json
import os
import tempfile
import unittest

from paw_backend.agents.reasoning import MAIN_REASONING_POLICY
from paw_backend.agents.wiring import LocalModelSetup
from paw_backend.compute.control import CommandModelControl, DeploymentCommands
from paw_backend.compute.domain import (
    DeploymentState,
    ModelRole,
    ResidencyPolicy,
)
from paw_backend.compute.limits import DEFAULT_HEADROOM_FRACTION, DEFAULT_KV_SAFETY
from paw_backend.compute.probe import NvidiaSmiProbe
from paw_backend.compute.wiring import ComputeSetup
from paw_backend.orchestrator.domain import NodeRole
from paw_backend.orchestrator.limits import (
    DEFAULT_MAX_PARALLEL_NODES,
    DEFAULT_NODE_TIMEOUT_SECONDS,
)
from paw_backend.repositories.ssh import SshGitRunner
from paw_backend.runtime_config import (
    MAX_CONFIG_BYTES,
    RealHost,
    RuntimeConfigError,
    RuntimeWiring,
    load_runtime_config,
)
from paw_backend.sandbox.runner import LocalWrapperSandboxRunner, SshSandboxRunner
from paw_backend.sandbox.wiring import SandboxSetup

from .runtime_config_support import (
    EXAMPLE,
    MINIMAL_COMPUTE,
    PAW,
    PYTHON,
    FakeHost,
    standard_host,
)
from .support import make_settings

PATH = "/etc/paw/runtime.toml"
MIB = 1024 * 1024


def load(
    text: str,
    host: FakeHost | None = None,
    *,
    mode: int = 0o640,
    uid: int = 0,
    path: str = PATH,
) -> RuntimeWiring:
    host = host if host is not None else standard_host()
    if path == PATH and PATH not in host.nodes:
        host.file(PATH, text, mode=mode, uid=uid)
    return load_runtime_config(
        path, make_settings(), host=host, python_executable=PYTHON
    )


def with_commands(**commands: str) -> str:
    """``MINIMAL_COMPUTE`` with a ``commands`` table (TOML values as text)."""
    lines = "\n".join(f"{key} = {value}" for key, value in commands.items())
    return MINIMAL_COMPUTE + "\n[compute.deployments.commands]\n" + lines + "\n"


def pids(argv0: str, *rest: str) -> str:
    return with_commands(pids=json.dumps([argv0, *rest]))


class Refused(unittest.TestCase):
    def assertRefused(self, text, where, *, host=None, reason=None, **kw):
        with self.assertRaises(RuntimeConfigError) as caught:
            load(text, host, **kw)
        self.assertEqual(caught.exception.where, where, str(caught.exception))
        if reason is not None:
            self.assertIn(reason, caught.exception.reason)
        return caught.exception


class ExampleTest(unittest.TestCase):
    """The Decision's example for this host (section 4) builds every part."""

    def test_the_example_builds_every_argument(self):
        wiring = load(EXAMPLE)
        compute = wiring.compute
        self.assertIsInstance(compute, ComputeSetup)
        self.assertIsInstance(compute.probe, NvidiaSmiProbe)
        self.assertIsInstance(compute.control, CommandModelControl)
        config = compute.config
        self.assertEqual(config.gpu_index, 0)
        self.assertEqual(config.restore_margin_bytes, 4096 * MIB)
        (main,) = config.deployments
        self.assertEqual(main.name, "main")
        self.assertIs(main.role, ModelRole.MAIN)
        self.assertEqual(main.weights_bytes, 34286 * MIB)
        self.assertEqual(main.kv_pool_bytes, 19866 * MIB)
        self.assertEqual(main.runtime_bytes, 0)
        self.assertEqual(main.kv_bytes_per_token, 68279)
        self.assertEqual(main.max_sequences, 8)
        self.assertEqual(main.max_context_tokens, 131072)
        self.assertIs(main.residency, ResidencyPolicy.ALWAYS)
        self.assertIs(main.initial, DeploymentState.GPU)
        # The frozen values stay the code's defaults (Decision 0093, 4).
        self.assertEqual(config.headroom_fraction, DEFAULT_HEADROOM_FRACTION)
        self.assertEqual(config.kv_safety, DEFAULT_KV_SAFETY)
        commands = compute.control._commands["main"]
        self.assertIsInstance(commands, DeploymentCommands)
        self.assertEqual(
            commands.gpu, ("/usr/bin/systemctl", "start", "paw-llm-main.service")
        )
        self.assertEqual(
            commands.pids,
            ("/usr/local/lib/paw/paw-unit-pids", "paw-llm-main.service"),
        )
        self.assertIsNone(commands.cpu)
        self.assertEqual(commands.unit, "paw-llm-main.service")
        self.assertEqual(commands.metrics_url, "http://127.0.0.1:18080/metrics")

        local = wiring.local_model
        self.assertIsInstance(local, LocalModelSetup)
        self.assertEqual(local.base_url, "http://127.0.0.1:18080/v1")
        self.assertEqual(
            (local.deployment, local.served_model, local.model_id, local.agent_label),
            ("main", "main", "Qwen3.8-27B-FP8", "local"),
        )
        self.assertIs(local.reasoning_history, MAIN_REASONING_POLICY)

        sandbox = wiring.sandbox
        self.assertIsInstance(sandbox, SandboxSetup)
        self.assertIsInstance(sandbox.runner, SshSandboxRunner)
        self.assertEqual(sandbox.shim_base, "/run/paw-sandbox")
        self.assertEqual(sandbox.shim_group, 2001)
        self.assertEqual(sandbox.idle_seconds, 900)
        self.assertIsInstance(wiring.git_runner, SshGitRunner)
        # One key directory for both, with the template of [ssh].
        self.assertIs(sandbox.runner._keys, wiring.git_runner._keys)
        self.assertEqual(
            wiring.git_runner._keys._template, "/etc/paw/ssh-keys/{user}.key"
        )
        self.assertEqual(wiring.git_runner._policy.host, "127.0.0.1")

        orchestrator = wiring.orchestrator_config
        self.assertEqual(orchestrator.ladders, {role: ("local",) for role in NodeRole})
        self.assertEqual(orchestrator.max_parallel_nodes, DEFAULT_MAX_PARALLEL_NODES)
        self.assertEqual(
            orchestrator.node_timeout_seconds, DEFAULT_NODE_TIMEOUT_SECONDS
        )

    def test_the_arguments_and_the_summary(self):
        wiring = load(EXAMPLE)
        self.assertEqual(
            set(wiring.create_app_arguments()),
            {"compute", "local_model", "sandbox", "git_runner", "orchestrator_config"},
        )
        self.assertEqual(
            wiring.summary(),
            "compute: deployments=main control=yes; local_model: yes; "
            "sandbox: ssh; git: ssh; orchestrator: ladders=local",
        )

    def test_the_mode_0644_is_accepted(self):
        load(EXAMPLE, mode=0o644)

    def test_the_mode_0600_is_accepted(self):
        # Root alone reads it then: the backend would fail to open it, not this check.
        load(EXAMPLE, mode=0o600)

    def test_an_empty_file_enables_nothing(self):
        wiring = load("version = 1\n")
        self.assertEqual(wiring.create_app_arguments(), {})
        self.assertEqual(wiring.summary(), "nothing enabled")


class FileTest(Refused):
    """The file and its directories (Decision 0093, 2)."""

    WHERE = "PAW_RUNTIME_CONFIG_FILE"

    def test_a_relative_path(self):
        self.assertRefused(EXAMPLE, self.WHERE, path="etc/paw/runtime.toml")

    def test_a_path_that_is_not_canonical(self):
        for path in (
            "/etc/paw/../paw/runtime.toml",
            "/etc/paw/./runtime.toml",
            "/etc//paw/runtime.toml",
            "//etc/paw/runtime.toml",
        ):
            host = standard_host()
            host.file(PATH, EXAMPLE, mode=0o640)
            with self.subTest(path=path):
                self.assertRefused(EXAMPLE, self.WHERE, host=host, path=path)

    def test_a_symbolic_link(self):
        host = standard_host()
        host.file("/etc/paw/real.toml", EXAMPLE, mode=0o640)
        host.link(PATH, "real.toml")
        self.assertRefused(EXAMPLE, self.WHERE, host=host, reason="symbolic link")

    def test_a_symbolic_link_above_it(self):
        host = standard_host()
        host.file("/srv/paw/runtime.toml", EXAMPLE, mode=0o640)
        host.link("/etc/paw2", "/srv/paw")
        self.assertRefused(
            EXAMPLE, self.WHERE, host=host, path="/etc/paw2/runtime.toml"
        )

    def test_a_missing_file(self):
        host = standard_host()
        self.assertRefused(EXAMPLE, self.WHERE, host=host, path="/etc/paw/other.toml")

    def test_a_directory(self):
        host = standard_host()
        host.dir(PATH)
        self.assertRefused(EXAMPLE, self.WHERE, host=host, reason="regular file")

    def test_an_owner_that_is_not_root(self):
        self.assertRefused(EXAMPLE, self.WHERE, uid=PAW, reason="owned by root")

    def test_a_file_others_or_the_group_can_write(self):
        for mode in (0o666, 0o660, 0o646, 0o620):
            with self.subTest(mode=oct(mode)):
                self.assertRefused(EXAMPLE, self.WHERE, mode=mode, reason="writable")

    def test_a_directory_others_can_write(self):
        for mode in (0o775, 0o757, 0o1777):
            host = standard_host()
            host.dir("/etc/paw", mode=mode)
            with self.subTest(mode=oct(mode)):
                self.assertRefused(EXAMPLE, self.WHERE, host=host)

    def test_a_directory_owned_by_the_backend(self):
        host = standard_host()
        host.dir("/etc/paw", uid=PAW)
        self.assertRefused(EXAMPLE, self.WHERE, host=host, reason="directory")

    def test_a_root_others_can_write(self):
        host = standard_host()
        host.nodes["/"].mode = 0o777
        self.assertRefused(EXAMPLE, self.WHERE, host=host)

    def test_a_file_too_large(self):
        text = "version = 1\n" + "#" * MAX_CONFIG_BYTES + "\n"
        self.assertRefused(text, self.WHERE, reason="too large")

    def test_not_utf8(self):
        host = standard_host()
        host.file(PATH, b"version = 1\n# \xff\xfe\n", mode=0o640)
        self.assertRefused("", self.WHERE, host=host, reason="UTF-8")

    def test_not_toml(self):
        self.assertRefused("version = = 1\n", self.WHERE, reason="TOML")

    def test_a_duplicate_key_is_not_toml(self):
        self.assertRefused("version = 1\nversion = 1\n", self.WHERE, reason="TOML")

    def test_the_version(self):
        for text in (
            "",
            "version = 2\n",
            'version = "1"\n',
            "version = true\n",
            "version = 1.0\n",
        ):
            with self.subTest(text=text):
                self.assertRefused(text, "version")


class SchemaTest(Refused):
    """Unknown sections and keys, types, ranges and the frozen values."""

    def test_an_unknown_section(self):
        self.assertRefused("version = 1\n[scheduler]\n", "scheduler", reason="unknown")

    def test_an_unknown_key(self):
        self.assertRefused(
            MINIMAL_COMPUTE.replace("[compute]", "[compute]\ncolour = 1"),
            "compute.colour",
            reason="unknown",
        )

    def test_the_values_a_decision_fixed_are_not_settable(self):
        cases = (
            (
                MINIMAL_COMPUTE.replace("[compute]", "[compute]\nkv_safety = 0.5"),
                "compute.kv_safety",
            ),
            (
                MINIMAL_COMPUTE.replace(
                    "[compute]", "[compute]\nheadroom_fraction = 0.01"
                ),
                "compute.headroom_fraction",
            ),
            (
                MINIMAL_COMPUTE.replace(
                    "[compute]", "[compute]\nrestore_margin_bytes = 1"
                ),
                "compute.restore_margin_bytes",
            ),
            (
                MINIMAL_COMPUTE + "weights_bytes = 1\n",
                "compute.deployments[0].weights_bytes",
            ),
            (
                with_commands(min_host_available_bytes="0"),
                "compute.deployments[0].commands.min_host_available_bytes",
            ),
            (
                with_commands(timeout="1"),
                "compute.deployments[0].commands.timeout",
            ),
            (
                MINIMAL_COMPUTE + '[local_model]\nbase_url = "http://127.0.0.1:1/v1"\n'
                'reasoning_history = "keep"\n',
                "local_model.reasoning_history",
            ),
            (
                EXAMPLE.replace(
                    "[orchestrator]", "[orchestrator]\nmax_parallel_nodes = 8"
                ),
                "orchestrator.max_parallel_nodes",
            ),
            (
                EXAMPLE.replace(
                    "[orchestrator]", "[orchestrator]\nnode_timeout_seconds = 60"
                ),
                "orchestrator.node_timeout_seconds",
            ),
        )
        for text, where in cases:
            with self.subTest(where=where):
                self.assertRefused(text, where, reason="not settable")

    def test_a_section_that_is_not_a_table(self):
        self.assertRefused("version = 1\ncompute = 1\n", "compute", reason="table")

    def test_compute_needs_a_deployment(self):
        self.assertRefused("version = 1\n[compute]\n", "compute.deployments")

    def test_the_deployment_values(self):
        cases = (
            ("weights_mib = 1000", 'weights_mib = "1000"', "weights_mib"),
            ("weights_mib = 1000", "weights_mib = true", "weights_mib"),
            ("weights_mib = 1000", "weights_mib = 0", "weights_mib"),
            ("weights_mib = 1000", "weights_mib = 1.5", "weights_mib"),
            (
                "weights_mib = 1000",
                "weights_mib = 1000\nkv_pool_mib = -1",
                "kv_pool_mib",
            ),
            ('role = "main"', 'role = "coder"', "role"),
            ("weights_mib = 1000", 'weights_mib = 1000\ninitial = "failed"', "initial"),
            (
                "weights_mib = 1000",
                'weights_mib = 1000\nresidency = "never"',
                "residency",
            ),
            (
                "weights_mib = 1000",
                "weights_mib = 1000\ncpu_fallback = 1",
                "cpu_fallback",
            ),
            ('name = "main"', 'name = "Main Model"', "name"),
            ('name = "main"\n', "", "name"),
            (
                "weights_mib = 1000",
                "weights_mib = 1000\nmax_sequences = 0",
                "max_sequences",
            ),
            # A KV pool needs its bytes per token (DeploymentSpec's own check).
            (
                "weights_mib = 1000",
                "weights_mib = 1000\nkv_pool_mib = 10",
                "kv_bytes_per_token",
            ),
            # A CPU fallback is for an embedding or a reranker (DeploymentSpec).
            (
                "weights_mib = 1000",
                "weights_mib = 1000\ncpu_fallback = true",
                "cpu_fallback",
            ),
        )
        for old, new, key in cases:
            with self.subTest(new=new):
                self.assertRefused(
                    MINIMAL_COMPUTE.replace(old, new), f"compute.deployments[0].{key}"
                )

    def test_the_mib_values_are_turned_into_bytes(self):
        wiring = load(
            MINIMAL_COMPUTE
            + "kv_pool_mib = 2\nruntime_mib = 3\nworkspace_mib = 4\n"
            + "kv_bytes_per_token = 5\n"
        )
        (spec,) = wiring.compute.config.deployments
        self.assertEqual(
            (spec.weights_bytes, spec.kv_pool_bytes, spec.runtime_bytes),
            (1000 * MIB, 2 * MIB, 3 * MIB),
        )
        self.assertEqual((spec.workspace_bytes, spec.kv_bytes_per_token), (4 * MIB, 5))

    def test_the_compute_values(self):
        for line, key in (
            ("gpu_index = -1", "gpu_index"),
            ("gpu_index = 64", "gpu_index"),
            ('gpu_index = "0"', "gpu_index"),
            ("restore_margin_mib = -1", "restore_margin_mib"),
            ("restore_margin_mib = 1.0", "restore_margin_mib"),
            ("nvidia_smi = 1", "nvidia_smi"),
            ('nvidia_smi = "nvidia-smi"', "nvidia_smi"),
        ):
            with self.subTest(line=line):
                self.assertRefused(
                    MINIMAL_COMPUTE.replace("[compute]", "[compute]\n" + line),
                    f"compute.{key}",
                )

    def test_the_defaults(self):
        wiring = load(MINIMAL_COMPUTE)
        config = wiring.compute.config
        self.assertEqual(config.gpu_index, 0)
        self.assertIsNone(config.restore_margin_bytes)
        self.assertIsNone(wiring.compute.control)
        self.assertEqual(wiring.compute.probe._gpu_argv[0], "/usr/bin/nvidia-smi")
        (spec,) = config.deployments
        self.assertIs(spec.residency, ResidencyPolicy.IF_ROOM)
        self.assertIs(spec.initial, DeploymentState.UNLOADED)
        self.assertEqual(wiring.summary(), "compute: deployments=main control=no")

    def test_a_duplicate_deployment_name(self):
        text = MINIMAL_COMPUTE + (
            '\n[[compute.deployments]]\nname = "main"\nrole = "embedding"\n'
            "weights_mib = 10\n"
        )
        self.assertRefused(text, "compute.deployments[1].name", reason="duplicate")

    def test_two_deployments_and_commands_on_one(self):
        text = (
            with_commands(unload='["/usr/bin/systemctl", "stop", "x.service"]')
            + '\n[[compute.deployments]]\nname = "embed"\nrole = "embedding"\n'
            "weights_mib = 10\ncpu_fallback = true\n"
        )
        wiring = load(text)
        self.assertEqual(set(wiring.compute.control._commands), {"main"})
        self.assertEqual(
            wiring.summary(), "compute: deployments=main,embed control=yes"
        )

    def test_the_cli_runtimes_are_checked_and_not_enabled(self):
        # Decision 0099, 7 replaces 0093's reservation: the sections are
        # parsed and checked (test_runtime_config_cloud), but a ladder that
        # names a CLI runtime is still refused until the CLI smoke.
        for name in ("codex", "claude"):
            with self.subTest(name=name):
                self.assertRefused(
                    f"version = 1\n[agents.{name}]\n",
                    f"agents.{name}.model",
                    reason="is required",
                )
        self.assertRefused("version = 1\n[agents.local]\n", "agents.local")
        self.assertRefused("version = 1\nagents = 1\n", "agents")
        self.assertEqual(load("version = 1\n[agents]\n").agents, {})


class CommandTest(Refused):
    """Every command's argv and its ``argv[0]`` (Decision 0093, 2)."""

    WHERE = "compute.deployments[0].commands.pids"

    def test_an_argv_is_a_list_of_strings(self):
        for value in ('"/usr/bin/systemctl start x"', "[]", "[1]", '[""]'):
            with self.subTest(value=value):
                self.assertRefused(with_commands(pids=value), self.WHERE)

    def test_the_actions(self):
        for action in ("gpu", "unload", "cpu", "pids"):
            with self.subTest(action=action):
                self.assertRefused(
                    with_commands(**{action: '["systemctl"]'}),
                    f"compute.deployments[0].commands.{action}",
                )

    def test_a_relative_or_unnormalised_argv0(self):
        for argv0 in (
            "systemctl",
            "./systemctl",
            "/usr/bin/../bin/systemctl",
            "/usr/bin/./systemctl",
            "/usr//bin/systemctl",
            "//usr/bin/systemctl",
            "/usr/bin/systemctl/",
        ):
            with self.subTest(argv0=argv0):
                self.assertRefused(pids(argv0), self.WHERE, reason="argv[0]")

    def test_a_missing_argv0(self):
        self.assertRefused(pids("/usr/bin/nothing"), self.WHERE, reason="exist")

    def test_an_argv0_that_is_not_root_owned(self):
        host = standard_host()
        host.file("/usr/local/bin/tool", uid=PAW)
        self.assertRefused(
            pids("/usr/local/bin/tool"), self.WHERE, host=host, reason="owned by root"
        )

    def test_an_argv0_others_can_write(self):
        host = standard_host()
        host.file("/usr/local/bin/tool", mode=0o775)
        self.assertRefused(
            pids("/usr/local/bin/tool"), self.WHERE, host=host, reason="writable"
        )

    def test_an_argv0_that_cannot_run(self):
        host = standard_host()
        host.file("/usr/local/bin/tool", mode=0o644)
        self.assertRefused(
            pids("/usr/local/bin/tool"), self.WHERE, host=host, reason="executable"
        )

    def test_an_argv0_that_is_a_directory(self):
        self.assertRefused(pids("/usr/bin"), self.WHERE, reason="regular file")

    def test_a_directory_above_argv0_the_backend_can_write(self):
        for mode, uid in ((0o775, 0), (0o757, 0), (0o755, PAW)):
            host = standard_host()
            host.dir("/srv/paw/bin", mode=mode, uid=uid)
            host.file("/srv/paw/bin/tool")
            with self.subTest(mode=oct(mode), uid=uid):
                self.assertRefused(
                    pids("/srv/paw/bin/tool"), self.WHERE, host=host, reason="directory"
                )

    def test_a_sticky_directory(self):
        for mode in (0o1777, 0o1755):
            host = standard_host()
            host.dir("/tmp", mode=mode)
            host.file("/tmp/tool")
            with self.subTest(mode=oct(mode)):
                self.assertRefused(pids("/tmp/tool"), self.WHERE, host=host)

    def test_a_shell_or_a_launcher(self):
        host = standard_host()
        for name in (
            "sh", "bash", "dash", "zsh", "env", "sudo", "su", "perl", "busybox",
            "python", "python3", "python3.13",
        ):  # fmt: skip
            host.file(f"/usr/bin/{name}")
        host.link("/bin", "usr/bin")
        for argv0 in (
            "/usr/bin/sh", "/usr/bin/bash", "/usr/bin/dash", "/usr/bin/zsh",
            "/usr/bin/env", "/usr/bin/sudo", "/usr/bin/su", "/usr/bin/perl",
            "/usr/bin/busybox", "/usr/bin/python", "/usr/bin/python3",
            "/usr/bin/python3.13",
        ):  # fmt: skip
            with self.subTest(argv0=argv0):
                self.assertRefused(
                    pids(argv0, "-c", "true"), self.WHERE, host=host, reason="shell"
                )

    def test_a_link_to_a_shell(self):
        host = standard_host()
        host.file("/usr/bin/bash")
        host.link("/usr/local/bin/tool", "/usr/bin/bash")
        self.assertRefused(
            pids("/usr/local/bin/tool"), self.WHERE, host=host, reason="shell"
        )

    def test_a_link_whose_every_hop_is_root_only(self):
        host = standard_host()
        host.file("/usr/lib/tool/tool-1")
        host.link("/usr/lib/tool/current", "tool-1")
        host.link("/usr/local/bin/tool", "../../lib/tool/current")
        wiring = load(pids("/usr/local/bin/tool"), host)
        self.assertEqual(
            wiring.compute.control._commands["main"].pids, ("/usr/local/bin/tool",)
        )

    def test_a_link_through_a_directory_the_backend_can_write(self):
        # /secure/cmd -> /paw-writable/hop -> /secure/tool: refused at the hop
        # (Codex review #263).
        host = standard_host()
        host.file("/secure/tool")
        host.dir("/paw-writable", uid=PAW)
        host.link("/paw-writable/hop", "/secure/tool")
        host.link("/secure/cmd", "/paw-writable/hop")
        self.assertRefused(pids("/secure/cmd"), self.WHERE, host=host)

    def test_a_link_in_the_path_of_a_directory(self):
        # /usr/local/lib/paw2 -> /paw-writable: the directory the link names is
        # checked too.
        host = standard_host()
        host.dir("/paw-writable", uid=PAW)
        host.file("/paw-writable/tool")
        host.link("/usr/local/lib/paw2", "/paw-writable")
        self.assertRefused(pids("/usr/local/lib/paw2/tool"), self.WHERE, host=host)

    def test_at_most_eight_hops(self):
        def chain(length):
            host = standard_host()
            host.file("/usr/local/bin/tool-0")
            for n in range(1, length + 1):
                host.link(f"/usr/local/bin/tool-{n}", f"tool-{n - 1}")
            return host, f"/usr/local/bin/tool-{length}"

        host, argv0 = chain(8)
        load(pids(argv0), host)
        host, argv0 = chain(9)
        self.assertRefused(pids(argv0), self.WHERE, host=host, reason="links")

    def test_a_link_loop(self):
        host = standard_host()
        host.link("/usr/local/bin/a", "b")
        host.link("/usr/local/bin/b", "a")
        self.assertRefused(pids("/usr/local/bin/a"), self.WHERE, host=host)

    def test_a_path_through_a_file(self):
        self.assertRefused(pids("/usr/bin/systemctl/x"), self.WHERE)

    def test_the_rest_of_the_argv(self):
        self.assertRefused(pids("/usr/bin/systemctl", "a\x00b"), self.WHERE)


class InterpreterTest(Refused):
    """A script's ``#!`` interpreter is checked as ``argv[0]`` is."""

    WHERE = "compute.deployments[0].commands.pids"

    def script(self, line: str, host: FakeHost | None = None) -> FakeHost:
        host = host if host is not None else standard_host()
        host.file("/usr/local/lib/paw/tool", line + "\nbody\n")
        return host

    def test_a_python_script_with_dash_i(self):
        load(pids("/usr/local/lib/paw/tool"), self.script("#!/usr/bin/python3 -I"))

    def test_a_python_script_without_dash_i(self):
        for line in (
            "#!/usr/bin/python3",
            "#!/usr/bin/python3 -u",
            "#!/usr/bin/python3.13 -Iu",
        ):
            with self.subTest(line=line):
                self.assertRefused(
                    pids("/usr/local/lib/paw/tool"),
                    self.WHERE,
                    host=self.script(line),
                    reason="-I",
                )

    def test_a_script_through_env(self):
        self.assertRefused(
            pids("/usr/local/lib/paw/tool"),
            self.WHERE,
            host=self.script("#!/usr/bin/env python3"),
            reason="PATH",
        )

    def test_an_interpreter_that_is_not_root_owned(self):
        host = standard_host()
        host.file("/usr/local/bin/interp", uid=PAW)
        self.assertRefused(
            pids("/usr/local/lib/paw/tool"),
            self.WHERE,
            host=self.script("#!/usr/local/bin/interp", host),
            reason="interpreter",
        )

    def test_an_interpreter_in_a_directory_the_backend_can_write(self):
        host = standard_host()
        host.dir("/srv/interp", uid=PAW)
        host.file("/srv/interp/run")
        self.assertRefused(
            pids("/usr/local/lib/paw/tool"),
            self.WHERE,
            host=self.script("#!/srv/interp/run", host),
            reason="interpreter",
        )

    def test_an_interpreter_link_through_a_writable_directory(self):
        host = standard_host()
        host.dir("/paw-writable", uid=PAW)
        host.link("/paw-writable/py", "/usr/bin/python3.13")
        host.link("/usr/local/bin/py", "/paw-writable/py")
        self.assertRefused(
            pids("/usr/local/lib/paw/tool"),
            self.WHERE,
            host=self.script("#!/usr/local/bin/py -I", host),
            reason="interpreter",
        )

    def test_an_interpreter_that_is_a_script(self):
        host = standard_host()
        host.file("/usr/local/bin/interp", "#!/usr/bin/python3.13 -I\n")
        self.assertRefused(
            pids("/usr/local/lib/paw/tool"),
            self.WHERE,
            host=self.script("#!/usr/local/bin/interp", host),
            reason="script",
        )

    def test_a_relative_or_missing_interpreter(self):
        for line in ("#!python3 -I", "#!", "#!   ", "#!/usr/bin/missing"):
            with self.subTest(line=line):
                self.assertRefused(
                    pids("/usr/local/lib/paw/tool"),
                    self.WHERE,
                    host=self.script(line),
                    reason="interpreter",
                )

    def test_a_shebang_line_without_its_end(self):
        host = standard_host()
        host.file("/usr/local/lib/paw/tool", "#!/usr/bin/python3 -I" + " " * 300)
        self.assertRefused(
            pids("/usr/local/lib/paw/tool"), self.WHERE, host=host, reason="interpreter"
        )

    def test_a_script_of_another_interpreter(self):
        host = standard_host()
        host.file("/usr/bin/awk")
        load(pids("/usr/local/lib/paw/tool"), self.script("#!/usr/bin/awk -f", host))


class UrlTest(Refused):
    def test_metrics_url_is_loopback_only(self):
        for url in (
            "http://localhost:18080/metrics",
            "http://10.0.0.1:18080/metrics",
            "http://127.0.0.2:18080/metrics",
            "https://127.0.0.1:18080/metrics",
            "http://127.0.0.1/metrics",
            "http://127.0.0.1:0/metrics",
            "http://127.0.0.1:70000/metrics",
            "http://user@127.0.0.1:18080/metrics",
            "http://127.0.0.1:18080",
            "http://127.0.0.1:18080/metrics?x=1",
            "http://127.0.0.1:18080/metrics#x",
            "http://127.0.0.1:18080/me trics",
            "http://[::2]:18080/metrics",
        ):
            with self.subTest(url=url):
                self.assertRefused(
                    with_commands(metrics_url=f'"{url}"'),
                    "compute.deployments[0].commands.metrics_url",
                    reason="loopback",
                )

    def test_ipv6_loopback(self):
        wiring = load(with_commands(metrics_url='"http://[::1]:18080/metrics"'))
        self.assertEqual(
            wiring.compute.control._commands["main"].metrics_url,
            "http://[::1]:18080/metrics",
        )

    def test_base_url_is_loopback_only(self):
        for url in ("http://localhost:18080/v1", "http://192.168.1.2:18080/v1"):
            with self.subTest(url=url):
                self.assertRefused(
                    MINIMAL_COMPUTE + f'[local_model]\nbase_url = "{url}"\n',
                    "local_model.base_url",
                    reason="loopback",
                )

    def test_the_unit(self):
        self.assertRefused(
            with_commands(unit='"paw llm"'), "compute.deployments[0].commands.unit"
        )


class PathTest(Refused):
    def test_nvidia_smi_is_checked_as_a_command(self):
        host = standard_host()
        host.file("/usr/local/bin/nvidia-smi", uid=PAW)
        self.assertRefused(
            MINIMAL_COMPUTE.replace(
                "[compute]", '[compute]\nnvidia_smi = "/usr/local/bin/nvidia-smi"'
            ),
            "compute.nvidia_smi",
            host=host,
            reason="owned by root",
        )

    def test_the_default_nvidia_smi_is_checked_too(self):
        host = standard_host()
        del host.nodes["/usr/bin/nvidia-smi"]
        self.assertRefused(MINIMAL_COMPUTE, "compute.nvidia_smi", host=host)

    def test_identity_template(self):
        for value in ("/home/{home}/key", "relative/{user}", "/etc/paw/key"):
            with self.subTest(value=value):
                self.assertRefused(
                    f'version = 1\n[ssh]\nidentity_template = "{value}"\n'
                    '[git]\nrunner = "ssh"\n',
                    "ssh.identity_template",
                )


class DependencyTest(Refused):
    """Which sections need which (Decision 0093, 2 and 3)."""

    def test_git_ssh_alone(self):
        wiring = load('version = 1\n[git]\nrunner = "ssh"\n')
        self.assertEqual(set(wiring.create_app_arguments()), {"git_runner"})
        self.assertIsInstance(wiring.git_runner, SshGitRunner)
        self.assertEqual(
            wiring.git_runner._keys._template, "/etc/paw/ssh-keys/{user}.key"
        )
        self.assertEqual(wiring.summary(), "git: ssh")

    def test_git_subprocess_is_the_default(self):
        wiring = load('version = 1\n[git]\nrunner = "subprocess"\n')
        self.assertEqual(wiring.create_app_arguments(), {})
        self.assertEqual(wiring.summary(), "git: subprocess")
        self.assertRefused('version = 1\n[git]\nrunner = "rsh"\n', "git.runner")

    def test_compute_and_local_model(self):
        wiring = load(
            MINIMAL_COMPUTE + '[local_model]\nbase_url = "http://127.0.0.1:18080/v1"\n'
        )
        self.assertEqual(set(wiring.create_app_arguments()), {"compute", "local_model"})
        self.assertEqual(
            (wiring.local_model.deployment, wiring.local_model.agent_label),
            ("main", "local"),
        )

    def test_local_model_needs_compute(self):
        self.assertRefused(
            'version = 1\n[local_model]\nbase_url = "http://127.0.0.1:18080/v1"\n',
            "local_model",
            reason="[compute]",
        )

    def test_local_model_needs_its_deployment(self):
        self.assertRefused(
            MINIMAL_COMPUTE + '[local_model]\nbase_url = "http://127.0.0.1:1/v1"\n'
            'deployment = "other"\n',
            "local_model.deployment",
        )

    def test_local_model_needs_a_base_url(self):
        self.assertRefused(MINIMAL_COMPUTE + "[local_model]\n", "local_model.base_url")

    def test_the_local_model_names(self):
        base = MINIMAL_COMPUTE + '[local_model]\nbase_url = "http://127.0.0.1:1/v1"\n'
        for line, key in (
            ('agent_label = "Local"', "agent_label"),
            ('model_id = "has space"', "model_id"),
            ('served_model = ""', "served_model"),
        ):
            with self.subTest(line=line):
                self.assertRefused(base + line + "\n", f"local_model.{key}")

    def test_sandbox_ssh_needs_git_ssh(self):
        self.assertRefused(
            EXAMPLE.replace('[git]\nrunner = "ssh"', '[git]\nrunner = "subprocess"'),
            "git.runner",
            reason="sandbox",
        )
        self.assertRefused(
            EXAMPLE.replace('[git]\nrunner = "ssh"', ""), "git.runner", reason="sandbox"
        )

    def test_sandbox_alone(self):
        wiring = load('version = 1\n[git]\nrunner = "ssh"\n[sandbox]\nrunner = "ssh"\n')
        self.assertEqual(set(wiring.create_app_arguments()), {"sandbox", "git_runner"})
        self.assertIsNone(wiring.sandbox.shim_base)
        self.assertIsNone(wiring.sandbox.shim_group)
        self.assertEqual(wiring.sandbox.idle_seconds, 900)

    def test_an_unknown_shim_group(self):
        self.assertRefused(
            EXAMPLE.replace('shim_group = "paw-sandbox"', 'shim_group = "nobody-here"'),
            "sandbox.shim_group",
            reason="group",
        )

    def test_the_sandbox_values(self):
        for old, new, key in (
            ('runner = "ssh"                       #', 'runner = "podman" #', "runner"),
            ("idle_seconds = 900", "idle_seconds = 0", "idle_seconds"),
            ("idle_seconds = 900", 'idle_seconds = "900"', "idle_seconds"),
            ('shim_base = "/run/paw-sandbox"', 'shim_base = "run/x"', "shim_base"),
            ('shim_base = "/run/paw-sandbox"', 'shim_base = "/run/../x"', "shim_base"),
            ('shim_group = "paw-sandbox"', "shim_group = 2001", "shim_group"),
            (
                "idle_seconds = 900",
                'wrapper = "/usr/local/lib/paw/paw-git-wrapper"',
                "wrapper",
            ),
            ("idle_seconds = 900", 'wrapper_options = ["--x"]', "wrapper_options"),
        ):
            text = EXAMPLE.replace(
                '[sandbox]\nrunner = "ssh"',
                '[sandbox]\nrunner = "ssh"                       #',
            ).replace(old, new)
            with self.subTest(new=new):
                self.assertRefused(text, f"sandbox.{key}")

    def test_the_local_wrapper(self):
        text = (
            'version = 1\n[sandbox]\nrunner = "local_wrapper"\n'
            'wrapper = "/usr/local/lib/paw/paw-git-wrapper"\n'
            'wrapper_options = ["--sandbox-shim-base=/run/paw-sandbox"]\n'
        )
        wiring = load(text)
        runner = wiring.sandbox.runner
        self.assertIsInstance(runner, LocalWrapperSandboxRunner)
        self.assertEqual(
            runner._argv,
            [
                PYTHON,
                "-I",
                "/usr/local/lib/paw/paw-git-wrapper",
                "--sandbox-shim-base=/run/paw-sandbox",
            ],
        )
        self.assertEqual(wiring.summary(), "sandbox: local_wrapper")

    def test_the_local_wrapper_needs_its_wrapper(self):
        self.assertRefused(
            'version = 1\n[sandbox]\nrunner = "local_wrapper"\n', "sandbox.wrapper"
        )

    def test_the_wrapper_is_checked(self):
        host = standard_host()
        host.file("/srv/wrapper", uid=PAW)
        self.assertRefused(
            'version = 1\n[sandbox]\nrunner = "local_wrapper"\n'
            'wrapper = "/srv/wrapper"\n',
            "sandbox.wrapper",
            host=host,
            reason="owned by root",
        )

    def test_the_backends_python_is_checked(self):
        # sys.executable runs the wrapper (LocalWrapperSandboxRunner):
        # /opt/paw/current -> releases/1, its directories must be root-only.
        host = standard_host()
        host.dir("/opt/paw/releases/1/venv/bin", uid=PAW)
        self.assertRefused(
            'version = 1\n[sandbox]\nrunner = "local_wrapper"\n'
            'wrapper = "/usr/local/lib/paw/paw-git-wrapper"\n',
            "sandbox.runner",
            host=host,
            reason="python",
        )

    def test_the_options_are_strings(self):
        self.assertRefused(
            'version = 1\n[sandbox]\nrunner = "local_wrapper"\n'
            'wrapper = "/usr/local/lib/paw/paw-git-wrapper"\nwrapper_options = [1]\n',
            "sandbox.wrapper_options",
        )

    def test_orchestrator_needs_a_runtime(self):
        ladders = (
            '[orchestrator.ladders]\nplanner = ["local"]\nworker = ["local"]\n'
            'researcher = ["local"]\nreviewer = ["local"]\n'
        )
        # Without the sandbox create_app builds no local agent runtime.
        text = (
            MINIMAL_COMPUTE
            + '[local_model]\nbase_url = "http://127.0.0.1:1/v1"\n'
            + ladders
        )
        self.assertRefused(text, "orchestrator", reason="runtime")
        self.assertRefused("version = 1\n" + ladders, "orchestrator", reason="runtime")

    def test_an_unknown_label(self):
        self.assertRefused(
            EXAMPLE.replace('reviewer = ["local"]', 'reviewer = ["local", "codex"]'),
            "orchestrator.ladders.reviewer",
            reason="label",
        )

    def test_a_label_of_another_agent_label(self):
        text = EXAMPLE.replace('agent_label = "local"', 'agent_label = "qwen"')
        self.assertRefused(text, "orchestrator.ladders.planner", reason="label")
        wiring = load(text.replace('["local"]', '["qwen"]'))
        self.assertEqual(wiring.local_model.agent_label, "qwen")
        self.assertTrue(wiring.summary().endswith("orchestrator: ladders=qwen"))

    def test_every_role_has_a_ladder(self):
        self.assertRefused(
            EXAMPLE.replace('researcher = ["local"]\n', ""),
            "orchestrator.ladders.researcher",
        )
        self.assertRefused(
            EXAMPLE.replace('worker = ["local"]', 'worker = "local"'),
            "orchestrator.ladders.worker",
        )
        self.assertRefused(
            EXAMPLE.replace('worker = ["local"]', "worker = []"),
            "orchestrator.ladders.worker",
        )
        self.assertRefused(
            EXAMPLE.replace(
                'worker = ["local"]', 'worker = ["local"]\ntester = ["local"]'
            ),
            "orchestrator.ladders.tester",
        )
        self.assertRefused(
            EXAMPLE.replace(
                '[orchestrator.ladders]\nplanner = ["local"]\nworker = ["local"]\n'
                'researcher = ["local"]\nreviewer = ["local"]\n',
                "",
            ),
            "orchestrator.ladders",
        )


class NoValueInErrorsTest(unittest.TestCase):
    """An error names the section, the key and a fixed reason: never a value."""

    MARK = "zq-secret-marker"

    def test_no_value_in_any_error(self):
        mark = self.MARK
        cases = (
            pids(f"/{mark}/tool", "x"),
            pids(f"{mark}/tool"),
            pids("/usr/bin/systemctl", f"{mark}\x00"),
            with_commands(metrics_url=f'"http://{mark}:1/metrics"'),
            with_commands(unit=f'"{mark}"'),
            MINIMAL_COMPUTE + f'[local_model]\nbase_url = "http://{mark}:1/v1"\n',
            MINIMAL_COMPUTE + f'[local_model]\nbase_url = "http://127.0.0.1:1/v1"\n'
            f'deployment = "{mark}"\n',
            MINIMAL_COMPUTE.replace('name = "main"', f'name = "{mark}!"'),
            MINIMAL_COMPUTE.replace('role = "main"', f'role = "{mark}"'),
            MINIMAL_COMPUTE.replace("[compute]", f'[compute]\nnvidia_smi = "/{mark}"'),
            f'version = 1\n[ssh]\nidentity_template = "/{mark}"\n'
            '[git]\nrunner = "ssh"\n',
            f'version = 1\n[git]\nrunner = "{mark}"\n',
            f'version = 1\n[sandbox]\nrunner = "ssh"\nshim_group = "{mark}"\n'
            '[git]\nrunner = "ssh"\n',
            EXAMPLE.replace('reviewer = ["local"]', f'reviewer = ["{mark}"]'),
            f'version = "{mark}"\n',
            f'version = 1\n[compute]\nx = "{mark}" = 1\n',
        )
        for text in cases:
            with self.subTest(text=text[-60:]):
                host = standard_host()
                host.file(f"/{mark}/tool", uid=PAW)
                with self.assertRaises(RuntimeConfigError) as caught:
                    load(text, host)
                self.assertNotIn(mark, str(caught.exception))
                # Not even in a chained exception's text.
                self.assertIsNone(caught.exception.__cause__)

    def test_the_unknown_key_names_the_key_only(self):
        with self.assertRaises(RuntimeConfigError) as caught:
            load('version = 1\nsome_key = "value-zq"\n')
        self.assertEqual(str(caught.exception), "some_key: unknown key")


class RealHostTest(unittest.TestCase):
    """The real host's primitives, on files of a temporary directory."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = self.directory.name
        self.addCleanup(self.directory.cleanup)
        self.host = RealHost()

    def path(self, name):
        return os.path.join(self.root, name)

    def test_read_file(self):
        with open(self.path("a.toml"), "wb") as file:
            file.write(b"version = 1\n")
        info, data = self.host.read_file(self.path("a.toml"), 100)
        self.assertEqual(data, b"version = 1\n")
        self.assertEqual(info.st_uid, os.geteuid())

    def test_read_file_reads_at_most_one_more_byte(self):
        with open(self.path("a.toml"), "wb") as file:
            file.write(b"x" * 50)
        _, data = self.host.read_file(self.path("a.toml"), 10)
        self.assertEqual(len(data), 11)

    def test_read_file_does_not_follow_a_link(self):
        with open(self.path("a.toml"), "wb") as file:
            file.write(b"version = 1\n")
        os.symlink("a.toml", self.path("b.toml"))
        with self.assertRaises(OSError):
            self.host.read_file(self.path("b.toml"), 100)

    def test_read_file_of_a_fifo_does_not_block(self):
        os.mkfifo(self.path("fifo"))
        info, data = self.host.read_file(self.path("fifo"), 100)
        self.assertFalse(os.path.isfile(self.path("fifo")))
        self.assertEqual(data, b"")
        import stat

        self.assertTrue(stat.S_ISFIFO(info.st_mode))

    def test_lstat_readlink_execute_and_groups(self):
        os.symlink("target", self.path("link"))
        import stat

        self.assertTrue(stat.S_ISLNK(self.host.lstat(self.path("link")).st_mode))
        self.assertEqual(self.host.readlink(self.path("link")), "target")
        with open(self.path("tool"), "wb"):
            pass
        os.chmod(self.path("tool"), 0o700)
        self.assertTrue(self.host.can_execute(self.path("tool")))
        os.chmod(self.path("tool"), 0o600)
        self.assertFalse(self.host.can_execute(self.path("tool")))
        self.assertIsNone(self.host.group_id("zq-no-such-group-name"))
        import grp

        group = grp.getgrgid(os.getegid())
        self.assertEqual(self.host.group_id(group.gr_name), group.gr_gid)


if __name__ == "__main__":
    unittest.main()
