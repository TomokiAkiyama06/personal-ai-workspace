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
* ``ceiling`` must be ``-`` or exactly the parent directory of ``cwd``;
* global options may only be ``-c <key>=<value>`` whose pair is **in a fixed
  list** (the hardening the client always sends, and — for ``merge`` only — the
  fixed merge identity of Decision 0036), and ``--git-dir=`` / ``--work-tree=``
  (Decision 0036 §13, with the human's condition of 2026-09-28): both or
  neither, only for the sub-commands that run inside a worktree, the work tree
  inside ``<root>/.paw-worktrees`` and the git directory a ``worktrees/<name>``
  directory inside the root but outside ``.paw-worktrees``, each checked after
  resolving every symbolic link;
* the sub-command and its arguments must match one of the fixed shapes of
  Decision 0029 §3 and Decision 0036 §13 (:data:`SUBCOMMANDS`) exactly.

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
import shlex
import sys
import syslog
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
    """What git always gets in front of the sub-command (the same rules as
    ``paw_backend.repositories.git.git_config_arguments``), whatever the client
    sent."""
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


def _check_status(args: list[str], places: Places, config: Config) -> None:
    if not _exactly(args, ["--porcelain=v1", "-z", "--untracked-files=all"]):
        raise Rejected("bad_arguments")


Checker = Callable[[list[str], Places, Config], None]

#: The allowlist: Decision 0029 §3 (``rev-parse``, ``symbolic-ref``, ``config``,
#: ``clone``, ``init``, ``remote``) and Decision 0036 §13 (``worktree``,
#: ``merge``, ``merge-tree``, ``merge-base``, ``status`` and the added
#: ``rev-parse`` / ``symbolic-ref`` shapes). ``True``: the sub-command may run
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
}


# -- the whole call -----------------------------------------------------------------


@dataclass(frozen=True)
class Invocation:
    """What an accepted call ``exec``-s: argv, environment and directory."""

    subcommand: str
    argv: list[str]
    env: dict[str, str]
    cwd: str


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
    if ceiling_text == "-":
        ceiling = None
    elif ceiling_text == os.path.dirname(cwd_text):
        ceiling = os.path.dirname(cwd)
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
    if git_dir is not None or work_tree is not None:
        if git_dir is None or work_tree is None or not pinnable:
            raise Rejected("bad_option")
        real_dir = places.check(git_dir, worktree=False, must_exist=True)
        real_tree = places.check(work_tree, worktree=True, must_exist=True)
        parent = os.path.basename(os.path.dirname(real_dir))
        if parent != "worktrees" or not os.path.isdir(real_dir):
            raise Rejected("bad_git_dir")
        if not os.path.isdir(real_tree) or real_tree != cwd:
            raise Rejected("bad_work_tree")
        pinned = [f"--git-dir={real_dir}", f"--work-tree={real_tree}"]
    elif subcommand != "rev-parse" and _within(cwd, places.worktrees, allow_equal=True):
        # A worker's worktree is written by an agent: its ``.git`` may name a
        # configuration of the agent's (a filter driver is a command). Only
        # ``rev-parse``, which runs none, may read it; everything else there runs
        # pinned to the worktree's git directory in the checkout (Decision 0036).
        raise Rejected("unpinned_worktree")
    checker(args, places, config)

    argv = [config.git]
    for key, value in hardening(config):
        argv.extend(("-c", f"{key}={value}"))
    if subcommand == "merge":
        for key, value in MERGE_CONFIG:
            argv.extend(("-c", f"{key}={value}"))
    argv.extend(pinned)
    argv.append(subcommand)
    argv.extend(args)
    return Invocation(subcommand, argv, git_environment(config, ceiling), cwd)


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
) -> int:
    """Decide one call; ``exec`` git or return :data:`REJECTED`."""
    arguments = sys.argv[1:] if argv is None else list(argv)
    environment = os.environ if environ is None else environ
    user = "-"
    try:
        config = parse_config(arguments)
        user = config.user
        invocation = plan(environment.get("SSH_ORIGINAL_COMMAND"), config)
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
