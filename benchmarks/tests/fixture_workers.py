"""Importable worker factories used by the benchmark CLI tests."""

import json

VALID_OUTPUT = json.dumps(
    {
        "memories": [
            {
                "key": "favorite_color",
                "scope": "user_preferences",
                "state": "confirmed",
                "supersedes": None,
            }
        ]
    }
)


class FixedWorker:
    def extract(self, input_text: str) -> str:
        return VALID_OUTPUT


def make_worker() -> FixedWorker:
    return FixedWorker()


class MalformedWorker:
    def extract(self, input_text: str):
        return 5


def make_malformed_worker() -> MalformedWorker:
    return MalformedWorker()
