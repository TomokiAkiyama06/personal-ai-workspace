"""The pure rules of the provenance store: normalisation, fingerprint, pair
order, source merging and the assembly of a trace (no database, no clock)."""

import time
import unittest
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

from paw_backend.research.provenance import (
    Claim,
    EntityKind,
    InputProblem,
    InvalidProvenanceInputError,
    Relation,
    RelationKind,
    Source,
    SourceLink,
    Stance,
    TracedClaim,
    assemble_traced_claims,
    claim_fingerprint,
    merge_duplicate_sources,
    normalize_claim_text,
    order_links,
    order_pair,
    order_relations,
)
from paw_backend.research.providers.contract import SourceType

from .provenance_support import T0, content_hash, link

PROJECT = UUID("aaaaaaaa-0000-0000-0000-000000000001")
USER = UUID("bbbbbbbb-0000-0000-0000-000000000001")
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
ABC_SHA256 = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def uid(number: int) -> UUID:
    return UUID(int=number)


class NormalizeClaimTextTest(unittest.TestCase):
    def test_the_documented_examples(self):
        cases = {
            "  The  Sky\tis BLUE. ": "the sky is blue.",
            "Ｒｕｓｔ　１.７５": "rust 1.75",
            "Straße": "strasse",
            "ﬁne": "fine",
            "Café": "café",
            "a b\r\nc": "a b c",
        }
        for value, expected in cases.items():
            with self.subTest(value):
                self.assertEqual(normalize_claim_text(value), expected)

    def test_every_kind_of_whitespace_collapses_to_one_space(self):
        for space in (" ", "\t", "\n", "\r\n", " ", " ", "　", " ", "\x0b"):
            with self.subTest(repr(space)):
                self.assertEqual(
                    normalize_claim_text(f"{space}a{space}{space}b{space}"), "a b"
                )

    def test_a_text_without_visible_characters_becomes_empty(self):
        for value in ("", " ", " \t\n　 "):
            with self.subTest(repr(value)):
                self.assertEqual(normalize_claim_text(value), "")

    def test_nothing_but_case_width_and_whitespace_changes(self):
        text = "Rust 1.75 is stable; isn't it? (Yes) - A/B, C_D!"
        self.assertEqual(normalize_claim_text(text), text.lower())
        self.assertEqual(normalize_claim_text("b a"), "b a")
        self.assertNotEqual(normalize_claim_text("a b"), normalize_claim_text("b a"))

    def test_accents_are_kept_and_composed_and_decomposed_forms_agree(self):
        self.assertEqual(normalize_claim_text("Café"), "café")
        self.assertEqual(normalize_claim_text("Café"), "café")
        self.assertNotEqual(normalize_claim_text("cafe"), normalize_claim_text("café"))

    def test_case_folding_is_used_not_lower_casing(self):
        self.assertEqual(
            normalize_claim_text("STRASSE"), normalize_claim_text("Straße")
        )
        # A final sigma folds to a plain sigma; lower() would keep it.
        self.assertEqual(normalize_claim_text("ς"), "σ")
        self.assertEqual(normalize_claim_text("İ"), "i̇")

    def test_nfkc_is_applied_again_after_case_folding(self):
        # Folding U+01F0 gives "j" + a combining caron, which NFKC composes again.
        self.assertEqual(normalize_claim_text("ǰ"), "ǰ")
        self.assertEqual(normalize_claim_text("ǰ"), "ǰ")
        self.assertEqual(normalize_claim_text("ΐ"), "ΐ")

    def test_nfkc_is_applied_before_case_folding(self):
        # "MODIFIER LETTER CAPITAL A" is "A" in NFKC: it must then fold to "a".
        # (Folding first would leave it alone, and the later NFKC gives "A".)
        self.assertEqual(normalize_claim_text("\u1d2c"), "a")
        self.assertEqual(normalize_claim_text("\u1d2c\u1d2e"), "ab")
        self.assertEqual(normalize_claim_text("\u03d2"), "\u03c5")

    def test_the_function_is_idempotent(self):
        for value in (
            "  The  Sky\tis BLUE. ",
            "Ｒｕｓｔ　１.７５",
            "Straße ﬁne İstanbul",
            "ǰ ΐ ẖ",
            "ΣΑΣ ς",
            "",
        ):
            with self.subTest(value):
                once = normalize_claim_text(value)
                self.assertEqual(normalize_claim_text(once), once)

    def test_only_a_str_is_accepted(self):
        for bad in (None, b"abc", 5, ["a"], object()):
            with self.subTest(type(bad).__name__):
                with self.assertRaises(TypeError):
                    normalize_claim_text(bad)

    def test_the_type_error_does_not_contain_the_value(self):
        with self.assertRaises(TypeError) as raised:
            normalize_claim_text(b"SECRET-TOKEN-4f9a1c")
        self.assertNotIn("SECRET", str(raised.exception))


class ClaimFingerprintTest(unittest.TestCase):
    def test_known_values(self):
        self.assertEqual(claim_fingerprint(""), EMPTY_SHA256)
        self.assertEqual(claim_fingerprint(" \n "), EMPTY_SHA256)
        self.assertEqual(claim_fingerprint("abc"), ABC_SHA256)

    def test_it_is_64_lowercase_hexadecimal_characters_without_a_prefix(self):
        value = claim_fingerprint("Rust 1.75 is stable")

        self.assertEqual(len(value), 64)
        self.assertEqual(value, value.lower())
        self.assertTrue(all(character in "0123456789abcdef" for character in value))

    def test_texts_that_normalise_alike_share_a_fingerprint(self):
        self.assertEqual(claim_fingerprint("ABC  "), ABC_SHA256)
        self.assertEqual(claim_fingerprint("  a\tB\nc"), claim_fingerprint("A b C"))
        self.assertEqual(claim_fingerprint("ＡＢＣ"), ABC_SHA256)
        self.assertEqual(claim_fingerprint("Straße"), claim_fingerprint("strasse"))

    def test_any_other_difference_changes_the_fingerprint(self):
        base = claim_fingerprint("The sky is blue.")
        for other in (
            "The sky is blue",
            "The sky is blue!",
            "The sky is not blue.",
            "The blue sky is.",
            "The sky is blue. ok",
            "the sky is bleu.",
        ):
            with self.subTest(other):
                self.assertNotEqual(claim_fingerprint(other), base)

    def test_only_a_str_is_accepted(self):
        for bad in (None, b"abc", 5):
            with self.subTest(type(bad).__name__):
                with self.assertRaises(TypeError):
                    claim_fingerprint(bad)


class OrderPairTest(unittest.TestCase):
    def test_the_smaller_integer_value_comes_first_in_either_order(self):
        one, two = uid(1), uid(2)

        self.assertEqual(order_pair(one, two), (one, two))
        self.assertEqual(order_pair(two, one), (one, two))

    def test_the_whole_128_bit_range_is_compared(self):
        low, middle, high = uid(0), uid(2**127), uid(2**128 - 1)

        self.assertEqual(order_pair(high, low), (low, high))
        self.assertEqual(order_pair(middle, high), (middle, high))
        self.assertEqual(order_pair(high, middle), (middle, high))
        self.assertEqual(order_pair(uid(2**127 - 1), middle), (uid(2**127 - 1), middle))

    def test_the_order_is_the_order_of_the_hexadecimal_text(self):
        a = UUID("0fffffff-ffff-ffff-ffff-ffffffffffff")
        b = UUID("10000000-0000-0000-0000-000000000000")
        c = UUID("a0000000-0000-0000-0000-000000000000")

        self.assertEqual(order_pair(c, a), (a, c))
        self.assertEqual(order_pair(b, a), (a, b))

    def test_the_result_is_a_tuple_of_the_same_objects(self):
        one, two = uid(1), uid(2)
        result = order_pair(two, one)

        self.assertIsInstance(result, tuple)
        self.assertIs(result[0], one)
        self.assertIs(result[1], two)

    def test_equal_ids_are_refused(self):
        with self.assertRaises(ValueError) as raised:
            order_pair(uid(7), UUID(int=7))
        self.assertNotIn(str(uid(7)), str(raised.exception))

    def test_only_uuids_are_accepted(self):
        for bad in (str(uid(1)), 1, None, uid(1).bytes):
            with self.subTest(repr(bad)):
                with self.assertRaises(TypeError):
                    order_pair(bad, uid(2))
                with self.assertRaises(TypeError):
                    order_pair(uid(2), bad)


class MergeDuplicateSourcesTest(unittest.TestCase):
    def test_no_entries_give_an_empty_tuple(self):
        self.assertEqual(merge_duplicate_sources([]), ())
        self.assertEqual(merge_duplicate_sources(()), ())

    def test_distinct_sources_keep_their_order(self):
        a, b, c = link("https://a.io/"), link("https://b.io/"), link("https://c.io/")

        result = merge_duplicate_sources([c, a, b])

        self.assertIsInstance(result, tuple)
        self.assertEqual(result, (c, a, b))
        self.assertIs(result[0], c)

    def test_a_repeated_source_is_kept_once_at_its_first_position(self):
        a, b = link("https://a.io/"), link("https://b.io/")

        self.assertEqual(merge_duplicate_sources([a, b, a]), (a, b))
        self.assertEqual(merge_duplicate_sources([b, a, b, a, a]), (b, a))
        self.assertEqual(merge_duplicate_sources((a, a, a)), (a,))

    def test_the_first_entry_wins_whatever_the_later_ones_say(self):
        first = link(
            "https://a.io/",
            fetched_at=T0,
            title="First",
            source_type=SourceType.OFFICIAL_DOCS,
        )
        later = link(
            "https://a.io/",
            fetched_at=T0 + timedelta(days=1),
            title="Later",
            source_type=SourceType.COMMUNITY,
            published_at=T0,
        )

        (kept,) = merge_duplicate_sources([first, later])

        self.assertIs(kept, first)
        self.assertEqual(kept.source.title, "First")
        self.assertEqual(kept.source.fetched_at, T0)

    def test_the_same_locator_with_another_content_hash_is_another_source(self):
        v1, v2 = (
            link("https://a.io/", content="v1"),
            link("https://a.io/", content="v2"),
        )

        self.assertEqual(merge_duplicate_sources([v1, v2, v1]), (v1, v2))

    def test_the_same_content_at_another_locator_is_another_source(self):
        a, b = (
            link("https://a.io/", content="same"),
            link("https://b.io/", content="same"),
        )

        self.assertEqual(merge_duplicate_sources([a, b]), (a, b))

    def test_two_spellings_of_one_page_are_one_source(self):
        first = link("https://EXAMPLE.com:443/a?utm_source=x")
        second = link("https://example.com/a")

        self.assertEqual(merge_duplicate_sources([first, second]), (first,))

    def test_the_same_stance_twice_is_not_a_conflict(self):
        for stance in Stance:
            with self.subTest(stance):
                a = link("https://a.io/", stance)
                self.assertEqual(merge_duplicate_sources([a, a]), (a,))

    def test_another_stance_for_the_same_source_is_a_conflict(self):
        for first, second in (
            (Stance.SUPPORTS, Stance.CONTRADICTS),
            (Stance.CONTRADICTS, Stance.SUPPORTS),
        ):
            with self.subTest(first):
                entries = [
                    link("https://a.io/", first),
                    link("https://b.io/"),
                    link("https://c.io/"),
                    link("https://a.io/", second),
                ]
                with self.assertRaises(InvalidProvenanceInputError) as raised:
                    merge_duplicate_sources(entries)
                self.assertEqual(raised.exception.field, "sources")
                self.assertEqual(raised.exception.problem, InputProblem.CONFLICT)
                self.assertNotIn("a.io", str(raised.exception))

    def test_a_conflict_is_found_after_many_consistent_repeats(self):
        a = link("https://a.io/")
        entries = (
            [a] * 5
            + [link("https://b.io/")]
            + [link("https://a.io/", Stance.CONTRADICTS)]
        )

        with self.assertRaises(InvalidProvenanceInputError):
            merge_duplicate_sources(entries)

    def test_the_input_is_not_modified(self):
        a, b = link("https://a.io/"), link("https://b.io/")
        entries = [a, b, a]

        merge_duplicate_sources(entries)

        self.assertEqual(entries, [a, b, a])

    def test_the_work_is_linear_in_the_number_of_entries(self):
        entries = [link(f"https://example.com/{n}") for n in range(25_000)]
        entries += entries[:5_000]

        started = time.monotonic()
        result = merge_duplicate_sources(entries)
        elapsed = time.monotonic() - started

        self.assertEqual(len(result), 25_000)
        self.assertEqual(result[0], entries[0])
        self.assertEqual(result[-1], entries[24_999])
        # A generous bound: a nested loop needs minutes, a dict needs milliseconds.
        self.assertLess(elapsed, 5.0)


def make_source(number: int, *, fetched_at: datetime = T0, **overrides) -> Source:
    values = {
        "id": uid(number),
        "project_id": PROJECT,
        "locator": f"https://example.com/{number}",
        "source_type": SourceType.PRIMARY,
        "title": "",
        "content_hash": content_hash(str(number)),
        "fetched_at": fetched_at,
        "published_at": None,
        "created_at": T0,
    }
    values.update(overrides)
    return Source(**values)


def make_link(
    claim: int,
    source: Source,
    stance: Stance = Stance.SUPPORTS,
    linked_at: datetime = T0,
) -> SourceLink:
    return SourceLink(
        claim_id=uid(claim), source=source, stance=stance, linked_at=linked_at
    )


def make_claim(number: int, **overrides) -> Claim:
    values = {
        "id": uid(number),
        "project_id": PROJECT,
        "text": f"Claim {number}",
        "task_id": None,
        "created_by": USER,
        "created_at": T0,
    }
    values.update(overrides)
    return Claim(**values)


def make_relation(
    low: int,
    high: int,
    kind: RelationKind = RelationKind.DUPLICATE,
    entity: EntityKind = EntityKind.CLAIM,
) -> Relation:
    return Relation(
        entity=entity,
        kind=kind,
        project_id=PROJECT,
        low_id=uid(low),
        high_id=uid(high),
        created_by=USER,
        created_at=T0,
    )


class OrderLinksTest(unittest.TestCase):
    def test_supporting_sources_come_before_contradicting_ones(self):
        contradicting = make_link(1, make_source(1), Stance.CONTRADICTS)
        supporting = make_link(1, make_source(2), Stance.SUPPORTS)

        self.assertEqual(
            order_links([contradicting, supporting]), (supporting, contradicting)
        )

    def test_within_a_stance_the_newest_fetch_comes_first(self):
        old = make_link(1, make_source(1, fetched_at=T0))
        new = make_link(1, make_source(2, fetched_at=T0 + timedelta(hours=2)))
        middle = make_link(1, make_source(3, fetched_at=T0 + timedelta(hours=1)))

        self.assertEqual(order_links([old, new, middle]), (new, middle, old))

    def test_the_documented_example(self):
        contradicts_10 = make_link(
            1, make_source(1, fetched_at=T0 - timedelta(hours=2)), Stance.CONTRADICTS
        )
        supports_09 = make_link(1, make_source(2, fetched_at=T0 - timedelta(hours=3)))
        supports_11 = make_link(1, make_source(3, fetched_at=T0 - timedelta(hours=1)))

        self.assertEqual(
            order_links([contradicts_10, supports_09, supports_11]),
            (supports_11, supports_09, contradicts_10),
        )

    def test_equal_stance_and_time_are_ordered_by_the_source_id(self):
        first = make_link(1, make_source(3))
        second = make_link(1, make_source(10))
        third = make_link(1, make_source(2**127))

        self.assertEqual(order_links([third, first, second]), (first, second, third))
        self.assertEqual(order_links([second, third, first]), (first, second, third))

    def test_times_are_compared_as_instants_not_as_text(self):
        tokyo = timezone(timedelta(hours=9))
        earlier = make_link(  # 01:00 UTC
            1, make_source(1, fetched_at=datetime(2026, 9, 24, 10, 0, tzinfo=tokyo))
        )
        later = make_link(  # 02:00 UTC
            1, make_source(2, fetched_at=datetime(2026, 9, 24, 2, 0, tzinfo=UTC))
        )

        self.assertEqual(order_links([earlier, later]), (later, earlier))

    def test_a_microsecond_decides(self):
        older = make_link(1, make_source(1, fetched_at=T0))
        newer = make_link(1, make_source(2, fetched_at=T0 + timedelta(microseconds=1)))

        self.assertEqual(order_links([older, newer]), (newer, older))

    def test_every_link_is_kept_and_the_input_is_not_modified(self):
        source = make_source(1)
        twice = [
            make_link(1, source),
            make_link(1, source, linked_at=T0 + timedelta(1)),
        ]
        original = list(twice)

        result = order_links(twice)

        self.assertEqual(len(result), 2)
        self.assertEqual(twice, original)
        self.assertIsInstance(result, tuple)

    def test_no_links_give_an_empty_tuple(self):
        self.assertEqual(order_links([]), ())


class OrderRelationsTest(unittest.TestCase):
    def test_only_the_relations_of_the_entity_are_kept(self):
        mine_low = make_relation(5, 7)
        mine_high = make_relation(2, 5)
        stranger = make_relation(8, 9)

        self.assertEqual(
            order_relations(EntityKind.CLAIM, uid(5), [stranger, mine_high, mine_low]),
            (mine_high, mine_low),
        )

    def test_the_entity_kind_must_match(self):
        claim_relation = make_relation(5, 7)
        source_relation = make_relation(5, 7, entity=EntityKind.SOURCE)

        self.assertEqual(
            order_relations(
                EntityKind.CLAIM, uid(5), [claim_relation, source_relation]
            ),
            (claim_relation,),
        )
        self.assertEqual(
            order_relations(
                EntityKind.SOURCE, uid(5), [claim_relation, source_relation]
            ),
            (source_relation,),
        )

    def test_contradictions_come_before_duplicates_then_the_other_id_ascending(self):
        entity = uid(5)
        relations = [
            make_relation(5, 7, RelationKind.DUPLICATE),
            make_relation(2, 5, RelationKind.DUPLICATE),
            make_relation(5, 9, RelationKind.CONTRADICTION),
            make_relation(1, 5, RelationKind.CONTRADICTION),
        ]

        result = order_relations(EntityKind.CLAIM, entity, relations)

        self.assertEqual(
            [(r.kind, r.other(entity).int) for r in result],
            [
                (RelationKind.CONTRADICTION, 1),
                (RelationKind.CONTRADICTION, 9),
                (RelationKind.DUPLICATE, 2),
                (RelationKind.DUPLICATE, 7),
            ],
        )

    def test_the_other_id_is_compared_as_an_integer(self):
        entity = uid(1)
        near = make_relation(1, 2**127 - 1)
        far = make_relation(1, 2**127)

        self.assertEqual(order_relations(EntityKind.SOURCE, entity, []), ())
        result = order_relations(EntityKind.CLAIM, entity, [far, near])
        self.assertEqual(result, (near, far))

    def test_a_pair_that_is_listed_twice_counts_once_and_the_first_wins(self):
        first = make_relation(5, 7, RelationKind.DUPLICATE)
        second = make_relation(5, 7, RelationKind.CONTRADICTION)

        result = order_relations(EntityKind.CLAIM, uid(5), [first, second])

        self.assertEqual(result, (first,))
        self.assertIs(result[0], first)

    def test_nothing_matching_gives_an_empty_tuple_and_the_input_is_kept(self):
        relations = [make_relation(8, 9)]

        self.assertEqual(order_relations(EntityKind.CLAIM, uid(5), relations), ())
        self.assertEqual(order_relations(EntityKind.CLAIM, uid(5), []), ())
        self.assertEqual(relations, [make_relation(8, 9)])


class AssembleTracedClaimsTest(unittest.TestCase):
    def test_one_traced_claim_per_claim_in_the_given_order(self):
        second, first = make_claim(2), make_claim(1)

        result = assemble_traced_claims([second, first], [], [])

        self.assertIsInstance(result, tuple)
        self.assertEqual([traced.claim for traced in result], [second, first])
        self.assertIs(result[0].claim, second)
        for traced in result:
            self.assertIsInstance(traced, TracedClaim)
            self.assertEqual(traced.links, ())
            self.assertEqual(traced.relations, ())

    def test_no_claims_give_an_empty_tuple(self):
        self.assertEqual(
            assemble_traced_claims([], [make_link(1, make_source(1))], []), ()
        )

    def test_a_claim_that_appears_again_is_skipped(self):
        first = make_claim(1)
        again = make_claim(1, text="Same id, other object")

        result = assemble_traced_claims([first, make_claim(2), again], [], [])

        self.assertEqual([traced.claim.id for traced in result], [uid(1), uid(2)])
        self.assertIs(result[0].claim, first)

    def test_links_are_grouped_by_claim_and_ordered(self):
        s1 = make_source(1, fetched_at=T0)
        s2 = make_source(2, fetched_at=T0 + timedelta(hours=1))
        s3 = make_source(3)
        links = [
            make_link(1, s1),
            make_link(2, s3, Stance.CONTRADICTS),
            make_link(1, s2),
            make_link(2, s1),
            make_link(1, s3, Stance.CONTRADICTS),
        ]

        first, second = assemble_traced_claims(
            [make_claim(1), make_claim(2)], links, []
        )

        self.assertEqual(
            [(link.source.id.int, link.stance) for link in first.links],
            [(2, Stance.SUPPORTS), (1, Stance.SUPPORTS), (3, Stance.CONTRADICTS)],
        )
        self.assertEqual(
            [(link.source.id.int, link.stance) for link in second.links],
            [(1, Stance.SUPPORTS), (3, Stance.CONTRADICTS)],
        )

    def test_a_link_that_appears_twice_counts_once_and_the_first_wins(self):
        source = make_source(1)
        first = make_link(1, source, Stance.SUPPORTS)
        again = make_link(1, source, Stance.CONTRADICTS)

        (traced,) = assemble_traced_claims([make_claim(1)], [first, again], [])

        self.assertEqual(traced.links, (first,))
        self.assertIs(traced.links[0], first)

    def test_the_same_source_may_back_two_claims(self):
        source = make_source(1)

        one, two = assemble_traced_claims(
            [make_claim(1), make_claim(2)],
            [make_link(1, source), make_link(2, source)],
            [],
        )

        self.assertEqual([link.claim_id for link in one.links], [uid(1)])
        self.assertEqual([link.claim_id for link in two.links], [uid(2)])

    def test_links_of_claims_that_are_not_listed_are_ignored(self):
        (traced,) = assemble_traced_claims(
            [make_claim(1)], [make_link(9, make_source(1))], []
        )

        self.assertEqual(traced.links, ())

    def test_relations_are_those_of_each_claim_ordered(self):
        relations = [
            make_relation(1, 2, RelationKind.DUPLICATE),
            make_relation(1, 3, RelationKind.CONTRADICTION),
            make_relation(2, 3, RelationKind.DUPLICATE),
            make_relation(1, 4, RelationKind.DUPLICATE, EntityKind.SOURCE),
        ]

        one, two, three = assemble_traced_claims(
            [make_claim(1), make_claim(2), make_claim(3)], [], relations
        )

        self.assertEqual(one.relations, (relations[1], relations[0]))
        self.assertEqual(two.relations, (relations[0], relations[2]))
        self.assertEqual(three.relations, (relations[1], relations[2]))

    def test_a_relation_to_a_claim_outside_the_list_is_still_shown(self):
        relation = make_relation(1, 99, RelationKind.CONTRADICTION)

        (traced,) = assemble_traced_claims([make_claim(1)], [], [relation])

        self.assertEqual(traced.relations, (relation,))

    def test_the_inputs_are_not_modified(self):
        claims = [make_claim(2), make_claim(1)]
        links = [make_link(1, make_source(1)), make_link(1, make_source(2))]
        relations = [make_relation(1, 2)]
        before = (list(claims), list(links), list(relations))

        assemble_traced_claims(claims, links, relations)

        self.assertEqual((claims, links, relations), before)


if __name__ == "__main__":
    unittest.main()
