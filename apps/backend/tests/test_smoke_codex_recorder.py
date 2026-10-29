"""The recorder of the Codex smoke test (Decision 0101, 4; issue #305): it
answers the first request with a call of the namespaced PAW ``submit`` and
records what the model was offered. The smoke itself runs a real ``codex``
and is a human step (``deploy/cli-session/smoke_codex_recorder.py``)."""

import importlib.util
import json
import unittest
import urllib.request
from pathlib import Path

from paw_backend.agents.codex_app_server import model_tool_names

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


if __name__ == "__main__":
    unittest.main()
