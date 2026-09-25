"""Login: credentials, no user enumeration, backoff, audit (PAW-022, PostgreSQL)."""

import asyncio
import uuid
from datetime import timedelta
from unittest.mock import patch

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from paw_backend.auth import tokens
from paw_backend.auth.audit import AuthAudit
from paw_backend.auth.context import RequestContext
from paw_backend.auth.errors import (
    AuthUnavailableError,
    InvalidCredentialsError,
    ThrottledError,
)
from paw_backend.auth.models import ThrottleScope
from paw_backend.auth.passwords import HashParameters, PasswordHasher
from paw_backend.authz.audit import InMemoryAuditSink
from paw_backend.authz.roles import SystemRole
from paw_backend.db import Database

from .auth_support import (
    PASSWORD,
    T0,
    PostgresAuthTestCase,
    RecordingSink,
    fast_settings,
    requires_postgres,
)

SOURCE = "203.0.113.7"
SECRET_HASH_MARK = "$argon2id$"


def context(source: str = SOURCE) -> RequestContext:
    """A request from ``source`` (an address: the route makes its bucket)."""
    return RequestContext(uuid.uuid4(), tokens.source_bucket(source))


class LoginTestCase(PostgresAuthTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.alice = await self.make_user("alice")

    async def login(self, name="alice", password=PASSWORD, source=SOURCE, **options):
        return await self.auth.login(name, password, context(source), **options)

    async def fails(self, name="alice", password="not the password", source=SOURCE):
        with self.assertRaises(InvalidCredentialsError):
            await self.login(name, password, source)

    async def throttle_row(self, scope, key):
        rows = await self.query(
            "SELECT * FROM auth_throttles WHERE scope = :s AND key_hash = :k",
            s=scope.value,
            k=key,
        )
        return rows[0] if rows else None


@requires_postgres
class SuccessTest(LoginTestCase):
    async def test_the_right_password_starts_a_session(self):
        result = await self.login()
        self.assertEqual(result.session.record.user_id, self.alice.id)
        self.assertEqual(result.session.login_name, "alice")
        self.assertEqual(result.session.system_role, SystemRole.USER)
        rows = await self.query("SELECT * FROM auth_sessions")
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            bytes(rows[0].token_hash), tokens.hash_session_token(result.token)
        )

    async def test_a_normal_and_a_remember_me_session_differ_only_in_their_lifetime(
        self,
    ):
        normal = (await self.login()).session.record
        remember = (
            await self.login(remember_me=True, device_label=" phone ")
        ).session.record
        self.assertFalse(normal.remember_me)
        self.assertEqual(normal.idle_expires_at, T0 + timedelta(days=30))
        self.assertTrue(remember.remember_me)
        self.assertEqual(remember.idle_expires_at, T0 + timedelta(days=90))
        self.assertEqual(remember.device_label, "phone")
        self.assertNotEqual(normal.id, remember.id)

    async def test_every_login_gets_a_new_random_session_id(self):
        first = await self.login()
        second = await self.login()
        self.assertNotEqual(first.token, second.token)
        self.assertNotEqual(first.session.record.id, second.session.record.id)

    async def test_the_login_name_is_normalised(self):
        for spelling in ("ALICE", "  alice ", "ａｌｉｃｅ"):
            with self.subTest(spelling=spelling):
                result = await self.login(spelling)
                self.assertEqual(result.session.record.user_id, self.alice.id)

    async def test_a_success_is_audited_with_the_account_and_the_source_bucket_only(
        self,
    ):
        await self.login()
        (row,) = await self.audit_rows()
        self.assertEqual(
            (row.action, row.decision, row.reason, row.actor_id, row.actor_role),
            ("auth.login", "allow", "authenticated", self.alice.id, "user"),
        )
        self.assertEqual(
            (row.resource_kind, row.resource_id),
            ("login_source", tokens.source_audit_id(SOURCE)),
        )
        self.assertIsNone(row.agent_id)
        stored = await self.everything_stored()
        for secret in (PASSWORD, SOURCE, "alice" + "\x00"):
            self.assertNotIn(secret, stored)

    async def test_a_success_clears_the_accounts_counter_but_only_refunds_the_source(
        self,
    ):
        await self.fails()
        await self.fails()
        account = tokens.account_key("alice")
        source = tokens.source_key(SOURCE)
        self.assertEqual(
            (await self.throttle_row(ThrottleScope.LOGIN_ACCOUNT, account)).attempts, 2
        )
        await self.login()
        self.assertIsNone(await self.throttle_row(ThrottleScope.LOGIN_ACCOUNT, account))
        # 2 failures and 1 success from this source: the success only took
        # back its own count.
        self.assertEqual(
            (await self.throttle_row(ThrottleScope.LOGIN_SOURCE, source)).attempts, 2
        )

    async def test_a_browser_that_still_holds_a_session_has_it_replaced(self):
        old = await self.login()
        new = await self.login(replace_token=old.token)
        rows = {r.id: r for r in await self.query("SELECT * FROM auth_sessions")}
        self.assertEqual(rows[old.session.record.id].revoked_reason, "replaced")
        self.assertIsNone(rows[new.session.record.id].revoked_at)
        self.assertNotEqual(old.token, new.token)

    async def test_a_session_of_another_user_on_the_same_browser_is_replaced_too(self):
        bob = await self.make_user("bobby", password="another good passphrase")
        theirs = await self.auth.login("bobby", "another good passphrase", context())
        await self.login(replace_token=theirs.token)
        row = (
            await self.query(
                "SELECT * FROM auth_sessions WHERE id = :id",
                id=theirs.session.record.id,
            )
        )[0]
        self.assertEqual(row.revoked_reason, "replaced")
        self.assertEqual(bob.login_name, "bobby")

    async def test_a_replace_token_that_is_not_a_session_changes_nothing(self):
        other = await self.login()
        for junk in ("short", "A" * 43, "x" * 5000):
            with self.subTest(junk=junk[:8]):
                await self.login(replace_token=junk)
        self.assertIsNone(
            (
                await self.query(
                    "SELECT revoked_at FROM auth_sessions WHERE id = :id",
                    id=other.session.record.id,
                )
            )[0].revoked_at
        )

    async def test_a_session_id_fixed_by_an_attacker_is_never_adopted(self):
        planted = tokens.new_session_token()
        result = await self.login(replace_token=planted)
        self.assertNotEqual(result.token, planted)
        self.assertEqual(
            await self.scalar(
                "SELECT count(*) FROM auth_sessions WHERE token_hash = :h",
                h=tokens.hash_session_token(planted),
            ),
            0,
        )

    async def test_an_owner_and_an_admin_log_in_with_their_own_role(self):
        await self.make_user("boss", role="owner", password="the owner passphrase")
        await self.make_user("adm", role="admin", password="the admin passphrase")
        owner = await self.auth.login("boss", "the owner passphrase", context())
        admin = await self.auth.login("adm", "the admin passphrase", context())
        self.assertEqual(
            (owner.session.system_role, admin.session.system_role),
            (SystemRole.OWNER, SystemRole.ADMIN),
        )


@requires_postgres
class FailureTest(LoginTestCase):
    async def test_a_wrong_password_is_refused_and_audited_for_a_known_account(self):
        await self.fails()
        (row,) = await self.audit_rows()
        self.assertEqual(
            (row.action, row.decision, row.reason, row.actor_id, row.actor_role),
            ("auth.login", "deny", "invalid_credentials", self.alice.id, "user"),
        )
        self.assertEqual(row.resource_id, tokens.source_audit_id(SOURCE))
        self.assertEqual(await self.scalar("SELECT count(*) FROM auth_sessions"), 0)

    async def test_an_unknown_name_is_refused_and_leaves_no_audit_row_and_no_name(self):
        with self.assertLogs("paw_backend.auth.service", "INFO") as logs:
            await self.fails("nobody-here")
        self.assertEqual(await self.audit_rows(), [])
        self.assertNotIn("nobody-here", "\n".join(logs.output))
        self.assertNotIn("nobody-here", await self.everything_stored())

    async def test_a_name_that_is_not_a_login_name_is_refused_the_same_way(self):
        for name in (
            "",
            "  ",
            "a",
            "x" * 100,
            "with space",
            "ünïcode",
            "a/b",
            "\x00nul",
            "; DROP TABLE users",
        ):
            with self.subTest(name=name[:12]):
                await self.fails(name)
        self.assertEqual(await self.audit_rows(), [])

    async def test_the_accounts_that_cannot_log_in_are_refused_and_audited_apart(self):
        cases = {}
        for status in ("invited", "pending_deletion", "deleted"):
            await self.make_user(f"user-{status}".replace("_", "-"), status=status)
            cases[status] = f"user-{status}".replace("_", "-")
        await self.make_user("no-password", password=None)
        for name in [*cases.values(), "no-password"]:
            with self.subTest(name=name):
                await self.fails(name, PASSWORD)
        summary = await self.audit_summary()
        self.assertEqual(summary[("auth.login", "deny", "account_not_active")], 3)
        self.assertEqual(summary[("auth.login", "deny", "no_password")], 1)
        self.assertEqual(await self.scalar("SELECT count(*) FROM auth_sessions"), 0)

    async def test_the_answer_is_the_same_whatever_was_wrong(self):
        await self.make_user("no-password", password=None)
        await self.make_user("gone", status="deleted")
        errors = []
        for name, password in (
            ("alice", "wrong wrong wrong"),
            ("nobody-here", PASSWORD),
            ("no-password", PASSWORD),
            ("gone", PASSWORD),
            ("", PASSWORD),
        ):
            try:
                await self.login(name, password)
            except InvalidCredentialsError as error:
                errors.append(
                    (type(error), str(error), error.__cause__, error.__context__)
                )
        self.assertEqual(len(errors), 5)
        self.assertEqual(len(set(errors)), 1)

    async def test_a_failure_does_the_same_database_work_whoever_the_account_is(self):
        await self.make_user("no-password", password=None)
        counts = {}
        for label, name in (
            ("wrong password", "alice"),
            ("unknown", "nobody-here"),
            ("not a name", "not a name!"),
            ("no password", "no-password"),
        ):
            calls = []
            real = Database.run_abortable

            async def counting(database, work, _real=real, _calls=calls):
                _calls.append(1)
                return await _real(database, work)

            with patch.object(Database, "run_abortable", counting):
                with patch.object(
                    type(self.services.service._hasher),
                    "verify_unknown",
                    wraps=self.services.service._hasher.verify_unknown,
                ) as unknown:
                    with patch.object(
                        type(self.services.service._hasher),
                        "verify",
                        wraps=self.services.service._hasher.verify,
                    ) as known:
                        await self.fails(name, "wrong wrong wrong")
            counts[label] = (len(calls), known.call_count + unknown.call_count)
            await self.execute("TRUNCATE auth_throttles")
        # One database transaction (counting both attempts, looking the account
        # up) and one password verification, whoever the account is.
        self.assertEqual(set(counts.values()), {(1, 1)}, counts)

    async def test_a_password_that_cannot_be_one_is_refused_without_touching_anything(
        self,
    ):
        calls = []
        real = Database.run_abortable

        async def counting(database, work):
            calls.append(1)
            return await real(database, work)

        with patch.object(Database, "run_abortable", counting):
            for bad in ("nul\x00inside", "x" * 5000, "line\nbreak"):
                with self.subTest(bad=bad[:8]):
                    await self.fails("alice", bad)
        self.assertEqual(calls, [])
        self.assertEqual(await self.scalar("SELECT count(*) FROM auth_throttles"), 0)

    async def test_no_password_hash_or_session_id_reaches_a_log_line(self):
        with self.no_secret_in_logs(
            PASSWORD, SECRET_HASH_MARK, "wrong wrong wrong"
        ) as logs:
            await self.login()
            await self.fails("alice", "wrong wrong wrong")
            await self.fails("nobody-here", "wrong wrong wrong")
        self.assertNotIn("session_id", logs.application_text)

    async def test_a_denial_stays_a_denial_when_it_cannot_be_recorded(self):
        sink = RecordingSink()
        sink.fail = True
        services = self.build(self.service_database, sink=sink)
        with self.assertLogs("paw_backend.auth.audit", "ERROR") as logs:
            with self.assertRaises(InvalidCredentialsError):
                await services.service.login("alice", "wrong wrong wrong", context())
        text = "\n".join(logs.output)
        self.assertIn("Audit write failed (RuntimeError)", text)
        self.assertNotIn("audit-secret-detail-022", text)


@requires_postgres
class BackoffTest(LoginTestCase):
    async def test_five_wrong_passwords_lock_the_account_for_30_seconds(self):
        for _ in range(4):
            await self.fails()
        # The fifth is still judged; it fails and starts the lock.
        await self.fails()
        with self.assertRaises(ThrottledError) as caught:
            await self.login()  # the RIGHT password is not even looked at
        self.assertEqual(caught.exception.retry_after_seconds, 30)
        self.assertEqual(await self.scalar("SELECT count(*) FROM auth_sessions"), 0)

    async def test_the_lock_ends_and_the_right_password_then_works_and_clears_the_count(
        self,
    ):
        for _ in range(5):
            await self.fails()
        self.clock.advance(seconds=30)
        result = await self.login()
        self.assertEqual(result.session.record.user_id, self.alice.id)
        self.assertIsNone(
            await self.throttle_row(
                ThrottleScope.LOGIN_ACCOUNT, tokens.account_key("alice")
            )
        )

    async def test_a_wrong_password_after_the_first_lock_starts_a_longer_one(self):
        for _ in range(5):
            await self.fails()
        self.clock.advance(seconds=30)
        await self.fails()
        with self.assertRaises(ThrottledError) as caught:
            await self.login()
        self.assertEqual(caught.exception.retry_after_seconds, 60)

    async def test_the_backoff_of_an_unknown_name_is_the_same_as_of_a_real_one(self):
        traces = {}
        for name in ("alice", "nobody-here"):
            trace = []
            for _ in range(8):
                try:
                    await self.login(name, "wrong wrong wrong")
                    trace.append("ok")
                except InvalidCredentialsError:
                    trace.append("invalid")
                except ThrottledError as error:
                    trace.append(("locked", error.retry_after_seconds))
            traces[name] = trace
            await self.execute("TRUNCATE auth_throttles")
        self.assertEqual(traces["alice"], traces["nobody-here"])
        self.assertEqual(traces["alice"][:5], ["invalid"] * 5)
        self.assertEqual(traces["alice"][5], ("locked", 30))

    async def test_no_lock_lasts_over_an_hour_so_the_owner_is_never_locked_out(
        self,
    ):
        await self.make_user("boss", role="owner", password="the owner passphrase")
        for attempt in range(40):
            self.clock.now = T0 + timedelta(days=attempt // 2, hours=attempt % 2)
            try:
                await self.login("boss", "wrong wrong wrong")
            except (InvalidCredentialsError, ThrottledError):
                pass
        with self.assertRaises(ThrottledError) as caught:
            await self.login("boss", "the owner passphrase")
        self.assertLessEqual(caught.exception.retry_after_seconds, 3600)
        self.clock.advance(seconds=3600)
        owner = await self.auth.login("boss", "the owner passphrase", context())
        self.assertEqual(owner.session.system_role, SystemRole.OWNER)

    async def test_the_first_failure_that_starts_a_lock_is_audited_once(self):
        for _ in range(5):
            await self.fails()
        with self.assertRaises(ThrottledError):
            await self.login()
        with self.assertRaises(ThrottledError):
            await self.login()
        summary = await self.audit_summary()
        self.assertEqual(summary[("auth.login", "deny", "invalid_credentials")], 5)
        self.assertEqual(summary[("auth.lockout", "deny", "backoff_started")], 1)
        # Refused attempts while locked write nothing: anybody could otherwise
        # fill an append-only table.
        self.assertEqual(sum(summary.values()), 6)

    async def test_a_locked_account_of_the_owner_is_recorded_for_the_owner_to_see(self):
        owner = await self.make_user(
            "boss", role="owner", password="the owner passphrase"
        )
        for _ in range(5):
            with self.assertRaises(InvalidCredentialsError):
                await self.login("boss", "wrong wrong wrong")
        rows = [r for r in await self.audit_rows() if r.actor_id == owner.id]
        self.assertEqual({r.actor_role for r in rows}, {"owner"})
        self.assertEqual({r.action for r in rows}, {"auth.login", "auth.lockout"})

    async def test_one_account_being_locked_does_not_lock_another(self):
        await self.make_user("bobby", password="another good passphrase")
        for _ in range(5):
            await self.fails()
        result = await self.auth.login(
            "bobby", "another good passphrase", context("198.51.100.9")
        )
        self.assertEqual(result.session.login_name, "bobby")

    async def test_the_source_is_locked_after_twenty_attempts_on_any_accounts(self):
        for index in range(20):
            await self.fails(f"guess-{index:03d}")
        with self.assertRaises(ThrottledError) as caught:
            await self.login()  # a correct login from the locked source
        self.assertEqual(caught.exception.retry_after_seconds, 30)
        # Another source is not affected, and can log in.
        result = await self.login(source="198.51.100.9")
        self.assertEqual(result.session.record.user_id, self.alice.id)

    async def test_a_source_lock_does_not_touch_the_account_counter_of_refused_attempts(
        self,
    ):
        for index in range(20):
            await self.fails(f"guess-{index:03d}")
        for _ in range(3):
            with self.assertRaises(ThrottledError):
                await self.login()
        self.assertIsNone(
            await self.throttle_row(
                ThrottleScope.LOGIN_ACCOUNT, tokens.account_key("alice")
            )
        )

    async def test_an_ipv6_source_is_counted_per_64_bit_prefix(self):
        for index in range(20):
            await self.fails(f"guess-{index:03d}", source=f"2001:db8:1:2::{index + 1}")
        with self.assertRaises(ThrottledError):
            await self.login(source="2001:db8:1:2:ffff::1")
        result = await self.login(source="2001:db8:1:3::1")
        self.assertEqual(result.session.record.user_id, self.alice.id)

    async def test_the_attempts_that_race_each_other_are_bounded_by_the_backoff(self):
        wrong = "wrong wrong wrong"

        async def attempt(services, index):
            return await services.service.login(
                "alice", wrong, context(f"192.0.2.{index + 1}")
            )

        results = await self.gather_on_own_engines(20, attempt)
        judged = [r for r in results if isinstance(r, InvalidCredentialsError)]
        locked = [r for r in results if isinstance(r, ThrottledError)]
        self.assertEqual(len(judged) + len(locked), 20, results)
        # Twenty different sources, one account: at most five passwords are compared.
        self.assertEqual(len(judged), 5)
        self.assertEqual(len(locked), 15)

    async def test_a_racing_correct_password_cannot_slip_past_a_lock_that_exists(self):
        for _ in range(5):
            await self.fails()

        async def attempt(services, index):
            return await services.service.login(
                "alice", PASSWORD, context(f"192.0.2.{index + 1}")
            )

        results = await self.gather_on_own_engines(8, attempt)
        self.assertTrue(all(isinstance(r, ThrottledError) for r in results), results)
        self.assertEqual(await self.scalar("SELECT count(*) FROM auth_sessions"), 0)


@requires_postgres
class RehashAndRaceTest(LoginTestCase):
    async def test_a_password_stored_with_older_parameters_is_rehashed_at_login(self):
        old = PasswordHasher(HashParameters(2, 19_456, 1), concurrency=1)
        self.addCleanup(old.close)
        older = await old.hash(PASSWORD)
        await self.execute("UPDATE password_credentials SET hash = :h", h=older)
        await self.login()
        stored = await self.scalar("SELECT hash FROM password_credentials")
        self.assertNotEqual(stored, older)
        self.assertTrue(stored.startswith("$argon2id$v=19$m=19456,t=1,p=1$"))
        self.assertFalse(self.services.service._hasher.needs_rehash(stored))
        # ... and the new hash verifies the same password.
        await self.login()

    async def test_a_current_hash_is_left_alone(self):
        before = await self.scalar("SELECT hash FROM password_credentials")
        await self.login()
        self.assertEqual(
            await self.scalar("SELECT hash FROM password_credentials"), before
        )

    async def test_credentials_that_change_after_the_check_do_not_produce_a_session(
        self,
    ):
        hasher = self.services.service._hasher
        real = type(hasher).verify

        async def verify_then_change(self_, encoded, password):
            matched = await real(self_, encoded, password)
            other = await hasher.hash("some other passphrase")
            async with self.service_database.session() as session:
                await session.execute(
                    text("UPDATE password_credentials SET hash = :h"),
                    {"h": other},
                )
                await session.commit()
            return matched

        with patch.object(type(hasher), "verify", verify_then_change):
            with self.assertRaises(InvalidCredentialsError):
                await self.login()
        self.assertEqual(await self.scalar("SELECT count(*) FROM auth_sessions"), 0)
        summary = await self.audit_summary()
        self.assertEqual(summary[("auth.login", "deny", "credentials_changed")], 1)

    async def test_a_login_that_waits_for_a_password_reset_is_refused_afterwards(self):
        # A reset holds the user row; a login that verified the old password
        # and then waits for that row must not create a session once it is free.
        holder = self.new_database()
        async with holder.session() as session:
            await session.execute(
                text("SELECT id FROM users WHERE id = :id FOR UPDATE"),
                {"id": self.alice.id},
            )
            task = asyncio.create_task(self.login())
            await self.wait_for_a_lock_wait()
            new_hash = await self.services.service._hasher.hash(
                "a different passphrase"
            )
            await session.execute(
                text("UPDATE password_credentials SET hash = :h"),
                {"h": new_hash},
            )
            await session.commit()
        with self.assertRaises(InvalidCredentialsError):
            await task
        self.assertEqual(await self.scalar("SELECT count(*) FROM auth_sessions"), 0)

    async def wait_for_a_lock_wait(self):
        for _ in range(3000):
            waiting = await self.scalar(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            )
            if waiting:
                return
            await asyncio.sleep(0.02)
        self.fail("nothing waits for a lock")


@requires_postgres
class FailClosedTest(LoginTestCase):
    async def test_a_login_whose_audit_row_cannot_be_written_creates_no_session(self):
        class Failing(AuthAudit):
            async def record_in(self, session, event):
                raise OperationalError(
                    "INSERT", {}, Exception("audit-secret-detail-022")
                )

        service = self.services.service
        service._audit = Failing(
            InMemoryAuditSink(), timeout_seconds=1, clock=self.clock
        )
        with self.assertLogs("paw_backend.auth.db", "ERROR") as logs:
            with self.assertRaises(AuthUnavailableError):
                await self.login()
        self.assertNotIn("audit-secret-detail-022", "\n".join(logs.output))
        self.assertIn("OperationalError", "\n".join(logs.output))
        self.assertEqual(await self.scalar("SELECT count(*) FROM auth_sessions"), 0)

    async def test_an_unreachable_database_is_unavailable_not_a_wrong_password(self):
        database = Database(
            fast_settings(database_url="postgresql://u:p@127.0.0.1:1/none")
        )
        self.addAsyncCleanup(database.dispose)
        services = self.build(database)
        with self.assertLogs("paw_backend.auth.db", "ERROR") as logs:
            with self.assertRaises(AuthUnavailableError):
                await services.service.login("alice", PASSWORD, context())
        self.assertNotIn("127.0.0.1", "\n".join(logs.output))

    async def test_a_database_that_is_not_configured_is_unavailable(self):
        database = Database(fast_settings(database_url=None))
        services = self.build(database)
        with self.assertRaises(AuthUnavailableError):
            await services.service.login("alice", PASSWORD, context())


if __name__ == "__main__":
    import unittest

    unittest.main()
