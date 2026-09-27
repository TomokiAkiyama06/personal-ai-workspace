"""PAW-024 without a database: tokens, the lifecycle table, the role rules, settings.

The PostgreSQL tests are ``test_onboarding_*`` (service, HTTP, migration, grants).
"""

import unittest
import uuid

from pydantic import ValidationError

from paw_backend.auth.models import (
    STEP_UP_METHODS,
    AuthMethod,
    ThrottleScope,
)
from paw_backend.auth.onboarding import onetime
from paw_backend.auth.onboarding.common import may_administer
from paw_backend.auth.onboarding.lifecycle import TRANSITIONS, transition_allowed
from paw_backend.auth.onboarding.pairing import IssuedPairing
from paw_backend.auth.throttle import OnSuccess, policies_from_settings
from paw_backend.authz.roles import SystemRole
from paw_backend.authz.subjects import Principal
from paw_backend.identity import UserStatus

from .support import make_settings


def principal(role: SystemRole) -> Principal:
    return Principal(user_id=uuid.uuid4(), system_role=role)


class OneTimeTokenTest(unittest.TestCase):
    def test_a_token_parses_back_to_its_id_and_verifies(self):
        for kind in (onetime.INVITATION, onetime.PAIRING, onetime.CLAIM):
            with self.subTest(kind.prefix):
                new = kind.generate()
                parsed = kind.parse(new.token)
                self.assertEqual(parsed.token_id, new.token_id)
                self.assertTrue(onetime.verify(parsed, new.salt, new.secret_hash))
                self.assertTrue(new.token.startswith(kind.prefix + "."))
                self.assertEqual(len(new.token), kind.length)

    def test_one_kind_is_never_accepted_as_another(self):
        invitation = onetime.INVITATION.generate().token
        pairing = onetime.PAIRING.generate().token
        self.assertIsNone(onetime.PAIRING.parse(invitation))
        self.assertIsNone(onetime.CLAIM.parse(pairing))
        self.assertIsNone(onetime.INVITATION.parse(pairing))

    def test_a_wrong_secret_or_malformed_token_does_not_verify(self):
        new = onetime.PAIRING.generate()
        other = onetime.PAIRING.generate(token_id=new.token_id)
        self.assertEqual(other.token_id, new.token_id)
        self.assertFalse(
            onetime.verify(
                onetime.PAIRING.parse(other.token), new.salt, new.secret_hash
            )
        )
        for value in (None, 1, "", new.token + "x", new.token.upper(), "pawpr1.a.b"):
            with self.subTest(value=value):
                self.assertIsNone(onetime.PAIRING.parse(value))
        self.assertFalse(onetime.verify(None, None, None))
        self.assertFalse(onetime.verify(onetime.PAIRING.parse(new.token), None, None))

    def test_the_token_is_not_in_the_repr(self):
        new = onetime.INVITATION.generate()
        self.assertNotIn(new.token, repr(new))
        self.assertNotIn(new.token.rsplit(".", 1)[1], repr(new))
        issued = IssuedPairing(uuid.uuid4(), new.token, None, False)
        self.assertNotIn(new.token, repr(issued))
        self.assertEqual(issued.link_path, "/pair#" + new.token)

    def test_a_bad_prefix_is_refused(self):
        with self.assertRaises(ValueError):
            onetime.TokenKind("nope")


class LifecycleTableTest(unittest.TestCase):
    def test_exactly_the_four_edges_of_decision_0033(self):
        self.assertEqual(
            TRANSITIONS,
            {
                (UserStatus.INVITED, UserStatus.ACTIVE),
                (UserStatus.INVITED, UserStatus.DELETED),
                (UserStatus.ACTIVE, UserStatus.PENDING_DELETION),
                (UserStatus.PENDING_DELETION, UserStatus.ACTIVE),
            },
        )

    def test_nothing_else_is_allowed(self):
        for old in UserStatus:
            for new in UserStatus:
                with self.subTest(old=old, new=new):
                    self.assertEqual(
                        transition_allowed(old, new), (old, new) in TRANSITIONS
                    )
        # In particular: deleted is final and pending deletion never jumps to it
        # (the erasure is a later issue).
        self.assertFalse(
            transition_allowed(UserStatus.PENDING_DELETION, UserStatus.DELETED)
        )
        self.assertFalse(transition_allowed(UserStatus.DELETED, UserStatus.ACTIVE))
        self.assertFalse(transition_allowed("invited", "nonsense"))


class AdministratorRuleTest(unittest.TestCase):
    def test_an_admin_manages_users_and_the_owner_also_admins(self):
        owner, admin, user = (
            principal(SystemRole.OWNER),
            principal(SystemRole.ADMIN),
            principal(SystemRole.USER),
        )
        self.assertTrue(may_administer(admin, SystemRole.USER))
        self.assertFalse(may_administer(admin, SystemRole.ADMIN))
        self.assertTrue(may_administer(owner, SystemRole.ADMIN))
        self.assertTrue(may_administer(owner, "user"))
        for actor in (owner, admin, user):
            with self.subTest(actor=actor.system_role):
                self.assertFalse(may_administer(actor, SystemRole.OWNER))
                self.assertFalse(may_administer(actor, SystemRole.SYSTEM))
                self.assertFalse(may_administer(actor, "nonsense"))
        self.assertFalse(may_administer(user, SystemRole.USER))


class SettingsAndEnumsTest(unittest.TestCase):
    def test_the_defaults_are_those_of_decision_0033(self):
        settings = make_settings()
        self.assertEqual(settings.invitation_ttl_seconds, 72 * 3600)
        # REQUIREMENTS.md: the pairing token lives 10 minutes initially.
        self.assertEqual(settings.pairing_token_ttl_seconds, 600)

    def test_the_bounds_are_enforced(self):
        for name, value in (
            ("invitation_ttl_seconds", 599),
            ("invitation_ttl_seconds", 1_209_601),
            ("pairing_token_ttl_seconds", 59),
            ("pairing_token_ttl_seconds", 3_601),
        ):
            with self.subTest(name=name, value=value):
                with self.assertRaises(ValidationError):
                    make_settings(**{name: value})

    def test_pairing_is_a_sign_in_method_but_never_a_step_up(self):
        self.assertIn(AuthMethod.PAIRING, set(AuthMethod))
        self.assertNotIn(AuthMethod.PAIRING, STEP_UP_METHODS)

    def test_the_pairing_throttles_give_a_correct_attempt_back(self):
        policies = policies_from_settings(make_settings())
        for scope in (ThrottleScope.PAIRING_SOURCE, ThrottleScope.PAIRING_GLOBAL):
            with self.subTest(scope):
                self.assertIs(policies[scope].on_success, OnSuccess.REFUND)
        self.assertEqual(
            policies[ThrottleScope.PAIRING_SOURCE].free_attempts,
            policies[ThrottleScope.REDEEM_SOURCE].free_attempts,
        )


if __name__ == "__main__":
    unittest.main()
