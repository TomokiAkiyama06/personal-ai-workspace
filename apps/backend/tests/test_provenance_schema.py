"""Provenance schema: constraints and project separation (real PostgreSQL).

Skipped unless ``PAW_TEST_DATABASE_URL`` is set. Every constraint is asserted by
name: a violation that fires on the wrong constraint would otherwise pass for
the wrong reason.
"""

import hashlib
from datetime import UTC, datetime
from functools import partial
from random import Random
from uuid import UUID, uuid4

from sqlalchemy import insert, text
from sqlalchemy.exc import IntegrityError

from paw_backend.research.provenance.models import (
    ClaimRelationRow,
    ClaimRow,
    ClaimSourceRow,
    ClaimUseRow,
    SourceRelationRow,
    SourceRow,
)

from .memory_support import MemoryDatabaseTestCase, requires_postgres

CREATED = datetime(2026, 9, 24, tzinfo=UTC)
GOOD_HASH = "sha256:" + "a" * 64
SRC = "ck_research_sources_"
CLM = "ck_research_claims_"


def source_values(**overrides):
    values = {
        "project_id": uuid4(),
        "locator": "https://example.com/a",
        "source_type": "primary",
        "content_hash": GOOD_HASH,
        "fetched_at": CREATED,
        "created_at": CREATED,
    }
    values.update(overrides)
    return values


def claim_values(**overrides):
    text_value = overrides.get("claim_text", "A claim")
    values = {
        "project_id": uuid4(),
        "created_by": uuid4(),
        "claim_text": text_value,
        "text_fingerprint": hashlib.sha256(text_value.encode()).hexdigest(),
        "created_at": CREATED,
    }
    values.update(overrides)
    return values


@requires_postgres
class ProvenanceSchemaTestCase(MemoryDatabaseTestCase):
    def add_source(self, **overrides):
        return self.session.execute(
            insert(SourceRow)
            .values(**source_values(**overrides))
            .returning(SourceRow.id)
        ).scalar_one()

    def try_source(self, **overrides):
        return self.violation(partial(self.add_source, **overrides))

    def add_claim(self, **overrides):
        return self.session.execute(
            insert(ClaimRow).values(**claim_values(**overrides)).returning(ClaimRow.id)
        ).scalar_one()

    def try_claim(self, **overrides):
        return self.violation(partial(self.add_claim, **overrides))

    def add_task(self, project_id=None):
        task_id = uuid4()
        self.session.execute(
            text(
                "INSERT INTO tasks (id, project_id, created_by, title, input, state,"
                " attempt, retry_count, version, created_at, updated_at)"
                " VALUES (:id, :project, :user, 'T', CAST('{}' AS jsonb), 'queued',"
                " 1, 0, 1, :now, :now)"
            ),
            {
                "id": task_id,
                "project": project_id or uuid4(),
                "user": uuid4(),
                "now": CREATED,
            },
        )
        return task_id

    def project_claim(self, project_id, label="c"):
        return self.add_claim(project_id=project_id, claim_text=f"{label} {uuid4()}")

    def project_source(self, project_id):
        return self.add_source(project_id=project_id, locator=f"https://a.io/{uuid4()}")

    def one(self, sql, **parameters):
        return self.session.execute(text(sql), parameters).mappings().one()


class SourceConstraintTest(ProvenanceSchemaTestCase):
    def test_defaults_of_a_new_source(self):
        source_id = self.add_source()

        row = self.one("SELECT * FROM research_sources WHERE id = :id", id=source_id)

        self.assertEqual(row["title"], "")
        self.assertIsNone(row["published_at"])
        self.assertIsNotNone(row["id"])

    def test_a_source_is_unique_per_project_locator_and_content_hash(self):
        project = uuid4()
        self.assertIsNone(self.try_source(project_id=project))
        self.assertEqual(
            self.try_source(project_id=project), "uq_research_sources_project_id"
        )
        # Another project, another locator, another content: all different sources.
        self.assertIsNone(self.try_source(project_id=uuid4()))
        self.assertIsNone(
            self.try_source(project_id=project, locator="https://example.com/b")
        )
        self.assertIsNone(
            self.try_source(project_id=project, content_hash="sha256:" + "b" * 64)
        )

    def test_source_type_takes_only_the_six_values(self):
        for value in (
            "official_docs",
            "official_github",
            "primary",
            "secondary",
            "community",
            "unknown",
        ):
            with self.subTest(value):
                self.assertIsNone(self.try_source(source_type=value))
        for value in ("Primary", "official", "", "blog"):
            with self.subTest(value):
                self.assertEqual(
                    self.try_source(source_type=value), SRC + "source_type_valid"
                )

    def test_the_locator_length_is_bounded_and_a_maximal_locator_is_indexable(self):
        longest = "https://example.com/" + "x" * (2048 - len("https://example.com/"))
        self.assertEqual(len(longest), 2048)
        self.assertIsNone(self.try_source(locator=longest))
        self.assertEqual(self.try_source(locator=longest + "x"), SRC + "locator_length")
        # A locator whose every character is a percent escape (the widest
        # canonical form) still fits the unique index.
        escaped = "https://example.com/" + "%C3%A9" * 338
        self.assertIsNone(self.try_source(locator=escaped[:2048]))

    def test_the_locator_must_be_a_printable_ascii_http_url(self):
        self.assertIsNone(self.try_source(locator="http://example.com/"))
        for bad in (
            "ftp://example.com/",
            "example.com/a",
            "https://",
            "https://example.com/a b",
            "https://example.com/é",
            "https://example.com/あ",
            "https://example.com/a\n",
            "javascript:alert(1)",
        ):
            with self.subTest(bad):
                self.assertEqual(self.try_source(locator=bad), SRC + "locator_shape")

    def test_the_title_is_bounded_and_may_be_empty(self):
        self.assertIsNone(self.try_source(title=""))
        self.assertIsNone(self.try_source(title="あ" * 300, locator="https://a.io/1"))
        self.assertEqual(
            self.try_source(title="あ" * 301, locator="https://a.io/2"),
            SRC + "title_length",
        )

    def test_the_content_hash_is_sha256_and_64_lowercase_hex_digits(self):
        for bad in (
            "sha256:" + "A" * 64,
            "sha256:" + "a" * 63,
            "sha256:" + "a" * 65,
            "sha256:" + "g" * 64,
            "sha1:" + "a" * 64,
            "a" * 64,
            "sha256:" + "a" * 64 + "\n",
            "",
        ):
            with self.subTest(bad):
                self.assertEqual(
                    self.try_source(content_hash=bad), SRC + "content_hash_format"
                )

    def test_dates_are_required_except_published_at(self):
        for column in ("fetched_at", "created_at", "locator", "source_type"):
            with self.subTest(column):
                values = source_values()
                del values[column]
                with self.assertRaises(IntegrityError) as raised:
                    with self.session.begin_nested():
                        self.session.execute(insert(SourceRow).values(**values))
                self.assertEqual(raised.exception.orig.diag.column_name, column)
        self.assertIsNone(self.try_source(published_at=None))


class ClaimConstraintTest(ProvenanceSchemaTestCase):
    def test_a_claim_is_unique_per_project_and_fingerprint(self):
        project = uuid4()
        self.assertIsNone(self.try_claim(project_id=project))
        self.assertEqual(
            self.try_claim(project_id=project), "uq_research_claims_project_id"
        )
        self.assertIsNone(self.try_claim(project_id=uuid4()))
        self.assertIsNone(
            self.try_claim(project_id=project, claim_text="Another claim")
        )

    def test_the_claim_text_is_bounded_in_characters(self):
        self.assertIsNone(self.try_claim(claim_text="あ" * 2000))
        self.assertEqual(
            self.try_claim(claim_text="あ" * 2001), CLM + "claim_text_length"
        )
        self.assertEqual(self.try_claim(claim_text=""), CLM + "claim_text_length")

    def test_the_fingerprint_is_64_lowercase_hex_digits(self):
        for bad in ("", "a" * 63, "a" * 65, "A" * 64, "g" * 64, "sha256:" + "a" * 64):
            with self.subTest(bad):
                self.assertEqual(
                    self.try_claim(text_fingerprint=bad),
                    CLM + "text_fingerprint_format",
                )

    def test_who_and_when_are_required(self):
        for column in ("created_by", "created_at", "claim_text", "text_fingerprint"):
            with self.subTest(column):
                values = claim_values()
                del values[column]
                with self.assertRaises(IntegrityError) as raised:
                    with self.session.begin_nested():
                        self.session.execute(insert(ClaimRow).values(**values))
                self.assertEqual(raised.exception.orig.diag.column_name, column)

    def test_the_task_must_exist_and_deleting_it_keeps_the_claim(self):
        task_id = self.add_task()
        claim_id = self.add_claim(task_id=task_id)

        self.assertEqual(
            self.try_claim(task_id=uuid4(), claim_text="Unknown task"),
            "fk_research_claims_task_id_tasks",
        )

        self.session.execute(text("DELETE FROM tasks WHERE id = :id"), {"id": task_id})
        row = self.one("SELECT * FROM research_claims WHERE id = :id", id=claim_id)
        self.assertIsNone(row["task_id"])


class LinkConstraintTest(ProvenanceSchemaTestCase):
    def add_link(self, claim_id, source_id, project_id, **overrides):
        values = {
            "claim_id": claim_id,
            "source_id": source_id,
            "project_id": project_id,
            "stance": "supports",
            "created_at": CREATED,
        }
        values.update(overrides)
        self.session.execute(insert(ClaimSourceRow).values(**values))

    def test_a_claim_and_a_source_are_linked_once(self):
        project = uuid4()
        claim, source = self.project_claim(project), self.project_source(project)
        self.assertIsNone(
            self.violation(partial(self.add_link, claim, source, project))
        )
        self.assertEqual(
            self.violation(
                partial(self.add_link, claim, source, project, stance="contradicts")
            ),
            "pk_research_claim_sources",
        )

    def test_stance_takes_only_the_two_values(self):
        project = uuid4()
        claim = self.project_claim(project)
        for stance in ("supports", "contradicts"):
            with self.subTest(stance):
                source = self.project_source(project)
                self.assertIsNone(
                    self.violation(
                        partial(self.add_link, claim, source, project, stance=stance)
                    )
                )
        for stance in ("support", "Supports", "neutral", ""):
            with self.subTest(stance):
                source = self.project_source(project)
                self.assertEqual(
                    self.violation(
                        partial(self.add_link, claim, source, project, stance=stance)
                    ),
                    "ck_research_claim_sources_stance_valid",
                )

    def test_a_link_cannot_join_two_projects(self):
        first, second = uuid4(), uuid4()
        claim_a, source_a = self.project_claim(first), self.project_source(first)
        claim_b, source_b = self.project_claim(second), self.project_source(second)

        # The link says project ``first``: the source of ``second`` is not there.
        self.assertEqual(
            self.violation(partial(self.add_link, claim_a, source_b, first)),
            "fk_research_claim_sources_source_id_research_sources",
        )
        # ... and the claim of ``second`` is not there either.
        self.assertEqual(
            self.violation(partial(self.add_link, claim_b, source_a, first)),
            "fk_research_claim_sources_claim_id_research_claims",
        )
        self.assertIsNone(
            self.violation(partial(self.add_link, claim_b, source_b, second))
        )

    def test_a_missing_claim_or_source_is_refused(self):
        project = uuid4()
        claim, source = self.project_claim(project), self.project_source(project)
        self.assertEqual(
            self.violation(partial(self.add_link, uuid4(), source, project)),
            "fk_research_claim_sources_claim_id_research_claims",
        )
        self.assertEqual(
            self.violation(partial(self.add_link, claim, uuid4(), project)),
            "fk_research_claim_sources_source_id_research_sources",
        )


class UseConstraintTest(ProvenanceSchemaTestCase):
    def add_use(self, claim_id, project_id, ref_kind="answer", ref_id=None, **more):
        values = {
            "claim_id": claim_id,
            "ref_kind": ref_kind,
            "ref_id": ref_id or uuid4(),
            "project_id": project_id,
            "created_by": uuid4(),
            "created_at": CREATED,
        }
        values.update(more)
        self.session.execute(insert(ClaimUseRow).values(**values))

    def test_a_reference_uses_a_claim_once_per_kind(self):
        project = uuid4()
        claim = self.project_claim(project)
        reference = uuid4()
        self.assertIsNone(
            self.violation(partial(self.add_use, claim, project, "answer", reference))
        )
        self.assertEqual(
            self.violation(partial(self.add_use, claim, project, "answer", reference)),
            "pk_research_claim_uses",
        )
        # The same id as a task is another reference.
        self.assertIsNone(
            self.violation(partial(self.add_use, claim, project, "task", reference))
        )

    def test_the_kind_is_answer_or_task(self):
        project = uuid4()
        claim = self.project_claim(project)
        for kind in ("Answer", "message", ""):
            with self.subTest(kind):
                self.assertEqual(
                    self.violation(partial(self.add_use, claim, project, kind)),
                    "ck_research_claim_uses_ref_kind_valid",
                )

    def test_a_use_cannot_name_a_claim_of_another_project(self):
        project, other = uuid4(), uuid4()
        claim = self.project_claim(project)
        self.assertEqual(
            self.violation(partial(self.add_use, claim, other)),
            "fk_research_claim_uses_claim_id_research_claims",
        )


class RelationConstraintTest(ProvenanceSchemaTestCase):
    """The claim and the source relation tables have the same rules."""

    CASES = (
        ("claim", ClaimRelationRow, "research_claim_relations", "research_claims"),
        ("source", SourceRelationRow, "research_source_relations", "research_sources"),
    )

    def add_entity(self, entity, project_id):
        if entity == "claim":
            return self.project_claim(project_id)
        return self.project_source(project_id)

    def add_relation(self, model, low, high, project_id, kind="duplicate"):
        self.session.execute(
            insert(model).values(
                low_id=low,
                high_id=high,
                project_id=project_id,
                kind=kind,
                created_by=uuid4(),
                created_at=CREATED,
            )
        )

    def pair(self, entity, project_id):
        first, second = (
            self.add_entity(entity, project_id),
            self.add_entity(entity, project_id),
        )
        return tuple(sorted((first, second), key=lambda value: value.int))

    def test_the_pair_is_stored_ordered(self):
        for entity, model, table, _ in self.CASES:
            with self.subTest(entity):
                project = uuid4()
                low, high = self.pair(entity, project)
                name = f"ck_{table}_ordered_pair"
                self.assertEqual(
                    self.violation(
                        partial(self.add_relation, model, high, low, project)
                    ),
                    name,
                )
                self.assertEqual(
                    self.violation(
                        partial(self.add_relation, model, low, low, project)
                    ),
                    name,
                )
                self.assertIsNone(
                    self.violation(
                        partial(self.add_relation, model, low, high, project)
                    )
                )

    def test_a_pair_has_one_relation_whatever_its_kind(self):
        for entity, model, table, _ in self.CASES:
            with self.subTest(entity):
                project = uuid4()
                low, high = self.pair(entity, project)
                self.add_relation(model, low, high, project, "duplicate")
                for kind in ("duplicate", "contradiction"):
                    self.assertEqual(
                        self.violation(
                            partial(self.add_relation, model, low, high, project, kind)
                        ),
                        f"pk_{table}",
                    )

    def test_the_kind_is_duplicate_or_contradiction(self):
        for entity, model, table, _ in self.CASES:
            with self.subTest(entity):
                project = uuid4()
                low, high = self.pair(entity, project)
                for kind in ("contradiction", "duplicate"):
                    third = self.add_entity(entity, project)
                    a, b = sorted((low, third), key=lambda value: value.int)
                    self.assertIsNone(
                        self.violation(
                            partial(self.add_relation, model, a, b, project, kind)
                        )
                    )
                for kind in ("Duplicate", "related", ""):
                    self.assertEqual(
                        self.violation(
                            partial(self.add_relation, model, low, high, project, kind)
                        ),
                        f"ck_{table}_kind_valid",
                    )

    def test_a_relation_cannot_join_two_projects(self):
        for entity, model, table, _ in self.CASES:
            with self.subTest(entity):
                first, second = uuid4(), uuid4()
                a = self.add_entity(entity, first)
                b = self.add_entity(entity, second)
                low, high = sorted((a, b), key=lambda value: value.int)
                # Whichever project the row names, one endpoint is not in it.
                for project in (first, second):
                    error = self.violation(
                        partial(self.add_relation, model, low, high, project)
                    )
                    self.assertIn(
                        error,
                        {
                            f"fk_{table}_low_id_{_referred(table)}",
                            f"fk_{table}_high_id_{_referred(table)}",
                        },
                    )

    def test_a_missing_endpoint_is_refused(self):
        for entity, model, table, _ in self.CASES:
            with self.subTest(entity):
                project = uuid4()
                real = self.add_entity(entity, project)
                ghost = uuid4()
                low, high = sorted((real, ghost), key=lambda value: value.int)
                self.assertIn(
                    self.violation(
                        partial(self.add_relation, model, low, high, project)
                    ),
                    {
                        f"fk_{table}_low_id_{_referred(table)}",
                        f"fk_{table}_high_id_{_referred(table)}",
                    },
                )


def _referred(table: str) -> str:
    return (
        "research_claims" if table == "research_claim_relations" else "research_sources"
    )


class UuidOrderTest(ProvenanceSchemaTestCase):
    def test_postgresql_orders_uuids_like_their_integer_value(self):
        # ``order_pair`` (Python) and ``CHECK (low_id < high_id)`` (PostgreSQL)
        # must agree, or a correctly ordered pair would be refused.
        random = Random(52)
        edge = [UUID(int=0), UUID(int=1), UUID(int=2**127), UUID(int=2**128 - 1)]
        ids = edge + [UUID(int=random.getrandbits(128)) for _ in range(60)]
        for _ in range(200):
            first, second = random.choice(ids), random.choice(ids)
            database = self.session.execute(
                text("SELECT CAST(:a AS uuid) < CAST(:b AS uuid)"),
                {"a": str(first), "b": str(second)},
            ).scalar_one()
            self.assertEqual(database, first.int < second.int)
        self.assertTrue(
            self.session.execute(
                text("SELECT CAST(:a AS uuid) < CAST(:b AS uuid)"),
                {"a": str(UUID(int=2**127 - 1)), "b": str(UUID(int=2**127))},
            ).scalar_one()
        )
