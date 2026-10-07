"""The decisions of the Inferred Preference flow, as pure functions (PAW-044).

No database. The service (``service.py``) reads the owner's own journal and memory
rows and asks these functions what they mean; keeping them apart lets
``tests/test_preference_rules.py`` try every branch with plain values, and lets a
reviewer read the policy in one place (Decision 0081).

The flow (REQUIREMENTS.md "Inferred Preference / Confirmation Flow",
docs/MEMORY_ARCHITECTURE.md section 9)::

    Conversation / Action -> Observation -> Inferred Preference Candidate
        -> evidence / scope -> User Confirmation -> Confirmed Memory

* **Observation**: one item of a consolidated journal entry's outcome that names a
  worker key (PAW-041 already stores it: ``memory_journal_entries.outcome``, with
  the project / repository the conversation was in). Nothing new is stored for it.
* **Candidate**: the owner's private, unconfirmed (``observed`` / ``inferred``)
  memory of a key, or a candidate the consolidator HELD for the user (a high-risk
  area, one that would replace a confirmed memory, or one about a memory the user
  widened: Decision 0018).
* **Evidence**: the five factors the requirements name (``frequency``,
  ``scope_diversity``, ``language_strength``, ``consistency``, ``risk_level``),
  computed from the observations of the candidate's key (:func:`evidence`).
* **Recommendation**: the scope rule of the requirements (:func:`recommend_scope`):
  repeated in one repository only -> Repo, in several repositories of one project
  -> Project, in several projects (or outside any project) -> User.
* **Ready**: when the confirmation UI should ask (:func:`is_ready`).

Nothing here confirms anything: only a person's explicit choice does (``service``),
and a high-risk candidate additionally needs an explicit acknowledgement.
"""

import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from paw_backend.memory.journal.domain import ItemResult
from paw_backend.memory.journal.rules import is_high_risk

# How many observations of a key make an ordinary candidate worth asking about
# ("反復が十分に蓄積した場合"). Provisional (Decision 0081 point 3).
MIN_REPEATS = 3


class LanguageStrength(StrEnum):
    """What the person's own words say about how long a preference holds."""

    # "今後" / "基本的に" / "always": meant as a standing default.
    STANDING = "standing"
    # Neither: the usual case.
    NEUTRAL = "neutral"
    # "今回" / "this time": meant for this once.
    ONCE = "once"


class Consistency(StrEnum):
    """Whether the observations of a key agree with each other."""

    CONSISTENT = "consistent"  # every observation said the same
    # The worker revised the candidate (a new version of the key): the preference
    # moved, which is not a contradiction.
    CHANGED = "changed"
    # A recorded contradiction: a ``conflicts_with`` relation touches the
    # candidate, or it contradicts a memory the user confirmed (``held_confirmed``).
    CONFLICTING = "conflicting"


class RiskLevel(StrEnum):
    LOW = "low"
    HIGH = "high"


class TargetScope(StrEnum):
    """Where a confirmed preference applies (the buttons of the confirmation UI)."""

    REPO = "repo"  # [このRepoだけ]
    PROJECT = "project"  # [このProject]
    USER = "user"  # [すべてのProject]: the person's own User Memory


class CandidateKind(StrEnum):
    MEMORY = "memory"  # an unconfirmed private memory version
    HELD = "held"  # a candidate the consolidator held for the user (no memory yet)


# The results of a journal outcome item that are an observation of its key: the
# worker said something about the key and the backend wrote it, found it written
# already, or held it for the user. ``stale`` (an older turn), ``blocked_by_user``
# (the user rejected it), ``refused_shared``, ``no_content`` and ``duplicate_key``
# are not evidence of a preference.
OBSERVED_RESULTS = frozenset(
    {
        ItemResult.CREATED,
        ItemResult.UPDATED,
        ItemResult.DUPLICATE,
        ItemResult.HELD_CONFIRMED,
        ItemResult.HELD_HIGH_RISK,
        ItemResult.HELD_WIDENED,
    }
)
HELD_RESULTS = frozenset(
    {ItemResult.HELD_CONFIRMED, ItemResult.HELD_HIGH_RISK, ItemResult.HELD_WIDENED}
)


# ---------------------------------------------------------------------------
# Language strength
# ---------------------------------------------------------------------------

# The words of the requirements' example ("今回", "今後", "基本的に") and their
# usual neighbours. A heuristic: the strength only decides when to ASK, never what
# is stored, so a miss costs one prompt more or one prompt later.
STANDING_PHRASES = (
    "今後", "これから", "基本的に", "基本は", "いつも", "毎回", "常に", "原則",
    "デフォルト", "既定", "ずっと",
)  # fmt: skip
STANDING_WORDS = (
    "always", "from now on", "by default", "every time", "going forward",
    "in general", "from here on",
)  # fmt: skip
ONCE_PHRASES = ("今回", "今だけ", "今日だけ", "この時だけ", "一時的に", "とりあえず")
ONCE_WORDS = ("this time", "just once", "only now", "for now", "this once")
_SPACES = re.compile(r"\s+")


def _fold(text: str) -> str:
    """NFKC (full-width letters), lower case, single spaces."""
    return _SPACES.sub(" ", unicodedata.normalize("NFKC", text)).lower()


def language_strength(text: str | None) -> LanguageStrength:
    """The strength one message expresses. A standing phrase wins over a once
    phrase ("今回も今後も" is a standing preference)."""
    if not text:
        return LanguageStrength.NEUTRAL
    folded = _fold(text)
    if any(p in folded for p in STANDING_PHRASES) or any(
        _has_words(folded, w) for w in STANDING_WORDS
    ):
        return LanguageStrength.STANDING
    if any(p in folded for p in ONCE_PHRASES) or any(
        _has_words(folded, w) for w in ONCE_WORDS
    ):
        return LanguageStrength.ONCE
    return LanguageStrength.NEUTRAL


def _has_words(folded: str, words: str) -> bool:
    return (
        re.search(rf"(?<![a-z0-9]){re.escape(words)}(?![a-z0-9])", folded) is not None
    )


def overall_strength(strengths: Iterable[LanguageStrength]) -> LanguageStrength:
    """Standing if any observation was standing; once if every one was once."""
    seen = list(strengths)
    if LanguageStrength.STANDING in seen:
        return LanguageStrength.STANDING
    if seen and all(s is LanguageStrength.ONCE for s in seen):
        return LanguageStrength.ONCE
    return LanguageStrength.NEUTRAL


# ---------------------------------------------------------------------------
# Observations and evidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Observation:
    """One journal outcome item about a key: where and when, and what came of it.

    ``strength`` is computed by the service from the message text, which is not
    kept here (nothing of a conversation leaves the service).
    """

    entry_id: UUID
    project_id: UUID | None
    repo_id: UUID | None
    recorded_at: datetime
    result: ItemResult
    strength: LanguageStrength = LanguageStrength.NEUTRAL


@dataclass(frozen=True, slots=True)
class Recommendation:
    """The scope the requirements' rule recommends, with the ids it needs."""

    scope: TargetScope
    project_id: UUID | None = None
    repo_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class Evidence:
    frequency: int
    project_count: int
    repo_count: int
    # Observations outside any project (a personal conversation).
    outside_projects: int
    last_observed_at: datetime | None
    language_strength: LanguageStrength
    consistency: Consistency
    risk_level: RiskLevel


def recommend_scope(observations: Sequence[Observation]) -> Recommendation:
    """The requirements' scope rule over where the key was observed.

    * every observation in the same repository -> Repo (that repository);
    * every observation in the same project, but not one repository -> Project;
    * several projects, or any observation outside a project, or none -> User.

    A repository is only recommended together with its project (the repository's
    real project is checked again when the person confirms).
    """
    if not observations:
        return Recommendation(TargetScope.USER)
    projects = {o.project_id for o in observations}
    if None in projects or len(projects) > 1:
        return Recommendation(TargetScope.USER)
    (project_id,) = projects
    repos = {o.repo_id for o in observations}
    if len(repos) == 1 and None not in repos:
        (repo_id,) = repos
        return Recommendation(TargetScope.REPO, project_id, repo_id)
    return Recommendation(TargetScope.PROJECT, project_id)


def evidence(
    observations: Sequence[Observation],
    *,
    texts: Iterable[str | None],
    conflicting: bool = False,
) -> Evidence:
    """The five factors over the observations of one key.

    ``texts`` are the candidate's key and content (and, for a free-text answer, its
    interpretation): what the high-risk vocabulary of the Journal is applied to.
    ``conflicting`` is a recorded ``conflicts_with`` relation of the candidate.
    """
    results = {o.result for o in observations}
    if conflicting or ItemResult.HELD_CONFIRMED in results:
        consistency = Consistency.CONFLICTING
    elif ItemResult.UPDATED in results:
        consistency = Consistency.CHANGED
    else:
        consistency = Consistency.CONSISTENT
    risky = ItemResult.HELD_HIGH_RISK in results or is_high_risk(*texts)
    return Evidence(
        frequency=len({o.entry_id for o in observations}),
        project_count=len({o.project_id for o in observations} - {None}),
        repo_count=len({o.repo_id for o in observations} - {None}),
        outside_projects=sum(1 for o in observations if o.project_id is None),
        last_observed_at=max((o.recorded_at for o in observations), default=None),
        language_strength=overall_strength(o.strength for o in observations),
        consistency=consistency,
        risk_level=RiskLevel.HIGH if risky else RiskLevel.LOW,
    )


def is_ready(found: Evidence) -> bool:
    """Should the confirmation UI ask now?

    Asked when the key was observed ``MIN_REPEATS`` times, or once with standing
    words ("今後は ..."); never while the person's words say "only this time" or the
    observations contradict each other (the conflict is shown, not a prompt). A
    high-risk candidate follows the same rule: being risky is a reason to ask with
    care, not a reason to ask sooner.
    """
    if found.consistency is Consistency.CONFLICTING:
        return False
    if found.language_strength is LanguageStrength.ONCE:
        return False
    return (
        found.frequency >= MIN_REPEATS
        or found.language_strength is LanguageStrength.STANDING
    )


# ---------------------------------------------------------------------------
# The options of the confirmation UI
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Option:
    """One button: a target scope with the ids it needs, and whether it is the one
    the rule recommends."""

    scope: TargetScope
    project_id: UUID | None = None
    repo_id: UUID | None = None
    recommended: bool = False


def _most_observed[T](values: Iterable[tuple[T, datetime]]) -> T | None:
    """The value seen most often; a tie goes to the one seen last."""
    counts: Counter[T] = Counter()
    last: dict[T, datetime] = {}
    for value, at in values:
        counts[value] += 1
        last[value] = max(at, last.get(value, at))
    if not counts:
        return None
    return max(counts, key=lambda v: (counts[v], last[v]))


def options(
    observations: Sequence[Observation], recommendation: Recommendation
) -> tuple[Option, ...]:
    """[このRepoだけ] [このProject] [すべてのProject], narrowest first.

    "This repository" / "this project" are the ones the key was observed in most
    (the recommended ones when the rule picked them); a button is left out when the
    key was never observed in a repository / project. [保存しない] and [その他...]
    are not options of a scope: the API has its own calls for them.
    """
    result: list[Option] = []
    repo = (
        (recommendation.project_id, recommendation.repo_id)
        if recommendation.scope is TargetScope.REPO
        else _most_observed(
            ((o.project_id, o.repo_id), o.recorded_at)
            for o in observations
            if o.project_id is not None and o.repo_id is not None
        )
    )
    if repo is not None:
        result.append(
            Option(
                TargetScope.REPO,
                repo[0],
                repo[1],
                recommended=recommendation.scope is TargetScope.REPO,
            )
        )
    project = (
        recommendation.project_id
        if recommendation.scope is not TargetScope.USER
        else _most_observed(
            (o.project_id, o.recorded_at)
            for o in observations
            if o.project_id is not None
        )
    )
    if project is not None:
        result.append(
            Option(
                TargetScope.PROJECT,
                project,
                recommended=recommendation.scope is TargetScope.PROJECT,
            )
        )
    result.append(
        Option(TargetScope.USER, recommended=recommendation.scope is TargetScope.USER)
    )
    return tuple(result)


__all__ = [
    "HELD_RESULTS",
    "MIN_REPEATS",
    "OBSERVED_RESULTS",
    "CandidateKind",
    "Consistency",
    "Evidence",
    "LanguageStrength",
    "Observation",
    "Option",
    "Recommendation",
    "RiskLevel",
    "TargetScope",
    "evidence",
    "is_ready",
    "language_strength",
    "options",
    "overall_strength",
    "recommend_scope",
]
