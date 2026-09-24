"""Bounds of the token settings (``Settings.setup_token_*`` uses the same numbers).

A test keeps the two in step; this module imports nothing so that both the
redeeming and the operator side (and the settings) can share the numbers.
"""

MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 14_400  # four hours
DEFAULT_TTL_SECONDS = 1_800
MIN_MAX_ATTEMPTS = 1
MAX_MAX_ATTEMPTS = 20
DEFAULT_MAX_ATTEMPTS = 5
