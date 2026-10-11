#!/usr/bin/env -S python3 -I
"""The file helper of the tool sandbox (Decision 0083, 2): ``repo.file.write`` and
``repo.file.str_replace`` run this, inside the container, as the task creator's
account (installed in the image as ``/usr/local/lib/paw/paw-file-tool``).

    paw-file-tool write <path>         stdin: {"content": "..."}
    paw-file-tool str-replace <path>   stdin: {"old": "...", "new": "..."}

It answers with ONE JSON object on stdout: ``{"ok": true, "chars": <n>}`` (exit
0), or ``{"error": "<code>"}`` (exit 3; ``occurrences`` also says ``count``).
The semantics are the benchmark harness's: ``write`` makes the parent
directories and replaces the whole file; ``str-replace`` replaces ``old`` only
when it occurs exactly once. The bytes of the file are kept as they are (no
newline translation). Only what the sandbox mounts can be reached: a path the
backend masks is an empty read-only tmpfs or file, so a write there fails
(``not_writable``). Standard library only.
"""

import json
import os
import sys

MAX_INPUT_BYTES = 1024 * 1024
FAILED = 3


def _answer(document: dict, code: int) -> int:
    sys.stdout.write(json.dumps(document) + "\n")
    return code


def _read_input() -> dict | None:
    data = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(data) > MAX_INPUT_BYTES:
        return None
    try:
        document = json.loads(data.decode("utf-8"))
    except ValueError:
        return None
    return document if isinstance(document, dict) else None


def write(path: str, document: dict) -> int:
    content = document.get("content")
    if not isinstance(content, str):
        return _answer({"error": "bad_input"}, FAILED)
    if os.path.isdir(path):
        return _answer({"error": "not_a_file"}, FAILED)
    try:
        data = content.encode("utf-8")
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)
    except UnicodeEncodeError:
        return _answer({"error": "bad_input"}, FAILED)
    except OSError:
        return _answer({"error": "not_writable"}, FAILED)
    return _answer({"ok": True, "chars": len(content)}, 0)


def str_replace(path: str, document: dict) -> int:
    old, new = document.get("old"), document.get("new")
    if not isinstance(old, str) or not isinstance(new, str):
        return _answer({"error": "bad_input"}, FAILED)
    if not old:
        return _answer({"error": "occurrences", "count": 0}, FAILED)
    if os.path.isdir(path):
        return _answer({"error": "not_a_file"}, FAILED)
    try:
        with open(path, "rb") as handle:
            text = handle.read().decode("utf-8")
    except FileNotFoundError:
        return _answer({"error": "not_found"}, FAILED)
    except UnicodeDecodeError:
        return _answer({"error": "not_utf8"}, FAILED)
    except OSError:
        return _answer({"error": "not_readable"}, FAILED)
    count = text.count(old)
    if count != 1:
        return _answer({"error": "occurrences", "count": count}, FAILED)
    try:
        updated = text.replace(old, new, 1).encode("utf-8")
        with open(path, "wb") as handle:
            handle.write(updated)
    except UnicodeEncodeError:
        return _answer({"error": "bad_input"}, FAILED)
    except OSError:
        return _answer({"error": "not_writable"}, FAILED)
    return _answer({"ok": True, "chars": len(new)}, 0)


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in ("write", "str-replace"):
        return _answer({"error": "usage"}, 2)
    document = _read_input()
    if document is None:
        return _answer({"error": "bad_input"}, FAILED)
    if argv[0] == "write":
        return write(argv[1], document)
    return str_replace(argv[1], document)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
