"""Arguments are validated before the database is touched.

The store here sits on a ``Database`` without a URL: any attempt to run a query
raises ``DatabaseNotConfiguredError``, so a validation error that is reported
instead proves that nothing was attempted. No PostgreSQL is needed.
"""

import unittest
from datetime import datetime
from uuid import uuid4

from paw_backend.db import Database
from paw_backend.research.scratch import (
    InputProblem,
    InvalidScratchInputError,
    PromotionOutcome,
    ScratchStore,
)

from .scratch_support import T0
from .support import make_settings

SECRET = "sk-live-SECRET-0123456789"


class ServiceValidationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = ScratchStore(Database(make_settings()), clock=lambda: T0)
        self.project, self.item, self.holder = uuid4(), uuid4(), uuid4()

    async def assertRejected(self, field, problem, awaitable):
        with self.assertRaises(InvalidScratchInputError) as raised:
            await awaitable
        self.assertEqual(
            (raised.exception.field, raised.exception.problem), (field, problem)
        )

    async def test_add_rejects_before_touching_the_database(self):
        await self.assertRejected(
            "summary",
            InputProblem.TOO_LONG,
            self.store.add(self.project, created_by=self.holder, summary="s" * 8001),
        )
        await self.assertRejected(
            "task_id",
            InputProblem.WRONG_TYPE,
            self.store.add(
                self.project, created_by=self.holder, summary="s", task_id="not-a-uuid"
            ),
        )
        await self.assertRejected(
            "summary",
            InputProblem.REQUIRED,
            self.store.add(self.project, created_by=self.holder),
        )

    async def test_add_does_not_echo_the_content_of_a_rejected_field(self):
        with self.assertRaises(InvalidScratchInputError) as raised:
            await self.store.add(
                self.project, created_by=self.holder, summary=SECRET * 1000
            )

        self.assertNotIn(SECRET, str(raised.exception))
        self.assertNotIn(SECRET, repr(raised.exception.args))

    async def test_get_and_list_reject_bad_ids(self):
        await self.assertRejected(
            "item_id", InputProblem.WRONG_TYPE, self.store.get(self.project, "x")
        )
        await self.assertRejected(
            "project_id", InputProblem.REQUIRED, self.store.list_items(None)
        )
        await self.assertRejected(
            "limit",
            InputProblem.OUT_OF_RANGE,
            self.store.list_items(self.project, limit=201),
        )
        await self.assertRejected(
            "include_content",
            InputProblem.WRONG_TYPE,
            self.store.list_items(self.project, include_content="yes"),
        )

    async def test_the_change_operations_reject_bad_ids(self):
        for name in ("pin", "unpin", "request_promotion"):
            with self.subTest(name):
                await self.assertRejected(
                    "item_id",
                    InputProblem.WRONG_TYPE,
                    getattr(self.store, name)(self.project, str(self.item)),
                )
        await self.assertRejected(
            "holder_id",
            InputProblem.WRONG_TYPE,
            self.store.acquire_use(self.project, self.item, str(self.holder)),
        )
        await self.assertRejected(
            "lease_seconds",
            InputProblem.OUT_OF_RANGE,
            self.store.acquire_use(
                self.project, self.item, self.holder, lease_seconds=0
            ),
        )
        await self.assertRejected(
            "holder_id",
            InputProblem.REQUIRED,
            self.store.release_use(self.project, self.item, None),
        )
        await self.assertRejected(
            "outcome",
            InputProblem.WRONG_TYPE,
            self.store.resolve_promotion(self.project, self.item, "promoted"),
        )

    async def test_purge_rejects_bad_arguments(self):
        await self.assertRejected(
            "now",
            InputProblem.NAIVE_DATETIME,
            self.store.purge_expired(now=datetime(2026, 9, 24, 12, 0)),
        )
        await self.assertRejected(
            "batch_size",
            InputProblem.OUT_OF_RANGE,
            self.store.purge_expired(batch_size=0),
        )

    async def test_a_valid_call_reaches_the_database_layer(self):
        # The counterpart of the tests above: a valid call is *not* stopped by
        # validation, it fails only when it needs the (unconfigured) database.
        from paw_backend.db import DatabaseNotConfiguredError

        with self.assertRaises(DatabaseNotConfiguredError):
            await self.store.get(self.project, self.item)
        with self.assertRaises(DatabaseNotConfiguredError):
            await self.store.purge_expired(batch_size=5000)
        with self.assertRaises(DatabaseNotConfiguredError):
            await self.store.resolve_promotion(
                self.project, self.item, PromotionOutcome.PROMOTED
            )


if __name__ == "__main__":
    unittest.main()
