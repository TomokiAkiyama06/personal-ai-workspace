"""Initial Owner setup, recovery and token redemption on a real PostgreSQL.

Skipped unless ``PAW_TEST_DATABASE_URL`` is set. Each class migrates the test
database to ``head`` and back to ``base``; each test starts without users and
looks only at the audit rows it caused.
"""

import hmac
import inspect
import logging
import os
import unittest
import uuid
from collections import Counter
from datetime import datetime, timedelta
from unittest.mock import patch

from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError

from paw_backend.authz import SystemRole
from paw_backend.identity import (
    AuditUnavailableError,
    InvalidLoginNameError,
    LoginNameTakenError,
    OwnerAlreadyExistsError,
    OwnerNotFoundError,
    OwnerNotLiveError,
    RecoveryNotPrivilegedError,
    RedeemHookError,
    Redemption,
    SetupTokenRejectedError,
    TokenPurpose,
    TokenRedeemer,
    UserStatus,
    limits,
    tokens,
)
from paw_backend.identity.audit import IdentityAudit
from paw_backend.identity.operator import (
    IssuedToken,
    OperatorIdentity,
    OwnerOperator,
)

from .identity_support import (
    SECRET_DETAIL,
    T0,
    FailingSink,
    FlakySink,
    LogCapture,
    PostgresIdentityTestCase,
    lookup_id_of,
    requires_postgres,
    running_as,
    secret_of,
    wrong_secret_for,
)

TTL = 1800


@requires_postgres
class SetupOwnerTest(PostgresIdentityTestCase):
    async def test_creates_the_owner_and_a_one_time_setup_token(self):
        issued = await self.operator.setup_owner("  Tomoki ")

        self.assertIsInstance(issued, IssuedToken)
        self.assertEqual(issued.login_name, "tomoki")
        self.assertEqual(issued.purpose, TokenPurpose.SETUP)
        self.assertEqual(issued.expires_at, T0 + timedelta(seconds=TTL))
        self.assertRegex(issued.token, r"^pawst1\.[0-9a-f]{32}\.[A-Za-z0-9_-]{43}$")
        (user,) = await self.query("SELECT * FROM users")
        self.assertEqual(
            (
                user.id,
                user.login_name,
                user.system_role,
                user.status,
                user.passkey_required,
                user.created_at,
                user.updated_at,
            ),
            (issued.user_id, "tomoki", "owner", "invited", True, T0, T0),
        )
        (row,) = await self.query("SELECT * FROM setup_tokens")
        self.assertEqual(
            (
                row.audit_ref,
                row.user_id,
                row.purpose,
                row.created_at,
                row.expires_at,
                row.used_at,
                row.revoked_at,
                row.locked_at,
                row.attempts,
            ),
            (
                issued.audit_ref,
                issued.user_id,
                "setup",
                T0,
                T0 + timedelta(seconds=TTL),
                None,
                None,
                None,
                0,
            ),
        )
        # The lookup id (in the token) and the audit reference are unrelated.
        self.assertEqual(row.id.hex, lookup_id_of(issued.token))
        self.assertNotEqual(row.id, row.audit_ref)

    async def test_the_users_table_has_no_password_session_or_passkey_column(self):
        columns = await self.query(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'users' ORDER BY column_name"
        )

        self.assertEqual(
            [column[0] for column in columns],
            [
                "created_at",
                "id",
                "login_name",
                "passkey_required",
                "status",
                "system_role",
                "updated_at",
            ],
        )

    async def test_the_owner_role_is_the_system_role_of_the_authorization_layer(self):
        await self.operator.setup_owner("boss")

        self.assertEqual(
            await self.scalar("SELECT system_role FROM users"), SystemRole.OWNER.value
        )

    async def test_the_ttl_is_configurable(self):
        service = self.make_operator(ttl_seconds=90)

        issued = await service.setup_owner("boss")

        self.assertEqual(issued.expires_at, T0 + timedelta(seconds=90))
        self.assertEqual(
            await self.scalar("SELECT expires_at FROM setup_tokens"),
            T0 + timedelta(seconds=90),
        )

    async def test_only_a_salted_hmac_of_the_secret_is_stored(self):
        issued = await self.operator.setup_owner("boss")

        (row,) = await self.query("SELECT salt, secret_hash FROM setup_tokens")
        secret = secret_of(issued.token)
        self.assertEqual(len(bytes(row.salt)), 16)
        self.assertEqual(
            bytes(row.secret_hash),
            hmac.new(bytes(row.salt), secret.encode(), "sha256").digest(),
        )
        stored = await self.everything_stored()
        self.assertNotIn(secret, stored)
        self.assertNotIn(issued.token, stored)

    async def test_the_result_does_not_print_the_token(self):
        issued = await self.operator.setup_owner("boss")

        self.assertNotIn(secret_of(issued.token), repr(issued))
        self.assertNotIn(secret_of(issued.token), str(issued))

    async def test_the_owner_passkey_requirement_is_set_and_cannot_be_cleared(self):
        await self.operator.setup_owner("boss")

        self.assertTrue(await self.scalar("SELECT passkey_required FROM users"))
        with self.assertRaises(DBAPIError):
            await self.execute("UPDATE users SET passkey_required = false")

    async def test_a_second_setup_is_refused_and_creates_nothing(self):
        first = await self.operator.setup_owner("boss")

        with self.assertRaises(OwnerAlreadyExistsError):
            await self.operator.setup_owner("someone-else")

        self.assertEqual(await self.owner_count(), 1)
        self.assertEqual(await self.scalar("SELECT count(*) FROM users"), 1)
        self.assertEqual(await self.scalar("SELECT count(*) FROM setup_tokens"), 1)
        self.assertEqual(await self.scalar("SELECT id FROM users"), first.user_id)

    async def test_setup_stays_closed_after_the_owner_finished_setup(self):
        issued = await self.operator.setup_owner("boss")
        await self.redeemer.redeem(issued.token)

        with self.assertRaises(OwnerAlreadyExistsError):
            await self.operator.setup_owner("another")

    async def test_a_login_name_used_by_another_user_is_refused(self):
        await self.execute(
            "INSERT INTO users (id, login_name, system_role, status, "
            "passkey_required, created_at, updated_at) VALUES "
            "(:id, 'boss', 'admin', 'active', true, :now, :now)",
            id=uuid.uuid4(),
            now=T0,
        )

        with self.assertRaises(LoginNameTakenError):
            await self.operator.setup_owner("BOSS")

        self.assertEqual(await self.owner_count(), 0)
        self.assertEqual(
            await self.audit_summary(),
            Counter({("owner.create", "deny", "login_name_taken"): 1}),
        )

    async def test_an_invalid_login_name_is_refused_before_anything_is_written(self):
        for name in ("no", "has space", "", None, 7):
            with self.subTest(name):
                with self.assertRaises(InvalidLoginNameError):
                    await self.operator.setup_owner(name)

        self.assertEqual(await self.scalar("SELECT count(*) FROM users"), 0)
        self.assertEqual(await self.audit_summary(), Counter())

    async def test_setup_is_audited_with_ids_and_enums_only(self):
        issued = await self.operator.setup_owner("Tomoki")

        rows = await self.audit_rows()
        self.assertEqual(
            Counter((r.action, r.decision, r.reason) for r in rows),
            Counter(
                {
                    ("owner.create", "allow", "created"): 1,
                    ("owner.setup_token.issue", "allow", "issued"): 1,
                }
            ),
        )
        by_action = {row.action: row for row in rows}
        create = by_action["owner.create"]
        issue = by_action["owner.setup_token.issue"]
        self.assertEqual(
            (
                create.resource_kind,
                create.resource_id,
                create.new_role,
                create.old_role,
            ),
            ("user", issued.user_id, "owner", None),
        )
        self.assertEqual(
            (issue.resource_kind, issue.resource_id),
            ("setup_token", issued.audit_ref),
        )
        for row in rows:
            self.assertEqual((row.actor_id, row.actor_role), (None, "system"))
            self.assertEqual(row.occurred_at, T0)
        self.assertEqual(create.correlation_id, issue.correlation_id)
        dump = "\n".join(str(tuple(row)) for row in rows)
        self.assertNotIn("tomoki", dump.lower())
        self.assertNotIn(secret_of(issued.token), dump)

    async def test_a_refused_setup_is_audited(self):
        await self.operator.setup_owner("boss")

        with self.assertRaises(OwnerAlreadyExistsError):
            await self.operator.setup_owner("boss2")

        self.assertEqual(
            (await self.audit_summary())[("owner.create", "deny", "owner_exists")], 1
        )

    async def test_two_concurrent_setups_exactly_one_succeeds(self):
        for round_number in range(1, 4):
            await self.reset_users()
            results = await self.gather_on_own_engines(
                6, lambda service, index: service.setup_owner(f"owner{index}")
            )

            issued = [r for r in results if isinstance(r, IssuedToken)]
            refused = [r for r in results if isinstance(r, OwnerAlreadyExistsError)]
            with self.subTest(round=round_number):
                self.assertEqual((len(issued), len(refused)), (1, 5), results)
                self.assertEqual(await self.owner_count(), 1)
                self.assertEqual(await self.scalar("SELECT count(*) FROM users"), 1)
                self.assertEqual(
                    await self.scalar("SELECT count(*) FROM setup_tokens"), 1
                )
                self.assertEqual(
                    await self.scalar("SELECT id FROM users"), issued[0].user_id
                )
                summary = await self.audit_summary()
                self.assertEqual(
                    summary[("owner.create", "allow", "created")], round_number
                )
                self.assertEqual(
                    summary[("owner.create", "deny", "owner_exists")], 5 * round_number
                )

    async def test_two_concurrent_setups_with_the_same_name_still_make_one_owner(self):
        results = await self.gather_on_own_engines(
            4, lambda service, index: service.setup_owner("boss")
        )

        issued = [r for r in results if isinstance(r, IssuedToken)]
        self.assertEqual(len(issued), 1, results)
        self.assertTrue(
            all(
                isinstance(r, OwnerAlreadyExistsError | LoginNameTakenError)
                for r in results
                if not isinstance(r, IssuedToken)
            ),
            results,
        )
        self.assertEqual(await self.owner_count(), 1)

    async def test_setup_fails_closed_when_the_audit_cannot_be_written(self):
        sink = FailingSink()
        service = self.make_operator(sink=sink)

        with LogCapture() as logs:
            with self.assertRaises(AuditUnavailableError):
                await service.setup_owner("boss")

        self.assertEqual(sink.attempts, 1)
        self.assertEqual(await self.scalar("SELECT count(*) FROM users"), 0)
        self.assertEqual(await self.scalar("SELECT count(*) FROM setup_tokens"), 0)
        self.assertIn("RuntimeError", logs.text)
        self.assertNotIn(SECRET_DETAIL, logs.text)
        # Nothing was left behind: the setup can simply be run again.
        again = await self.operator.setup_owner("boss")
        self.assertEqual(await self.owner_count(), 1)
        self.assertEqual(again.login_name, "boss")

    async def test_setup_rolls_back_when_the_audit_fails_after_its_first_event(self):
        service = self.make_operator(sink=FlakySink(self.database, allow=1))

        with self.audit_failure_is_logged_by_type_only():
            with self.assertRaises(AuditUnavailableError):
                await service.setup_owner("boss")

        self.assertEqual(await self.scalar("SELECT count(*) FROM users"), 0)
        self.assertEqual(await self.scalar("SELECT count(*) FROM setup_tokens"), 0)


@requires_postgres
class RecoverOwnerTest(PostgresIdentityTestCase):
    async def test_there_is_nothing_to_recover_without_an_owner(self):
        with self.assertRaises(OwnerNotFoundError):
            await self.operator.recover_owner()

        self.assertEqual(await self.scalar("SELECT count(*) FROM setup_tokens"), 0)
        self.assertEqual(
            await self.audit_summary(),
            Counter({("owner.recovery_token.issue", "deny", "owner_missing"): 1}),
        )

    async def test_recovery_is_refused_unless_the_process_is_root(self):
        # The requirement is a recovery through sudo, that is, as root. Holding the
        # database credential, or naming a sudo user, is not that.
        setup = await self.operator.setup_owner("boss")
        for name, uid, sudo_uid in (
            ("an ordinary user", 1000, None),
            ("SUDO_UID is only a hint", 1000, 1000),
            ("SUDO_UID of root", 1000, 0),
            ("a system user", 1, None),
            ("the highest uid", 4_294_967_295, None),
        ):
            with self.subTest(name), running_as(uid, sudo_uid):
                with self.assertRaises(RecoveryNotPrivilegedError) as caught:
                    await self.operator.recover_owner()
                self.assertNotIn("boss", str(caught.exception))
        with self.subTest("no uid at all"):
            with patch("os.geteuid", side_effect=AttributeError):
                with self.assertRaises(RecoveryNotPrivilegedError):
                    await self.operator.recover_owner()

        # Nothing changed: the setup token still works, and no token was added.
        self.assertEqual(await self.scalar("SELECT count(*) FROM setup_tokens"), 1)
        self.assertEqual(
            await self.scalar(
                "SELECT count(*) FROM setup_tokens "
                "WHERE used_at IS NULL AND revoked_at IS NULL"
            ),
            1,
        )
        self.assertEqual(
            (await self.redeemer.redeem(setup.token)).purpose, TokenPurpose.SETUP
        )
        summary = await self.audit_summary()
        self.assertEqual(
            summary[("owner.recovery_token.issue", "deny", "not_privileged")], 6
        )
        self.assertNotIn(("owner.recovery_token.issue", "allow", "issued"), summary)

    async def test_the_service_reads_the_effective_uid_of_the_process_itself(self):
        # The privilege is the running process's, read inside the service: the same
        # service and the same call are refused or allowed by nothing but the uid
        # the process has.
        await self.operator.setup_owner("boss")

        with running_as(1000):
            with self.assertRaises(RecoveryNotPrivilegedError):
                await self.operator.recover_owner()
        with running_as(0):
            recovery = await self.operator.recover_owner()

        self.assertEqual(recovery.purpose, TokenPurpose.RECOVERY)
        summary = await self.audit_summary()
        self.assertEqual(
            summary[("owner.recovery_token.issue", "deny", "not_privileged")], 1
        )
        self.assertEqual(summary[("owner.recovery_token.issue", "allow", "issued")], 1)

    async def test_a_caller_cannot_vouch_for_root(self):
        # A library caller that is not root cannot make itself root by passing an
        # identity: there is no such parameter to trust (it was the review finding
        # that ``operator=OperatorIdentity(uid=0)`` was enough).
        setup = await self.operator.setup_owner("boss")

        with running_as(1000):
            with self.assertRaises(TypeError):
                await self.operator.recover_owner(operator=OperatorIdentity(0))
            with self.assertRaises(TypeError):
                await self.operator.recover_owner(OperatorIdentity(0))
            with self.assertRaises(TypeError):
                await self.operator.setup_owner("other", operator=OperatorIdentity(0))
            with self.assertRaises(RecoveryNotPrivilegedError):
                await self.operator.recover_owner()

        self.assertEqual(
            list(inspect.signature(OwnerOperator.recover_owner).parameters), ["self"]
        )
        self.assertEqual(
            list(inspect.signature(OwnerOperator.setup_owner).parameters),
            ["self", "login_name", "replace_non_live_owner"],
        )
        self.assertEqual(await self.scalar("SELECT count(*) FROM setup_tokens"), 1)
        self.assertEqual(
            (await self.redeemer.redeem(setup.token)).purpose, TokenPurpose.SETUP
        )
        self.assertNotIn(
            ("owner.recovery_token.issue", "allow", "issued"),
            await self.audit_summary(),
        )

    async def test_a_second_service_object_is_refused_in_the_same_process(self):
        # No state of the object grants the privilege: every service, however it
        # was built, asks the process.
        await self.operator.setup_owner("boss")
        other = self.make_operator(self.new_database())

        with running_as(1000):
            with self.assertRaises(RecoveryNotPrivilegedError):
                await other.recover_owner()
            with self.assertRaises(RecoveryNotPrivilegedError):
                await self.operator.recover_owner()

    async def test_recovery_as_root_records_uid_zero_and_the_sudo_user(self):
        await self.operator.setup_owner("boss")

        with running_as(0, sudo_uid=1000):
            recovery = await self.operator.recover_owner()

        row = (
            await self.query(
                "SELECT issued_by_uid, issued_by_sudo_uid FROM setup_tokens "
                "WHERE audit_ref = :ref",
                ref=recovery.audit_ref,
            )
        )[0]
        self.assertEqual(tuple(row), (0, 1000))
        self.assertEqual(recovery.operator, OperatorIdentity(0, sudo_uid=1000))

    async def test_issues_a_recovery_token_for_the_existing_owner(self):
        setup = await self.operator.setup_owner("boss")
        self.clock.advance(seconds=60)

        recovery = await self.operator.recover_owner()

        self.assertEqual(recovery.purpose, TokenPurpose.RECOVERY)
        self.assertEqual(
            (recovery.user_id, recovery.login_name), (setup.user_id, "boss")
        )
        self.assertNotEqual(recovery.token, setup.token)
        self.assertEqual(recovery.expires_at, T0 + timedelta(seconds=60 + TTL))
        self.assertEqual(await self.owner_count(), 1)
        row = (
            await self.query(
                "SELECT * FROM setup_tokens WHERE audit_ref = :ref",
                ref=recovery.audit_ref,
            )
        )[0]
        self.assertEqual(
            (row.purpose, row.user_id, row.used_at, row.attempts),
            ("recovery", setup.user_id, None, 0),
        )

    async def test_outstanding_tokens_are_invalidated_and_the_revoke_is_audited(self):
        setup = await self.operator.setup_owner("boss")
        self.clock.advance(seconds=60)

        recovery = await self.operator.recover_owner()

        self.assertEqual(
            await self.scalar(
                "SELECT revoked_at FROM setup_tokens WHERE audit_ref = :ref",
                ref=setup.audit_ref,
            ),
            T0 + timedelta(seconds=60),
        )
        with self.assertRaises(SetupTokenRejectedError):
            await self.redeemer.redeem(setup.token)
        redemption = await self.redeemer.redeem(recovery.token)
        self.assertEqual(redemption.purpose, TokenPurpose.RECOVERY)
        summary = await self.audit_summary()
        self.assertEqual(summary[("owner.token.revoke", "allow", "superseded")], 1)
        self.assertEqual(summary[("owner.recovery_token.issue", "allow", "issued")], 1)
        self.assertEqual(summary[("owner.token.redeem", "deny", "token_revoked")], 1)
        self.assertEqual(summary[("owner.token.redeem", "allow", "redeemed")], 1)

    async def test_a_second_recovery_supersedes_the_first(self):
        await self.operator.setup_owner("boss")
        first = await self.operator.recover_owner()
        second = await self.operator.recover_owner()

        with self.assertRaises(SetupTokenRejectedError):
            await self.redeemer.redeem(first.token)
        await self.redeemer.redeem(second.token)
        self.assertEqual(
            await self.scalar(
                "SELECT count(*) FROM setup_tokens WHERE revoked_at IS NOT NULL"
            ),
            2,
        )

    async def test_at_most_one_token_is_outstanding(self):
        await self.operator.setup_owner("boss")
        for _ in range(3):
            await self.operator.recover_owner()

        self.assertEqual(
            await self.scalar(
                "SELECT count(*) FROM setup_tokens "
                "WHERE used_at IS NULL AND revoked_at IS NULL"
            ),
            1,
        )

    async def test_a_used_token_is_left_alone(self):
        setup = await self.operator.setup_owner("boss")
        await self.redeemer.redeem(setup.token)
        used_at = await self.scalar("SELECT used_at FROM setup_tokens")

        await self.operator.recover_owner()

        self.assertEqual(
            await self.scalar(
                "SELECT used_at FROM setup_tokens WHERE audit_ref = :ref",
                ref=setup.audit_ref,
            ),
            used_at,
        )
        self.assertEqual(
            (await self.audit_summary())[("owner.token.revoke", "allow", "superseded")],
            0,
        )

    async def test_an_owner_who_is_pending_deletion_cannot_be_recovered(self):
        await self.operator.setup_owner("boss")
        await self.execute("UPDATE users SET status = 'pending_deletion'")

        with self.assertRaises(OwnerNotLiveError) as caught:
            await self.operator.recover_owner()

        # The real state is reported, not "no Owner".
        self.assertEqual(caught.exception.status, "pending_deletion")
        self.assertIn("pending_deletion", str(caught.exception))
        self.assertEqual(
            (await self.audit_summary())[
                ("owner.recovery_token.issue", "deny", "owner_not_live")
            ],
            1,
        )

    async def test_an_active_owner_can_be_recovered(self):
        setup = await self.operator.setup_owner("boss")
        await self.redeemer.redeem(setup.token)
        await self.execute("UPDATE users SET status = 'active'")

        recovery = await self.operator.recover_owner()
        redemption = await self.redeemer.redeem(recovery.token)

        self.assertEqual(
            (redemption.purpose, redemption.user_status),
            (TokenPurpose.RECOVERY, UserStatus.ACTIVE),
        )

    async def test_concurrent_recoveries_leave_exactly_one_working_token(self):
        await self.operator.setup_owner("boss")

        results = await self.gather_on_own_engines(
            4, lambda service, index: service.recover_owner()
        )

        issued = [r for r in results if isinstance(r, IssuedToken)]
        self.assertEqual(len(issued), 4, results)
        self.assertEqual(
            await self.scalar(
                "SELECT count(*) FROM setup_tokens "
                "WHERE used_at IS NULL AND revoked_at IS NULL"
            ),
            1,
        )
        outcomes = []
        for candidate in issued:
            try:
                await self.redeemer.redeem(candidate.token)
                outcomes.append("redeemed")
            except SetupTokenRejectedError:
                outcomes.append("rejected")
        self.assertEqual(
            sorted(outcomes), ["redeemed", "rejected", "rejected", "rejected"]
        )

    async def test_recovery_fails_closed_when_the_audit_cannot_be_written(self):
        setup = await self.operator.setup_owner("boss")
        service = self.make_operator(sink=FailingSink())

        with self.audit_failure_is_logged_by_type_only():
            with self.assertRaises(AuditUnavailableError):
                await service.recover_owner()

        # Nothing changed: the old token is still the outstanding one.
        self.assertEqual(await self.scalar("SELECT count(*) FROM setup_tokens"), 1)
        self.assertIsNone(await self.scalar("SELECT revoked_at FROM setup_tokens"))
        await self.redeemer.redeem(setup.token)

    async def test_a_recovery_with_no_outstanding_token_still_fails_closed(self):
        # Nothing to revoke: the issue event is the only one, so it must be
        # stored before the commit (an event written after it would not be).
        setup = await self.operator.setup_owner("boss")
        await self.redeemer.redeem(setup.token)  # the only token is now used
        broken = self.make_operator(sink=FailingSink())

        with self.audit_failure_is_logged_by_type_only():
            with self.assertRaises(AuditUnavailableError):
                await broken.recover_owner()

        self.assertEqual(await self.scalar("SELECT count(*) FROM setup_tokens"), 1)
        self.assertEqual(
            (await self.audit_summary())[
                ("owner.recovery_token.issue", "allow", "issued")
            ],
            0,
        )
        # And once the audit works again, the recovery goes through.
        await self.operator.recover_owner()
        self.assertEqual(await self.scalar("SELECT count(*) FROM setup_tokens"), 2)

    async def test_a_refusal_stands_even_when_its_audit_cannot_be_written(self):
        broken = self.make_operator(sink=FailingSink())

        with self.audit_failure_is_logged_by_type_only():
            with self.assertRaises(OwnerNotFoundError):  # not AuditUnavailableError
                await broken.recover_owner()
            await self.operator.setup_owner("boss")
            with self.assertRaises(OwnerAlreadyExistsError):
                await broken.setup_owner("someone")


@requires_postgres
class RedeemTest(PostgresIdentityTestCase):
    async def test_a_setup_token_redeems_once(self):
        issued = await self.operator.setup_owner("boss")
        self.clock.advance(seconds=30)

        redemption = await self.redeemer.redeem(issued.token)

        self.assertEqual(
            redemption,
            Redemption(
                user_id=issued.user_id,
                audit_ref=issued.audit_ref,
                purpose=TokenPurpose.SETUP,
                user_status=UserStatus.INVITED,
                passkey_required=True,
            ),
        )
        self.assertEqual(
            await self.scalar("SELECT used_at FROM setup_tokens"),
            T0 + timedelta(seconds=30),
        )
        with self.assertRaises(SetupTokenRejectedError):
            await self.redeemer.redeem(issued.token)

    async def test_redemption_creates_no_user_and_no_session(self):
        issued = await self.operator.setup_owner("boss")

        await self.redeemer.redeem(issued.token)

        self.assertEqual(await self.scalar("SELECT count(*) FROM users"), 1)
        self.assertEqual(await self.owner_count(), 1)
        # The status is the web flow's to move once credentials exist (PAW-022).
        self.assertEqual(await self.scalar("SELECT status FROM users"), "invited")

    async def test_redemption_is_audited_as_the_owner(self):
        issued = await self.operator.setup_owner("boss")

        await self.redeemer.redeem(issued.token)

        (row,) = [
            r for r in await self.audit_rows() if r.action == "owner.token.redeem"
        ]
        self.assertEqual(
            (row.decision, row.reason, row.actor_id, row.actor_role),
            ("allow", "redeemed", issued.user_id, "owner"),
        )
        self.assertEqual(
            (row.resource_kind, row.resource_id), ("setup_token", issued.audit_ref)
        )

    async def test_the_expiry_boundary(self):
        issued = await self.operator.setup_owner("boss")
        self.clock.advance(seconds=TTL - 1)
        early = await self.redeemer.redeem(issued.token)
        self.assertEqual(early.audit_ref, issued.audit_ref)

        second = await self.operator.recover_owner()
        self.clock.advance(seconds=TTL)  # exactly the expiry instant
        with self.assertRaises(SetupTokenRejectedError):
            await self.redeemer.redeem(second.token)
        self.assertEqual(
            (await self.audit_summary())[
                ("owner.token.redeem", "deny", "token_expired")
            ],
            1,
        )

    async def test_an_expired_token_is_rejected_and_stays_unused(self):
        issued = await self.operator.setup_owner("boss")
        self.clock.advance(hours=3)

        with self.assertRaises(SetupTokenRejectedError):
            await self.redeemer.redeem(issued.token)

        self.assertIsNone(await self.scalar("SELECT used_at FROM setup_tokens"))

    async def test_a_wrong_secret_is_rejected_and_counted(self):
        issued = await self.operator.setup_owner("boss")

        with self.assertRaises(SetupTokenRejectedError):
            await self.redeemer.redeem(wrong_secret_for(issued.token))

        self.assertEqual(await self.scalar("SELECT attempts FROM setup_tokens"), 1)
        self.assertIsNone(await self.scalar("SELECT used_at FROM setup_tokens"))
        self.assertEqual(
            (await self.audit_summary())[
                ("owner.token.redeem", "deny", "token_mismatch")
            ],
            1,
        )
        # The right token still works while attempts remain.
        await self.redeemer.redeem(issued.token)
        self.assertEqual(await self.scalar("SELECT attempts FROM setup_tokens"), 2)

    async def test_unknown_and_malformed_tokens_are_rejected_without_any_write(self):
        await self.operator.setup_owner("boss")
        before = await self.audit_summary()
        for value in [
            f"pawst1.{uuid.uuid4().hex}.{'A' * 43}",
            "garbage",
            "",
            None,
            12345,
            b"pawst1",
            "x" * 100_000,
        ]:
            with self.subTest(str(value)[:20]):
                with self.assertRaises(SetupTokenRejectedError):
                    await self.redeemer.redeem(value)

        self.assertEqual(await self.audit_summary(), before)
        self.assertEqual(await self.scalar("SELECT attempts FROM setup_tokens"), 0)

    async def test_lockout_after_the_maximum_number_of_attempts(self):
        redeemer = self.make_redeemer(max_attempts=3)
        issued = await self.operator.setup_owner("boss")
        wrong = wrong_secret_for(issued.token)

        for _ in range(3):
            with self.assertRaises(SetupTokenRejectedError):
                await redeemer.redeem(wrong)
        # Locked out for good: even the correct token no longer works ...
        with self.assertRaises(SetupTokenRejectedError):
            await redeemer.redeem(issued.token)

        self.assertEqual(await self.scalar("SELECT attempts FROM setup_tokens"), 3)
        self.assertIsNone(await self.scalar("SELECT used_at FROM setup_tokens"))
        summary = await self.audit_summary()
        self.assertEqual(summary[("owner.token.redeem", "deny", "token_mismatch")], 3)
        self.assertEqual(
            summary[("owner.token.redeem", "deny", "attempts_exhausted")], 1
        )
        # ... and further attempts add nothing to the audit table (bounded rows).
        before = await self.audit_summary()
        for _ in range(5):
            with self.assertRaises(SetupTokenRejectedError):
                await redeemer.redeem(wrong)
        self.assertEqual(await self.audit_summary(), before)
        self.assertEqual(await self.scalar("SELECT attempts FROM setup_tokens"), 3)
        # A new recovery token is the way out.
        recovery = await self.operator.recover_owner()
        await redeemer.redeem(recovery.token)

    async def test_the_last_permitted_attempt_can_still_succeed(self):
        redeemer = self.make_redeemer(max_attempts=3)
        issued = await self.operator.setup_owner("boss")
        for _ in range(2):
            with self.assertRaises(SetupTokenRejectedError):
                await redeemer.redeem(wrong_secret_for(issued.token))

        await redeemer.redeem(issued.token)

        summary = await self.audit_summary()
        self.assertEqual(
            summary[("owner.token.redeem", "deny", "attempts_exhausted")], 0
        )

    async def test_concurrent_wrong_guesses_never_exceed_the_maximum(self):
        issued = await self.operator.setup_owner("boss")
        wrong = wrong_secret_for(issued.token)

        results = await self.gather_on_own_engines(
            10,
            lambda service, index: service.redeem(wrong),
            redeemer=True,
            max_attempts=3,
        )

        self.assertTrue(
            all(isinstance(r, SetupTokenRejectedError) for r in results), results
        )
        self.assertEqual(await self.scalar("SELECT attempts FROM setup_tokens"), 3)
        summary = await self.audit_summary()
        self.assertEqual(summary[("owner.token.redeem", "deny", "token_mismatch")], 3)
        self.assertEqual(
            summary[("owner.token.redeem", "deny", "attempts_exhausted")], 1
        )

    async def test_concurrent_redemptions_exactly_one_succeeds(self):
        issued = await self.operator.setup_owner("boss")

        results = await self.gather_on_own_engines(
            6,
            lambda service, index: service.redeem(issued.token),
            redeemer=True,
            max_attempts=20,
        )

        redeemed = [r for r in results if isinstance(r, Redemption)]
        rejected = [r for r in results if isinstance(r, SetupTokenRejectedError)]
        self.assertEqual((len(redeemed), len(rejected)), (1, 5), results)
        summary = await self.audit_summary()
        self.assertEqual(summary[("owner.token.redeem", "allow", "redeemed")], 1)
        denied = sum(
            count
            for (action, decision, _), count in summary.items()
            if action == "owner.token.redeem" and decision == "deny"
        )
        self.assertEqual(denied, 5)

    async def test_every_failure_is_the_same_failure(self):
        outcomes: dict[str, SetupTokenRejectedError] = {}

        async def rejection(name, token, service=None):
            try:
                await (service or self.redeemer).redeem(token)
            except SetupTokenRejectedError as error:
                outcomes[name] = error
            else:
                self.fail(f"{name}: the token was accepted")

        setup = await self.operator.setup_owner("boss")
        await rejection("wrong secret", wrong_secret_for(setup.token))
        await rejection("unknown id", tokens.generate().token)
        await rejection("malformed", "not-a-token")
        await rejection("not a string", None)
        await self.redeemer.redeem(setup.token)
        await rejection("used", setup.token)
        revoked = await self.operator.recover_owner()
        expired = await self.operator.recover_owner()
        await rejection("revoked", revoked.token)
        self.clock.advance(hours=3)
        await rejection("expired", expired.token)
        locked = await self.operator.recover_owner()
        strict = self.make_redeemer(max_attempts=2)
        await rejection(
            "wrong before the lockout", wrong_secret_for(locked.token), strict
        )
        await rejection("wrong at the lockout", wrong_secret_for(locked.token), strict)
        await rejection("locked out", locked.token, strict)
        # Every reason the audit trail can tell apart was reached.
        summary = await self.audit_summary()
        for reason in (
            "token_mismatch",
            "token_used",
            "token_revoked",
            "token_expired",
            "attempts_exhausted",
        ):
            self.assertEqual(
                summary[("owner.token.redeem", "deny", reason)] >= 1, True, reason
            )

        self.assertEqual(len(outcomes), 10)
        signatures = {
            (
                type(error),
                error.args,
                str(error),
                repr(error),
                error.__cause__,
                error.__context__,
                error.__suppress_context__,
            )
            for error in outcomes.values()
        }
        self.assertEqual(len(signatures), 1, signatures)

    async def test_every_failure_does_the_same_comparison_and_reservation_work(self):
        setup = await self.operator.setup_owner("boss")
        statements: list[str] = []

        @event.listens_for(self.database.engine.sync_engine, "before_cursor_execute")
        def capture(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        real_compare = hmac.compare_digest
        cases = {
            "wrong secret": wrong_secret_for(setup.token),
            "unknown id": tokens.generate().token,
            "the dummy secret": f"pawst1.{uuid.uuid4().hex}.{'A' * 43}",
            "malformed": "not-a-token",
            "empty": "",
            "not a string": None,
        }
        for name, token in cases.items():
            statements.clear()
            with self.subTest(name):
                with patch.object(
                    tokens.hmac, "compare_digest", side_effect=real_compare
                ) as compare:
                    with self.assertRaises(SetupTokenRejectedError):
                        await self.redeemer.redeem(token)
                self.assertEqual(compare.call_count, 1)
                reservations = [
                    s
                    for s in statements
                    if s.startswith("UPDATE setup_tokens SET attempts")
                ]
                self.assertEqual(len(reservations), 1)

    async def test_the_web_flow_can_finish_its_part_in_the_same_transaction(self):
        issued = await self.operator.setup_owner("boss")
        seen = []

        async def apply(session, redemption):
            seen.append(redemption)
            # The token is already consumed in this transaction ...
            used = await session.scalar(text("SELECT used_at FROM setup_tokens"))
            seen.append(used)
            # ... and the flow's own change commits or rolls back with it.
            await session.execute(
                text("UPDATE users SET status = 'active' WHERE id = :id"),
                {"id": redemption.user_id},
            )

        redemption = await self.redeemer.redeem(issued.token, apply=apply)

        self.assertEqual(seen, [redemption, T0])
        self.assertEqual(await self.scalar("SELECT status FROM users"), "active")
        self.assertEqual(await self.scalar("SELECT used_at FROM setup_tokens"), T0)

    async def test_a_failing_hook_rolls_the_redemption_back(self):
        issued = await self.operator.setup_owner("boss")

        async def apply(session, redemption):
            await session.execute(text("UPDATE users SET status = 'active'"))
            raise ZeroDivisionError

        with self.assertRaises(ZeroDivisionError):
            await self.redeemer.redeem(issued.token, apply=apply)

        self.assertEqual(await self.scalar("SELECT status FROM users"), "invited")
        self.assertIsNone(await self.scalar("SELECT used_at FROM setup_tokens"))
        self.assertEqual(
            (await self.audit_summary())[("owner.token.redeem", "allow", "redeemed")], 0
        )
        # The token was not burned: the flow can try again.
        await self.redeemer.redeem(issued.token)

    async def test_the_hook_must_be_callable(self):
        with self.assertRaises(TypeError):
            await self.redeemer.redeem("x", apply="not callable")

    async def test_redemption_fails_closed_when_the_audit_cannot_be_written(self):
        issued = await self.operator.setup_owner("boss")
        service = self.make_redeemer(sink=FailingSink())

        with self.audit_failure_is_logged_by_type_only():
            with self.assertRaises(AuditUnavailableError):
                await service.redeem(issued.token)

        self.assertIsNone(await self.scalar("SELECT used_at FROM setup_tokens"))

    async def test_a_rejection_stays_a_rejection_when_the_audit_is_down(self):
        issued = await self.operator.setup_owner("boss")
        service = self.make_redeemer(sink=FailingSink())

        with self.audit_failure_is_logged_by_type_only():
            with self.assertRaises(SetupTokenRejectedError):
                await service.redeem(wrong_secret_for(issued.token))

    async def test_a_token_of_a_user_who_is_no_longer_the_owner_is_rejected(self):
        issued = await self.operator.setup_owner("boss")
        # As after an ownership transfer: the former Owner is an Admin.
        await self.execute("UPDATE users SET system_role = 'admin'")

        with self.assertRaises(SetupTokenRejectedError):
            await self.redeemer.redeem(issued.token)

        self.assertIsNone(await self.scalar("SELECT used_at FROM setup_tokens"))
        self.assertEqual(
            (await self.audit_summary())[
                ("owner.token.redeem", "deny", "user_not_eligible")
            ],
            1,
        )

    async def test_a_token_of_a_user_who_is_pending_deletion_is_rejected(self):
        issued = await self.operator.setup_owner("boss")
        await self.execute("UPDATE users SET status = 'pending_deletion'")

        with self.assertRaises(SetupTokenRejectedError):
            await self.redeemer.redeem(issued.token)

        self.assertIsNone(await self.scalar("SELECT used_at FROM setup_tokens"))

    async def test_a_token_survives_a_restart(self):
        issued = await self.operator.setup_owner("boss")

        other_process = self.make_redeemer(self.new_database())

        redemption = await other_process.redeem(issued.token)
        self.assertEqual(redemption.user_id, issued.user_id)


@requires_postgres
class NothingSecretIsLeakedTest(PostgresIdentityTestCase):
    async def test_no_plaintext_token_reaches_a_log_a_row_or_an_audit_event(self):
        tokens_seen: list[str] = []
        with LogCapture() as logs:
            setup = await self.operator.setup_owner("boss")
            tokens_seen.append(setup.token)
            with self.assertRaises(SetupTokenRejectedError):
                await self.redeemer.redeem(wrong_secret_for(setup.token))
            await self.redeemer.redeem(setup.token)
            with self.assertRaises(SetupTokenRejectedError):
                await self.redeemer.redeem(setup.token)
            recovery = await self.operator.recover_owner()
            tokens_seen.append(recovery.token)
            self.clock.advance(hours=3)
            with self.assertRaises(SetupTokenRejectedError):
                await self.redeemer.redeem(recovery.token)
            with self.assertRaises(OwnerAlreadyExistsError):
                await self.operator.setup_owner("again")

        # The capture is not vacuous: SQL and its parameters were logged.
        self.assertIn("INSERT INTO setup_tokens", logs.text)
        self.assertIn("UPDATE setup_tokens SET attempts", logs.text)
        stored = await self.everything_stored()
        for token in tokens_seen:
            for secret in (token, secret_of(token)):
                self.assertNotIn(secret, logs.text)
                self.assertNotIn(secret, stored)
        self.assertNotIn("A" * 43, logs.text)


@requires_postgres
class AuditReferenceTest(PostgresIdentityTestCase):
    """The token's lookup id is a secret-adjacent key: it never leaves the row."""

    async def lifecycle(self):
        """A setup, wrong tries, a redemption, a recovery, an expiry and a lockout."""
        strict = self.make_redeemer(max_attempts=2)
        setup = await self.operator.setup_owner("boss")
        with self.assertRaises(SetupTokenRejectedError):
            await self.redeemer.redeem(wrong_secret_for(setup.token))
        await self.redeemer.redeem(setup.token)
        with self.assertRaises(SetupTokenRejectedError):
            await self.redeemer.redeem(setup.token)
        first = await self.operator.recover_owner()
        second = await self.operator.recover_owner()
        with self.assertRaises(SetupTokenRejectedError):
            await self.redeemer.redeem(first.token)
        for _ in range(2):
            with self.assertRaises(SetupTokenRejectedError):
                await strict.redeem(wrong_secret_for(second.token))
        with self.assertRaises(SetupTokenRejectedError):
            await strict.redeem(second.token)
        self.clock.advance(hours=3)
        third = await self.operator.recover_owner()
        self.clock.advance(hours=3)
        with self.assertRaises(SetupTokenRejectedError):
            await self.redeemer.redeem(third.token)
        return [setup, first, second, third]

    def spellings(self, token: str) -> list[str]:
        lookup = uuid.UUID(hex=lookup_id_of(token))
        return [lookup.hex, str(lookup)]

    async def test_no_audit_row_and_no_log_line_contains_the_lookup_id(self):
        with LogCapture() as logs:
            issued = await self.lifecycle()

        rows = await self.audit_rows()
        dump = "\n".join(str(tuple(row)) for row in rows)
        self.assertGreater(len(rows), 10)  # the check is not vacuous
        for token in issued:
            for spelling in self.spellings(token.token):
                with self.subTest(spelling=spelling[:8]):
                    self.assertNotIn(spelling, dump)
                    # The application's own log lines. (The SQL echo of the
                    # SQLAlchemy engine carries every parameter, by design.)
                    self.assertNotIn(spelling, logs.application_text)
        self.assertIn("paw_backend", logs.application_text)

    async def test_audit_rows_name_each_token_by_its_audit_ref_only(self):
        issued = await self.lifecycle()

        refs = {token.audit_ref for token in issued}
        named = {
            row.resource_id
            for row in await self.audit_rows()
            if row.resource_kind == "setup_token"
        }
        self.assertEqual(named, refs)
        lookups = {row.id for row in await self.query("SELECT id FROM setup_tokens")}
        self.assertEqual(lookups & refs, set())

    async def test_the_redemption_carries_the_audit_ref_not_the_lookup_id(self):
        issued = await self.operator.setup_owner("boss")

        redemption = await self.redeemer.redeem(issued.token)

        self.assertEqual(redemption.audit_ref, issued.audit_ref)
        self.assertNotIn(lookup_id_of(issued.token), repr(redemption))
        self.assertNotIn(
            str(uuid.UUID(hex=lookup_id_of(issued.token))), repr(redemption)
        )
        self.assertFalse(hasattr(redemption, "token_id"))
        self.assertFalse(hasattr(issued, "token_id"))

    async def test_knowing_the_audit_ref_lets_nobody_redeem_or_lock_the_token(self):
        # What an Admin who can read the audit trail knows: the audit_ref.
        issued = await self.operator.setup_owner("boss")
        known = issued.audit_ref.hex
        secret = secret_of(issued.token)
        before = await self.audit_summary()

        for _ in range(12):
            for attempt in (
                f"pawst1.{known}.{'A' * 43}",  # the audit_ref as the id, a guess
                f"pawst1.{known}.{secret}",  # even with the real secret
            ):
                with self.assertRaises(SetupTokenRejectedError):
                    await self.redeemer.redeem(attempt)

        # Nothing was counted, locked or written, and the Owner's token works.
        self.assertEqual(
            await self.query("SELECT attempts, locked_at FROM setup_tokens"),
            [(0, None)],
        )
        self.assertEqual(await self.audit_summary(), before)
        redemption = await self.redeemer.redeem(issued.token)
        self.assertEqual(redemption.user_id, issued.user_id)


@requires_postgres
class HookGuardTest(PostgresIdentityTestCase):
    """The ``apply`` hook may write, but not end the transaction it is lent."""

    async def redeem_with(self, hook, redeemer=None):
        issued = await self.operator.setup_owner("boss")
        with self.assertRaises(Exception) as caught:
            await (redeemer or self.redeemer).redeem(issued.token, apply=hook)
        return issued, caught.exception

    async def assert_untouched(self):
        self.assertEqual(await self.scalar("SELECT status FROM users"), "invited")
        self.assertIsNone(await self.scalar("SELECT used_at FROM setup_tokens"))
        self.assertEqual(
            (await self.audit_summary())[("owner.token.redeem", "allow", "redeemed")], 0
        )

    async def test_a_hook_that_commits_is_refused_and_nothing_is_consumed(self):
        async def hook(session, redemption):
            await session.execute(text("UPDATE users SET status = 'active'"))
            await session.commit()

        issued, error = await self.redeem_with(hook)

        self.assertIsInstance(error, RedeemHookError)
        await self.assert_untouched()
        # Not burned: the flow can be repaired and try again.
        await self.redeemer.redeem(issued.token)

    async def test_a_committing_hook_cannot_beat_the_audit_failure(self):
        # The review's case: the hook commits, then the audit sink fails. The
        # token used to stay consumed with no audit row at all. Now the commit is
        # refused before the audit is even attempted.
        async def hook(session, redemption):
            await session.execute(text("UPDATE users SET status = 'active'"))
            await session.commit()

        sink = FailingSink()
        _, error = await self.redeem_with(hook, self.make_redeemer(sink=sink))

        self.assertIsInstance(error, RedeemHookError)
        self.assertEqual(sink.attempts, 0)
        await self.assert_untouched()

    async def test_a_hook_that_rolls_back_is_refused(self):
        async def hook(session, redemption):
            await session.execute(text("UPDATE users SET status = 'active'"))
            await session.rollback()

        _, error = await self.redeem_with(hook)

        self.assertIsInstance(error, RedeemHookError)
        await self.assert_untouched()

    async def test_a_hook_that_closes_the_session_is_refused(self):
        async def hook(session, redemption):
            await session.close()

        _, error = await self.redeem_with(hook)

        self.assertIsInstance(error, RedeemHookError)
        await self.assert_untouched()

    async def test_a_hook_that_swallows_the_commit_error_still_redeems_normally(self):
        async def hook(session, redemption):
            await session.execute(text("UPDATE users SET status = 'active'"))
            try:
                await session.commit()
            except RedeemHookError:
                pass  # carries on; the transaction is still open

        issued = await self.operator.setup_owner("boss")

        redemption = await self.redeemer.redeem(issued.token, apply=hook)

        self.assertEqual(redemption.user_id, issued.user_id)
        self.assertEqual(await self.scalar("SELECT status FROM users"), "active")
        self.assertIsNotNone(await self.scalar("SELECT used_at FROM setup_tokens"))
        self.assertEqual(
            (await self.audit_summary())[("owner.token.redeem", "allow", "redeemed")], 1
        )

    async def test_the_guard_is_only_attached_while_the_hook_runs(self):
        issued = await self.operator.setup_owner("boss")
        listeners = []

        async def hook(session, redemption):
            listeners.append(len(session.sync_session.dispatch.before_commit))
            sessions.append(session)

        sessions = []
        await self.redeemer.redeem(issued.token, apply=hook)

        (session,) = sessions
        listeners.append(len(session.sync_session.dispatch.before_commit))
        self.assertEqual(listeners, [1, 0])

    async def test_the_owner_row_stays_locked_until_the_redemption_ends(self):
        # The documented contract for PAW-022: while the hook runs, nobody else
        # can lock (or change) the Owner's users row or the token row.
        issued = await self.operator.setup_owner("boss")
        states = []

        async def hook(session, redemption):
            for table in ("users", "setup_tokens"):
                try:
                    async with self.database.session() as other:
                        await other.execute(
                            text(f"SELECT 1 FROM {table} FOR UPDATE NOWAIT")
                        )
                    states.append((table, "free"))
                except DBAPIError as error:
                    states.append((table, error.orig.sqlstate))

        await self.redeemer.redeem(issued.token, apply=hook)

        self.assertEqual(
            states,
            [("users", "55P03"), ("setup_tokens", "55P03")],  # lock_not_available
        )
        async with self.database.session() as other:  # released afterwards
            await other.execute(text("SELECT 1 FROM users FOR UPDATE NOWAIT"))


@requires_postgres
class LockoutPersistenceTest(PostgresIdentityTestCase):
    """A lockout is stored: changing the setting later cannot reopen a token."""

    async def lock_a_token(self, max_attempts: int = 3):
        issued = await self.operator.setup_owner("boss")
        strict = self.make_redeemer(max_attempts=max_attempts)
        for _ in range(max_attempts):
            with self.assertRaises(SetupTokenRejectedError):
                await strict.redeem(wrong_secret_for(issued.token))
        return issued

    async def test_the_attempt_that_uses_the_last_one_records_the_lock(self):
        issued = await self.lock_a_token(3)

        row = (await self.query("SELECT attempts, locked_at FROM setup_tokens"))[0]
        self.assertEqual(tuple(row), (3, T0))
        self.assertIsNone(await self.scalar("SELECT used_at FROM setup_tokens"))
        self.assertEqual(issued.purpose, TokenPurpose.SETUP)

    async def test_raising_the_maximum_does_not_reopen_a_locked_token(self):
        issued = await self.lock_a_token(3)

        for maximum in (4, 20):
            with self.subTest(maximum=maximum):
                relaxed = self.make_redeemer(max_attempts=maximum)
                with self.assertRaises(SetupTokenRejectedError):
                    await relaxed.redeem(issued.token)
                with self.assertRaises(SetupTokenRejectedError):
                    await relaxed.redeem(wrong_secret_for(issued.token))

        self.assertEqual(
            await self.query("SELECT attempts, locked_at FROM setup_tokens"),
            [(3, T0)],
        )
        # Recovery is the way out, as documented.
        recovery = await self.operator.recover_owner()
        await self.make_redeemer(max_attempts=20).redeem(recovery.token)

    async def test_a_token_that_is_not_locked_follows_the_new_maximum(self):
        issued = await self.operator.setup_owner("boss")
        loose = self.make_redeemer(max_attempts=20)
        for _ in range(2):
            with self.assertRaises(SetupTokenRejectedError):
                await loose.redeem(wrong_secret_for(issued.token))
        self.assertIsNone(await self.scalar("SELECT locked_at FROM setup_tokens"))

        tight = self.make_redeemer(max_attempts=3)
        with self.assertRaises(SetupTokenRejectedError):
            await tight.redeem(wrong_secret_for(issued.token))  # the 3rd: locks
        self.assertEqual(await self.scalar("SELECT locked_at FROM setup_tokens"), T0)
        with self.assertRaises(SetupTokenRejectedError):
            await loose.redeem(issued.token)

    async def test_the_lockout_is_audited_once_however_the_setting_changes(self):
        issued = await self.lock_a_token(2)
        relaxed = self.make_redeemer(max_attempts=20)
        for _ in range(4):
            with self.assertRaises(SetupTokenRejectedError):
                await relaxed.redeem(wrong_secret_for(issued.token))

        summary = await self.audit_summary()
        self.assertEqual(summary[("owner.token.redeem", "deny", "token_mismatch")], 2)
        self.assertEqual(
            summary[("owner.token.redeem", "deny", "attempts_exhausted")], 1
        )

    async def test_a_successful_last_attempt_locks_a_token_that_is_already_used(self):
        redeemer = self.make_redeemer(max_attempts=2)
        issued = await self.operator.setup_owner("boss")
        with self.assertRaises(SetupTokenRejectedError):
            await redeemer.redeem(wrong_secret_for(issued.token))

        await redeemer.redeem(issued.token)

        self.assertEqual(
            await self.query("SELECT attempts, used_at IS NOT NULL FROM setup_tokens"),
            [(2, True)],
        )
        self.assertEqual(
            (await self.audit_summary())[
                ("owner.token.redeem", "deny", "attempts_exhausted")
            ],
            0,
        )


@requires_postgres
class NonLiveOwnerTest(PostgresIdentityTestCase):
    """An Owner row that is pending deletion or deleted, and the way out."""

    async def owner_in(self, status: str):
        issued = await self.operator.setup_owner("boss")
        await self.execute("UPDATE users SET status = :s", s=status)
        return issued

    async def test_setup_reports_the_real_state_instead_of_pointing_at_recovery(self):
        for status in ("pending_deletion", "deleted"):
            with self.subTest(status):
                await self.reset_users()
                await self.owner_in(status)
                before = await self.audit_summary()

                with self.assertRaises(OwnerNotLiveError) as caught:
                    await self.operator.setup_owner("another")

                self.assertEqual(caught.exception.status, status)
                self.assertNotIsInstance(caught.exception, OwnerAlreadyExistsError)
                self.assertEqual(await self.scalar("SELECT count(*) FROM users"), 1)
                after = await self.audit_summary()
                self.assertEqual(
                    after[("owner.create", "deny", "owner_not_live")]
                    - before[("owner.create", "deny", "owner_not_live")],
                    1,
                )

    async def test_recovery_reports_the_real_state_instead_of_pointing_at_setup(self):
        for status in ("pending_deletion", "deleted"):
            with self.subTest(status):
                await self.reset_users()
                await self.owner_in(status)

                with self.assertRaises(OwnerNotLiveError) as caught:
                    await self.operator.recover_owner()

                self.assertEqual(caught.exception.status, status)
                self.assertNotIsInstance(caught.exception, OwnerNotFoundError)

    async def test_the_explicit_flag_replaces_the_account_in_one_audited_step(self):
        old = await self.owner_in("deleted")

        new = await self.operator.setup_owner("newboss", replace_non_live_owner=True)

        rows = await self.query(
            "SELECT id, login_name, system_role, status, passkey_required "
            "FROM users ORDER BY login_name"
        )
        self.assertEqual(
            [tuple(row) for row in rows],
            [
                (old.user_id, "boss", "user", "deleted", True),  # demoted, not erased
                (new.user_id, "newboss", "owner", "invited", True),
            ],
        )
        self.assertEqual(await self.owner_count(), 1)
        # The old Owner's outstanding token is revoked: it cannot be redeemed.
        with self.assertRaises(SetupTokenRejectedError):
            await self.redeemer.redeem(old.token)
        self.assertEqual((await self.redeemer.redeem(new.token)).user_id, new.user_id)
        events = [
            row for row in await self.audit_rows() if row.action.startswith("owner.")
        ]
        by_action = {}
        for row in events:
            by_action.setdefault(row.action, []).append(row)
        (replace,) = by_action["owner.replace"]
        self.assertEqual(
            (
                replace.decision,
                replace.reason,
                replace.resource_id,
                replace.old_role,
                replace.new_role,
            ),
            ("allow", "replaced", old.user_id, "owner", "user"),
        )
        # All the events of the replacement share one correlation id.
        together = sorted(
            row.action for row in events if row.correlation_id == replace.correlation_id
        )
        self.assertEqual(
            together,
            [
                "owner.create",
                "owner.replace",
                "owner.setup_token.issue",
                "owner.token.revoke",
            ],
        )
        summary = await self.audit_summary()
        self.assertEqual(summary[("owner.token.revoke", "allow", "superseded")], 1)
        self.assertEqual(summary[("owner.create", "allow", "created")], 2)

    async def test_a_live_owner_is_never_replaced_not_even_with_the_flag(self):
        for status in ("invited", "active"):
            with self.subTest(status):
                await self.reset_users()
                old = await self.owner_in(status)

                with self.assertRaises(OwnerAlreadyExistsError):
                    await self.operator.setup_owner(
                        "another", replace_non_live_owner=True
                    )

                self.assertEqual(
                    await self.query("SELECT id, system_role FROM users"),
                    [(old.user_id, "owner")],
                )
                self.assertEqual(await self.outstanding_tokens(), 1)

    async def outstanding_tokens(self) -> int:
        return await self.scalar(
            "SELECT count(*) FROM setup_tokens "
            "WHERE used_at IS NULL AND revoked_at IS NULL"
        )

    async def test_the_flag_changes_nothing_when_there_is_no_owner(self):
        issued = await self.operator.setup_owner("boss", replace_non_live_owner=True)

        self.assertEqual(issued.login_name, "boss")
        self.assertEqual(await self.owner_count(), 1)
        self.assertEqual(
            (await self.audit_summary())[("owner.replace", "allow", "replaced")], 0
        )

    async def test_the_replacement_cannot_reuse_the_old_login_name(self):
        await self.owner_in("deleted")

        with self.assertRaises(LoginNameTakenError):
            await self.operator.setup_owner("boss", replace_non_live_owner=True)

        # Rolled back: the old account is still the (non-live) Owner.
        self.assertEqual(await self.scalar("SELECT system_role FROM users"), "owner")

    async def test_the_replacement_fails_closed_when_the_audit_cannot_be_written(self):
        old = await self.owner_in("deleted")
        for allowed in (0, 1, 2):
            with self.subTest(events_stored_first=allowed):
                broken = self.make_operator(sink=FlakySink(self.database, allowed))
                with self.assertRaises(AuditUnavailableError):
                    await broken.setup_owner("newboss", replace_non_live_owner=True)

                self.assertEqual(
                    await self.query("SELECT id, system_role FROM users"),
                    [(old.user_id, "owner")],
                )
                self.assertEqual(
                    await self.scalar("SELECT count(*) FROM setup_tokens"), 1
                )
                self.assertEqual(
                    await self.scalar("SELECT revoked_at IS NULL FROM setup_tokens"),
                    True,
                )

    async def test_concurrent_replacements_produce_exactly_one_new_owner(self):
        await self.owner_in("deleted")

        results = await self.gather_on_own_engines(
            4,
            lambda service, index: service.setup_owner(
                f"owner{index}", replace_non_live_owner=True
            ),
        )

        issued = [r for r in results if isinstance(r, IssuedToken)]
        self.assertEqual(len(issued), 1, results)
        self.assertTrue(
            all(
                isinstance(r, OwnerAlreadyExistsError)
                for r in results
                if not isinstance(r, IssuedToken)
            ),
            results,
        )
        self.assertEqual(await self.owner_count(), 1)
        self.assertEqual(await self.scalar("SELECT count(*) FROM users"), 2)

    async def test_the_flag_must_be_a_real_boolean(self):
        for value in ("yes", 1, None, "true"):
            with self.subTest(value):
                with self.assertRaises(TypeError):
                    await self.operator.setup_owner(
                        "boss", replace_non_live_owner=value
                    )
        self.assertEqual(await self.scalar("SELECT count(*) FROM users"), 0)


@requires_postgres
class OperatorRecordTest(PostgresIdentityTestCase):
    """Who ran the command is recorded (numeric ids) on the token it issued."""

    async def test_the_uids_are_stored_with_the_token_the_audit_refers_to(self):
        with running_as(1000, sudo_uid=1001):
            setup = await self.operator.setup_owner("boss")
        recovery = await self.operator.recover_owner()

        rows = await self.query(
            "SELECT audit_ref, purpose, issued_by_uid, issued_by_sudo_uid "
            "FROM setup_tokens ORDER BY created_at, purpose"
        )
        by_ref = {row.audit_ref: tuple(row)[1:] for row in rows}
        self.assertEqual(by_ref[setup.audit_ref], ("setup", 1000, 1001))
        self.assertEqual(by_ref[recovery.audit_ref], ("recovery", 0, None))
        # An audit event reaches the uids through the token's audit_ref.
        joined = await self.query(
            "SELECT a.action, t.issued_by_uid, t.issued_by_sudo_uid "
            "FROM audit_events a JOIN setup_tokens t ON t.audit_ref = a.resource_id "
            "WHERE a.action = 'owner.setup_token.issue' AND a.recorded_at >= :since",
            since=self.started_at,
        )
        self.assertEqual(
            [tuple(r) for r in joined], [("owner.setup_token.issue", 1000, 1001)]
        )

    async def test_nothing_is_recorded_where_the_process_has_no_uid(self):
        with patch("os.geteuid", side_effect=AttributeError):
            issued = await self.operator.setup_owner("boss")

        self.assertIsNone(issued.operator)

        self.assertEqual(
            await self.query(
                "SELECT issued_by_uid, issued_by_sudo_uid FROM setup_tokens"
            ),
            [(None, None)],
        )

    def test_the_identity_is_read_from_the_process_and_sudo(self):
        with running_as(1234):
            self.assertEqual(
                OperatorIdentity.from_environment({"SUDO_UID": "1001"}),
                OperatorIdentity(1234, 1001),
            )
            self.assertEqual(
                OperatorIdentity.from_environment({}), OperatorIdentity(1234, None)
            )
            with patch.dict(os.environ, {"SUDO_UID": "77"}):
                self.assertEqual(
                    OperatorIdentity.from_environment(), OperatorIdentity(1234, 77)
                )

    def test_a_malformed_sudo_uid_is_ignored_not_trusted(self):
        for raw in (
            "",
            "abc",
            "-1",
            "1e3",
            " 1001",
            "１００１",
            "4294967296",
            "9" * 30,
        ):
            with self.subTest(raw):
                self.assertIsNone(
                    OperatorIdentity.from_environment({"SUDO_UID": raw}).sudo_uid
                )
        self.assertEqual(
            OperatorIdentity.from_environment({"SUDO_UID": "4294967295"}).sudo_uid,
            4_294_967_295,
        )

    def test_identities_are_validated(self):
        for uid, sudo in (
            (-1, None),
            (2**32, None),
            (True, None),
            ("0", None),
            (0, -1),
            (0, 2**32),
            (0, False),
        ):
            with self.subTest(uid=uid, sudo=sudo):
                with self.assertRaises(ValueError):
                    OperatorIdentity(uid, sudo)
        OperatorIdentity(0)
        OperatorIdentity(4_294_967_295, 4_294_967_295)


class ServiceConstructionTest(unittest.TestCase):
    """The arguments of both services are checked when they are built."""

    def build(self, kind, **options):
        from .support import FakeDatabase

        class Sink:
            async def record(self, event):
                return None

        return kind(FakeDatabase(), options.pop("sink", Sink()), **options)

    def test_the_ttl_is_bounded_for_the_operator(self):
        for ttl in (59, limits.MAX_TTL_SECONDS + 1, True, 60.5, "60"):
            with self.subTest(ttl):
                with self.assertRaises(ValueError):
                    self.build(OwnerOperator, ttl_seconds=ttl)
        self.build(OwnerOperator, ttl_seconds=60)
        self.build(OwnerOperator, ttl_seconds=14_400)

    def test_the_ttl_is_capped_at_four_hours(self):
        self.assertEqual(limits.MAX_TTL_SECONDS, 4 * 60 * 60)

    def test_the_attempt_limit_is_bounded_for_the_redeemer(self):
        for attempts in (0, 21, False, 2.5):
            with self.subTest(attempts):
                with self.assertRaises(ValueError):
                    self.build(TokenRedeemer, max_attempts=attempts)
        self.build(TokenRedeemer, max_attempts=1)
        self.build(TokenRedeemer, max_attempts=20)

    def test_the_audit_timeout_is_bounded(self):
        for kind in (OwnerOperator, TokenRedeemer):
            for timeout in (0, 61, "3"):
                with self.subTest(kind=kind.__name__, timeout=timeout):
                    with self.assertRaises(ValueError):
                        self.build(kind, audit_timeout_seconds=timeout)

    def test_an_audit_sink_that_cannot_work_is_refused_up_front(self):
        class NotAsync:
            def record(self, event):
                return None

        class WrongSignature:
            async def record(self):
                return None

        for kind in (OwnerOperator, TokenRedeemer):
            for sink in (object(), NotAsync(), WrongSignature(), None):
                with self.subTest(kind=kind.__name__, sink=type(sink).__name__):
                    with self.assertRaises(TypeError):
                        self.build(kind, sink=sink)

    def test_a_clock_must_be_callable_and_timezone_aware(self):
        for kind in (OwnerOperator, TokenRedeemer):
            with self.subTest(kind.__name__):
                with self.assertRaises(TypeError):
                    self.build(kind, clock="now")
                naive = self.build(kind, clock=lambda: datetime(2030, 1, 1))
                with self.assertRaises(ValueError):
                    naive._audit.now()

    def test_the_web_facing_service_can_only_redeem(self):
        public = {name for name in dir(TokenRedeemer) if not name.startswith("_")}

        self.assertEqual(public, {"from_settings", "redeem"})

    def test_the_operator_cannot_redeem(self):
        public = {name for name in dir(OwnerOperator) if not name.startswith("_")}

        self.assertEqual(public, {"from_settings", "recover_owner", "setup_owner"})

    def test_the_audit_helper_is_shared_by_both(self):
        self.assertIsInstance(self.build(TokenRedeemer)._audit, IdentityAudit)
        self.assertIsInstance(self.build(OwnerOperator)._audit, IdentityAudit)


if __name__ == "__main__":
    logging.basicConfig()
    unittest.main()
