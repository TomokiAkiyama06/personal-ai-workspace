#!/usr/bin/env python3
"""``paw-cli-session@<uuid>.service``'s program (Decision 0083, 6; #208).

Runs as ``paw-agent-cli`` (no login, in no user's group but
``paw-cli-bridge``) and starts ONE CLI session's container with rootless
Podman. Everything it takes comes from root-owned configuration or from the
session's directory that the backend made (``<root>/<uuid>``, group
``paw-cli-bridge``):

* ``launch.json``: the CLI's arguments (strings) and its environment (names
  from a fixed list only);
* ``credential.env``: the one ``NAME=value`` line of the credential
  (``CLAUDE_CODE_OAUTH_TOKEN``), handed to Podman as an env file;
* ``prompt``: the task, the container's stdin;
* ``stream.sock``: the backend's socket; the container's stdout goes there.

The container: the image pinned by digest (``/etc/paw/cli-session.toml``),
``--network none``, a read-only root, no capabilities, no new privileges,
PID and memory limits, the session's directory mounted at the same path (the
MCP configuration names its socket and token file there) and the egress
proxy's socket. Nothing else of the host. Standard library only.

    paw_cli_session_run.py <uuid> [--config /etc/paw/cli-session.toml]
"""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
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
    }
)
ALLOWED_CREDENTIALS = frozenset({"CLAUDE_CODE_OAUTH_TOKEN"})
EGRESS_IN_CONTAINER = "/run/paw-egress.sock"


class Refused(Exception):
    """A session this program does not start (exit code 2, a fixed text)."""


def load_config(path: str) -> dict:
    with open(path, "rb") as handle:
        config = tomllib.load(handle)
    root = config.get("root")
    image = config.get("image")
    egress = config.get("egress_socket")
    if not isinstance(root, str) or not os.path.isabs(root):
        raise Refused("root must be an absolute path")
    if not isinstance(image, str) or not _DIGEST.fullmatch(image):
        raise Refused("image must be pinned by its digest")
    if not isinstance(egress, str) or not os.path.isabs(egress):
        raise Refused("egress_socket must be an absolute path")
    for name, default in (("memory", "4g"), ("pids", 512), ("cpus", 2)):
        config.setdefault(name, default)
    return config


def read_launch(directory: Path) -> tuple[list[str], dict[str, str]]:
    data = json.loads((directory / "launch.json").read_text(encoding="utf-8"))
    argv, env = data.get("argv"), data.get("env")
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
    return argv, env


def check_credential(directory: Path) -> Path:
    path = directory / "credential.env"
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) != 1 or lines[0].split("=", 1)[0] not in ALLOWED_CREDENTIALS:
        raise Refused("credential.env")
    return path


def podman_command(
    session_id: str,
    directory: Path,
    config: dict,
    argv: Sequence[str],
    env: dict[str, str],
    credential: Path,
) -> list[str]:
    command = [
        "podman",
        "run",
        "--rm",
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
        "--volume",
        f"{directory}:{directory}:rw",
        "--volume",
        f"{config['egress_socket']}:{EGRESS_IN_CONTAINER}",
        "--env-file",
        str(credential),
    ]
    for name in sorted(env):
        command += ["--env", f"{name}={env[name]}"]
    command += [config["image"], *argv]
    return command


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
        launch_argv, env = read_launch(directory)
        credential = check_credential(directory)
        command = podman_command(
            arguments.session_id, directory, config, launch_argv, env, credential
        )
        stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stream.connect(str(directory / "stream.sock"))
    except (Refused, OSError, ValueError) as error:
        reason = str(error) if isinstance(error, Refused) else type(error).__name__
        print(f"paw-cli-session: refused ({reason})", file=sys.stderr)
        return 2
    with open(directory / "prompt", "rb") as prompt, stream:
        result = subprocess.run(
            command, stdin=prompt, stdout=stream.fileno(), stderr=subprocess.DEVNULL
        )
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
