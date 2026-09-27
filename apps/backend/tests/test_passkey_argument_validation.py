"""Every public Passkey method refuses bad input with a typed error, before the DB.

Table driven, in the style of ``test_auth_argument_validation``: for each method and
each argument a list of bad values; the database is replaced by one that fails the test
if it is reached, and an ``AsyncSession`` stand-in fails it if a statement is executed,
so "before any database access" is checked, not assumed. Needs no PostgreSQL.
"""

import unittest
import uuid
from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.auth.audit import AuthReason
from paw_backend.auth.errors import InvalidAuthInputError, PasskeyUnavailableError
from paw_backend.auth.models import AuthMethod
from paw_backend.auth.passkeys import ceremony
from paw_backend.auth.passkeys.approvals import PasskeyApprovalStepUp
from paw_backend.auth.passkeys.models import PasskeyPurpose, PasskeyRevokeReason
from paw_backend.auth.passkeys.service import PasskeyService, PasskeyStepUpVerifier
from paw_backend.auth.passkeys.store import PasskeyRegistry
from paw_backend.auth.sessions import SessionStore
from paw_backend.auth.state import StepUpEvidence, StepUpRefused
from paw_backend.auth.stepup import read_freshness_in
from paw_backend.auth.wiring import build_auth
from paw_backend.authz import Capability, require_capability

from .passkey_pg_support import PASSKEY_SETTINGS
from .support import make_settings
from .test_auth_argument_validation import (
    AUTH,
    CONTEXT,
    NOT_A_UUID,
    NOT_AUTH,
    NOT_CONTEXT,
    T0,
    NoDatabase,
)
from .test_passkey_types import registration_answer

NOT_BYTES = [None, "text", 5, [b"x"], bytearray(b"x" * 32), uuid.uuid4()]
NOT_A_SESSION = [None, "session", 5, object(), uuid.uuid4()]


def session_double() -> AsyncMock:
    """Passes ``isinstance(x, AsyncSession)`` and fails the test if it is used."""
    double = AsyncMock(spec=AsyncSession)
    double.execute.side_effect = AssertionError("a statement was executed")
    return double


class ValidationCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        settings = make_settings(
            database_url="postgresql://u:p@127.0.0.1:1/none",
            password_hash_time_cost=1,
            password_hash_memory_kib=19_456,
            password_hash_parallelism=1,
            **PASSKEY_SETTINGS,
        )
        self.settings = settings
        self.database = NoDatabase(settings)
        self.services = build_auth(settings, self.database, clock=lambda: T0)
        self.addCleanup(self.services.close)
        self.auth = self.services.service
        self.passkeys = self.services.passkeys
        self.registry = self.passkeys._registry
        self.session = session_double()

    async def refused(self, call, error=InvalidAuthInputError):
        with self.assertRaises(error):
            await call()
        self.session.execute.assert_not_called()


class ServiceTest(ValidationCase):
    async def test_register_begin(self):
        for index, values in {0: NOT_AUTH, 1: NOT_CONTEXT}.items():
            for value in values:
                args = [AUTH, CONTEXT]
                args[index] = value
                with self.subTest(argument=index, value=repr(value)[:20]):
                    await self.refused(lambda a=args: self.passkeys.register_begin(*a))

    async def test_authenticate_begin_and_the_list(self):
        for value in NOT_AUTH:
            with self.subTest(value=repr(value)[:20]):
                await self.refused(lambda v=value: self.passkeys.list_passkeys(v))
                await self.refused(
                    lambda v=value: self.passkeys.authenticate_begin(v, CONTEXT)
                )
        for value in NOT_CONTEXT:
            with self.subTest(context=repr(value)[:20]):
                await self.refused(
                    lambda v=value: self.passkeys.authenticate_begin(AUTH, v)
                )

    async def test_register_finish(self):
        good = registration_answer()
        for index, values in {0: NOT_AUTH, 3: NOT_CONTEXT}.items():
            for value in values:
                args = [AUTH, good, None, CONTEXT]
                args[index] = value
                with self.subTest(argument=index, value=repr(value)[:20]):
                    await self.refused(lambda a=args: self.passkeys.register_finish(*a))
        for value in (None, "credential", 5, [good], type("D", (dict,), {})(good)):
            with self.subTest(credential=repr(value)[:20]):
                await self.refused(
                    lambda v=value: self.passkeys.register_finish(
                        AUTH, v, None, CONTEXT
                    )
                )
        for value in (5, b"x", ["a"], "x" * 65, "a\x00b", "line\nbreak"):
            with self.subTest(name=repr(value)[:20]):
                await self.refused(
                    lambda v=value: self.passkeys.register_finish(
                        AUTH, good, v, CONTEXT
                    )
                )

    async def test_revoke(self):
        for index, values in {0: NOT_AUTH, 1: NOT_A_UUID, 2: NOT_CONTEXT}.items():
            for value in values:
                args = [AUTH, uuid.uuid4(), CONTEXT]
                args[index] = value
                with self.subTest(argument=index, value=repr(value)[:20]):
                    await self.refused(lambda a=args: self.passkeys.revoke(*a))

    async def test_without_configuration_the_ceremonies_are_unavailable(self):
        plain = build_auth(
            self.settings.model_copy(
                update={"passkey_rp_id": None, "passkey_origins": []}
            ),
            self.database,
            clock=lambda: T0,
        )
        self.addCleanup(plain.close)
        self.assertFalse(plain.passkeys.available)
        for call in (
            lambda: plain.passkeys.register_begin(AUTH, CONTEXT),
            lambda: plain.passkeys.authenticate_begin(AUTH, CONTEXT),
            lambda: plain.passkeys.register_finish(AUTH, {}, None, CONTEXT),
        ):
            await self.refused(call, PasskeyUnavailableError)

    async def test_the_constructors(self):
        registry, sessions = self.registry, self.services.sessions
        good = (
            self.database,
            registry,
            sessions,
            self.services.throttle,
            self.services.service._audit,
            self.services.policy,
        )
        PasskeyService(*good)
        for index in range(len(good)):
            args = list(good)
            args[index] = object()
            with self.subTest(argument=index):
                with self.assertRaises(TypeError):
                    PasskeyService(*args)
        for timeout in (0, -1, 61, True, "3", None):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):
                    PasskeyService(*good, timeout_seconds=timeout)


class EvidenceTest(ValidationCase):
    async def test_the_step_up_evidence(self):
        assertion = ceremony_assertion()
        StepUpEvidence(AuthMethod.PASSKEY, assertion=assertion)
        for value in ("assertion", {"id": "x"}, 5, b"x", object()):
            with self.subTest(assertion=repr(value)[:20]):
                with self.assertRaises(InvalidAuthInputError):
                    StepUpEvidence(AuthMethod.PASSKEY, assertion=value)
        for value in ("session", 5, b"x", uuid.uuid4().hex):
            with self.subTest(session_id=repr(value)[:20]):
                with self.assertRaises(InvalidAuthInputError):
                    StepUpEvidence(AuthMethod.PASSKEY, session_id=value)
        evidence = StepUpEvidence(AuthMethod.PASSKEY, assertion=assertion)
        # The payloads are not in its repr (a log line could not leak them).
        self.assertEqual(
            repr(evidence),
            "StepUpEvidence(method=<AuthMethod.PASSKEY: 'passkey'>, session_id=None)",
        )

    async def test_a_passkey_step_up_is_refused_for_a_bad_evidence(self):
        for value in (None, "evidence", 5, {"method": "passkey"}):
            with self.subTest(value=repr(value)[:20]):
                await self.refused(lambda v=value: self.auth.step_up(AUTH, v, CONTEXT))

    async def test_the_verifier_without_an_assertion_or_a_session_says_no(self):
        verifier = self.auth._verifiers[AuthMethod.PASSKEY]
        self.assertIsInstance(verifier, PasskeyStepUpVerifier)
        for evidence in (
            StepUpEvidence(AuthMethod.PASSKEY),
            StepUpEvidence(AuthMethod.PASSKEY, assertion=ceremony_assertion()),
            StepUpEvidence(AuthMethod.PASSKEY, session_id=uuid.uuid4()),
        ):
            with self.subTest(evidence=repr(evidence)[:40]):
                self.assertIs(
                    await verifier.verify(uuid.uuid4(), "alice", evidence), False
                )

    async def test_a_refusal_carries_only_an_enum(self):
        refused = StepUpRefused(AuthReason.CHALLENGE_INVALID)
        self.assertEqual(
            (str(refused), refused.reason),
            ("challenge_invalid", AuthReason.CHALLENGE_INVALID),
        )

    async def test_the_verifier_needs_a_configured_registry(self):
        plain = build_auth(
            self.settings.model_copy(
                update={"passkey_rp_id": None, "passkey_origins": []}
            ),
            self.database,
            clock=lambda: T0,
        )
        self.addCleanup(plain.close)
        for args in (
            (self.database, plain.passkeys._registry),
            (self.database, object()),
            (object(), self.registry),
        ):
            with self.subTest(args=repr(args)[:30]):
                with self.assertRaises(TypeError):
                    PasskeyStepUpVerifier(*args, timeout_seconds=3)


def ceremony_assertion():
    """A syntactically valid assertion (only its shape matters here)."""
    from paw_backend.auth.passkeys.types import parse_assertion_credential

    from .test_passkey_types import assertion_answer

    return parse_assertion_credential(assertion_answer())


class RegistryTest(ValidationCase):
    async def test_every_method_checks_its_arguments_first(self):
        registry = self.registry
        user, other = uuid.uuid4(), uuid.uuid4()
        registration = ceremony.VerifiedRegistration(
            b"c" * 16, b"k" * 16, 0, None, False, False
        )
        assertion = ceremony.VerifiedAssertion(1, False, False)
        methods = {
            "count_active_in": (
                lambda s, u: registry.count_active_in(s, u),
                [self.session, user],
            ),
            "confirm_in": (
                lambda s, u, c: registry.confirm_in(s, u, c),
                [self.session, user, b"c" * 16],
            ),
            "revoke_all_in": (
                lambda s, u: registry.revoke_all_in(s, u),
                [self.session, user],
            ),
            "list_active_in": (
                lambda s, u: registry.list_active_in(s, u),
                [self.session, user],
            ),
            "active_credential_ids_in": (
                lambda s, u: registry.active_credential_ids_in(s, u),
                [self.session, user],
            ),
            "stored_in": (
                lambda s, u, c: registry.stored_in(s, u, c),
                [self.session, user, b"c" * 16],
            ),
            "delete_challenges_of_in": (
                lambda s, u: registry.delete_challenges_of_in(s, u),
                [self.session, user],
            ),
            "issue_challenge_in": (
                lambda s, u, i, p, c: registry.issue_challenge_in(
                    s, user_id=u, session_id=i, purpose=p, challenge=c
                ),
                [self.session, user, other, PasskeyPurpose.REGISTER, b"x" * 32],
            ),
            "consume_challenge_in": (
                lambda s, u, i, p: registry.consume_challenge_in(
                    s, user_id=u, session_id=i, purpose=p
                ),
                [self.session, user, other, PasskeyPurpose.REGISTER],
            ),
            "insert_in": (
                lambda s, u, n, r: registry.insert_in(
                    s, user_id=u, name=n, registration=r
                ),
                [self.session, user, "phone", registration],
            ),
            "record_use_in": (
                lambda s, p, u, a: registry.record_use_in(
                    s, passkey_id=p, user_id=u, assertion=a
                ),
                [self.session, other, user, assertion],
            ),
            "revoke_in": (
                lambda s, p, u, r: registry.revoke_in(
                    s, passkey_id=p, user_id=u, reason=r
                ),
                [self.session, other, user, PasskeyRevokeReason.REVOKED_BY_USER],
            ),
        }
        bad_for = {
            "session": NOT_A_SESSION,
            "uuid": NOT_A_UUID,
            "credential": [*NOT_BYTES, b"", b"x" * 1024],
            "purpose": [None, "register", 5, PasskeyRevokeReason.RECOVERY],
            "challenge": [*NOT_BYTES, b"x" * 31, b"x" * 33],
            "name": [None, 5, b"x", "", "x" * 65],
            "registration": [None, "r", {"credential_id": b"c"}, assertion],
            "assertion": [None, "a", 5, registration],
            "reason": [None, "revoked_by_user", 5, PasskeyPurpose.REGISTER],
        }
        kinds = {
            "count_active_in": ("session", "uuid"),
            "confirm_in": ("session", "uuid", "credential"),
            "revoke_all_in": ("session", "uuid"),
            "list_active_in": ("session", "uuid"),
            "active_credential_ids_in": ("session", "uuid"),
            "stored_in": ("session", "uuid", "credential"),
            "delete_challenges_of_in": ("session", "uuid"),
            "issue_challenge_in": ("session", "uuid", "uuid", "purpose", "challenge"),
            "consume_challenge_in": ("session", "uuid", "uuid", "purpose"),
            "insert_in": ("session", "uuid", "name", "registration"),
            "record_use_in": ("session", "uuid", "uuid", "assertion"),
            "revoke_in": ("session", "uuid", "uuid", "reason"),
        }
        checked = 0
        for name, (call, good) in methods.items():
            for index, kind in enumerate(kinds[name]):
                for value in bad_for[kind]:
                    args = list(good)
                    args[index] = value
                    with self.subTest(
                        method=name, argument=index, value=repr(value)[:20]
                    ):
                        await self.refused(lambda c=call, a=args: c(*a))
                        checked += 1
        self.assertGreater(checked, 200)  # the table really was walked

    async def test_is_enrolled_checks_the_id(self):
        for value in NOT_A_UUID:
            with self.subTest(value=repr(value)[:20]):
                await self.refused(lambda v=value: self.registry.is_enrolled(v))

    async def test_the_constructor(self):
        config = self.registry.config
        PasskeyRegistry(self.database, config)
        PasskeyRegistry(self.database, None)
        for args, error in (
            ((object(), config), TypeError),
            ((self.database, "config"), TypeError),
            ((self.database, config, "clock"), TypeError),
        ):
            with self.subTest(args=repr(args)[:30]):
                with self.assertRaises(error):
                    PasskeyRegistry(
                        args[0],
                        args[1],
                        **({"clock": args[2]} if len(args) > 2 else {}),
                    )
        for timeout in (0, -1, 61, True, "3"):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):
                    PasskeyRegistry(self.database, config, timeout_seconds=timeout)

    async def test_the_clock_must_be_aware(self):
        from datetime import datetime

        registry = PasskeyRegistry(
            self.database, self.registry.config, clock=lambda: datetime(2030, 1, 1)
        )
        with self.assertRaises(ValueError):
            registry.now()


class SessionAndStepUpTest(ValidationCase):
    def store(self) -> SessionStore:
        return self.services.sessions

    async def test_the_session_store_methods_of_the_gate(self):
        store = self.store()
        good = {
            "record_step_up": (
                [self.session, uuid.uuid4(), b"h" * 32, AuthMethod.PASSKEY],
                {"passkey_id": uuid.uuid4()},
            ),
            "open_gate": (
                [self.session, uuid.uuid4(), b"h" * 32, uuid.uuid4()],
                {},
            ),
            "revoke_bound_to_passkey": ([self.session, uuid.uuid4()], {}),
            "clear_passkey_step_ups": ([self.session, uuid.uuid4()], {}),
        }
        bad = {
            "record_step_up": {
                0: NOT_A_SESSION,
                1: NOT_A_UUID,
                2: [None, "h", b"h" * 31, b"h" * 33, 5],
                3: [None, "x", 5, NotAMethod()],
            },
            "open_gate": {
                0: NOT_A_SESSION,
                1: NOT_A_UUID,
                2: [None, "h", b"h" * 31, 5],
                3: NOT_A_UUID,
            },
            "revoke_bound_to_passkey": {0: NOT_A_SESSION, 1: NOT_A_UUID},
            "clear_passkey_step_ups": {0: NOT_A_SESSION, 1: NOT_A_UUID},
        }
        for method, (args, options) in good.items():
            for index, values in bad[method].items():
                for value in values:
                    call_args = list(args)
                    call_args[index] = value
                    with self.subTest(
                        method=method, argument=index, value=repr(value)[:20]
                    ):
                        await self.refused(
                            lambda m=method, a=call_args, o=options: getattr(store, m)(
                                *a, **o
                            )
                        )
        # A Passkey id is only meaningful for a Passkey step-up.
        await self.refused(
            lambda: store.record_step_up(
                self.session,
                uuid.uuid4(),
                b"h" * 32,
                AuthMethod.PASSWORD,
                passkey_id=uuid.uuid4(),
            )
        )
        for value in NOT_A_UUID[1:]:
            with self.subTest(passkey_id=repr(value)[:20]):
                await self.refused(
                    lambda v=value: store.record_step_up(
                        self.session,
                        uuid.uuid4(),
                        b"h" * 32,
                        AuthMethod.PASSKEY,
                        passkey_id=v,
                    )
                )

    async def test_a_session_is_created_with_a_valid_gate_only(self):
        for value in (None, "closed", 5, b"open", PasskeyPurpose.REGISTER):
            with self.subTest(gate=repr(value)[:20]):
                await self.refused(
                    lambda v=value: self.store().create(
                        self.session, uuid.uuid4(), remember_me=False, passkey_gate=v
                    )
                )

    async def test_the_freshness_reader(self):
        good = dict(
            session_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            window_minutes=30,
            now=T0,
        )
        for name, values in {
            "session_id": NOT_A_UUID,
            "user_id": NOT_A_UUID,
            "window_minutes": [None, "30", True, 0, -1, 1441, 30.0],
        }.items():
            for value in values:
                with self.subTest(argument=name, value=repr(value)[:20]):
                    await self.refused(
                        lambda n=name, v=value: read_freshness_in(
                            self.session, **{**good, n: v}
                        )
                    )
        for value in NOT_A_SESSION:
            with self.subTest(session=repr(value)[:20]):
                await self.refused(lambda v=value: read_freshness_in(v, **good))

    async def test_the_approval_adapter_constructor(self):
        PasskeyApprovalStepUp(self.database)
        with self.assertRaises(TypeError):
            PasskeyApprovalStepUp(object())
        with self.assertRaises(TypeError):
            PasskeyApprovalStepUp(self.database, clock="now")
        for timeout in (0, -1, 61, True, "3"):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):
                    PasskeyApprovalStepUp(self.database, timeout_seconds=timeout)

    async def test_the_guard_takes_a_boolean_flag(self):
        require_capability(Capability.ACCOUNT_READ, allow_restricted=True)
        require_capability(Capability.ACCOUNT_READ, allow_restricted=False)
        for value in (None, 0, 1, "true", [], 2):
            with self.subTest(value=repr(value)):
                with self.assertRaises(TypeError):
                    require_capability(Capability.ACCOUNT_READ, allow_restricted=value)


class NotAMethod:
    """Not a method: only its type matters."""
