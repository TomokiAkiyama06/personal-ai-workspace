"""Immediate Journal and background consolidation of Memory (PAW-041).

The tables (``models``), the enums and value objects (``domain``), the typed errors
(``errors``), the numbers (``limits``), the Memory Worker contract (``worker``) and
the consolidator's decisions (``rules``). See ``apps/backend/README.md``
("Immediate Journal / Background Consolidation") and Decision 0018.
"""

from paw_backend.memory.journal import models
from paw_backend.memory.journal.rules import Backoff
from paw_backend.memory.journal.worker import (
    MemoryWorker,
    WorkerMemory,
    parse_worker_output,
)

__all__ = [
    "Backoff",
    "MemoryWorker",
    "WorkerMemory",
    "models",
    "parse_worker_output",
]
