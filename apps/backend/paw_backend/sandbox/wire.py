"""The ``paw-sandbox/v1`` protocol the server's SSH wrapper accepts (Decision 0083, 6).

Decision 0083 widens Decision 0029's forced-command wrapper with three
operations on a sandbox, and nothing else: start it (a fixed image, the mounts
and masks of a :class:`~.spec.SandboxSpec`), run one command in it, stop it.
Like ``paw-git-run/v1`` (``repositories/ssh.py``) the remote command is ONE
string whose words are each ``shlex.quote``-d, so that the wrapper recovers them
with POSIX word rules (it never runs a shell)::

    paw-sandbox/v1 start <sandbox id> <spec JSON>
    paw-sandbox/v1 exec  <sandbox id> <nonce> <workdir> <kind> [<file>]
    paw-sandbox/v1 stop  <sandbox id>

* ``<sandbox id>`` is a UUID (the container is ``paw-sbx-<hex>``);
* ``<spec JSON>`` is :func:`spec_document` (mounts, masks, read-only paths,
  whether the git shim's socket is mounted), compact and with sorted keys;
* ``exec`` reads the shell command (``shell``) or the file helper's input (the
  file kinds, which name their file as the last word) on stdin: a command is
  never part of the remote command line. After the command ends, the
  wrapper writes the command's stdout to its stdout, the command's stderr to its
  stderr, and then a newline and ONE status line (ending in a newline) as the
  very last line of its stderr::

      paw-sandbox-status <nonce> exit=<code> truncated=<0|1>
      paw-sandbox-status <nonce> missing

  ``<nonce>`` is chosen by the backend per call and never enters the container,
  so a command cannot forge the line (it could only write it before the
  wrapper's own). ``missing``: the container is not running.

The wrapper re-checks everything (it does not trust the backend): the README
next to it lists the rules.
"""

import json
import re
import shlex
import uuid
from dataclasses import dataclass

from paw_backend.sandbox.spec import CommandKind, SandboxCommand, SandboxSpec

PROTOCOL_TAG = "paw-sandbox/v1"
STATUS_PREFIX = "paw-sandbox-status"
_NONCE = re.compile(r"[0-9a-f]{32}")
_STATUS = re.compile(
    r"paw-sandbox-status (?P<nonce>[0-9a-f]{32}) "
    r"(?:exit=(?P<code>[0-9]{1,3}) truncated=(?P<truncated>[01])|(?P<missing>missing))"
)


def spec_document(spec: SandboxSpec) -> str:
    """The JSON word of ``start``."""
    document = {
        "mounts": [
            {"source": m.source, "target": m.target, "writable": m.writable}
            for m in spec.mounts
        ],
        "masks": list(spec.masks),
        "readonly": list(spec.readonly),
        "shim": spec.shim,
    }
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )


def _quoted(words: list[str]) -> str:
    return " ".join(shlex.quote(word) for word in words)


def start_command(sandbox_id: uuid.UUID, spec: SandboxSpec) -> str:
    return _quoted([PROTOCOL_TAG, "start", str(sandbox_id), spec_document(spec)])


def exec_command(sandbox_id: uuid.UUID, nonce: str, command: SandboxCommand) -> str:
    if _NONCE.fullmatch(nonce) is None:
        raise ValueError("a nonce is 32 lowercase hex characters")
    words = [
        PROTOCOL_TAG,
        "exec",
        str(sandbox_id),
        nonce,
        command.workdir,
        command.kind.value,
    ]
    if command.kind is not CommandKind.SHELL:
        words.append(command.argument)
    return _quoted(words)


def exec_input(command: SandboxCommand) -> bytes:
    """What ``exec`` sends on stdin: the shell command, or the file helper's
    input."""
    if command.kind is CommandKind.SHELL:
        return command.argument.encode("utf-8")
    return command.stdin


def stop_command(sandbox_id: uuid.UUID) -> str:
    return _quoted([PROTOCOL_TAG, "stop", str(sandbox_id)])


@dataclass(frozen=True, slots=True)
class ExecStatus:
    """The wrapper's status line: ``exit_code`` is ``None`` when ``missing``."""

    exit_code: int | None
    truncated: bool


def split_status(stderr: bytes, nonce: str) -> tuple[bytes, ExecStatus | None]:
    """The command's own stderr and the wrapper's status line of ``nonce``
    (``None``: no such line ends the stream, so the call did not complete)."""
    if not stderr.endswith(b"\n"):
        return stderr, None
    head, newline, last = stderr[:-1].rpartition(b"\n")
    if not newline:
        return stderr, None
    try:
        line = last.decode("ascii")
    except UnicodeDecodeError:
        return stderr, None
    found = _STATUS.fullmatch(line)
    if found is None or found["nonce"] != nonce:
        return stderr, None
    # The newline before the status line is the wrapper's, not the command's.
    if found["missing"]:
        return head, ExecStatus(None, False)
    return head, ExecStatus(int(found["code"]), found["truncated"] == "1")
