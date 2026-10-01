"""The host's available memory, read before a GPU runtime starts (issue #182).

Decision 0039, 4 (Approved): the first load of a vLLM runtime may build JIT
kernels (FlashInfer) on the host, and on 2026-09-30 such a build, at ``ninja``'s
default parallelism, took about 75 GiB of host RAM and the machine rebooted.
``CommandModelControl`` reads ``MemAvailable`` here and does not start a GPU
runtime below its minimum (the value: Decision 0072, Proposed).

Only ``/proc/meminfo`` is read; nothing is written, and no process is looked at.
"""

import os
import re

MEMINFO_PATH = "/proc/meminfo"
_MEM_AVAILABLE = re.compile(r"^MemAvailable:[ \t]+([0-9]{1,15}) kB$", re.MULTILINE)
_MAX_MEMINFO_BYTES = 64 * 1024


def parse_mem_available(text: str) -> int:
    """``MemAvailable`` of a ``/proc/meminfo`` text, in bytes. ``ValueError``
    unless the text has exactly one such line, in kB."""
    found = _MEM_AVAILABLE.findall(text)
    if len(found) != 1 or text.count("MemAvailable:") != 1:
        raise ValueError("MemAvailable is not readable")
    return int(found[0]) * 1024


def read_mem_available_bytes(path: str | os.PathLike[str] = MEMINFO_PATH) -> int:
    """The host's ``MemAvailable`` in bytes. Raises ``OSError`` when the file
    cannot be read and ``ValueError`` when it has no usable value."""
    with open(path, encoding="ascii", errors="replace") as meminfo:
        return parse_mem_available(meminfo.read(_MAX_MEMINFO_BYTES))
