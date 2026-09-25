"""The numbers of the login, session and password code that are not settings.

The values the requirements do not fix are collected in Decision 0015 with a
recommendation for each. The ones an operator may want to tune are settings
(``paw_backend.config``); the rest, which are limits on the *shape* of input or
on what is stored, are constants here so that a schema or a test can name them.
"""

# REQUIREMENTS.md "Password / Login Failure / Session Policy": at least 10
# characters, long passphrases allowed, no composition rules.
PASSWORD_MIN_LENGTH = 10
# Proposed (Decision 0015). Argon2 accepts far more; the cap bounds the work an
# anonymous request can cause before it is even hashed.
PASSWORD_MAX_LENGTH = 256
PASSWORD_MAX_UTF8_BYTES = 1_024
# A password that contains the login name is refused only when the name is at
# least this long (a 3-letter name inside a long passphrase is not a weakness).
PASSWORD_LOGIN_NAME_CONTAINED_FROM = 6

# What a client may send as a login name or a device label. The login name has
# its own strict format (``paw_backend.identity.login_name``); this bounds the
# raw input before it is normalised.
LOGIN_NAME_MAX_INPUT = 128
DEVICE_LABEL_MAX_LENGTH = 64

# Argon2id output and salt (RFC 9106 recommends 128-bit salt and 256-bit tag).
ARGON2_HASH_BYTES = 32
ARGON2_SALT_BYTES = 16
# A hash stored in the database is refused (never verified) if it asks for more
# memory than a setting could ever allow: a tampered row cannot be used to make
# the server allocate gigabytes.
ARGON2_MAX_MEMORY_KIB = 1_048_576
# Hash jobs waiting for a worker at most this many, beyond the ones running:
# more are refused (503) instead of queued without limit.
PASSWORD_HASH_MAX_PENDING = 64

# A session id is 32 random bytes (256 bits) from the operating system's CSPRNG,
# sent as URL-safe base64 without padding (43 characters). Only its SHA-256 is
# stored.
SESSION_TOKEN_BYTES = 32
SESSION_TOKEN_LENGTH = 43
# ``__Host-`` makes the browser refuse the cookie unless it is Secure, has no
# Domain and has Path=/ (it cannot be planted by a sibling sub-domain).
SESSION_COOKIE_NAME = "__Host-paw_session"

# Sessions and throttle rows that ended more than this long ago are deleted
# (opportunistically, in small batches); a shorter memory would lose the
# "last used" list of a device the user may still want to look at.
SESSION_RETENTION_SECONDS = 30 * 24 * 3600
PURGE_BATCH = 50

# The most a caller can ask to list.
SESSION_LIST_LIMIT = 100

# The most an authentication request body may hold (the biggest legitimate one is
# a password of at most 1,024 characters and a few short fields).
AUTH_BODY_MAX_BYTES = 16 * 1024
