"""Limits and provisional values of the Inferred Preference flow (PAW-044).

Every value that comes from a caller or a model is bounded by one of these. They
are provisional (Decision 0081) and change without a migration.
"""

# The structured preview of a free-text answer.
MAX_FREE_TEXT_CHARS = 2_000
MAX_RULE_CHARS = 2_000
MAX_EXCEPTIONS = 10
MAX_EXCEPTION_CHARS = 500
MAX_APPLY_TO_CHARS = 200
MAX_INTERPRETER_OUTPUT_CHARS = 20_000
# How long the interpreter (a model) may take before the rule interpreter answers.
INTERPRETER_TIMEOUT_SECONDS = 20.0

# The candidate list: at most this many memory candidates and held keys, newest
# first, and at most this many observations per key are read for the evidence.
MAX_CANDIDATES = 200
MAX_OBSERVATIONS_PER_KEY = 100
