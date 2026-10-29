"""The deployment files of the runtime configuration (Decision 0093, 4 and 8).

``deploy/runtime/paw-unit-pids`` (the ``pids`` command: a fake ``systemctl``
and a temporary cgroup tree, never the host's), ``runtime.example.toml`` (this
host's example, parsed by the backend's own parser over the fake file system)
and the polkit rule.
"""

import functools
import importlib.machinery
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from paw_backend.runtime_config import load_runtime_config

from .runtime_config_support import EXAMPLE, PYTHON, standard_host
from .support import make_settings

DEPLOY = Path(__file__).resolve().parents[1] / "deploy" / "runtime"
HELPER = DEPLOY / "paw-unit-pids"
UNIT = "paw-llm-main.service"
GROUP = "/system.slice/paw-llm-main.service"


def load_helper():
    loader = importlib.machinery.SourceFileLoader("paw_unit_pids", str(HELPER))
    spec = importlib.util.spec_from_loader("paw_unit_pids", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


helper = load_helper()


def shown(load_state="loaded", control_group=GROUP, returncode=0, raw=None):
    """A ``run`` that answers like ``systemctl show`` and records its argv."""
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        text = (
            raw
            if raw is not None
            else f"LoadState={load_state}\nControlGroup={control_group}\n"
        )
        return subprocess.CompletedProcess(argv, returncode, text.encode(), b"")

    run.calls = calls
    return run


class HelperTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = directory.name
        self.unit = os.path.join(self.root, GROUP.lstrip("/"))
        os.makedirs(os.path.join(self.unit, "child", "grandchild"))
        self.procs("", "100\n101\n")
        self.procs("child", "102\n")
        self.procs("child/grandchild", "")

    def procs(self, below, text):
        with open(os.path.join(self.unit, below, "cgroup.procs"), "w") as file:
            file.write(text)

    def call(self, argv=(UNIT,), run=None):
        out, err = io.StringIO(), io.StringIO()
        code = helper.main(
            list(argv),
            systemctl="/usr/bin/systemctl",
            cgroup_root=self.root,
            run=run if run is not None else shown(),
            stdout=out,
            stderr=err,
        )
        return code, out.getvalue(), err.getvalue()

    def test_a_running_unit_prints_every_pid_of_its_cgroups(self):
        run = shown()
        code, out, err = self.call(run=run)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.split(), ["100", "101", "102"])
        ((argv, kwargs),) = run.calls
        self.assertEqual(
            argv,
            [
                "/usr/bin/systemctl",
                "show",
                "-p",
                "LoadState",
                "-p",
                "ControlGroup",
                "--",
                UNIT,
            ],
        )
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["timeout"], helper.TIMEOUT_SECONDS)

    def test_a_stopped_unit_prints_nothing_and_succeeds(self):
        code, out, err = self.call(run=shown(control_group=""))
        self.assertEqual((code, out, err), (0, "", ""))

    def test_a_unit_that_is_not_loaded_fails(self):
        for state in ("not-found", "masked", "error", ""):
            with self.subTest(state=state):
                code, out, err = self.call(
                    run=shown(load_state=state, control_group="")
                )
                self.assertEqual((code, out), (1, ""))
                self.assertIn("not loaded", err)

    def test_the_unit_name(self):
        for argv in (
            (),
            ("a.service", "b.service"),
            ("paw-llm-main",),
            ("paw-llm-main.socket",),
            ("--all",),
            ("../x.service",),
            ("a b.service",),
        ):
            with self.subTest(argv=argv):
                run = shown()
                code, out, _ = self.call(argv, run=run)
                self.assertEqual((code, out), (2, ""))
                self.assertEqual(run.calls, [])

    def test_systemctl_failures(self):
        for run in (
            shown(returncode=1),
            shown(raw="LoadState=loaded\n"),
            shown(raw="LoadState=loaded\nControlGroup=x\nExtra=1\n"),
            shown(raw="LoadState=loaded\nLoadState=loaded\nControlGroup=\n"),
            shown(raw="garbage\n"),
        ):
            with self.subTest():
                code, out, _ = self.call(run=run)
                self.assertEqual((code, out), (1, ""))

    def test_systemctl_that_cannot_run(self):
        def run(argv, **kwargs):
            raise FileNotFoundError(argv[0])

        code, out, err = self.call(run=run)
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "paw-unit-pids: systemctl could not be run\n")

    def test_a_control_group_that_is_not_canonical(self):
        for group in ("relative", "/a/../b", "//x", "/a/./b"):
            with self.subTest(group=group):
                code, out, _ = self.call(run=shown(control_group=group))
                self.assertEqual((code, out), (1, ""))

    def test_a_missing_cgroup_fails(self):
        code, out, _ = self.call(run=shown(control_group="/system.slice/gone.service"))
        self.assertEqual((code, out), (1, ""))

    def test_a_cgroup_procs_that_cannot_be_read_fails(self):
        os.remove(os.path.join(self.unit, "child", "cgroup.procs"))
        code, out, _ = self.call()
        self.assertEqual((code, out), (1, ""))

    def test_a_cgroup_procs_with_something_else_fails(self):
        self.procs("child", "102\nnot-a-pid\n")
        code, out, _ = self.call()
        self.assertEqual((code, out), (1, ""))

    def test_a_link_in_the_tree_is_not_followed(self):
        os.symlink("/proc", os.path.join(self.unit, "child", "link"))
        code, out, _ = self.call()
        self.assertEqual(code, 0)
        self.assertEqual(out.split(), ["100", "101", "102"])

    def test_with_a_fake_systemctl_program(self):
        # The real subprocess path, with a fake systemctl that prints its argv's
        # unit back as the cgroup's name.
        fake = os.path.join(self.root, "systemctl")
        with open(fake, "w") as file:
            file.write(
                f"#!{sys.executable}\nimport sys\n"
                "assert sys.argv[1:7] == ['show', '-p', 'LoadState', '-p', "
                "'ControlGroup', '--'], sys.argv\n"
                "print('LoadState=loaded')\n"
                "print('ControlGroup=/system.slice/' + sys.argv[7])\n"
            )
        os.chmod(fake, 0o755)
        out, err = io.StringIO(), io.StringIO()
        code = helper.main(
            [UNIT], systemctl=fake, cgroup_root=self.root, stdout=out, stderr=err
        )
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(out.getvalue().split(), ["100", "101", "102"])

    def test_the_installed_form(self):
        self.assertTrue(os.access(HELPER, os.X_OK))
        first = HELPER.read_text().splitlines()[0]
        self.assertEqual(first, "#!/usr/bin/python3 -I")
        self.assertEqual(helper.SYSTEMCTL, "/usr/bin/systemctl")
        self.assertEqual(helper.CGROUP_ROOT, "/sys/fs/cgroup")
        text = HELPER.read_text()
        for word in ('"start"', '"stop"', "shell=True", "os.system"):
            self.assertNotIn(word, text)


class ExampleTest(unittest.TestCase):
    def test_the_example_is_the_decisions_and_parses(self):
        text = (DEPLOY / "runtime.example.toml").read_text()
        host = standard_host()
        host.file("/etc/paw/runtime.toml", text, mode=0o640)
        load = functools.partial(
            load_runtime_config, host=host, python_executable=PYTHON
        )
        wiring = load("/etc/paw/runtime.toml", make_settings())
        self.assertEqual(
            wiring.summary(),
            "compute: deployments=main control=yes; local_model: yes; "
            "sandbox: ssh; git: ssh; orchestrator: ladders=local",
        )
        # The same values as the tests' copy of the Decision's example.
        host = standard_host()
        host.file("/etc/paw/runtime.toml", EXAMPLE, mode=0o640)
        expected = load_runtime_config(
            "/etc/paw/runtime.toml",
            make_settings(),
            host=host,
            python_executable=PYTHON,
        )
        self.assertEqual(wiring.compute.config, expected.compute.config)
        self.assertEqual(
            wiring.compute.control._commands, expected.compute.control._commands
        )
        self.assertEqual(wiring.local_model, expected.local_model)
        self.assertEqual(
            wiring.orchestrator_config.ladders, expected.orchestrator_config.ladders
        )

    def test_the_commented_step_4_enables_the_cli_runtimes(self):
        # Decision 0099, 7: step 4 replaces step 3's [orchestrator].
        text = (DEPLOY / "runtime.example.toml").read_text()
        head, marker, step4 = text.partition("# --- step 4")
        self.assertTrue(marker)
        head = head[: head.index("[orchestrator]\n")]
        enabled = "\n".join(
            line[2:] if line.startswith("# [") or line.startswith("# ") else line
            for line in step4.splitlines()
            if line.startswith("# ")
            and not line.startswith("# ---")
            and ("=" in line or line.startswith("# ["))
        )
        host = standard_host()
        host.file("/etc/paw/runtime.toml", head + enabled + "\n", mode=0o640)
        wiring = load_runtime_config(
            "/etc/paw/runtime.toml",
            make_settings(),
            host=host,
            python_executable=PYTHON,
        )
        cli = wiring.create_app_arguments()["cli"]
        self.assertTrue(cli.cloud.enabled)
        self.assertEqual(sorted(cli.agents), ["claude", "codex"])
        self.assertIn("assignment: yes", wiring.summary())

    def test_the_polkit_rule(self):
        rule = (DEPLOY / "50-paw-llm-main.rules").read_text()
        self.assertIn('subject.user !== "paw"', rule)
        self.assertIn('unit === "paw-llm-main.service"', rule)
        self.assertIn('verb === "start" || verb === "stop"', rule)
        self.assertIn('"org.freedesktop.systemd1.manage-units"', rule)


if __name__ == "__main__":
    unittest.main()
