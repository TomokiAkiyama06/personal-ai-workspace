"""Limits of the Evidence / Claim Provenance store (PAW-052).

Every number here bounds something a caller controls. The database repeats the
text limits as CHECK constraints (migration ``0052``); ``tests/test_provenance_
migration.py`` fails when the two drift apart.

The numbers are provisional values, approved as such in Decision 0011
(2026-09-25); they may be changed here (the text limits also in the migration).
"""

from paw_backend.research.providers.contract import MAX_LOCATOR_CHARS

# --- text (counted in characters, i.e. Unicode code points) -------------------

MAX_CLAIM_TEXT_CHARS = 2000
# The same limit as ``SourceMetadata.title`` (PAW-051).
MAX_SOURCE_TITLE_CHARS = 300
# ``MAX_LOCATOR_CHARS`` (2048) is the limit of the canonical locator (PAW-051).
MAX_SOURCE_LOCATOR_CHARS = MAX_LOCATOR_CHARS

# --- counts -------------------------------------------------------------------

# Distinct sources one ``record_claim`` call may attach (after the duplicates of
# the call are merged, the limit applies to the number given).
MAX_SOURCES_PER_CALL = 20
# Source links one claim can ever have, over all calls. Bounds the size of a trace.
MAX_SOURCES_PER_CLAIM = 50
# Claims one ``add_reference`` call may attach (after duplicates are merged, the
# limit applies to the number given).
MAX_CLAIMS_PER_CALL = 50
# Claims one ``trace`` returns.
DEFAULT_TRACE_LIMIT = 100
MAX_TRACE_LIMIT = 200

# --- locks --------------------------------------------------------------------

DEFAULT_LOCK_TIMEOUT_MS = 5000
MIN_LOCK_TIMEOUT_MS = 50
MAX_LOCK_TIMEOUT_MS = 60_000
