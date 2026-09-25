"""Deduplication, conflict groups and the Top-N cut (pure rules, no database)."""

import random
import unittest
from uuid import uuid4

from paw_backend.memory.models import ConfirmationState, MemoryScope
from paw_backend.memory.retrieval.grouping import _text_of as text_of
from paw_backend.memory.retrieval.grouping import (
    conflict_groups,
    deduplicate,
    near_duplicate,
    precedence_key,
    rank_key,
    select,
)
from paw_backend.memory.retrieval.records import Freshness, StaleReason

from .retrieval_support import ids, make_ranked

SIMILARITY = 0.9
WORDS = "alpha beta gamma delta epsilon zeta eta theta iota kappa".split()


def words(count: int, *, extra: str = "") -> str:
    return " ".join([*WORDS[:count], extra]).strip()


def dedup(items, conflicts=(), similarity=SIMILARITY):
    return deduplicate(items, conflicts, similarity)


class PrecedenceTest(unittest.TestCase):
    def test_the_order_is_confirmation_freshness_scope_score(self):
        confirmed = make_ranked(
            score=0.1, confirmation_state=ConfirmationState.CONFIRMED
        )
        inferred = make_ranked(score=0.9, confirmation_state=ConfirmationState.INFERRED)
        self.assertLess(precedence_key(confirmed), precedence_key(inferred))

        fresh = make_ranked(score=0.1)
        stale = make_ranked(
            score=0.9, freshness=Freshness.STALE, stale_reason=StaleReason.MARKED_STALE
        )
        self.assertLess(precedence_key(fresh), precedence_key(stale))

        repo = make_ranked(score=0.1, scope=MemoryScope.REPO)
        user = make_ranked(score=0.9, scope=MemoryScope.USER)
        self.assertLess(precedence_key(repo), precedence_key(user))

        high = make_ranked(score=0.9)
        low = make_ranked(score=0.1)
        self.assertLess(precedence_key(high), precedence_key(low))

    def test_freshness_outweighs_scope_and_scope_outweighs_score(self):
        stale_repo = make_ranked(
            score=0.9,
            scope=MemoryScope.REPO,
            freshness=Freshness.STALE,
            stale_reason=StaleReason.MARKED_STALE,
        )
        fresh_user = make_ranked(score=0.1, scope=MemoryScope.USER)
        self.assertLess(precedence_key(fresh_user), precedence_key(stale_repo))
        # Confirmation outweighs freshness.
        stale_confirmed = make_ranked(
            score=0.1, freshness=Freshness.STALE, stale_reason=StaleReason.MARKED_STALE
        )
        fresh_inferred = make_ranked(
            score=0.9, confirmation_state=ConfirmationState.INFERRED
        )
        self.assertLess(precedence_key(stale_confirmed), precedence_key(fresh_inferred))

    def test_the_final_order_is_by_score_first(self):
        confirmed_low = make_ranked(score=0.1)
        inferred_high = make_ranked(
            score=0.9, confirmation_state=ConfirmationState.INFERRED
        )
        self.assertLess(rank_key(inferred_high), rank_key(confirmed_low))

    def test_ties_are_broken_by_the_version_id(self):
        a, b = make_ranked(score=0.5), make_ranked(score=0.5)
        first, second = sorted([a, b], key=rank_key)
        self.assertLess(
            str(first.candidate.version_id), str(second.candidate.version_id)
        )


class NearDuplicateTest(unittest.TestCase):
    def make(self, text):
        return text_of(make_ranked(title="t", content=text))

    def test_the_same_words_in_the_same_order_are_equal_whatever_the_punctuation(self):
        self.assertTrue(near_duplicate(self.make("OK"), self.make("ok."), SIMILARITY))
        self.assertTrue(
            near_duplicate(
                self.make("Use tabs, please!"), self.make("use TABS please"), 0.9
            )
        )

    def test_two_short_texts_that_differ_are_not_copies(self):
        self.assertFalse(
            near_duplicate(self.make("use tabs"), self.make("use spaces"), 0.1)
        )

    def test_overlap_at_the_threshold_counts_below_it_does_not(self):
        # 10 words shared, one extra in one text (plus the shared title "t"):
        # |intersection| 11, |union| 12 -> 0.9167.
        a, b = self.make(words(10)), self.make(words(10, extra="omega"))
        self.assertTrue(near_duplicate(a, b, 0.91))
        self.assertFalse(near_duplicate(a, b, 0.92))
        # Nine shared and one different word each: 10 / 12 = 0.8333.
        c = self.make(words(9, extra="omega"))
        d = self.make(words(9, extra="sigma"))
        self.assertTrue(near_duplicate(c, d, 0.83))
        self.assertFalse(near_duplicate(c, d, 0.84))

    def test_overlap_exactly_at_the_threshold_is_a_copy(self):
        # Nine shared features (eight words and the title) and one extra: 9 / 10.
        a, b = self.make(words(8)), self.make(words(9))
        self.assertTrue(near_duplicate(a, b, 0.9))
        self.assertFalse(near_duplicate(a, b, 0.9000001))

    def test_japanese_copies_are_found_through_character_pairs(self):
        a = self.make("デプロイの手順はマージの後にステージングで確認する")
        b = self.make("デプロイの手順は、マージの後に、ステージングで確認する。")
        self.assertTrue(near_duplicate(a, b, 0.9))


class DeduplicateTest(unittest.TestCase):
    def test_a_copy_is_merged_into_the_better_claim_and_listed(self):
        repo = make_ranked(score=0.4, scope=MemoryScope.REPO, content="same text here")
        user = make_ranked(score=0.9, scope=MemoryScope.USER, content="same text here")
        survivors, merged = dedup([user, repo])
        self.assertEqual(ids(survivors), [repo.candidate.version_id])
        self.assertEqual(survivors[0].duplicates, (user.candidate.version_id,))
        self.assertEqual(merged, {user.candidate.version_id: repo.candidate.version_id})

    def test_confirmed_beats_inferred_and_fresh_beats_stale(self):
        inferred = make_ranked(
            score=0.9, confirmation_state=ConfirmationState.INFERRED, content="same"
        )
        confirmed = make_ranked(score=0.1, content="same")
        survivors, _ = dedup([inferred, confirmed])
        self.assertEqual(ids(survivors), [confirmed.candidate.version_id])

        stale = make_ranked(
            score=0.9,
            freshness=Freshness.STALE,
            stale_reason=StaleReason.MARKED_STALE,
            content="same",
        )
        fresh = make_ranked(score=0.1, content="same")
        survivors, _ = dedup([stale, fresh])
        self.assertEqual(ids(survivors), [fresh.candidate.version_id])

    def test_the_higher_score_wins_between_equal_claims(self):
        low = make_ranked(score=0.2, content="same")
        high = make_ranked(score=0.8, content="same")
        survivors, _ = dedup([low, high])
        self.assertEqual(ids(survivors), [high.candidate.version_id])

    def test_different_memories_are_all_kept(self):
        a = make_ranked(content=words(10))
        b = make_ranked(content="completely different words appear over here now")
        survivors, merged = dedup([a, b])
        self.assertEqual(len(survivors), 2)
        self.assertEqual(merged, {})
        self.assertTrue(all(item.duplicates == () for item in survivors))

    def test_memories_that_conflict_are_never_merged_even_when_identical(self):
        a = make_ranked(score=0.9, content="Deploys go out on Friday")
        b = make_ranked(score=0.5, content="Deploys go out on Friday")
        survivors, merged = dedup(
            [a, b], [(a.candidate.version_id, b.candidate.version_id)]
        )
        self.assertEqual(len(survivors), 2)
        self.assertEqual(merged, {})

    def test_the_conflict_relation_counts_in_either_direction(self):
        a, b = make_ranked(content="x y z w v"), make_ranked(content="x y z w v")
        for pair in ((a, b), (b, a)):
            with self.subTest(pair=pair[0] is a):
                survivors, _ = dedup(
                    [a, b],
                    [(pair[0].candidate.version_id, pair[1].candidate.version_id)],
                )
                self.assertEqual(len(survivors), 2)

    def test_two_versions_of_one_memory_keep_only_the_better_one(self):
        memory_id = uuid4()
        old = make_ranked(score=0.3, memory_id=memory_id, content="alpha")
        new = make_ranked(score=0.8, memory_id=memory_id, content="beta gamma")
        survivors, merged = dedup([old, new])
        self.assertEqual(ids(survivors), [new.candidate.version_id])
        self.assertEqual(merged, {old.candidate.version_id: new.candidate.version_id})

    def test_the_result_does_not_depend_on_the_input_order(self):
        items = [make_ranked(score=0.1 * n, content="same") for n in range(1, 6)]
        forward, _ = dedup(items)
        backward, _ = dedup(list(reversed(items)))
        self.assertEqual(ids(forward), ids(backward))

    def test_an_empty_input_gives_nothing(self):
        self.assertEqual(dedup([]), ([], {}))


SAME = "deploy the backend every friday after the merge is green"


class IndirectMergeTest(unittest.TestCase):
    """A conflict never disappears through a merge of one end into a third memory."""

    def trio(self):
        best = make_ranked(score=0.9, content=SAME)  # confirmed: the better claim
        first = make_ranked(
            score=0.6, content=SAME, confirmation_state=ConfirmationState.INFERRED
        )
        second = make_ranked(
            score=0.5, content=SAME, confirmation_state=ConfirmationState.INFERRED
        )
        edge = (first.candidate.version_id, second.candidate.version_id)
        return best, first, second, edge

    def test_a_copy_of_both_ends_of_a_conflict_does_not_absorb_both(self):
        best, first, second, edge = self.trio()
        survivors, merged = dedup([second, first, best], [edge])
        # ``first`` is a copy of ``best`` and joins it; ``second`` conflicts with what
        # ``best`` already stands for, so it stays a memory of its own.
        self.assertEqual(
            ids(survivors), [best.candidate.version_id, second.candidate.version_id]
        )
        self.assertEqual(
            merged, {first.candidate.version_id: best.candidate.version_id}
        )
        self.assertEqual(survivors[0].duplicates, (first.candidate.version_id,))
        groups = conflict_groups(survivors, [edge], merged)
        self.assertEqual(len(groups), 1)
        self.assertEqual(
            ids(groups[0]), [best.candidate.version_id, second.candidate.version_id]
        )

    def test_the_result_does_not_depend_on_the_input_order(self):
        best, first, second, edge = self.trio()
        outcomes = set()
        for order in (
            [best, first, second],
            [second, first, best],
            [first, best, second],
        ):
            survivors, merged = dedup(order, [edge])
            groups = conflict_groups(survivors, [edge], merged)
            outcomes.add((tuple(ids(survivors)), tuple(tuple(ids(g)) for g in groups)))
        self.assertEqual(len(outcomes), 1)

    def test_a_chain_of_copies_keeps_every_conflict(self):
        # a ~ b ~ c ~ d are all copies; a conflicts with c and b with d.
        items = [make_ranked(score=0.9 - n / 10, content=SAME) for n in range(4)]
        a, b, c, d = (i.candidate.version_id for i in items)
        edges = [(a, c), (b, d)]
        survivors, merged = dedup(items, edges)
        groups = conflict_groups(survivors, edges, merged)
        for x, y in edges:
            self.assertNotEqual(merged.get(x, x), merged.get(y, y))
            self.assertTrue(
                any(
                    {merged.get(x, x), merged.get(y, y)} <= set(ids(group))
                    for group in groups
                )
            )

    def test_copies_that_do_not_conflict_still_merge_into_one(self):
        best, first, second, _ = self.trio()
        survivors, merged = dedup([best, first, second], [])
        self.assertEqual(ids(survivors), [best.candidate.version_id])
        self.assertEqual(
            set(merged), {first.candidate.version_id, second.candidate.version_id}
        )


def random_world(rng, size):
    """Items whose texts are copies of a few templates, and random conflict edges."""
    templates = [
        "deploy the backend every friday after the merge is green",
        "use tabs for indentation in every python module we write",
        "the staging database is reset every night at midnight",
    ]
    items = []
    for _ in range(size):
        text = rng.choice(templates)
        if rng.random() < 0.3:
            text += " " + rng.choice(["today", "again", "please"])
        items.append(
            make_ranked(
                score=rng.random(),
                content=text,
                confirmation_state=rng.choice(list(ConfirmationState)[:3]),
                scope=rng.choice(list(MemoryScope)),
                freshness=rng.choice([Freshness.FRESH, Freshness.STALE]),
                stale_reason=None,
            )
        )
    versions = [i.candidate.version_id for i in items]
    edges = [
        (a, b) for a in versions for b in versions if a < b and rng.random() < 0.25
    ]
    return items, edges


class ConflictsNeverDisappearTest(unittest.TestCase):
    """Random duplicate / conflict graphs against an oracle written out plainly."""

    def test_every_conflict_between_candidates_ends_up_in_one_group(self):
        checked_edges = merged_pairs = 0
        for seed in range(400):
            rng = random.Random(seed)
            items, edges = random_world(rng, rng.randint(2, 12))
            survivors, merged = dedup(items, edges)
            groups = conflict_groups(survivors, edges, merged)
            group_of = {}
            for number, group in enumerate(groups):
                for member in group:
                    group_of[member.candidate.version_id] = number
            survivor_ids = set(ids(survivors))
            for version_id in ids(items):
                representative = merged.get(version_id, version_id)
                self.assertIn(representative, survivor_ids, f"seed {seed}")
            for x, y in edges:
                sx, sy = merged.get(x, x), merged.get(y, y)
                # 1. Two ends of a conflict never share a survivor.
                self.assertNotEqual(sx, sy, f"seed {seed}: a conflict merged away")
                # 2. They are in the same group.
                self.assertIn(sx, group_of, f"seed {seed}")
                self.assertEqual(group_of[sx], group_of.get(sy), f"seed {seed}")
                checked_edges += 1
            merged_pairs += len(merged)
            # 3. Nothing is lost or invented: each item is a survivor or merged once.
            self.assertEqual(len(survivors) + len(merged), len(items))
            # 4. ``duplicates`` says exactly what was merged.
            listed = {d for s in survivors for d in s.duplicates}
            self.assertEqual(listed, set(merged))
        self.assertGreater(checked_edges, 300)
        self.assertGreater(merged_pairs, 300)


class ConflictGroupsTest(unittest.TestCase):
    def test_related_memories_form_a_group_ordered_by_precedence(self):
        confirmed = make_ranked(score=0.2)
        inferred = make_ranked(score=0.9, confirmation_state=ConfirmationState.INFERRED)
        loner = make_ranked(score=0.5)
        edge = (inferred.candidate.version_id, confirmed.candidate.version_id)
        groups = conflict_groups([inferred, confirmed, loner], [edge], {})
        self.assertEqual(len(groups), 1)
        self.assertEqual(
            ids(groups[0]),
            [confirmed.candidate.version_id, inferred.candidate.version_id],
        )

    def test_a_chain_is_one_group_and_two_pairs_are_two(self):
        a, b, c, d, e = (make_ranked(score=0.9 - n / 10) for n in range(5))
        edges = [
            (a.candidate.version_id, b.candidate.version_id),
            (b.candidate.version_id, c.candidate.version_id),
            (d.candidate.version_id, e.candidate.version_id),
        ]
        groups = conflict_groups([a, b, c, d, e], edges, {})
        self.assertEqual([len(g) for g in groups], [3, 2])
        self.assertEqual(set(ids(groups[0])), set(ids([a, b, c])))

    def test_an_edge_to_a_merged_copy_belongs_to_its_survivor(self):
        survivor, copy, other = make_ranked(), make_ranked(), make_ranked()
        merged = {copy.candidate.version_id: survivor.candidate.version_id}
        edge = (copy.candidate.version_id, other.candidate.version_id)
        groups = conflict_groups([survivor, other], [edge], merged)
        self.assertEqual(len(groups), 1)
        self.assertEqual(set(ids(groups[0])), set(ids([survivor, other])))

    def test_an_edge_to_something_that_is_not_a_survivor_is_ignored(self):
        a = make_ranked()
        groups = conflict_groups([a], [(a.candidate.version_id, uuid4())], {})
        self.assertEqual(groups, [])

    def test_a_self_edge_and_no_edges_give_no_group(self):
        a = make_ranked()
        self.assertEqual(conflict_groups([a], [(a.candidate.version_id,) * 2], {}), [])
        self.assertEqual(conflict_groups([a], [], {}), [])

    def test_groups_are_numbered_by_their_best_member(self):
        low = [make_ranked(score=0.2), make_ranked(score=0.1)]
        high = [make_ranked(score=0.9), make_ranked(score=0.3)]
        edges = [
            (low[0].candidate.version_id, low[1].candidate.version_id),
            (high[0].candidate.version_id, high[1].candidate.version_id),
        ]
        groups = conflict_groups([*low, *high], edges, {})
        self.assertEqual(set(ids(groups[0])), set(ids(high)))


class SelectTest(unittest.TestCase):
    def test_the_best_memories_fill_the_limit_in_score_order(self):
        items = [make_ranked(score=s) for s in (0.3, 0.9, 0.6)]
        selection = select(items, [], 2)
        self.assertEqual([item.score for item, _ in selection.hits], [0.9, 0.6])
        self.assertEqual(selection.groups, ())
        self.assertEqual(selection.dropped_groups, 0)

    def test_a_group_ranks_with_its_best_member_and_stays_together(self):
        a, b = make_ranked(score=0.9), make_ranked(score=0.1)
        single = make_ranked(score=0.5)
        group = tuple(sorted([a, b], key=precedence_key))
        selection = select([a, b, single], [group], 10)
        order = [item.candidate.version_id for item, _ in selection.hits]
        self.assertEqual(order[:2], ids(group))
        self.assertEqual(order[2], single.candidate.version_id)
        self.assertEqual([number for _, number in selection.hits], [0, 0, None])
        self.assertEqual(selection.groups[0].version_ids, tuple(ids(group)))

    def test_a_group_that_does_not_fit_is_skipped_whole_and_counted(self):
        a, b, c = (make_ranked(score=s) for s in (0.9, 0.8, 0.7))
        single = make_ranked(score=0.1)
        selection = select([a, b, c, single], [(a, b, c)], 2)
        self.assertEqual(
            [item.candidate.version_id for item, _ in selection.hits],
            [single.candidate.version_id],
        )
        self.assertEqual(selection.groups, ())
        self.assertEqual(selection.dropped_groups, 1)

    def test_a_group_exactly_as_large_as_the_limit_fits(self):
        a, b = make_ranked(score=0.9), make_ranked(score=0.8)
        selection = select([a, b], [(a, b)], 2)
        self.assertEqual(len(selection.hits), 2)
        self.assertEqual(selection.dropped_groups, 0)

    def test_never_more_than_the_limit_is_returned(self):
        items = [make_ranked(score=n / 100) for n in range(1, 40)]
        for limit in (1, 5, 39, 100):
            with self.subTest(limit=limit):
                self.assertEqual(len(select(items, [], limit).hits), min(limit, 39))

    def test_group_numbers_follow_the_selected_order(self):
        first = (make_ranked(score=0.9), make_ranked(score=0.8))
        second = (make_ranked(score=0.5), make_ranked(score=0.4))
        selection = select([*second, *first], [second, first], 10)
        self.assertEqual([g.group_id for g in selection.groups], [0, 1])
        self.assertEqual(set(selection.groups[0].version_ids), set(ids(first)))

    def test_nothing_in_nothing_out(self):
        selection = select([], [], 5)
        self.assertEqual(
            (selection.hits, selection.groups, selection.dropped_groups), ((), (), 0)
        )


if __name__ == "__main__":
    unittest.main()
