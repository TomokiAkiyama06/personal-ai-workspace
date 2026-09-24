"""Normalisation of login names.

A login name is stored in one canonical form so that two spellings of the same
name cannot be two accounts: Unicode NFKC, lower case, and a deliberately narrow
alphabet (ASCII letters and digits with ``.``, ``_``, ``-`` inside). A narrow
alphabet also rules out look-alike characters and control characters. The same
rule is a CHECK constraint on ``users.login_name`` (migration ``0021``), so a
row that skipped this function is still rejected by the database.

Rejections never quote the input.
"""

import re
import unicodedata

from paw_backend.identity.errors import InvalidLoginNameError

MIN_LENGTH = 3
MAX_LENGTH = 64
# The database CHECK constraint uses exactly this expression.
LOGIN_NAME_SQL_PATTERN = r"^[a-z0-9][a-z0-9._-]{1,62}[a-z0-9]$"
_LOGIN_NAME = re.compile(LOGIN_NAME_SQL_PATTERN)
# Refuse absurd input before normalising it (NFKC of a huge string is work).
_MAX_INPUT_LENGTH = 256


def normalize_login_name(value: object) -> str:
    """The canonical form of ``value``, or ``InvalidLoginNameError``."""
    if not isinstance(value, str) or len(value) > _MAX_INPUT_LENGTH:
        raise InvalidLoginNameError
    name = unicodedata.normalize("NFKC", value).strip().lower()
    if _LOGIN_NAME.fullmatch(name) is None:
        raise InvalidLoginNameError
    return name
