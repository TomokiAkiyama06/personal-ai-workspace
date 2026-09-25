import inspect
import unittest

from paw_backend.authz import (
    Authorizer,
    Capability,
    InMemoryAuditSink,
    Reason,
    SystemRole,
)
from paw_backend.authz.policy import decide_ownership_transfer, decide_role_change

from .authz_support import U1, U2, U3, FailingSink, principal

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
    # the Owner role never moves through a role change (see the transfer below)
    (USER, OWNER): set(),
    (ADMIN, OWNER): set(),
    (OWNER, ADMIN): set(),
    (OWNER, USER): set(),
    (OWNER, None): set(),
    (OWNER, OWNER): set(),
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
                    decision = decide_role_change(
                        principal(actor_role, user_id=U1), U2, target, new
                    )
                    self.assertEqual(decision.allowed, actor_role in allowed_for)

    def test_the_target_user_is_a_required_argument(self):
        # Omitting it used to switch the self-change check off.
        parameter = inspect.signature(decide_role_change).parameters["target_user_id"]
        self.assertIs(parameter.default, inspect.Parameter.empty)
        with self.assertRaises(TypeError):
            decide_role_change(principal(OWNER), USER, None)

    def test_an_admin_cannot_manage_the_owner_or_other_admins(self):
        admin = principal(ADMIN, user_id=U1)
        for target, new in ((OWNER, USER), (ADMIN, USER), (ADMIN, None), (USER, ADMIN)):
            with self.subTest(target=target.value, new=new.value if new else None):
                decision = decide_role_change(admin, U2, target, new)
                self.assertFalse(decision.allowed)

    def test_the_owner_role_never_moves_through_a_role_change(self):
        owner = principal(OWNER, user_id=U1)
        for target, new in (
            (USER, OWNER),
            (ADMIN, OWNER),
            (OWNER, ADMIN),
            (OWNER, None),
        ):
            with self.subTest(target=target.value, new=new.value if new else None):
                decision = decide_role_change(owner, U2, target, new)
                self.assertEqual(decision.reason, Reason.ROLE_CHANGE_NOT_ALLOWED)
                self.assertIs(decision.capability, Capability.OWNER_OWNERSHIP_TRANSFER)

    def test_the_capability_needed_depends_on_the_roles_involved(self):
        owner = principal(OWNER, user_id=U1)
        self.assertIs(
            decide_role_change(owner, U2, USER, ADMIN).capability,
            Capability.OWNER_ADMINS_MANAGE,
        )
        self.assertIs(
            decide_role_change(owner, U2, USER, None).capability,
            Capability.ADMIN_USERS_MANAGE,
        )

    def test_nobody_changes_their_own_role(self):
        for actor_role in (ADMIN, OWNER):
            for target, new in ((USER, None), (ADMIN, USER), (OWNER, ADMIN)):
                with self.subTest(actor=actor_role.value, target=target.value):
                    decision = decide_role_change(
                        principal(actor_role, user_id=U1), U1, target, new
                    )
                    self.assertFalse(decision.allowed)
                    self.assertEqual(decision.reason, Reason.SELF_ROLE_CHANGE)
        other = decide_role_change(principal(ADMIN, user_id=U1), U2, USER, None)
        self.assertTrue(other.allowed)

    def test_a_target_given_as_a_canonical_string_is_still_the_same_user(self):
        decision = decide_role_change(principal(OWNER, user_id=U1), str(U1), USER, None)
        self.assertEqual(decision.reason, Reason.SELF_ROLE_CHANGE)

    def test_a_malformed_target_id_is_denied(self):
        for bad in ("x", None, 5, ""):
            with self.subTest(target=bad):
                decision = decide_role_change(principal(OWNER), bad, USER, None)
                self.assertEqual(decision.reason, Reason.ROLE_CHANGE_NOT_ALLOWED)

    def test_no_actor_and_wrong_types_are_denied(self):
        self.assertEqual(
            decide_role_change(None, U2, USER, None).reason, Reason.UNAUTHENTICATED
        )
        for target, new in (("user", None), (USER, "admin"), (None, None)):
            with self.subTest(target=target, new=new):
                decision = decide_role_change(principal(OWNER), U2, target, new)
                self.assertEqual(decision.reason, Reason.ROLE_CHANGE_NOT_ALLOWED)


class OwnershipTransferTest(unittest.TestCase):
    def test_only_the_owner_may_hand_over_ownership(self):
        for actor_role in SystemRole:
            with self.subTest(actor=actor_role.value):
                decision = decide_ownership_transfer(
                    principal(actor_role, user_id=U1), U2, ADMIN
                )
                self.assertEqual(decision.allowed, actor_role is OWNER)
                self.assertIs(decision.capability, Capability.OWNER_OWNERSHIP_TRANSFER)
                if actor_role is not OWNER:
                    self.assertEqual(decision.reason, Reason.CAPABILITY_NOT_GRANTED)

    def test_the_new_owner_may_be_an_admin_or_a_user(self):
        for new_owner_role in (ADMIN, USER):
            decision = decide_ownership_transfer(
                principal(OWNER, user_id=U1), U2, new_owner_role
            )
            self.assertTrue(decision.allowed)
            self.assertEqual(decision.reason, Reason.GRANTED_BY_SYSTEM_ROLE)

    def test_ownership_cannot_go_to_oneself(self):
        # Without a transfer step this used to be impossible: Owner -> Admin on
        # oneself hit the self-change rule, so ownership could never move.
        decision = decide_ownership_transfer(principal(OWNER, user_id=U1), U1, ADMIN)
        self.assertEqual(decision.reason, Reason.SELF_ROLE_CHANGE)
        same = decide_ownership_transfer(principal(OWNER, user_id=U1), str(U1), ADMIN)
        self.assertEqual(same.reason, Reason.SELF_ROLE_CHANGE)

    def test_there_is_never_a_second_owner_or_an_internal_owner(self):
        for new_owner_role in (OWNER, SYSTEM, "admin", None):
            with self.subTest(role=new_owner_role):
                decision = decide_ownership_transfer(
                    principal(OWNER, user_id=U1), U2, new_owner_role
                )
                self.assertEqual(decision.reason, Reason.ROLE_CHANGE_NOT_ALLOWED)

    def test_a_malformed_new_owner_and_no_actor_are_denied(self):
        for bad in ("x", None, 5):
            with self.subTest(new_owner=bad):
                decision = decide_ownership_transfer(principal(OWNER), bad, ADMIN)
                self.assertEqual(decision.reason, Reason.ROLE_CHANGE_NOT_ALLOWED)
        decision = decide_ownership_transfer(None, U2, ADMIN)
        self.assertFalse(decision.allowed)


class RoleChangeAuditTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_event_is_about_the_target_user_and_carries_both_roles(self):
        sink = InMemoryAuditSink()
        authorizer = Authorizer(sink)
        allowed = await authorizer.authorize_role_change(
            principal(OWNER, user_id=U1), U2, USER, ADMIN
        )
        denied = await authorizer.authorize_role_change(
            principal(ADMIN, user_id=U3), U2, ADMIN, None
        )
        removed = await authorizer.authorize_role_change(
            principal(ADMIN, user_id=U3), U2, USER, None
        )
        self.assertEqual(
            (bool(allowed), bool(denied), bool(removed)), (True, False, True)
        )
        first, second, third = sink.events
        self.assertEqual(
            (first.actor_id, first.action, first.decision),
            (U1, "owner.admins.manage", "allow"),
        )
        self.assertEqual(
            (first.resource_kind, first.resource_id, first.old_role, first.new_role),
            ("user", U2, "user", "admin"),
        )
        self.assertEqual(
            (second.actor_id, second.action, second.decision, second.reason),
            (U3, "owner.admins.manage", "deny", "capability_not_granted"),
        )
        self.assertEqual(
            (second.resource_id, second.old_role, second.new_role), (U2, "admin", None)
        )
        self.assertEqual(
            (third.action, third.old_role, third.new_role),
            ("admin.users.manage", "user", None),
        )

    async def test_a_self_change_is_denied_and_audited_against_the_user(self):
        sink = InMemoryAuditSink()
        decision = await Authorizer(sink).authorize_role_change(
            principal(OWNER, user_id=U1), U1, OWNER, ADMIN
        )
        self.assertFalse(decision)
        (event,) = sink.events
        self.assertEqual(
            (event.reason, event.resource_kind, event.resource_id),
            ("self_role_change", "user", U1),
        )

    async def test_a_malformed_target_is_audited_without_a_resource_id(self):
        sink = InMemoryAuditSink()
        await Authorizer(sink).authorize_role_change(
            principal(OWNER, user_id=U1), "not-a-uuid", USER, None
        )
        (event,) = sink.events
        self.assertEqual(
            (event.resource_kind, event.resource_id, event.reason),
            ("unknown", None, "role_change_not_allowed"),
        )

    async def test_an_ownership_transfer_is_one_audited_decision(self):
        sink = InMemoryAuditSink()
        decision = await Authorizer(sink).authorize_ownership_transfer(
            principal(OWNER, user_id=U1), U2, ADMIN
        )
        self.assertTrue(decision)
        (event,) = sink.events  # one decision, one event: never a two-step gap
        self.assertEqual(
            (event.actor_id, event.actor_role, event.action, event.decision),
            (U1, "owner", "owner.ownership.transfer", "allow"),
        )
        self.assertEqual(
            (event.resource_kind, event.resource_id, event.old_role, event.new_role),
            ("user", U2, "admin", "owner"),
        )

    async def test_a_refused_transfer_is_audited_too(self):
        sink = InMemoryAuditSink()
        decision = await Authorizer(sink).authorize_ownership_transfer(
            principal(ADMIN, user_id=U1), U2, USER
        )
        self.assertFalse(decision)
        (event,) = sink.events
        self.assertEqual(
            (event.actor_id, event.decision, event.reason),
            (U1, "deny", "capability_not_granted"),
        )

    async def test_no_audit_means_no_transfer(self):
        with self.assertLogs("paw_backend.authz.authorizer", level="ERROR"):
            decision = await Authorizer(FailingSink()).authorize_ownership_transfer(
                principal(OWNER, user_id=U1), U2, ADMIN
            )
        self.assertEqual(decision.reason, Reason.AUDIT_UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
