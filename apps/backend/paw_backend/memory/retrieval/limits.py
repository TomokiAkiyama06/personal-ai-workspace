"""Limits and provisional defaults of Hybrid Retrieval (PAW-043).

Every value that comes from a caller, and every list a stage builds, is bounded
by one of these. The numbers are provisional: nothing in REQUIREMENTS.md or
MEMORY_ARCHITECTURE.md fixes them (the Context Budget is a ``[BENCHMARK]``
item), so they are collected here and in Decision 0019 (Approved) and can be
changed without a migration.
"""

from paw_backend.memory import fulltext

# The query the caller asks with.
MAX_QUERY_CHARS = 2_000
# A query is cut into terms for the keyword leg: words, and character pairs of
# Japanese text. The two bounds are defined in ``memory/fulltext.py`` (the model
# needs that module and must not import this package): more terms than
# ``MAX_QUERY_TERMS`` are dropped, in order.
MAX_QUERY_TERMS = fulltext.MAX_QUERY_TERMS
MAX_TERM_CHARS = fulltext.MAX_TERM_CHARS

# The size of the answer (Top-N) and of the candidate lists that lead to it.
DEFAULT_LIMIT = 10
MAX_LIMIT = 50
DEFAULT_KEYWORD_CANDIDATES = 50
DEFAULT_VECTOR_CANDIDATES = 50
MAX_CANDIDATES = 200
DEFAULT_RERANK_CANDIDATES = 50
MAX_RERANK_CANDIDATES = 100
# Memories pulled in only because a ``conflicts_with`` relation ties them to a
# candidate, and the relation rows read for that.
MAX_CONFLICT_PARTNERS = 20
MAX_CONFLICT_EDGES = 200

# What a caller may name.
MAX_REQUESTED_PROJECTS = 200
MAX_REQUESTED_REPOS = 200
MAX_REPO_HEADS = 200
# Memberships read for one call; a user in more projects has to narrow the
# query with ``project_ids`` instead of being silently cut off.
MAX_PROJECTS_PER_CALL = 200
MAX_REPOS_PER_CALL = 500
MAX_PROJECT_GROUPS = 200

# Text that goes to a Reranker is cut here (the answer carries the full text).
MAX_RERANK_CONTENT_CHARS = 2_000

# Time. ``timeout_seconds`` bounds a whole ``retrieve`` call (its database work
# runs on a connection that is shut down at the deadline); ``stage_timeout_seconds``
# bounds each call to a foreign component (Embedder, Reranker, policy and scope
# sources).
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_TIMEOUT_SECONDS = 60.0
DEFAULT_STAGE_TIMEOUT_SECONDS = 3.0

# Length of the commit ids a caller may name (SHA-1 and SHA-256 object names).
COMMIT_SHA_LENGTHS = (40, 64)
