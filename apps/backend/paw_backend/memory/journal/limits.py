"""Numeric limits and policy defaults of the Immediate Journal (PAW-041).

REQUIREMENTS.md does not state any of these numbers; they are provisional and
proposed in Decision 0018
(``docs/decisions/0018-memory-journal-consolidation-policy.md``).
They live in this one module so that a changed value is a code change without a
schema change, except the ones a CHECK constraint repeats (marked below; the
migration writes the number out and ``tests/test_journal_migration.py`` compares
the two).
"""

# -- Raw messages ---------------------------------------------------------------
# A user message becomes the text a Memory Worker reads, so it is bounded more
# tightly than the output of a tool or an agent, which is Raw Conversation only.
MAX_USER_MESSAGE_CHARS = 100_000
MAX_OTHER_MESSAGE_CHARS = 1_000_000

# -- What a Memory Worker may return (memory-worker-output-v1) -------------------
MAX_RAW_OUTPUT_CHARS = 400_000  # the JSON text, before it is parsed
MAX_MEMORIES_PER_OUTPUT = 20
MAX_KEY_CHARS = 200  # a memory version's title (CHECK 1..200) is the key
MAX_CONTENT_CHARS = 8_000
MAX_CONFLICTS_PER_MEMORY = 10

# -- Reading -----------------------------------------------------------------------
DEFAULT_PENDING_LIMIT = 50
MAX_PENDING_LIMIT = 200

# -- Queue: lease, retry, dead letter ------------------------------------------------
DEFAULT_LEASE_SECONDS = 300
MAX_LEASE_SECONDS = 86_400
# A failure that counts (invalid output, worker error, timeout) is retried this many
# times in all; the last one dead-letters the job (``status = 'dead'``). The
# observation itself stays ``pending``: nothing is lost, it can be enqueued again.
DEFAULT_MAX_ATTEMPTS = 5
MAX_MAX_ATTEMPTS = 100
# Delay before a failed job may be claimed again: ``base * factor ** (n - 1)`` for
# the n-th failure, at most ``max``. A worker that is unavailable (GPU stopped) is
# deferred with the same formula on its own counter and is never dead-lettered.
DEFAULT_BACKOFF_BASE_SECONDS = 30
DEFAULT_BACKOFF_FACTOR = 2
DEFAULT_BACKOFF_MAX_SECONDS = 900
MAX_BACKOFF_SECONDS = 86_400
MAX_BACKOFF_FACTOR = 10

# -- Runner --------------------------------------------------------------------------
DEFAULT_BATCH_SIZE = 10
MAX_BATCH_SIZE = 1_000
# The worker call must end well inside the lease: the runner does not heartbeat
# while it waits, so the lease has to outlive the call and the write that follows.
DEFAULT_WORKER_TIMEOUT_SECONDS = 120.0
MAX_WORKER_TIMEOUT_SECONDS = 3_600.0
MIN_LEASE_TO_TIMEOUT_RATIO = 2

# -- Database ------------------------------------------------------------------------
DEFAULT_LOCK_TIMEOUT_MS = 3_000
MAX_LOCK_TIMEOUT_MS = 60_000
MAX_WORKER_ID_CHARS = 100
MAX_JOB_ID = 2**63 - 1
MAX_CLAIM_COUNT = 2**31 - 1
