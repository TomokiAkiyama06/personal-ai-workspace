"""The connections of the workspace: created, replaced, disabled, removed, checked."""

import asyncio
import dataclasses
import logging
import unittest
import uuid

from paw_backend.authz import Principal, SystemRole
from paw_backend.authz.policy import Reason
from paw_backend.connections import (
    AdapterFailure,
    ConnectionExistsError,
    ConnectionKind,
    ConnectionNotFoundError,
    ConnectionPermissionDeniedError,
    ConnectionStatus,
    FailureCode,
    InvalidConnectionInputError,
)

from .connections_fakes import CANARY, handle
from .connections_support import (
    FailingSink,
    PostgresConnectionTestCase,
    requires_postgres,
)

CODEX, CLAUDE = ConnectionKind.CODEX, ConnectionKind.CLAUDE


@requires_postgres
class ConnectTest(PostgresConnectionTestCase):
    async def test_an_admin_creates_the_connection_from_a_handle(self):
        info = await self.service.connect(self.principal(self.admin), CODEX, handle(1))

        self.assertEqual(info.kind, CODEX)
        # Not verified yet: it is not usable until a health check says connected.
        self.assertEqual(info.status, ConnectionStatus.UNAVAILABLE)
        self.assertTrue(info.enabled)
        self.assertFalse(info.available)
        self.assertIsNone(info.checked_at)
        self.assertEqual(info.created_at, info.updated_at)
        row = self.connection_row(CODEX)
        self.assertEqual(row["id"], info.id)
        self.assertEqual(row["secret_handle"], handle(1))
        self.assertEqual((row["status"], row["enabled"]), ("unavailable", True))

    async def test_an_owner_may_connect_too(self):
        info = await self.service.connect(self.principal(self.owner), CLAUDE, handle(2))
        self.assertEqual(info.kind, CLAUDE)

    async def test_both_kinds_can_exist_side_by_side(self):
        await self.service.connect(self.principal(self.admin), CODEX, handle(1))
        await self.service.connect(self.principal(self.admin), CLAUDE, handle(2))
        listed = await self.service.list_connections(self.principal(self.admin))
        self.assertEqual([info.kind for info in listed], [CODEX, CLAUDE])

    async def test_connecting_twice_is_refused_and_changes_nothing(self):
        await self.service.connect(self.principal(self.admin), CODEX, handle(1))
        with self.assertRaises(ConnectionExistsError):
            await self.service.connect(self.principal(self.admin), CODEX, handle(2))
        self.assertEqual(self.connection_row(CODEX)["secret_handle"], handle(1))

    async def test_the_decision_and_the_outcome_are_audited_without_the_handle(self):
        info = await self.service.connect(self.principal(self.admin), CODEX, handle(1))

        self.assertEqual(
            self.audit_actions(),
            [
                ("admin.config.manage", "allow", "granted_by_system_role"),
                ("connection.connect", "allow", "succeeded"),
            ],
        )
        outcome = self.sink.events[1]
        self.assertEqual(outcome.actor_id, self.admin)
        self.assertEqual(outcome.actor_role, "admin")
        self.assertEqual(outcome.resource_kind, "connection")
        self.assertEqual(outcome.resource_id, info.id)
        self.assertEqual(outcome.correlation_id, self.sink.events[0].correlation_id)
        for event in self.sink.events:
            self.assertNotIn(handle(1), repr(event))

    async def test_a_value_that_is_not_a_handle_is_refused_and_nothing_is_stored(self):
        plaintext = "sk-" + "ant-" + "q" * 40
        for value in (plaintext, CANARY, "", None):
            with self.subTest(value=repr(value)[:10]):
                with self.assertRaises(InvalidConnectionInputError):
                    await self.service.connect(self.principal(self.admin), CODEX, value)
        self.assertIsNone(self.connection_row(CODEX))
        self.assertEqual(self.sink.events, [])

    async def test_the_returned_info_holds_no_credential_and_no_handle(self):
        info = await self.service.connect(self.principal(self.admin), CODEX, handle(1))
        self.assertEqual(
            {field.name for field in dataclasses.fields(info)},
            {
                "id",
                "kind",
                "status",
                "enabled",
                "checked_at",
                "created_at",
                "updated_at",
            },
        )
        self.assertNotIn(handle(1), repr(info))
        self.assertNotIn("cred_", repr(info))


@requires_postgres
class AuthorizationMatrixTest(PostgresConnectionTestCase):
    """Who may do what, for every administrative method (default deny)."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.other = self.seed_user()
        self.seed_connection(CODEX, status="connected")

    def calls(self):
        """Every administrative call, as ``(name, capability, callable)``."""
        subject = self.other
        return [
            (
                "connect",
                "admin.config.manage",
                lambda p: self.service.connect(p, CLAUDE, handle(2)),
            ),
            (
                "replace",
                "admin.config.manage",
                lambda p: self.service.replace_credential(p, CODEX, handle(2)),
            ),
            (
                "disable",
                "admin.config.manage",
                lambda p: self.service.disable(p, CODEX),
            ),
            ("enable", "admin.config.manage", lambda p: self.service.enable(p, CODEX)),
            (
                "get",
                "admin.config.manage",
                lambda p: self.service.get_connection(p, CODEX),
            ),
            ("list", "admin.config.manage", lambda p: self.service.list_connections(p)),
            (
                "set_quota",
                "admin.quota.manage",
                lambda p: self.service.set_quota(
                    p, subject, CODEX, "requests", "day", 5
                ),
            ),
            (
                "remove_quota",
                "admin.quota.manage",
                lambda p: self.service.remove_quota(
                    p, subject, CODEX, "requests", "day"
                ),
            ),
            (
                "view another user's quotas",
                "admin.usage.view",
                lambda p: self.service.quota_status(p, subject),
            ),
            (
                "view another user's usage",
                "admin.usage.view",
                lambda p: self.service.list_usage(p, subject),
            ),
            (
                "disconnect",
                "admin.config.manage",
                lambda p: self.service.disconnect(p, CODEX),
            ),
        ]

    async def test_owner_and_admin_may_do_everything_and_user_and_system_nothing(self):
        for name, capability, call in self.calls():
            for role, actor, allowed in (
                ("owner", self.principal(self.owner), True),
                ("admin", self.principal(self.admin), True),
                ("user", self.principal(self.user), False),
                ("system", Principal(uuid.uuid4(), SystemRole.SYSTEM), False),
            ):
                with self.subTest(call=name, role=role):
                    self.sink.events.clear()
                    if allowed:
                        try:
                            await call(actor)
                        except (ConnectionNotFoundError, ConnectionExistsError):
                            pass  # allowed; the earlier calls changed the state
                        self.assertEqual(
                            self.audit_actions()[0][:2], (capability, "allow")
                        )
                    else:
                        with self.assertRaises(
                            ConnectionPermissionDeniedError
                        ) as caught:
                            await call(actor)
                        self.assertEqual(
                            caught.exception.reason, Reason.CAPABILITY_NOT_GRANTED
                        )
                        # The denial is audited by the Authorizer, and nothing else is.
                        self.assertEqual(
                            self.audit_actions(),
                            [(capability, "deny", "capability_not_granted")],
                        )
                if name == "disconnect" and allowed:
                    self.seed_connection_if_missing()

    def seed_connection_if_missing(self):
        if self.connection_row(CODEX) is None:
            self.seed_connection(CODEX, status="connected")

    async def test_a_denied_call_changes_nothing(self):
        before = self.rows("SELECT * FROM shared_connections")
        user = self.principal(self.user)
        for call in (
            lambda: self.service.connect(user, CLAUDE, handle(2)),
            lambda: self.service.replace_credential(user, CODEX, handle(2)),
            lambda: self.service.disable(user, CODEX),
            lambda: self.service.disconnect(user, CODEX),
        ):
            with self.assertRaises(ConnectionPermissionDeniedError):
                await call()
        self.assertEqual(self.rows("SELECT * FROM shared_connections"), before)

    async def test_an_agent_style_principal_without_role_is_denied(self):
        with self.assertRaises(ConnectionPermissionDeniedError):
            await self.service.disable(Principal(self.user, SystemRole.SYSTEM), CODEX)

    async def test_a_user_cannot_use_the_admin_role_of_another_user(self):
        # The principal is what the caller resolved: a user id with the User role
        # stays a User even if the same id also exists as an admin row elsewhere.
        forged = Principal(self.admin, SystemRole.USER)
        with self.assertRaises(ConnectionPermissionDeniedError):
            await self.service.disable(forged, CODEX)

    async def test_a_failing_audit_stops_an_administrative_change(self):
        service = self.new_service(authorizer_sink=FailingSink())
        with self.assertRaises(ConnectionPermissionDeniedError) as caught:
            await service.disable(self.principal(self.admin), CODEX)
        self.assertEqual(caught.exception.reason, Reason.AUDIT_UNAVAILABLE)
        self.assertTrue(self.connection_row(CODEX)["enabled"])


@requires_postgres
class ReplaceCredentialTest(PostgresConnectionTestCase):
    async def test_the_new_handle_is_stored_and_the_connection_must_be_verified_again(
        self,
    ):
        connection_id = self.seed_connection(CODEX, status="connected")
        info = await self.service.replace_credential(
            self.principal(self.admin), CODEX, handle(2)
        )

        self.assertEqual(info.id, connection_id)
        self.assertEqual(info.status, ConnectionStatus.UNAVAILABLE)
        self.assertIsNone(info.checked_at)
        self.assertGreater(info.updated_at, info.created_at)
        row = self.connection_row(CODEX)
        self.assertEqual(row["secret_handle"], handle(2))
        self.assertEqual(row["status"], "unavailable")
        self.assertEqual(
            self.own_audit(), [("connection.replace", "allow", "succeeded")]
        )

    async def test_a_missing_connection_is_not_found(self):
        with self.assertRaises(ConnectionNotFoundError):
            await self.service.replace_credential(
                self.principal(self.admin), CODEX, handle(2)
            )

    async def test_replacing_keeps_the_admin_switch(self):
        self.seed_connection(CODEX, status="connected", enabled=False)
        info = await self.service.replace_credential(
            self.principal(self.admin), CODEX, handle(2)
        )
        self.assertFalse(info.enabled)

    async def test_a_plaintext_is_refused_and_the_old_handle_stays(self):
        self.seed_connection(CODEX)
        with self.assertRaises(InvalidConnectionInputError):
            await self.service.replace_credential(
                self.principal(self.admin), CODEX, CANARY
            )
        self.assertEqual(self.connection_row(CODEX)["secret_handle"], handle(1))
        self.assertNotIn(CANARY, self.everything_stored())


@requires_postgres
class SwitchTest(PostgresConnectionTestCase):
    async def test_disable_and_enable_flip_the_switch_and_are_audited(self):
        self.seed_connection(CODEX, status="connected")
        admin = self.principal(self.admin)

        info = await self.service.disable(admin, CODEX)
        self.assertFalse(info.enabled)
        self.assertFalse(info.available)
        self.assertFalse(self.connection_row(CODEX)["enabled"])

        info = await self.service.enable(admin, CODEX)
        self.assertTrue(info.enabled)
        self.assertTrue(info.available)
        self.assertEqual(
            self.own_audit(),
            [
                ("connection.disable", "allow", "succeeded"),
                ("connection.enable", "allow", "succeeded"),
            ],
        )

    async def test_disabling_keeps_the_status_and_the_handle(self):
        self.seed_connection(CODEX, status="expired")
        await self.service.disable(self.principal(self.admin), CODEX)
        row = self.connection_row(CODEX)
        self.assertEqual((row["status"], row["secret_handle"]), ("expired", handle(1)))

    async def test_enabling_does_not_make_an_unverified_connection_available(self):
        self.seed_connection(CODEX, status="unavailable", enabled=False)
        info = await self.service.enable(self.principal(self.admin), CODEX)
        self.assertTrue(info.enabled)
        self.assertFalse(info.available)

    async def test_a_missing_connection_is_not_found(self):
        for call in (self.service.disable, self.service.enable):
            with self.assertRaises(ConnectionNotFoundError):
                await call(self.principal(self.admin), CODEX)
        self.assertEqual(self.own_audit(), [])

    async def test_a_health_check_never_re_enables_a_disabled_connection(self):
        self.seed_connection(CODEX, status="unavailable", enabled=False)
        await self.service.check_health(CODEX)
        row = self.connection_row(CODEX)
        self.assertEqual((row["status"], row["enabled"]), ("connected", False))


@requires_postgres
class DisconnectTest(PostgresConnectionTestCase):
    async def test_the_connection_is_removed_and_its_usage_history_stays(self):
        self.seed_connection(CODEX, status="connected")
        task = self.seed_task(self.user)
        self.seed_usage(self.user, task)

        await self.service.disconnect(self.principal(self.admin), CODEX)

        self.assertIsNone(self.connection_row(CODEX))
        self.assertEqual(len(self.usage_rows()), 1)
        self.assertEqual(
            self.own_audit(), [("connection.disconnect", "allow", "succeeded")]
        )

    async def test_the_outcome_names_the_removed_connection(self):
        connection_id = self.seed_connection(CODEX)
        await self.service.disconnect(self.principal(self.admin), CODEX)
        outcome = [e for e in self.sink.events if e.action == "connection.disconnect"][
            0
        ]
        self.assertEqual(outcome.resource_id, connection_id)

    async def test_a_missing_connection_is_not_found(self):
        with self.assertRaises(ConnectionNotFoundError):
            await self.service.disconnect(self.principal(self.admin), CODEX)

    async def test_the_other_kind_is_untouched(self):
        self.seed_connection(CODEX)
        self.seed_connection(CLAUDE, secret_handle=handle(2))
        await self.service.disconnect(self.principal(self.admin), CODEX)
        self.assertIsNotNone(self.connection_row(CLAUDE))

    async def test_a_new_connection_can_be_made_afterwards(self):
        self.seed_connection(CODEX)
        await self.service.disconnect(self.principal(self.admin), CODEX)
        info = await self.service.connect(self.principal(self.admin), CODEX, handle(3))
        self.assertEqual(self.connection_row(CODEX)["secret_handle"], handle(3))
        self.assertEqual(info.status, ConnectionStatus.UNAVAILABLE)


@requires_postgres
class ReadTest(PostgresConnectionTestCase):
    async def test_an_admin_reads_status_switch_and_last_check(self):
        self.seed_connection(CODEX, status="connected")
        info = await self.service.get_connection(self.principal(self.admin), CODEX)
        self.assertEqual(
            (info.kind, info.status, info.enabled),
            (CODEX, ConnectionStatus.CONNECTED, True),
        )
        self.assertTrue(info.available)
        self.assertIsNone(info.checked_at)

    async def test_a_missing_connection_is_not_found(self):
        with self.assertRaises(ConnectionNotFoundError):
            await self.service.get_connection(self.principal(self.admin), CLAUDE)

    async def test_the_list_holds_only_the_configured_connections_in_kind_order(self):
        self.assertEqual(
            await self.service.list_connections(self.principal(self.admin)), ()
        )
        self.seed_connection(CLAUDE, secret_handle=handle(2))
        listed = await self.service.list_connections(self.principal(self.admin))
        self.assertEqual([info.kind for info in listed], [CLAUDE])
        self.seed_connection(CODEX)
        listed = await self.service.list_connections(self.principal(self.admin))
        self.assertEqual([info.kind for info in listed], [CODEX, CLAUDE])

    async def test_a_read_of_the_connection_never_shows_the_handle(self):
        self.seed_connection(CODEX)
        info = await self.service.get_connection(self.principal(self.admin), CODEX)
        listed = await self.service.list_connections(self.principal(self.admin))
        self.assertNotIn("cred_", repr(info) + repr(listed))


@requires_postgres
class AvailabilityTest(PostgresConnectionTestCase):
    """What a general user sees: ``Codex: Available / Unavailable`` and nothing more."""

    async def available(self):
        result = await self.service.availability(self.principal(self.user))
        return {item.kind: item.available for item in result}

    async def test_nothing_configured_is_unavailable_for_both_kinds(self):
        self.assertEqual(await self.available(), {CODEX: False, CLAUDE: False})

    async def test_a_connected_enabled_connection_with_an_adapter_is_available(self):
        self.seed_connection(CODEX, status="connected")
        self.assertEqual(await self.available(), {CODEX: True, CLAUDE: False})

    async def test_every_other_state_is_simply_unavailable(self):
        for status, enabled in (
            ("unavailable", True),
            ("expired", True),
            ("connected", False),
            ("expired", False),
        ):
            with self.subTest(status=status, enabled=enabled):
                self.clear_connections()
                self.seed_connection(CODEX, status=status, enabled=enabled)
                self.assertEqual(await self.available(), {CODEX: False, CLAUDE: False})

    async def test_a_connection_without_an_adapter_is_unavailable(self):
        self.adapters._adapters.pop(CODEX)
        self.seed_connection(CODEX, status="connected")
        self.assertEqual(await self.available(), {CODEX: False, CLAUDE: False})

    async def test_the_answer_carries_a_kind_and_a_flag_only(self):
        self.seed_connection(CODEX, status="expired")
        (first, second) = await self.service.availability(self.principal(self.user))
        self.assertEqual(
            {field.name for field in dataclasses.fields(first)}, {"kind", "available"}
        )
        # Why it is unavailable (expired, disabled, not configured) is not said.
        self.assertEqual((first.available, second.available), (False, False))

    async def test_the_user_role_is_enough_and_the_decision_is_audited(self):
        await self.available()
        self.assertEqual(
            self.audit_actions(),
            [("agent.use", "allow", "granted_to_resource_owner")],
        )

    async def test_the_system_role_may_not_look(self):
        with self.assertRaises(ConnectionPermissionDeniedError):
            await self.service.availability(Principal(uuid.uuid4(), SystemRole.SYSTEM))


@requires_postgres
class HealthCheckTest(PostgresConnectionTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.connection_id = self.seed_connection(CODEX, status="unavailable")

    async def test_a_good_credential_is_marked_connected_with_the_check_time(self):
        status = await self.service.check_health(CODEX)

        self.assertEqual(status, ConnectionStatus.CONNECTED)
        row = self.connection_row(CODEX)
        self.assertEqual(row["status"], "connected")
        self.assertIsNotNone(row["checked_at"])
        self.assertEqual(self.resolver.resolved, [handle(1)])
        self.assertEqual(self.codex.revealed, [CANARY])  # the adapter got the value

    async def test_a_change_of_status_is_audited_without_an_actor(self):
        await self.service.check_health(CODEX)
        (event,) = [e for e in self.sink.events if e.action == "connection.status"]
        self.assertEqual((event.decision, event.reason), ("allow", "connected"))
        self.assertIsNone(event.actor_id)
        self.assertEqual(event.actor_role, "system")
        self.assertEqual(event.resource_id, self.connection_id)

    async def test_a_check_that_finds_the_same_status_writes_no_audit(self):
        await self.service.check_health(CODEX)
        self.sink.events.clear()
        await self.service.check_health(CODEX)
        self.assertEqual(self.sink.events, [])
        self.assertEqual(self.codex.health_checks, 2)

    async def test_the_adapter_can_report_expired_and_unavailable(self):
        for verdict in (ConnectionStatus.EXPIRED, ConnectionStatus.UNAVAILABLE):
            with self.subTest(verdict=verdict.value):
                self.codex.health = verdict
                self.assertEqual(await self.service.check_health(CODEX), verdict)
                self.assertEqual(self.connection_row(CODEX)["status"], verdict.value)

    async def test_an_expired_failure_of_the_adapter_marks_the_credential_expired(self):
        self.codex.health_error = AdapterFailure(FailureCode.EXPIRED)
        with self.assertLogs("paw_backend.connections", logging.WARNING):
            status = await self.service.check_health(CODEX)
        self.assertEqual(status, ConnectionStatus.EXPIRED)
        self.assertEqual(self.connection_row(CODEX)["status"], "expired")

    async def test_any_other_failure_is_unavailable_and_only_the_type_is_logged(self):
        self.codex.health_error = RuntimeError("provider said " + CANARY)
        with self.assertLogs("paw_backend.connections", logging.WARNING) as logs:
            status = await self.service.check_health(CODEX)
        self.assertEqual(status, ConnectionStatus.UNAVAILABLE)
        text = "\n".join(logs.output)
        self.assertIn("RuntimeError", text)
        self.assertNotIn(CANARY, text)
        self.assertNotIn("provider said", text)

    async def test_a_connected_connection_becomes_unavailable_when_the_check_fails(
        self,
    ):
        self.codex.health_error = RuntimeError("down")
        self.clear_connections()
        self.seed_connection(CODEX, status="connected")
        with self.assertLogs("paw_backend.connections", logging.WARNING):
            await self.service.check_health(CODEX)
        self.assertEqual(self.connection_row(CODEX)["status"], "unavailable")
        self.assertIn("connection.status", [a for a, _, _ in self.audit_actions()])

    async def test_an_answer_that_is_not_a_status_is_unavailable(self):
        for verdict in ("connected", True, None, 1, ConnectionKind.CODEX):
            with self.subTest(verdict=repr(verdict)):
                self.codex.health = verdict
                with self.assertLogs("paw_backend.connections", logging.WARNING):
                    status = await self.service.check_health(CODEX)
                self.assertEqual(status, ConnectionStatus.UNAVAILABLE)

    async def test_a_resolver_that_fails_makes_the_connection_unavailable(self):
        self.resolver.error = KeyError(handle(1))
        with self.assertLogs("paw_backend.connections", logging.WARNING):
            status = await self.service.check_health(CODEX)
        self.assertEqual(status, ConnectionStatus.UNAVAILABLE)
        self.assertEqual(self.codex.health_checks, 0)

    async def test_a_resolver_that_returns_no_secret_makes_it_unavailable(self):
        self.resolver.result = CANARY  # a plain str is not a Secret
        with self.assertLogs("paw_backend.connections", logging.WARNING):
            status = await self.service.check_health(CODEX)
        self.assertEqual(status, ConnectionStatus.UNAVAILABLE)
        self.assertEqual(self.codex.health_checks, 0)

    async def test_a_missing_adapter_is_unavailable_without_a_resolve(self):
        self.adapters._adapters.pop(CODEX)
        self.assertEqual(
            await self.service.check_health(CODEX), ConnectionStatus.UNAVAILABLE
        )
        self.assertEqual(self.resolver.resolved, [])

    async def test_a_check_that_never_answers_is_unavailable_at_the_deadline(self):
        self.seed_connection(CLAUDE, status="connected", secret_handle=handle(2))
        self.claude.gate = asyncio.Event()  # never set

        async def hang(secret):
            await self.claude.gate.wait()

        self.claude.check_health = hang
        service = self.new_service(health_timeout_seconds=0.2)
        with self.assertLogs("paw_backend.connections", logging.WARNING):
            status = await service.check_health(CLAUDE)
        self.assertEqual(status, ConnectionStatus.UNAVAILABLE)
        self.assertEqual(self.connection_row(CLAUDE)["status"], "unavailable")

    async def test_no_connection_is_not_found(self):
        with self.assertRaises(ConnectionNotFoundError):
            await self.service.check_health(CLAUDE)

    async def test_a_verdict_about_a_replaced_credential_is_dropped(self):
        gate = asyncio.Event()

        async def slow_check(secret):
            await gate.wait()
            return ConnectionStatus.EXPIRED

        self.codex.check_health = slow_check
        check = self.spawn(self.service.check_health(CODEX))
        await self.wait_for(lambda: self.resolver.resolved, "the check to start")
        await self.service.replace_credential(
            self.principal(self.admin), CODEX, handle(2)
        )
        gate.set()

        self.assertEqual(await check, ConnectionStatus.EXPIRED)
        # The new credential was not checked: it is still waiting for its own check.
        row = self.connection_row(CODEX)
        self.assertEqual(
            (row["secret_handle"], row["status"]), (handle(2), "unavailable")
        )
        self.assertNotIn("connection.status", [a for a, _, _ in self.audit_actions()])

    async def test_a_failing_audit_does_not_undo_the_stored_status(self):
        service = self.new_service(audit_sink=FailingSink())
        with self.assertLogs("paw_backend.connections", logging.ERROR) as logs:
            await service.check_health(CODEX)
        self.assertEqual(self.connection_row(CODEX)["status"], "connected")
        text = "\n".join(logs.output)
        self.assertIn("RuntimeError", text)
        self.assertNotIn(CANARY, text)


if __name__ == "__main__":
    unittest.main()
