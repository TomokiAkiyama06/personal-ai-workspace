"""Progressive backoff and rate limits on a real PostgreSQL (PAW-022)."""

import unittest
from datetime import UTC, datetime, timedelta

from paw_backend.auth.errors import InvalidAuthInputError, ThrottledError
from paw_backend.auth.models import ThrottleScope
from paw_backend.auth.throttle import (
    OnSuccess,
    Reservation,
    Throttle,
    ThrottlePolicy,
    policies_from_settings,
)
from paw_backend.auth.tokens import GLOBAL_KEY, account_key, source_key

from .auth_support import T0, PostgresAuthTestCase, fast_settings, requires_postgres

ACCOUNT = ThrottleScope.LOGIN_ACCOUNT
SOURCE = ThrottleScope.LOGIN_SOURCE
REDEEM = ThrottleScope.REDEEM_SOURCE
GLOBAL = ThrottleScope.REDEEM_GLOBAL
SCHEDULE = (30, 60, 300, 900, 3600)


class ThrottleTestCase(PostgresAuthTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.throttle = self.make_throttle(self.service_database)

    def make_throttle(self, database, **policies) -> Throttle:
        base = policies_from_settings(fast_settings())
        base.update(policies)
        return Throttle(database, base, clock=self.clock, timeout_seconds=10)

    async def attempt(self, scope=ACCOUNT, key=None, throttle=None):
        return await (throttle or self.throttle).reserve(
            scope, key or account_key("alice")
        )

    async def refused(self, scope=ACCOUNT, key=None) -> int:
        with self.assertRaises(ThrottledError) as caught:
            await self.attempt(scope, key)
        return caught.exception.retry_after_seconds

    async def row(self, scope=ACCOUNT, key=None):
        rows = await self.query(
            "SELECT * FROM auth_throttles WHERE scope = :s AND key_hash = :k",
            s=scope.value,
            k=key or account_key("alice"),
        )
        return rows[0] if rows else None


@requires_postgres
class BackoffTest(ThrottleTestCase):
    async def test_four_attempts_are_normal_the_fifth_sets_a_30_second_lock(
        self,
    ):
        for count in range(1, 5):
            reservation = await self.attempt()
            self.assertEqual(
                (reservation.attempts, reservation.locked_now), (count, False)
            )
        self.assertIsNone((await self.row()).locked_until)
        fifth = await self.attempt()
        self.assertEqual((fifth.attempts, fifth.locked_now), (5, True))
        self.assertEqual((await self.row()).locked_until, T0 + timedelta(seconds=30))

    async def test_an_attempt_during_the_lock_is_refused_and_not_counted(self):
        for _ in range(5):
            await self.attempt()
        self.clock.advance(seconds=10)
        self.assertEqual(await self.refused(), 20)
        self.clock.advance(seconds=10)
        self.assertEqual(await self.refused(), 10)
        self.assertEqual((await self.row()).attempts, 5)
        self.assertEqual((await self.row()).locked_until, T0 + timedelta(seconds=30))

    async def test_the_retry_after_is_rounded_up_and_at_least_one_second(self):
        for _ in range(5):
            await self.attempt()
        self.clock.advance(seconds=29, milliseconds=500)
        self.assertEqual(await self.refused(), 1)

    async def test_the_lock_ends_exactly_at_its_deadline(self):
        for _ in range(5):
            await self.attempt()
        self.clock.now = T0 + timedelta(seconds=30) - timedelta(microseconds=1)
        await self.refused()
        self.clock.now = T0 + timedelta(seconds=30)
        reservation = await self.attempt()
        self.assertEqual(reservation.attempts, 6)

    async def test_each_later_lock_is_longer_and_the_last_length_repeats(self):
        for _ in range(5):
            await self.attempt()
        expected = list(SCHEDULE[1:]) + [3600, 3600]
        clock_at = T0
        lock = 30
        for length in expected:
            clock_at = clock_at + timedelta(seconds=lock)
            self.clock.now = clock_at
            reservation = await self.attempt()
            self.assertTrue(reservation.locked_now)
            self.assertEqual(
                (await self.row()).locked_until, clock_at + timedelta(seconds=length)
            )
            lock = length
        self.assertEqual((await self.row()).attempts, 5 + len(expected))

    async def test_a_lock_is_never_longer_than_the_last_entry_of_the_schedule(self):
        for _ in range(40):
            self.clock.now = T0 + timedelta(days=_)
            await self.attempt()
        self.assertEqual(
            (await self.row()).locked_until - self.clock.now, timedelta(seconds=3600)
        )

    async def test_the_counter_is_forgotten_after_the_decay_period(self):
        for _ in range(5):
            await self.attempt()
        # The lock ended at +30 s; the decay (a day) runs from there, and one
        # second short of it the count goes on (and the next lock is 60 s).
        self.clock.now = T0 + timedelta(seconds=30, days=1) - timedelta(seconds=1)
        self.assertEqual((await self.attempt()).attempts, 6)
        # Exactly a day after THAT lock ended is still not forgotten ...
        self.clock.now += timedelta(seconds=60, days=1)
        self.assertEqual((await self.attempt()).attempts, 7)
        # ... and a second more than a day after the next one (300 s) is a new start.
        self.clock.now += timedelta(seconds=300 + 1, days=1)
        fresh = await self.attempt()
        self.assertEqual((fresh.attempts, fresh.locked_now), (1, False))
        self.assertIsNone((await self.row()).locked_until)

    async def test_a_lock_that_is_still_running_is_not_lifted_by_the_decay(self):
        policy = ThrottlePolicy(2, (3600,), 60, OnSuccess.RESET)
        throttle = (
            self.make_throttle(self.service_database, **{ACCOUNT.value: policy})
            if False
            else Throttle(
                self.service_database,
                {**policies_from_settings(fast_settings()), ACCOUNT: policy},
                clock=self.clock,
                timeout_seconds=10,
            )
        )
        await self.attempt(throttle=throttle)
        await self.attempt(throttle=throttle)
        self.clock.advance(seconds=120)  # past the decay, inside the hour of lock
        with self.assertRaises(ThrottledError):
            await self.attempt(throttle=throttle)

    async def test_keys_and_scopes_are_independent(self):
        for _ in range(5):
            await self.attempt(ACCOUNT, account_key("alice"))
        self.assertEqual(
            (await self.attempt(ACCOUNT, account_key("bobby"))).attempts, 1
        )
        self.assertEqual((await self.attempt(SOURCE, account_key("alice"))).attempts, 1)
        await self.refused(ACCOUNT, account_key("alice"))

    async def test_an_unknown_name_is_throttled_exactly_like_a_known_one(self):
        await self.make_user("alice")
        rows = {}
        for name in ("alice", "nobody-here"):
            key = account_key(name)
            trace = []
            for _ in range(7):
                try:
                    reservation = await self.attempt(ACCOUNT, key)
                    trace.append(("ok", reservation.attempts, reservation.locked_now))
                except ThrottledError as error:
                    trace.append(("locked", error.retry_after_seconds))
                self.clock.advance(seconds=1)
            rows[name] = trace
            self.clock.now = T0
            await self.execute("TRUNCATE auth_throttles")
        self.assertEqual(rows["alice"], rows["nobody-here"])

    async def test_a_thing_stored_is_a_hash_never_the_name(self):
        await self.attempt(ACCOUNT, account_key("alice"))
        await self.attempt(SOURCE, source_key("203.0.113.7"))
        stored = await self.everything_stored()
        self.assertNotIn("alice", stored)
        self.assertNotIn("203.0.113.7", stored)


@requires_postgres
class SuccessTest(ThrottleTestCase):
    async def test_a_success_clears_the_account_counter_and_its_lock(self):
        for _ in range(5):
            reservation = await self.attempt()

        async def work(session):
            await self.throttle.succeed_in(session, reservation)

        async with self.service_database.session() as session:
            await work(session)
            await session.commit()
        self.assertIsNone(await self.row())
        self.assertEqual((await self.attempt()).attempts, 1)

    async def test_a_success_of_a_source_takes_back_only_its_own_attempt(self):
        reservations = [
            await self.attempt(SOURCE, source_key("203.0.113.7")) for _ in range(3)
        ]
        async with self.service_database.session() as session:
            await self.throttle.succeed_in(session, reservations[-1])
            await session.commit()
        self.assertEqual(
            (await self.row(SOURCE, source_key("203.0.113.7"))).attempts, 2
        )

    async def test_a_source_that_succeeds_is_not_forgiven_for_its_earlier_failures(
        self,
    ):
        policy = ThrottlePolicy(3, (60,), 3600, OnSuccess.REFUND)
        throttle = Throttle(
            self.service_database,
            {**policies_from_settings(fast_settings()), SOURCE: policy},
            clock=self.clock,
            timeout_seconds=10,
        )
        key = source_key("203.0.113.7")
        for _ in range(2):
            await throttle.reserve(SOURCE, key)
        third = await throttle.reserve(SOURCE, key)  # sets the lock (count 3)
        self.assertTrue(third.locked_now)
        async with self.service_database.session() as session:
            await throttle.succeed_in(session, third)
            await session.commit()
        row = await self.row(SOURCE, key)
        # Count 2 is below the threshold again: the lock this attempt set is undone.
        self.assertEqual((row.attempts, row.locked_until), (2, None))
        # ... but the two failures still count: the next attempt reaches 3 again.
        self.assertTrue((await throttle.reserve(SOURCE, key)).locked_now)

    async def test_a_refund_keeps_a_lock_that_other_attempts_still_justify(self):
        policy = ThrottlePolicy(2, (60,), 3600, OnSuccess.REFUND)
        throttle = Throttle(
            self.service_database,
            {**policies_from_settings(fast_settings()), SOURCE: policy},
            clock=self.clock,
            timeout_seconds=10,
        )
        key = source_key("203.0.113.7")
        reservations = [await throttle.reserve(SOURCE, key) for _ in range(2)]
        self.clock.advance(seconds=60)
        reservations.append(
            await throttle.reserve(SOURCE, key)
        )  # count 3, locked again
        async with self.service_database.session() as session:
            await throttle.succeed_in(session, reservations[0])
            await session.commit()
        row = await self.row(SOURCE, key)
        self.assertEqual(row.attempts, 2)
        self.assertEqual(row.locked_until, T0 + timedelta(seconds=120))

    async def test_a_refund_never_goes_below_zero(self):
        key = source_key("203.0.113.7")
        reservation = await self.attempt(SOURCE, key)
        async with self.service_database.session() as session:
            await self.throttle.succeed_in(session, reservation)
            await self.throttle.succeed_in(session, reservation)
            await session.commit()
        self.assertEqual((await self.row(SOURCE, key)).attempts, 0)

    async def test_the_token_endpoint_counts_every_attempt_a_success_included(self):
        key = source_key("203.0.113.7")
        reservation = await self.attempt(REDEEM, key)
        async with self.service_database.session() as session:
            await self.throttle.succeed_in(session, reservation)
            await session.commit()
        self.assertEqual((await self.row(REDEEM, key)).attempts, 1)

    async def test_reset_forgets_a_counter_and_its_lock(self):
        for _ in range(5):
            await self.attempt()
        await self.throttle.reset(ACCOUNT, account_key("alice"))
        self.assertIsNone(await self.row())
        await self.throttle.reset(ACCOUNT, account_key("alice"))  # nothing left: fine
        self.assertEqual((await self.attempt()).attempts, 1)


@requires_postgres
class RateLimitTest(ThrottleTestCase):
    """The token endpoint's limits: per source and in total (Decision 0005)."""

    async def test_a_source_gets_five_attempts_then_a_minute_of_lock(self):
        key = source_key("203.0.113.7")
        for count in range(1, 6):
            self.assertEqual((await self.attempt(REDEEM, key)).attempts, count)
        self.assertEqual(await self.refused(REDEEM, key), 60)

    async def test_another_source_is_not_affected(self):
        for _ in range(5):
            await self.attempt(REDEEM, source_key("203.0.113.7"))
        self.assertEqual(
            (await self.attempt(REDEEM, source_key("203.0.113.8"))).attempts, 1
        )

    async def test_the_global_limit_stops_many_sources_together(self):
        # 30 attempts from 30 different sources: every source is under its own
        # limit, and the thirtieth still starts the global lock.
        for index in range(30):
            reservation = await self.attempt(GLOBAL, GLOBAL_KEY)
            self.assertEqual(reservation.attempts, index + 1)
        self.assertEqual(await self.refused(GLOBAL, GLOBAL_KEY), 60)
        self.clock.advance(seconds=60)
        self.assertEqual((await self.attempt(GLOBAL, GLOBAL_KEY)).attempts, 31)

    async def test_the_settings_change_the_limits(self):
        settings = fast_settings(
            redeem_source_free_attempts=2, redeem_backoff_seconds=[7, 9]
        )
        throttle = Throttle(
            self.service_database,
            policies_from_settings(settings),
            clock=self.clock,
            timeout_seconds=10,
        )
        key = source_key("203.0.113.7")
        await throttle.reserve(REDEEM, key)
        second = await throttle.reserve(REDEEM, key)
        self.assertTrue(second.locked_now)
        self.assertEqual(
            (await self.row(REDEEM, key)).locked_until, T0 + timedelta(seconds=7)
        )
        self.clock.advance(seconds=7)
        await throttle.reserve(REDEEM, key)
        self.assertEqual(
            (await self.row(REDEEM, key)).locked_until, T0 + timedelta(seconds=7 + 9)
        )


@requires_postgres
class ConcurrencyTest(ThrottleTestCase):
    async def test_of_many_simultaneous_attempts_only_the_free_ones_are_judged(self):
        async def reserve(services, index):
            return await services.throttle.reserve(ACCOUNT, account_key("alice"))

        results = await self.gather_on_own_engines(24, reserve)
        granted = [r for r in results if isinstance(r, Reservation)]
        refused = [r for r in results if isinstance(r, ThrottledError)]
        self.assertEqual(len(granted) + len(refused), 24, results)
        # Exactly the five attempts before the lock, each with its own count.
        self.assertEqual(sorted(r.attempts for r in granted), [1, 2, 3, 4, 5])
        self.assertEqual(sum(r.locked_now for r in granted), 1)
        self.assertEqual(len(refused), 19)
        self.assertEqual((await self.row()).attempts, 5)

    async def test_simultaneous_attempts_on_the_global_limit_are_bounded_too(self):
        async def reserve(services, index):
            return await services.throttle.reserve(GLOBAL, GLOBAL_KEY)

        results = await self.gather_on_own_engines(40, reserve)
        granted = [r for r in results if isinstance(r, Reservation)]
        self.assertEqual(len(granted), 30)
        self.assertEqual(sorted(r.attempts for r in granted), list(range(1, 31)))

    async def test_simultaneous_attempts_on_different_accounts_do_not_block_each_other(
        self,
    ):
        async def reserve(services, index):
            return await services.throttle.reserve(
                ACCOUNT, account_key(f"user-{index}")
            )

        results = await self.gather_on_own_engines(12, reserve)
        self.assertTrue(
            all(isinstance(r, Reservation) and r.attempts == 1 for r in results),
            results,
        )


@requires_postgres
class ClockTest(ThrottleTestCase):
    async def test_a_clock_behind_the_database_cannot_shorten_a_lock(self):
        self.clock.now = datetime(2001, 1, 1, tzinfo=UTC)
        before = await self.scalar("SELECT clock_timestamp()")
        for _ in range(5):
            await self.attempt()
        after = await self.scalar("SELECT clock_timestamp()")
        row = await self.row()
        # The lock runs from the database's time (some moment between the two
        # readings), not from 2001.
        self.assertGreaterEqual(row.locked_until, before + timedelta(seconds=30))
        self.assertLessEqual(row.locked_until, after + timedelta(seconds=30))
        with self.assertRaises(ThrottledError):
            await self.attempt()

    async def test_the_process_clock_is_used_when_it_is_ahead(self):
        for _ in range(5):
            await self.attempt()
        self.assertEqual((await self.row()).locked_until, T0 + timedelta(seconds=30))


@requires_postgres
class PurgeTest(ThrottleTestCase):
    async def test_a_new_key_clears_a_few_counters_that_no_longer_count(self):
        for index in range(60):
            await self.execute(
                "INSERT INTO auth_throttles "
                "(scope, key_hash, attempts, last_attempt_at) "
                "VALUES ('login_account', :k, 1, :t)",
                k=account_key(f"old-{index}"),
                t=T0 - timedelta(days=2),
            )
        await self.attempt(ACCOUNT, account_key("newcomer"))
        self.assertEqual(
            await self.scalar("SELECT count(*) FROM auth_throttles"), 60 + 1 - 50
        )
        await self.attempt(ACCOUNT, account_key("newcomer"))  # not a new key: no purge
        self.assertEqual(await self.scalar("SELECT count(*) FROM auth_throttles"), 11)

    async def test_a_counter_that_still_counts_is_not_purged(self):
        await self.execute(
            "INSERT INTO auth_throttles "
            "(scope, key_hash, attempts, last_attempt_at, locked_until) "
            "VALUES ('login_account', :k, 9, :t, :u)",
            k=account_key("locked"),
            t=T0 - timedelta(days=2),
            u=T0 + timedelta(hours=1),
        )
        await self.execute(
            "INSERT INTO auth_throttles "
            "(scope, key_hash, attempts, last_attempt_at) "
            "VALUES ('login_account', :k, 2, :t)",
            k=account_key("recent"),
            t=T0 - timedelta(hours=1),
        )
        await self.attempt(ACCOUNT, account_key("newcomer"))
        self.assertIsNotNone(await self.row(ACCOUNT, account_key("locked")))
        self.assertIsNotNone(await self.row(ACCOUNT, account_key("recent")))


class ValidationTest(unittest.TestCase):
    def test_a_policy_is_checked(self):
        for args, error in (
            ((0, (30,), 60, OnSuccess.RESET), ValueError),
            ((True, (30,), 60, OnSuccess.RESET), TypeError),
            (("5", (30,), 60, OnSuccess.RESET), TypeError),
            ((5, (), 60, OnSuccess.RESET), ValueError),
            ((5, (0,), 60, OnSuccess.RESET), ValueError),
            ((5, (86_401,), 60, OnSuccess.RESET), ValueError),
            ((5, (True,), 60, OnSuccess.RESET), ValueError),
            ((5, (30,), 0, OnSuccess.RESET), ValueError),
            ((5, (30,), True, OnSuccess.RESET), ValueError),
            ((5, (30,), 60, "forgive"), ValueError),
        ):
            with self.subTest(args=args):
                with self.assertRaises(error):
                    ThrottlePolicy(*args)
        ok = ThrottlePolicy(5, [30, 60], 60, "reset")
        self.assertEqual(
            (ok.backoff_seconds, ok.on_success), ((30, 60), OnSuccess.RESET)
        )


@requires_postgres
class ArgumentTest(ThrottleTestCase):
    async def test_every_argument_is_checked_before_the_database(self):
        good = account_key("alice")
        for scope, key in (
            ("login_account", good),
            (None, good),
            (ACCOUNT, "alice"),
            (ACCOUNT, good[:-1]),
            (ACCOUNT, bytearray(good)),
            (ACCOUNT, None),
        ):
            with self.subTest(scope=scope, key=repr(key)[:12]):
                with self.assertRaises(InvalidAuthInputError):
                    await self.throttle.reserve(scope, key)
                with self.assertRaises(InvalidAuthInputError):
                    await self.throttle.reset(scope, key)
        for bad in (None, "reservation", 5, (ACCOUNT, good)):
            with self.subTest(bad=repr(bad)[:20]):
                with self.assertRaises(InvalidAuthInputError):
                    async with self.service_database.session() as session:
                        await self.throttle.succeed_in(session, bad)
        self.assertEqual(await self.scalar("SELECT count(*) FROM auth_throttles"), 0)

    async def test_the_constructor_checks_its_arguments(self):
        policies = policies_from_settings(fast_settings())
        for args, error in (
            (("db", policies), TypeError),
            ((self.service_database, {ACCOUNT: policies[ACCOUNT]}), ValueError),
            ((self.service_database, {**policies, ACCOUNT: "policy"}), ValueError),
        ):
            with self.subTest(args=repr(args)[:30]):
                with self.assertRaises(error):
                    Throttle(*args, clock=self.clock, timeout_seconds=1)
        with self.assertRaises(TypeError):
            Throttle(self.service_database, policies, clock=None, timeout_seconds=1)


if __name__ == "__main__":
    unittest.main()
