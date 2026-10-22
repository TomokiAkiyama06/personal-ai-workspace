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
* for every sub-command but ``clone`` and ``init`` (:data:`UNPROBED_SUBCOMMANDS`),
  the configuration git would read must name no command (a ``filter`` driver, a
  ``merge`` driver, an ``include``, ...) and no on-demand fetch (a partial
  clone's promisor remote): :func:`check_configuration`; for ``push``, also
  no ``url.<base>.insteadOf`` / ``pushInsteadOf`` (:func:`redirects_push`).

Decision 0083 adds two things. The sandbox git shim's reads (§2): ``status``,
``diff``, ``log``, ``show`` in the forms of :func:`check_read`, ``cat-file -t``,
``merge-base --is-ancestor`` of two object ids and more ``rev-parse --verify``
revisions. And the tool sandbox (§6): a command whose first word is
``paw-sandbox/v1`` (:data:`SANDBOX_TAG`) starts, runs a command in or stops a
rootless Podman container of a fixed image (``--sandbox-image``, by digest),
with the mounts and masks re-checked here (:func:`sandbox_plan`); see
``../sandbox/README.md``. Decision 0092 (5) adds ``paw-sandbox/v2 exec``: the
same ``exec``, whose status line also says ``oom=1`` when the container's
cgroup counted an ``oom_kill`` (its host-side ``memory.events``, read before and
after the command) while it ran, ``oom=0`` when not, ``oom=-`` when it could not
be read; ``v1`` keeps its old line.

The client's ``-c`` values are checked, then **dropped**: git always gets this
file's own hardening (:func:`hardening`) and, for ``merge``, this file's own
merge identity, never the client's copy. The environment of git is a fixed
allowlist (:func:`git_environment`); nothing of ``sshd``'s environment (and so
nothing a client could send with ``SendEnv``) reaches it.

Exit status: git's own when a command is accepted (this process ``exec``-s git);
:data:`POPULATED_SUBMODULE` (125) when it is refused because a submodule is
populated, :data:`REJECTED` (126) for every other refusal (Decision 0063); never
255, which ``SshGitRunner`` reads as "ssh itself failed" (Decision 0029 §5).

What is logged
--------------

One ``syslog`` line per call (``LOG_AUTH``): accepted or rejected, a fixed
reason code, the sub-command name when it is one of the allowed ones, and the
Linux user. Never an argument, a path, a URL, a ``-c`` value or git's output: a
URL or a configuration value may carry a credential (Issue #134).
"""

import json
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
#: Exit status of a call refused as ``populated_submodule``: the backend reads it
#: as a worktree a human must clean up (``dirty``), every other refusal as
#: ``git_failed`` (Decision 0063). Not git's, not ``ssh``'s either.
POPULATED_SUBMODULE = 125
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
_HEAD_DEPTH = re.compile(r"HEAD~[1-9][0-9]{0,4}")
#: Where a writing node's base commit is recorded (Decision 0083 §10).
BASE_REF_PREFIX = "refs/paw/base/"
_OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_PROTOCOL = re.compile(r"[a-z][a-z0-9+.-]{0,15}")
_HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62})(?:\.[a-z0-9](?:[a-z0-9-]{0,62}))*")
#: ``https://<host>/<owner>/<repo>.git``: what the backend pushes to (the
#: registered GitHub repository's clone URL), and nothing else.
_PUSH_URL = re.compile(
    r"https://(?P<host>[a-z0-9.-]{1,253})/[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})"
    r"/[A-Za-z0-9._-]{1,100}\.git"
)
#: What ``push`` takes in front of its URL and refspec, exactly (:func:`_check_push`).
PUSH_OPTIONS = (
    "--quiet",
    "--no-follow-tags",
    "--no-recurse-submodules",
    "--no-signed",
    "--",
)
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


#: Sub-commands whose repository configuration is NOT listed before they run:
#: ``clone`` and ``init`` read no existing repository (``init`` only in a
#: directory without a ``.git``). Before every other one runs, the
#: configuration git would read is listed (:func:`configuration_probe`) and the
#: call is refused if it names a command, another work tree or an on-demand
#: fetch (:func:`unsafe_setting`). A sub-command that reads or writes file
#: content may start a command the configuration names (a ``filter`` driver's
#: ``clean`` / ``smudge`` / ``process``, a ``merge`` driver, ...); and any
#: sub-command that resolves an object (``rev-parse --verify``, ``merge-base``,
#: ...) fetches a missing one from a partial clone's promisor remote on a git
#: that predates ``GIT_NO_LAZY_FETCH`` (Codex review of #150, P2): so every
#: call is checked, not only those that read file content.
UNPROBED_SUBCOMMANDS = frozenset({"clone", "init"})

#: Configuration sections every key of which is (or leads to) a command, or to
#: another file this wrapper would not have listed.
#: ``gpg``: every signing setting (``gpg.program``, ``gpg.<format>.program``,
#: ``gpg.ssh.defaultKeyCommand``, ...) names or leads to a command.
#: ``protocol``: a repository's ``protocol.<name>.allow`` would re-enable a
#: transport (``ext::`` runs a command) over the wrapper's
#: ``protocol.allow=never``.
_REFUSED_SECTIONS = frozenset(
    {"filter", "include", "includeif", "hook", "pager", "gpg", "protocol"}
)
#: Two-part keys that name a command, or (``core.worktree``) a work tree other
#: than the one this wrapper checked: an unpinned ``status`` / ``merge`` in the
#: checkout would read and write the files there, outside the root.
_REFUSED_KEYS = frozenset(
    {
        "core.worktree",
        # A partial clone fetches missing objects on demand: refused even
        # where a git without ``GIT_NO_LAZY_FETCH`` would ignore the
        # environment's ban (:func:`git_environment`).
        "extensions.partialclone",
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
        # ``remote.<name>.promisor``: that remote is fetched from on demand.
        "promisor",
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
    (PAW-028); ``None`` refuses any credential helper. ``push_hosts``: the hosts
    a ``push`` may go to (``--push-host=``, Decision 0052); none refuses every
    push. The client is not trusted with the destination: a backend that is
    not itself could otherwise push a commit of any repository in the root to
    a server of its choosing.
    """

    root: str
    home: str
    user: str
    git: str = "/usr/bin/git"
    protocols: tuple[str, ...] = ("https",)
    extra: tuple[tuple[str, str], ...] = ()
    gh: str | None = None
    path: str = SAFE_PATH
    push_hosts: tuple[str, ...] = ()
    # Decision 0083 §6: the tool sandbox (``paw-sandbox/v1``). Without an
    # image every sandbox command is refused (``sandbox_not_configured``).
    podman: str = "/usr/bin/podman"
    sandbox_image: str | None = None
    sandbox_shim_base: str | None = None
    sandbox_cpus: str = "4"
    sandbox_memory: str = "8g"
    sandbox_pids: int = 1024
    sandbox_lifetime: int = 7200
    sandbox_command_timeout: int = 300
    sandbox_keep_groups: bool = False


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
    push_hosts: list[str] = []
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
        elif name == "--push-host":
            if _HOST.fullmatch(value) is None or value != value.lower():
                raise Rejected("misconfigured")
            push_hosts.append(value)
        elif name == "--allow-protocol":
            if _PROTOCOL.fullmatch(value) is None:
                raise Rejected("misconfigured")
            protocols.append(value)
        elif name == "--config":
            key, sep, setting = value.partition("=")
            if not sep or _CONFIG_KEY.fullmatch(key) is None:
                raise Rejected("misconfigured")
            extra.append((key, setting))
        elif name in _SANDBOX_OPTIONS:
            if name in seen or not _SANDBOX_OPTIONS[name](value):
                raise Rejected("misconfigured")
            seen.add(name)
            options[name[2:].replace("-", "_")] = value
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
        push_hosts=tuple(dict.fromkeys(push_hosts)),
        podman=str(options.get("podman", "/usr/bin/podman")),
        sandbox_image=_optional(options.get("sandbox_image")),
        sandbox_shim_base=_optional(options.get("sandbox_shim_base")),
        sandbox_cpus=str(options.get("sandbox_cpus", "4")),
        sandbox_memory=str(options.get("sandbox_memory", "8g")),
        sandbox_pids=int(str(options.get("sandbox_pids", 1024))),
        sandbox_lifetime=int(str(options.get("sandbox_lifetime", 7200))),
        sandbox_command_timeout=int(str(options.get("sandbox_command_timeout", 300))),
        sandbox_keep_groups=options.get("sandbox_keep_groups") == "yes",
    )


def _optional(value: object) -> str | None:
    return None if value is None else str(value)


def _bounded_int(low: int, high: int) -> Callable[[str], bool]:
    def check(value: str) -> bool:
        return re.fullmatch(r"[1-9][0-9]{0,6}", value) is not None and (
            low <= int(value) <= high
        )

    return check


#: The sandbox's options of the ``command=`` line (Decision 0083 §6) and what
#: each value must look like: the image only by digest (never a tag), paths
#: canonical, limits bounded.
_SANDBOX_OPTIONS: Mapping[str, Callable[[str], bool]] = {
    "--podman": _canonical,
    "--sandbox-image": lambda v: _IMAGE.fullmatch(v) is not None,
    "--sandbox-shim-base": lambda v: _canonical(v) and v != "/",
    "--sandbox-cpus": lambda v: _CPUS.fullmatch(v) is not None,
    "--sandbox-memory": lambda v: _MEMORY.fullmatch(v) is not None,
    "--sandbox-pids": _bounded_int(16, 65_536),
    "--sandbox-lifetime": _bounded_int(60, 86_400),
    "--sandbox-command-timeout": _bounded_int(1, 3600),
    "--sandbox-keep-groups": lambda v: v in ("yes", "no"),
}


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
    """``<rev>^{commit}`` for the revisions the backend asks about: Decision
    0036's, and the sandbox git shim's (Decision 0083 §2: ``HEAD~<n>``, an
    object id, a node's base ``refs/paw/base/paw/...``)."""
    if not value.endswith("^{commit}"):
        return False
    revision = value.removesuffix("^{commit}")
    if revision in ("HEAD", "MERGE_HEAD") or _OBJECT_ID.fullmatch(revision):
        return True
    if _HEAD_DEPTH.fullmatch(revision):
        return True
    if revision.startswith(BASE_REF_PREFIX):
        return _paw_branch(revision.removeprefix(BASE_REF_PREFIX))
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
        # The sandbox git shim's range check (Decision 0083 §2): two commits.
        if _OBJECT_ID.fullmatch(args[1]) and _OBJECT_ID.fullmatch(args[2]):
            return
    raise Rejected("bad_arguments")


def _check_cat_file(args: list[str], places: Places, config: Config) -> None:
    """``cat-file -t <object id>``: whether an id the shim was given names a
    commit (Decision 0083 §2: a tree, a blob or a tag is refused)."""
    if len(args) == 2 and args[0] == "-t" and _OBJECT_ID.fullmatch(args[1]):
        return
    raise Rejected("bad_arguments")


#: The read forms of the sandbox's git shim (Decision 0083 §2): what
#: ``paw_backend.sandbox.gitread`` sends, in the canonical words of
#: ``paw_backend.sandbox.gitargs`` (a test keeps these lists equal to that
#: module's). Each is ``<sub-command> <fixed> [options] [revisions] --
#: <pathspecs>``: the options from these lists only, revisions only as object
#: ids (``^<id>`` for ``log``), pathspecs only ``.``, ``:(literal)<path>`` and
#: ``:(exclude,literal)<path>`` with a relative path that cannot leave the work
#: tree. Unpinned (a checkout read by a node without a worktree) they read the
#: current state only: no ``show``, no revision, ``log`` by name only.
READ_FIXED: Mapping[str, tuple[str, ...]] = {
    "status": (),
    "diff": ("--no-color", "--no-ext-diff", "--no-textconv"),
    "log": ("--no-color", "--no-ext-diff", "--no-textconv", "--no-decorate"),
    "show": ("--no-color", "--no-ext-diff", "--no-textconv", "--no-decorate"),
}
_READ_DIFF_OUTPUT = frozenset(
    {
        "--stat",
        "--numstat",
        "--shortstat",
        "--name-only",
        "--name-status",
        "--summary",
        "--patch",
        "--no-renames",
        "--find-renames",
        "--ignore-all-space",
        "--ignore-space-change",
        "--ignore-blank-lines",
        "--word-diff=plain",
        "--minimal",
        "--patience",
        "--histogram",
    }
)
READ_OPTIONS: Mapping[str, frozenset[str]] = {
    "status": frozenset(
        {
            "--short",
            "--branch",
            "--porcelain=v1",
            "--long",
            "-z",
            "--untracked-files=all",
            "--untracked-files=normal",
            "--untracked-files=no",
        }
    ),
    "diff": _READ_DIFF_OUTPUT | {"--cached", "-R", "--exit-code", "--quiet", "-z"},
    "log": _READ_DIFF_OUTPUT
    | {
        "--no-patch",
        "--oneline",
        "--abbrev-commit",
        "--reverse",
        "--first-parent",
        "--no-merges",
        "--merges",
        "--graph",
    },
    "show": _READ_DIFF_OUTPUT | {"--no-patch", "--oneline", "--abbrev-commit"},
}
READ_FORMATS = ("oneline", "short", "medium", "full", "fuller", "reference")
#: ``log`` by name only (an unpinned read: Decision 0083 §2).
READ_NAMES_ONLY_LOG = frozenset({"--oneline", "--stat", "--name-only"})
_READ_NUMBER = re.compile(r"[0-9]{1,6}")
_READ_NUMBERED = {
    "log": (("--max-count=", 100_000), ("--skip=", 100_000), ("--unified=", 1000)),
    "diff": (("--unified=", 1000),),
    "show": (("--unified=", 1000),),
}


def _read_option(subcommand: str, word: str) -> bool:
    if word in READ_OPTIONS[subcommand]:
        return True
    for prefix, maximum in _READ_NUMBERED.get(subcommand, ()):
        if word.startswith(prefix):
            number = word.removeprefix(prefix)
            return _READ_NUMBER.fullmatch(number) is not None and int(number) <= maximum
    if subcommand in ("log", "show") and word.startswith("--format="):
        return word.removeprefix("--format=") in READ_FORMATS
    return False


def _read_pathspec(word: str) -> str | None:
    """``positive`` / ``exclude`` for an accepted pathspec, else ``None``."""
    if word == ".":
        return "positive"
    for prefix, kind in ((":(literal)", "positive"), (":(exclude,literal)", "exclude")):
        if word.startswith(prefix):
            path = word.removeprefix(prefix)
            if (
                path
                and len(path) <= MAX_PATH_CHARS
                and not path.startswith("/")
                and path.isprintable()
                and all(part not in ("", ".", "..") for part in path.split("/"))
            ):
                return kind
            return None
    return None


def check_read(subcommand: str, args: Sequence[str], *, pinned: bool) -> None:
    """A read form of the sandbox's git shim, or ``Rejected``."""
    fixed = list(READ_FIXED[subcommand])
    if list(args[: len(fixed)]) != fixed or "--" not in args:
        raise Rejected("bad_arguments")
    separator = list(args).index("--")
    words, specs = list(args[len(fixed) : separator]), list(args[separator + 1 :])
    if not specs or "positive" not in {_read_pathspec(w) for w in specs}:
        raise Rejected("bad_arguments")
    if any(_read_pathspec(w) is None for w in specs):
        raise Rejected("bad_arguments")
    revisions = 0
    for word in words:
        if word.startswith("-"):
            if not _read_option(subcommand, word):
                raise Rejected("bad_arguments")
            if (
                not pinned
                and subcommand == "log"
                and not (word in READ_NAMES_ONLY_LOG or word.startswith("--max-count="))
            ):
                raise Rejected("bad_arguments")
        elif _OBJECT_ID.fullmatch(word) or (
            subcommand == "log"
            and word.startswith("^")
            and _OBJECT_ID.fullmatch(word[1:])
        ):
            revisions += 1
        else:
            raise Rejected("bad_arguments")
    if subcommand == "status" and revisions:
        raise Rejected("bad_arguments")
    if not pinned and (subcommand == "show" or revisions):
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


def gh_credential_helper(config: Config) -> str | None:
    """The one credential helper accepted (``--gh``), as ``key=value``."""
    if config.gh is None:
        return None
    return f"credential.helper=!{config.gh} auth git-credential"


def _check_push(args: list[str], places: Places, config: Config) -> None:
    """Decisions 0052 and 0062 (issues #132, #90): ``push --quiet --no-follow-tags
    --no-recurse-submodules --no-signed -- <URL> <commit>:refs/heads/paw/...``.
    One commit id (never a ``+``: no force, no other refspec, no option such as
    ``--delete``, ``--mirror`` or ``--tags``) to one ``paw/`` branch of
    ``https://<host>/<owner>/<repo>.git`` on a host the administrator allowed
    (``--push-host``), and only with gh's credential helper configured
    (``--gh``). The ``--no-`` options are required: they override the
    repository's ``push.followTags`` (an annotated tag would be pushed too)
    and ``push.recurseSubmodules`` (a submodule's commits would be pushed to
    its own remote); a push without them is refused (Codex review of #159).
    ``--no-signed`` likewise overrides ``push.gpgSign`` (a signed push fails
    without a key, #90)."""
    if config.gh is None:
        raise Rejected("config_not_allowed")
    count = len(PUSH_OPTIONS)
    if len(args) == count + 2 and tuple(args[:count]) == PUSH_OPTIONS:
        url, refspec = args[count], args[count + 1]
        found = _PUSH_URL.fullmatch(url)
        if found is None or not _url(url, config) or ".." in url:
            raise Rejected("bad_arguments")
        if found["host"] not in config.push_hosts:
            raise Rejected("push_host_not_allowed")
        source, separator, destination = refspec.partition(":")
        if separator and _OBJECT_ID.fullmatch(source) and _paw_ref(destination):
            return
    raise Rejected("bad_arguments")


def _check_status(args: list[str], places: Places, config: Config) -> None:
    if _exactly(
        args, ["--porcelain=v1", "-z", "--untracked-files=all"], STATUS_WITH_SUBMODULES
    ):
        return
    # The sandbox git shim's form (pinned or not: :func:`check_read`, from
    # :func:`plan`, which knows which).
    if "--" not in args:
        raise Rejected("bad_arguments")


def _check_read_form(args: list[str], places: Places, config: Config) -> None:
    """``diff`` / ``log`` / ``show``: the shape is checked by :func:`check_read`
    in :func:`plan`, which knows whether the call is pinned."""


#: The fixed author of a Worker node's commit (Decision 0083 §10;
#: ``paw_backend.sandbox.gitread.COMMIT_CONFIG``). Accepted from the client only
#: in front of ``commit``, and applied by this file itself for ``commit``.
COMMIT_CONFIG = (
    ("user.name", "Personal AI Workspace Agent"),
    ("user.email", "agent@paw.invalid"),
    ("commit.gpgSign", "false"),
)
_COMMIT_MESSAGE = re.compile(
    r"paw: node work\n\nPAW-Node: [0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}"
    r"-[0-9a-f]{12}/[a-z][a-z0-9_-]{0,63}\n"
)
#: How the worktree's preparation finds a lost base again (Decision 0083 §10):
#: the first-parent history of a ``paw/`` branch with its node trailers.
BASE_LOG_FORMAT = "--format=%H %(trailers:key=PAW-Node,valueonly,separator=%x2C)"


def is_base_history(args: Sequence[str]) -> bool:
    """``log --first-parent --max-count=<n> <BASE_LOG_FORMAT> refs/heads/paw/<b>``."""
    return (
        len(args) == 4
        and args[0] == "--first-parent"
        and re.fullmatch(r"--max-count=[1-9][0-9]{0,4}", args[1]) is not None
        and args[2] == BASE_LOG_FORMAT
        and _paw_ref(args[3])
    )


def _literal_paths(words: Sequence[str]) -> bool:
    return bool(words) and all(
        _read_pathspec(word) == "positive" and word.startswith(":(literal)")
        for word in words
    )


def _check_update_ref(args: list[str], places: Places, config: Config) -> None:
    """Create a Worker branch's base ref once (Decision 0083 §10): ``update-ref
    refs/paw/base/paw/<b> <id> <zero id>`` (the zero id: only if absent)."""
    if (
        len(args) == 3
        and args[0].startswith("refs/paw/base/")
        and _paw_branch(args[0].removeprefix("refs/paw/base/"))
        and _OBJECT_ID.fullmatch(args[1])
        and args[2] == "0" * len(args[1])
    ):
        return
    raise Rejected("bad_arguments")


def _check_ls_files(args: list[str], places: Places, config: Config) -> None:
    """The untracked files of a worktree (Decision 0083 §10)."""
    head = ["--others", "--exclude-standard", "-z", "--"]
    specs = args[len(head) :]
    if args[: len(head)] != head or not specs:
        raise Rejected("bad_arguments")
    kinds = [_read_pathspec(word) for word in specs]
    if None in kinds or "positive" not in kinds:
        raise Rejected("bad_arguments")


def _check_add(args: list[str], places: Places, config: Config) -> None:
    """``add -A -- :(literal)<path>...``: the paths a node commits."""
    if args[:2] != ["-A", "--"] or not _literal_paths(args[2:]):
        raise Rejected("bad_arguments")


def _check_commit(args: list[str], places: Places, config: Config) -> None:
    """``commit --quiet --no-verify -m <fixed message> -- :(literal)<path>...``."""
    if (
        len(args) < 6
        or args[:3] != ["--quiet", "--no-verify", "-m"]
        or _COMMIT_MESSAGE.fullmatch(args[3]) is None
        or args[4] != "--"
        or not _literal_paths(args[5:])
    ):
        raise Rejected("bad_arguments")


Checker = Callable[[list[str], Places, Config], None]

#: The allowlist: Decision 0029 §3 (``rev-parse``, ``symbolic-ref``, ``config``,
#: ``clone``, ``init``, ``remote``) and Decision 0036 §13 (``worktree``,
#: ``merge``, ``merge-tree``, ``merge-base``, ``status`` and the added
#: ``rev-parse`` / ``symbolic-ref`` shapes), and Decision 0051 (PR #130: the
#: second ``status`` form and ``submodule status --cached``, pinned only; no
#: other ``submodule`` sub-command or option), and Decision 0052 (issue #132:
#: one ``push`` form, never pinned), and Decision 0083 §2 (the sandbox git
#: shim's reads: ``status`` / ``diff`` / ``log`` / ``show`` in the forms of
#: :func:`check_read`, ``cat-file -t <id>``, ``merge-base --is-ancestor <id>
#: <id>`` and the added ``rev-parse --verify`` revisions). ``True``: the
#: sub-command may run pinned to a worktree (``--git-dir=`` / ``--work-tree=``,
#: Decision 0036 §13). Anything not here — any other ``push``, ``fetch``,
#: ``pull``, ``checkout``, ``switch``, ``reset``, ``rebase``, ``gc``, ``commit``,
#: ``config`` writes, ... — is refused.
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
    "merge-base": (_check_merge_base, True),
    "status": (_check_status, True),
    "submodule": (_check_submodule, True),
    "push": (_check_push, False),
    # Decision 0083 §2: the sandbox git shim's reads (``check_read``).
    "diff": (_check_read_form, True),
    "log": (_check_read_form, True),
    "show": (_check_read_form, True),
    "cat-file": (_check_cat_file, True),
    # Decision 0083 §10: a Worker node's base ref, its changes and its commit.
    "update-ref": (_check_update_ref, False),
    "ls-files": (_check_ls_files, True),
    "add": (_check_add, True),
    "commit": (_check_commit, True),
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
    #: ``argv`` runs; ``None`` for :data:`UNPROBED_SUBCOMMANDS`.
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


def _split(original: str | None, *, limit: int = MAX_COMMAND_BYTES) -> list[str]:
    if original is None or original == "":
        raise Rejected("no_command")  # an interactive login or a bare `ssh host`
    if len(original.encode("utf-8", "surrogateescape")) > limit:
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
    if subcommand == "commit":
        allowed |= {f"{key}={value}" for key, value in COMMIT_CONFIG}
    helper = gh_credential_helper(config)
    if subcommand == "push" and helper is not None:
        # Decision 0052: the push empties the repository's credential helpers
        # and names gh's own (this file adds both itself, whatever was sent).
        allowed |= {"credential.helper=", helper}
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
    if subcommand in ("add", "commit") and not pinned:
        raise Rejected("bad_arguments")  # only in a worktree, pinned
    if subcommand == "log" and is_base_history(args):
        if pinned:
            raise Rejected("bad_arguments")
    elif subcommand in READ_FIXED and not (
        subcommand == "status"
        and _exactly(
            args,
            ["--porcelain=v1", "-z", "--untracked-files=all"],
            STATUS_WITH_SUBMODULES,
        )
    ):
        check_read(subcommand, args, pinned=bool(pinned))
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
    if subcommand == "push":
        # The only other call that talks to a remote: with gh's helper alone
        # (``_check_push`` refused the call without ``--gh``).
        assert helper is not None
        argv.extend(("-c", helper))
    if subcommand == "merge":
        for key, value in MERGE_CONFIG:
            argv.extend(("-c", f"{key}={value}"))
    if subcommand == "commit":
        for key, value in COMMIT_CONFIG:
            argv.extend(("-c", f"{key}={value}"))
    argv.extend(pinned)
    argv.append(subcommand)
    argv.extend(args)
    probe = None
    if subcommand not in UNPROBED_SUBCOMMANDS:
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


def redirects_push(key: str) -> bool:
    """Whether the configuration ``key`` changes where or how a push goes from
    the URL the backend named: ``url.<base>.insteadOf`` / ``pushInsteadOf``
    rewrite it, and ``http.*`` (``http.proxy``, ``http.sslVerify``,
    ``http.extraHeader``, ...) would route it, or its credential, elsewhere
    (Decision 0052). A named remote's ``url`` / ``pushurl`` do not apply: the
    push names a URL, not a remote."""
    key = key.lower()
    section, _, rest = key.partition(".")
    variable = key.rpartition(".")[2]
    if section == "http":
        return True
    return (
        section == "url" and "." in rest and variable in ("insteadof", "pushinsteadof")
    )


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


#: The most entries :func:`check_links` looks at, and for how long (the probes'
#: deadline), before git runs: a repository with a planted fan-out of loose
#: objects or refs is refused (``git_dir_too_large``) instead of being walked
#: for an unbounded time on every call (Codex review of #150, P2). A repository
#: git keeps packed (``git gc --auto`` packs loose objects past about 6,700)
#: stays far below it.
MAX_METADATA_ENTRIES = 500_000
METADATA_TIMEOUT_S = PROBE_TIMEOUT_S


def check_links(
    directories: Sequence[str],
    *,
    limit: int = MAX_METADATA_ENTRIES,
    timeout: float = METADATA_TIMEOUT_S,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Refuse the call when any of ``directories`` (a git directory, a common
    directory) holds a symbolic link, a file with another hard link
    (``objects/`` aside) or an ``objects/info/alternates`` (``git_dir_alternates``),
    ``hooks/`` aside: git creates files under
    ``refs/``, ``logs/``, ``objects/``, ``worktrees/``, ... and would follow a
    linked directory there out of the root, and writes ``MERGE_MSG``,
    ``config``, ... whose other link may be outside it, although the
    directory itself was checked to be inside it.

    At most ``limit`` entries, within ``timeout`` seconds, over all the
    directories; beyond either the call is refused (``git_dir_too_large``).
    Each directory is read one entry at a time, so that one huge directory
    counts as it is read."""
    deadline = clock() + timeout
    seen = 0
    todo = sorted(set(directories))
    for index, top in enumerate(todo):
        if any(_within(top, other, allow_equal=False) for other in todo[:index]):
            continue  # already walked with the directory it is in
        if os.path.islink(top):
            raise Rejected("git_dir_link")
        pending = [top]
        while pending:
            path = pending.pop()
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
            in_info = parts[-2:] == ["objects", "info"]
            try:
                with os.scandir(path) as entries:
                    for entry in entries:
                        seen += 1
                        if seen > limit or clock() > deadline:
                            raise Rejected("git_dir_too_large")
                        if path == top and entry.name in _UNWRITTEN:
                            continue
                        if in_info and entry.name in _ALTERNATES:
                            # Another object directory (anywhere: another
                            # user's repository, say) whose objects git would
                            # read, and a ``worktree add`` of one of its
                            # commits would check out.
                            raise Rejected("git_dir_alternates")
                        info = entry.stat(follow_symlinks=False)
                        if stat.S_ISLNK(info.st_mode):
                            raise Rejected("git_dir_link")
                        if stat.S_ISDIR(info.st_mode):
                            pending.append(entry.path)
                        elif (
                            stat.S_ISREG(info.st_mode)
                            and info.st_nlink > 1
                            and not linked_ok
                        ):
                            raise Rejected("git_dir_link")
            except OSError:
                raise Rejected("git_dir_link") from None


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
    command, another work tree or an on-demand fetch (every sub-command but
    :data:`UNPROBED_SUBCOMMANDS`), or cannot be listed.

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
        if unsafe_setting(key) or (
            invocation.subcommand == "push" and redirects_push(key)
        ):
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


# -- the sandbox of Decision 0083 §6 (``paw-sandbox/v1``) ---------------------------

#: The first word of a sandbox command (``paw_backend.sandbox.wire``).
SANDBOX_TAG = "paw-sandbox/v1"
#: The same ``exec`` whose status line also says whether the container's cgroup
#: counted an OOM kill while the command ran (``oom=<0|1|->``; Decision 0092,
#: 5). ``start`` and ``stop`` stay ``v1``; a backend sends ``v2`` only once every
#: account's wrapper is this one (``PAW_SANDBOX_OOM_STATUS``).
SANDBOX_TAG_V2 = "paw-sandbox/v2"
#: Where the host's cgroup v2 hierarchy is mounted (read only, as the account).
CGROUP_ROOT = "/sys/fs/cgroup"
MAX_MEMORY_EVENTS_BYTES = 4096
#: A sandbox command may be longer than a git one (a start lists every mask).
MAX_SANDBOX_COMMAND_BYTES = 128 * 1024
SANDBOX_NAME_PREFIX = "paw-sbx-"
SANDBOX_LABEL = "paw.sandbox=v1"
WORKSPACE = "/workspace"
SHIM_MOUNT = "/run/paw-shim"
#: What a ``shell`` command runs as, and what a file tool runs (in the image).
FILE_HELPER = "/usr/local/lib/paw/paw-file-tool"
SANDBOX_KINDS = ("shell", "file-write", "file-str-replace")
#: The most of each stream of a command that is kept (the rest is dropped and
#: the status line says so), and the most a command may read on stdin.
MAX_STREAM_BYTES = 4 * 1024 * 1024
MAX_STDIN_BYTES = 1024 * 1024
#: A shell command is one argument of ``bash -lc``: Linux takes 128 KiB.
MAX_SHELL_COMMAND_BYTES = 100 * 1024
MAX_MASKS = 256
#: How long ``start``'s and ``exec``'s own Podman calls may take.
SANDBOX_CONTROL_TIMEOUT_S = 120
_IMAGE = re.compile(r"[a-z0-9][a-z0-9._/:-]{0,254}@sha256:[0-9a-f]{64}")
_CPUS = re.compile(r"[1-9][0-9]{0,2}(?:\.[0-9]{1,2})?")
_MEMORY = re.compile(r"[1-9][0-9]{0,5}[kmg]")
_NONCE = re.compile(r"[0-9a-f]{32}")
_UNMOUNTABLE = re.compile(r'[,"\x00-\x1f\x7f]')


@dataclass(frozen=True)
class SandboxCall:
    """One accepted sandbox command: what runs (``argv``) and how.

    ``op``: ``start`` / ``exec`` / ``stop``. For ``exec``, ``inspect`` asks
    Podman whether the container runs, ``nonce`` ends the status line and
    ``kind`` says what ``argv`` is; the command or the file helper's input
    comes on stdin."""

    op: str
    name: str
    argv: list[str]
    env: dict[str, str]
    inspect: list[str] = field(default_factory=list)
    nonce: str = ""
    kind: str = ""
    #: ``paw-sandbox/v2``: the status line carries ``oom=``.
    oom: bool = False


def _sandbox_id(word: str) -> str:
    """The container name of a sandbox id (a canonical UUID), or ``Rejected``."""
    if (
        re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", word
        )
        is None
    ):
        raise Rejected("bad_sandbox_id")
    return SANDBOX_NAME_PREFIX + word.replace("-", "")


def _sandbox_path(value: object) -> str:
    """A canonical absolute path inside the sandbox (under ``/workspace``)."""
    if (
        not isinstance(value, str)
        or not _canonical(value)
        or (value != WORKSPACE and not value.startswith(f"{WORKSPACE}/"))
    ):
        raise Rejected("bad_path")
    return value


def _host_path(value: object, places: Places, *, worktree: bool | None) -> str:
    """A host path of a mount, a mask or a read-only path: canonical, mountable
    (``podman --mount`` reads its value as CSV), in the root, and already the
    path every symbolic link resolves to (nothing on the way is a link)."""
    if not isinstance(value, str) or _UNMOUNTABLE.search(value):
        raise Rejected("bad_path")
    real = places.check(value, worktree=worktree, must_exist=True)
    if real != value:
        raise Rejected("path_is_link")
    return real


def _container_target(path: str, source: str, target: str) -> str:
    return target + path[len(source) :]


def _own_user() -> str:
    """``<uid>:<gid>`` of this process (the account the sandbox runs as)."""
    return f"{os.geteuid()}:{os.getegid()}"


def sandbox_environment(config: Config) -> dict[str, str]:
    """Podman's whole environment: a fixed allowlist (rootless Podman needs the
    user's runtime directory), nothing inherited from ``sshd``."""
    return {
        "PATH": config.path,
        "HOME": config.home,
        "LC_ALL": "C",
        "LANG": "C",
        "XDG_RUNTIME_DIR": f"/run/user/{os.geteuid()}",
    }


def _start_mounts(spec: object, places: Places, config: Config, name: str) -> list[str]:
    """The ``--mount`` options of a ``start`` spec, every path re-checked."""
    if not isinstance(spec, dict) or set(spec) != {
        "mounts",
        "masks",
        "readonly",
        "shim",
    }:
        raise Rejected("bad_spec")
    mounts, masks, readonly, shim = (
        spec["mounts"],
        spec["masks"],
        spec["readonly"],
        spec["shim"],
    )
    if (
        not isinstance(mounts, list)
        or len(mounts) != 1
        or not isinstance(masks, list)
        or not isinstance(readonly, list)
        or len(masks) + len(readonly) > MAX_MASKS
        or not isinstance(shim, bool)
    ):
        raise Rejected("bad_spec")
    mount = mounts[0]
    if not isinstance(mount, dict) or set(mount) != {"source", "target", "writable"}:
        raise Rejected("bad_spec")
    writable = mount["writable"]
    if not isinstance(writable, bool):
        raise Rejected("bad_spec")
    # Only a worktree (inside ``.paw-worktrees``, with its ``.git`` file) is
    # ever writable: a user's checkout is mounted read-only or not at all.
    source = _host_path(mount["source"], places, worktree=True if writable else None)
    target = _sandbox_path(mount["target"])
    if (
        target != WORKSPACE
        and re.fullmatch(
            r"/workspace/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            target,
        )
        is None
    ):
        raise Rejected("bad_spec")
    if not os.path.isdir(source):
        raise Rejected("bad_path")
    if writable:
        try:
            own_git = os.lstat(os.path.join(source, ".git"))
        except OSError:
            raise Rejected("not_a_worktree") from None
        if not stat.S_ISREG(own_git.st_mode):
            raise Rejected("not_a_worktree")
    options = [
        f"type=bind,source={source},destination={target}"
        + ("" if writable else ",ro=true")
    ]
    placed: list[str] = []
    for value, kind in [
        *((m, "mask") for m in masks),
        *((r, "readonly") for r in readonly),
    ]:
        path = _host_path(value, places, worktree=None)
        if not _within(path, source, allow_equal=False):
            raise Rejected("bad_mask")
        if any(
            _within(path, other, allow_equal=True)
            or _within(other, path, allow_equal=True)
            for other in placed
        ):
            raise Rejected("bad_mask")
        placed.append(path)
        info = _lstat(path)
        inside = _container_target(path, source, target)
        if kind == "readonly":
            if not writable or not (
                stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)
            ):
                raise Rejected("bad_mask")
            options.append(f"type=bind,source={path},destination={inside},ro=true")
        elif stat.S_ISDIR(info.st_mode):
            # An empty tmpfs that the sandbox's user (not its root) cannot write.
            # ``notmpcopyup``: Podman copies what is under a tmpfs mount into
            # it by default, so a small directory would show what it hides
            # and a larger one (a checkout's .git) fails the start (#286).
            options.append(
                f"type=tmpfs,destination={inside},tmpfs-size=4k,tmpfs-mode=0555,"
                "notmpcopyup"
            )
        elif stat.S_ISREG(info.st_mode):
            options.append(f"type=bind,source=/dev/null,destination={inside},ro=true")
        else:
            raise Rejected("mask_unavailable")
    if shim:
        if config.sandbox_shim_base is None:
            raise Rejected("sandbox_not_configured")
        directory = (
            f"{config.sandbox_shim_base}/{name.removeprefix(SANDBOX_NAME_PREFIX)}"
        )
        info = _lstat(directory)
        if not stat.S_ISDIR(info.st_mode) or os.path.realpath(directory) != directory:
            raise Rejected("bad_shim")
        options.append(f"type=bind,source={directory},destination={SHIM_MOUNT},ro=true")
    return options


def _lstat(path: str) -> os.stat_result:
    try:
        return os.lstat(path)
    except OSError:
        raise Rejected("mask_unavailable") from None


def sandbox_plan(original: str, config: Config) -> SandboxCall:
    """The :class:`SandboxCall` of a ``paw-sandbox/v1`` command, or ``Rejected``."""
    if config.sandbox_image is None:
        raise Rejected("sandbox_not_configured")
    words = _split(original, limit=MAX_SANDBOX_COMMAND_BYTES)
    if len(words) < 3 or words[0] not in (SANDBOX_TAG, SANDBOX_TAG_V2):
        raise Rejected("bad_protocol")
    oom = words[0] == SANDBOX_TAG_V2
    if oom and words[1] != "exec":
        raise Rejected("bad_protocol")  # only ``exec`` has a ``v2``
    op, name = words[1], _sandbox_id(words[2])
    env = sandbox_environment(config)
    podman = config.podman
    if op == "start" and len(words) == 4:
        try:
            spec = json.loads(words[3])
        except ValueError:
            raise Rejected("bad_spec") from None
        places = Places.of(config)
        mounts = _start_mounts(spec, places, config, name)
        argv = [
            podman,
            "run",
            "--detach",
            "--rm",
            "--name",
            name,
            "--label",
            SANDBOX_LABEL,
            "--pull",
            "never",
            "--network",
            "none",
            "--userns",
            "keep-id",
            # The account's own uid / gid, whatever the image's USER says: a
            # container root would be another (unprivileged) uid here and could
            # not write the account's worktree.
            "--user",
            _own_user(),
            "--cap-drop",
            "all",
            "--security-opt",
            "no-new-privileges",
            "--cpus",
            config.sandbox_cpus,
            "--memory",
            config.sandbox_memory,
            "--pids-limit",
            str(config.sandbox_pids),
            "--timeout",
            str(config.sandbox_lifetime),
            "--env",
            "HOME=/tmp",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--workdir",
            WORKSPACE,
        ]
        if config.sandbox_keep_groups:
            argv.extend(("--group-add", "keep-groups"))
        for option in mounts:
            argv.extend(("--mount", option))
        argv.extend((config.sandbox_image, "sleep", "infinity"))
        return SandboxCall("start", name, argv, env)
    if op == "stop" and len(words) == 3:
        argv = [podman, "rm", "--force", "--time", "5", "--ignore", name]
        return SandboxCall("stop", name, argv, env)
    if op == "exec" and len(words) in (6, 7):
        nonce, workdir, kind = words[3], _sandbox_path(words[4]), words[5]
        if _NONCE.fullmatch(nonce) is None or kind not in SANDBOX_KINDS:
            raise Rejected("bad_arguments")
        if (kind == "shell") != (len(words) == 6):
            raise Rejected("bad_arguments")
        argv = [podman, "exec"]
        if kind == "shell":
            entry = [
                "timeout",
                "-k",
                "5",
                str(config.sandbox_command_timeout),
                "bash",
                "-lc",
            ]
        else:
            argv.append("--interactive")
            helper = "write" if kind == "file-write" else "str-replace"
            entry = [
                "timeout",
                "-k",
                "5",
                str(config.sandbox_command_timeout),
                "python3",
                "-I",
                FILE_HELPER,
                helper,
                _sandbox_path(words[6]),
            ]
        argv.extend(("--user", _own_user(), "--workdir", workdir, name, *entry))
        inspect = [
            podman,
            "container",
            "inspect",
            "--format",
            # ``v2``: where its cgroup is, to read its ``memory.events``.
            "{{.State.Running}} {{.State.CgroupPath}}" if oom else "{{.State.Running}}",
            name,
        ]
        return SandboxCall("exec", name, argv, env, inspect, nonce, kind, oom)
    raise Rejected("bad_arguments")


def _capture(
    argv: Sequence[str],
    env: Mapping[str, str],
    stdin: bytes,
    timeout: float,
) -> tuple[int | None, bytes, bytes, bool]:
    """Run ``argv`` with ``stdin``: ``(status, stdout, stderr, truncated)``,
    each stream kept to :data:`MAX_STREAM_BYTES` (read on to its end, so that
    the command never blocks on a full pipe). ``status`` is ``None`` when
    ``timeout`` passed (the process is killed)."""
    deadline = time.monotonic() + timeout
    truncated = False
    with subprocess.Popen(
        list(argv),
        env=dict(env),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as process:
        assert process.stdin and process.stdout and process.stderr
        try:
            if stdin:
                process.stdin.write(stdin)
            process.stdin.close()
        except BrokenPipeError:
            pass
        out_fd, err_fd = process.stdout.fileno(), process.stderr.fileno()
        kept: dict[int, list[bytes]] = {out_fd: [], err_fd: []}
        sizes = {out_fd: 0, err_fd: 0}
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                selector.register(process.stderr, selectors.EVENT_READ)
                remaining_streams = 2
                while remaining_streams:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        process.kill()
                        process.wait()
                        return None, b"", b"", truncated
                    for key, _ in selector.select(remaining):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            remaining_streams -= 1
                            continue
                        room = MAX_STREAM_BYTES - sizes[key.fd]
                        if room > 0:
                            kept[key.fd].append(chunk[:room])
                            sizes[key.fd] += min(len(chunk), room)
                        if len(chunk) > room:
                            truncated = True
            returncode = process.wait(max(0.0, deadline - time.monotonic()))
        except BaseException:
            process.kill()
            raise
    return returncode, b"".join(kept[out_fd]), b"".join(kept[err_fd]), truncated


def _read_stdin(limit: int) -> bytes:
    data = sys.stdin.buffer.read(limit + 1)
    if len(data) > limit:
        raise Rejected("too_long")
    return data


class SandboxTimeout(Exception):
    """An ``exec`` that outlived even the wrapper's own deadline."""


def memory_events_path(cgroup_path: str, root: str = CGROUP_ROOT) -> str | None:
    """The host's ``memory.events`` of a container's ``State.CgroupPath``
    (``None``: not a path under ``root``: relative, empty, or with ``..``)."""
    if not cgroup_path.startswith("/") or "\x00" in cgroup_path:
        return None
    parts = cgroup_path.split("/")
    if ".." in parts or "." in parts:
        return None
    path = os.path.normpath(root + cgroup_path)
    if path == root or not path.startswith(root.rstrip("/") + "/"):
        return None
    return path + "/memory.events"


def read_oom_kills(path: str | None) -> int | None:
    """The ``oom_kill`` count of a cgroup's ``memory.events`` (counting its
    descendants), ``None`` when it cannot be read or has another form."""
    if path is None:
        return None
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_MEMORY_EVENTS_BYTES + 1)
    except OSError:
        return None
    if len(data) > MAX_MEMORY_EVENTS_BYTES:
        return None
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        return None
    for line in text.splitlines():
        key, _, value = line.partition(" ")
        if key == "oom_kill":
            if re.fullmatch(r"[0-9]{1,20}", value) is None:
                return None
            return int(value)
    return None


def run_sandbox_exec(
    call: SandboxCall,
    config: Config,
    stdin: bytes,
    *,
    capture: Callable[..., tuple[int | None, bytes, bytes, bool]] = _capture,
    cgroup_root: str = CGROUP_ROOT,
) -> tuple[bytes, bytes]:
    """Run an accepted ``exec``: ``(stdout, stderr)`` to write, the status line
    of ``call.nonce`` last (``missing``: the container is not running)."""
    status_prefix = f"\npaw-sandbox-status {call.nonce} "
    running, answer, _, _ = capture(
        call.inspect, call.env, b"", SANDBOX_CONTROL_TIMEOUT_S
    )
    words = answer.split()
    if running != 0 or not words or words[0] != b"true":
        return b"", (status_prefix + "missing\n").encode("ascii")
    if not call.oom and len(words) != 1:
        return b"", (status_prefix + "missing\n").encode("ascii")
    events = None
    if call.oom and len(words) == 2:
        # Read on the host, as the account (never inside the container, whose
        # programs the model may have replaced).
        try:
            events = memory_events_path(words[1].decode("utf-8"), cgroup_root)
        except UnicodeDecodeError:
            events = None
    before = read_oom_kills(events)
    argv = list(call.argv)
    if call.kind == "shell":
        try:
            command = stdin.decode("utf-8")
        except UnicodeDecodeError:
            raise Rejected("bad_encoding") from None
        if not command or "\x00" in command or len(stdin) > MAX_SHELL_COMMAND_BYTES:
            raise Rejected("too_long")
        argv.append(command)
        stdin = b""
    limit = config.sandbox_command_timeout + 60
    code, out, err, truncated = capture(argv, call.env, stdin, limit)
    if code is None:
        # ``timeout -k 5`` in the container did not end it: no status line, so
        # that the backend reads the call as not completed and stops the whole
        # sandbox (whatever is left running in it).
        raise SandboxTimeout()
    if code < 0:
        code = 128 - code  # a signal, as a shell reports it
    status = f"exit={min(code, 255)} truncated={1 if truncated else 0}"
    if call.oom:
        after = read_oom_kills(events)
        if before is None or after is None:
            oom = "-"
        else:
            oom = "1" if after > before else "0"
        status += f" oom={oom}"
    return out, err + (status_prefix + status + "\n").encode("ascii")


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
    read_stdin: Callable[[int], bytes] = _read_stdin,
) -> int:
    """Decide one call; ``exec`` git (or Podman, for a sandbox's ``start`` /
    ``stop``), run a sandbox's ``exec``, or return :data:`REJECTED` (or
    :data:`POPULATED_SUBMODULE`)."""
    arguments = sys.argv[1:] if argv is None else list(argv)
    environment = os.environ if environ is None else environ
    user = "-"
    call: SandboxCall | None = None
    stdin = b""
    try:
        config = parse_config(arguments)
        user = config.user
        original = environment.get("SSH_ORIGINAL_COMMAND")
        if original is not None and original.startswith(
            (f"{SANDBOX_TAG} ", f"{SANDBOX_TAG_V2} ")
        ):
            call = sandbox_plan(original, config)
            if call.op == "exec":
                stdin = read_stdin(MAX_STDIN_BYTES)
        else:
            invocation = plan(original, config)
            check(invocation)
    except Rejected as rejected:
        log(f"rejected user={user} reason={rejected.reason}")
        sys.stderr.write(f"paw-git-wrapper: rejected ({rejected.reason})\n")
        if rejected.reason == "populated_submodule":
            return POPULATED_SUBMODULE
        return REJECTED
    if call is not None:
        return _sandbox_main(call, config, stdin, execve=execve, chdir=chdir, log=log)
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


def _sandbox_main(
    call: SandboxCall,
    config: Config,
    stdin: bytes,
    *,
    execve: Callable[[str, list[str], dict[str, str]], object],
    chdir: Callable[[str], object],
    log: Callable[[str], None],
) -> int:
    """Run an accepted sandbox command (Decision 0083 §6). The log names the
    operation and the user only: never a path, a command or its output."""
    user = config.user
    if call.op == "exec":
        try:
            out, err = run_sandbox_exec(call, config, stdin)
        except Rejected as rejected:
            log(f"rejected user={user} reason={rejected.reason}")
            sys.stderr.write(f"paw-git-wrapper: rejected ({rejected.reason})\n")
            return REJECTED
        except SandboxTimeout:
            log(f"failed user={user} sandbox=exec reason=timeout")
            sys.stderr.write("paw-git-wrapper: the command did not end\n")
            return 1
        except (OSError, subprocess.SubprocessError):
            log(f"failed user={user} sandbox=exec reason=podman")
            sys.stderr.write("paw-git-wrapper: the sandbox could not be used\n")
            return REJECTED
        log(f"accepted user={user} sandbox=exec kind={call.kind}")
        sys.stdout.buffer.write(out)
        sys.stdout.flush()
        sys.stderr.buffer.write(err)
        sys.stderr.flush()
        return 0
    log(f"accepted user={user} sandbox={call.op}")
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        chdir(config.home)
        execve(call.argv[0], call.argv, call.env)
    except OSError:
        log(f"failed user={user} sandbox={call.op} reason=exec")
        sys.stderr.write("paw-git-wrapper: podman could not be started\n")
        return REJECTED
    return 0  # only reached when ``execve`` is a test double


if __name__ == "__main__":
    sys.exit(main())
