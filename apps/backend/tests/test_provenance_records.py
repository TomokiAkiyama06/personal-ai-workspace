"""The values of the provenance store: inputs validate themselves, outputs are
immutable (pure, no I/O)."""

import dataclasses
import unittest
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

from paw_backend.research.provenance import (
    Claim,
    EntityKind,
    InputProblem,
    InvalidProvenanceInputError,
    RecordedClaim,
    Reference,
    ReferenceKind,
    Relation,
    RelationKind,
    Source,
    SourceInput,
    SourceLink,
    SourceLinkInput,
    Stance,
    Trace,
    TracedClaim,
)
from paw_backend.research.providers.contract import (
    ProviderKind,
    SourceMetadata,
    SourceType,
)

from .provenance_support import T0, content_hash


def problem_of(test: unittest.TestCase, function, *args, **kwargs):
    with test.assertRaises(InvalidProvenanceInputError) as raised:
        function(*args, **kwargs)
    return raised.exception.field, raised.exception.problem


def make_source(**overrides) -> Source:
    values = {
        "id": uuid4(),
        "project_id": uuid4(),
        "locator": "https://example.com/a",
        "source_type": SourceType.PRIMARY,
        "title": "T",
        "content_hash": content_hash(),
        "fetched_at": T0,
        "published_at": None,
        "created_at": T0,
    }
    values.update(overrides)
    return Source(**values)


class EnumsTest(unittest.TestCase):
    def test_the_values_are_the_ones_stored_in_the_database(self):
        self.assertEqual(
            [member.value for member in Stance], ["supports", "contradicts"]
        )
        self.assertEqual(
            [member.value for member in RelationKind], ["duplicate", "contradiction"]
        )
        self.assertEqual([member.value for member in EntityKind], ["claim", "source"])
        self.assertEqual([member.value for member in ReferenceKind], ["answer", "task"])


class SourceInputTest(unittest.TestCase):
    def build(self, **overrides) -> SourceInput:
        values = {
            "locator": "https://example.com/a",
            "source_type": SourceType.OFFICIAL_DOCS,
            "fetched_at": T0,
            "content_hash": content_hash(),
        }
        values.update(overrides)
        return SourceInput(**values)

    def test_a_valid_source_keeps_its_values_and_has_defaults(self):
        source = self.build()

        self.assertEqual(source.locator, "https://example.com/a")
        self.assertIs(source.source_type, SourceType.OFFICIAL_DOCS)
        self.assertEqual(source.fetched_at, T0)
        self.assertEqual(source.content_hash, content_hash())
        self.assertIsNone(source.published_at)
        self.assertEqual(source.title, "")

    def test_the_locator_is_canonicalised_on_construction(self):
        source = self.build(
            locator="HTTPS://Example.COM:443/a?utm_source=x&b=2&a=1#frag"
        )

        self.assertEqual(source.locator, "https://example.com/a?a=1&b=2")

    def test_two_spellings_of_one_page_give_equal_sources(self):
        first = self.build(locator="https://EXAMPLE.com/a?utm_medium=mail")
        second = self.build(locator="https://example.com:443/a")

        self.assertEqual(first, second)

    def test_the_instants_are_converted_to_utc(self):
        tokyo = timezone(timedelta(hours=9))
        source = self.build(
            fetched_at=datetime(2026, 9, 24, 21, 0, tzinfo=tokyo),
            published_at=datetime(2026, 9, 1, 9, 0, tzinfo=tokyo),
        )

        self.assertEqual(source.fetched_at, datetime(2026, 9, 24, 12, 0, tzinfo=UTC))
        self.assertEqual(source.fetched_at.utcoffset(), timedelta(0))
        self.assertEqual(source.published_at, datetime(2026, 9, 1, 0, 0, tzinfo=UTC))
        self.assertEqual(source.published_at.utcoffset(), timedelta(0))

    def test_invalid_values_name_their_field(self):
        cases = {
            "locator": ("ftp://example.com/", InputProblem.INVALID_FORMAT),
            "source_type": ("primary", InputProblem.WRONG_TYPE),
            "fetched_at": (datetime(2026, 9, 24), InputProblem.NAIVE_DATETIME),
            "published_at": (datetime(2026, 9, 24), InputProblem.NAIVE_DATETIME),
            "content_hash": ("sha256:abc", InputProblem.INVALID_FORMAT),
            "title": ("x" * 301, InputProblem.TOO_LONG),
        }
        for field, (value, problem) in cases.items():
            with self.subTest(field):
                self.assertEqual(
                    problem_of(self, self.build, **{field: value}), (field, problem)
                )

    def test_required_fields_reject_none(self):
        for field in ("locator", "source_type", "fetched_at", "content_hash"):
            with self.subTest(field):
                self.assertEqual(
                    problem_of(self, self.build, **{field: None}),
                    (field, InputProblem.REQUIRED),
                )

    def test_the_title_may_be_empty_and_is_kept_as_given(self):
        self.assertEqual(self.build(title="").title, "")
        self.assertEqual(self.build(title="  Spaced  ").title, "  Spaced  ")
        self.assertEqual(len(self.build(title="あ" * 300).title), 300)
        self.assertEqual(
            problem_of(self, self.build, title="a\x00b"),
            ("title", InputProblem.INVALID_CHARACTERS),
        )
        self.assertEqual(
            problem_of(self, self.build, title=None), ("title", InputProblem.REQUIRED)
        )

    def test_the_source_is_immutable_and_takes_keywords_only(self):
        source = self.build()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            source.title = "changed"
        with self.assertRaises(TypeError):
            SourceInput("https://example.com/", SourceType.PRIMARY, T0, content_hash())
        self.assertEqual(hash(source), hash(self.build()))

    def test_replace_validates_again(self):
        source = self.build()

        self.assertEqual(dataclasses.replace(source, title="New").title, "New")
        self.assertEqual(
            problem_of(self, dataclasses.replace, source, locator="mailto:a@b.c"),
            ("locator", InputProblem.INVALID_FORMAT),
        )

    def test_the_error_does_not_contain_the_secret_of_the_locator(self):
        with self.assertRaises(InvalidProvenanceInputError) as raised:
            self.build(locator="https://user:SECRET-TOKEN-4f9a1c@example.com/")
        self.assertNotIn("SECRET", str(raised.exception))


class SourceInputFromMetadataTest(unittest.TestCase):
    def metadata(self, **overrides) -> SourceMetadata:
        values = {
            "provider_kind": ProviderKind.DOCS,
            "provider_id": "docs-a",
            "locator": "https://docs.example.com/guide",
            "title": "Guide",
            "retrieved_at": T0,
            "content_hash": content_hash("guide"),
            "source_type": SourceType.OFFICIAL_DOCS,
            "published_at": datetime(2026, 8, 1, tzinfo=UTC),
            "private_source": True,
        }
        values.update(overrides)
        return SourceMetadata(**values)

    def test_the_metadata_of_the_provider_adapter_becomes_a_source(self):
        source = SourceInput.from_metadata(self.metadata())

        self.assertEqual(
            source,
            SourceInput(
                locator="https://docs.example.com/guide",
                source_type=SourceType.OFFICIAL_DOCS,
                fetched_at=T0,
                content_hash=content_hash("guide"),
                published_at=datetime(2026, 8, 1, tzinfo=UTC),
                title="Guide",
            ),
        )

    def test_an_unknown_publication_date_stays_unknown(self):
        source = SourceInput.from_metadata(self.metadata(published_at=None))

        self.assertIsNone(source.published_at)

    def test_something_else_is_the_wrong_type(self):
        for bad in (None, {"locator": "https://example.com/"}, "https://example.com/"):
            with self.subTest(repr(bad)):
                self.assertEqual(
                    problem_of(self, SourceInput.from_metadata, bad),
                    ("metadata", InputProblem.WRONG_TYPE),
                )


class SourceLinkInputTest(unittest.TestCase):
    def source(self) -> SourceInput:
        return SourceInput(
            locator="https://example.com/a",
            source_type=SourceType.PRIMARY,
            fetched_at=T0,
            content_hash=content_hash(),
        )

    def test_the_default_stance_is_supports(self):
        self.assertIs(SourceLinkInput(source=self.source()).stance, Stance.SUPPORTS)
        self.assertIs(
            SourceLinkInput(source=self.source(), stance=Stance.CONTRADICTS).stance,
            Stance.CONTRADICTS,
        )

    def test_the_source_and_the_stance_are_checked(self):
        self.assertEqual(
            problem_of(self, SourceLinkInput, source="https://example.com/a"),
            ("source", InputProblem.WRONG_TYPE),
        )
        self.assertEqual(
            problem_of(self, SourceLinkInput, source=None),
            ("source", InputProblem.WRONG_TYPE),
        )
        self.assertEqual(
            problem_of(self, SourceLinkInput, source=self.source(), stance="supports"),
            ("stance", InputProblem.WRONG_TYPE),
        )
        self.assertEqual(
            problem_of(self, SourceLinkInput, source=self.source(), stance=None),
            ("stance", InputProblem.REQUIRED),
        )

    def test_the_link_is_immutable(self):
        link = SourceLinkInput(source=self.source())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            link.stance = Stance.CONTRADICTS


class ReferenceTest(unittest.TestCase):
    def test_a_reference_is_a_kind_and_an_id(self):
        identifier = uuid4()

        self.assertEqual(
            Reference.answer(identifier), Reference(ReferenceKind.ANSWER, identifier)
        )
        self.assertEqual(
            Reference.task(identifier), Reference(ReferenceKind.TASK, identifier)
        )
        self.assertNotEqual(Reference.answer(identifier), Reference.task(identifier))
        self.assertEqual(Reference.task(identifier).kind, ReferenceKind.TASK)
        self.assertEqual(Reference.task(identifier).id, identifier)

    def test_a_reference_is_hashable(self):
        identifier = uuid4()

        self.assertEqual(
            {
                Reference.answer(identifier),
                Reference.answer(identifier),
                Reference.task(identifier),
            },
            {Reference.answer(identifier), Reference.task(identifier)},
        )

    def test_the_kind_and_the_id_are_checked_without_coercion(self):
        identifier = uuid4()
        self.assertEqual(
            problem_of(self, Reference, "task", identifier),
            ("kind", InputProblem.WRONG_TYPE),
        )
        self.assertEqual(
            problem_of(self, Reference, None, identifier),
            ("kind", InputProblem.REQUIRED),
        )
        self.assertEqual(
            problem_of(self, Reference, ReferenceKind.TASK, str(identifier)),
            ("id", InputProblem.WRONG_TYPE),
        )
        self.assertEqual(
            problem_of(self, Reference.answer, None), ("id", InputProblem.REQUIRED)
        )


class RelationTest(unittest.TestCase):
    def relation(self, low: UUID, high: UUID) -> Relation:
        return Relation(
            entity=EntityKind.CLAIM,
            kind=RelationKind.DUPLICATE,
            project_id=uuid4(),
            low_id=low,
            high_id=high,
            created_by=uuid4(),
            created_at=T0,
        )

    def test_other_returns_the_opposite_endpoint(self):
        low, high = UUID(int=1), UUID(int=2)
        relation = self.relation(low, high)

        self.assertEqual(relation.other(low), high)
        self.assertEqual(relation.other(high), low)

    def test_other_refuses_an_id_that_is_not_an_endpoint_without_echoing_it(self):
        stranger = UUID("12345678-1234-5678-1234-567812345678")
        with self.assertRaises(ValueError) as raised:
            self.relation(UUID(int=1), UUID(int=2)).other(stranger)

        self.assertNotIn(str(stranger), str(raised.exception))
        self.assertEqual(str(raised.exception), "not an endpoint of this relation")


class TraceSourcesTest(unittest.TestCase):
    def traced(self, *sources: Source, claim_id: UUID | None = None) -> TracedClaim:
        claim = Claim(
            id=claim_id or uuid4(),
            project_id=uuid4(),
            text="c",
            task_id=None,
            created_by=uuid4(),
            created_at=T0,
        )
        links = tuple(SourceLink(claim.id, s, Stance.SUPPORTS, T0) for s in sources)
        return TracedClaim(claim=claim, links=links, relations=())

    def test_the_sources_of_a_trace_are_distinct_and_in_order_of_appearance(self):
        first, second, third = make_source(), make_source(), make_source()
        trace = Trace(
            reference=Reference.answer(uuid4()),
            claims=(self.traced(second, first), self.traced(first, third)),
            truncated=False,
        )

        self.assertEqual(trace.sources, (second, first, third))

    def test_a_trace_without_claims_has_no_sources(self):
        trace = Trace(reference=Reference.task(uuid4()), claims=(), truncated=False)

        self.assertEqual(trace.sources, ())


class RecordedClaimTest(unittest.TestCase):
    def test_the_values_are_immutable(self):
        claim = Claim(uuid4(), uuid4(), "c", None, uuid4(), T0)
        recorded = RecordedClaim(claim=claim, created=True, links=(), new_links=0)

        with self.assertRaises(dataclasses.FrozenInstanceError):
            recorded.created = False
        with self.assertRaises(dataclasses.FrozenInstanceError):
            claim.text = "changed"


if __name__ == "__main__":
    unittest.main()
