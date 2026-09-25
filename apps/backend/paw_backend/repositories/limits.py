"""Limits of the repository module (PAW-027).

Every size here bounds something a caller controls. The database repeats the
ones that are also stored (name, branch, URL and path lengths) as CHECK
constraints (migration ``0027``); ``tests/test_repositories_schema.py`` fails
when the two disagree. Raising one of those needs a new migration that replaces
the CHECK constraint (and ``models.py``); the constant alone would let the
validation accept values PostgreSQL rejects.

``MAX_REMOTES_PER_REPOSITORY`` and ``MAX_PATH_CHARS`` are the limits of the Tool
Broker's ``ScopedRepository`` (``paw_backend.tools.scope``), imported so that a
stored repository can always be turned into one.
"""

from paw_backend.tools.scope import MAX_PATH_LENGTH, MAX_REMOTES

# --- text (counted in characters, i.e. Unicode code points) --------------------

MAX_NAME_CHARS = 100  # the length GitHub allows for a repository name
MAX_BRANCH_CHARS = 200
# Shorter than the Tool Broker accepts (2048): a btree key must stay small.
MAX_REMOTE_URL_CHARS = 1024
MAX_PATH_CHARS = MAX_PATH_LENGTH
# The path is also bounded by its **encoded** (UTF-8) length: it is the key of a unique
# btree index, an entry of which cannot exceed about 2700 bytes, and 1024 characters
# can be 4096 bytes. Half of the entry limit leaves room for the index's own overhead.
# Repeated as a CHECK constraint (migration ``0027``); a longer limit needs a migration.
MAX_PATH_BYTES = 2048
# A raw string longer than this many times the limit is refused before it is
# scanned (bounds the work done for a hostile caller).
RAW_TEXT_FACTOR = 10

# --- registry sizes ----------------------------------------------------------------

MAX_REMOTES_PER_REPOSITORY = MAX_REMOTES
# Provisional (Decision 0017): the ready checkouts of one user a scope is derived
# against (every one is verified on the file system for each scope).
MAX_SCOPE_CHECKOUTS = 500
# Provisional (Decision 0017): the directories searched below a requested checkout root
# for a checkout that vanished from its path (it may have been renamed into the root).
# Only searched when some other checkout of the user changed; a root that is larger than
# this is refused, never guessed.
MAX_IDENTITY_SCAN_DIRECTORIES = 20_000
# Provisional (Decision 0017): the repositories one project can hold.
MAX_REPOSITORIES_PER_PROJECT = 100
# The readable part of a project's directory name (``<slug>-<8 hex of the id>``).
MAX_PROJECT_SLUG_CHARS = 40

# --- listing, purging, locking ----------------------------------------------------

DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200
MAX_LIST_OFFSET = 100_000
MAX_PURGE_PROJECTS = 500
DEFAULT_LOCK_TIMEOUT_MS = 3000
MIN_LOCK_TIMEOUT_MS = 1
MAX_LOCK_TIMEOUT_MS = 60_000

# --- git -----------------------------------------------------------------------------

# What one git command may write to stdout or stderr before it is stopped.
MAX_GIT_OUTPUT_BYTES = 65_536
DEFAULT_GIT_TIMEOUT_S = 30.0
DEFAULT_CLONE_TIMEOUT_S = 900.0
MAX_GIT_TIMEOUT_S = 7_200.0
# A pending checkout older than this many clone timeouts is stale (its process
# died): the next attempt takes it over.
PENDING_TIMEOUT_FACTOR = 2.0

# --- accounts and roots --------------------------------------------------------------

# The first uid of a human account on Ubuntu (``UID_MIN`` of ``/etc/login.defs``).
DEFAULT_MIN_LINUX_UID = 1000
MAX_ROOT_TEMPLATES = 8
MAX_CLONE_HOSTS = 8
