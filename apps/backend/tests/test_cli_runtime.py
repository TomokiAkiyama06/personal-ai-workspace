"""``python -m paw_backend.cli runtime-config-check <path>`` (Decision 0093, 7)."""

import functools
import io
import unittest

from paw_backend.cli import dispatch, runtime
from paw_backend.runtime_config import load_runtime_config

from .runtime_config_support import EXAMPLE, PYTHON, standard_host
from .support import paw_environment
from .test_scratch_janitor_lifespan import UNREACHABLE

PATH = "/etc/paw/runtime.toml"


def fake_load(text: str):
    host = standard_host()
    host.file(PATH, text, mode=0o640)
    return functools.partial(load_runtime_config, host=host, python_executable=PYTHON)


class RuntimeConfigCheckTest(unittest.TestCase):
    def check(self, argv, load=None, **environment):
        out, err = io.StringIO(), io.StringIO()
        with paw_environment(**environment):
            code = runtime.main(argv, stdout=out, stderr=err, load=load)
        return code, out.getvalue(), err.getvalue()

    def test_the_dispatcher_picks_it(self):
        self.assertIs(dispatch.command_module(["runtime-config-check", PATH]), runtime)

    def test_the_orchestrator_needs_the_database(self):
        # As the backend's start does (Codex review #265).
        code, out, err = self.check(["runtime-config-check", PATH], fake_load(EXAMPLE))
        self.assertEqual((code, out), (2, ""))
        self.assertIn(
            "Invalid runtime configuration: orchestrator: needs PAW_DATABASE_URL", err
        )

    def test_a_file_that_can_be_used(self):
        code, out, err = self.check(
            ["runtime-config-check", PATH],
            fake_load(EXAMPLE),
            PAW_DATABASE_URL=UNREACHABLE,
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(
            out.splitlines(),
            [
                "The runtime configuration can be used. It enables:",
                "  compute: deployments=main control=yes",
                "  local_model: yes",
                "  sandbox: ssh",
                "  git: ssh",
                "  orchestrator: ladders=local",
            ],
        )
        # No path, command or URL.
        for value in ("/usr", "/etc", "127.0.0.1", "systemctl"):
            self.assertNotIn(value, out)

    def test_an_empty_file(self):
        code, out, _ = self.check(
            ["runtime-config-check", PATH], fake_load("version = 1\n")
        )
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[1:], ["  nothing"])

    def test_a_refused_file(self):
        text = EXAMPLE.replace(
            'base_url = "http://127.0.0.1:18080/v1"',
            'base_url = "http://zq.example:1/v1"',
        )
        code, out, err = self.check(["runtime-config-check", PATH], fake_load(text))
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("Invalid runtime configuration: local_model.base_url: ", err)
        self.assertNotIn("zq.example", err)

    def test_a_missing_file_on_the_real_host(self):
        code, _, err = self.check(["runtime-config-check", "/zq-nowhere/runtime.toml"])
        self.assertEqual(code, 2)
        self.assertIn("PAW_RUNTIME_CONFIG_FILE: ", err)
        self.assertNotIn("zq-nowhere", err)

    def test_invalid_settings(self):
        code, _, err = self.check(
            ["runtime-config-check", PATH],
            fake_load(EXAMPLE),
            PAW_REPOSITORY_SSH_PORT="0",
        )
        self.assertEqual(code, 2)
        self.assertIn("Invalid configuration", err)

    def test_usage(self):
        code, _, _ = self.check(["runtime-config-check"])
        self.assertEqual(code, 1)
        code, _, _ = self.check(["runtime-config-check", PATH, "extra"])
        self.assertEqual(code, 1)
        code, out, _ = self.check(["runtime-config-check", "--help"])
        self.assertEqual(code, 0)
        self.assertIn("runtime-config-check", out)


if __name__ == "__main__":
    unittest.main()
