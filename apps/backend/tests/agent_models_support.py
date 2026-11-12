"""The repository's verified pairs as a test says (Decision 0103, 3 A).

No Codex pair is verified in the code yet (``agent_models/verified.py``), so a
test that needs a verified Codex model runs under :func:`verified_pairs_of`
with a pair of its own; the shipped pairs are tested as they are in
``test_agents_cli_models``.
"""

from types import MappingProxyType
from unittest.mock import patch

from paw_backend.connections import ConnectionKind

#: A Codex model a test treats as verified (never a real model name).
TEST_CODEX_MODEL = "gpt-test-codex"
TEST_PAIRS = {
    ConnectionKind.CODEX: frozenset({("0.156.1", TEST_CODEX_MODEL)}),
    ConnectionKind.CLAUDE: frozenset({("2.1.301", "claude-opus-5-5")}),
}


def verified_pairs_of(pairs=None):
    """A patch of the verified pairs (``TEST_PAIRS`` by default)."""
    return patch(
        "paw_backend.agent_models.verified.VERIFIED_MODELS",
        MappingProxyType(dict(TEST_PAIRS if pairs is None else pairs)),
    )
