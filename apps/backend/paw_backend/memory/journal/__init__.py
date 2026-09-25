"""Immediate Journal and background consolidation of Memory (PAW-041).

The tables (``models``), the enums and value objects (``domain``), the typed errors
(``errors``) and the numbers (``limits``). See ``apps/backend/README.md``
("Immediate Journal / Background Consolidation") and Decision 0018.
"""

from paw_backend.memory.journal import models

__all__ = ["models"]
