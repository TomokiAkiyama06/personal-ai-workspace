"""The recorder of the Codex smoke test (Decision 0101, 4; issue #305): it
answers the first request with a call of the namespaced PAW ``submit`` and
records what the model was offered. The session's config is the backend's
with the recorder as its provider, and its catalog line only dropped with
``--without-catalog`` (Decision 0104: no override of the smoke's own). The
smoke itself runs a real ``codex`` and is a human step
(``deploy/cli-session/smoke_codex_recorder.py``)."""

import importlib.util
import json
import shutil
import tempfile
import tomllib
import unittest
import urllib.request
from pathlib import Path

from paw_backend.agents.codex_app_server import model_tool_names
from paw_backend.agents.codex_catalog import REPOSITORY_CATALOGS
from paw_backend.agents.codex_cli import probe_config

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "deploy"
    / "cli-session"
    / "smoke_codex_recorder.py"
)


def load():
    spec = importlib.util.spec_from_file_location("smoke_codex_recorder", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PAW = {
    "type": "namespace",
    "name": "mcp__paw",
    "tools": [{"type": "function", "name": "submit", "parameters": {}}],
}


class RecorderTest(unittest.TestCase):
    def setUp(self):
        self.recorder = load().Recorder(model_tool_names)
        self.addCleanup(self.recorder.close)

    def post(self, body):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.recorder.port}/v1/responses",
            data=json.dumps(body).encode(),
            headers={"content-type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as answer:
            text = answer.read().decode()
        return [
            json.loads(line[len("data: ") :])
            for line in text.splitlines()
            if line.startswith("data: ")
        ]

    def test_the_first_answer_calls_the_namespaced_submit(self):
        events = self.post(
            {"model": "m", "tools": [PAW], "input": [{"type": "message"}]}
        )
        item = events[1]["item"]
        self.assertEqual(item["type"], "function_call")
        self.assertEqual((item["namespace"], item["name"]), ("mcp__paw", "submit"))
        self.assertEqual(json.loads(item["arguments"]), {"summary": "ok"})
        self.assertEqual(events[2]["response"]["usage"]["input_tokens"], 1000)
        events = self.post({"model": "m", "tools": [PAW], "input": []})
        self.assertEqual(events[1]["item"]["type"], "message")
        (first, second) = self.recorder.requests
        self.assertEqual(first["model_tool_names"], ["mcp__paw__submit"])
        self.assertEqual(first["input_item_types"], ["message"])
        self.assertEqual(second["tools"], [PAW])

    def test_without_submit_it_only_answers(self):
        events = self.post({"model": "m", "tools": [{"type": "tool_search"}]})
        self.assertEqual(events[1]["item"]["type"], "message")

    def test_the_probes_recorder_answers_the_probes_word(self):
        # ``--probe`` (Decision 0100, 10): the answer the probe accepts, and
        # a request that offers no tool is recorded as such.
        recorder = load().Recorder(model_tool_names, message="PAW_PROBE_OK")
        self.addCleanup(recorder.close)
        self.recorder = recorder
        events = self.post({"model": "m", "input": [{"type": "message"}]})
        (content,) = events[1]["item"]["content"]
        self.assertEqual(content["text"], "PAW_PROBE_OK")
        (request,) = recorder.requests
        self.assertEqual((request["tools"], request["model_tool_names"]), ([], []))


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.module = load()
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.catalog = self.directory / "models.json"
        self.catalog.write_text(
            REPOSITORY_CATALOGS.session_catalog("0.156.1", "gpt-6-sol")
        )

    def test_the_backends_config_with_the_recorder(self):
        backend = probe_config(self.catalog)
        config = tomllib.loads(self.module.with_recorder(backend, 4242))
        self.assertEqual(config["model_catalog_json"], str(self.catalog))
        self.assertEqual(config["model_provider"], "pawsmoke")
        self.assertEqual(
            config["model_providers"]["pawsmoke"]["base_url"],
            "http://127.0.0.1:4242/v1",
        )
        # Everything else is the backend's.
        del config["model_provider"], config["model_providers"]
        self.assertEqual(config, tomllib.loads(backend))

    def test_without_catalog_drops_only_that_line(self):
        backend = probe_config(self.catalog)
        text = self.module.with_recorder(backend, 4242, catalog=False)
        config = tomllib.loads(text)
        self.assertNotIn("model_catalog_json", config)
        expected = tomllib.loads(backend)
        del expected["model_catalog_json"]
        del config["model_provider"], config["model_providers"]
        self.assertEqual(config, expected)

    def test_the_result_says_which_catalog_and_values(self):
        seen = self.module.catalog_seen(
            probe_config(self.catalog), "gpt-6-sol", "0.156.1"
        )
        self.assertTrue(seen["in_backend_config"])
        self.assertEqual(seen["repository_file"], "codex-models-0.156.1.json")
        self.assertRegex(seen["repository_sha256"], "^[0-9a-f]{64}$")
        self.assertEqual(
            seen["entry"],
            {
                "tool_mode": "direct",
                "multi_agent_version": None,
                "experimental_supported_tools": [],
                "use_responses_lite": False,
                "supports_search_tool": False,
                "supports_experimental_context": True,
            },
        )

    def test_the_default_model_is_the_catalog_model(self):
        self.assertEqual(self.module.MODEL, "gpt-6-sol")


if __name__ == "__main__":
    unittest.main()
