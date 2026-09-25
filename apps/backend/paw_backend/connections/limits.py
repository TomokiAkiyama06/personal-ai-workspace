"""Limits of the shared connection module (PAW-030).

Every size here bounds something a caller, an adapter or an operator controls.
The database repeats the ones that are stored (``MAX_MODEL_CHARS``,
``MAX_QUOTA_LIMIT``, the token and duration ranges) as CHECK constraints
(migration ``0030``); ``tests/test_connections_schema.py`` fails when the two
disagree. Raising a stored limit therefore needs a new migration that replaces
the CHECK constraint: the constant alone would let validation accept values
PostgreSQL rejects. The others (prompt, secret, result and list sizes, timeouts)
are enforced by the code only and can be changed alone.
"""

import re

# --- text (counted in characters, i.e. Unicode code points) -----------------------

# A model name as a routing table writes it (``gpt-5-codex``, ``claude-opus-4``,
# ``vendor/model:tag``). Never free text: the charset keeps a prompt or a secret
# from being smuggled into a usage row through this column.
MAX_MODEL_CHARS = 100
MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}")
# What an agent asked for. Not stored, not logged: it only travels to the adapter.
MAX_PROMPT_CHARS = 500_000
# What an adapter may answer with (a longer answer is an invalid response).
MAX_RESULT_CHARS = 2_000_000
# A credential as the secret store hands it to an adapter.
MAX_SECRET_CHARS = 16_384

# --- numbers ------------------------------------------------------------------------

# The largest quota limit (a bigint column). One trillion tokens is far beyond any
# real subscription; a larger value is a typing mistake, not a policy.
MAX_QUOTA_LIMIT = 10**12
# The tokens one call may report (input and output each) and the duration it may
# be recorded with. Both are CHECK constraints of ``connection_usage``.
MAX_TOKENS_PER_CALL = 10**9
MAX_DURATION_MS = 10**12  # about 31 years: a bound, not an expectation

# --- time -----------------------------------------------------------------------------

# The most a database call of this module waits, in total (a free connection, a
# row lock and the statements). Past it the call fails with ``ConnectionBusyError``.
DEFAULT_DATABASE_TIMEOUT_SECONDS = 5.0
MAX_DATABASE_TIMEOUT_SECONDS = 60.0
# The most one adapter call may run. Bounds the runtime of a call until the task's
# own budget (PAW-033) takes over; a caller may pass a shorter one per request.
DEFAULT_CALL_TIMEOUT_SECONDS = 600.0
MAX_CALL_TIMEOUT_SECONDS = 86_400.0
DEFAULT_HEALTH_TIMEOUT_SECONDS = 30.0
MAX_HEALTH_TIMEOUT_SECONDS = 600.0
# The rolling window that mirrors the 5-hour window of the subscription plans.
ROLLING_WINDOW_HOURS = 5

# --- listing ---------------------------------------------------------------------

DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200
MAX_LIST_OFFSET = 100_000
