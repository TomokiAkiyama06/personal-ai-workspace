"""Importable worker factories used by the benchmark CLI tests."""

import json

VALID_OUTPUT = json.dumps(
    {
        "memories": [
            {
                "key": "favorite_color",
                "scope": "user",
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


def make_failing_worker():
    raise RuntimeError("model init failed: token=SECRET-TOKEN")


def make_none_worker():
    return None


def make_wrong_signature_worker():
    class WrongSignature:
        def extract(self):
            return "{}"

    return WrongSignature()
