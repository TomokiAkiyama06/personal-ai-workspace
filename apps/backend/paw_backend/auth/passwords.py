"""Password policy and Argon2id hashing.

**Policy** (``validate_new_password``): at least 10 characters (REQUIREMENTS.md),
long passphrases allowed, no composition rules, and a short list of refusals a
strong policy still needs (NIST SP 800-63B): a password everybody tries, one that
is a single repeated pattern, one that is or contains the login name. A password
is NFKC-normalised before it is checked and hashed, so the same characters typed
on another device (full-width forms, composed / decomposed kana) verify. The
numbers are in ``limits`` and are decided in Decision 0015 (Approved).

**Hashing** (``PasswordHasher``): Argon2id through ``argon2-cffi``. Every call
runs on a small dedicated thread pool (Argon2 is CPU- and memory-bound and must
not block the event loop); at most ``concurrency`` hashes run at once, so the
memory in use is bounded, and no more than ``PASSWORD_HASH_MAX_PENDING`` wait.
A job that is cancelled while it runs finishes on its thread (it cannot be
interrupted) but nothing waits for it.

Verifying an unknown account is made to cost the same as a real one by verifying
against a fixed dummy hash made with the same parameters (``verify_unknown``).
"""

import asyncio
import concurrent.futures
import threading
import unicodedata
from dataclasses import dataclass

from argon2 import PasswordHasher as _Argon2
from argon2 import Type, extract_parameters
from argon2.exceptions import InvalidHashError, VerificationError

from paw_backend.auth.errors import (
    AuthUnavailableError,
    InvalidAuthInputError,
    PasswordPolicyError,
    PasswordProblem,
)
from paw_backend.auth.limits import (
    ARGON2_HASH_BYTES,
    ARGON2_MAX_MEMORY_KIB,
    ARGON2_SALT_BYTES,
    PASSWORD_HASH_MAX_PENDING,
    PASSWORD_LOGIN_NAME_CONTAINED_FROM,
    PASSWORD_MAX_LENGTH,
    PASSWORD_MAX_UTF8_BYTES,
    PASSWORD_MIN_LENGTH,
)
from paw_backend.config import Settings

_ARGON2ID_PREFIX = "$argon2id$"
# Code points that cannot be part of a password: controls (NUL, newline, tab),
# surrogates (they cannot be encoded) and unassigned code points (their
# normalisation may change with the Unicode version).
_REFUSED_CATEGORIES = frozenset({"Cc", "Cs", "Cn"})

# Passwords of at least 10 characters that appear in every breach list. Not a
# substitute for a breach corpus (a later issue may add one), but it stops the
# ones an attacker tries first. Compared case-folded after normalisation.
COMMON_PASSWORDS = frozenset(
    {
        "0123456789",
        "01234567890",
        "0987654321",
        "1029384756",
        "1111111111",
        "11111111111",
        "12345678910",
        "123456789012",
        "1234567890",
        "12345678901",
        "123456789a",
        "123456789q",
        "1234567890123",
        "12345qwert",
        "1q2w3e4r5t",
        "1q2w3e4r5t6y",
        "1qaz2wsx3edc",
        "1qaz@wsx3edc",
        "abcdefghij",
        "abcdefghijk",
        "abcd123456",
        "abc1234567",
        "abcdef123456",
        "administrator",
        "adminadmin",
        "admin12345",
        "admin123456",
        "asdfasdfasdf",
        "asdfghjkl1",
        "asdfghjklqwerty",
        "changeme123",
        "chocolate1",
        "computer123",
        "football123",
        "internet123",
        "iloveyou123",
        "iloveyou12",
        "letmein1234",
        "letmein123",
        "letmeinnow",
        "masterkey123",
        "monkey1234",
        "mypassword",
        "mypassword1",
        "mypassword123",
        "passw0rd123",
        "password01",
        "password1!",
        "password10",
        "password11",
        "password12",
        "password123",
        "password1234",
        "password12345",
        "password!23",
        "pass123456",
        "passwordpassword",
        "qazwsxedcrfv",
        "qwer123456",
        "qwerty1234",
        "qwerty12345",
        "qwerty123456",
        "qwertyuiop",
        "qwertyuiop1",
        "qwertyuiop123",
        "qwertyuiopasdfghjkl",
        "superman123",
        "sunshine123",
        "trustno1234",
        "welcome123",
        "welcome1234",
        "zaq12wsxcde",
        "zxcvbnm123",
        "zxcvbnmasdf",
        "1234qwerasdf",
        "9876543210",
        "987654321a",
        "aaaaaaaaaa",
        "1234abcd1234",
        "changeit123",
        "letmein!!!!",
        "p@ssw0rd123",
        "p@ssword123",
        "p@ssw0rd!23",
        "password@123",
        "passw0rd!!!",
        "pa$$w0rd123",
    }
)


def normalize_password(password: object) -> str:
    """NFKC-normalised ``password`` (or a typed error).

    Checks only what makes a *string* unusable as a password (type, length caps,
    refused code points), so it is the entry check of both hashing and
    verifying. Whether a *new* password is good enough is
    ``validate_new_password``.
    """
    if not isinstance(password, str):
        raise InvalidAuthInputError("password")
    # Bound the work before normalising: NFKC of a huge string is work too.
    if len(password) > PASSWORD_MAX_UTF8_BYTES:
        raise PasswordPolicyError(PasswordProblem.TOO_LONG)
    if any(
        unicodedata.category(character) in _REFUSED_CATEGORIES for character in password
    ):
        raise PasswordPolicyError(PasswordProblem.INVALID_CHARACTER)
    normalized = unicodedata.normalize("NFKC", password)
    if len(normalized) > PASSWORD_MAX_LENGTH or (
        len(normalized.encode("utf-8")) > PASSWORD_MAX_UTF8_BYTES
    ):
        raise PasswordPolicyError(PasswordProblem.TOO_LONG)
    return normalized


def validate_new_password(password: object, login_name: str | None = None) -> str:
    """The normalised password if it meets the policy; else ``PasswordPolicyError``.

    ``login_name`` (the account's normalised name, when known) is used to refuse a
    password that is or contains it. The error names the rule, never the input.
    """
    normalized = normalize_password(password)
    if len(normalized) < PASSWORD_MIN_LENGTH:
        raise PasswordPolicyError(PasswordProblem.TOO_SHORT)
    folded = normalized.casefold()
    if folded in COMMON_PASSWORDS or _is_a_repeated_pattern(folded):
        raise PasswordPolicyError(PasswordProblem.TOO_COMMON)
    if login_name is not None:
        if not isinstance(login_name, str):
            raise InvalidAuthInputError("login_name")
        name = login_name.casefold()
        if name and (
            folded == name
            or (len(name) >= PASSWORD_LOGIN_NAME_CONTAINED_FROM and name in folded)
        ):
            raise PasswordPolicyError(PasswordProblem.CONTAINS_LOGIN_NAME)
    return normalized


def _is_a_repeated_pattern(folded: str) -> bool:
    """``aaaaaaaaaa``, ``1212121212``, ``abcabcabcabc``: three characters or fewer."""
    return len(set(folded)) <= 3


@dataclass(frozen=True, slots=True)
class HashParameters:
    """The Argon2id cost parameters (validated by ``Settings``)."""

    time_cost: int
    memory_kib: int
    parallelism: int

    @classmethod
    def from_settings(cls, settings: Settings) -> "HashParameters":
        return cls(
            settings.password_hash_time_cost,
            settings.password_hash_memory_kib,
            settings.password_hash_parallelism,
        )


class PasswordHasher:
    """Argon2id hashing and verification off the event loop, with a bounded pool."""

    def __init__(self, parameters: HashParameters, *, concurrency: int = 2) -> None:
        if not isinstance(parameters, HashParameters):
            raise TypeError("parameters must be HashParameters")
        if isinstance(concurrency, bool) or not isinstance(concurrency, int):
            raise TypeError("concurrency must be an int")
        if not 1 <= concurrency <= 16:
            raise ValueError("concurrency must be from 1 to 16")
        self.parameters = parameters
        self._argon2 = _Argon2(
            time_cost=parameters.time_cost,
            memory_cost=parameters.memory_kib,
            parallelism=parameters.parallelism,
            hash_len=ARGON2_HASH_BYTES,
            salt_len=ARGON2_SALT_BYTES,
            type=Type.ID,
        )
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=concurrency, thread_name_prefix="paw-argon2"
        )
        self._concurrency = concurrency
        self._pending = 0
        self._lock = threading.Lock()
        self._dummy: str | None = None
        self._closed = False

    @classmethod
    def from_settings(cls, settings: Settings) -> "PasswordHasher":
        return cls(
            HashParameters.from_settings(settings),
            concurrency=settings.password_hash_concurrency,
        )

    def close(self) -> None:
        """Stop the worker threads (a running hash finishes, queued ones go)."""
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)

    async def hash(self, password: str) -> str:
        """The Argon2id encoding of a password that has passed the policy."""
        password = normalize_password(password)
        return await self._submit(self._argon2.hash, password)

    async def verify(self, encoded: str, password: str) -> bool:
        """Whether ``password`` matches ``encoded``. ``False`` for anything else.

        A hash that is not a well-formed Argon2id encoding, or that asks for
        more memory than a setting could allow, is a mismatch (never verified).
        A mismatch is not an error. A password that cannot be a password (wrong
        type, refused characters, too long) raises the errors of
        ``normalize_password``.
        """
        password = normalize_password(password)
        if not _acceptable_encoding(encoded):
            return False
        return await self._submit(self._verify, encoded, password)

    async def verify_unknown(self, password: str) -> None:
        """Spend the time of a verification (for an account that cannot log in)."""
        password = normalize_password(password)
        dummy = await self._dummy_hash()
        await self._submit(self._verify, dummy, password)

    def needs_rehash(self, encoded: str) -> bool:
        """Whether ``encoded`` was made with other parameters than the current ones."""
        if not _acceptable_encoding(encoded):
            return True
        return self._argon2.check_needs_rehash(encoded)

    async def warm(self) -> None:
        """Make the dummy hash now (at start-up), not at the first unknown login."""
        await self._dummy_hash()

    async def _dummy_hash(self) -> str:
        if self._dummy is None:
            # A fixed password nobody can present: it is not 10+ characters of
            # anything a user types, and the hash is only ever compared with.
            self._dummy = await self._submit(
                self._argon2.hash, "paw-dummy-password-for-unknown-accounts"
            )
        return self._dummy

    def _verify(self, encoded: str, password: str) -> bool:
        try:
            return self._argon2.verify(encoded, password)
        except (VerificationError, InvalidHashError):
            return False

    async def _submit[T](self, function, *arguments) -> T:
        if self._closed:
            raise AuthUnavailableError
        with self._lock:
            if self._pending >= self._concurrency + PASSWORD_HASH_MAX_PENDING:
                raise AuthUnavailableError
            self._pending += 1
        try:
            future = self._executor.submit(function, *arguments)
        except RuntimeError:  # the executor was shut down
            with self._lock:
                self._pending -= 1
            raise AuthUnavailableError from None
        # The count goes down when the job has really ended (or was dropped),
        # not when a cancelled caller stops waiting for it.
        future.add_done_callback(self._job_ended)
        try:
            return await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if future.cancelled() and task is not None and not task.cancelling():
                # Dropped by ``close()``, not cancelled by the caller.
                raise AuthUnavailableError from None
            raise

    def _job_ended(self, _future: concurrent.futures.Future) -> None:
        with self._lock:
            self._pending -= 1


def _acceptable_encoding(encoded: object) -> bool:
    if not isinstance(encoded, str) or not encoded.startswith(_ARGON2ID_PREFIX):
        return False
    try:
        parameters = extract_parameters(encoded)
    except (InvalidHashError, ValueError):
        return False
    return parameters.memory_cost <= ARGON2_MAX_MEMORY_KIB
