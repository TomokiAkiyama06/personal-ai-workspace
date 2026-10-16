"""``server.main`` with the runtime configuration (Decision 0093, Approved, 1-3).

``PAW_RUNTIME_CONFIG_FILE`` unset: ``create_app(settings)`` as before and one
warning. Set: the file is read once, its wiring goes to ``create_app``, and
anything wrong (in the file, or refused by ``create_app``) exits with code 2
and a message without a value. The file system is the fake one of
``runtime_config_support``.
"""

import contextlib
import functools
import io
import unittest
from unittest.mock import MagicMock, patch

from fastapi import FastAPI

from paw_backend.compute.wiring import ComputeServices
from paw_backend.runtime_config import (
    RuntimeConfigError,
    RuntimeWiring,
    load_runtime_config,
)
from paw_backend.server import (
    RUNTIME_CONFIG_NOT_SET,
    build_app,
    main,
    runtime_wiring,
)

from .runtime_config_support import EXAMPLE, MINIMAL_COMPUTE, PYTHON, standard_host
from .support import make_settings, paw_environment
from .test_scratch_janitor_lifespan import UNREACHABLE

PATH = "/etc/paw/runtime.toml"


def fake_load(text: str):
    """``load_runtime_config`` over the fake host with ``text`` at ``PATH``."""
    host = standard_host()
    host.file(PATH, text, mode=0o640)
    return functools.partial(load_runtime_config, host=host, python_executable=PYTHON)


class RuntimeWiringTest(unittest.TestCase):
    def test_unset_builds_nothing_and_warns_once(self):
        with self.assertLogs("paw_backend.server", "WARNING") as logs:
            wiring = runtime_wiring(make_settings())
        self.assertEqual(wiring, RuntimeWiring())
        self.assertEqual(wiring.create_app_arguments(), {})
        self.assertEqual(
            [record.getMessage() for record in logs.records], [RUNTIME_CONFIG_NOT_SET]
        )

    def test_an_empty_setting_is_unset(self):
        with paw_environment(PAW_RUNTIME_CONFIG_FILE=""):
            from paw_backend.config import Settings

            self.assertIsNone(Settings().runtime_config_file)

    def test_a_file_is_read_and_summarised(self):
        settings = make_settings(runtime_config_file=PATH, database_url=UNREACHABLE)
        with self.assertLogs("paw_backend.server", "INFO") as logs:
            wiring = runtime_wiring(settings, load=fake_load(EXAMPLE))
        self.assertEqual(
            set(wiring.create_app_arguments()),
            {"compute", "local_model", "sandbox", "git_runner", "orchestrator_config"},
        )
        (message,) = [record.getMessage() for record in logs.records]
        self.assertEqual(
            message,
            "Runtime configuration: compute: deployments=main control=yes; "
            "local_model: yes; sandbox: ssh; git: ssh; orchestrator: ladders=local",
        )
        self.assertNotIn("/usr/bin", message)

    def test_the_file_is_read_once(self):
        load = MagicMock(return_value=RuntimeWiring())
        runtime_wiring(make_settings(runtime_config_file=PATH), load=load)
        load.assert_called_once()
        self.assertEqual(load.call_args.args[0], PATH)

    def test_the_orchestrator_needs_the_database(self):
        settings = make_settings(runtime_config_file=PATH)
        with self.assertRaises(RuntimeConfigError) as caught:
            runtime_wiring(settings, load=fake_load(EXAMPLE))
        self.assertEqual(caught.exception.where, "orchestrator")


class BuildAppTest(unittest.TestCase):
    def test_the_example_is_accepted_by_create_app(self):
        settings = make_settings(runtime_config_file=PATH, database_url=UNREACHABLE)
        wiring = runtime_wiring(settings, load=fake_load(EXAMPLE))
        app = build_app(settings, wiring)
        self.assertIsInstance(app.state.compute, ComputeServices)
        self.assertIsNotNone(app.state.preference_interpreter)
        execution = app.state.task_execution
        self.assertIsNotNone(execution.orchestrator)
        self.assertIsNotNone(execution.sandboxes)

    def test_compute_alone(self):
        settings = make_settings(runtime_config_file=PATH)
        app = build_app(
            settings, runtime_wiring(settings, load=fake_load(MINIMAL_COMPUTE))
        )
        self.assertIsInstance(app.state.compute, ComputeServices)

    def test_a_refusal_of_create_app_is_a_runtime_configuration_error(self):
        wiring = RuntimeWiring(git_runner=object())
        for error in (TypeError("zq /secret/path"), ValueError("zq /secret/path")):
            with self.subTest(error=type(error).__name__):
                with patch("paw_backend.server.create_app", side_effect=error):
                    with self.assertRaises(RuntimeConfigError) as caught:
                        build_app(make_settings(), wiring)
                self.assertEqual(caught.exception.where, "create_app")
                self.assertNotIn("secret", str(caught.exception))
                self.assertIn(type(error).__name__, str(caught.exception))

    def test_without_wiring_create_app_gets_the_settings_only(self):
        settings = make_settings()
        with patch("paw_backend.server.create_app") as create:
            build_app(settings, RuntimeWiring())
        create.assert_called_once_with(settings)


class MainTest(unittest.TestCase):
    def run_main(self, **environment):
        stderr = io.StringIO()
        with (
            paw_environment(**environment),
            contextlib.redirect_stderr(stderr),
            patch("paw_backend.server.uvicorn.Server") as server,
            patch("paw_backend.server.create_app", return_value=FastAPI()) as create,
        ):
            try:
                main()
                code = 0
            except SystemExit as stop:
                code = stop.code
        return code, stderr.getvalue(), create, server

    def test_unset_starts_as_before(self):
        with self.assertLogs("paw_backend.server", "WARNING"):
            code, _, create, server = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(create.call_count, 1)
        self.assertEqual(create.call_args.kwargs, {})
        server.return_value.run.assert_called_once()

    def test_a_file_is_passed_to_create_app(self):
        with patch(
            "paw_backend.server.load_runtime_config", fake_load(MINIMAL_COMPUTE)
        ):
            code, _, create, server = self.run_main(PAW_RUNTIME_CONFIG_FILE=PATH)
        self.assertEqual(code, 0)
        self.assertEqual(set(create.call_args.kwargs), {"compute"})
        server.return_value.run.assert_called_once()

    def test_a_missing_file_exits_2_without_its_path(self):
        code, stderr, create, server = self.run_main(
            PAW_RUNTIME_CONFIG_FILE="/zq-nowhere/runtime.toml"
        )
        self.assertEqual(code, 2)
        self.assertIn(
            "Invalid runtime configuration: PAW_RUNTIME_CONFIG_FILE: ", stderr
        )
        self.assertNotIn("zq-nowhere", stderr)
        create.assert_not_called()
        server.return_value.run.assert_not_called()

    def test_a_wrong_file_exits_2(self):
        text = MINIMAL_COMPUTE + '\n[compute.deployments.commands]\npids = ["/zq/x"]\n'
        with patch("paw_backend.server.load_runtime_config", fake_load(text)):
            code, stderr, create, _ = self.run_main(PAW_RUNTIME_CONFIG_FILE=PATH)
        self.assertEqual(code, 2)
        self.assertIn(
            "Invalid runtime configuration: "
            "compute.deployments[0].commands.pids: argv[0] does not exist",
            stderr,
        )
        self.assertNotIn("/zq/x", stderr)
        create.assert_not_called()

    def test_a_refusal_of_create_app_exits_2(self):
        stderr = io.StringIO()
        with (
            paw_environment(PAW_RUNTIME_CONFIG_FILE=PATH),
            contextlib.redirect_stderr(stderr),
            patch("paw_backend.server.uvicorn.Server") as server,
            patch("paw_backend.server.load_runtime_config", fake_load(MINIMAL_COMPUTE)),
            patch("paw_backend.server.create_app", side_effect=TypeError("zq")),
            self.assertRaises(SystemExit) as caught,
        ):
            main()
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("Invalid runtime configuration: create_app: ", stderr.getvalue())
        server.return_value.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
