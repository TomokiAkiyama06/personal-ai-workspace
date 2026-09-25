"""Every public method refuses bad input with its typed error, before the database.

Table driven, in the style of ``tests/test_task_argument_validation.py``: for each
method and each argument a list of bad values. The database is replaced by one
that fails the test if it is reached, so "before any database access" is checked,
not assumed. Needs no PostgreSQL.
"""

import unittest
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.auth import tokens
from paw_backend.auth.context import RequestContext
from paw_backend.auth.errors import (
    InvalidAuthInputError,
    InvalidCredentialsError,
)
from paw_backend.auth.models import AuthMethod, RevokeReason
from paw_backend.auth.sessions import (
    AuthenticatedSession,
    SessionLifetimes,
    SessionRecord,
    SessionStore,
)
from paw_backend.auth.state import StepUpEvidence
from paw_backend.auth.wiring import build_auth
from paw_backend.authz import Principal, SystemRole
from paw_backend.db import Database

from .support import make_settings

T0 = datetime(2030, 1, 1, tzinfo=UTC)
UID = uuid.uuid4()
CONTEXT = RequestContext(uuid.uuid4(), "203.0.113.7")
RECORD = SessionRecord(
    id=uuid.uuid4(),
    user_id=UID,
    remember_me=False,
    auth_method=AuthMethod.PASSWORD,
    device_label=None,
    created_at=T0,
    last_used_at=T0,
    idle_expires_at=T0,
    absolute_expires_at=T0,
    stepup_at=None,
    stepup_method=None,
)
AUTH = AuthenticatedSession(
    record=RECORD,
    login_name="alice",
    system_role=SystemRole.USER,
    checked_at=T0,
    token_hash=b"h" * 32,
)
ADMIN = Principal(uuid.uuid4(), SystemRole.ADMIN)
GOOD_PASSWORD = "a passphrase that is long enough"

NOT_A_STRING = [None, 5, b"bytes", ["a"], 1.5, True, object()]
NOT_A_BOOL = [None, 0, 1, "true", "", [], 2]
NOT_A_UUID = [None, "not-a-uuid", str(uuid.uuid4()), 5, b"x", uuid.uuid4().hex]
NOT_AUTH = [None, "auth", RECORD, {"record": RECORD}, 5]
NOT_CONTEXT = [None, "context", 5, {"correlation_id": 1}, uuid.uuid4()]


class NoDatabase(Database):
    """A database the test fails on when anything reaches for it."""

    async def run_abortable(self, work):
        raise AssertionError("the database was reached")

    async def fetch_abortable(self, *args, **kwargs):
        raise AssertionError("the database was reached")


class ArgumentValidationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        settings = make_settings(
            database_url="postgresql://u:p@127.0.0.1:1/none",
            password_hash_time_cost=1,
            password_hash_memory_kib=19_456,
            password_hash_parallelism=1,
        )
        self.database = NoDatabase(settings)
        self.services = build_auth(settings, self.database, clock=lambda: T0)
        self.addCleanup(self.services.close)
        self.auth = self.services.service

    async def refused(self, call, error=InvalidAuthInputError):
        with self.assertRaises(error):
            await call()

    async def test_login(self):
        good = ["alice", GOOD_PASSWORD, CONTEXT]
        options = {
            "remember_me": False,
            "device_label": None,
            "replace_token": None,
        }
        bad = {
            0: [*NOT_A_STRING, "x" * 129],
            2: NOT_CONTEXT,
        }
        for index, values in bad.items():
            for value in values:
                args = list(good)
                args[index] = value
                with self.subTest(argument=index, value=repr(value)[:20]):
                    await self.refused(lambda a=args: self.auth.login(*a, **options))
        for name, values in {
            "remember_me": NOT_A_BOOL,
            "device_label": [5, b"x", "x" * 65, "bell\x07", "nul\x00", "a\nb"],
            "replace_token": [5, b"x" * 43, ["a"], True],
        }.items():
            for value in values:
                with self.subTest(option=name, value=repr(value)[:20]):
                    await self.refused(
                        lambda n=name, v=value: self.auth.login(
                            *good, **{**options, n: v}
                        )
                    )

    async def test_a_password_that_is_not_a_string_is_a_typed_error(self):
        for value in NOT_A_STRING:
            with self.subTest(value=repr(value)[:20]):
                await self.refused(lambda v=value: self.auth.login("alice", v, CONTEXT))

    async def test_a_password_that_cannot_be_one_is_the_wrong_password_not_an_error(
        self,
    ):
        for value in ("nul\x00inside", "x" * 5000, "line\nbreak", "lone\ud800"):
            with self.subTest(value=repr(value)[:20]):
                await self.refused(
                    lambda v=value: self.auth.login("alice", v, CONTEXT),
                    InvalidCredentialsError,
                )

    async def test_view_list_logout_and_the_revocations(self):
        for bad in NOT_AUTH:
            with self.subTest(auth=repr(bad)[:20]):
                await self.refused(lambda b=bad: self.auth.view(b))
                await self.refused(lambda b=bad: self.auth.list_sessions(b))
                await self.refused(lambda b=bad: self.auth.logout(b, CONTEXT))
                await self.refused(
                    lambda b=bad: self.auth.revoke_other_sessions(b, CONTEXT)
                )
                await self.refused(
                    lambda b=bad: self.auth.revoke_session(b, uuid.uuid4(), CONTEXT)
                )
        for bad in NOT_CONTEXT:
            with self.subTest(context=repr(bad)[:20]):
                await self.refused(lambda b=bad: self.auth.logout(AUTH, b))
                await self.refused(
                    lambda b=bad: self.auth.revoke_other_sessions(AUTH, b)
                )
                await self.refused(
                    lambda b=bad: self.auth.revoke_session(AUTH, uuid.uuid4(), b)
                )
        for bad in NOT_A_UUID:
            with self.subTest(session_id=repr(bad)[:20]):
                await self.refused(
                    lambda b=bad: self.auth.revoke_session(AUTH, b, CONTEXT)
                )

    async def test_revoke_all_sessions_of(self):
        for bad in NOT_A_UUID:
            with self.subTest(user_id=repr(bad)[:20]):
                await self.refused(
                    lambda b=bad: self.auth.revoke_all_sessions_of(
                        b, RevokeReason.ACCOUNT_CLOSED
                    )
                )
        for bad in ("boredom", None, 5, "LOGOUT", b"logout"):
            with self.subTest(reason=repr(bad)):
                await self.refused(
                    lambda b=bad: self.auth.revoke_all_sessions_of(UID, b)
                )

    async def test_change_password(self):
        good = [AUTH, GOOD_PASSWORD, "another long passphrase", CONTEXT]
        for index, values in {
            0: NOT_AUTH,
            1: NOT_A_STRING,
            2: NOT_A_STRING,
            3: NOT_CONTEXT,
        }.items():
            for value in values:
                args = list(good)
                args[index] = value
                with self.subTest(argument=index, value=repr(value)[:20]):
                    await self.refused(lambda a=args: self.auth.change_password(*a))
        for value in NOT_A_BOOL:
            with self.subTest(revoke=repr(value)):
                await self.refused(
                    lambda v=value: self.auth.change_password(
                        *good, revoke_other_sessions=v
                    )
                )

    async def test_change_password_refuses_a_bad_new_password_before_the_database(self):
        from paw_backend.auth.errors import PasswordPolicyError

        for new in ("short", "password123", "x" * 500, "nul\x00-in-password"):
            with self.subTest(new=repr(new)[:12]):
                await self.refused(
                    lambda n=new: self.auth.change_password(
                        AUTH, GOOD_PASSWORD, n, CONTEXT
                    ),
                    PasswordPolicyError,
                )

    async def test_step_up(self):
        evidence = StepUpEvidence(AuthMethod.PASSWORD, GOOD_PASSWORD)
        for bad in NOT_AUTH:
            with self.subTest(auth=repr(bad)[:20]):
                await self.refused(
                    lambda b=bad: self.auth.step_up(b, evidence, CONTEXT)
                )
        for bad in [None, "evidence", GOOD_PASSWORD, {"method": "password"}, 5]:
            with self.subTest(evidence=repr(bad)[:20]):
                await self.refused(lambda b=bad: self.auth.step_up(AUTH, b, CONTEXT))
        for bad in NOT_CONTEXT:
            with self.subTest(context=repr(bad)[:20]):
                await self.refused(lambda b=bad: self.auth.step_up(AUTH, evidence, b))
        # A method nobody registered is refused before the database too.
        await self.refused(
            lambda: self.auth.step_up(AUTH, StepUpEvidence(AuthMethod.PASSKEY), CONTEXT)
        )

    async def test_the_step_up_evidence_checks_itself(self):
        for method in ("sms", "", None, 5, "PASSWORD"):
            with self.subTest(method=repr(method)):
                with self.assertRaises(InvalidAuthInputError):
                    StepUpEvidence(method, "x")
        for password in (5, b"x", ["a"], True):
            with self.subTest(password=repr(password)):
                with self.assertRaises(InvalidAuthInputError):
                    StepUpEvidence(AuthMethod.PASSWORD, password)
        self.assertEqual(StepUpEvidence("password", None).method, AuthMethod.PASSWORD)
        self.assertNotIn(
            "hunter2", repr(StepUpEvidence(AuthMethod.PASSWORD, "hunter2"))
        )

    async def test_redeem_owner_token(self):
        good = ["pawst1.a.b", GOOD_PASSWORD, CONTEXT]
        for index, values in {
            0: [*NOT_A_STRING, "t" * 513],
            1: NOT_A_STRING,
            2: NOT_CONTEXT,
        }.items():
            for value in values:
                args = list(good)
                args[index] = value
                with self.subTest(argument=index, value=repr(value)[:20]):
                    await self.refused(lambda a=args: self.auth.redeem_owner_token(*a))

    async def test_unlock_account(self):
        for index, values in {
            0: [None, "admin", AUTH, uuid.uuid4()],
            1: NOT_A_UUID,
            2: NOT_CONTEXT,
        }.items():
            for value in values:
                args = [ADMIN, uuid.uuid4(), CONTEXT]
                args[index] = value
                with self.subTest(argument=index, value=repr(value)[:20]):
                    await self.refused(lambda a=args: self.auth.unlock_account(*a))

    async def test_a_user_that_may_not_unlock_is_refused_before_the_database_too(self):
        from paw_backend.auth.errors import AuthPermissionError

        user = Principal(uuid.uuid4(), SystemRole.USER)
        with patch.object(self.services.service._audit, "record_best_effort"):
            await self.refused(
                lambda: self.auth.unlock_account(user, uuid.uuid4(), CONTEXT),
                AuthPermissionError,
            )

    async def test_the_policy_service(self):
        policy = self.services.policy
        good = dict(
            expected_version=1,
            passkey_owner="required",
            passkey_admin="required",
            passkey_user="optional",
            recommend_passkey_to_users=True,
            stepup_window_minutes=30,
        )
        owner = Principal(uuid.uuid4(), SystemRole.OWNER)
        for args in (
            (None, uuid.uuid4(), CONTEXT),
            (owner, None, CONTEXT),
            (owner, uuid.uuid4(), None),
        ):
            with self.subTest(args=repr(args)[:30]):
                await self.refused(lambda a=args: policy.update(*a, **good))
        for name, values in {
            "expected_version": [0, True, "1", None, 2**31],
            "passkey_owner": ["always", None, 1],
            "passkey_admin": ["", None, True],
            "passkey_user": ["Optional", None, b"optional"],
            "recommend_passkey_to_users": [1, "true", None],
            "stepup_window_minutes": [4, 241, True, "30", None],
        }.items():
            for value in values:
                with self.subTest(name=name, value=repr(value)):
                    await self.refused(
                        lambda n=name, v=value: policy.update(
                            owner, uuid.uuid4(), CONTEXT, **{**good, n: v}
                        )
                    )


class StoreArgumentTest(unittest.IsolatedAsyncioTestCase):
    """The session store and the throttle refuse bad arguments before any query."""

    async def asyncSetUp(self):
        settings = make_settings(database_url="postgresql://u:p@127.0.0.1:1/none")
        self.services = build_auth(settings, NoDatabase(settings), clock=lambda: T0)
        self.addCleanup(self.services.close)
        self.store = self.services.sessions
        self.throttle = self.services.throttle
        self.session = AsyncMock(spec=AsyncSession)
        self.session.execute.side_effect = AssertionError("queried")
        self.uid = uuid.uuid4()
        self.hash = b"h" * 32

    async def refused(self, call):
        self.session.execute.reset_mock()
        with self.assertRaises(InvalidAuthInputError):
            await call()
        self.session.execute.assert_not_called()

    def bad_sessions(self):
        return [None, "session", object(), 5]

    async def test_every_store_method_checks_the_session_first(self):
        s = self.store
        calls = {
            "create": lambda x: s.create(x, self.uid, remember_me=False),
            "authenticate": lambda x: s.authenticate(x, "A" * 43),
            "rotate": lambda x: s.rotate(x, self.uid, self.hash),
            "record_step_up": lambda x: s.record_step_up(
                x, self.uid, self.hash, "password"
            ),
            "revoke": lambda x: s.revoke(x, self.uid, self.uid, "logout"),
            "revoke_by_token": lambda x: s.revoke_by_token(x, "A" * 43, "logout"),
            "revoke_all": lambda x: s.revoke_all(x, self.uid, "logout"),
            "list_active": lambda x: s.list_active(x, self.uid),
            "purge": lambda x: s.purge(x),
        }
        for name, call in calls.items():
            for bad in self.bad_sessions():
                with self.subTest(method=name, session=repr(bad)[:12]):
                    await self.refused(lambda c=call, b=bad: c(b))

    async def test_every_store_method_checks_its_other_arguments(self):
        s, session = self.store, self.session
        not_uuid = [None, "x", str(self.uid), 5, b"x" * 16]
        not_hash = [None, "h" * 32, b"h" * 31, b"h" * 33, bytearray(32), 5]
        not_reason = [None, "boredom", "LOGOUT", 5, b"logout"]
        cases = []
        for bad in not_uuid:
            cases += [
                ("create user", lambda b=bad: s.create(session, b, remember_me=False)),
                ("rotate id", lambda b=bad: s.rotate(session, b, self.hash)),
                (
                    "step-up id",
                    lambda b=bad: s.record_step_up(session, b, self.hash, "password"),
                ),
                ("revoke id", lambda b=bad: s.revoke(session, b, self.uid, "logout")),
                ("revoke user", lambda b=bad: s.revoke(session, self.uid, b, "logout")),
                ("revoke_all user", lambda b=bad: s.revoke_all(session, b, "logout")),
                (
                    "revoke_all except",
                    lambda b=bad: (
                        s.revoke_all(session, self.uid, "logout", except_id=b)
                        if b is not None
                        else s.create(session, "x", remember_me=False)
                    ),
                ),
                ("list user", lambda b=bad: s.list_active(session, b)),
            ]
        for bad in not_hash:
            cases += [
                ("rotate hash", lambda b=bad: s.rotate(session, self.uid, b)),
                (
                    "step-up hash",
                    lambda b=bad: s.record_step_up(session, self.uid, b, "password"),
                ),
            ]
        for bad in not_reason:
            cases += [
                (
                    "revoke reason",
                    lambda b=bad: s.revoke(session, self.uid, self.uid, b),
                ),
                ("revoke_all reason", lambda b=bad: s.revoke_all(session, self.uid, b)),
                (
                    "revoke_by_token reason",
                    lambda b=bad: s.revoke_by_token(session, "A" * 43, b),
                ),
            ]
        for bad in (None, 0, 1, "true", []):
            cases.append(
                (
                    "create remember_me",
                    lambda b=bad: s.create(session, self.uid, remember_me=b),
                )
            )
        for bad in (None, "sms", "PASSWORD", 5):
            cases.append(
                (
                    "create method",
                    lambda b=bad: s.create(
                        session, self.uid, remember_me=False, auth_method=b
                    ),
                )
            )
            cases.append(
                (
                    "step-up method",
                    lambda b=bad: s.record_step_up(session, self.uid, self.hash, b),
                )
            )
        for bad in (5, b"x", "x" * 65, "bell\x07"):
            cases.append(
                (
                    "create label",
                    lambda b=bad: s.create(
                        session, self.uid, remember_me=False, device_label=b
                    ),
                )
            )
        for name, call in cases:
            with self.subTest(argument=name):
                await self.refused(call)

    async def test_the_throttle_checks_its_arguments_first(self):
        t, session = self.throttle, self.session
        from paw_backend.auth.models import ThrottleScope

        good = tokens.account_key("alice")
        scope = ThrottleScope.LOGIN_ACCOUNT
        for bad in self.bad_sessions():
            with self.subTest(session=repr(bad)[:12]):
                await self.refused(lambda b=bad: t.reserve_in(b, scope, good))
                await self.refused(lambda b=bad: t.reset_in(b, scope, good))
        for bad in (None, [], "reservation", (scope, good)):
            await self.refused(lambda b=bad: t.succeed_in(session, b))
        for bad in (
            None,
            "x",
            [],
            [(scope, good)] * 5,
            [scope],
            [(scope,)],
            [(scope, good, 1)],
            [("login_account", good)],
            [(scope, good[:5])],
            5,
            {(scope, good)},
        ):
            with self.subTest(keys=repr(bad)[:30]):
                with self.assertRaises(InvalidAuthInputError):
                    await t.reserve_many(bad)

    def test_the_key_helpers_refuse_what_is_not_text(self):
        for helper in (
            tokens.account_key,
            tokens.source_key,
            tokens.source_audit_id,
            tokens.hash_session_token,
        ):
            for bad in (None, 5, b"x", ["a"], object()):
                with self.subTest(helper=helper.__name__, bad=repr(bad)[:10]):
                    with self.assertRaises(InvalidAuthInputError):
                        helper(bad)
        with self.assertRaises(InvalidAuthInputError):
            tokens.hash_session_token("é" * 43)

    async def test_the_audit_and_service_constructors_check_their_parts(self):
        from paw_backend.auth.audit import AuthAudit
        from paw_backend.auth.service import AuthService
        from paw_backend.authz.audit import InMemoryAuditSink

        sink = InMemoryAuditSink()
        for args, error in (
            ((object(),), TypeError),
            ((sink,), None),
        ):
            if error:
                with self.assertRaises(error):
                    AuthAudit(*args, timeout_seconds=1)
        for timeout in (0, -1, 61, True, "1", None):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):
                    AuthAudit(sink, timeout_seconds=timeout)
        with self.assertRaises(TypeError):
            AuthAudit(sink, timeout_seconds=1, clock="now")
        service = self.services.service
        parts = dict(
            hasher=service._hasher,
            sessions=service._sessions,
            throttle=service._throttle,
            audit=service._audit,
            policy=service._policy,
        )
        database = self.services.provider._database
        for changes, error in (
            ({"redeemer": object()}, TypeError),
            ({"passkeys": object()}, TypeError),
            ({"timeout_seconds": 0}, ValueError),
            ({"timeout_seconds": True}, ValueError),
            ({"step_up_verifiers": {"password": object()}}, TypeError),
            ({"step_up_verifiers": {AuthMethod.PASSKEY: object()}}, TypeError),
            ({"credential_invalidators": [object()]}, TypeError),
        ):
            with self.subTest(changes=list(changes)):
                with self.assertRaises(error):
                    AuthService(database, **parts, **changes)

    def test_the_build_and_the_middleware_check_their_arguments(self):
        from paw_backend.auth.csrf import OriginCheckMiddleware

        settings = make_settings()
        for args in (("settings", NoDatabase(settings)), (settings, "database")):
            with self.assertRaises(TypeError):
                build_auth(*args)
        with self.assertRaises(TypeError):
            build_auth(settings, NoDatabase(settings), clock="now")
        for bad in ("https://a.example", [5], [None], 5):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(TypeError):
                    OriginCheckMiddleware(lambda *a: None, allowed_origins=bad)


class ContextTest(unittest.TestCase):
    def test_a_context_checks_its_fields_and_drops_a_bad_request_id(self):
        for args in (
            (None, "203.0.113.7"),
            ("id", "203.0.113.7"),
            (uuid.uuid4(), ""),
            (uuid.uuid4(), None),
            (uuid.uuid4(), 5),
        ):
            with self.subTest(args=repr(args)[:30]):
                with self.assertRaises(TypeError):
                    RequestContext(*args)
        context = RequestContext(uuid.uuid4(), "203.0.113.7", "bad id with spaces")
        self.assertIsNone(context.client_request_id)
        self.assertEqual(
            RequestContext(uuid.uuid4(), "x", "req-1").client_request_id, "req-1"
        )


class ConstructionTest(unittest.TestCase):
    def test_the_service_refuses_parts_of_the_wrong_type(self):
        settings = make_settings(database_url="postgresql://u:p@127.0.0.1:1/none")
        services = build_auth(settings, Database(settings), clock=lambda: T0)
        self.addCleanup(services.close)
        from paw_backend.auth.service import AuthService

        parts = dict(
            hasher=services.service._hasher,
            sessions=services.service._sessions,
            throttle=services.service._throttle,
            audit=services.service._audit,
            policy=services.service._policy,
        )
        for name in parts:
            with self.subTest(name=name):
                with self.assertRaises(TypeError):
                    AuthService(Database(settings), **{**parts, name: object()})
        with self.assertRaises(TypeError):
            AuthService("database", **parts)

    def test_the_session_store_needs_lifetimes_and_a_clock(self):
        with self.assertRaises(TypeError):
            SessionStore(None, clock=lambda: T0)
        self.assertEqual(SessionLifetimes(1, 2, 3, 4).touch_interval_seconds, 4)

    def test_the_source_helpers_cannot_be_given_a_bucket_that_is_not_one(self):
        self.assertEqual(tokens.source_bucket("2001:db8::1"), "2001:db8::/64")


if __name__ == "__main__":
    unittest.main()
