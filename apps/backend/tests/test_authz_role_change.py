import unittest

from paw_backend.authz import (
    Authorizer,
    Capability,
    InMemoryAuditSink,
    Reason,
    SystemRole,
)
from paw_backend.authz.policy import decide_role_change

from .authz_support import U1, U2, principal

OWNER, ADMIN, USER, SYSTEM = (
    SystemRole.OWNER,
    SystemRole.ADMIN,
    SystemRole.USER,
    SystemRole.SYSTEM,
)

# (role of the target, new role or None = remove) -> actor roles that may do it.
# Spelled out so that any change in who may manage whom fails a test.
EXPECTED = {
    # ordinary users: Admin and Owner
    (USER, None): {ADMIN, OWNER},
    (USER, USER): {ADMIN, OWNER},
    # adding, removing, demoting or promoting an Admin: Owner only
    (USER, ADMIN): {OWNER},
    (ADMIN, USER): {OWNER},
    (ADMIN, ADMIN): {OWNER},
    (ADMIN, None): {OWNER},
    # anything that involves the Owner is an ownership transfer: Owner only
    (USER, OWNER): {OWNER},
    (ADMIN, OWNER): {OWNER},
    (OWNER, ADMIN): {OWNER},
    (OWNER, USER): {OWNER},
    (OWNER, None): {OWNER},
    (OWNER, OWNER): {OWNER},
    # the internal identity is never assigned, changed or removed here
    (SYSTEM, None): set(),
    (SYSTEM, USER): set(),
    (USER, SYSTEM): set(),
    (ADMIN, SYSTEM): set(),
    (OWNER, SYSTEM): set(),
}


class RoleChangeMatrixTest(unittest.TestCase):
    def test_who_may_change_whom(self):
        for (target, new), allowed_for in EXPECTED.items():
            for actor_role in SystemRole:
                with self.subTest(
                    target=target.value,
                    new=new.value if new else None,
                    actor=actor_role.value,
                ):
                    decision = decide_role_change(principal(actor_role), target, new)
                    self.assertEqual(decision.allowed, actor_role in allowed_for)

    def test_an_admin_cannot_manage_the_owner_or_other_admins(self):
        admin = principal(ADMIN)
        for target, new in ((OWNER, USER), (ADMIN, USER), (ADMIN, None), (USER, ADMIN)):
            with self.subTest(target=target.value, new=new.value if new else None):
                decision = decide_role_change(admin, target, new)
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, Reason.CAPABILITY_NOT_GRANTED)

    def test_the_capability_needed_depends_on_the_roles_involved(self):
        owner = principal(OWNER)
        self.assertIs(
            decide_role_change(owner, USER, ADMIN).capability,
            Capability.OWNER_ADMINS_MANAGE,
        )
        self.assertIs(
            decide_role_change(owner, ADMIN, OWNER).capability,
            Capability.OWNER_OWNERSHIP_TRANSFER,
        )
        self.assertIs(
            decide_role_change(owner, USER, None).capability,
            Capability.ADMIN_USERS_MANAGE,
        )

    def test_nobody_changes_their_own_role(self):
        for actor_role in (ADMIN, OWNER):
            with self.subTest(actor=actor_role.value):
                decision = decide_role_change(
                    principal(actor_role, user_id=U1), USER, None, target_user_id=U1
                )
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, Reason.SELF_ROLE_CHANGE)
        other = decide_role_change(
            principal(ADMIN, user_id=U1), USER, None, target_user_id=U2
        )
        self.assertTrue(other.allowed)

    def test_a_malformed_target_id_is_denied(self):
        decision = decide_role_change(principal(OWNER), USER, None, target_user_id="x")
        self.assertEqual(decision.reason, Reason.ROLE_CHANGE_NOT_ALLOWED)

    def test_no_actor_and_wrong_types_are_denied(self):
        self.assertEqual(
            decide_role_change(None, USER, None).reason, Reason.UNAUTHENTICATED
        )
        for target, new in (("user", None), (USER, "admin"), (None, None)):
            with self.subTest(target=target, new=new):
                decision = decide_role_change(principal(OWNER), target, new)
                self.assertEqual(decision.reason, Reason.ROLE_CHANGE_NOT_ALLOWED)


class RoleChangeAuditTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_decision_is_audited_under_the_capability_it_needed(self):
        sink = InMemoryAuditSink()
        authorizer = Authorizer(sink)
        allowed = await authorizer.authorize_role_change(
            principal(OWNER, user_id=U1), USER, ADMIN, target_user_id=U2
        )
        denied = await authorizer.authorize_role_change(
            principal(ADMIN, user_id=U2), ADMIN, None, target_user_id=U1
        )
        self.assertTrue(allowed)
        self.assertFalse(denied)
        first, second = sink.events
        self.assertEqual(
            (first.actor_id, first.action, first.decision),
            (U1, "owner.admins.manage", "allow"),
        )
        self.assertEqual(
            (second.actor_id, second.action, second.decision, second.reason),
            (U2, "owner.admins.manage", "deny", "capability_not_granted"),
        )


if __name__ == "__main__":
    unittest.main()
