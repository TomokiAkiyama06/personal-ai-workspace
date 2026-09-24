"""ScratchStore.add / get / list_items / pin / unpin against a real PostgreSQL.

Rows are seeded and asserted with SQL (see ``scratch_support``), so each test
depends on the one method it exercises.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from paw_backend.research.scratch import (
    DeferralReason,
    InputProblem,
    InvalidScratchInputError,
    PromotionState,
    ScratchItemNotFoundError,
)

from .scratch_support import T0, PostgresScratchTestCase, requires_postgres

HOUR = timedelta(hours=1)
DAY = timedelta(hours=24)
MICRO = timedelta(microseconds=1)


@requires_postgres
class AddTest(PostgresScratchTestCase):
    async def add(self, **overrides):
        arguments = {"created_by": self.user_id, "summary": "A summary"}
        arguments.update(overrides)
        return await self.store.add(self.project_id, **arguments)

    async def test_the_expiry_is_created_at_plus_24_hours_from_the_injected_clock(self):
        self.clock.now = T0 + timedelta(microseconds=123_456)

        item = await self.add()

        now = self.clock.now
        self.assertEqual(item.created_at, now)
        self.assertEqual(item.expires_at, now + DAY)
        row = self.row(item.id)
        self.assertEqual(row["created_at"], now)
        self.assertEqual(row["expires_at"], now + DAY)

    async def test_a_clock_in_another_time_zone_gives_the_same_instants(self):
        jst = timezone(timedelta(hours=9))
        self.clock.now = datetime(2026, 9, 24, 21, 0, tzinfo=jst)  # 12:00 UTC

        item = await self.add()

        self.assertEqual(item.created_at, T0)
        self.assertEqual(item.expires_at, T0 + DAY)
        self.assertEqual(self.row(item.id)["expires_at"], T0 + DAY)

    async def test_the_snapshot_describes_a_new_unpinned_item_and_matches_the_row(self):
        item = await self.add(
            query="how do I configure X",
            title="Configuring X",
            summary="Short summary",
            content="The whole page",
            source_metadata={"url": "https://docs.example.org/x", "confidence": 0.5},
        )

        self.assertEqual(item.project_id, self.project_id)
        self.assertEqual(item.created_by, self.user_id)
        self.assertIsNone(item.task_id)
        self.assertEqual(item.query, "how do I configure X")
        self.assertEqual(item.title, "Configuring X")
        self.assertEqual(item.summary, "Short summary")
        self.assertEqual(item.content, "The whole page")
        self.assertEqual(
            item.source_metadata,
            {"url": "https://docs.example.org/x", "confidence": 0.5},
        )
        self.assertFalse(item.expired)
        self.assertFalse(item.pinned)
        self.assertFalse(item.in_use)
        self.assertIs(item.promotion_state, PromotionState.NONE)
        self.assertIsNone(item.promotion_requested_at)
        self.assertEqual(item.deferral_reasons, ())
        self.assertEqual(item, self.snapshot(item.id))
        self.assertEqual(self.lease_rows(item.id), {})

    async def test_the_project_and_task_relation_is_kept(self):
        task_id = self.seed_task()

        item = await self.add(task_id=task_id)

        self.assertEqual(item.task_id, task_id)
        row = self.row(item.id)
        self.assertEqual(
            (row["project_id"], row["task_id"]), (self.project_id, task_id)
        )

    async def test_a_task_that_does_not_exist_is_rejected_and_nothing_is_stored(self):
        unknown = uuid4()

        with self.assertRaises(InvalidScratchInputError) as raised:
            await self.add(task_id=unknown)

        self.assertEqual(raised.exception.field, "task_id")
        self.assertIs(raised.exception.problem, InputProblem.UNKNOWN_REFERENCE)
        self.assertNotIn(str(unknown), str(raised.exception))
        self.assertEqual(self.item_ids(), set())

    async def test_a_task_of_another_project_is_rejected_like_a_missing_one(self):
        foreign_task = self.seed_task(project_id=uuid4())

        with self.assertRaises(InvalidScratchInputError) as raised:
            await self.add(task_id=foreign_task)

        self.assertEqual(raised.exception.field, "task_id")
        self.assertIs(raised.exception.problem, InputProblem.UNKNOWN_REFERENCE)
        self.assertNotIn(str(foreign_task), str(raised.exception))
        self.assertEqual(self.item_ids(), set())

    async def test_every_call_stores_a_new_item(self):
        first = await self.add(summary="same")
        second = await self.add(summary="same")

        self.assertNotEqual(first.id, second.id)
        self.assertEqual(self.item_ids(), {first.id, second.id})

    async def test_only_a_body_is_required_and_text_round_trips(self):
        body = "日本語の本文 😀 with 'quotes' and \"double\" and \\ backslash\nnewline"

        item = await self.add(summary=None, content=body)

        self.assertIsNone(item.summary)
        self.assertEqual(item.content, body)
        self.assertEqual(self.row(item.id)["content"], body)

    async def test_the_documented_maximum_sizes_are_stored(self):
        item = await self.add(
            query="q" * 1000,
            title="t" * 500,
            summary="s" * 8000,
            content="c" * 100_000,
            source_metadata={"k": "a" * (16384 - 8)},
        )

        row = self.row(item.id)
        self.assertEqual(
            (
                len(row["query"]),
                len(row["title"]),
                len(row["summary"]),
                len(row["content"]),
            ),
            (1000, 500, 8000, 100_000),
        )
        self.assertEqual(row["source_metadata"], {"k": "a" * (16384 - 8)})

    async def test_the_stored_metadata_is_a_copy(self):
        metadata = {"claims": [{"text": "a"}]}

        item = await self.add(source_metadata=metadata)
        metadata["claims"].append({"text": "changed later"})

        self.assertEqual(
            self.row(item.id)["source_metadata"], {"claims": [{"text": "a"}]}
        )
        self.assertEqual(item.source_metadata, {"claims": [{"text": "a"}]})

    async def test_invalid_input_is_rejected_and_stores_nothing(self):
        cases = [
            ("summary", {"summary": "s" * 8001}),
            ("query", {"query": "  "}),
            ("content", {"content": "x\x00"}),
            ("source_metadata", {"source_metadata": {"a": (1, 2)}}),
            ("summary", {"summary": None, "content": None}),
            ("created_by", {"created_by": str(self.user_id)}),
        ]
        for field, overrides in cases:
            with self.subTest(field=field, overrides=list(overrides)):
                with self.assertRaises(InvalidScratchInputError) as raised:
                    await self.add(**overrides)
                self.assertEqual(raised.exception.field, field)
        self.assertEqual(self.item_ids(), set())

    async def test_a_bad_project_id_is_reported(self):
        with self.assertRaises(InvalidScratchInputError) as raised:
            await self.store.add("not-a-uuid", created_by=self.user_id, summary="s")

        self.assertEqual(raised.exception.field, "project_id")
        self.assertIs(raised.exception.problem, InputProblem.WRONG_TYPE)

    async def test_a_naive_clock_is_rejected_and_stores_nothing(self):
        self.clock.now = datetime(2026, 9, 24, 12, 0)

        with self.assertRaises(InvalidScratchInputError) as raised:
            await self.add()

        self.assertEqual(raised.exception.field, "clock")
        self.assertIs(raised.exception.problem, InputProblem.NAIVE_DATETIME)
        self.assertEqual(self.item_ids(), set())


@requires_postgres
class GetTest(PostgresScratchTestCase):
    async def test_returns_the_full_snapshot(self):
        task_id = self.seed_task()
        item_id = self.seed_item(
            task_id=task_id,
            query="q",
            title="t",
            summary="s",
            content="the content",
            source_metadata={"url": "https://example.org"},
            expires_at=T0 + HOUR,
        )

        item = await self.store.get(self.project_id, item_id)

        self.assertEqual(item, self.snapshot(item_id))
        self.assertEqual(item.content, "the content")
        self.assertEqual(item.task_id, task_id)
        self.assertEqual(item.project_id, self.project_id)
        self.assertFalse(item.expired)
        self.assertEqual(item.expires_at, T0 + HOUR)

    async def test_a_missing_id_is_not_found(self):
        with self.assertRaises(ScratchItemNotFoundError):
            await self.store.get(self.project_id, uuid4())

    async def test_an_item_of_another_project_is_not_found(self):
        item_id = self.seed_item(project_id=uuid4(), expires_at=T0 + HOUR)

        with self.assertRaises(ScratchItemNotFoundError):
            await self.store.get(self.project_id, item_id)

    async def test_the_message_does_not_contain_the_id(self):
        item_id = uuid4()

        with self.assertRaises(ScratchItemNotFoundError) as raised:
            await self.store.get(self.project_id, item_id)

        self.assertNotIn(str(item_id), str(raised.exception))

    async def test_visibility_follows_the_ttl_and_the_exemptions(self):
        soon = T0 + MICRO  # not yet expired
        cases = {
            "unexpired by one microsecond": (
                {"expires_at": soon},
                None,
                True,
                False,
                (),
            ),
            "expires exactly now": ({"expires_at": T0}, None, False, True, ()),
            "expired one microsecond ago": (
                {"expires_at": T0 - MICRO},
                None,
                False,
                True,
                (),
            ),
            "expired but pinned": (
                {"expires_at": T0 - HOUR, "pinned": True},
                None,
                True,
                True,
                (DeferralReason.PINNED,),
            ),
            "expired with a pending promotion": (
                {
                    "expires_at": T0 - HOUR,
                    "promotion_state": "pending",
                    "promotion_requested_at": T0 - HOUR,
                },
                None,
                True,
                True,
                (DeferralReason.PROMOTION_PENDING,),
            ),
            "expired but in use": (
                {"expires_at": T0 - HOUR},
                T0 + MICRO,
                True,
                True,
                (DeferralReason.IN_USE,),
            ),
            "expired and the lease ends exactly now": (
                {"expires_at": T0 - HOUR},
                T0,
                False,
                True,
                (),
            ),
            "expired and the lease ended earlier": (
                {"expires_at": T0 - HOUR},
                T0 - MICRO,
                False,
                True,
                (),
            ),
            "expired, promotion already promoted": (
                {"expires_at": T0 - HOUR, "promotion_state": "promoted"},
                None,
                False,
                True,
                (),
            ),
            "expired, promotion rejected": (
                {"expires_at": T0 - HOUR, "promotion_state": "rejected"},
                None,
                False,
                True,
                (),
            ),
            "unexpired and in use": (
                {"expires_at": T0 + HOUR},
                T0 + timedelta(minutes=5),
                True,
                False,
                (DeferralReason.IN_USE,),
            ),
            "unexpired with an ended lease": (
                {"expires_at": T0 + HOUR},
                T0 - timedelta(minutes=5),
                True,
                False,
                (),
            ),
            "unexpired and pinned": (
                {"expires_at": T0 + HOUR, "pinned": True},
                None,
                True,
                False,
                (DeferralReason.PINNED,),
            ),
        }
        for label, (seed, lease_end, visible, expired, reasons) in cases.items():
            with self.subTest(label):
                item_id = self.seed_item(**seed)
                if lease_end is not None:
                    self.seed_lease(item_id, lease_end)

                if not visible:
                    with self.assertRaises(ScratchItemNotFoundError):
                        await self.store.get(self.project_id, item_id)
                    continue
                item = await self.store.get(self.project_id, item_id)

                self.assertEqual(item.expired, expired)
                self.assertEqual(item.deferral_reasons, reasons)
                self.assertEqual(item.in_use, DeferralReason.IN_USE in reasons)

    async def test_an_expired_item_is_not_visible_even_though_its_row_still_exists(
        self,
    ):
        item_id = self.seed_item(expires_at=T0 - HOUR)

        with self.assertRaises(ScratchItemNotFoundError):
            await self.store.get(self.project_id, item_id)

        self.assertTrue(self.exists(item_id))  # reading never deletes

    async def test_get_does_not_change_anything(self):
        item_id = self.seed_item(expires_at=T0 + HOUR)
        self.seed_lease(item_id, T0 + timedelta(minutes=5))
        before = self.row(item_id), self.lease_rows(item_id)

        await self.store.get(self.project_id, item_id)

        self.assertEqual((self.row(item_id), self.lease_rows(item_id)), before)

    async def test_the_visibility_follows_the_clock(self):
        item_id = self.seed_item(expires_at=T0 + HOUR)

        await self.store.get(self.project_id, item_id)
        self.clock.now = T0 + HOUR - MICRO
        await self.store.get(self.project_id, item_id)
        self.clock.now = T0 + HOUR
        with self.assertRaises(ScratchItemNotFoundError):
            await self.store.get(self.project_id, item_id)

    async def test_bad_ids_are_reported_with_their_field(self):
        with self.assertRaises(InvalidScratchInputError) as raised:
            await self.store.get(self.project_id, str(uuid4()))
        self.assertEqual(raised.exception.field, "item_id")

        with self.assertRaises(InvalidScratchInputError) as raised:
            await self.store.get(None, uuid4())
        self.assertEqual(
            (raised.exception.field, raised.exception.problem),
            ("project_id", InputProblem.REQUIRED),
        )


@requires_postgres
class ListItemsTest(PostgresScratchTestCase):
    async def test_newest_first_with_ties_broken_by_id_descending(self):
        older = self.seed_item(created_at=T0 - HOUR)
        same = [self.seed_item(created_at=T0 - timedelta(minutes=10)) for _ in range(3)]
        newest = self.seed_item(created_at=T0 - timedelta(minutes=1))

        items = await self.store.list_items(self.project_id)

        self.assertEqual(
            [item.id for item in items],
            [newest, *sorted(same, reverse=True), older],
        )

    async def test_the_content_is_left_out_unless_asked_for(self):
        item_id = self.seed_item(summary="short", content="long body")

        (without,) = await self.store.list_items(self.project_id)
        (with_content,) = await self.store.list_items(
            self.project_id, include_content=True
        )

        self.assertEqual(without, self.snapshot(item_id, content=False))
        self.assertIsNone(without.content)
        self.assertEqual(without.summary, "short")
        self.assertEqual(with_content, self.snapshot(item_id))
        self.assertEqual(with_content.content, "long body")

    async def test_only_the_requested_project_is_listed(self):
        mine = self.seed_item()
        self.seed_item(project_id=uuid4())

        items = await self.store.list_items(self.project_id)

        self.assertEqual([item.id for item in items], [mine])

    async def test_a_task_filter_keeps_the_relation(self):
        task_a, task_b = self.seed_task(), self.seed_task()
        in_a = self.seed_item(task_id=task_a, created_at=T0 - timedelta(minutes=2))
        in_b = self.seed_item(task_id=task_b, created_at=T0 - timedelta(minutes=3))
        no_task = self.seed_item(created_at=T0 - timedelta(minutes=4))

        for_a = await self.store.list_items(self.project_id, task_id=task_a)
        for_b = await self.store.list_items(self.project_id, task_id=task_b)
        everything = await self.store.list_items(self.project_id)
        unknown = await self.store.list_items(self.project_id, task_id=uuid4())

        self.assertEqual([item.id for item in for_a], [in_a])
        self.assertEqual([item.id for item in for_b], [in_b])
        self.assertEqual([item.id for item in everything], [in_a, in_b, no_task])
        self.assertEqual(unknown, [])
        self.assertEqual(for_a[0].task_id, task_a)
        self.assertEqual(for_a[0].project_id, self.project_id)

    async def test_a_task_of_another_project_lists_nothing(self):
        foreign_task = self.seed_task(project_id=uuid4())
        self.seed_item(project_id=self.project_id)

        items = await self.store.list_items(self.project_id, task_id=foreign_task)

        self.assertEqual(items, [])

    async def test_invisible_items_are_left_out_and_exempt_expired_ones_are_kept(self):
        visible = self.seed_item(created_at=T0 - timedelta(minutes=1))
        gone = self.seed_item(expires_at=T0)  # expires exactly now
        pinned = self.seed_item(expires_at=T0 - HOUR, pinned=True)
        pending = self.seed_pending(expires_at=T0 - HOUR)
        in_use = self.seed_item(expires_at=T0 - HOUR)
        self.seed_lease(in_use, T0 + timedelta(minutes=1))
        ended = self.seed_item(expires_at=T0 - HOUR)
        self.seed_lease(ended, T0)

        items = await self.store.list_items(self.project_id)

        self.assertEqual(
            {item.id for item in items}, {visible, pinned, pending, in_use}
        )
        self.assertNotIn(gone, {item.id for item in items})
        by_id = {item.id: item for item in items}
        self.assertEqual(by_id[visible].deferral_reasons, ())
        self.assertEqual(by_id[pinned].deferral_reasons, (DeferralReason.PINNED,))
        self.assertTrue(by_id[pinned].expired)
        self.assertEqual(
            by_id[pending].deferral_reasons, (DeferralReason.PROMOTION_PENDING,)
        )
        self.assertEqual(by_id[in_use].deferral_reasons, (DeferralReason.IN_USE,))
        self.assertFalse(by_id[visible].in_use)

    async def test_the_limit_keeps_the_newest_items(self):
        ids = [
            self.seed_item(created_at=T0 - timedelta(minutes=minutes))
            for minutes in (5, 4, 3, 2, 1)
        ]

        items = await self.store.list_items(self.project_id, limit=2)

        self.assertEqual([item.id for item in items], [ids[4], ids[3]])
        self.assertEqual(
            len(await self.store.list_items(self.project_id, limit=200)), 5
        )
        self.assertEqual(len(await self.store.list_items(self.project_id, limit=1)), 1)

    async def test_no_items_is_an_empty_list(self):
        self.assertEqual(await self.store.list_items(self.project_id), [])

    async def test_bad_arguments_are_reported_in_the_documented_order(self):
        cases = [
            ({"limit": 0}, "limit", InputProblem.OUT_OF_RANGE),
            ({"limit": 201}, "limit", InputProblem.OUT_OF_RANGE),
            ({"limit": True}, "limit", InputProblem.WRONG_TYPE),
            ({"limit": "5"}, "limit", InputProblem.WRONG_TYPE),
            ({"include_content": 1}, "include_content", InputProblem.WRONG_TYPE),
            ({"task_id": str(uuid4())}, "task_id", InputProblem.WRONG_TYPE),
            (
                {"task_id": "x", "limit": 0, "include_content": 1},
                "task_id",
                InputProblem.WRONG_TYPE,
            ),
            (
                {"limit": 0, "include_content": 1},
                "limit",
                InputProblem.OUT_OF_RANGE,
            ),
        ]
        for arguments, field, problem in cases:
            with self.subTest(arguments=arguments):
                with self.assertRaises(InvalidScratchInputError) as raised:
                    await self.store.list_items(self.project_id, **arguments)
                self.assertEqual(
                    (raised.exception.field, raised.exception.problem), (field, problem)
                )

    async def test_a_bad_project_id_is_reported_first(self):
        with self.assertRaises(InvalidScratchInputError) as raised:
            await self.store.list_items("x", task_id="y", limit=0)

        self.assertEqual(raised.exception.field, "project_id")


@requires_postgres
class PinTest(PostgresScratchTestCase):
    async def test_pin_keeps_the_item_and_returns_the_new_state(self):
        item_id = self.seed_item(expires_at=T0 + HOUR)
        before = self.row(item_id)

        item = await self.store.pin(self.project_id, item_id)

        self.assertTrue(item.pinned)
        self.assertEqual(item.deferral_reasons, (DeferralReason.PINNED,))
        self.assertEqual(item, self.snapshot(item_id))
        after = self.row(item_id)
        self.assertTrue(after.pop("pinned"))
        self.assertFalse(before.pop("pinned"))
        self.assertEqual(after, before)  # expires_at and everything else unchanged

    async def test_pin_is_idempotent(self):
        item_id = self.seed_item(expires_at=T0 + HOUR)

        first = await self.store.pin(self.project_id, item_id)
        second = await self.store.pin(self.project_id, item_id)

        self.assertEqual(first, second)
        self.assertTrue(self.row(item_id)["pinned"])

    async def test_unpin_removes_the_pin_and_is_idempotent(self):
        item_id = self.seed_item(expires_at=T0 + HOUR, pinned=True)

        first = await self.store.unpin(self.project_id, item_id)
        second = await self.store.unpin(self.project_id, item_id)

        self.assertFalse(first.pinned)
        self.assertEqual(first, second)
        self.assertEqual(first.deferral_reasons, ())
        self.assertFalse(self.row(item_id)["pinned"])

    async def test_an_item_of_another_project_or_a_missing_one_is_not_found(self):
        foreign = self.seed_item(project_id=uuid4(), expires_at=T0 + HOUR)
        pinned_foreign = self.seed_item(
            project_id=uuid4(), expires_at=T0 + HOUR, pinned=True
        )

        for call, item_id in [
            (self.store.pin, foreign),
            (self.store.pin, uuid4()),
            (self.store.unpin, pinned_foreign),
            (self.store.unpin, uuid4()),
        ]:
            with self.subTest(call=call.__name__):
                with self.assertRaises(ScratchItemNotFoundError):
                    await call(self.project_id, item_id)
        self.assertFalse(self.row(foreign)["pinned"])
        self.assertTrue(self.row(pinned_foreign)["pinned"])

    async def test_an_expired_item_without_an_exemption_cannot_be_pinned(self):
        item_id = self.seed_item(expires_at=T0)  # expired exactly now

        with self.assertRaises(ScratchItemNotFoundError):
            await self.store.pin(self.project_id, item_id)

        self.assertFalse(self.row(item_id)["pinned"])

    async def test_an_expired_item_kept_by_another_exemption_can_be_pinned(self):
        pending = self.seed_pending(expires_at=T0 - HOUR)
        in_use = self.seed_item(expires_at=T0 - HOUR)
        self.seed_lease(in_use, T0 + timedelta(minutes=1))

        for item_id in (pending, in_use):
            with self.subTest(item_id=item_id):
                item = await self.store.pin(self.project_id, item_id)
                self.assertTrue(item.pinned)
                self.assertTrue(item.expired)

    async def test_pin_works_up_to_the_last_microsecond_before_expiry(self):
        item_id = self.seed_item(expires_at=T0 + MICRO)

        item = await self.store.pin(self.project_id, item_id)

        self.assertTrue(item.pinned)
        self.assertFalse(item.expired)

    async def test_unpinning_the_last_exemption_of_an_expired_item_ends_it(self):
        item_id = self.seed_item(expires_at=T0 - HOUR, pinned=True)

        item = await self.store.unpin(self.project_id, item_id)

        self.assertFalse(item.pinned)
        self.assertTrue(item.expired)
        self.assertEqual(item.deferral_reasons, ())
        with self.assertRaises(ScratchItemNotFoundError):
            await self.store.get(self.project_id, item_id)
        self.assertTrue(self.exists(item_id))  # the row waits for the next purge

    async def test_unpinning_keeps_an_item_that_is_still_exempt_for_another_reason(
        self,
    ):
        item_id = self.seed_pending(expires_at=T0 - HOUR, pinned=True)

        item = await self.store.unpin(self.project_id, item_id)

        self.assertEqual(item.deferral_reasons, (DeferralReason.PROMOTION_PENDING,))
        self.assertEqual((await self.store.get(self.project_id, item_id)).id, item_id)

    async def test_bad_arguments_are_reported_with_their_field(self):
        for call in (self.store.pin, self.store.unpin):
            with self.subTest(call=call.__name__):
                with self.assertRaises(InvalidScratchInputError) as raised:
                    await call(self.project_id, "nope")
                self.assertEqual(raised.exception.field, "item_id")
                with self.assertRaises(InvalidScratchInputError) as raised:
                    await call("nope", "nope")
                self.assertEqual(raised.exception.field, "project_id")
