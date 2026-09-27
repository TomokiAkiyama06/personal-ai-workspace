"""The audit row of an external send, built from the record (issue #87).

No database: ``external_send_event`` is a pure function. What matters is what the
row holds (the hash, the counts, the kinds, never text) and that a record that
is not exactly what ``ExternalSendRecord`` promises is refused before anything is
written, with an error that never echoes a value.
"""

import json
import unittest
import uuid
from dataclasses import fields
from datetime import UTC, datetime, timedelta, timezone
from enum import StrEnum

from paw_backend.authz.models import (
    EXTERNAL_SEND_DETAILS_KEYS,
    EXTERNAL_SEND_WITHHELD_KEYS,
)
from paw_backend.research.privacy import (
    EXTERNAL_SEND_ACTION,
    EXTERNAL_SEND_REASON,
    EXTERNAL_SEND_RESOURCE_KIND,
    ContextLabel,
    ExternalSendRecord,
    PrivacyGate,
    WithheldCounts,
    external_send_event,
)
from paw_backend.research.providers import ProviderKind

from .privacy_audit_support import NOW, fingerprint_of, make_record, tampered_record
from .privacy_support import PROJECT_ID, guarded, piece

SECRET_TEXT = "SECRET-CANARY-91b7"


class MappingTest(unittest.TestCase):
    def test_the_event_holds_ids_enums_and_the_time_only(self):
        project = uuid.uuid4()
        record = make_record(project, query="python asyncio")
        event_id, correlation_id = uuid.uuid4(), uuid.uuid4()
        event, _ = external_send_event(
            record, event_id=event_id, correlation_id=correlation_id
        )
        self.assertEqual(
            event.model_dump(),
            {
                "event_id": event_id,
                "correlation_id": correlation_id,
                "occurred_at": NOW,
                "actor_id": None,
                "actor_role": "system",
                "agent_id": None,
                "action": "research.external_send",
                "resource_kind": "research_query",
                "resource_id": None,
                "project_id": project,
                "repo_id": None,
                "repo_acl": None,
                "decision": "allow",
                "reason": "send_authorized",
                "old_role": None,
                "new_role": None,
                "client_request_id": None,
            },
        )

    def test_the_names_are_the_documented_literals(self):
        # Spelled out: the migration's CHECK and the README repeat these.
        self.assertEqual(EXTERNAL_SEND_ACTION, "research.external_send")
        self.assertEqual(EXTERNAL_SEND_REASON, "send_authorized")
        self.assertEqual(EXTERNAL_SEND_RESOURCE_KIND, "research_query")

    def test_the_details_are_the_hash_the_length_the_kinds_and_the_counts(self):
        record = make_record(
            query="asyncio gather timeout",
            provider_kinds=(ProviderKind.WEB, ProviderKind.GITHUB),
            withheld=WithheldCounts(
                private_source=2, private_memory=1, raw_conversation=0, secret=3
            ),
            credentials_removed=4,
            pieces_matched=5,
            abstractions=6,
            truncated=True,
        )
        _, details = external_send_event(record)
        self.assertEqual(
            details,
            {
                "query_fingerprint": fingerprint_of("asyncio gather timeout"),
                "query_chars": 22,
                "provider_kinds": ["web", "github"],
                "withheld": {
                    "private_source": 2,
                    "private_memory": 1,
                    "raw_conversation": 0,
                    "secret": 3,
                },
                "credentials_removed": 4,
                "pieces_matched": 5,
                "abstractions": 6,
                "truncated": True,
            },
        )
        self.assertEqual(tuple(details), EXTERNAL_SEND_DETAILS_KEYS)
        self.assertEqual(tuple(details["withheld"]), EXTERNAL_SEND_WITHHELD_KEYS)

    def test_the_details_are_plain_json_types_and_a_fresh_copy(self):
        record = make_record()
        first = external_send_event(record)[1]
        second = external_send_event(record)[1]
        self.assertIsNot(first, second)
        first["withheld"]["secret"] = 99
        first["provider_kinds"].append("docs")
        self.assertEqual(second["withheld"]["secret"], 0)
        self.assertEqual(second["provider_kinds"], ["web"])

        def types(value):
            if isinstance(value, dict):
                found = {type(key) for key in value}
                for item in value.values():
                    found |= types(item)
                return found
            if isinstance(value, list):
                found = set()
                for item in value:
                    found |= types(item)
                return found
            return {type(value)}

        # Exactly these types: no enum member, no subclass of them.
        self.assertEqual(types(second), {str, int, bool})
        json.dumps(second)

    def test_every_provider_kind_maps_to_its_value_in_kind_order(self):
        record = make_record(provider_kinds=tuple(ProviderKind))
        _, details = external_send_event(record)
        self.assertEqual(
            details["provider_kinds"], ["web", "docs", "github", "opencode"]
        )

    def test_the_time_is_the_time_of_the_decision_in_utc(self):
        moment = datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=UTC)
        event, _ = external_send_event(make_record(recorded_at=moment))
        self.assertEqual(event.occurred_at, moment)
        self.assertEqual(event.occurred_at.utcoffset(), timedelta(0))

    def test_ids_default_to_fresh_uuids(self):
        record = make_record()
        first = external_send_event(record)[0]
        second = external_send_event(record)[0]
        self.assertNotEqual(first.event_id, second.event_id)
        self.assertNotEqual(first.correlation_id, second.correlation_id)
        self.assertNotEqual(first.event_id, first.correlation_id)


class NoTextTest(unittest.IsolatedAsyncioTestCase):
    """The gate's real record, with private text everywhere, holds none of it."""

    async def test_neither_the_draft_nor_the_context_reaches_the_row(self):
        seen: list[tuple] = []

        class Capture:
            async def record(self, record):
                seen.append(external_send_event(record))

        private = (
            "the billing service retries payment three times before alerting finance"
        )
        draft = f"how to retry payments {private} in stripe {SECRET_TEXT}"
        gate = PrivacyGate(Capture(), clock=lambda: NOW)
        query = await guarded(
            gate.authorize(
                draft,
                [
                    piece(ContextLabel.PRIVATE_SOURCE, private),
                    piece(ContextLabel.SECRET, SECRET_TEXT),
                    piece(ContextLabel.PUBLIC, "stripe idempotency keys"),
                ],
                project_id=PROJECT_ID,
                provider_kinds=frozenset({ProviderKind.WEB, ProviderKind.DOCS}),
            )
        )
        ((event, details),) = seen
        rendered = json.dumps(
            [event.model_dump(mode="json"), details], ensure_ascii=False
        )
        for text in (
            private,
            SECRET_TEXT,
            "billing service",
            "alerting finance",
            draft,
            query.query,
            "stripe idempotency",
        ):
            self.assertNotIn(text, rendered)
        self.assertEqual(details["query_fingerprint"], fingerprint_of(query.query))
        self.assertEqual(details["query_chars"], len(query.query))
        self.assertEqual(details["provider_kinds"], ["web", "docs"])
        self.assertEqual(
            details["withheld"],
            {
                "private_source": 1,
                "private_memory": 0,
                "raw_conversation": 0,
                "secret": 1,
            },
        )
        self.assertEqual(event.project_id, PROJECT_ID)


class ValidationTest(unittest.TestCase):
    """Every field, every kind of bad value: refused, and never echoed."""

    def refused(self, record, expected):
        with self.assertRaises(expected) as caught:
            external_send_event(record)
        message = str(caught.exception)
        self.assertNotIn(SECRET_TEXT, message)
        self.assertNotIn("sha256", message)
        return message

    def test_a_record_that_is_not_a_record_is_a_type_error(self):
        for label, value in {
            "None": None,
            "a dict": make_record().to_dict(),
            "a str": "sha256:" + "a" * 64,
            "an int": 5,
            "an object": object(),
            "the class": ExternalSendRecord,
        }.items():
            with self.subTest(value=label):
                self.refused(value, TypeError)

    def test_a_field_of_the_wrong_type_is_a_type_error(self):
        fingerprint = fingerprint_of("q")
        cases = {
            "recorded_at": [None, "2026-09-24T12:00:00+00:00", 5, NOW.timestamp()],
            "project_id": [None, str(uuid.uuid4()), 5, uuid.uuid4().hex, b"x"],
            "query_fingerprint": [None, 5, fingerprint.encode(), [fingerprint]],
            "query_chars": [None, "5", 5.0, True, False, [5]],
            "provider_kinds": [
                None,
                ["web"],
                "web",
                ("web",),
                (ProviderKind.WEB.value,),
                (None,),
                frozenset({ProviderKind.WEB}),
            ],
            "withheld": [None, {"secret": 1}, WithheldCounts().to_dict(), 5],
            "credentials_removed": [None, "0", 0.0, True],
            "pieces_matched": [None, "0", 0.0, False],
            "abstractions": [None, "0", 0.0, True],
            "truncated": [None, 0, 1, "false", "true"],
        }
        for name, values in cases.items():
            for index, value in enumerate(values):
                with self.subTest(field=name, value=index):
                    self.refused(tampered_record(**{name: value}), TypeError)

    def test_a_field_out_of_range_or_shape_is_a_value_error(self):
        cases = {
            "recorded_at": [
                datetime(2026, 9, 24, 12, 0),
                datetime(2026, 9, 24, 12, 0, tzinfo=timezone(timedelta(hours=9))),
            ],
            "query_fingerprint": [
                "",
                "sha256:",
                "sha256:" + "a" * 63,
                "sha256:" + "a" * 65,
                "sha256:" + "A" * 64,
                "sha256:" + "g" * 64,
                "sha256:" + "a" * 64 + "\n",
                " sha256:" + "a" * 64,
                "SHA256:" + "a" * 64,
                "sha1:" + "a" * 64,
                "a" * 64,
                "sha256:" + "a" * 63 + "\x00",
                "select 1; drop table audit_events; --",
                SECRET_TEXT,
                "sha256:" + "\u0661" * 64,
            ],
            "query_chars": [0, -1, 257, 10**9, -(10**9)],
            "provider_kinds": [(), tuple(ProviderKind) + (ProviderKind.WEB,)],
            "credentials_removed": [-1, 10**6 + 1, 10**30],
            "pieces_matched": [-1, 33, 10**30],
            "abstractions": [-1, 10**6 + 1],
        }
        for name, values in cases.items():
            for index, value in enumerate(values):
                with self.subTest(field=name, value=index):
                    self.refused(tampered_record(**{name: value}), ValueError)

    def test_provider_kinds_must_be_distinct_and_in_kind_order(self):
        for kinds in (
            (ProviderKind.DOCS, ProviderKind.WEB),
            (ProviderKind.WEB, ProviderKind.WEB),
            (ProviderKind.GITHUB, ProviderKind.DOCS, ProviderKind.WEB),
        ):
            with self.subTest(kinds=[kind.value for kind in kinds]):
                self.refused(tampered_record(provider_kinds=kinds), ValueError)

    def test_a_withheld_count_out_of_range_or_of_the_wrong_type_is_refused(self):
        for label, value, expected in (
            ("negative", -1, ValueError),
            ("above the piece limit", 33, ValueError),
            ("a bool", True, TypeError),
            ("a float", 1.0, TypeError),
            ("a str", "1", TypeError),
            ("None", None, TypeError),
        ):
            for name in EXTERNAL_SEND_WITHHELD_KEYS:
                with self.subTest(case=label, label=name):
                    withheld = WithheldCounts()
                    object.__setattr__(withheld, name, value)
                    self.refused(tampered_record(withheld=withheld), expected)

    def test_a_slot_that_was_never_set_is_a_type_error_not_an_attribute_error(self):
        for name in (
            "recorded_at",
            "project_id",
            "query_fingerprint",
            "query_chars",
            "provider_kinds",
            "withheld",
            "credentials_removed",
            "pieces_matched",
            "abstractions",
            "truncated",
        ):
            with self.subTest(field=name):
                record = make_record()
                object.__delattr__(record, name)
                self.refused(record, TypeError)

    def test_a_withheld_count_that_was_never_set_is_a_type_error(self):
        for name in EXTERNAL_SEND_WITHHELD_KEYS:
            with self.subTest(label=name):
                withheld = WithheldCounts()
                object.__delattr__(withheld, name)
                self.refused(tampered_record(withheld=withheld), TypeError)

    def test_a_subclass_of_a_type_cannot_stand_in_for_it(self):
        good = fingerprint_of("q")

        class LyingStr(str):
            """Looks valid to a pattern, but is not the string it seems to be."""

            def __str__(self):
                return SECRET_TEXT

        class BigInt(int):
            pass

        class FakeUUID(uuid.UUID):
            pass

        class FakeWithheld(WithheldCounts):
            pass

        class LaterDatetime(datetime):
            pass

        class FakeKind(StrEnum):
            WEB = "web"

        cases = {
            "query_fingerprint": LyingStr(good),
            "query_chars": BigInt(5),
            "project_id": FakeUUID(int=5),
            "withheld": FakeWithheld(),
            "recorded_at": LaterDatetime(2026, 9, 24, 12, tzinfo=UTC),
            "provider_kinds": (FakeKind.WEB,),
            "credentials_removed": BigInt(0),
            "pieces_matched": BigInt(0),
            "abstractions": BigInt(0),
        }
        for name, value in cases.items():
            with self.subTest(field=name):
                self.refused(tampered_record(**{name: value}), TypeError)

    def test_ids_of_the_event_must_be_uuids(self):
        for name in ("event_id", "correlation_id"):
            for value in ("x", 5, uuid.uuid4().hex, b"x", object()):
                with self.subTest(argument=name, value=type(value).__name__):
                    with self.assertRaises(TypeError):
                        external_send_event(make_record(), **{name: value})

    def test_unknown_arguments_are_refused(self):
        with self.assertRaises(TypeError):
            external_send_event(make_record(), query="python")  # type: ignore[call-arg]

    def test_extra_attributes_of_a_subclass_are_never_read(self):
        class Sneaky(ExternalSendRecord):
            pass

        base = make_record()
        record = Sneaky(
            **{field.name: getattr(base, field.name) for field in fields(base)}
        )
        object.__setattr__(record, "query", SECRET_TEXT)
        event, details = external_send_event(record)
        rendered = json.dumps([event.model_dump(mode="json"), details])
        self.assertNotIn(SECRET_TEXT, rendered)
        self.assertEqual(tuple(details), EXTERNAL_SEND_DETAILS_KEYS)

    def test_a_valid_record_is_accepted_at_every_boundary(self):
        for overrides, key, expected in (
            ({"query_chars": 1}, "query_chars", 1),
            ({"query_chars": 256}, "query_chars", 256),
            ({"credentials_removed": 10**6}, "credentials_removed", 10**6),
            ({"abstractions": 10**6}, "abstractions", 10**6),
            ({"pieces_matched": 32}, "pieces_matched", 32),
            ({"truncated": True}, "truncated", True),
            (
                {"withheld": WithheldCounts(32, 32, 32, 32)},
                "withheld",
                dict.fromkeys(EXTERNAL_SEND_WITHHELD_KEYS, 32),
            ),
            (
                {"provider_kinds": tuple(ProviderKind)},
                "provider_kinds",
                ["web", "docs", "github", "opencode"],
            ),
        ):
            with self.subTest(key=key, expected=expected):
                event, details = external_send_event(make_record(**overrides))
                self.assertEqual(event.action, "research.external_send")
                self.assertEqual(details[key], expected)


if __name__ == "__main__":
    unittest.main()
