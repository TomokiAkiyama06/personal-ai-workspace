"""Limits and provisional defaults of Memory versioning and freshness (PAW-042).

Every value that comes from a caller is bounded by one of these. The text limits
repeat the CHECK constraints of ``memory_versions`` (revisions 0040 and 0147)
where one exists; the others are provisional (Decision 0034) and can change
without a migration.
"""

from datetime import timedelta

MAX_TITLE_CHARS = 200  # also the CHECK of memory_versions.title
MAX_CONTENT_CHARS = 20_000  # also the CHECK of memory_versions.content (0147)
MAX_MEMORY_TYPE_CHARS = 64  # also the CHECK of memory_versions.memory_type
MAX_REASON_CHARS = 500
MAX_BRANCH_CHARS = 255
DEFAULT_IMPORTANCE = 50

# ``revalidate``: how long a verified fact stays fresh (the requirements' example is
# 90 days). Shorter than an hour would turn a memory stale while it is being used;
# longer than ten years is a ``permanent`` memory.
MIN_REVALIDATE_AFTER = timedelta(hours=1)
MAX_REVALIDATE_AFTER = timedelta(days=3650)
# ``expiring``: how far ahead an expiry may be set.
MAX_EXPIRES_IN = timedelta(days=3650)

MAX_VERSION_NUMBER = 2_147_483_647  # the version_number column is an integer
COMMIT_SHA_LENGTHS = (40, 64)  # SHA-1 and SHA-256 object names

# The freshness maintenance handles at most this many versions per call; a caller
# repeats the call until it reports 0.
DEFAULT_SWEEP_BATCH = 500
MAX_SWEEP_BATCH = 10_000

DEFAULT_LOCK_TIMEOUT_MS = 3_000
MAX_LOCK_TIMEOUT_MS = 60_000

# How many times a write re-locks the current version when another writer (the
# Journal) committed a newer one while it waited; then it is ``MemoryBusyError``.
CURRENT_VERSION_RETRIES = 5
