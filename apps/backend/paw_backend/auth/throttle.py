"""Progressive backoff and rate limits, shared by the login and the token endpoint.

One counter per (scope, hashed key) in ``auth_throttles``. An attempt is
**reserved before it is judged**: one statement counts it and, when the count
reaches the scope's threshold, sets the lock for that count (the ``n``-th lock
lasts the ``n``-th entry of the schedule, the last entry repeating). The attempt
that set a lock still runs (it is the one that may succeed); every attempt that
arrives while the lock is set is refused without being looked at. So

* however many requests arrive at once, at most ``free_attempts`` passwords are
  ever compared before the lock exists (each request gets its own count from
  the same atomic statement); this is what makes the backoff hold under
  concurrency, and it is tested with racing requests on separate connections;
* an attempt that is counted and never finished (a crash) is not forgotten,
  which can only make a lock come earlier;
* a lock is never permanent: the schedule is bounded (settings: at most a day
  per entry), and a count is forgotten after ``decay_seconds`` without an
  attempt or a lock. The key is a hash: an unknown login name is throttled
  exactly like a real one, so the throttle does not tell which accounts exist.

What a success does to the counter is the scope's ``on_success``: ``reset`` (the
person who knows the password has shown they are the account's owner), ``refund``
(the attempt no longer counts, but the others still do: a source that succeeded
once has not shown that its failed attempts on other accounts were innocent) or
``keep`` (the token endpoint counts every attempt).

Time: the "now" of a statement is the later of the caller's clock (a test moves
it) and the database's ``clock_timestamp()``, so a caller whose clock is behind
cannot shorten a lock.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.auth.db import run
from paw_backend.auth.errors import InvalidAuthInputError, ThrottledError
from paw_backend.auth.limits import PURGE_BATCH
from paw_backend.auth.models import ThrottleScope
from paw_backend.config import Settings
from paw_backend.db import Database


class OnSuccess(StrEnum):
    RESET = "reset"
    REFUND = "refund"
    KEEP = "keep"


@dataclass(frozen=True, slots=True)
class ThrottlePolicy:
    free_attempts: int
    backoff_seconds: tuple[int, ...]
    decay_seconds: int
    on_success: OnSuccess

    def __post_init__(self) -> None:
        if isinstance(self.free_attempts, bool) or not isinstance(
            self.free_attempts, int
        ):
            raise TypeError("free_attempts must be an int")
        if self.free_attempts < 1:
            raise ValueError("free_attempts must be at least 1")
        schedule = tuple(self.backoff_seconds)
        if not schedule or not all(
            isinstance(s, int) and not isinstance(s, bool) and 1 <= s <= 86_400
            for s in schedule
        ):
            raise ValueError("backoff_seconds must be a schedule of 1..86400 seconds")
        object.__setattr__(self, "backoff_seconds", schedule)
        if (
            isinstance(self.decay_seconds, bool)
            or not isinstance(self.decay_seconds, int)
            or self.decay_seconds < 1
        ):
            raise ValueError("decay_seconds must be a positive int")
        object.__setattr__(self, "on_success", OnSuccess(self.on_success))


def policies_from_settings(settings: Settings) -> dict[ThrottleScope, ThrottlePolicy]:
    login = tuple(settings.login_backoff_seconds)
    redeem = tuple(settings.redeem_backoff_seconds)
    return {
        ThrottleScope.LOGIN_ACCOUNT: ThrottlePolicy(
            settings.login_account_free_attempts,
            login,
            settings.login_decay_seconds,
            OnSuccess.RESET,
        ),
        ThrottleScope.LOGIN_SOURCE: ThrottlePolicy(
            settings.login_source_free_attempts,
            login,
            settings.login_decay_seconds,
            OnSuccess.REFUND,
        ),
        ThrottleScope.REDEEM_SOURCE: ThrottlePolicy(
            settings.redeem_source_free_attempts,
            redeem,
            settings.redeem_decay_seconds,
            OnSuccess.KEEP,
        ),
        ThrottleScope.REDEEM_GLOBAL: ThrottlePolicy(
            settings.redeem_global_free_attempts,
            redeem,
            settings.redeem_decay_seconds,
            OnSuccess.KEEP,
        ),
    }


@dataclass(frozen=True, slots=True)
class Refused:
    """An attempt that met a lock (and was not counted)."""

    retry_after_seconds: int


@dataclass(frozen=True, slots=True)
class Reservation:
    """An attempt that was counted and may be judged."""

    scope: ThrottleScope
    key: bytes = field(repr=False)
    attempts: int
    # True when THIS attempt set a lock (its count reached the threshold).
    locked_now: bool


# The count after this attempt, written once so that the two places that need it
# (the count itself and the lock derived from it) cannot disagree. Constant text:
# nothing from the caller is ever formatted into the statement.
_NEW_ATTEMPTS = (
    "CASE WHEN greatest(t.last_attempt_at, coalesce(t.locked_until, t.last_attempt_at))"
    " < (SELECT ts FROM clock) - make_interval(secs => :decay)"
    " THEN 1 ELSE t.attempts + 1 END"
)
_LOCK_FOR = (
    "CASE WHEN {n} >= :free THEN (SELECT ts FROM clock) + make_interval(secs => "
    "(CAST(:schedule AS integer[]))[least({n} - :free + 1, "
    "cardinality(CAST(:schedule AS integer[])))]) END"
)
_RESERVE = f"""
WITH clock AS (SELECT greatest(CAST(:now AS timestamptz), clock_timestamp()) AS ts)
INSERT INTO auth_throttles AS t
    (scope, key_hash, attempts, last_attempt_at, locked_until)
SELECT :scope, :key, 1, clock.ts, {_LOCK_FOR.format(n="1")} FROM clock
ON CONFLICT (scope, key_hash) DO UPDATE SET
    attempts = {_NEW_ATTEMPTS},
    last_attempt_at = (SELECT ts FROM clock),
    locked_until = {_LOCK_FOR.format(n=f"({_NEW_ATTEMPTS})")}
WHERE t.locked_until IS NULL OR t.locked_until <= (SELECT ts FROM clock)
RETURNING attempts, locked_until IS NOT NULL AS locked_now, (xmax = 0) AS inserted
"""  # noqa: S608 (constant fragments only)
_REMAINING = """
SELECT extract(epoch FROM locked_until - greatest(CAST(:now AS timestamptz),
                                                  clock_timestamp()))
FROM auth_throttles WHERE scope = :scope AND key_hash = :key
"""
_REFUND = """
UPDATE auth_throttles
   SET attempts = greatest(attempts - 1, 0),
       locked_until = CASE WHEN greatest(attempts - 1, 0) < :free
                           THEN NULL ELSE locked_until END
 WHERE scope = :scope AND key_hash = :key
"""
_RESET = "DELETE FROM auth_throttles WHERE scope = :scope AND key_hash = :key"
_PURGE = """
DELETE FROM auth_throttles WHERE (scope, key_hash) IN (
    SELECT scope, key_hash FROM auth_throttles
     WHERE last_attempt_at < CAST(:cutoff AS timestamptz)
       AND (locked_until IS NULL OR locked_until < CAST(:cutoff AS timestamptz))
     LIMIT :batch)
"""


class Throttle:
    """Reserves, settles and resets attempts. See the module docstring."""

    def __init__(
        self,
        database: Database,
        policies: Mapping[ThrottleScope, ThrottlePolicy],
        *,
        clock,
        timeout_seconds: float,
    ) -> None:
        if not isinstance(database, Database):
            raise TypeError("database must be a Database")
        if not callable(clock):
            raise TypeError("clock must be callable")
        expected = set(ThrottleScope)
        if set(policies) != expected or not all(
            isinstance(p, ThrottlePolicy) for p in policies.values()
        ):
            raise ValueError("policies must hold a ThrottlePolicy for every scope")
        self._database = database
        self._policies = dict(policies)
        self._clock = clock
        self._timeout = float(timeout_seconds)
        self._memory = max(policy.decay_seconds for policy in policies.values())

    def _now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("clock must return timezone-aware datetimes")
        return now

    @staticmethod
    def _check(scope: object, key: object) -> tuple[ThrottleScope, bytes]:
        if not isinstance(scope, ThrottleScope):
            raise InvalidAuthInputError("scope")
        if not isinstance(key, bytes) or len(key) != 32:
            raise InvalidAuthInputError("key")
        return scope, key

    def _parameters(self, scope: ThrottleScope, key: bytes) -> dict:
        policy = self._policies[scope]
        return {
            "scope": scope.value,
            "key": key,
            "now": self._now(),
            "free": policy.free_attempts,
            "schedule": list(policy.backoff_seconds),
            "decay": policy.decay_seconds,
        }

    async def reserve_in(
        self, session: AsyncSession, scope: ThrottleScope, key: bytes
    ) -> "Reservation | Refused":
        """Count an attempt in ``session``: a ``Reservation`` or a ``Refused``.

        Does not raise for a lock (the caller decides what to do with the rest
        of its transaction: the attempt stays counted either way).
        """
        if not isinstance(session, AsyncSession):
            raise InvalidAuthInputError("session")
        scope, key = self._check(scope, key)
        parameters = self._parameters(scope, key)
        row = (await session.execute(text(_RESERVE), parameters)).first()
        if row is None:
            remaining = (
                await session.execute(
                    text(_REMAINING),
                    {k: parameters[k] for k in ("now", "scope", "key")},
                )
            ).scalar()
            return Refused(max(1, math.ceil(float(remaining or 1))))
        if row.inserted:
            # A new key: forget a few counters that no longer count.
            cutoff = parameters["now"].timestamp() - self._memory
            await session.execute(
                text(_PURGE),
                {
                    "cutoff": datetime.fromtimestamp(cutoff, parameters["now"].tzinfo),
                    "batch": PURGE_BATCH,
                },
            )
        return Reservation(scope, key, row.attempts, row.locked_now)

    async def reserve_many(
        self, keys: Sequence[tuple[ThrottleScope, bytes]]
    ) -> tuple[Reservation, ...]:
        """Count an attempt against each key, in order, in ONE transaction.

        The first key that is locked ends the sequence with ``ThrottledError``;
        the attempts counted before it (and the refused one, which is not
        counted) stay: a source that hammers a locked account still counts.
        """
        if not isinstance(keys, list | tuple) or not 1 <= len(keys) <= len(
            ThrottleScope
        ):
            raise InvalidAuthInputError("keys")
        for item in keys:
            if not isinstance(item, tuple) or len(item) != 2:
                raise InvalidAuthInputError("keys")
        checked = [self._check(scope, key) for scope, key in keys]

        async def work(session: AsyncSession):
            granted: list[Reservation] = []
            for scope, key in checked:
                outcome = await self.reserve_in(session, scope, key)
                if isinstance(outcome, Refused):
                    return granted, outcome
                granted.append(outcome)
            return granted, None

        granted, refused = await run(self._database, work, self._timeout)
        if refused is not None:
            raise ThrottledError(refused.retry_after_seconds)
        return tuple(granted)

    async def reserve(self, scope: ThrottleScope, key: bytes) -> Reservation:
        """Count an attempt; ``ThrottledError`` (with the seconds left) if locked."""
        return (await self.reserve_many([(scope, key)]))[0]

    async def succeed_in(self, session: AsyncSession, reservation: Reservation) -> None:
        """The attempt succeeded: what the scope does about it (in ``session``).

        A login commits this with the session it creates, so a success that
        cannot be recorded is not a success.
        """
        if not isinstance(session, AsyncSession):
            raise InvalidAuthInputError("session")
        if not isinstance(reservation, Reservation):
            raise InvalidAuthInputError("reservation")
        policy = self._policies[reservation.scope]
        if policy.on_success is OnSuccess.KEEP:
            return
        parameters = self._parameters(reservation.scope, reservation.key)
        statement = _RESET if policy.on_success is OnSuccess.RESET else _REFUND
        await session.execute(text(statement), parameters)

    async def reset(self, scope: ThrottleScope, key: bytes) -> None:
        """Forget a counter and its lock (an administrator's unlock)."""
        scope, key = self._check(scope, key)
        parameters = self._parameters(scope, key)

        async def work(session: AsyncSession) -> None:
            await session.execute(text(_RESET), parameters)

        await run(self._database, work, self._timeout)

    async def reset_in(
        self, session: AsyncSession, scope: ThrottleScope, key: bytes
    ) -> None:
        """``reset`` inside the caller's transaction (recovery, unlock)."""
        if not isinstance(session, AsyncSession):
            raise InvalidAuthInputError("session")
        scope, key = self._check(scope, key)
        await session.execute(text(_RESET), {"scope": scope.value, "key": key})
