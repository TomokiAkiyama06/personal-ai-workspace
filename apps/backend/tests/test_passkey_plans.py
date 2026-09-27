"""The statements the Passkey code runs can use their indexes (PostgreSQL).

Same method as ``test_auth_plans``: the statements are the ones the code really sends
(captured from the driver), each planned as a prepared statement in
``force_generic_plan`` mode with sequential and bitmap scans switched off, so the test
answers "can the index serve this statement" and does not depend on the statistics of
a small table. A partial index is only usable by a generic plan when its condition is
written into the statement, which is what these tests hold the code to.
"""

import json
import uuid

from paw_backend.auth.models import PasskeyGate

from . import test_auth_plans
from .auth_support import requires_postgres
from .passkey_pg_support import PasskeyTestCase
from .test_auth_plans import nodes


@requires_postgres
class PlanTest(PasskeyTestCase):
    captured = test_auth_plans.PlanTest.captured
    plan_of = test_auth_plans.PlanTest.plan_of
    statements = test_auth_plans.PlanTest.statements
    indexes_used = test_auth_plans.PlanTest.indexes_used

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.alice = await self.make_user("alice")
        self.session, self.authenticator = await self.fully_stepped_up(self.alice)

    async def plan_for(self, captured, marker: str, count: int = 1) -> dict:
        found = self.statements(captured, marker)
        self.assertEqual(len(found), count, [s[:80] for s, _ in captured])
        sql, params = found[0]
        return await self.plan_of(sql, params)

    def assertUses(self, plan: dict, *names: str) -> None:
        self.assertTrue(
            self.indexes_used(plan) & set(names),
            f"{sorted(names)} not in {json.dumps(plan)}",
        )

    async def test_the_count_of_a_users_passkeys_uses_the_partial_index(self):
        with self.captured() as captured:
            async with self.service_database.session() as session:
                await self.registry.count_active_in(session, self.alice.id)
                await session.commit()
        plan = await self.plan_for(captured, "SELECT count(*) FROM user_passkeys")
        self.assertUses(plan, "ix_user_passkeys_user_id_active")

    async def test_the_list_and_the_ceremony_options_use_the_partial_index(self):
        with self.captured() as captured:
            async with self.service_database.session() as session:
                await self.registry.list_active_in(session, self.alice.id)
                await self.registry.active_credential_ids_in(session, self.alice.id)
                await session.commit()
        for marker in ("SELECT id, name, created_at", "SELECT credential_id FROM"):
            with self.subTest(marker):
                plan = await self.plan_for(captured, marker)
                self.assertUses(plan, "ix_user_passkeys_user_id_active")

    async def test_a_credential_is_found_by_its_id_through_the_unique_index(self):
        credential = self.authenticator.credentials[0].credential_id
        with self.captured() as captured:
            async with self.service_database.session() as session:
                await self.registry.stored_in(session, self.alice.id, credential)
                await self.registry.confirm_in(session, self.alice.id, credential)
                await session.commit()
        for marker in ("SELECT id, public_key, sign_count", "FOR SHARE"):
            with self.subTest(marker):
                plan = await self.plan_for(captured, marker)
                self.assertUses(
                    plan,
                    "uq_user_passkeys_credential_id",
                    "ix_user_passkeys_user_id_active",
                )

    async def test_the_counter_update_and_the_revocation_use_an_index(self):
        passkey = (await self.passkey_rows(self.alice.id))[0].id
        from paw_backend.auth.passkeys.ceremony import VerifiedAssertion
        from paw_backend.auth.passkeys.models import PasskeyRevokeReason

        with self.captured() as captured:
            async with self.service_database.session() as session:
                await self.registry.record_use_in(
                    session,
                    passkey_id=passkey,
                    user_id=self.alice.id,
                    assertion=VerifiedAssertion(9, False, False),
                )
                await self.registry.revoke_in(
                    session,
                    passkey_id=passkey,
                    user_id=self.alice.id,
                    reason=PasskeyRevokeReason.REVOKED_BY_USER,
                )
                await session.commit()
        for marker in ("SET sign_count", "SET revoked_at"):
            with self.subTest(marker):
                plan = await self.plan_for(captured, marker)
                # (the key, or the partial index of the user's active credentials)
                self.assertUses(
                    plan, "pk_user_passkeys", "ix_user_passkeys_user_id_active"
                )

    async def test_a_challenge_is_consumed_and_replaced_through_its_unique_index(self):
        from paw_backend.auth.passkeys.models import PasskeyPurpose

        with self.captured() as captured:
            async with self.service_database.session() as session:
                await self.registry.issue_challenge_in(
                    session,
                    user_id=self.alice.id,
                    session_id=self.session.record.id,
                    purpose=PasskeyPurpose.AUTHENTICATE,
                    challenge=b"\x01" * 32,
                )
                await self.registry.consume_challenge_in(
                    session,
                    user_id=self.alice.id,
                    session_id=self.session.record.id,
                    purpose=PasskeyPurpose.AUTHENTICATE,
                )
                await session.commit()
        upsert = await self.plan_for(captured, "INSERT INTO passkey_challenges")
        arbiters = [
            node.get("Conflict Arbiter Indexes")
            for node in nodes(upsert)
            if node.get("Conflict Arbiter Indexes")
        ]
        self.assertEqual(arbiters, [["uq_passkey_challenges_session_id"]])
        deletes = self.statements(captured, "DELETE FROM passkey_challenges")
        self.assertEqual(len(deletes), 2)
        used = [
            self.indexes_used(await self.plan_of(sql, params))
            for sql, params in deletes
        ]
        # The purge of the long expired (by time) and the consumption (by session).
        self.assertIn("ix_passkey_challenges_expires_at", used[0])
        self.assertIn("uq_passkey_challenges_session_id", used[1])

    async def test_the_sessions_a_passkey_opened_are_found_by_a_partial_index(self):
        passkey = (await self.passkey_rows(self.alice.id))[0].id
        with self.captured() as captured:
            async with self.service_database.session() as session:
                await self.services.sessions.revoke_bound_to_passkey(session, passkey)
                await session.commit()
        plan = await self.plan_for(captured, "WHERE s.passkey_id = ")
        self.assertUses(plan, "ix_auth_sessions_passkey_id_active")

    async def test_the_step_ups_of_a_user_are_cleared_through_the_active_index(self):
        with self.captured() as captured:
            async with self.service_database.session() as session:
                await self.services.sessions.clear_passkey_step_ups(
                    session, self.alice.id
                )
                await session.commit()
        plan = await self.plan_for(captured, "SET stepup_at = NULL")
        self.assertUses(plan, "ix_auth_sessions_user_id_active")

    async def test_the_strong_approvals_question_uses_the_active_index(self):
        with self.captured() as captured:
            await self.services.approval_step_up.verify(self.alice.id, uuid.uuid4())
        plan = await self.plan_for(captured, "SELECT EXISTS")
        self.assertUses(plan, "ix_auth_sessions_user_id_active")

    async def test_the_freshness_of_a_session_is_read_through_an_index(self):
        from paw_backend.auth.stepup import read_freshness_in

        with self.captured() as captured:
            async with self.service_database.session() as session:
                freshness = await read_freshness_in(
                    session,
                    session_id=self.session.record.id,
                    user_id=self.alice.id,
                    window_minutes=30,
                    now=self.clock.now,
                )
                await session.commit()
        self.assertIs(freshness.gate, PasskeyGate.OPEN)
        for marker in ("FOR SHARE", "SELECT CASE WHEN"):
            with self.subTest(marker):
                plan = await self.plan_for(captured, marker)
                self.assertUses(
                    plan, "pk_auth_sessions", "ix_auth_sessions_user_id_active"
                )
