"""Server-side sessions on a real PostgreSQL (PAW-022)."""

import hashlib
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.auth import tokens
from paw_backend.auth.errors import InvalidAuthInputError
from paw_backend.auth.models import AuthMethod, RevokeReason
from paw_backend.auth.sessions import validate_device_label
from paw_backend.authz.roles import SystemRole

from .auth_support import (
    T0,
    PostgresAuthTestCase,
    fast_settings,
    requires_postgres,
)

DAY = 86_400


class SessionTestCase(PostgresAuthTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.store = self.services.sessions
        self.alice = await self.make_user("alice")

    async def tx(self, work, database=None):
        async with (database or self.service_database).session() as session:
            result = await work(session)
            await session.commit()
            return result

    async def create(self, user=None, **options):
        user = user or self.alice
        options.setdefault("remember_me", False)
        return await self.tx(lambda s: self.store.create(s, user.id, **options))

    async def authenticate(self, token, store=None, database=None):
        store = store or self.store
        return await self.tx(lambda s: store.authenticate(s, token), database)

    async def row(self, session_id):
        rows = await self.query(
            "SELECT * FROM auth_sessions WHERE id = :id", id=session_id
        )
        return rows[0] if rows else None


@requires_postgres
class CreateTest(SessionTestCase):
    async def test_a_normal_session_has_the_requirements_lifetimes(self):
        issued = await self.create()
        record = issued.record
        self.assertEqual(record.created_at, T0)
        self.assertEqual(record.last_used_at, T0)
        # 30 days without use, and 90 days at the latest.
        self.assertEqual(record.idle_expires_at, T0 + timedelta(days=30))
        self.assertEqual(record.absolute_expires_at, T0 + timedelta(days=90))
        self.assertEqual(record.expires_at, T0 + timedelta(days=30))
        self.assertFalse(record.remember_me)
        self.assertEqual(record.auth_method, AuthMethod.PASSWORD)

    async def test_a_remember_me_session_lives_at_most_90_days(self):
        record = (await self.create(remember_me=True)).record
        self.assertTrue(record.remember_me)
        self.assertEqual(record.idle_expires_at, T0 + timedelta(days=90))
        self.assertEqual(record.absolute_expires_at, T0 + timedelta(days=90))

    async def test_the_lifetimes_come_from_the_settings_and_are_copied_into_the_row(
        self,
    ):
        settings = fast_settings(
            session_idle_days=2, session_remember_days=5, session_absolute_days=3
        )
        services = self.build(self.service_database, settings=settings)
        store = services.sessions
        normal = (
            await self.tx(lambda s: store.create(s, self.alice.id, remember_me=False))
        ).record
        remember = (
            await self.tx(lambda s: store.create(s, self.alice.id, remember_me=True))
        ).record
        self.assertEqual(normal.idle_expires_at, T0 + timedelta(days=2))
        self.assertEqual(normal.absolute_expires_at, T0 + timedelta(days=3))
        self.assertEqual(remember.absolute_expires_at, T0 + timedelta(days=5))
        stored = await self.row(normal.id)
        self.assertEqual(stored.idle_timeout_seconds, 2 * DAY)
        # A session made under other settings is not changed by them later.
        old = await self.create()
        self.assertEqual((await self.row(old.record.id)).idle_timeout_seconds, 30 * DAY)

    async def test_the_id_is_random_shown_once_and_only_its_hash_is_stored(self):
        first = await self.create()
        second = await self.create()
        self.assertNotEqual(first.token, second.token)
        self.assertEqual(len(first.token), 43)
        stored = await self.row(first.record.id)
        self.assertEqual(
            bytes(stored.token_hash),
            hashlib.sha256(b"paw.session.v1\0" + first.token.encode()).digest(),
        )
        self.assertNotIn(first.token, await self.everything_stored())
        self.assertNotIn(first.token, repr(first))
        self.assertNotIn(first.token, repr(first.record))

    async def test_the_time_written_is_the_later_of_the_clock_and_the_database(self):
        # A clock far behind the database's cannot make a session start (and so
        # end) in the past: the row is stamped with the database's time.
        self.clock.now = datetime(2001, 1, 1, tzinfo=UTC)
        record = (await self.create()).record
        db_now = await self.scalar("SELECT clock_timestamp()")
        self.assertGreater(record.created_at, datetime(2020, 1, 1, tzinfo=UTC))
        self.assertLessEqual(record.created_at, db_now)
        self.assertEqual(
            record.absolute_expires_at - record.created_at, timedelta(days=90)
        )

    async def test_a_device_label_is_trimmed_and_bounded(self):
        self.assertEqual(
            (await self.create(device_label="  My laptop ")).record.device_label,
            "My laptop",
        )
        self.assertIsNone((await self.create(device_label="   ")).record.device_label)
        self.assertIsNone((await self.create(device_label=None)).record.device_label)
        self.assertEqual(validate_device_label("x" * 64), "x" * 64)
        for bad in ("x" * 65, "bell\x07", "nul\x00", "line\nbreak", 5, b"laptop"):
            with self.subTest(bad=repr(bad)[:20]):
                with self.assertRaises(InvalidAuthInputError):
                    validate_device_label(bad)

    async def test_old_rows_are_purged_a_few_at_a_time_when_a_session_is_created(self):
        old = []
        for _ in range(60):
            old.append((await self.create()).record.id)
        # 60 sessions that ended a long time ago: revoked far in the past.
        await self.execute(
            "UPDATE auth_sessions SET revoked_at = :t, revoked_reason = 'logout'",
            t=T0 - timedelta(days=60),
        )
        self.clock.advance(days=1)
        await self.create()
        remaining = await self.scalar("SELECT count(*) FROM auth_sessions")
        # 60 old + the new one - a batch of 50 deleted (the new one is not old).
        self.assertEqual(remaining, 60 + 1 - 50)
        self.clock.advance(seconds=1)
        await self.create()
        self.assertEqual(
            await self.scalar("SELECT count(*) FROM auth_sessions"), 60 + 2 - 50 - 10
        )

    async def test_a_session_that_ended_recently_is_not_purged(self):
        issued = await self.create()
        await self.tx(
            lambda s: self.store.revoke(
                s, issued.record.id, self.alice.id, RevokeReason.LOGOUT
            )
        )
        self.clock.advance(days=29)
        await self.create()
        self.assertIsNotNone(await self.row(issued.record.id))
        self.clock.advance(days=2)
        await self.create()
        self.assertIsNone(await self.row(issued.record.id))


@requires_postgres
class AuthenticateTest(SessionTestCase):
    async def test_a_valid_session_resolves_to_its_user(self):
        issued = await self.create()
        found = await self.authenticate(issued.token)
        self.assertEqual(found.record.id, issued.record.id)
        self.assertEqual(found.record.user_id, self.alice.id)
        self.assertEqual(found.login_name, "alice")
        self.assertEqual(found.system_role, SystemRole.USER)
        self.assertEqual(found.token_hash, tokens.hash_session_token(issued.token))
        self.assertNotIn(issued.token, repr(found))

    async def test_a_token_that_is_not_a_session_is_nobody_without_a_query(self):
        session = AsyncMock(spec=AsyncSession)
        session.execute.side_effect = AssertionError("queried")

        for bad in (
            None,
            "",
            "short",
            "A" * 42,
            "A" * 44,
            12,
            b"A" * 43,
            "A" * 21 + " " + "A" * 21,
        ):
            with self.subTest(bad=repr(bad)[:20]):
                self.assertIsNone(await self.store.authenticate(session, bad))

    async def test_an_unknown_token_is_nobody(self):
        await self.create()
        self.assertIsNone(await self.authenticate(tokens.new_session_token()))

    async def test_a_token_that_differs_in_one_character_is_nobody(self):
        issued = await self.create()
        flipped = ("B" if issued.token[0] != "B" else "C") + issued.token[1:]
        self.assertIsNone(await self.authenticate(flipped))
        self.assertIsNotNone(await self.authenticate(issued.token))

    async def test_a_revoked_session_is_nobody(self):
        issued = await self.create()
        await self.tx(
            lambda s: self.store.revoke(
                s, issued.record.id, self.alice.id, RevokeReason.LOGOUT
            )
        )
        self.assertIsNone(await self.authenticate(issued.token))

    async def test_only_an_active_user_has_a_session(self):
        issued = await self.create()
        for status in ("invited", "pending_deletion", "deleted"):
            with self.subTest(status=status):
                await self.execute("UPDATE users SET status = :s", s=status)
                self.assertIsNone(await self.authenticate(issued.token))
        await self.execute("UPDATE users SET status = 'active'")
        self.assertIsNotNone(await self.authenticate(issued.token))

    async def test_a_normal_session_ends_after_30_days_without_use(self):
        issued = await self.create()
        self.clock.now = T0 + timedelta(days=30) - timedelta(seconds=1)
        self.assertIsNotNone(await self.authenticate(issued.token))

    async def test_the_idle_limit_is_exact_and_the_boundary_is_expired(self):
        issued = await self.create()
        self.clock.now = T0 + timedelta(days=30)
        self.assertIsNone(await self.authenticate(issued.token))
        self.clock.now = T0 + timedelta(days=31)
        self.assertIsNone(await self.authenticate(issued.token))

    async def test_use_moves_the_idle_limit_forward(self):
        issued = await self.create()
        self.clock.now = T0 + timedelta(days=29)
        used = await self.authenticate(issued.token)
        self.assertEqual(used.record.last_used_at, T0 + timedelta(days=29))
        self.assertEqual(used.record.idle_expires_at, T0 + timedelta(days=59))
        self.clock.now = T0 + timedelta(days=58)
        self.assertIsNotNone(await self.authenticate(issued.token))
        # ... 58 + 30 = 88 days now; 89 is past the idle limit.
        self.clock.now = T0 + timedelta(days=89)
        self.assertIsNone(await self.authenticate(issued.token))

    async def test_the_absolute_limit_ends_a_session_that_is_used_every_day(self):
        issued = await self.create()
        day = 0
        while day < 89:
            day += 1
            self.clock.now = T0 + timedelta(days=day)
            self.assertIsNotNone(await self.authenticate(issued.token), day)
        self.clock.now = T0 + timedelta(days=90) - timedelta(seconds=1)
        found = await self.authenticate(issued.token)
        self.assertIsNotNone(found)
        # The idle limit never moves past the absolute one.
        self.assertEqual(found.record.idle_expires_at, T0 + timedelta(days=90))
        self.clock.now = T0 + timedelta(days=90)
        self.assertIsNone(await self.authenticate(issued.token))

    async def test_remember_me_survives_89_days_of_silence_and_not_90(self):
        issued = await self.create(remember_me=True)
        self.clock.now = T0 + timedelta(days=89, hours=23)
        self.assertIsNotNone(await self.authenticate(issued.token))
        self.clock.now = T0 + timedelta(days=90)
        self.assertIsNone(await self.authenticate(issued.token))

    async def test_a_normal_session_does_not_survive_the_silence_remember_me_does(self):
        normal = await self.create()
        remember = await self.create(remember_me=True)
        self.clock.now = T0 + timedelta(days=45)
        self.assertIsNone(await self.authenticate(normal.token))
        self.assertIsNotNone(await self.authenticate(remember.token))

    async def test_remember_me_is_capped_at_90_days_however_often_it_is_used(self):
        issued = await self.create(remember_me=True)
        for day in range(10, 90, 10):
            self.clock.now = T0 + timedelta(days=day)
            self.assertIsNotNone(await self.authenticate(issued.token))
        self.clock.now = T0 + timedelta(days=90)
        self.assertIsNone(await self.authenticate(issued.token))

    async def test_last_used_is_written_at_most_once_per_touch_interval(self):
        issued = await self.create()
        self.clock.now = T0 + timedelta(seconds=30)
        first = await self.authenticate(issued.token)
        self.assertEqual(first.record.last_used_at, T0)  # inside the interval: no write
        self.clock.now = T0 + timedelta(seconds=60)
        second = await self.authenticate(issued.token)
        self.assertEqual(second.record.last_used_at, T0 + timedelta(seconds=60))
        self.clock.now = T0 + timedelta(seconds=90)
        third = await self.authenticate(issued.token)
        self.assertEqual(third.record.last_used_at, T0 + timedelta(seconds=60))
        self.assertEqual(
            (await self.row(issued.record.id)).last_used_at, T0 + timedelta(seconds=60)
        )

    async def test_an_expired_session_is_not_extended_by_being_presented(self):
        issued = await self.create()
        self.clock.now = T0 + timedelta(days=31)
        self.assertIsNone(await self.authenticate(issued.token))
        row = await self.row(issued.record.id)
        self.assertEqual(row.last_used_at, T0)
        self.assertEqual(row.idle_expires_at, T0 + timedelta(days=30))

    async def test_the_database_clock_ends_a_session_whatever_the_callers_clock_says(
        self,
    ):
        # The caller's clock is far in the past; the row says it ended a second
        # ago by the database's clock: it has ended.
        issued = await self.create()
        await self.execute(
            "UPDATE auth_sessions SET "
            "idle_expires_at = clock_timestamp() - interval '1 second', "
            "absolute_expires_at = clock_timestamp() - interval '1 second', "
            "created_at = created_at - interval '10 years', "
            "last_used_at = last_used_at - interval '10 years'"
        )
        self.clock.now = datetime(2001, 1, 1, tzinfo=UTC)
        self.assertIsNone(await self.authenticate(issued.token))

    async def test_the_absolute_limit_is_enforced_by_the_database_clock_too(self):
        issued = await self.create()
        await self.execute(
            "UPDATE auth_sessions SET "
            "absolute_expires_at = clock_timestamp() - interval '1 second', "
            "idle_expires_at = clock_timestamp() - interval '2 seconds', "
            "created_at = created_at - interval '10 years', "
            "last_used_at = last_used_at - interval '10 years'"
        )
        self.clock.now = datetime(2001, 1, 1, tzinfo=UTC)
        self.assertIsNone(await self.authenticate(issued.token))

    async def test_a_caller_whose_clock_is_ahead_can_only_end_a_session_earlier(self):
        issued = await self.create()
        self.clock.now = T0 + timedelta(days=400)
        self.assertIsNone(await self.authenticate(issued.token))
        # ... and the row was not touched by the attempt.
        self.assertEqual((await self.row(issued.record.id)).revoked_at, None)


@requires_postgres
class RotateTest(SessionTestCase):
    async def test_rotation_gives_a_new_id_and_kills_the_old_one(self):
        issued = await self.create()
        found = await self.authenticate(issued.token)
        new_token = await self.tx(
            lambda s: self.store.rotate(s, found.record.id, found.token_hash)
        )
        self.assertIsNotNone(new_token)
        self.assertNotEqual(new_token, issued.token)
        self.assertIsNone(await self.authenticate(issued.token))
        again = await self.authenticate(new_token)
        self.assertEqual(again.record.id, issued.record.id)  # the same session
        self.assertEqual((await self.row(issued.record.id)).rotated_at, T0)
        self.assertNotIn(new_token, await self.everything_stored())

    async def test_rotation_with_a_stale_id_does_nothing(self):
        issued = await self.create()
        found = await self.authenticate(issued.token)
        first = await self.tx(
            lambda s: self.store.rotate(s, found.record.id, found.token_hash)
        )
        second = await self.tx(
            lambda s: self.store.rotate(s, found.record.id, found.token_hash)
        )
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertIsNotNone(await self.authenticate(first))

    async def test_a_revoked_or_expired_session_cannot_be_rotated(self):
        issued = await self.create()
        found = await self.authenticate(issued.token)
        self.clock.now = T0 + timedelta(days=31)
        self.assertIsNone(
            await self.tx(
                lambda s: self.store.rotate(s, found.record.id, found.token_hash)
            )
        )
        self.clock.now = T0
        await self.tx(
            lambda s: self.store.revoke(
                s, found.record.id, self.alice.id, RevokeReason.LOGOUT
            )
        )
        self.assertIsNone(
            await self.tx(
                lambda s: self.store.rotate(s, found.record.id, found.token_hash)
            )
        )

    async def test_exactly_one_of_many_concurrent_rotations_wins(self):
        issued = await self.create()
        found = await self.authenticate(issued.token)

        async def rotate(services, index):
            database = services.provider._database
            async with database.session() as session:
                token = await services.sessions.rotate(
                    session, found.record.id, found.token_hash
                )
                await session.commit()
                return token

        results = await self.gather_on_own_engines(8, rotate)
        winners = [r for r in results if isinstance(r, str)]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual(sum(r is None for r in results), 7)
        self.assertIsNone(await self.authenticate(issued.token))
        self.assertIsNotNone(await self.authenticate(winners[0]))

    async def test_step_up_marks_the_session_and_rotates_it(self):
        issued = await self.create()
        found = await self.authenticate(issued.token)
        self.clock.now = T0 + timedelta(minutes=5)
        result = await self.tx(
            lambda s: self.store.record_step_up(
                s, found.record.id, found.token_hash, AuthMethod.PASSWORD
            )
        )
        # The session as it is now, under its new id.
        self.assertEqual(result.record.stepup_at, T0 + timedelta(minutes=5))
        self.assertEqual(result.record.stepup_method, AuthMethod.PASSWORD)
        self.assertEqual(result.record.id, found.record.id)
        self.assertNotEqual(result.token, issued.token)
        self.assertIsNone(await self.authenticate(issued.token))
        stepped = await self.authenticate(result.token)
        self.assertEqual(stepped.record.stepup_at, T0 + timedelta(minutes=5))
        self.assertEqual(stepped.record.stepup_method, AuthMethod.PASSWORD)
        # A stale id cannot record a step-up.
        self.assertIsNone(
            await self.tx(
                lambda s: self.store.record_step_up(
                    s, found.record.id, found.token_hash, AuthMethod.PASSWORD
                )
            )
        )


@requires_postgres
class RevokeTest(SessionTestCase):
    async def test_a_user_ends_only_their_own_session(self):
        bob = await self.make_user("bobby")
        mine = await self.create()
        theirs = await self.create(bob)
        self.assertFalse(
            await self.tx(
                lambda s: self.store.revoke(
                    s, theirs.record.id, self.alice.id, RevokeReason.REVOKED_BY_USER
                )
            )
        )
        self.assertIsNotNone(await self.authenticate(theirs.token))
        self.assertTrue(
            await self.tx(
                lambda s: self.store.revoke(
                    s, mine.record.id, self.alice.id, RevokeReason.REVOKED_BY_USER
                )
            )
        )
        row = await self.row(mine.record.id)
        self.assertEqual((row.revoked_at, row.revoked_reason), (T0, "revoked_by_user"))

    async def test_ending_a_session_twice_reports_nothing_the_second_time(self):
        issued = await self.create()
        self.assertTrue(
            await self.tx(
                lambda s: self.store.revoke(
                    s, issued.record.id, self.alice.id, RevokeReason.LOGOUT
                )
            )
        )
        self.assertFalse(
            await self.tx(
                lambda s: self.store.revoke(
                    s, issued.record.id, self.alice.id, RevokeReason.PASSWORD_CHANGED
                )
            )
        )
        self.assertEqual((await self.row(issued.record.id)).revoked_reason, "logout")

    async def test_revoke_all_ends_every_session_of_the_user_and_only_theirs(self):
        bob = await self.make_user("bobby")
        mine = [await self.create() for _ in range(3)]
        keep = await self.create(bob)
        count = await self.tx(
            lambda s: self.store.revoke_all(s, self.alice.id, RevokeReason.RECOVERY)
        )
        self.assertEqual(count, 3)
        for issued in mine:
            self.assertIsNone(await self.authenticate(issued.token))
        self.assertIsNotNone(await self.authenticate(keep.token))
        self.assertEqual(
            await self.scalar(
                "SELECT count(*) FROM auth_sessions WHERE revoked_reason = 'recovery'"
            ),
            3,
        )
        again = await self.tx(
            lambda s: self.store.revoke_all(s, self.alice.id, RevokeReason.RECOVERY)
        )
        self.assertEqual(again, 0)

    async def test_revoke_all_can_spare_the_current_session(self):
        current = await self.create()
        others = [await self.create() for _ in range(2)]
        count = await self.tx(
            lambda s: self.store.revoke_all(
                s,
                self.alice.id,
                RevokeReason.LOGOUT_OTHERS,
                except_id=current.record.id,
            )
        )
        self.assertEqual(count, 2)
        self.assertIsNotNone(await self.authenticate(current.token))
        for issued in others:
            self.assertIsNone(await self.authenticate(issued.token))

    async def test_revoke_by_token_ends_the_session_of_a_token_whoever_owns_it(self):
        issued = await self.create()
        self.assertIsNone(
            await self.tx(
                lambda s: self.store.revoke_by_token(s, "short", RevokeReason.REPLACED)
            )
        )
        self.assertIsNone(
            await self.tx(
                lambda s: self.store.revoke_by_token(
                    s, tokens.new_session_token(), RevokeReason.REPLACED
                )
            )
        )
        self.assertEqual(
            await self.tx(
                lambda s: self.store.revoke_by_token(
                    s, issued.token, RevokeReason.REPLACED
                )
            ),
            issued.record.id,
        )
        self.assertIsNone(await self.authenticate(issued.token))
        self.assertIsNone(
            await self.tx(
                lambda s: self.store.revoke_by_token(
                    s, issued.token, RevokeReason.REPLACED
                )
            )
        )

    async def test_an_unknown_reason_is_refused_before_the_database(self):
        issued = await self.create()
        for call in (
            lambda s: self.store.revoke(s, issued.record.id, self.alice.id, "boredom"),
            lambda s: self.store.revoke_all(s, self.alice.id, "boredom"),
        ):
            with self.assertRaises(InvalidAuthInputError):
                await self.tx(call)


@requires_postgres
class ListTest(SessionTestCase):
    async def test_the_list_holds_the_valid_sessions_most_recently_used_first(self):
        first = await self.create(device_label="phone")
        self.clock.advance(hours=1)
        second = await self.create(device_label="laptop")
        self.clock.advance(hours=1)
        third = await self.create(device_label="tablet")
        ended = await self.create(device_label="old")
        await self.tx(
            lambda s: self.store.revoke(
                s, ended.record.id, self.alice.id, RevokeReason.LOGOUT
            )
        )
        records = await self.tx(lambda s: self.store.list_active(s, self.alice.id))
        self.assertEqual(
            [r.device_label for r in records], ["tablet", "laptop", "phone"]
        )
        self.assertEqual(
            {r.id for r in records},
            {first.record.id, second.record.id, third.record.id},
        )

    async def test_a_session_of_another_user_and_an_expired_one_are_not_listed(self):
        bob = await self.make_user("bobby")
        await self.create(bob)
        expired = await self.create()
        self.clock.now = T0 + timedelta(days=31)
        live = await self.create()
        records = await self.tx(lambda s: self.store.list_active(s, self.alice.id))
        self.assertEqual([r.id for r in records], [live.record.id])
        self.assertNotIn(expired.record.id, [r.id for r in records])

    async def test_the_limit_is_validated(self):
        for bad in (0, -1, 101, True, "5", None, 2.5):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidAuthInputError):
                    await self.tx(
                        lambda s, limit=bad: self.store.list_active(
                            s, self.alice.id, limit
                        )
                    )
        await self.create()
        await self.create()
        self.assertEqual(
            len(await self.tx(lambda s: self.store.list_active(s, self.alice.id, 1))), 1
        )


class ConstructionTest(unittest.TestCase):
    def test_bad_arguments_are_refused(self):
        from paw_backend.auth.sessions import SessionLifetimes, SessionStore

        for bad in (0, -1, True, 1.5, "30"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    SessionLifetimes(bad, 1, 1, 1)
                with self.assertRaises(ValueError):
                    SessionLifetimes(1, 1, 1, bad)
        lifetimes = SessionLifetimes(1, 1, 1, 1)
        with self.assertRaises(TypeError):
            SessionStore("lifetimes", clock=lambda: T0)
        with self.assertRaises(TypeError):
            SessionStore(lifetimes, clock=None)
        naive = SessionStore(lifetimes, clock=lambda: datetime(2030, 1, 1))
        with self.assertRaises(ValueError):
            naive._now()


if __name__ == "__main__":
    unittest.main()
