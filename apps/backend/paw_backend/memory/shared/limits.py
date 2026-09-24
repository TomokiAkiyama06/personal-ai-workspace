"""Limits of Shared Memory administration (PAW-046).

Every value that comes from a caller is bounded by one of these. The database
repeats the text limits as CHECK constraints (migration ``0046``); a test fails
when the two drift apart.
"""

MAX_TITLE_CHARS = 200  # also the CHECK of memory_versions.title
MAX_CONTENT_CHARS = 20_000
MAX_REASON_CHARS = 500
MAX_MEMORY_TYPE_CHARS = 64  # also the CHECK of memory_versions.memory_type
MAX_POLICY_SUBJECTS = 20  # subjects one memory (or candidate) may declare
MAX_SUBJECT_CHARS = 100
MAX_SUBJECT_SEGMENTS = 5  # "a.b.c.d.e"
MAX_POLICY_ID_CHARS = 64
MAX_POLICY_STATEMENT_CHARS = 2_000
MAX_POLICY_ITEMS = 500  # items a policy source may return

DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200
MAX_LIST_OFFSET = 100_000

MAX_PENDING_CANDIDATES_PER_PROPOSER = 50
MAX_VERSION_NUMBER = 2_147_483_647  # the version_number column is an integer

DEFAULT_LOCK_TIMEOUT_MS = 3_000
MAX_LOCK_TIMEOUT_MS = 60_000
DEFAULT_POLICY_TIMEOUT_SECONDS = 3.0
MAX_POLICY_TIMEOUT_SECONDS = 60.0
