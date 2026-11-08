"""The repository's verified pairs of a CLI's version and a model (Decision
0103, 3 A, 1; approved 2026-11-08; issue #341).

A Codex / Claude model may be allowed by the Admin's policy (and sent) only
when this backend's code lists it, paired with the version of the CLI it was
checked on: Codex as ``(codex-cli version, model)`` (its built-in tools depend
on the model's catalog entry, so the tool check of Decision 0101 is per pair),
Claude as ``(Claude Code version, full model id)`` (no alias such as ``opus``:
what an alias names changes with the CLI and the provider).

A pair is added by a code PR (merged by the Human) that records its evidence:
for Codex, the recorder smoke's ``tools`` check (Decision 0101) and the health
probe (Decision 0100) on that version and model; for Claude, the probe and the
``system/init`` tool check (Decision 0100, 2). The hidden Codex models
(``gpt-5.4``, ``codex-auto-review``, ``gpt-daybreak-*``) are never listed.

The pairs listed, with their evidence (production, 2026-11-05 to 2026-11-08):

* Codex ``0.156.1`` x ``gpt-6-sol``: the Codex connection's health probe
  ``connected`` (2026-11-08 16:10, 4084 input / 9 output tokens, one model
  call), and node sessions of task ``516904fd`` (2026-11-08 16:31) whose
  per-session tool check (Decision 0101: every request's ``tools`` the PAW
  tools and the three exceptions, fail closed) passed;
* Claude ``2.1.301`` x ``claude-opus-5-5``: the Claude connection's health
  probe ``connected`` (2026-11-05 15:24, no fallback) and the planner and
  reviewer sessions of the same task, whose ``system/init`` tool check passed.

``gpt-6-astra`` and ``gpt-6-luna`` (the Human's answer 2) are added once their
own smoke passed (the Decision's "PR の分け方" 5).
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from paw_backend.connections.domain import ConnectionKind

#: ``(CLI version, model)`` per connection kind (see the module).
VERIFIED_MODELS: Final[Mapping[ConnectionKind, frozenset[tuple[str, str]]]] = (
    MappingProxyType(
        {
            ConnectionKind.CODEX: frozenset({("0.156.1", "gpt-6-sol")}),
            ConnectionKind.CLAUDE: frozenset({("2.1.301", "claude-opus-5-5")}),
        }
    )
)


def verified_pairs(kind: ConnectionKind) -> frozenset[tuple[str, str]]:
    """The verified ``(version, model)`` pairs of ``kind`` (none for another)."""
    return VERIFIED_MODELS.get(kind, frozenset())


def verified_models(kind: ConnectionKind) -> frozenset[str]:
    """The models of ``kind`` verified on at least one CLI version: the
    candidates the Admin's policy may allow (Decision 0103, 3 A, 2)."""
    return frozenset(model for _, model in verified_pairs(kind))


def verified_versions(kind: ConnectionKind, model: str) -> tuple[str, ...]:
    """The CLI versions ``model`` of ``kind`` was verified on, sorted."""
    return tuple(sorted(v for v, m in verified_pairs(kind) if m == model))


def is_verified(kind: ConnectionKind, version: object, model: object) -> bool:
    """Is ``(version, model)`` a verified pair of ``kind``? Only exact strings
    match (a missing version never does)."""
    return (
        type(version) is str
        and type(model) is str
        and (version, model) in verified_pairs(kind)
    )


__all__ = [
    "VERIFIED_MODELS",
    "is_verified",
    "verified_models",
    "verified_pairs",
    "verified_versions",
]
