#!/usr/bin/env -S python3 -I
"""``git`` inside the tool sandbox (Decision 0083, 2): it runs no git here.

Installed in the image as ``/usr/local/lib/paw/paw-git-shim``; the image's
``git`` (``paw_git_front.py``) runs it for a call aimed at the node's
repository (``/workspace``; Decision 0090, 1). It sends its arguments and
working directory to the backend over the sandbox's own Unix socket
(``/run/paw-shim/git.sock``, with the token next to it), which runs a
read-only git on the host, pinned to the repository and limited to
``status`` / ``diff`` / ``log`` / ``show`` with a fixed list of options, the
node's own commits and the paths in scope. It prints git's output and exits
with git's status, or prints why the call was refused and exits 128 (git's own
status for a fatal error). Standard library only.
"""

import json
import os
import socket
import sys

SHIM_DIRECTORY = "/run/paw-shim"
MAX_REQUEST_BYTES = 64 * 1024
MAX_ANSWER_BYTES = 64 * 1024 * 1024
#: Longer than the backend's own limit for one read (it answers first).
TIMEOUT_SECONDS = 330
FATAL = 128


def _fail(message: str) -> int:
    sys.stderr.write(f"git: {message}\n")
    return FATAL


def main(argv: list[str]) -> int:
    try:
        with open(os.path.join(SHIM_DIRECTORY, "token"), encoding="ascii") as handle:
            token = handle.read().strip()
    except OSError:
        return _fail("git is not available in this sandbox")
    try:
        cwd = os.getcwd()
    except OSError:
        return _fail("the current directory is not available")
    request = (json.dumps({"token": token, "cwd": cwd, "argv": argv}) + "\n").encode()
    if len(request) > MAX_REQUEST_BYTES:
        return _fail("the git command is too long")
    chunks: list[bytes] = []
    size = 0
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(TIMEOUT_SECONDS)
            connection.connect(os.path.join(SHIM_DIRECTORY, "git.sock"))
            connection.sendall(request)
            while not chunks or not chunks[-1].endswith(b"\n"):
                chunk = connection.recv(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_ANSWER_BYTES:
                    return _fail("the answer is too large")
                chunks.append(chunk)
    except OSError:
        return _fail("git is not available right now")
    try:
        answer = json.loads(b"".join(chunks).decode("utf-8"))
    except ValueError:
        return _fail("git gave no answer")
    if not isinstance(answer, dict):
        return _fail("git gave no answer")
    if isinstance(answer.get("exit"), int) and isinstance(answer.get("output"), str):
        sys.stdout.buffer.write(answer["output"].encode("utf-8", "replace"))
        sys.stdout.flush()
        return answer["exit"]
    code, message = answer.get("error"), answer.get("message")
    detail = message if isinstance(message, str) and message else "refused"
    return _fail(f"{detail} ({code})" if isinstance(code, str) else detail)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
