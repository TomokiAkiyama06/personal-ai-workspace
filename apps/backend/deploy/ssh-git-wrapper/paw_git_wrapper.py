#!/usr/bin/python3 -I
"""The forced command of Decision 0029: git, and nothing else, as this Linux user.

Installed once per server (``/usr/local/lib/paw/paw-git-wrapper``, owned by
root, not writable by anyone else) and named by the ``command=`` option of the
one ``authorized_keys`` line that lets the backend's key in as a workspace
user (Issue #134; see ``README.md`` next to this file). ``sshd`` runs it as that
user, with what the client asked for in ``$SSH_ORIGINAL_COMMAND``. The client
(``paw_backend.repositories.ssh.SshGitRunner``) is **not trusted**: everything it
sends is re-checked here, and anything this file does not recognise exactly is
refused (fail closed). It never runs a shell and never ``eval``-s anything.

Standard library only (it runs as a workspace user, outside the backend's
virtual environment), one file, no configuration file: the only configuration is
the fixed options on the ``command=`` line (``--root``, ``--git``, ...), which
only the server's administrator writes.

What is accepted
----------------

``$SSH_ORIGINAL_COMMAND`` is split with POSIX word rules (``shlex.split``, which
undoes ``shlex.quote``) into the words of Decision 0029 §2::

    paw-git-run/v1  <cwd>  <ceiling or ->  --  [global options]  <sub-command> <args>

* the protocol tag must be exactly ``paw-git-run/v1``;
* ``cwd`` must be a canonical absolute path of an existing directory inside the
  root (``<home>/workspaces`` by default), after every symbolic link is resolved;
* ``ceiling`` must be ``-`` or exactly the parent directory of ``cwd``; the
  parent of the root is always added to it, so git's repository discovery never
  leaves the root;
* global options may only be ``-c <key>=<value>`` whose pair is **in a fixed
  list** (the hardening the client always sends, and — for ``merge`` only — the
  fixed merge identity of Decision 0036), and ``--git-dir=`` / ``--work-tree=``
  (Decision 0036 §13, with the human's condition of 2026-09-28): both or
  neither, only for the sub-commands that run inside a worktree, the work tree
  inside ``<root>/.paw-worktrees`` and the git directory a ``worktrees/<name>``
  directory inside the root but outside ``.paw-worktrees``, each checked after
  resolving every symbolic link;
* the git directory's ``commondir`` must lead back to the ``.git`` it sits in,
  and its ``gitdir`` must name the work tree's ``.git``: a worktree directory
  is only ever used with its own work tree;
* the sub-command and its arguments must match one of the fixed shapes of
  Decision 0029 §3 and Decision 0036 §13 (:data:`SUBCOMMANDS`) exactly;
* for a call not pinned to a git directory (``clone`` aside), the git
  directory and common directory git finds from the cwd must be inside the
  root and outside ``.paw-worktrees`` (:func:`check_location`), and hold no
  symbolic link (:func:`check_links`; so must a pinned call's);
* for a sub-command that reads file content (:data:`CONTENT_SUBCOMMANDS`), the
  configuration git would read must name no command (a ``filter`` driver, a
  ``merge`` driver, an ``include``, ...: :func:`check_configuration`).

The client's ``-c`` values are checked, then **dropped**: git always gets this
file's own hardening (:func:`hardening`) and, for ``merge``, this file's own
merge identity, never the client's copy. The environment of git is a fixed
allowlist (:func:`git_environment`); nothing of ``sshd``'s environment (and so
nothing a client could send with ``SendEnv``) reaches it.

Exit status: git's own when a command is accepted (this process ``exec``-s git);
:data:`REJECTED` (126) when it is refused; never 255, which ``SshGitRunner``
reads as "ssh itself failed" (Decision 0029 §5).

What is logged
--------------

One ``syslog`` line per call (``LOG_AUTH``): accepted or rejected, a fixed
reason code, the sub-command name when it is one of the allowed ones, and the
Linux user. Never an argument, a path, a URL, a ``-c`` value or git's output: a
URL or a configuration value may carry a credential (Issue #134).
"""

import os
import pwd
import re
import selectors
import shlex
import stat
import subprocess
import sys
import syslog
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

PROTOCOL_TAG = "paw-git-run/v1"
#: Exit status of a refused call: not git's 0/1 (``merge-base --is-ancestor`` and
#: ``merge-tree`` read those as answers), not 128/129 (git's own failures), not
#: 255 (``ssh``'s own failure, Decision 0029 §5).
REJECTED = 126
SAFE_PATH = "/usr/local/bin:/usr/bin:/bin"
WORKTREE_DIRECTORY = ".paw-worktrees"
BRANCH_NAMESPACE = "paw"
MAX_COMMAND_BYTES = 16_384
MAX_PATH_CHARS = 1024
MAX_URL_CHARS = 2048

#: The fixed merge identity and signing rules of Decision 0036 §5 / §13
#: (``paw_backend.integration.git._COMMIT_CONFIG``). Accepted from the client
#: only in front of ``merge``, and applied by this file itself for ``merge``.
MERGE_CONFIG = (
    ("user.name", "Personal AI Workspace"),
    ("user.email", "integration@paw.invalid"),
    ("commit.gpgSign", "false"),
    ("merge.verifySignatures", "false"),
)

_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
_OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_PROTOCOL = re.compile(r"[a-z][a-z0-9+.-]{0,15}")
_CONFIG_KEY = re.compile(r"[A-Za-z][A-Za-z0-9-]*(\.[^\s=]+)*\.[A-Za-z][A-Za-z0-9-]*")


#: Settings git gets from the wrapper alone (the client neither sends nor needs
#: them). ``diff.ignoreSubmodules=all``: ``status`` in an agent's worktree would
#: otherwise start git inside a nested repository the agent planted (a gitlink
#: it committed), whose own configuration — a filter driver is a command —
#: nothing here checked. ``maintenance.auto=false``: ``merge`` would otherwise
#: leave a detached ``git maintenance`` running after the call.
OWN_HARDENING = (
    ("diff.ignoreSubmodules", "all"),
    ("maintenance.auto", "false"),
)


#: Sub-commands that read or write file content through the repository's
#: attributes, and so may start a command the repository's own configuration
#: names (a ``filter`` driver's ``clean`` / ``smudge`` / ``process``, a ``merge``
#: driver, a ``diff`` ``textconv``, ...): ``status`` (``clean`` on a modified
#: file), ``merge`` (``merge --abort`` too), ``merge-tree`` (a merge driver) and
#: ``worktree add`` (``smudge`` on checkout). Before any of them runs, the
#: configuration git would read is listed (:func:`configuration_probe`) and the
#: call is refused if it names a command, or another work tree
#: (:func:`unsafe_setting`).
CONTENT_SUBCOMMANDS = frozenset(
    {"status", "merge", "merge-tree", "worktree", "submodule"}
)

#: Configuration sections every key of which is (or leads to) a command, or to
#: another file this wrapper would not have listed.
#: ``gpg``: every signing setting (``gpg.program``, ``gpg.<format>.program``,
#: ``gpg.ssh.defaultKeyCommand``, ...) names or leads to a command.
_REFUSED_SECTIONS = frozenset(
    {"filter", "include", "includeif", "hook", "pager", "gpg"}
)
#: Two-part keys that name a command, or (``core.worktree``) a work tree other
#: than the one this wrapper checked: an unpinned ``status`` / ``merge`` in the
#: checkout would read and write the files there, outside the root.
_REFUSED_KEYS = frozenset(
    {
        "core.worktree",
        "core.pager",
        "core.editor",
        "core.askpass",
        "core.sshcommand",
        "core.gitproxy",
        "core.alternaterefscommand",
        "sequence.editor",
        "diff.external",
        "gpg.program",
        "uploadpack.packobjectshook",
    }
)
#: The last part of a ``<section>.<name>.<key>`` key that names a command
#: (``diff.<driver>.textconv``, ``merge.<driver>.driver``,
#: ``gpg.<format>.program``, ``remote.<name>.uploadpack``, ...).
_REFUSED_VARIABLES = frozenset(
    {
        "textconv",
        "command",
        "driver",
        "program",
        "cmd",
        "uploadpack",
        "receivepack",
        # ``branch.<name>.mergeOptions`` adds options to ``merge`` (``-S``
        # signs, whatever the command line's ``commit.gpgSign=false`` says).
        "mergeoptions",
    }
)
PROBE_TIMEOUT_S = 30
#: The most a check's git may print (a repository's whole configuration, or
#: its whole index): read as it comes, and refused beyond it, so that a huge
#: configuration planted in a repository cannot exhaust this process's memory.
PROBE_OUTPUT_LIMIT = 64 * 1024 * 1024


class ProbeOutputTooLarge(subprocess.SubprocessError):
    """A check's git printed more than :data:`PROBE_OUTPUT_LIMIT`."""


def bounded_run(
    argv: Sequence[str],
    *,
    cwd: str,
    env: Mapping[str, str],
    timeout: float,
    limit: int,
) -> "subprocess.CompletedProcess[bytes]":
    """Run a check's git, keeping at most ``limit`` bytes of its output and
    at most ``timeout`` seconds (``ProbeOutputTooLarge`` /
    ``TimeoutExpired`` beyond either; the process is killed)."""
    deadline = time.monotonic() + timeout
    chunks: list[bytes] = []
    size = 0
    with subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ) as process:
        assert process.stdout is not None
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(list(argv), timeout)
                    if not selector.select(remaining):
                        continue
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
                        raise ProbeOutputTooLarge()
                    chunks.append(chunk)
            returncode = process.wait(max(0.0, deadline - time.monotonic()))
        except BaseException:
            process.kill()
            raise
    return subprocess.CompletedProcess(list(argv), returncode, b"".join(chunks), b"")


class Rejected(Exception):
    """A refused call. ``reason`` is one of a fixed set of short codes: it is
    what is logged and printed, never anything the client sent."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# -- configuration (the fixed options of the ``command=`` line) --------------------


@dataclass(frozen=True)
class Config:
    """The wrapper's own settings, from its ``command=`` line only.

    ``root``: the directory every path must stay in (default
    ``<home>/workspaces``). ``home``: git's ``HOME`` (default: this Linux
    user's home in the account database). ``git``: the git executable.
    ``protocols``: the transports git may use (``https``). ``extra``: further
    ``key=value`` pairs
    git always gets and the client may send (a deployment's own, fixed values).
    ``gh``: the ``gh`` executable whose credential helper ``clone`` may name
    (PAW-028); ``None`` refuses any credential helper.
    """

    root: str
    home: str
    user: str
    git: str = "/usr/bin/git"
    protocols: tuple[str, ...] = ("https",)
    extra: tuple[tuple[str, str], ...] = ()
    gh: str | None = None
    path: str = SAFE_PATH


def _canonical(value: str) -> bool:
    return (
        value.startswith("/")
        and not value.startswith("//")
        and len(value) <= MAX_PATH_CHARS
        and os.path.normpath(value) == value
        and value.isprintable()
    )


def parse_config(argv: Sequence[str], *, uid: int | None = None) -> Config:
    """The :class:`Config` of the wrapper's own arguments, or ``Rejected``
    (``misconfigured``): a typo on the ``command=`` line refuses every call
    rather than falling back to something looser."""
    account = pwd.getpwuid(os.geteuid() if uid is None else uid)
    options: dict[str, object] = {"home": account.pw_dir, "user": account.pw_name}
    protocols: list[str] = []
    extra: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in argv:
        name, sep, value = item.partition("=")
        if not sep or not value:
            raise Rejected("misconfigured")
        if name in ("--root", "--home", "--git", "--gh", "--path"):
            parts = value.split(":") if name == "--path" else [value]
            if name in seen or not all(_canonical(part) for part in parts):
                raise Rejected("misconfigured")
            seen.add(name)
            options[name[2:]] = value
        elif name == "--allow-protocol":
            if _PROTOCOL.fullmatch(value) is None:
                raise Rejected("misconfigured")
            protocols.append(value)
        elif name == "--config":
            key, sep, setting = value.partition("=")
            if not sep or _CONFIG_KEY.fullmatch(key) is None:
                raise Rejected("misconfigured")
            extra.append((key, setting))
        else:
            raise Rejected("misconfigured")
    home = str(options["home"])
    root = str(options.get("root", f"{home.rstrip('/')}/workspaces"))
    if not _canonical(home) or not _canonical(root) or root == "/":
        raise Rejected("misconfigured")
    return Config(
        root=root,
        home=home,
        user=str(options["user"]),
        git=str(options.get("git", "/usr/bin/git")),
        protocols=tuple(dict.fromkeys(protocols)) or ("https",),
        extra=tuple(extra),
        gh=None if options.get("gh") is None else str(options["gh"]),
        path=str(options.get("path", SAFE_PATH)),
    )


def hardening(config: Config) -> list[tuple[str, str]]:
    """What the client must send in front of the sub-command, and git always
    gets whatever it sent (the rules of
    ``paw_backend.repositories.git.git_config_arguments``); git also gets
    :data:`OWN_HARDENING`."""
    pairs = [
        ("core.hooksPath", "/dev/null"),
        ("core.fsmonitor", "false"),
        ("submodule.recurse", "false"),
        ("protocol.allow", "never"),
    ]
    pairs.extend((f"protocol.{name}.allow", "always") for name in config.protocols)
    pairs.extend(config.extra)
    return pairs


def git_environment(config: Config, ceiling: str | None) -> dict[str, str]:
    """git's whole environment: a fixed allowlist (``git.py``'s
    ``git_environment``), nothing inherited from ``sshd``."""
    environment = {
        "PATH": config.path,
        "HOME": config.home,
        "LC_ALL": "C",
        "LANG": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_ATTR_NOSYSTEM": "1",
        # A partial clone would otherwise fetch a missing object on demand from
        # its promisor remote, from inside ``status`` or ``worktree add``: a
        # network call (and the repository's credential helper, a command) no
        # allowed sub-command other than ``clone`` needs.
        "GIT_NO_LAZY_FETCH": "1",
    }
    if ceiling is not None:
        environment["GIT_CEILING_DIRECTORIES"] = ceiling
    return environment


# -- paths --------------------------------------------------------------------------


def resolve(path: str) -> str:
    """``path`` with every symbolic link resolved; a part that does not exist
    yet is kept as written. A dangling link or a loop is ``Rejected``."""
    missing: list[str] = []
    probe = path
    while not os.path.lexists(probe):
        probe, name = os.path.split(probe)
        missing.insert(0, name)
    try:
        real = os.path.realpath(probe, strict=True)
    except (OSError, RuntimeError):
        raise Rejected("path_unresolvable") from None
    return os.path.join(real, *missing) if missing else real


def _within(path: str, base: str, *, allow_equal: bool) -> bool:
    if path == base:
        return allow_equal
    return path.startswith(base.rstrip("/") + "/")


@dataclass(frozen=True)
class Places:
    """The root and the worktree area of one call, as configured (``*_text``)
    and with every symbolic link resolved."""

    root_text: str
    root: str
    worktrees: str

    @classmethod
    def of(cls, config: Config) -> "Places":
        if not os.path.isdir(config.root):
            raise Rejected("root_unavailable")
        root = resolve(config.root)
        worktrees = resolve(f"{config.root}/{WORKTREE_DIRECTORY}")
        if worktrees != f"{root.rstrip('/')}/{WORKTREE_DIRECTORY}":
            # ``.paw-worktrees`` is itself a link: it could make a checkout (or
            # anything else) count as a worktree. Not followed, whatever it is.
            raise Rejected("bad_worktrees")
        return cls(config.root, root, worktrees)

    def check(
        self,
        path: str,
        *,
        worktree: bool | None,
        allow_root: bool = False,
        must_exist: bool = False,
    ) -> str:
        """``path`` resolved, when it is canonical and stays in the root both as
        written and once resolved. ``worktree=True``: strictly inside
        ``.paw-worktrees``; ``False``: never inside it; ``None``: either.

        "As written" accepts either spelling of the root (the configured one,
        usually ``<home>/workspaces`` as the account database spells the home,
        or the resolved one); "once resolved" is what decides, so a symbolic
        link anywhere on the way that leads out of the root is refused."""
        if not _canonical(path):
            raise Rejected("bad_path")
        if not any(
            _within(path, base, allow_equal=allow_root)
            for base in (self.root_text, self.root)
        ):
            raise Rejected("path_outside_root")
        real = resolve(path)
        if must_exist and not os.path.exists(real):
            raise Rejected("bad_path")
        if not _within(real, self.root, allow_equal=allow_root):
            raise Rejected("path_outside_root")
        in_worktrees = _within(real, self.worktrees, allow_equal=True)
        if worktree is True and not _within(real, self.worktrees, allow_equal=False):
            raise Rejected("path_outside_worktrees")
        if worktree is False and in_worktrees:
            raise Rejected("path_in_worktrees")
        return real


# -- argument shapes ----------------------------------------------------------------


def _branch(value: str) -> bool:
    if _BRANCH.fullmatch(value) is None or ".." in value or value.endswith(("/", ".")):
        return False
    return all(
        part and not part.startswith(".") and not part.endswith(".lock")
        for part in value.split("/")
    )


def _paw_branch(value: str) -> bool:
    return _branch(value) and value.startswith(f"{BRANCH_NAMESPACE}/")


def _paw_ref(value: str) -> bool:
    return value.startswith("refs/heads/") and _paw_branch(
        value.removeprefix("refs/heads/")
    )


def _commit_revision(value: str) -> bool:
    """``<rev>^{commit}`` for the revisions the backend asks about."""
    if not value.endswith("^{commit}"):
        return False
    revision = value.removesuffix("^{commit}")
    if revision in ("HEAD", "MERGE_HEAD"):
        return True
    for prefix in ("refs/heads/", "refs/remotes/origin/"):
        if revision.startswith(prefix):
            return _branch(revision.removeprefix(prefix))
    return False


def _url(value: str, config: Config) -> bool:
    return (
        len(value) <= MAX_URL_CHARS
        and value.isascii()
        and value.isprintable()
        and " " not in value
        and any(value.startswith(f"{name}://") for name in config.protocols)
    )


@dataclass
class Call:
    """What one accepted call runs: the sub-command, its (checked) arguments,
    and whether it may carry ``--git-dir=`` / ``--work-tree=``."""

    subcommand: str
    args: list[str]
    pinnable: bool = False
    merge: bool = False
    extra: list[str] = field(default_factory=list)


def _exactly(args: Sequence[str], *shapes: Sequence[str]) -> bool:
    return any(list(args) == list(shape) for shape in shapes)


def _check_rev_parse(args: list[str], places: Places, config: Config) -> None:
    if _exactly(
        args,
        ["--is-bare-repository"],
        ["--show-toplevel", "--absolute-git-dir"],
        ["--show-toplevel"],
        ["--path-format=absolute", "--git-dir"],
        ["--path-format=absolute", "--git-common-dir"],
    ):
        return
    if len(args) == 3 and args[:2] == ["--verify", "--quiet"]:
        if _commit_revision(args[2]):
            return
    raise Rejected("bad_arguments")


def _check_symbolic_ref(args: list[str], places: Places, config: Config) -> None:
    if not _exactly(
        args,
        ["--quiet", "--short", "HEAD"],
        ["--quiet", "--short", "refs/remotes/origin/HEAD"],
        ["--quiet", "HEAD"],
    ):
        raise Rejected("bad_arguments")


def _check_config(args: list[str], places: Places, config: Config) -> None:
    if not _exactly(args, ["--local", "--get", "remote.origin.url"]):
        raise Rejected("bad_arguments")


def _check_clone(args: list[str], places: Places, config: Config) -> None:
    rest = list(args)
    if rest[:1] != ["--quiet"]:
        raise Rejected("bad_arguments")
    rest = rest[1:]
    if rest[:1] == ["-c"]:
        helper = f"credential.helper=!{config.gh} auth git-credential"
        if config.gh is None or rest[1:2] != [helper]:
            raise Rejected("config_not_allowed")
        rest = rest[2:]
    if rest[:1] == ["--branch"]:
        if len(rest) < 2 or not _branch(rest[1]):
            raise Rejected("bad_arguments")
        rest = rest[2:]
    if len(rest) != 3 or rest[0] != "--" or not _url(rest[1], config):
        raise Rejected("bad_arguments")
    places.check(rest[2], worktree=False)


def _check_init(args: list[str], places: Places, config: Config) -> None:
    if (
        len(args) != 5
        or args[:2] != ["--quiet", "--template="]
        or not args[2].startswith("--initial-branch=")
        or not _branch(args[2].removeprefix("--initial-branch="))
        or args[3] != "--"
    ):
        raise Rejected("bad_arguments")
    places.check(args[4], worktree=False)


def _check_remote(args: list[str], places: Places, config: Config) -> None:
    if len(args) != 4 or args[:3] != ["add", "--", "origin"]:
        raise Rejected("bad_arguments")
    if not _url(args[3], config):
        raise Rejected("bad_arguments")


def _check_worktree(args: list[str], places: Places, config: Config) -> None:
    if _exactly(args, ["list", "--porcelain", "-z"], ["prune"]):
        return
    if len(args) == 7 and args[:3] == ["add", "--quiet", "-b"] and args[4] == "--":
        # add --quiet -b paw/<...> -- <path> <commit id>
        if _paw_branch(args[3]) and _OBJECT_ID.fullmatch(args[6]):
            places.check(args[5], worktree=True)
            return
    if len(args) == 5 and args[:3] == ["add", "--quiet", "--"]:
        # add --quiet -- <path> paw/<...> (the worktree was removed by hand)
        if _paw_branch(args[4]):
            places.check(args[3], worktree=True)
            return
    raise Rejected("bad_arguments")


def _check_merge(args: list[str], places: Places, config: Config) -> None:
    if _exactly(args, ["--abort"]):
        return
    if (
        len(args) == 6
        and args[:4] == ["--no-ff", "--no-edit", "--quiet", "-m"]
        and _paw_ref(args[5])
        and args[4] == f"Integrate {args[5].removeprefix('refs/heads/')}"
    ):
        return
    raise Rejected("bad_arguments")


def _check_merge_tree(args: list[str], places: Places, config: Config) -> None:
    if (
        len(args) == 6
        and args[:4] == ["--write-tree", "--name-only", "-z", "--no-messages"]
        and _paw_ref(args[4])
        and _paw_ref(args[5])
    ):
        return
    raise Rejected("bad_arguments")


def _check_merge_base(args: list[str], places: Places, config: Config) -> None:
    if len(args) == 3 and args[0] == "--is-ancestor":
        if _paw_ref(args[1]) and _paw_ref(args[2]):
            return
    raise Rejected("bad_arguments")


#: The ``status`` of Decision 0051 (PR #130): whether an integration worktree
#: is exactly its commit, ignored files and changes inside submodules included.
#: Only pinned; ``--ignore-submodules=none`` overrides the wrapper's own
#: ``diff.ignoreSubmodules=all``, so git would start a child git inside every
#: populated submodule, whose configuration nothing here checked: such a call
#: is refused when any is populated (:func:`check_submodules`).
STATUS_WITH_SUBMODULES = [
    "--porcelain=v1",
    "-z",
    "--untracked-files=normal",
    "--ignored=traditional",
    "--ignore-submodules=none",
]


#: The ``submodule`` of Decision 0051 (PR #130), run before its ``status``:
#: which submodules the integration worktree has (any makes it not exactly its
#: commit). Only this form, only pinned; ``submodule`` runs ``git describe``
#: inside each populated one, so it is refused like the ``status`` above when
#: any is populated.
SUBMODULE_STATUS = ["status", "--cached"]


def _check_submodule(args: list[str], places: Places, config: Config) -> None:
    if not _exactly(args, SUBMODULE_STATUS):
        raise Rejected("bad_arguments")


def _check_status(args: list[str], places: Places, config: Config) -> None:
    if not _exactly(
        args, ["--porcelain=v1", "-z", "--untracked-files=all"], STATUS_WITH_SUBMODULES
    ):
        raise Rejected("bad_arguments")


Checker = Callable[[list[str], Places, Config], None]

#: The allowlist: Decision 0029 §3 (``rev-parse``, ``symbolic-ref``, ``config``,
#: ``clone``, ``init``, ``remote``) and Decision 0036 §13 (``worktree``,
#: ``merge``, ``merge-tree``, ``merge-base``, ``status`` and the added
#: ``rev-parse`` / ``symbolic-ref`` shapes), and Decision 0051 (PR #130: the
#: second ``status`` form and ``submodule status --cached``, pinned only; no
#: other ``submodule`` sub-command or option). ``True``: the sub-command may run
#: pinned to a worktree (``--git-dir=`` / ``--work-tree=``, Decision 0036 §13).
#: Anything not here — ``push``, ``fetch``, ``pull``, ``checkout``, ``switch``,
#: ``reset``, ``rebase``, ``gc``, ``config`` writes, ... — is refused.
SUBCOMMANDS: Mapping[str, tuple[Checker, bool]] = {
    "rev-parse": (_check_rev_parse, True),
    "symbolic-ref": (_check_symbolic_ref, True),
    "config": (_check_config, False),
    "clone": (_check_clone, False),
    "init": (_check_init, False),
    "remote": (_check_remote, False),
    "worktree": (_check_worktree, False),
    "merge": (_check_merge, True),
    "merge-tree": (_check_merge_tree, False),
    "merge-base": (_check_merge_base, False),
    "status": (_check_status, True),
    "submodule": (_check_submodule, True),
}


# -- the whole call -----------------------------------------------------------------


@dataclass(frozen=True)
class Invocation:
    """What an accepted call ``exec``-s: argv, environment and directory."""

    subcommand: str
    argv: list[str]
    env: dict[str, str]
    cwd: str
    #: The ``git config`` call whose output must name no command before
    #: ``argv`` runs (:data:`CONTENT_SUBCOMMANDS`); ``None`` for the others.
    probe: list[str] | None = None
    #: For a call not pinned to a git directory (``clone`` aside): the
    #: ``git rev-parse`` call that says which git directory and common
    #: directory git finds from the cwd; both must be inside ``root`` and
    #: outside ``worktrees`` (:func:`check_repository`).
    locate: list[str] | None = None
    root: str = ""
    worktrees: str = ""
    #: For the ``status`` of :data:`STATUS_WITH_SUBMODULES`: the ``git
    #: ls-files`` call that lists the index, whose submodules (gitlinks) must
    #: all be unpopulated (:func:`check_submodules`).
    gitlinks: list[str] | None = None
    #: The git directory and common directory of a pinned call (checked in
    #: :func:`plan`), which must hold no symbolic link (:func:`check_links`).
    metadata: tuple[str, ...] = ()


def _split(original: str | None) -> list[str]:
    if original is None or original == "":
        raise Rejected("no_command")  # an interactive login or a bare `ssh host`
    if len(original.encode("utf-8", "surrogateescape")) > MAX_COMMAND_BYTES:
        raise Rejected("too_long")
    try:
        return shlex.split(original, comments=False, posix=True)
    except ValueError:
        raise Rejected("bad_encoding") from None


def plan(original: str | None, config: Config) -> Invocation:
    """The :class:`Invocation` of ``$SSH_ORIGINAL_COMMAND``, or ``Rejected``."""
    words = _split(original)
    if len(words) < 5 or words[0] != PROTOCOL_TAG:
        raise Rejected("bad_protocol")
    _, cwd_text, ceiling_text, separator, *rest = words
    if separator != "--":
        raise Rejected("bad_protocol")
    places = Places.of(config)
    cwd = places.check(cwd_text, worktree=None, allow_root=True, must_exist=True)
    if not os.path.isdir(cwd):
        raise Rejected("bad_path")
    # Repository discovery never leaves the root, whatever the client sent: the
    # parent of the (resolved) root is always a ceiling, so a repository above
    # it (a dotfiles checkout in the home, say, whose configuration nothing
    # here checked) is never found from a cwd in the root that is not one.
    bound = os.path.dirname(places.root)
    if ceiling_text == "-":
        ceiling = bound
    elif ceiling_text == os.path.dirname(cwd_text):
        ceiling = os.path.dirname(cwd)
        if ceiling != bound:
            ceiling = f"{ceiling}:{bound}"
    else:
        raise Rejected("bad_ceiling")

    sent_config: list[str] = []
    git_dir: str | None = None
    work_tree: str | None = None
    index = 0
    while index < len(rest):
        word = rest[index]
        if word == "-c":
            if index + 1 >= len(rest):
                raise Rejected("bad_option")
            sent_config.append(rest[index + 1])
            index += 2
        elif word.startswith("--git-dir="):
            if git_dir is not None:
                raise Rejected("bad_option")
            git_dir = word.removeprefix("--git-dir=")
            index += 1
        elif word.startswith("--work-tree="):
            if work_tree is not None:
                raise Rejected("bad_option")
            work_tree = word.removeprefix("--work-tree=")
            index += 1
        elif word.startswith("-"):
            raise Rejected("bad_option")
        else:
            break
    if index >= len(rest):
        raise Rejected("no_subcommand")
    subcommand, args = rest[index], list(rest[index + 1 :])
    if subcommand not in SUBCOMMANDS:
        raise Rejected("subcommand_not_allowed")
    checker, pinnable = SUBCOMMANDS[subcommand]

    allowed = {f"{key}={value}" for key, value in hardening(config)}
    if subcommand == "merge":
        allowed |= {f"{key}={value}" for key, value in MERGE_CONFIG}
    for pair in sent_config:
        if pair not in allowed:
            raise Rejected("config_not_allowed")

    pinned: list[str] = []
    metadata: list[str] = []
    if git_dir is not None or work_tree is not None:
        if git_dir is None or work_tree is None or not pinnable:
            raise Rejected("bad_option")
        real_dir = places.check(git_dir, worktree=False, must_exist=True)
        real_tree = places.check(work_tree, worktree=True, must_exist=True)
        parent = os.path.basename(os.path.dirname(real_dir))
        if parent != "worktrees" or not os.path.isdir(real_dir):
            raise Rejected("bad_git_dir")
        _check_common_dir(real_dir)
        if not os.path.isdir(real_tree) or real_tree != cwd:
            raise Rejected("bad_work_tree")
        _check_backlink(real_dir, real_tree)
        metadata = [real_dir, os.path.dirname(os.path.dirname(real_dir))]
        pinned = [f"--git-dir={real_dir}", f"--work-tree={real_tree}"]
    elif subcommand != "rev-parse" and _within(cwd, places.worktrees, allow_equal=True):
        # A worker's worktree is written by an agent: its ``.git`` may name a
        # configuration of the agent's (a filter driver is a command). Only
        # ``rev-parse``, which runs none, may read it; everything else there runs
        # pinned to the worktree's git directory in the checkout (Decision 0036).
        raise Rejected("unpinned_worktree")
    checker(args, places, config)
    if subcommand == "init" and places.check(args[4], worktree=False) != cwd:
        # ``init`` runs where it creates the repository, so the check of the
        # repository found from the cwd (:func:`check_repository`) is the
        # check of the ``.git`` it would write to.
        raise Rejected("bad_arguments")
    if subcommand == "init" and os.path.lexists(os.path.join(cwd, ".git")):
        # ``init`` creates a repository (the backend's is an empty directory);
        # re-initialising an existing ``.git`` would write through whatever
        # is planted there, even one git cannot read as a repository (so that
        # :func:`check_repository` finds nothing to check).
        raise Rejected("init_existing")

    argv = [config.git]
    for key, value in (*hardening(config), *OWN_HARDENING):
        argv.extend(("-c", f"{key}={value}"))
    if subcommand != "clone":
        # An empty value empties the list of credential helpers read so far
        # (the repository's own included): only ``clone`` talks to a remote,
        # with the helper its own ``-c`` names.
        argv.extend(("-c", "credential.helper="))
    # The checks before the call run git too, with the same hardening (a
    # repository's ``core.fsmonitor`` is a command, which only the hardening's
    # ``core.fsmonitor=false`` keeps from running).
    hardened = list(argv) if subcommand != "clone" else []
    if subcommand == "merge":
        for key, value in MERGE_CONFIG:
            argv.extend(("-c", f"{key}={value}"))
    argv.extend(pinned)
    argv.append(subcommand)
    argv.extend(args)
    probe = None
    if subcommand in CONTENT_SUBCOMMANDS and args[:1] not in (["list"], ["prune"]):
        probe = configuration_probe(hardened, pinned)
    gitlinks = None
    if (subcommand, args) in (
        ("status", STATUS_WITH_SUBMODULES),
        ("submodule", SUBMODULE_STATUS),
    ):
        if not pinned:
            raise Rejected("bad_arguments")
        gitlinks = [*hardened, *pinned, "ls-files", "--stage", "-z"]
    locate = None
    if not pinned and subcommand != "clone":
        locate = [
            *hardened,
            "rev-parse",
            "--path-format=absolute",
            "--git-dir",
            "--git-common-dir",
        ]
    return Invocation(
        subcommand,
        argv,
        git_environment(config, ceiling),
        cwd,
        probe,
        locate,
        places.root,
        places.worktrees,
        gitlinks,
        tuple(metadata),
    )


def configuration_probe(hardened: Sequence[str], pinned: Sequence[str]) -> list[str]:
    """The ``git config`` call that lists every setting the call itself would
    read (the repository's, the worktree's ``config.worktree``; the system and
    global ones are off, :func:`git_environment`), each with its scope, without
    following an ``include`` (an ``include`` is itself refused). It starts no
    command. ``hardened``: git and the wrapper's own ``-c``, whose (``command``
    scope) settings are not the repository's and are not checked."""
    return [
        *hardened,
        *pinned,
        "config",
        "--no-includes",
        "--show-scope",
        "--list",
        "-z",
    ]


def unsafe_setting(key: str) -> bool:
    """Whether the configuration ``key`` names a command git may start, another
    file of settings (``include``) or another work tree (``core.worktree``).
    The hardening's own keys (``core.hooksPath``, ``core.fsmonitor``) are not
    here: the wrapper's ``-c`` overrides them."""
    key = key.lower()
    section, _, rest = key.partition(".")
    variable = key.rpartition(".")[2]
    if section in _REFUSED_SECTIONS or key in _REFUSED_KEYS:
        return True
    return "." in rest and variable in _REFUSED_VARIABLES


def check_repository(
    invocation: Invocation,
    run: Callable[..., "subprocess.CompletedProcess[bytes]"] = bounded_run,
) -> None:
    """What must hold of the repository itself before ``invocation`` runs:
    where its git directory is (:func:`check_location`), and what its
    configuration names (:func:`check_configuration`)."""
    found = check_location(invocation, run)
    check_links([*invocation.metadata, *found])
    check_configuration(invocation, run)
    check_submodules(invocation, run)


#: Directories of a git directory that git reads but never writes, where a
#: symbolic link is left alone (hooks never run here: ``core.hooksPath``).
_UNWRITTEN = frozenset({"hooks"})
#: ``objects/info/`` files that name other object directories.
_ALTERNATES = frozenset({"alternates", "http-alternates"})


def check_links(directories: Sequence[str]) -> None:
    """Refuse the call when any of ``directories`` (a git directory, a common
    directory) holds a symbolic link, a file with another hard link
    (``objects/`` aside) or an ``objects/info/alternates`` (``git_dir_alternates``),
    ``hooks/`` aside: git creates files under
    ``refs/``, ``logs/``, ``objects/``, ``worktrees/``, ... and would follow a
    linked directory there out of the root, and writes ``MERGE_MSG``,
    ``config``, ... whose other link may be outside it, although the
    directory itself was checked to be inside it."""
    todo = sorted(set(directories))
    for index, top in enumerate(todo):
        if any(_within(top, other, allow_equal=False) for other in todo[:index]):
            continue  # already walked with the directory it is in
        if os.path.islink(top):
            raise Rejected("git_dir_link")

        def fail(error: OSError) -> None:
            raise Rejected("git_dir_link")

        for path, dirs, files in os.walk(top, onerror=fail, followlinks=False):
            if path == top:
                dirs[:] = [name for name in dirs if name not in _UNWRITTEN]
            # Object files are never written once there (a new object is a
            # new file): a hard link among them (``clone --local`` and
            # ``submodule add`` of a local path make them), in the
            # repository's ``objects/`` or a submodule's under ``modules/``
            # (or ``worktrees/<name>/modules/``), is left alone. Any other
            # file git may rewrite in place.
            parts = os.path.relpath(path, top).split(os.sep)
            if parts[:1] == ["worktrees"] and parts[2:3] == ["modules"]:
                parts = parts[2:]
            linked_ok = parts[0] == "objects" or (
                parts[0] == "modules" and "objects" in parts[1:]
            )
            if parts[-2:] == ["objects", "info"] and _ALTERNATES & set(files):
                # Another object directory (anywhere: another user's
                # repository, say) whose objects git would read, and a
                # ``worktree add`` of one of its commits would check out.
                raise Rejected("git_dir_alternates")
            for name in (*dirs, *files):
                try:
                    info = os.lstat(os.path.join(path, name))
                except OSError:
                    raise Rejected("git_dir_link") from None
                if stat.S_ISLNK(info.st_mode):
                    raise Rejected("git_dir_link")
                if stat.S_ISREG(info.st_mode) and info.st_nlink > 1 and not linked_ok:
                    raise Rejected("git_dir_link")


def check_submodules(
    invocation: Invocation,
    run: Callable[..., "subprocess.CompletedProcess[bytes]"] = bounded_run,
) -> None:
    """Refuse the ``status`` of :data:`STATUS_WITH_SUBMODULES` (or the
    ``submodule`` of :data:`SUBMODULE_STATUS`) when a submodule (a gitlink in
    the index) is populated (has a ``.git`` in the work tree): git would run a
    child git there, with that repository's own
    configuration (a ``filter`` is a command), which an agent may have
    planted. ``ls-files`` reads only the index and starts no command."""
    if invocation.gitlinks is None:
        return
    listed = _probe(invocation, invocation.gitlinks, run, "probe_failed")
    if listed.returncode != 0:
        raise Rejected("probe_failed")
    for entry in listed.stdout.split(b"\0"):
        if not entry.startswith(b"160000 "):
            continue
        path = entry.partition(b"\t")[2].decode("utf-8", "surrogateescape")
        if not path or os.path.lexists(os.path.join(invocation.cwd, path, ".git")):
            raise Rejected("populated_submodule")


def _probe(
    invocation: Invocation, argv: list[str], run: Callable[..., object], reason: str
) -> "subprocess.CompletedProcess[bytes]":
    try:
        result = run(
            argv,
            cwd=invocation.cwd,
            env=invocation.env,
            timeout=PROBE_TIMEOUT_S,
            limit=PROBE_OUTPUT_LIMIT,
        )
    except (OSError, subprocess.SubprocessError):
        raise Rejected(reason) from None
    return result  # type: ignore[return-value]


def check_location(
    invocation: Invocation,
    run: Callable[..., "subprocess.CompletedProcess[bytes]"] = bounded_run,
) -> list[str]:
    """Refuse an unpinned ``invocation`` whose git directory or common
    directory, as git itself finds them from the cwd (a ``.git`` directory, a
    ``gitdir:`` file, a link, a ``commondir``), is outside the root or inside
    ``.paw-worktrees`` (where an agent writes). Checking the cwd alone would
    let a ``.git`` in the root lead git to a repository outside it.

    A cwd in no repository at all is left to git (``rev-parse
    --is-bare-repository`` answers 128 there, which the backend reads).
    Returns the two directories, resolved (none when not located)."""
    if invocation.locate is None:
        return []
    found = _probe(invocation, invocation.locate, run, "probe_failed")
    if found.returncode != 0:
        return []
    try:
        lines = found.stdout.decode("utf-8").split("\n")
    except UnicodeDecodeError:
        raise Rejected("git_dir_outside_root") from None
    if len(lines) != 3 or lines[2] != "" or not all(lines[:2]):
        raise Rejected("git_dir_outside_root")
    places = []
    for place in lines[:2]:
        try:
            real = os.path.realpath(place, strict=True)
        except (OSError, RuntimeError):
            raise Rejected("git_dir_outside_root") from None
        if not _within(real, invocation.root, allow_equal=False) or _within(
            real, invocation.worktrees, allow_equal=True
        ):
            raise Rejected("git_dir_outside_root")
        places.append(real)
    return places


def check_configuration(
    invocation: Invocation,
    run: Callable[..., "subprocess.CompletedProcess[bytes]"] = bounded_run,
) -> None:
    """Refuse ``invocation`` when the configuration it would read names a
    command (:data:`CONTENT_SUBCOMMANDS`), or cannot be listed.

    The repository's configuration is shared by the checkout and every worktree
    of it, and an agent working in a worktree may write it: a ``filter`` there,
    selected by a ``.gitattributes`` the agent committed, would otherwise run
    as this user during an allowed ``status``. Only something that can already
    write that file (and so run git itself there) could change it between this
    check and the ``exec``."""
    if invocation.probe is None:
        return
    listed = _probe(invocation, invocation.probe, run, "config_unreadable")
    if listed.returncode != 0:
        raise Rejected("config_unreadable")
    words = listed.stdout.split(b"\0")
    if words[-1:] == [b""]:
        words.pop()
    if len(words) % 2:
        raise Rejected("config_unreadable")
    for scope, entry in zip(words[::2], words[1::2], strict=True):
        if scope == b"command":
            continue  # the wrapper's own ``-c`` (nothing else sets it here)
        key = entry.partition(b"\n")[0].decode("utf-8", "replace")
        if unsafe_setting(key):
            raise Rejected("config_unsafe")


def _check_backlink(git_dir: str, work_tree: str) -> None:
    """The ``gitdir`` file of the worktree directory ``git_dir`` (git's record
    of which work tree it belongs to) must name ``work_tree``'s ``.git``, once
    every symbolic link is resolved: another repository's worktree directory
    must never be paired with this work tree (its index and ``HEAD`` would be
    applied to these files)."""
    text = _read_small_file(git_dir, "gitdir")
    target = text if text.startswith("/") else os.path.join(git_dir, text)
    if os.path.basename(target) != ".git":
        raise Rejected("bad_git_dir")
    try:
        real = os.path.realpath(os.path.dirname(target), strict=True)
    except (OSError, RuntimeError):
        raise Rejected("bad_git_dir") from None
    if real != work_tree:
        raise Rejected("bad_git_dir")


def _check_common_dir(git_dir: str) -> None:
    """The ``commondir`` file of the worktree directory ``git_dir`` (git reads
    the repository's objects, refs and configuration from where it points) must
    lead back to the ``.git`` it sits in (``<.git>/worktrees/<name>``), once
    every symbolic link is resolved; anything else — missing, not a regular
    file, a link, another repository — is refused."""
    common = os.path.dirname(os.path.dirname(git_dir))
    text = _read_small_file(git_dir, "commondir")
    target = text if text.startswith("/") else os.path.join(git_dir, text)
    try:
        real = os.path.realpath(target, strict=True)
    except (OSError, RuntimeError):
        raise Rejected("bad_git_dir") from None
    if real != common:
        raise Rejected("bad_git_dir")


def _read_small_file(git_dir: str, name: str) -> str:
    """The one-line path file ``name`` of ``git_dir`` (a regular file, not a
    link), or ``Rejected`` (``bad_git_dir``)."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(os.path.join(git_dir, name), flags)
    except OSError:
        raise Rejected("bad_git_dir") from None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise Rejected("bad_git_dir")
        data = os.read(descriptor, MAX_PATH_CHARS + 2)
    except OSError:
        raise Rejected("bad_git_dir") from None
    finally:
        os.close(descriptor)
    try:
        text = data.decode("utf-8").removesuffix("\n")
    except UnicodeDecodeError:
        raise Rejected("bad_git_dir") from None
    if not text or len(text) > MAX_PATH_CHARS or not text.isprintable():
        raise Rejected("bad_git_dir")
    return text


def _syslog(message: str) -> None:
    try:
        syslog.openlog("paw-git-wrapper", syslog.LOG_PID, syslog.LOG_AUTH)
        syslog.syslog(syslog.LOG_INFO, message)
    except OSError:  # no syslog: the call is still decided the same way
        pass


def main(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
    *,
    execve: Callable[[str, list[str], dict[str, str]], object] = os.execve,
    chdir: Callable[[str], object] = os.chdir,
    log: Callable[[str], None] = _syslog,
    check: Callable[[Invocation], None] = check_repository,
) -> int:
    """Decide one call; ``exec`` git or return :data:`REJECTED`."""
    arguments = sys.argv[1:] if argv is None else list(argv)
    environment = os.environ if environ is None else environ
    user = "-"
    try:
        config = parse_config(arguments)
        user = config.user
        invocation = plan(environment.get("SSH_ORIGINAL_COMMAND"), config)
        check(invocation)
    except Rejected as rejected:
        log(f"rejected user={user} reason={rejected.reason}")
        sys.stderr.write(f"paw-git-wrapper: rejected ({rejected.reason})\n")
        return REJECTED
    log(f"accepted user={user} subcommand={invocation.subcommand}")
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        chdir(invocation.cwd)
        execve(invocation.argv[0], invocation.argv, invocation.env)
    except OSError:
        log(f"failed user={user} subcommand={invocation.subcommand} reason=exec")
        sys.stderr.write("paw-git-wrapper: git could not be started\n")
        return REJECTED
    return 0  # only reached when ``execve`` is a test double


if __name__ == "__main__":
    sys.exit(main())
