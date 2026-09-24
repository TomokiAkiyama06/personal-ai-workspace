"""Initial Owner setup, recovery and token redemption on a real PostgreSQL.

Skipped unless ``PAW_TEST_DATABASE_URL`` is set. Each class migrates the test
database to ``head`` and back to ``base``; each test starts without users and
looks only at the audit rows it caused.
"""

import hmac
import logging
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
    IssuedToken,
    LoginNameTakenError,
    OwnerAlreadyExistsError,
    OwnerNotFoundError,
    Redemption,
    SetupTokenRejectedError,
    TokenPurpose,
    UserStatus,
    tokens,
)
from paw_backend.identity import service as service_module

from .identity_support import (
    SECRET_DETAIL,
    T0,
    FailingSink,
    FlakySink,
    LogCapture,
    PostgresIdentityTestCase,
    requires_postgres,
    secret_of,
)

TTL = 1800


def wrong_secret_for(token: str) -> str:
    """A well-formed token for the same id with another secret."""
    prefix, token_id, _ = token.split(".")
    return f"{prefix}.{token_id}.{'A' * 43}"


@requires_postgres
class SetupOwnerTest(PostgresIdentityTestCase):
    async def test_creates_the_owner_and_a_one_time_setup_token(self):
        issued = await self.service.setup_owner("  Tomoki ")

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
                row.id,
                row.user_id,
                row.purpose,
                row.created_at,
                row.expires_at,
                row.used_at,
                row.revoked_at,
                row.attempts,
            ),
            (
                issued.token_id,
                issued.user_id,
                "setup",
                T0,
                T0 + timedelta(seconds=TTL),
                None,
                None,
                0,
            ),
        )

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
        await self.service.setup_owner("boss")

        self.assertEqual(
            await self.scalar("SELECT system_role FROM users"), SystemRole.OWNER.value
        )

    async def test_the_ttl_is_configurable(self):
        service = self.make_service(ttl_seconds=90)

        issued = await service.setup_owner("boss")

        self.assertEqual(issued.expires_at, T0 + timedelta(seconds=90))
        self.assertEqual(
            await self.scalar("SELECT expires_at FROM setup_tokens"),
            T0 + timedelta(seconds=90),
        )

    async def test_only_a_salted_hmac_of_the_secret_is_stored(self):
        issued = await self.service.setup_owner("boss")

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
        issued = await self.service.setup_owner("boss")

        self.assertNotIn(secret_of(issued.token), repr(issued))
        self.assertNotIn(secret_of(issued.token), str(issued))

    async def test_the_owner_passkey_requirement_is_set_and_cannot_be_cleared(self):
        await self.service.setup_owner("boss")

        self.assertTrue(await self.scalar("SELECT passkey_required FROM users"))
        with self.assertRaises(DBAPIError):
            await self.execute("UPDATE users SET passkey_required = false")

    async def test_a_second_setup_is_refused_and_creates_nothing(self):
        first = await self.service.setup_owner("boss")

        with self.assertRaises(OwnerAlreadyExistsError):
            await self.service.setup_owner("someone-else")

        self.assertEqual(await self.owner_count(), 1)
        self.assertEqual(await self.scalar("SELECT count(*) FROM users"), 1)
        self.assertEqual(await self.scalar("SELECT count(*) FROM setup_tokens"), 1)
        self.assertEqual(await self.scalar("SELECT id FROM users"), first.user_id)

    async def test_setup_stays_closed_after_the_owner_finished_setup(self):
        issued = await self.service.setup_owner("boss")
        await self.service.redeem(issued.token)

        with self.assertRaises(OwnerAlreadyExistsError):
            await self.service.setup_owner("another")

    async def test_a_login_name_used_by_another_user_is_refused(self):
        await self.execute(
            "INSERT INTO users (id, login_name, system_role, status, "
            "passkey_required, created_at, updated_at) VALUES "
            "(:id, 'boss', 'admin', 'active', true, :now, :now)",
            id=uuid.uuid4(),
            now=T0,
        )

        with self.assertRaises(LoginNameTakenError):
            await self.service.setup_owner("BOSS")

        self.assertEqual(await self.owner_count(), 0)
        self.assertEqual(
            await self.audit_summary(),
            Counter({("owner.create", "deny", "login_name_taken"): 1}),
        )

    async def test_an_invalid_login_name_is_refused_before_anything_is_written(self):
        for name in ("no", "has space", "", None, 7):
            with self.subTest(name):
                with self.assertRaises(InvalidLoginNameError):
                    await self.service.setup_owner(name)

        self.assertEqual(await self.scalar("SELECT count(*) FROM users"), 0)
        self.assertEqual(await self.audit_summary(), Counter())

    async def test_setup_is_audited_with_ids_and_enums_only(self):
        issued = await self.service.setup_owner("Tomoki")

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
            ("setup_token", issued.token_id),
        )
        for row in rows:
            self.assertEqual((row.actor_id, row.actor_role), (None, "system"))
            self.assertEqual(row.occurred_at, T0)
        self.assertEqual(create.correlation_id, issue.correlation_id)
        dump = "\n".join(str(tuple(row)) for row in rows)
        self.assertNotIn("tomoki", dump.lower())
        self.assertNotIn(secret_of(issued.token), dump)

    async def test_a_refused_setup_is_audited(self):
        await self.service.setup_owner("boss")

        with self.assertRaises(OwnerAlreadyExistsError):
            await self.service.setup_owner("boss2")

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
        service = self.make_service(sink=sink)

        with LogCapture() as logs:
            with self.assertRaises(AuditUnavailableError):
                await service.setup_owner("boss")

        self.assertEqual(sink.attempts, 1)
        self.assertEqual(await self.scalar("SELECT count(*) FROM users"), 0)
        self.assertEqual(await self.scalar("SELECT count(*) FROM setup_tokens"), 0)
        self.assertIn("RuntimeError", logs.text)
        self.assertNotIn(SECRET_DETAIL, logs.text)
        # Nothing was left behind: the setup can simply be run again.
        again = await self.service.setup_owner("boss")
        self.assertEqual(await self.owner_count(), 1)
        self.assertEqual(again.login_name, "boss")

    async def test_setup_rolls_back_when_the_audit_fails_after_its_first_event(self):
        service = self.make_service(sink=FlakySink(self.database, allow=1))

        with self.audit_failure_is_logged_by_type_only():
            with self.assertRaises(AuditUnavailableError):
                await service.setup_owner("boss")

        self.assertEqual(await self.scalar("SELECT count(*) FROM users"), 0)
        self.assertEqual(await self.scalar("SELECT count(*) FROM setup_tokens"), 0)


@requires_postgres
class RecoverOwnerTest(PostgresIdentityTestCase):
    async def test_there_is_nothing_to_recover_without_an_owner(self):
        with self.assertRaises(OwnerNotFoundError):
            await self.service.recover_owner()

        self.assertEqual(await self.scalar("SELECT count(*) FROM setup_tokens"), 0)
        self.assertEqual(
            await self.audit_summary(),
            Counter({("owner.recovery_token.issue", "deny", "owner_missing"): 1}),
        )

    async def test_issues_a_recovery_token_for_the_existing_owner(self):
        setup = await self.service.setup_owner("boss")
        self.clock.advance(seconds=60)

        recovery = await self.service.recover_owner()

        self.assertEqual(recovery.purpose, TokenPurpose.RECOVERY)
        self.assertEqual(
            (recovery.user_id, recovery.login_name), (setup.user_id, "boss")
        )
        self.assertNotEqual(recovery.token, setup.token)
        self.assertEqual(recovery.expires_at, T0 + timedelta(seconds=60 + TTL))
        self.assertEqual(await self.owner_count(), 1)
        row = (
            await self.query(
                "SELECT * FROM setup_tokens WHERE id = :id", id=recovery.token_id
            )
        )[0]
        self.assertEqual(
            (row.purpose, row.user_id, row.used_at, row.attempts),
            ("recovery", setup.user_id, None, 0),
        )

    async def test_outstanding_tokens_are_invalidated_and_the_revoke_is_audited(self):
        setup = await self.service.setup_owner("boss")
        self.clock.advance(seconds=60)

        recovery = await self.service.recover_owner()

        self.assertEqual(
            await self.scalar(
                "SELECT revoked_at FROM setup_tokens WHERE id = :id", id=setup.token_id
            ),
            T0 + timedelta(seconds=60),
        )
        with self.assertRaises(SetupTokenRejectedError):
            await self.service.redeem(setup.token)
        redemption = await self.service.redeem(recovery.token)
        self.assertEqual(redemption.purpose, TokenPurpose.RECOVERY)
        summary = await self.audit_summary()
        self.assertEqual(summary[("owner.token.revoke", "allow", "superseded")], 1)
        self.assertEqual(summary[("owner.recovery_token.issue", "allow", "issued")], 1)
        self.assertEqual(summary[("owner.token.redeem", "deny", "token_revoked")], 1)
        self.assertEqual(summary[("owner.token.redeem", "allow", "redeemed")], 1)

    async def test_a_second_recovery_supersedes_the_first(self):
        await self.service.setup_owner("boss")
        first = await self.service.recover_owner()
        second = await self.service.recover_owner()

        with self.assertRaises(SetupTokenRejectedError):
            await self.service.redeem(first.token)
        await self.service.redeem(second.token)
        self.assertEqual(
            await self.scalar(
                "SELECT count(*) FROM setup_tokens WHERE revoked_at IS NOT NULL"
            ),
            2,
        )

    async def test_at_most_one_token_is_outstanding(self):
        await self.service.setup_owner("boss")
        for _ in range(3):
            await self.service.recover_owner()

        self.assertEqual(
            await self.scalar(
                "SELECT count(*) FROM setup_tokens "
                "WHERE used_at IS NULL AND revoked_at IS NULL"
            ),
            1,
        )

    async def test_a_used_token_is_left_alone(self):
        setup = await self.service.setup_owner("boss")
        await self.service.redeem(setup.token)
        used_at = await self.scalar("SELECT used_at FROM setup_tokens")

        await self.service.recover_owner()

        self.assertEqual(
            await self.scalar(
                "SELECT used_at FROM setup_tokens WHERE id = :id", id=setup.token_id
            ),
            used_at,
        )
        self.assertEqual(
            (await self.audit_summary())[("owner.token.revoke", "allow", "superseded")],
            0,
        )

    async def test_an_owner_who_is_pending_deletion_cannot_be_recovered(self):
        await self.service.setup_owner("boss")
        await self.execute("UPDATE users SET status = 'pending_deletion'")

        with self.assertRaises(OwnerNotFoundError):
            await self.service.recover_owner()

    async def test_an_active_owner_can_be_recovered(self):
        setup = await self.service.setup_owner("boss")
        await self.service.redeem(setup.token)
        await self.execute("UPDATE users SET status = 'active'")

        recovery = await self.service.recover_owner()
        redemption = await self.service.redeem(recovery.token)

        self.assertEqual(
            (redemption.purpose, redemption.user_status),
            (TokenPurpose.RECOVERY, UserStatus.ACTIVE),
        )

    async def test_concurrent_recoveries_leave_exactly_one_working_token(self):
        await self.service.setup_owner("boss")

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
                await self.service.redeem(candidate.token)
                outcomes.append("redeemed")
            except SetupTokenRejectedError:
                outcomes.append("rejected")
        self.assertEqual(
            sorted(outcomes), ["redeemed", "rejected", "rejected", "rejected"]
        )

    async def test_recovery_fails_closed_when_the_audit_cannot_be_written(self):
        setup = await self.service.setup_owner("boss")
        service = self.make_service(sink=FailingSink())

        with self.audit_failure_is_logged_by_type_only():
            with self.assertRaises(AuditUnavailableError):
                await service.recover_owner()

        # Nothing changed: the old token is still the outstanding one.
        self.assertEqual(await self.scalar("SELECT count(*) FROM setup_tokens"), 1)
        self.assertIsNone(await self.scalar("SELECT revoked_at FROM setup_tokens"))
        await self.service.redeem(setup.token)

    async def test_a_refusal_stands_even_when_its_audit_cannot_be_written(self):
        broken = self.make_service(sink=FailingSink())

        with self.audit_failure_is_logged_by_type_only():
            with self.assertRaises(OwnerNotFoundError):  # not AuditUnavailableError
                await broken.recover_owner()
            await self.service.setup_owner("boss")
            with self.assertRaises(OwnerAlreadyExistsError):
                await broken.setup_owner("someone")


@requires_postgres
class RedeemTest(PostgresIdentityTestCase):
    async def test_a_setup_token_redeems_once(self):
        issued = await self.service.setup_owner("boss")
        self.clock.advance(seconds=30)

        redemption = await self.service.redeem(issued.token)

        self.assertEqual(
            redemption,
            Redemption(
                user_id=issued.user_id,
                token_id=issued.token_id,
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
            await self.service.redeem(issued.token)

    async def test_redemption_creates_no_user_and_no_session(self):
        issued = await self.service.setup_owner("boss")

        await self.service.redeem(issued.token)

        self.assertEqual(await self.scalar("SELECT count(*) FROM users"), 1)
        self.assertEqual(await self.owner_count(), 1)
        # The status is the web flow's to move once credentials exist (PAW-022).
        self.assertEqual(await self.scalar("SELECT status FROM users"), "invited")

    async def test_redemption_is_audited_as_the_owner(self):
        issued = await self.service.setup_owner("boss")

        await self.service.redeem(issued.token)

        (row,) = [
            r for r in await self.audit_rows() if r.action == "owner.token.redeem"
        ]
        self.assertEqual(
            (row.decision, row.reason, row.actor_id, row.actor_role),
            ("allow", "redeemed", issued.user_id, "owner"),
        )
        self.assertEqual(
            (row.resource_kind, row.resource_id), ("setup_token", issued.token_id)
        )

    async def test_the_expiry_boundary(self):
        issued = await self.service.setup_owner("boss")
        self.clock.advance(seconds=TTL - 1)
        early = await self.service.redeem(issued.token)
        self.assertEqual(early.token_id, issued.token_id)

        second = await self.service.recover_owner()
        self.clock.advance(seconds=TTL)  # exactly the expiry instant
        with self.assertRaises(SetupTokenRejectedError):
            await self.service.redeem(second.token)
        self.assertEqual(
            (await self.audit_summary())[
                ("owner.token.redeem", "deny", "token_expired")
            ],
            1,
        )

    async def test_an_expired_token_is_rejected_and_stays_unused(self):
        issued = await self.service.setup_owner("boss")
        self.clock.advance(hours=3)

        with self.assertRaises(SetupTokenRejectedError):
            await self.service.redeem(issued.token)

        self.assertIsNone(await self.scalar("SELECT used_at FROM setup_tokens"))

    async def test_a_wrong_secret_is_rejected_and_counted(self):
        issued = await self.service.setup_owner("boss")

        with self.assertRaises(SetupTokenRejectedError):
            await self.service.redeem(wrong_secret_for(issued.token))

        self.assertEqual(await self.scalar("SELECT attempts FROM setup_tokens"), 1)
        self.assertIsNone(await self.scalar("SELECT used_at FROM setup_tokens"))
        self.assertEqual(
            (await self.audit_summary())[
                ("owner.token.redeem", "deny", "token_mismatch")
            ],
            1,
        )
        # The right token still works while attempts remain.
        await self.service.redeem(issued.token)
        self.assertEqual(await self.scalar("SELECT attempts FROM setup_tokens"), 2)

    async def test_unknown_and_malformed_tokens_are_rejected_without_any_write(self):
        await self.service.setup_owner("boss")
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
                    await self.service.redeem(value)

        self.assertEqual(await self.audit_summary(), before)
        self.assertEqual(await self.scalar("SELECT attempts FROM setup_tokens"), 0)

    async def test_lockout_after_the_maximum_number_of_attempts(self):
        service = self.make_service(max_attempts=3)
        issued = await service.setup_owner("boss")
        wrong = wrong_secret_for(issued.token)

        for _ in range(3):
            with self.assertRaises(SetupTokenRejectedError):
                await service.redeem(wrong)
        # Locked out for good: even the correct token no longer works ...
        with self.assertRaises(SetupTokenRejectedError):
            await service.redeem(issued.token)

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
                await service.redeem(wrong)
        self.assertEqual(await self.audit_summary(), before)
        self.assertEqual(await self.scalar("SELECT attempts FROM setup_tokens"), 3)
        # A new recovery token is the way out.
        recovery = await service.recover_owner()
        await service.redeem(recovery.token)

    async def test_the_last_permitted_attempt_can_still_succeed(self):
        service = self.make_service(max_attempts=3)
        issued = await service.setup_owner("boss")
        for _ in range(2):
            with self.assertRaises(SetupTokenRejectedError):
                await service.redeem(wrong_secret_for(issued.token))

        await service.redeem(issued.token)

        summary = await self.audit_summary()
        self.assertEqual(
            summary[("owner.token.redeem", "deny", "attempts_exhausted")], 0
        )

    async def test_concurrent_wrong_guesses_never_exceed_the_maximum(self):
        issued = await self.service.setup_owner("boss")
        wrong = wrong_secret_for(issued.token)

        results = await self.gather_on_own_engines(
            10, lambda service, index: service.redeem(wrong), max_attempts=3
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
        issued = await self.service.setup_owner("boss")

        results = await self.gather_on_own_engines(
            6, lambda service, index: service.redeem(issued.token), max_attempts=20
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
                await (service or self.service).redeem(token)
            except SetupTokenRejectedError as error:
                outcomes[name] = error
            else:
                self.fail(f"{name}: the token was accepted")

        setup = await self.service.setup_owner("boss")
        await rejection("wrong secret", wrong_secret_for(setup.token))
        await rejection("unknown id", tokens.generate().token)
        await rejection("malformed", "not-a-token")
        await rejection("not a string", None)
        await self.service.redeem(setup.token)
        await rejection("used", setup.token)
        revoked = await self.service.recover_owner()
        expired = await self.service.recover_owner()
        await rejection("revoked", revoked.token)
        self.clock.advance(hours=3)
        await rejection("expired", expired.token)
        locked = await self.service.recover_owner()
        strict = self.make_service(max_attempts=2)
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
        setup = await self.service.setup_owner("boss")
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
                        await self.service.redeem(token)
                self.assertEqual(compare.call_count, 1)
                reservations = [
                    s
                    for s in statements
                    if s.startswith("UPDATE setup_tokens SET attempts")
                ]
                self.assertEqual(len(reservations), 1)

    async def test_the_web_flow_can_finish_its_part_in_the_same_transaction(self):
        issued = await self.service.setup_owner("boss")
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

        redemption = await self.service.redeem(issued.token, apply=apply)

        self.assertEqual(seen, [redemption, T0])
        self.assertEqual(await self.scalar("SELECT status FROM users"), "active")
        self.assertEqual(await self.scalar("SELECT used_at FROM setup_tokens"), T0)

    async def test_a_failing_hook_rolls_the_redemption_back(self):
        issued = await self.service.setup_owner("boss")

        async def apply(session, redemption):
            await session.execute(text("UPDATE users SET status = 'active'"))
            raise ZeroDivisionError

        with self.assertRaises(ZeroDivisionError):
            await self.service.redeem(issued.token, apply=apply)

        self.assertEqual(await self.scalar("SELECT status FROM users"), "invited")
        self.assertIsNone(await self.scalar("SELECT used_at FROM setup_tokens"))
        self.assertEqual(
            (await self.audit_summary())[("owner.token.redeem", "allow", "redeemed")], 0
        )
        # The token was not burned: the flow can try again.
        await self.service.redeem(issued.token)

    async def test_the_hook_must_be_callable(self):
        with self.assertRaises(TypeError):
            await self.service.redeem("x", apply="not callable")

    async def test_redemption_fails_closed_when_the_audit_cannot_be_written(self):
        issued = await self.service.setup_owner("boss")
        service = self.make_service(sink=FailingSink())

        with self.audit_failure_is_logged_by_type_only():
            with self.assertRaises(AuditUnavailableError):
                await service.redeem(issued.token)

        self.assertIsNone(await self.scalar("SELECT used_at FROM setup_tokens"))

    async def test_a_rejection_stays_a_rejection_when_the_audit_is_down(self):
        issued = await self.service.setup_owner("boss")
        service = self.make_service(sink=FailingSink())

        with self.audit_failure_is_logged_by_type_only():
            with self.assertRaises(SetupTokenRejectedError):
                await service.redeem(wrong_secret_for(issued.token))

    async def test_a_token_of_a_user_who_is_no_longer_the_owner_is_rejected(self):
        issued = await self.service.setup_owner("boss")
        # As after an ownership transfer: the former Owner is an Admin.
        await self.execute("UPDATE users SET system_role = 'admin'")

        with self.assertRaises(SetupTokenRejectedError):
            await self.service.redeem(issued.token)

        self.assertIsNone(await self.scalar("SELECT used_at FROM setup_tokens"))
        self.assertEqual(
            (await self.audit_summary())[
                ("owner.token.redeem", "deny", "user_not_eligible")
            ],
            1,
        )

    async def test_a_token_of_a_user_who_is_pending_deletion_is_rejected(self):
        issued = await self.service.setup_owner("boss")
        await self.execute("UPDATE users SET status = 'pending_deletion'")

        with self.assertRaises(SetupTokenRejectedError):
            await self.service.redeem(issued.token)

        self.assertIsNone(await self.scalar("SELECT used_at FROM setup_tokens"))

    async def test_a_token_survives_a_restart(self):
        issued = await self.service.setup_owner("boss")

        other_process = self.make_service(self.new_database())

        redemption = await other_process.redeem(issued.token)
        self.assertEqual(redemption.user_id, issued.user_id)


@requires_postgres
class NothingSecretIsLeakedTest(PostgresIdentityTestCase):
    async def test_no_plaintext_token_reaches_a_log_a_row_or_an_audit_event(self):
        tokens_seen: list[str] = []
        with LogCapture() as logs:
            setup = await self.service.setup_owner("boss")
            tokens_seen.append(setup.token)
            with self.assertRaises(SetupTokenRejectedError):
                await self.service.redeem(wrong_secret_for(setup.token))
            await self.service.redeem(setup.token)
            with self.assertRaises(SetupTokenRejectedError):
                await self.service.redeem(setup.token)
            recovery = await self.service.recover_owner()
            tokens_seen.append(recovery.token)
            self.clock.advance(hours=3)
            with self.assertRaises(SetupTokenRejectedError):
                await self.service.redeem(recovery.token)
            with self.assertRaises(OwnerAlreadyExistsError):
                await self.service.setup_owner("again")

        # The capture is not vacuous: SQL and its parameters were logged.
        self.assertIn("INSERT INTO setup_tokens", logs.text)
        self.assertIn("UPDATE setup_tokens SET attempts", logs.text)
        stored = await self.everything_stored()
        for token in tokens_seen:
            for secret in (token, secret_of(token)):
                self.assertNotIn(secret, logs.text)
                self.assertNotIn(secret, stored)
        self.assertNotIn("A" * 43, logs.text)


class ServiceConstructionTest(unittest.TestCase):
    def build(self, **options):
        from .support import FakeDatabase

        class Sink:
            async def record(self, event):
                return None

        return service_module.OwnerSetupService(
            FakeDatabase(), options.pop("sink", Sink()), **options
        )

    def test_the_ttl_and_the_attempt_limit_are_bounded(self):
        for options in (
            {"ttl_seconds": 59},
            {"ttl_seconds": 86_401},
            {"ttl_seconds": True},
            {"ttl_seconds": 60.5},
            {"max_attempts": 0},
            {"max_attempts": 21},
            {"max_attempts": False},
            {"audit_timeout_seconds": 0},
            {"audit_timeout_seconds": 61},
        ):
            with self.subTest(options):
                with self.assertRaises(ValueError):
                    self.build(**options)
        self.build(ttl_seconds=60, max_attempts=1)
        self.build(ttl_seconds=86_400, max_attempts=20)

    def test_an_audit_sink_that_cannot_work_is_refused_up_front(self):
        class NotAsync:
            def record(self, event):
                return None

        class WrongSignature:
            async def record(self):
                return None

        for sink in (object(), NotAsync(), WrongSignature(), None):
            with self.subTest(type(sink).__name__):
                with self.assertRaises(TypeError):
                    self.build(sink=sink)

    def test_a_clock_must_be_callable_and_timezone_aware(self):
        with self.assertRaises(TypeError):
            self.build(clock="now")
        naive = self.build(clock=lambda: datetime(2030, 1, 1))
        with self.assertRaises(ValueError):
            naive._now()


if __name__ == "__main__":
    logging.basicConfig()
    unittest.main()
