"""Limits and time rules of the Research Scratch Store.

Every size here bounds something a caller controls. The database repeats the
text limits and the TTL as CHECK constraints (migration ``0050``); the values in
this module are the contract of the service layer, and
``tests/test_scratch_records.py`` checks that the two agree.
"""

from datetime import UTC, datetime, timedelta

# --- time ------------------------------------------------------------------

# Research Scratch lives for created_at + 24 hours (REQUIREMENTS.md "Research
# Scratch"). The database enforces ``expires_at = created_at + interval '24 hours'``.
SCRATCH_TTL = timedelta(hours=24)

DEFAULT_LEASE_SECONDS = 300
MIN_LEASE_SECONDS = 1
# A lease never lasts longer than an hour (a CHECK constraint enforces it too):
# a crashed worker can keep an item alive for at most this long.
MAX_LEASE_SECONDS = 3600
# Distinct holders whose leases are active at the same time on one item.
MAX_ACTIVE_LEASES_PER_ITEM = 16

# --- item text (counted in characters, i.e. Unicode code points) --------------

MAX_QUERY_CHARS = 1000
MAX_TITLE_CHARS = 500
MAX_SUMMARY_CHARS = 8000
MAX_CONTENT_CHARS = 100_000

# --- source metadata (a JSON object) ---------------------------------------

# Size of the compact UTF-8 JSON text: ``json.dumps(value, ensure_ascii=False,
# separators=(",", ":"), allow_nan=False).encode("utf-8")``.
MAX_SOURCE_METADATA_BYTES = 16_384
# Nesting depth; the top-level object is depth 1.
MAX_SOURCE_METADATA_DEPTH = 6
MAX_SOURCE_METADATA_KEY_CHARS = 128
# Integers must be exactly representable as a JSON number in every client.
MAX_JSON_INT = 2**53 - 1
# Floats must be finite, and either 0 or 1e-6 <= abs(value) < 1e15. PostgreSQL
# stores JSON numbers as decimals, so an extreme exponent (1e300) would expand
# to hundreds of digits and defeat the size bound.
MIN_JSON_FLOAT_MAGNITUDE = 1e-6
MAX_JSON_FLOAT_MAGNITUDE = 1e15

# --- listing and purging ----------------------------------------------------

DEFAULT_LIST_LIMIT = 100
MAX_LIST_LIMIT = 200
DEFAULT_PURGE_BATCH_SIZE = 500
MAX_PURGE_BATCH_SIZE = 5000


def utc_now() -> datetime:
    """The default clock: the current instant as an aware UTC datetime."""
    return datetime.now(UTC)


def expiry_of(created_at: datetime) -> datetime:
    """``created_at + 24 hours``: the only way ``expires_at`` is ever derived."""
    return created_at + SCRATCH_TTL
