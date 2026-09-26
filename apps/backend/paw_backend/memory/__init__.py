"""Memory / Conversation schema (PAW-040): tables, ACL conditions, pgvector type.

This package is the PostgreSQL schema, plus ``shared/`` (the administration of
Shared Memory, PAW-046) and ``retrieval/`` (Hybrid Retrieval, PAW-043: permission
first, keyword + vector, rerank, dedup and conflicts). There are no other
repositories or services here: writing memories, consolidation (PAW-041),
conflict handling (PAW-042) and Markdown projection (PAW-045) come later and build
on these tables. See ``models.py`` for the layout and ``acl.py`` for how
permissions are filtered in SQL; ``fulltext.py`` is the full-text document of the
keyword search (its index is revision 0043).

``metadata.py`` names the actor of a pin / importance / status / stale-state
change, which a trigger records in ``memory_metadata_changes`` (REQUIREMENTS.md
"Manual Memory Editing"; the status and stale state since revision 0071).

Users, projects and repositories are plain UUID columns without foreign keys,
because those tables do not exist yet (PAW-021 / PAW-026 / PAW-027). The
embedding model, and so the vector dimension, is not chosen yet (PAW-019), so
the ``vector`` column has no fixed dimension and there is no ANN index (Decision
0019 adds one only after the model is chosen, keeping the permission filter).
"""

from paw_backend.memory import models
from paw_backend.memory.acl import (
    Principal,
    readable_conversations,
    readable_memory_versions,
)
from paw_backend.memory.metadata import metadata_change_actor

__all__ = [
    "Principal",
    "metadata_change_actor",
    "models",
    "readable_conversations",
    "readable_memory_versions",
]
