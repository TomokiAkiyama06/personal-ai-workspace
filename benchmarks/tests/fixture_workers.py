"""Importable worker factories used by the benchmark CLI tests."""

import json
import threading

VALID_OUTPUT = json.dumps(
    {
        "memories": [
            {
                "key": "favorite_color",
                "scope": "user",
                "state": "confirmed",
                "supersedes": None,
                "content": "The user's favorite color is blue.",
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


HANG_RELEASE = threading.Event()
_hanging_threads: list[threading.Thread] = []


class HangingWorker:
    """Blocks in extract() like a stalled inference until ``release_hanging_workers``."""

    def extract(self, input_text: str) -> str:
        _hanging_threads.append(threading.current_thread())
        HANG_RELEASE.wait(60)
        return VALID_OUTPUT


def make_hanging_worker() -> HangingWorker:
    return HangingWorker()


def release_hanging_workers() -> None:
    """Let every stalled call finish and wait for its thread (test cleanup)."""
    HANG_RELEASE.set()
    for thread in _hanging_threads:
        thread.join(10)
    _hanging_threads.clear()
    HANG_RELEASE.clear()
