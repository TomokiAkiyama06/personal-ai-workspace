"""``mark_related`` and ``list_relations`` on a real PostgreSQL.

A relation is symmetric (stored once as an ordered pair), a pair has at most one
relation, and both ends must exist in the caller's project.
"""

from datetime import timedelta
from random import Random
from uuid import UUID, uuid4

from paw_backend.research.provenance import (
    ClaimNotFoundError,
    EntityKind,
    InputProblem,
    InvalidProvenanceInputError,
    ProvenanceConflictError,
    Relation,
    RelationKind,
    SourceNotFoundError,
)

from .provenance_support import (
    T0,
    PostgresProvenanceTestCase,
    requires_postgres,
)

HOUR = timedelta(hours=1)
TABLES = {
    EntityKind.CLAIM: "research_claim_relations",
    EntityKind.SOURCE: "research_source_relations",
}
NOT_FOUND = {
    EntityKind.CLAIM: ClaimNotFoundError,
    EntityKind.SOURCE: SourceNotFoundError,
}


@requires_postgres
class RelationTestCase(PostgresProvenanceTestCase):
    def seed_entity(self, entity: EntityKind, name: str = "x", project=None) -> UUID:
        if entity is EntityKind.CLAIM:
            return self.seed_claim(f"claim {name} {uuid4()}", project_id=project)
        return self.seed_source(
            locator=f"https://example.com/{name}/{uuid4()}", project_id=project
        )

    async def mark(self, entity, kind, first, second, **options):
        arguments = {"created_by": self.user_id}
        arguments.update(options)
        project = arguments.pop("project_id", self.project_id)
        return await self.store.mark_related(
            project,
            entity=entity,
            kind=kind,
            first_id=first,
            second_id=second,
            **arguments,
        )

    def relation_rows(self, entity: EntityKind):
        return self.rows(f"SELECT * FROM {TABLES[entity]} ORDER BY low_id, high_id")

    def expected(self, entity, kind, first, second, **overrides) -> Relation:
        low, high = sorted((first, second), key=lambda value: value.int)
        values = {
            "entity": entity,
            "kind": kind,
            "project_id": self.project_id,
            "low_id": low,
            "high_id": high,
            "created_by": self.user_id,
            "created_at": T0,
        }
        values.update(overrides)
        return Relation(**values)


class MarkRelatedTest(RelationTestCase):
    async def test_a_relation_is_recorded_for_claims_and_for_sources(self):
        for entity in EntityKind:
            for kind in RelationKind:
                with self.subTest(entity=entity, kind=kind):
                    self.clean_tables()
                    first, second = self.seed_entity(entity), self.seed_entity(entity)

                    relation = await self.mark(entity, kind, first, second)

                    self.assertEqual(
                        relation, self.expected(entity, kind, first, second)
                    )
                    (row,) = self.relation_rows(entity)
                    self.assertEqual(
                        (row["low_id"], row["high_id"], row["kind"], row["project_id"]),
                        (
                            relation.low_id,
                            relation.high_id,
                            kind.value,
                            self.project_id,
                        ),
                    )
                    other = TABLES[
                        EntityKind.SOURCE
                        if entity is EntityKind.CLAIM
                        else EntityKind.CLAIM
                    ]
                    self.assertEqual(self.table_count(other), 0)

    async def test_the_pair_is_stored_ordered_whichever_order_it_was_named_in(self):
        random = Random(52)
        for number in range(12):
            with self.subTest(number):
                first, second = (
                    self.seed_claim(f"a{number}"),
                    self.seed_claim(f"b{number}"),
                )
                if random.random() < 0.5:
                    first, second = second, first

                relation = await self.mark(
                    EntityKind.CLAIM, RelationKind.DUPLICATE, first, second
                )

                self.assertLess(relation.low_id.int, relation.high_id.int)
                self.assertEqual({relation.low_id, relation.high_id}, {first, second})

    async def test_naming_the_pair_the_other_way_round_is_the_same_relation(self):
        first, second = self.seed_claim("a"), self.seed_claim("b")
        original = await self.mark(
            EntityKind.CLAIM, RelationKind.DUPLICATE, first, second
        )
        self.clock.advance(hours=2)

        again = await self.mark(
            EntityKind.CLAIM, RelationKind.DUPLICATE, second, first, created_by=uuid4()
        )

        self.assertEqual(again, original)
        self.assertEqual(again.created_by, self.user_id)
        self.assertEqual(again.created_at, T0)
        self.assertEqual(len(self.relation_rows(EntityKind.CLAIM)), 1)

    async def test_marking_the_same_relation_twice_returns_the_first_one(self):
        first, second = self.seed_claim("a"), self.seed_claim("b")
        original = await self.mark(
            EntityKind.CLAIM, RelationKind.CONTRADICTION, first, second
        )
        self.clock.advance(hours=1)

        again = await self.mark(
            EntityKind.CLAIM, RelationKind.CONTRADICTION, first, second
        )

        self.assertEqual(again, original)
        self.assertEqual(len(self.relation_rows(EntityKind.CLAIM)), 1)

    async def test_the_other_kind_for_the_same_pair_is_a_conflict(self):
        for entity in EntityKind:
            for first_kind, second_kind in (
                (RelationKind.DUPLICATE, RelationKind.CONTRADICTION),
                (RelationKind.CONTRADICTION, RelationKind.DUPLICATE),
            ):
                with self.subTest(entity=entity, kind=first_kind):
                    self.clean_tables()
                    first, second = self.seed_entity(entity), self.seed_entity(entity)
                    await self.mark(entity, first_kind, first, second)

                    with self.assertRaises(ProvenanceConflictError) as raised:
                        await self.mark(entity, second_kind, second, first)

                    self.assertNotIn(str(first), str(raised.exception))
                    (row,) = self.relation_rows(entity)
                    self.assertEqual(row["kind"], first_kind.value)

    async def test_an_entity_can_be_related_to_several_others(self):
        hub = self.seed_claim("hub")
        others = [self.seed_claim(f"other {n}") for n in range(3)]

        await self.mark(EntityKind.CLAIM, RelationKind.DUPLICATE, hub, others[0])
        await self.mark(EntityKind.CLAIM, RelationKind.CONTRADICTION, others[1], hub)
        await self.mark(EntityKind.CLAIM, RelationKind.DUPLICATE, hub, others[2])

        self.assertEqual(len(self.relation_rows(EntityKind.CLAIM)), 3)

    async def test_a_thing_is_not_related_to_itself_and_nothing_is_written(self):
        claim = self.seed_claim("a")

        with self.assertRaises(InvalidProvenanceInputError) as raised:
            await self.mark(EntityKind.CLAIM, RelationKind.DUPLICATE, claim, claim)

        self.assertEqual(
            (raised.exception.field, raised.exception.problem),
            ("second_id", InputProblem.SELF_REFERENCE),
        )
        self.assertEqual(self.relation_rows(EntityKind.CLAIM), [])

    async def test_a_missing_end_is_not_found_and_nothing_is_written(self):
        for entity in EntityKind:
            with self.subTest(entity):
                real = self.seed_entity(entity)
                for first, second in (
                    (real, uuid4()),
                    (uuid4(), real),
                    (uuid4(), uuid4()),
                ):
                    with self.assertRaises(NOT_FOUND[entity]):
                        await self.mark(entity, RelationKind.DUPLICATE, first, second)
                self.assertEqual(self.relation_rows(entity), [])

    async def test_ids_of_the_other_kind_of_entity_are_not_found(self):
        claims = self.seed_claim("a"), self.seed_claim("b")
        sources = (
            self.seed_entity(EntityKind.SOURCE),
            self.seed_entity(EntityKind.SOURCE),
        )

        with self.assertRaises(SourceNotFoundError):
            await self.mark(EntityKind.SOURCE, RelationKind.DUPLICATE, *claims)
        with self.assertRaises(ClaimNotFoundError):
            await self.mark(EntityKind.CLAIM, RelationKind.DUPLICATE, *sources)

        self.assertEqual(self.counts()["research_claim_relations"], 0)
        self.assertEqual(self.counts()["research_source_relations"], 0)

    async def test_an_end_that_belongs_to_another_project_is_not_found(self):
        for entity in EntityKind:
            with self.subTest(entity):
                mine = self.seed_entity(entity)
                theirs = self.seed_entity(entity, project=self.other_project_id)

                with self.assertRaises(NOT_FOUND[entity]):
                    await self.mark(entity, RelationKind.DUPLICATE, mine, theirs)
                with self.assertRaises(NOT_FOUND[entity]):
                    await self.mark(entity, RelationKind.DUPLICATE, theirs, mine)
                # ... and the other project cannot relate its own things through us.
                with self.assertRaises(NOT_FOUND[entity]):
                    await self.mark(
                        entity,
                        RelationKind.DUPLICATE,
                        theirs,
                        self.seed_entity(entity, project=self.other_project_id),
                    )
                self.assertEqual(self.relation_rows(entity), [])

    async def test_the_relation_of_two_things_of_the_other_project_is_theirs(self):
        first = self.seed_entity(EntityKind.CLAIM, project=self.other_project_id)
        second = self.seed_entity(EntityKind.CLAIM, project=self.other_project_id)

        relation = await self.mark(
            EntityKind.CLAIM,
            RelationKind.DUPLICATE,
            first,
            second,
            project_id=self.other_project_id,
        )

        self.assertEqual(relation.project_id, self.other_project_id)
        (row,) = self.relation_rows(EntityKind.CLAIM)
        self.assertEqual(row["project_id"], self.other_project_id)

    async def test_the_creator_and_the_clock_time_are_recorded(self):
        creator = uuid4()
        first, second = self.seed_claim("a"), self.seed_claim("b")
        self.clock.advance(hours=7)

        relation = await self.mark(
            EntityKind.CLAIM, RelationKind.DUPLICATE, first, second, created_by=creator
        )

        self.assertEqual(relation.created_by, creator)
        self.assertEqual(relation.created_at, T0 + 7 * HOUR)
        (row,) = self.relation_rows(EntityKind.CLAIM)
        self.assertEqual(
            (row["created_by"], row["created_at"]), (creator, T0 + 7 * HOUR)
        )


class ListRelationsTest(RelationTestCase):
    async def test_the_relations_of_a_claim_are_ordered(self):
        hub = self.seed_claim("hub")
        others = sorted(
            (self.seed_claim(f"other {n}") for n in range(4)), key=lambda v: v.int
        )
        self.seed_relation("claim", hub, others[3], "duplicate")
        self.seed_relation("claim", others[0], hub, "duplicate")
        self.seed_relation("claim", hub, others[2], "contradiction")
        self.seed_relation("claim", others[1], hub, "contradiction")

        relations = await self.store.list_relations(
            self.project_id, EntityKind.CLAIM, hub
        )

        self.assertIsInstance(relations, tuple)
        self.assertEqual(
            [(r.kind, r.other(hub)) for r in relations],
            [
                (RelationKind.CONTRADICTION, others[1]),
                (RelationKind.CONTRADICTION, others[2]),
                (RelationKind.DUPLICATE, others[0]),
                (RelationKind.DUPLICATE, others[3]),
            ],
        )

    async def test_the_relations_of_a_source(self):
        first, second = (
            self.seed_entity(EntityKind.SOURCE),
            self.seed_entity(EntityKind.SOURCE),
        )
        self.seed_relation(
            "source", first, second, "contradiction", created_at=T0 + HOUR
        )

        from_first = await self.store.list_relations(
            self.project_id, EntityKind.SOURCE, first
        )
        from_second = await self.store.list_relations(
            self.project_id, EntityKind.SOURCE, second
        )

        self.assertEqual(from_first, from_second)
        self.assertEqual(
            from_first,
            (
                self.expected(
                    EntityKind.SOURCE,
                    RelationKind.CONTRADICTION,
                    first,
                    second,
                    created_at=T0 + HOUR,
                ),
            ),
        )

    async def test_a_relation_recorded_by_mark_related_is_listed(self):
        first, second = self.seed_claim("a"), self.seed_claim("b")
        await self.store.mark_related(
            self.project_id,
            entity=EntityKind.CLAIM,
            kind=RelationKind.DUPLICATE,
            first_id=second,
            second_id=first,
            created_by=self.user_id,
        )

        for claim in (first, second):
            relations = await self.store.list_relations(
                self.project_id, EntityKind.CLAIM, claim
            )
            self.assertEqual(
                relations,
                (
                    self.expected(
                        EntityKind.CLAIM, RelationKind.DUPLICATE, first, second
                    ),
                ),
            )

    async def test_an_entity_without_relations_has_an_empty_tuple(self):
        claim = self.seed_claim("alone")

        self.assertEqual(
            await self.store.list_relations(self.project_id, EntityKind.CLAIM, claim),
            (),
        )

    async def test_claims_and_sources_have_separate_relations(self):
        claim_a, claim_b = self.seed_claim("a"), self.seed_claim("b")
        self.seed_relation("claim", claim_a, claim_b, "duplicate")

        with self.assertRaises(SourceNotFoundError):
            await self.store.list_relations(self.project_id, EntityKind.SOURCE, claim_a)

    async def test_an_unknown_id_is_not_found(self):
        with self.assertRaises(ClaimNotFoundError):
            await self.store.list_relations(self.project_id, EntityKind.CLAIM, uuid4())
        with self.assertRaises(SourceNotFoundError):
            await self.store.list_relations(self.project_id, EntityKind.SOURCE, uuid4())

    async def test_an_entity_of_another_project_is_not_found(self):
        for entity in EntityKind:
            with self.subTest(entity):
                theirs = self.seed_entity(entity, project=self.other_project_id)

                with self.assertRaises(NOT_FOUND[entity]):
                    await self.store.list_relations(self.project_id, entity, theirs)

    async def test_relations_of_other_projects_are_not_listed(self):
        mine = self.seed_claim("mine")
        theirs_a = self.seed_claim("ta", project_id=self.other_project_id)
        theirs_b = self.seed_claim("tb", project_id=self.other_project_id)
        self.seed_relation(
            "claim", theirs_a, theirs_b, "duplicate", project_id=self.other_project_id
        )

        self.assertEqual(
            await self.store.list_relations(self.project_id, EntityKind.CLAIM, mine), ()
        )
        self.assertEqual(
            len(
                await self.store.list_relations(
                    self.other_project_id, EntityKind.CLAIM, theirs_a
                )
            ),
            1,
        )
