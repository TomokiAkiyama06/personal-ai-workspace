"""The tool approval diagnostics check EVERY state column the store updates.

``PostgresApprovalStore`` updates seven columns of ``tool_approvals`` (status,
approver_id, decided_at, step_up_verified, consumed_at, revoked_at, revoked_by).
The startup check used ``has_any_column_privilege(..., 'UPDATE')``, so a split-role
deployment with, say, INSERT plus UPDATE(status) only looked healthy while every
approval decision failed with a permission error (issue #90, finding on #74).
``cannot_work`` is now true unless UPDATE on all of them is held (through the
table or column by column). Needs ``PAW_TEST_DATABASE_URL`` (skipped otherwise);
the roles are created per run by ``RoleTestCase``.
"""

import re
import unittest
from pathlib import Path

from sqlalchemy import text

from paw_backend.authz import diagnostics
from paw_backend.authz.diagnostics import (
    read_tool_table_access,
    warn_if_tool_approval_tables_are_mutable,
)
from paw_backend.tools import approval_store

from .test_tools_postgres_roles import RoleTestCase

LOGGER = "paw_backend.authz.diagnostics"
# The columns ``PostgresApprovalStore`` updates. Written out here on purpose: the
# tests below check it against the store's SQL, against what the migration
# grants and against the diagnostics' own list.
APPROVAL_STATE_COLUMNS = (
    "status",
    "approver_id",
    "decided_at",
    "step_up_verified",
    "consumed_at",
    "revoked_at",
    "revoked_by",
)


class StoreColumnsTest(unittest.TestCase):
    """The list the check uses is what the store updates, not a guess."""

    def test_it_is_the_set_of_columns_the_store_updates(self):
        source = Path(approval_store.__file__).read_text(encoding="utf-8")
        blocks = re.findall(
            r"UPDATE tool_approvals\s+SET(.*?)\bWHERE", source, flags=re.DOTALL
        )
        self.assertGreaterEqual(len(blocks), 5)  # revoke x3, expire, decide, consume
        assigned = {
            name for block in blocks for name in re.findall(r"(\w+)\s*=", block)
        }
        self.assertEqual(assigned, set(APPROVAL_STATE_COLUMNS))

    def test_the_diagnostics_check_exactly_those_columns(self):
        self.assertEqual(
            set(diagnostics.APPROVAL_STATE_COLUMNS), set(APPROVAL_STATE_COLUMNS)
        )
        self.assertEqual(
            len(diagnostics.APPROVAL_STATE_COLUMNS),
            len(set(diagnostics.APPROVAL_STATE_COLUMNS)),
        )


class MigrationGrantsTest(RoleTestCase):
    async def test_it_is_what_the_migration_grants_the_application_role(self):
        async with self.owner_db.session() as session:
            rows = await session.execute(
                text(
                    "SELECT attname FROM pg_attribute"
                    " WHERE attrelid = 'tool_approvals'::regclass"
                    " AND attnum > 0 AND NOT attisdropped"
                    " AND has_column_privilege(:role, attrelid, attnum, 'UPDATE')"
                ),
                {"role": self.role},
            )
            granted = {row[0] for row in rows}
        self.assertEqual(granted, set(APPROVAL_STATE_COLUMNS))


class IncompleteUpdateGrantsTest(RoleTestCase):
    async def revoke_update_of(self, *columns: str) -> None:
        await self.owner_execute(
            f"REVOKE UPDATE ({', '.join(columns)}) ON tool_approvals FROM {self.role}"
        )

    async def test_the_complete_grant_can_work(self):
        access = (await read_tool_table_access(self.app_db))["tool_approvals"]
        self.assertFalse(access.cannot_work)
        self.assertTrue(access.can_update_state)
        with self.assertNoLogs(LOGGER, level="WARNING"):
            await warn_if_tool_approval_tables_are_mutable(self.app_db, 3)

    async def test_each_missing_state_column_makes_the_role_unable_to_work(self):
        for column in APPROVAL_STATE_COLUMNS:
            with self.subTest(missing=column):
                await self.revoke_update_of(column)
                try:
                    access = (await read_tool_table_access(self.app_db))[
                        "tool_approvals"
                    ]
                    self.assertEqual(
                        (access.can_insert, access.can_update_some), (True, True)
                    )
                    self.assertTrue(access.cannot_work)
                    self.assertFalse(access.can_update_state)
                    with self.assertLogs(LOGGER, level="WARNING") as logs:
                        await warn_if_tool_approval_tables_are_mutable(self.app_db, 3)
                    self.assertEqual(len(logs.output), 1)
                    self.assertIn("cannot use tool_approvals", logs.output[0])
                finally:
                    await self.owner_execute(
                        f"GRANT UPDATE ({column}) ON tool_approvals TO {self.role}"
                    )
        with self.assertNoLogs(LOGGER, level="WARNING"):
            await warn_if_tool_approval_tables_are_mutable(self.app_db, 3)

    async def test_insert_and_update_of_the_status_alone_is_not_enough(self):
        # The reviewer's case: nothing but ``status`` can be updated, so an
        # approval can never record its approver, decision time or consumption.
        await self.revoke_update_of(
            *(c for c in APPROVAL_STATE_COLUMNS if c != "status")
        )
        access = (await read_tool_table_access(self.app_db))["tool_approvals"]
        self.assertTrue(access.can_insert and access.can_update_some)
        self.assertTrue(access.cannot_work)
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            await warn_if_tool_approval_tables_are_mutable(self.app_db, 3)
        self.assertIn("cannot use tool_approvals", logs.output[0])

    async def test_a_table_level_update_covers_every_column(self):
        await self.owner_execute(f"GRANT UPDATE ON tool_approvals TO {self.role}")
        access = (await read_tool_table_access(self.app_db))["tool_approvals"]
        self.assertTrue(access.can_update and access.can_update_state)
        self.assertFalse(access.cannot_work)
        # ... but the role is no longer restricted, which is still warned about.
        self.assertFalse(access.protected)

    async def test_no_update_at_all_still_cannot_work(self):
        await self.revoke_update_of(*APPROVAL_STATE_COLUMNS)
        access = (await read_tool_table_access(self.app_db))["tool_approvals"]
        self.assertEqual(
            (access.can_update_some, access.can_update_state, access.cannot_work),
            (False, False, True),
        )

    async def test_the_history_table_needs_no_update(self):
        events = (await read_tool_table_access(self.app_db))["tool_approval_events"]
        self.assertEqual(
            (events.can_insert, events.can_update_state, events.cannot_work),
            (True, False, False),
        )
