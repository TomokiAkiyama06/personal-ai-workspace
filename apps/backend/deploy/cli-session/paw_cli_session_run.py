#!/usr/bin/env python3
"""``paw-cli-session@<uuid>.service``'s program (Decision 0083, 6; #208).

Runs as ``paw-agent-cli`` (no login; its primary group ``paw-agent-cli``,
which owns its Podman storage, and the supplementary group
``paw-cli-bridge`` of the session's directory: the unit's
``SupplementaryGroups=``, issue #322) and starts ONE CLI session's container
with rootless Podman. Everything it takes comes from root-owned
configuration or from the session's directory that the backend made
(``<root>/<uuid>``, group ``paw-cli-bridge``):

* ``launch.json``: the CLI's arguments (strings), its environment (names
  from a fixed list only) and which CLI it is (``cli``: ``claude``, the
  default, or ``codex``);
* ``credential.env`` (Claude only): the one ``NAME=value`` line of the
  credential (``CLAUDE_CODE_OAUTH_TOKEN``), handed to Podman as an env file;
* ``prompt`` (Claude only): the task, the container's stdin;
* ``stream.sock``: the backend's socket; the container's stdout goes there.
  For Codex (``codex app-server`` is a conversation over stdio) it is the
  container's stdin as well, and there is no prompt and no credential file
  (Codex's login is a file in its ``CODEX_HOME``; a credential file is
  refused).

The container: the image pinned by digest (``/etc/paw/cli-session.toml``:
``image`` for Claude, ``codex_image`` for Codex),
``--network none``, a read-only root, no capabilities, no new privileges,
PID and memory limits, the session's directory mounted at the same path (the
MCP configuration names its socket and token file there) and the session's
own egress socket (``<egress_session_dir>/<uuid>.sock``, which systemd made
with this unit: ``paw-egress-session@<uuid>.socket``, Decision 0091) at
``/run/paw-egress.sock``. Without that socket the session does not start: it
never falls back to a socket shared by the sessions. Nothing else of the host.
Standard library only.

Codex's ``CODEX_HOME`` (Decision 0083, 6: the CLI's tmpfs, 0700, its own
account; issue #239) must be the session's ``config`` directory. Over it the
container gets a tmpfs of its own (``tmpfs-mode=0700``, owned by the CLI's
account: ``chown``), into which only the session's ``config.toml``
(read-only) and ``auth.json`` (read-write: Codex rewrites it in place when it
refreshes its login, and the backend reads it back from the session's
directory) are mounted, each when the backend wrote it. Everything else Codex
writes there (its logs, state, caches) stays in the container's tmpfs and
goes with the container.

The container's output is the backend's only (issue #324): ``podman run
--log-driver none`` (no copy of the stream in the journal or a log file; the
attached stdout still goes to ``stream.sock``) and ``podman --events-backend
none`` (no event entries carrying this command line in the journal).

Podman's own failures (exit 125, and 126 / 127: the entry could not be run)
are this unit's journal's (issue #322): the tail of Podman's stderr, at most
``STDERR_LINES`` lines and ``STDERR_CHARS`` characters, with the session's
secrets taken out (``redact``). Any other exit is the CLI's own: only its
code is written, never its stderr (it may hold anything of the task).

    paw_cli_session_run.py <uuid> [--config /etc/paw/cli-session.toml]
"""

import argparse
import json
import os
import re
import socket
import stat
import subprocess
import sys
import threading
import tomllib
from collections.abc import Sequence
from pathlib import Path

DEFAULT_CONFIG = "/etc/paw/cli-session.toml"
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_DIGEST = re.compile(r"[a-z0-9./:_-]+@sha256:[0-9a-f]{64}")
#: The environment the backend may set for the CLI (no secret among them).
ALLOWED_ENV = frozenset(
    {
        "HOME",
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        "DISABLE_TELEMETRY",
        "DISABLE_ERROR_REPORTING",
        "ENABLE_CLAUDEAI_MCP_SERVERS",
        "HTTPS_PROXY",
        "CODEX_HOME",
    }
)
#: Which image each CLI runs, and whether the stream socket is its stdin.
CLIS = {"claude": ("image", False), "codex": ("codex_image", True)}
ALLOWED_CREDENTIALS = frozenset({"CLAUDE_CODE_OAUTH_TOKEN"})
#: Codex's ``CODEX_HOME``: the session's directory's ``config``; the files of
#: it that are mounted into the container's own tmpfs there, and how.
CODEX_HOME_NAME = "config"
CODEX_HOME_FILES = (("config.toml", "ro"), ("auth.json", "rw"))
EGRESS_IN_CONTAINER = "/run/paw-egress.sock"
#: Where systemd makes each session's egress socket (Decision 0091, 1).
DEFAULT_EGRESS_SESSION_DIR = "/run/paw-egress/session"
#: Podman's own exit codes (125: Podman failed; 126 / 127: the entry could
#: not be run): only then is its stderr written to the journal (issue #322).
PODMAN_FAILURES = frozenset({125, 126, 127})
#: How much of Podman's stderr is kept while it runs (its tail, in bytes)
#: and how much of that is written (lines, characters).
STDERR_KEEP = 16 * 1024
STDERR_LINES = 20
STDERR_CHARS = 2000
#: Shapes of a token that no file of the session names (defence in depth):
#: an Anthropic key or OAuth token, a JWT (an OpenAI login's tokens).
_TOKEN_SHAPES = re.compile(
    r"sk-ant-[A-Za-z0-9_\-]+"
    r"|eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}(?:\.[A-Za-z0-9_\-]*)?"
)
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
REDACTED = "[redacted]"


class Refused(Exception):
    """A session this program does not start (exit code 2, a fixed text)."""


def load_config(path: str) -> dict:
    with open(path, "rb") as handle:
        config = tomllib.load(handle)
    root = config.get("root")
    image = config.get("image")
    if not isinstance(root, str) or not os.path.isabs(root):
        raise Refused("root must be an absolute path")
    if not isinstance(image, str) or not _DIGEST.fullmatch(image):
        raise Refused("image must be pinned by its digest")
    codex_image = config.get("codex_image")
    if codex_image is not None and (
        not isinstance(codex_image, str) or not _DIGEST.fullmatch(codex_image)
    ):
        raise Refused("codex_image must be pinned by its digest")
    if "egress_socket" in config:
        # The shared socket of before Decision 0091: never mounted again.
        raise Refused("egress_socket is no longer used (egress_session_dir)")
    egress = config.setdefault("egress_session_dir", DEFAULT_EGRESS_SESSION_DIR)
    if not isinstance(egress, str) or not os.path.isabs(egress):
        raise Refused("egress_session_dir must be an absolute path")
    for name, default in (("memory", "4g"), ("pids", 512), ("cpus", 2)):
        config.setdefault(name, default)
    return config


def read_launch(directory: Path) -> tuple[list[str], dict[str, str], str]:
    data = json.loads((directory / "launch.json").read_text(encoding="utf-8"))
    argv, env = data.get("argv"), data.get("env")
    cli = data.get("cli", "claude")
    if not isinstance(cli, str) or cli not in CLIS:
        raise Refused("launch.json: cli")
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(a, str) and "\x00" not in a for a in argv)
    ):
        raise Refused("launch.json: argv")
    if not isinstance(env, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and k in ALLOWED_ENV and "\n" not in v
        for k, v in env.items()
    ):
        raise Refused("launch.json: env")
    return argv, env, cli


def check_credential(directory: Path, cli: str) -> Path | None:
    path = directory / "credential.env"
    if cli == "codex":
        # Codex's login is its CODEX_HOME's file, never an environment
        # variable: a credential file here is not one this program passes.
        if os.path.lexists(path):
            raise Refused("credential.env")
        return None
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) != 1 or lines[0].split("=", 1)[0] not in ALLOWED_CREDENTIALS:
        raise Refused("credential.env")
    return path


def codex_home_mounts(directory: Path, env: dict[str, str]) -> list[str]:
    """The container's own ``CODEX_HOME`` (a tmpfs, 0700, the CLI's account)
    and the session's files mounted into it (see the module)."""
    home = directory / CODEX_HOME_NAME
    if env.get("CODEX_HOME") != str(home):
        raise Refused("launch.json: CODEX_HOME")
    mounts = [
        "--mount",
        f"type=tmpfs,destination={home},tmpfs-mode=0700,chown=true",
    ]
    for name, mode in CODEX_HOME_FILES:
        path = home / name
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            continue  # not written (yet): the version probe has neither
        if not stat.S_ISREG(info.st_mode):
            raise Refused(f"{CODEX_HOME_NAME}/{name}")
        mounts += ["--volume", f"{path}:{path}:{mode}"]
    return mounts


def egress_socket(config: dict, session_id: str) -> Path:
    """The session's own egress socket, which must exist (systemd made it
    before this unit started: ``Requires=``/``After=``)."""
    path = Path(config["egress_session_dir"]) / f"{session_id}.sock"
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        raise Refused("no egress socket of this session") from None
    if not stat.S_ISSOCK(mode):
        raise Refused("the session's egress path is not a socket")
    return path


def podman_command(
    session_id: str,
    directory: Path,
    config: dict,
    egress: Path,
    argv: Sequence[str],
    env: dict[str, str],
    credential: Path | None,
    image: str,
    extra_mounts: Sequence[str] = (),
    gid: int | None = None,
) -> list[str]:
    if gid is None:
        # The primary gid (``paw-agent-cli``): with keep-id the one mapped.
        gid = os.getgid()
    command = [
        "podman",
        # Podman's events (create, start, attach, ...) go nowhere (issue
        # #324): with the journald events backend each one is a journal entry
        # whose ``_CMDLINE`` is this whole command line (the CLI's arguments:
        # its system prompt, the session's paths).
        "--events-backend",
        "none",
        "run",
        "--rm",
        # The container's stdout and stderr are logged nowhere (issue #324):
        # the default driver (journald, through conmon) wrote every line of
        # the CLI's stream (the task's answers, its tool calls, the session's
        # ids and usage) to the journal. ``podman run`` still attaches: the
        # stdout goes to ``stream.sock`` and the stderr to the tail read
        # below, which is written only on Podman's own failure.
        "--log-driver",
        "none",
        # The container's cgroup below this unit's (Decision 0092, 4): an OOM
        # kill in the container is then the unit's (``Result=oom-kill``).
        "--cgroups=split",
        "--init",
        "-i",
        "--name",
        f"paw-cli-{session_id}",
        "--network",
        "none",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,size=256m,mode=1777",
        "--cap-drop",
        "all",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        str(int(config["pids"])),
        "--memory",
        str(config["memory"]),
        "--cpus",
        str(config["cpus"]),
        "--userns",
        "keep-id",
        # keep-id maps no gid 0 into the container: Podman's default
        # ``ping_group_range=0 0`` cannot be written there (crun: Invalid
        # argument). The range of the one gid that is mapped (issue #322).
        "--sysctl",
        f"net.ipv4.ping_group_range={gid} {gid}",
        # The unit's supplementary group (``paw-cli-bridge``, the session's
        # directory's) stays the container process's: the session's files
        # (0640 / 0660, that group) are read and written through it. crun.
        "--group-add",
        "keep-groups",
        "--volume",
        f"{directory}:{directory}:rw",
        "--volume",
        f"{egress}:{EGRESS_IN_CONTAINER}",
        *extra_mounts,
    ]
    if credential is not None:
        command += ["--env-file", str(credential)]
    for name in sorted(env):
        command += ["--env", f"{name}={env[name]}"]
    command += [image, *argv]
    return command


class _StderrTail(threading.Thread):
    """Reads a pipe to its end and keeps only its last ``STDERR_KEEP`` bytes
    (Podman never blocks on a full pipe; nothing grows without bound)."""

    def __init__(self, pipe) -> None:
        super().__init__(daemon=True)
        self._pipe = pipe
        self.data = b""
        self.truncated = False

    def run(self) -> None:
        with self._pipe:
            while chunk := self._pipe.read1(65536):
                self.data += chunk
                if len(self.data) > STDERR_KEEP:
                    self.data = self.data[-STDERR_KEEP:]
                    self.truncated = True


def session_secrets(directory: Path) -> list[str]:
    """What of the session's directory is secret, to take out of what is
    written: the credential's value, the bridge's token and the strings of
    Codex's login. A file that cannot be read has nothing to take out."""
    secrets: list[str] = []
    try:
        line = (directory / "credential.env").read_text(encoding="utf-8")
        secrets.append(line.strip().split("=", 1)[-1])
    except (OSError, ValueError):
        pass
    try:
        secrets.append((directory / "bridge.token").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pass
    try:
        login = json.loads(
            (directory / CODEX_HOME_NAME / "auth.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        login = None
    pending = [login]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, str):
            secrets.append(value)
    # Short strings (a flag, a mode) are no secrets, and taking them out
    # would take out ordinary words.
    return sorted({s for s in secrets if len(s) >= 8}, key=len, reverse=True)


def redact(text: str, secrets: Sequence[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, REDACTED)
    return _TOKEN_SHAPES.sub(REDACTED, text)


def stderr_excerpt(data: bytes, truncated: bool, secrets: Sequence[str]) -> list[str]:
    """The lines of Podman's stderr to write: its tail, without the first
    line when the start was cut (it may begin inside a secret), the secrets
    taken out, no control characters, at most ``STDERR_LINES`` lines and
    ``STDERR_CHARS`` characters."""
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if truncated and lines:
        lines = lines[1:]
    lines = [
        _CONTROL.sub("?", redact(line, secrets)).rstrip()
        for line in lines
        if line.strip()
    ][-STDERR_LINES:]
    kept: list[str] = []
    total = 0
    for line in reversed(lines):
        if total + len(line) > STDERR_CHARS:
            if not kept:
                kept.append("..." + line[-(STDERR_CHARS - 3) :])
            break
        kept.append(line)
        total += len(line)
    return kept[::-1]


def run_podman(command: Sequence[str], stdin, stdout: int, directory: Path) -> int:
    """Podman with its stderr read (never left to block, never kept whole);
    its tail is written only on Podman's own failure."""
    process = subprocess.Popen(
        command, stdin=stdin, stdout=stdout, stderr=subprocess.PIPE
    )
    tail = _StderrTail(process.stderr)
    tail.start()
    code = process.wait()
    tail.join(5)
    if code in PODMAN_FAILURES:
        lines = stderr_excerpt(tail.data, tail.truncated, session_secrets(directory))
        print(f"paw-cli-session: podman failed (exit {code})", file=sys.stderr)
        for line in lines:
            print(f"paw-cli-session: podman: {line}", file=sys.stderr)
    elif code != 0:
        print(f"paw-cli-session: the CLI ended (exit {code})", file=sys.stderr)
    sys.stderr.flush()
    return code


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="paw_cli_session_run.py", allow_abbrev=False)
    parser.add_argument("session_id")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    arguments = parser.parse_args(argv)
    try:
        if not _UUID.fullmatch(arguments.session_id):
            raise Refused("the session id must be a UUID")
        config = load_config(arguments.config)
        directory = Path(config["root"]) / arguments.session_id
        launch_argv, env, cli = read_launch(directory)
        image_key, interactive = CLIS[cli]
        image = config.get(image_key)
        if not isinstance(image, str):
            raise Refused(f"{image_key} must be pinned by its digest")
        credential = check_credential(directory, cli)
        extra = codex_home_mounts(directory, env) if cli == "codex" else []
        egress = egress_socket(config, arguments.session_id)
        command = podman_command(
            arguments.session_id,
            directory,
            config,
            egress,
            launch_argv,
            env,
            credential,
            image,
            extra,
        )
        stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stream.connect(str(directory / "stream.sock"))
    except (Refused, OSError, ValueError) as error:
        reason = str(error) if isinstance(error, Refused) else type(error).__name__
        print(f"paw-cli-session: refused ({reason})", file=sys.stderr)
        return 2
    with stream:
        if interactive:
            # The conversation: the backend's socket is stdin and stdout.
            return run_podman(command, stream.fileno(), stream.fileno(), directory)
        with open(directory / "prompt", "rb") as prompt:
            return run_podman(command, prompt, stream.fileno(), directory)


if __name__ == "__main__":
    sys.exit(main())
