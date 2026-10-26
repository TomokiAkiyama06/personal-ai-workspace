"""What the sandbox's ``git`` may be asked, word by word (Decision 0083, 2).

Inside a sandbox ``git`` is a shim (``deploy/sandbox/paw-git-shim``): it never
runs git there, it sends its arguments to the backend, which runs a read-only
git on the host with the git directory pinned (``gitread.py``). This module turns
those arguments — what a model typed — into a :class:`GitReadArgs` of **canonical**
words, or refuses them (:class:`GitReadRefused`, a fixed code). Nothing here is
guessed or passed through:

* the sub-command is ``status``, ``diff``, ``log`` or ``show``; ``commit``,
  ``checkout``, ``reset`` and the rest are refused;
* every option is in a fixed table for its sub-command and is rewritten to one
  spelling (``-s`` → ``--short``, ``-n 5`` / ``-5`` → ``--max-count=5``);
  ``--no-index``, ``-O``, ``--output``, ``--ext-diff``, ``--textconv``, ``-c``,
  ``--git-dir``, ``--work-tree``, ``--exec-path`` and everything else not in a
  table are refused. ``--format`` / ``--pretty`` take only the named formats;
* a revision is ``HEAD``, ``HEAD~<n>`` (``HEAD^`` / ``HEAD~`` as their
  ``HEAD~<n>``), the node's base ref (``refs/paw/base/<branch>``, or a
  reviewer's ``refs/paw/review-base/<branch>``, Decision 0099, 5) or a full
  40 / 64 character object id, alone, as ``A..B`` or (``log``) ``^A``;
  ``<rev>:<path>``, ``A...B``, ``@{...}``, branch names and abbreviated ids are
  refused. Whether a revision lies in the node's range is decided on the host
  (``gitread.py``), after it is resolved to a commit;
* any other word is a path (all words after ``--`` are). The host turns each
  into a ``:(literal)`` pathspec only once the broker's scope check passed (a
  branch name is such a word: as a literal path it matches nothing).

The lists of canonical options are repeated in the SSH wrapper, which re-checks
the host command it is sent (``deploy/ssh-git-wrapper``); a test keeps the two
equal.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType


class GitRefusal(StrEnum):
    """Why the sandbox's git refused. The value is what the model is shown."""

    SUBCOMMAND = "subcommand_not_allowed"
    OPTION = "option_not_allowed"
    REVISION = "revision_not_allowed"
    OUT_OF_RANGE = "revision_out_of_range"
    # A node without a worktree (or whose base is not known) reads the current
    # state only: no ``show``, no revision for ``diff``, ``log`` by name only.
    HISTORY = "history_not_available"
    PATH = "path_not_allowed"
    TOO_LONG = "too_long"
    UNAVAILABLE = "repository_unavailable"


class GitReadRefused(Exception):
    """A git call the shim does not run. ``code`` is fixed, never an argument."""

    def __init__(self, code: GitRefusal) -> None:
        super().__init__(code.value)
        self.code = code


#: What the model sees for a refusal (it must be able to correct its call).
REFUSAL_HINTS: Mapping[GitRefusal, str] = MappingProxyType(
    {
        GitRefusal.SUBCOMMAND: (
            "only `git status`, `git diff`, `git log` and `git show` are"
            " available here (read-only); do not commit"
        ),
        GitRefusal.OPTION: "this option is not available in the sandbox's git",
        GitRefusal.REVISION: (
            "use HEAD, HEAD~<n> or a full 40-character commit id"
            " (`git log --format=oneline` shows them)"
        ),
        GitRefusal.OUT_OF_RANGE: (
            "only the commits of this task's branch (since its base) can be read"
        ),
        GitRefusal.HISTORY: (
            "only the current state can be read here: `git status`, `git diff`"
            " without revisions, and `git log` with --oneline, --stat,"
            " --name-only or -n <count>"
        ),
        GitRefusal.PATH: "the path is outside this repository or not readable",
        GitRefusal.TOO_LONG: "the git command is too long",
        GitRefusal.UNAVAILABLE: "git is not available for this directory",
    }
)

READ_SUBCOMMANDS = ("status", "diff", "log", "show")
MAX_WORDS = 64
MAX_WORD_CHARS = 1024
MAX_COUNT = 100_000
MAX_CONTEXT_LINES = 1000
MAX_HEAD_DEPTH = 10_000

#: The named formats ``--format=`` / ``--pretty=`` may take (no custom format).
FORMATS = ("oneline", "short", "medium", "full", "fuller", "reference")

#: Options shared by ``diff``, ``log`` and ``show`` (how a change is shown).
_DIFF_OUTPUT: dict[str, str] = {
    "--stat": "--stat",
    "--numstat": "--numstat",
    "--shortstat": "--shortstat",
    "--name-only": "--name-only",
    "--name-status": "--name-status",
    "--summary": "--summary",
    "-p": "--patch",
    "--patch": "--patch",
    "--no-renames": "--no-renames",
    "-M": "--find-renames",
    "--find-renames": "--find-renames",
    "-w": "--ignore-all-space",
    "--ignore-all-space": "--ignore-all-space",
    "--ignore-space-change": "--ignore-space-change",
    "--ignore-blank-lines": "--ignore-blank-lines",
    "--word-diff": "--word-diff=plain",
    "--word-diff=plain": "--word-diff=plain",
    "--minimal": "--minimal",
    "--patience": "--patience",
    "--histogram": "--histogram",
}
#: Options the backend always adds itself; a model's copy is dropped.
_DROPPED = frozenset({"--no-color", "--color=never", "--no-ext-diff", "--no-textconv"})

_OPTIONS: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {
        "status": MappingProxyType(
            {
                "-s": "--short",
                "--short": "--short",
                "-b": "--branch",
                "--branch": "--branch",
                "--porcelain": "--porcelain=v1",
                "--porcelain=v1": "--porcelain=v1",
                "--long": "--long",
                "-z": "-z",
                "-u": "--untracked-files=all",
                "-uall": "--untracked-files=all",
                "--untracked-files": "--untracked-files=all",
                "--untracked-files=all": "--untracked-files=all",
                "-unormal": "--untracked-files=normal",
                "--untracked-files=normal": "--untracked-files=normal",
                "-uno": "--untracked-files=no",
                "--untracked-files=no": "--untracked-files=no",
            }
        ),
        "diff": MappingProxyType(
            {
                **_DIFF_OUTPUT,
                "-u": "--patch",
                "--cached": "--cached",
                "--staged": "--cached",
                "-b": "--ignore-space-change",
                "-R": "-R",
                "--exit-code": "--exit-code",
                "--quiet": "--quiet",
                "-z": "-z",
            }
        ),
        "log": MappingProxyType(
            {
                **_DIFF_OUTPUT,
                "-u": "--patch",
                "-s": "--no-patch",
                "--no-patch": "--no-patch",
                "--oneline": "--oneline",
                "--abbrev-commit": "--abbrev-commit",
                "--reverse": "--reverse",
                "--first-parent": "--first-parent",
                "--no-merges": "--no-merges",
                "--merges": "--merges",
                "--graph": "--graph",
            }
        ),
        "show": MappingProxyType(
            {
                **_DIFF_OUTPUT,
                "-s": "--no-patch",
                "--no-patch": "--no-patch",
                "--oneline": "--oneline",
                "--abbrev-commit": "--abbrev-commit",
            }
        ),
    }
)

#: The canonical words each sub-command may carry (besides ``--max-count=<n>``,
#: ``--skip=<n>``, ``--unified=<n>`` and ``--format=<named>``): what the wrapper
#: accepts in the host command.
CANONICAL_OPTIONS: Mapping[str, frozenset[str]] = MappingProxyType(
    {name: frozenset(table.values()) for name, table in _OPTIONS.items()}
)
#: Which numbered / named option forms each sub-command takes.
_COUNTED = frozenset({"log"})  # --max-count=<n>, --skip=<n>
_CONTEXT = frozenset({"diff", "log", "show"})  # --unified=<n>
_FORMATTED = frozenset({"log", "show"})  # --format=<named>

#: The few options a node without a worktree (or without a base) may give
#: ``log`` (Decision 0083, 2: names, never contents, of the history).
NAMES_ONLY_LOG = frozenset({"--oneline", "--stat", "--name-only"})

_OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_HEAD = re.compile(r"HEAD(?:~(?P<tilde>[0-9]{0,5})|(?P<carets>\^{1,8}))?")
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
BASE_REF_PREFIX = "refs/paw/base/"
#: A reviewer's review base (Decision 0099, 5).
REVIEW_BASE_REF_PREFIX = "refs/paw/review-base/"
_NUMBER = re.compile(r"[0-9]{1,6}")


def valid_branch(value: str) -> bool:
    """A branch name the backend makes (``paw/...``), spelled safely."""
    if _BRANCH.fullmatch(value) is None or ".." in value or value.endswith(("/", ".")):
        return False
    return all(
        part and not part.startswith(".") and not part.endswith(".lock")
        for part in value.split("/")
    )


@dataclass(frozen=True, slots=True)
class Revision:
    """One revision word: ``HEAD``, ``HEAD~<n>``, a base ref or an object id.
    ``negated``: ``^A`` (or the left side of ``A..B``)."""

    text: str
    negated: bool = False


@dataclass(frozen=True, slots=True)
class GitReadArgs:
    """A checked git call: canonical options, revisions and raw paths."""

    subcommand: str
    options: tuple[str, ...]
    revisions: tuple[Revision, ...]
    paths: tuple[str, ...]

    @property
    def positive(self) -> tuple[Revision, ...]:
        return tuple(r for r in self.revisions if not r.negated)


def canonical_revision(word: str) -> str | None:
    """The canonical spelling of one revision word (``None``: not one)."""
    if _OBJECT_ID.fullmatch(word):
        return word
    found = _HEAD.fullmatch(word)
    if found is not None:
        if found["carets"]:
            depth = len(found["carets"])
        elif found["tilde"] is not None:
            depth = int(found["tilde"]) if found["tilde"] else 1
        else:
            return "HEAD"
        if not 0 <= depth <= MAX_HEAD_DEPTH:
            return None
        return "HEAD" if depth == 0 else f"HEAD~{depth}"
    for prefix in (BASE_REF_PREFIX, REVIEW_BASE_REF_PREFIX):
        if word.startswith(prefix) and valid_branch(word.removeprefix(prefix)):
            return word
    return None


def _looks_like_revision(word: str) -> bool:
    """Whether a word is meant as a revision (a refused one is refused, never
    taken for a path): ``..`` ranges, ``^x``, ``x~n``, ``x^``, ``x@{...}``,
    ``x:y`` and the names the grammar knows."""
    return (
        canonical_revision(word) is not None
        or ".." in word
        or word.startswith("^")
        or "@{" in word
        or "^{" in word
        or (":" in word and not word.startswith(":"))
        or re.fullmatch(r"[^/]*[~^][0-9]*", word) is not None
        or re.fullmatch(r"[0-9a-fA-F]{7,64}", word) is not None
        or word in ("@", "HEAD", "FETCH_HEAD", "ORIG_HEAD", "MERGE_HEAD")
    )


def _revisions(word: str, subcommand: str) -> list[Revision]:
    """The revisions of one word, or ``GitReadRefused`` (``REVISION``)."""
    if "..." in word:
        raise GitReadRefused(GitRefusal.REVISION)
    if ".." in word:
        left, _, right = word.partition("..")
        start = canonical_revision(left or "HEAD")
        end = canonical_revision(right or "HEAD")
        if start is None or end is None or subcommand not in ("diff", "log"):
            raise GitReadRefused(GitRefusal.REVISION)
        return [Revision(start, negated=True), Revision(end)]
    if word.startswith("^") and not word.startswith("^{"):
        found = canonical_revision(word[1:])
        if found is None or subcommand != "log":
            raise GitReadRefused(GitRefusal.REVISION)
        return [Revision(found, negated=True)]
    found = canonical_revision(word)
    if found is None:
        raise GitReadRefused(GitRefusal.REVISION)
    return [Revision(found)]


def _number(text: str, maximum: int) -> int:
    if _NUMBER.fullmatch(text) is None or not 0 <= int(text) <= maximum:
        raise GitReadRefused(GitRefusal.OPTION)
    return int(text)


def parse_git_read(argv: Sequence[str]) -> GitReadArgs:
    """The checked form of ``git <argv>``, or :class:`GitReadRefused`."""
    if isinstance(argv, str) or not isinstance(argv, Sequence):
        raise GitReadRefused(GitRefusal.SUBCOMMAND)
    words = list(argv)
    if len(words) > MAX_WORDS:
        raise GitReadRefused(GitRefusal.TOO_LONG)
    for word in words:
        if type(word) is not str or "\x00" in word:
            raise GitReadRefused(GitRefusal.OPTION)
        if len(word) > MAX_WORD_CHARS:
            raise GitReadRefused(GitRefusal.TOO_LONG)
    # Global options a model often types out of habit and that change nothing
    # here (there is never a pager). Any other one is refused.
    while words and words[0] in ("--no-pager", "-P"):
        words.pop(0)
    if not words or words[0] not in READ_SUBCOMMANDS:
        raise GitReadRefused(GitRefusal.SUBCOMMAND)
    subcommand, rest = words[0], words[1:]
    table = _OPTIONS[subcommand]
    options: list[str] = []
    revisions: list[Revision] = []
    paths: list[str] = []
    index = 0
    after_separator = False
    while index < len(rest):
        word = rest[index]
        index += 1
        if after_separator:
            paths.append(word)
            continue
        if word == "--":
            after_separator = True
            continue
        if word.startswith("-") and word != "-":
            option, used = _option(subcommand, table, word, rest[index:])
            index += used
            if option is not None:
                options.append(option)
            continue
        if subcommand != "status" and _looks_like_revision(word):
            revisions.extend(_revisions(word, subcommand))
            continue
        paths.append(word)
    if subcommand == "diff" and len([r for r in revisions if not r.negated]) > 2:
        raise GitReadRefused(GitRefusal.REVISION)
    if subcommand == "diff" and len([r for r in revisions if r.negated]) > 1:
        raise GitReadRefused(GitRefusal.REVISION)
    if subcommand == "show" and len(revisions) > 4:
        raise GitReadRefused(GitRefusal.REVISION)
    # One spelling per option, in the order first given.
    return GitReadArgs(
        subcommand,
        tuple(dict.fromkeys(options)),
        tuple(revisions),
        tuple(paths),
    )


def _option(
    subcommand: str, table: Mapping[str, str], word: str, following: list[str]
) -> tuple[str | None, int]:
    """The canonical spelling of option ``word`` (``None``: dropped) and how
    many of the ``following`` words it used, or ``GitReadRefused``."""
    if word in _DROPPED and subcommand != "status":
        return None, 0
    if word in table:
        return table[word], 0
    if subcommand in _COUNTED:
        if word in ("-n", "--max-count"):
            if not following:
                raise GitReadRefused(GitRefusal.OPTION)
            return f"--max-count={_number(following[0], MAX_COUNT)}", 1
        if word.startswith("--max-count="):
            return f"--max-count={_number(word[12:], MAX_COUNT)}", 0
        if word.startswith("-n") and len(word) > 2:
            return f"--max-count={_number(word[2:], MAX_COUNT)}", 0
        if _NUMBER.fullmatch(word[1:]):
            return f"--max-count={_number(word[1:], MAX_COUNT)}", 0
        if word.startswith("--skip="):
            return f"--skip={_number(word[7:], MAX_COUNT)}", 0
    if subcommand in _CONTEXT:
        if word.startswith("--unified="):
            return f"--unified={_number(word[10:], MAX_CONTEXT_LINES)}", 0
        if word.startswith("-U") and len(word) > 2:
            return f"--unified={_number(word[2:], MAX_CONTEXT_LINES)}", 0
    if subcommand in _FORMATTED:
        if word == "--pretty":
            return "--format=medium", 0
        for prefix in ("--format=", "--pretty="):
            if word.startswith(prefix):
                name = word.removeprefix(prefix)
                if name not in FORMATS:
                    raise GitReadRefused(GitRefusal.OPTION)
                return f"--format={name}", 0
    raise GitReadRefused(GitRefusal.OPTION)


def is_canonical_option(subcommand: str, word: str) -> bool:
    """Whether ``word`` is a canonical option of ``subcommand`` (what the
    backend may send to the host's git)."""
    if word in CANONICAL_OPTIONS.get(subcommand, ()):
        return True
    for prefix, kinds, maximum in (
        ("--max-count=", _COUNTED, MAX_COUNT),
        ("--skip=", _COUNTED, MAX_COUNT),
        ("--unified=", _CONTEXT, MAX_CONTEXT_LINES),
    ):
        if subcommand in kinds and word.startswith(prefix):
            text = word.removeprefix(prefix)
            return _NUMBER.fullmatch(text) is not None and int(text) <= maximum
    if subcommand in _FORMATTED and word.startswith("--format="):
        return word.removeprefix("--format=") in FORMATS
    return False
