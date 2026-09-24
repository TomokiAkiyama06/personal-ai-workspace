"""Memory / Conversation schema (PAW-040): tables, ACL conditions, pgvector type.

This package is the PostgreSQL schema only, apart from ``shared/`` (the
administration of Shared Memory, PAW-046). There are no other repositories or
services here: writing memories, consolidation (PAW-041), conflict handling
(PAW-042), retrieval (PAW-043) and Markdown projection (PAW-045) come later and
build on these tables. See ``models.py`` for the layout and ``acl.py`` for how
permissions are filtered in SQL.

Users, projects and repositories are plain UUID columns without foreign keys,
because those tables do not exist yet (PAW-021 / PAW-026 / PAW-027). The
embedding model, and so the vector dimension, is not chosen yet (PAW-019), so
the ``vector`` column has no fixed dimension and there is no ANN index (PAW-043).
"""

from paw_backend.memory import models
from paw_backend.memory.acl import (
    Principal,
    readable_conversations,
    readable_memory_versions,
)

__all__ = [
    "Principal",
    "models",
    "readable_conversations",
    "readable_memory_versions",
]
