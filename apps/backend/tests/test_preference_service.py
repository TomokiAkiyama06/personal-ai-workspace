"""The Inferred Preference confirmation flow on a real PostgreSQL (PAW-044).

Observation -> Candidate -> Confirmation -> Confirmed: the candidates and their
evidence, the recommended Repo / Project / User scope, the confirmation at the
chosen scope (a new confirmed version; widening needs the right to write there),
[保存しない], the free-text preview, and that a high-risk preference is never
applied without an explicit acknowledgement and never changes a permission.
"""

import json
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from paw_backend.authz import Principal, ProjectRole, SystemRole
from paw_backend.memory.models import MemoryScope
from paw_backend.memory.preferences import (
    CandidateKind,
    Confirmation,
    Consistency,
    HeldCandidateRef,
    InterpretedScope,
    Interpreter,
    LanguageStrength,
    MemoryCandidateRef,
    PreferenceCandidateChangedError,
    PreferenceHighRiskError,
    RiskLevel,
    Strength,
    StructuredPreference,
    TargetScope,
)
from paw_backend.memory.preferences import limits as preference_limits
from paw_backend.memory.versioning import (
    InputProblem,
    InvalidMemoryInputError,
    MemoryDraft,
    MemoryNotFoundError,
    MemoryPermissionError,
    MemoryVersionConflictError,
)

from .preference_support import T0, PostgresPreferenceTestCase, requires_postgres

USER = Confirmation(scope=TargetScope.USER)


class FakeInterpreter:
    """A model interpreter without a model: answers from a script."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    async def interpret(self, text, candidate):
        self.calls.append((text, candidate))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


@requires_postgres
class CandidatesTest(PostgresPreferenceTestCase):
    async def test_a_candidate_repeated_in_one_repository(self):
        project = self.seed_project()
        me = self.actor(self.seed_member(project, role=ProjectRole.CONTRIBUTOR))
        repo = self.seed_repo(project)
        memory_id = self.seed_candidate(me.user_id, "indent_style", "use tabs")
        for _ in range(3):
            self.observe(me.user_id, "indent_style", project=project, repo=repo)

        (found,) = await self.preferences.candidates(me)

        self.assertIs(found.kind, CandidateKind.MEMORY)
        self.assertEqual(
            (found.memory_id, found.version_number, found.key, found.content),
            (memory_id, 1, "indent_style", "use tabs"),
        )
        self.assertEqual(found.evidence.frequency, 3)
        self.assertEqual(
            (found.evidence.project_count, found.evidence.repo_count), (1, 1)
        )
        self.assertIs(found.evidence.risk_level, RiskLevel.LOW)
        self.assertIs(found.evidence.consistency, Consistency.CONSISTENT)
        self.assertEqual(
            (found.recommendation.scope, found.recommendation.repo_id),
            (TargetScope.REPO, repo),
        )
        self.assertEqual(
            [(o.scope, o.recommended) for o in found.options],
            [
                (TargetScope.REPO, True),
                (TargetScope.PROJECT, False),
                (TargetScope.USER, False),
            ],
        )
        self.assertTrue(found.ready)
        # A read: no audit row (memory.read is DENIED_ONLY).
        self.assertEqual(self.audit_actions(), [])

    async def test_the_strength_of_the_persons_words(self):
        me = self.user()
        self.seed_candidate(me.user_id, "indent_style", "use tabs")
        self.observe(me.user_id, "indent_style", message="今後はtabにして")
        (found,) = await self.preferences.candidates(me)
        self.assertIs(found.evidence.language_strength, LanguageStrength.STANDING)
        self.assertTrue(found.ready)  # once, with standing words

    async def test_only_the_persons_own_unconfirmed_candidates(self):
        me, other = self.user(), self.user()
        self.seed_candidate(me.user_id, "mine", "x")
        self.seed_candidate(other.user_id, "theirs", "y")
        self.seed_candidate(me.user_id, "confirmed", "z", state="confirmed")
        self.seed_candidate(me.user_id, "gone", "w", status="deprecated")
        self.observe(other.user_id, "mine")  # another person's observation

        found = await self.preferences.candidates(me)

        self.assertEqual([c.key for c in found], ["mine"])
        self.assertEqual(found[0].evidence.frequency, 0)

    async def test_held_candidates(self):
        me = self.user()
        self.observe(
            me.user_id, "merge_policy", result="held_high_risk", content="merge freely"
        )
        latest = self.observe(
            me.user_id, "merge_policy", result="held_high_risk", content="merge it"
        )
        # A held item that a later written observation made outdated.
        self.observe(me.user_id, "old", result="held_high_risk")
        self.observe(me.user_id, "old", result="created")

        (found,) = await self.preferences.candidates(me)

        self.assertIs(found.kind, CandidateKind.HELD)
        self.assertEqual((found.entry_id, found.item_index), (latest, 0))
        self.assertEqual(found.content, "merge it")
        self.assertEqual(found.held_reason.value, "held_high_risk")
        self.assertEqual(found.evidence.frequency, 2)
        self.assertIs(found.evidence.risk_level, RiskLevel.HIGH)
        self.assertIsNone(found.memory_id)

    async def test_held_ordering_follows_the_journal_in_one_conversation(self):
        # In one conversation the event sequence orders the entries, whatever the
        # recorded times say (Codex P1 on #205): the clock stepped backwards here.
        me = self.user()
        conversation = self.execute(
            "INSERT INTO conversations (owner_user_id) VALUES (:o) RETURNING id",
            o=me.user_id,
        ).scalar_one()
        late_clock = T0 + timedelta(minutes=10)
        self.observe(
            me.user_id,
            "k",
            result="held_high_risk",
            conversation=conversation,
            sequence=1,
            at=late_clock,
        )
        # Written afterwards (sequence 2) but recorded with an earlier time.
        self.observe(
            me.user_id,
            "k",
            result="created",
            conversation=conversation,
            sequence=2,
            at=T0,
        )
        self.assertEqual(await self.preferences.candidates(me), ())

    async def test_the_latest_held_item_follows_the_journal_in_one_conversation(self):
        me = self.user()
        conversation = self.execute(
            "INSERT INTO conversations (owner_user_id) VALUES (:o) RETURNING id",
            o=me.user_id,
        ).scalar_one()
        self.observe(
            me.user_id,
            "k",
            result="held_high_risk",
            content="older",
            conversation=conversation,
            sequence=1,
            at=T0 + timedelta(minutes=10),
        )
        newer = self.observe(
            me.user_id,
            "k",
            result="held_high_risk",
            content="newer",
            conversation=conversation,
            sequence=2,
            at=T0,
        )
        (found,) = await self.preferences.candidates(me)
        self.assertEqual((found.entry_id, found.content), (newer, "newer"))

    async def test_the_capped_observations_follow_the_journal_in_one_conversation(
        self,
    ):
        # Codex P2 on #205: the per-key cap kept the latest recorded times, so a
        # clock that stepped back in one conversation dropped the newest entries
        # (by event sequence) from the evidence.
        me = self.user()
        self.seed_candidate(me.user_id, "indent_style", "use tabs")
        conversation = self.execute(
            "INSERT INTO conversations (owner_user_id) VALUES (:o) RETURNING id",
            o=me.user_id,
        ).scalar_one()
        for sequence, minutes, message in (
            (1, 10, "tabs please"),
            (2, 5, "tabs please"),
            (3, 0, "今後はtabにして"),  # the newest: written last, clock stepped back
        ):
            self.observe(
                me.user_id,
                "indent_style",
                message=message,
                conversation=conversation,
                sequence=sequence,
                at=T0 + timedelta(minutes=minutes),
            )
        with patch.object(preference_limits, "MAX_OBSERVATIONS_PER_KEY", 2):
            (found,) = await self.preferences.candidates(me)
        self.assertEqual(found.evidence.frequency, 2)
        self.assertIs(found.evidence.language_strength, LanguageStrength.STANDING)

    async def test_another_keys_observations_do_not_rank_this_key(self):
        # Codex P2 on #213: the time carried forward in a conversation is the key's
        # own, so a future-dated observation of another key does not make this
        # key's older observation outrank a newer one from another conversation.
        me = self.user()
        self.seed_candidate(me.user_id, "a", "x")
        self.seed_candidate(me.user_id, "b", "y")
        conversation = self.execute(
            "INSERT INTO conversations (owner_user_id) VALUES (:o) RETURNING id",
            o=me.user_id,
        ).scalar_one()
        self.observe(
            me.user_id,
            "a",
            conversation=conversation,
            sequence=1,
            at=T0 + timedelta(minutes=60),
        )
        self.observe(me.user_id, "b", conversation=conversation, sequence=2, at=T0)
        self.observe(
            me.user_id, "b", message="今後はtabにして", at=T0 + timedelta(minutes=30)
        )
        with patch.object(preference_limits, "MAX_OBSERVATIONS_PER_KEY", 1):
            found = {c.key: c for c in await self.preferences.candidates(me)}
        self.assertEqual(found["b"].evidence.frequency, 1)
        self.assertIs(found["b"].evidence.language_strength, LanguageStrength.STANDING)

    async def test_a_confirmation_checks_every_held_item_of_its_key(self):
        # Codex P2 on #205: the cap on the listing must not decide which held item
        # of a key is the latest one when the person answers it.
        me = self.user()
        conversation = self.execute(
            "INSERT INTO conversations (owner_user_id) VALUES (:o) RETURNING id",
            o=me.user_id,
        ).scalar_one()
        older = self.observe(
            me.user_id,
            "k",
            result="held_high_risk",
            content="older",
            conversation=conversation,
            sequence=1,
            at=T0 + timedelta(minutes=10),
        )
        newer = self.observe(
            me.user_id,
            "k",
            result="held_high_risk",
            content="newer",
            conversation=conversation,
            sequence=2,
            at=T0,
        )
        answer = Confirmation(scope=TargetScope.USER, acknowledge_high_risk=True)
        with patch.object(preference_limits, "MAX_HELD_ITEMS", 1):
            with self.assertRaises(PreferenceCandidateChangedError):
                await self.preferences.confirm(me, HeldCandidateRef(older, 0), answer)
            written = await self.preferences.confirm(
                me, HeldCandidateRef(newer, 0), answer
            )
        self.assertEqual(written.content, "newer")

    async def test_a_conflict_is_shown_but_not_asked(self):
        me = self.user()
        first = self.seed_candidate(me.user_id, "a", "use tabs")
        second = self.seed_candidate(me.user_id, "b", "use spaces")
        self.execute(
            "INSERT INTO memory_relations (from_version_id, to_version_id,"
            " relation_type) SELECT b.id, a.id, 'conflicts_with' FROM"
            " memory_versions a, memory_versions b WHERE a.memory_id = :a"
            " AND b.memory_id = :b",
            a=first,
            b=second,
        )
        for _ in range(3):
            self.observe(me.user_id, "a")
        found = {c.key: c for c in await self.preferences.candidates(me)}
        self.assertIs(found["a"].evidence.consistency, Consistency.CONFLICTING)
        self.assertFalse(found["a"].ready)

    async def test_the_backend_identity_has_no_candidates(self):
        with self.assertRaises(MemoryPermissionError):
            await self.preferences.candidates(Principal(uuid4(), SystemRole.SYSTEM))


@requires_postgres
class ConfirmMemoryTest(PostgresPreferenceTestCase):
    async def test_confirm_as_the_persons_own_preference(self):
        me = self.user()
        memory_id = self.seed_candidate(me.user_id, "indent_style", "use tabs")
        self.observe(me.user_id, "indent_style")
        (old,) = self.versions(memory_id)
        self.execute(
            "INSERT INTO memory_sources (memory_version_id, source_type, source_ref)"
            " VALUES (:v, 'task', 'task-1')",
            v=old.id,
        )

        written = await self.preferences.confirm(
            me, MemoryCandidateRef(memory_id, 1), USER
        )

        old, new = self.versions(memory_id)
        self.assertEqual(written.version_number, 2)
        self.assertEqual((old.status, new.status), ("superseded", "active"))
        self.assertEqual(new.confirmation_state, "confirmed")
        self.assertEqual((new.scope, new.owner_user_id), ("user", me.user_id))
        self.assertEqual(new.content, "use tabs")
        self.assertEqual(new.memory_type, "preference")
        self.assertEqual(new.freshness_policy, "permanent")
        self.assertEqual((new.actor_type, new.actor_user_id), ("user", me.user_id))
        self.assertEqual(new.attributes["preference"]["policy_effect"], "none")
        self.assertEqual(new.attributes["preference"]["target"], "user")
        self.assertEqual(
            sorted(r[2] for r in self.relations()), ["confirmed_from", "supersedes"]
        )
        self.assertEqual(
            self.sources(new.id),
            [
                ("task", "task-1"),
                ("user_confirmation", f"memory_confirmed_by:{me.user_id}"),
            ],
        )
        self.assertEqual(self.audit_actions(), [("memory.use", "allow")])
        self.assertEqual(await self.preferences.candidates(me), ())

    async def test_confirm_into_a_project_needs_a_contributor(self):
        project = self.seed_project()
        team = self.seed_team(project)
        viewer = self.actor(team.viewer)
        memory_id = self.seed_candidate(viewer.user_id, "k", "use tabs")
        choice = Confirmation(scope=TargetScope.PROJECT, project_id=project)
        with self.assertRaises(MemoryPermissionError):
            await self.preferences.confirm(
                viewer, MemoryCandidateRef(memory_id, 1), choice
            )
        self.assertEqual(len(self.versions(memory_id)), 1)

        outsider = self.user()
        theirs = self.seed_candidate(outsider.user_id, "k", "use tabs")
        with self.assertRaises(MemoryPermissionError) as caught:
            await self.preferences.confirm(
                outsider, MemoryCandidateRef(theirs, 1), choice
            )
        self.assertEqual(caught.exception.reason, "not_project_member")

        contributor = self.actor(team.contributor)
        mine = self.seed_candidate(contributor.user_id, "k", "use tabs")
        await self.preferences.confirm(contributor, MemoryCandidateRef(mine, 1), choice)
        old, new = self.versions(mine)
        self.assertEqual((new.scope, new.project_id), ("project", project))
        self.assertIsNone(new.owner_user_id)
        # A member reads the widened version, never the private one before it.
        history = await self.versioning.history(self.actor(team.manager), mine)
        self.assertEqual([v.version_number for v in history], [2])

    async def test_confirm_into_a_repository(self):
        project = self.seed_project()
        me = self.actor(self.seed_member(project, role=ProjectRole.CONTRIBUTOR))
        repo = self.seed_repo(project)
        closed = self.seed_repo(project, acl=["write"])  # read removed
        elsewhere = self.seed_repo(self.seed_project())
        memory_id = self.seed_candidate(me.user_id, "k", "use tabs")
        ref = MemoryCandidateRef(memory_id, 1)

        for repo_id in (closed, elsewhere, uuid4()):
            with self.subTest(repo=repo_id), self.assertRaises(MemoryPermissionError):
                await self.preferences.confirm(
                    me,
                    ref,
                    Confirmation(
                        scope=TargetScope.REPO, project_id=project, repo_id=repo_id
                    ),
                )
        await self.preferences.confirm(
            me,
            ref,
            Confirmation(scope=TargetScope.REPO, project_id=project, repo_id=repo),
        )
        _, new = self.versions(memory_id)
        self.assertEqual((new.scope, new.repo_id, new.project_id), ("repo", repo, None))

    async def test_an_old_version_is_a_conflict_and_another_persons_is_not_found(self):
        me = self.user()
        memory_id = self.seed_candidate(me.user_id, "k", "use tabs")
        with self.assertRaises(MemoryVersionConflictError):
            await self.preferences.confirm(me, MemoryCandidateRef(memory_id, 2), USER)
        with self.assertRaises(PreferenceCandidateChangedError):
            await self.preferences.confirm(
                self.user(), MemoryCandidateRef(memory_id, 1), USER
            )
        await self.preferences.confirm(me, MemoryCandidateRef(memory_id, 1), USER)
        # Confirmed now: no longer a candidate.
        with self.assertRaises(PreferenceCandidateChangedError):
            await self.preferences.confirm(me, MemoryCandidateRef(memory_id, 2), USER)

    async def test_an_unregistered_memory_is_not_a_candidate(self):
        me = self.user()
        created = await self.versioning.create_memory(
            me,
            MemoryDraft(
                scope=MemoryScope.USER, memory_type="note", title="t", content="c"
            ),
        )
        with self.assertRaises(PreferenceCandidateChangedError):
            await self.preferences.confirm(
                me, MemoryCandidateRef(created.memory_id, 1), USER
            )

    async def test_the_backend_identity_cannot_confirm(self):
        me = self.user()
        memory_id = self.seed_candidate(me.user_id, "k", "use tabs")
        system = Principal(me.user_id, SystemRole.SYSTEM)
        with self.assertRaises(MemoryPermissionError):
            await self.preferences.confirm(
                system, MemoryCandidateRef(memory_id, 1), USER
            )
        self.assertEqual(len(self.versions(memory_id)), 1)


@requires_postgres
class HighRiskTest(PostgresPreferenceTestCase):
    async def test_never_applied_without_an_explicit_acknowledgement(self):
        project = self.seed_project()
        team = self.seed_team(project)
        me = self.actor(team.contributor)
        memory_id = self.seed_candidate(
            me.user_id, "merge_policy", "merge to main without asking"
        )
        before = self.snapshot()

        with self.assertRaises(PreferenceHighRiskError):
            await self.preferences.confirm(me, MemoryCandidateRef(memory_id, 1), USER)
        self.assertEqual(len(self.versions(memory_id)), 1)

        await self.preferences.confirm(
            me,
            MemoryCandidateRef(memory_id, 1),
            Confirmation(scope=TargetScope.USER, acknowledge_high_risk=True),
        )
        _, new = self.versions(memory_id)
        preference = new.attributes["preference"]
        self.assertEqual(
            (preference["risk_level"], preference["acknowledged_high_risk"]),
            ("high", True),
        )
        self.assertEqual(preference["policy_effect"], "none")
        # A memory and nothing else: no project, role or membership changed.
        self.assertEqual(self.snapshot(), before)

    async def test_a_held_high_risk_candidate_needs_it_too(self):
        me = self.user()
        entry = self.observe(me.user_id, "k", result="held_high_risk", content="x")
        with self.assertRaises(PreferenceHighRiskError):
            await self.preferences.confirm(me, HeldCandidateRef(entry, 0), USER)
        self.assertEqual(self.resolutions(), [])
        self.assertEqual(self.rows("SELECT id FROM memory_versions"), [])

    async def test_a_free_text_answer_that_turns_risky_needs_it(self):
        me = self.user()
        memory_id = self.seed_candidate(me.user_id, "review", "review PRs first")
        risky = StructuredPreference(
            InterpretedScope.USER, "review PRs first", exceptions=("then merge",)
        )
        with self.assertRaises(PreferenceHighRiskError):
            await self.preferences.confirm(
                me, MemoryCandidateRef(memory_id, 1), Confirmation(preference=risky)
            )
        required = StructuredPreference(
            InterpretedScope.USER, "review PRs first", strength=Strength.REQUIRED
        )
        with self.assertRaises(PreferenceHighRiskError):
            await self.preferences.confirm(
                me, MemoryCandidateRef(memory_id, 1), Confirmation(preference=required)
            )


@requires_postgres
class RejectTest(PostgresPreferenceTestCase):
    async def test_rejecting_a_memory_candidate(self):
        me = self.user()
        memory_id = self.seed_candidate(me.user_id, "k", "use tabs")
        written = await self.preferences.reject_candidate(
            me, MemoryCandidateRef(memory_id, 1)
        )
        old, new = self.versions(memory_id)
        self.assertEqual(written.version_number, 2)
        self.assertEqual((old.status, new.status), ("superseded", "deprecated"))
        self.assertEqual(new.confirmation_state, "rejected")
        self.assertEqual(self.relations()[0][2:], ("supersedes", "preference rejected"))
        self.assertEqual(await self.preferences.candidates(me), ())

    async def test_a_rejected_version_keeps_the_provenance(self):
        # Codex P1 on #205: the deletion flow finds a version through its sources.
        me = self.user()
        memory_id = self.seed_candidate(me.user_id, "k", "use tabs")
        (old,) = self.versions(memory_id)
        self.execute(
            "INSERT INTO memory_sources (memory_version_id, source_type, source_ref)"
            " VALUES (:v, 'task', 'task-1')",
            v=old.id,
        )
        written = await self.preferences.reject_candidate(
            me, MemoryCandidateRef(memory_id, 1)
        )
        # The same sources, and no confirmation: the person did not confirm it.
        self.assertEqual(self.sources(written.version_id), [("task", "task-1")])

    async def test_a_held_candidate_after_a_rejection_can_be_confirmed(self):
        # Codex P2 on #205: the journal holds a high-risk candidate of a key the
        # person rejected before; it is offered and can be confirmed.
        me = self.user()
        memory_id = self.seed_candidate(me.user_id, "k", "use tabs")
        await self.preferences.reject_candidate(me, MemoryCandidateRef(memory_id, 1))
        entry = self.observe(me.user_id, "k", result="held_high_risk", content="m")
        (found,) = await self.preferences.candidates(me)
        self.assertEqual(found.entry_id, entry)
        self.assertEqual([o.scope for o in found.options], [TargetScope.USER])
        await self.preferences.confirm(
            me,
            HeldCandidateRef(entry, 0),
            Confirmation(scope=TargetScope.USER, acknowledge_high_risk=True),
        )
        versions = self.versions(memory_id)
        self.assertEqual(
            [(v.status, v.confirmation_state) for v in versions],
            [
                ("superseded", "inferred"),
                ("superseded", "rejected"),
                ("active", "confirmed"),
            ],
        )
        # A rejection is not "confirmed from".
        kinds = [r[2] for r in self.relations() if r[0] == versions[2].id]
        self.assertEqual(kinds, ["supersedes"])
        self.assertEqual(await self.preferences.candidates(me), ())

    async def test_a_held_candidate_of_a_retired_memory_is_only_rejected(self):
        me = self.user()
        memory_id = self.seed_candidate(
            me.user_id, "k", "use tabs", status="superseded"
        )
        entry = self.observe(me.user_id, "k", result="held_high_risk", content="m")
        (found,) = await self.preferences.candidates(me)
        self.assertEqual((found.memory_id, found.options), (memory_id, ()))
        await self.preferences.reject_candidate(me, HeldCandidateRef(entry, 0))
        self.assertEqual(await self.preferences.candidates(me), ())

    async def test_rejecting_a_held_candidate_answers_it_until_a_newer_one(self):
        me = self.user()
        first = self.observe(me.user_id, "k", result="held_high_risk")
        second = self.observe(me.user_id, "k", result="held_high_risk")
        with self.assertRaises(PreferenceCandidateChangedError):
            # Not the latest of its key.
            await self.preferences.reject_candidate(me, HeldCandidateRef(first, 0))
        self.assertIsNone(
            await self.preferences.reject_candidate(me, HeldCandidateRef(second, 0))
        )
        self.assertEqual(
            sorted(self.resolutions(), key=str),
            sorted(
                [(first, 0, "rejected", None), (second, 0, "rejected", None)], key=str
            ),
        )
        self.assertEqual(await self.preferences.candidates(me), ())
        self.assertEqual(self.audit_actions(), [("memory.use", "allow")])
        third = self.observe(me.user_id, "k", result="held_high_risk")
        (found,) = await self.preferences.candidates(me)
        self.assertEqual(found.entry_id, third)

    async def test_another_persons_held_candidate_does_not_exist(self):
        entry = self.observe(self.user().user_id, "k", result="held_high_risk")
        with self.assertRaises(PreferenceCandidateChangedError):
            await self.preferences.reject_candidate(
                self.user(), HeldCandidateRef(entry, 0)
            )


@requires_postgres
class ConfirmHeldTest(PostgresPreferenceTestCase):
    async def test_a_held_candidate_becomes_a_registered_memory(self):
        me = self.user()
        entry = self.observe(me.user_id, "k", result="held_high_risk", content="x y")
        written = await self.preferences.confirm(
            me,
            HeldCandidateRef(entry, 0),
            Confirmation(scope=TargetScope.USER, acknowledge_high_risk=True),
        )
        (row,) = self.versions(written.memory_id)
        self.assertEqual(
            (row.version_number, row.confirmation_state, row.content, row.title),
            (1, "confirmed", "x y", "k"),
        )
        self.assertEqual(
            row.attributes["preference"]["held"]["result"], "held_high_risk"
        )
        self.assertEqual(
            self.rows(
                "SELECT memory_id FROM memory_consolidation_keys WHERE key = 'k'"
            )[0].memory_id,
            written.memory_id,
        )
        self.assertEqual(
            [s[0] for s in self.sources(row.id)], ["conversation", "user_confirmation"]
        )
        self.assertEqual(
            self.resolutions(), [(entry, 0, "confirmed", written.memory_id)]
        )
        self.assertEqual(await self.preferences.candidates(me), ())

    async def test_a_held_candidate_replaces_the_confirmed_memory_it_contradicts(self):
        me = self.user()
        memory_id = self.seed_candidate(me.user_id, "k", "use tabs", state="confirmed")
        entry = self.observe(me.user_id, "k", result="held_confirmed", content="spaces")
        (found,) = await self.preferences.candidates(me)
        self.assertEqual(found.memory_id, memory_id)
        self.assertIs(found.evidence.consistency, Consistency.CONFLICTING)
        self.assertIs(found.evidence.risk_level, RiskLevel.LOW)
        # Held because it contradicts a confirmed memory, not for risk: no
        # acknowledgement needed.
        await self.preferences.confirm(me, HeldCandidateRef(entry, 0), USER)
        old, new = self.versions(memory_id)
        self.assertEqual((old.status, new.content), ("superseded", "spaces"))
        self.assertEqual([r[2] for r in self.relations()], ["supersedes"])

    async def test_a_widened_memory_stays_or_narrows(self):
        project = self.seed_project()
        team = self.seed_team(project)
        me = self.actor(team.contributor)
        memory_id = self.seed_candidate(me.user_id, "k", "use tabs")
        await self.preferences.confirm(
            me,
            MemoryCandidateRef(memory_id, 1),
            Confirmation(scope=TargetScope.PROJECT, project_id=project),
        )
        entry = self.observe(me.user_id, "k", result="held_widened", content="spaces")
        (found,) = await self.preferences.candidates(me)
        self.assertEqual(
            [(o.scope, o.project_id) for o in found.options],
            [(TargetScope.PROJECT, project), (TargetScope.USER, None)],
        )
        other = self.seed_project()
        self.seed_member(other, user_id=me.user_id, role=ProjectRole.CONTRIBUTOR)
        with self.assertRaises(InvalidMemoryInputError) as caught:
            await self.preferences.confirm(
                me,
                HeldCandidateRef(entry, 0),
                Confirmation(
                    scope=TargetScope.PROJECT,
                    project_id=other,
                    acknowledge_high_risk=True,
                ),
            )
        self.assertEqual(caught.exception.problem, InputProblem.NOT_ALLOWED)
        await self.preferences.confirm(
            me,
            HeldCandidateRef(entry, 0),
            Confirmation(
                scope=TargetScope.PROJECT,
                project_id=project,
                acknowledge_high_risk=True,
            ),
        )
        versions = self.versions(memory_id)
        self.assertEqual(
            [(v.scope, v.status, v.content) for v in versions],
            [
                ("user", "superseded", "use tabs"),
                ("project", "superseded", "use tabs"),
                ("project", "active", "spaces"),
            ],
        )

    async def test_a_widened_memory_of_a_non_member_is_not_found(self):
        project = self.seed_project()
        me = self.actor(self.seed_member(project, role=ProjectRole.CONTRIBUTOR))
        memory_id = self.seed_candidate(me.user_id, "k", "use tabs")
        await self.preferences.confirm(
            me,
            MemoryCandidateRef(memory_id, 1),
            Confirmation(scope=TargetScope.PROJECT, project_id=project),
        )
        self.execute(
            "DELETE FROM project_members WHERE user_id = :u",
            u=me.user_id,
        )
        entry = self.observe(me.user_id, "k", result="held_widened", content="spaces")
        with self.assertRaises(MemoryNotFoundError):
            await self.preferences.confirm(
                me,
                HeldCandidateRef(entry, 0),
                Confirmation(scope=TargetScope.USER, acknowledge_high_risk=True),
            )


@requires_postgres
class FreeTextTest(PostgresPreferenceTestCase):
    async def test_the_rule_interpreter_previews_the_requirements_example(self):
        me = self.user()
        memory_id = self.seed_candidate(
            me.user_id, "review", "subscription review first"
        )
        preview = await self.preferences.interpret(
            me,
            MemoryCandidateRef(memory_id, 1),
            "開発系のProjectだけ適用して。ただしmainへのMergeは毎回確認して",
        )
        self.assertIs(preview.interpreted_by, Interpreter.RULES)
        self.assertIs(preview.preference.scope, InterpretedScope.PROJECT_GROUP)
        self.assertEqual(preview.preference.apply_to, "開発系のProject")
        self.assertIs(preview.risk_level, RiskLevel.HIGH)
        self.assertTrue(preview.requires_acknowledgement)
        self.assertEqual(len(self.versions(memory_id)), 1)  # nothing written

        await self.preferences.confirm(
            me,
            MemoryCandidateRef(memory_id, 1),
            Confirmation(preference=preview.preference, acknowledge_high_risk=True),
        )
        _, new = self.versions(memory_id)
        self.assertEqual((new.scope, new.owner_user_id), ("user", me.user_id))
        self.assertEqual(new.content, preview.content)
        structured = new.attributes["preference"]["structured"]
        self.assertEqual(structured["scope"], "project_group")
        self.assertEqual(structured["apply_to"], "開発系のProject")

    async def test_a_scope_phrase_takes_the_ids_of_the_observed_repository(self):
        project = self.seed_project()
        me = self.actor(self.seed_member(project, role=ProjectRole.CONTRIBUTOR))
        repo = self.seed_repo(project)
        memory_id = self.seed_candidate(me.user_id, "k", "use tabs")
        self.observe(me.user_id, "k", project=project, repo=repo)
        preview = await self.preferences.interpret(
            me, MemoryCandidateRef(memory_id, 1), "このRepoだけで"
        )
        self.assertEqual(
            (
                preview.preference.scope,
                preview.preference.project_id,
                preview.preference.repo_id,
            ),
            (InterpretedScope.REPO, project, repo),
        )
        self.assertIs(preview.risk_level, RiskLevel.LOW)

    async def test_the_model_answers_first_and_its_risk_only_raises(self):
        me = self.user()
        memory_id = self.seed_candidate(me.user_id, "k", "use tabs")
        model = FakeInterpreter(
            json.dumps(
                {
                    "scope": "user",
                    "apply_to": None,
                    "rule": "use tabs in Go",
                    "exceptions": ["not in YAML"],
                    "strength": "default",
                    "risk_level": "high",
                    "expires_at": (T0 + timedelta(days=30)).isoformat(),
                }
            )
        )
        service = self.new_preferences(interpreter=model)
        preview = await service.interpret(
            me, MemoryCandidateRef(memory_id, 1), "Goだけ。YAMLは除く。今月だけ"
        )
        self.assertEqual(model.calls, [("Goだけ。YAMLは除く。今月だけ", "use tabs")])
        self.assertIs(preview.interpreted_by, Interpreter.MODEL)
        self.assertEqual(preview.preference.rule, "use tabs in Go")
        self.assertIs(preview.risk_level, RiskLevel.HIGH)

        await service.confirm(
            me,
            MemoryCandidateRef(memory_id, 1),
            Confirmation(preference=preview.preference, acknowledge_high_risk=True),
        )
        _, new = self.versions(memory_id)
        self.assertEqual(new.freshness_policy, "expiring")
        self.assertEqual(new.expires_at, T0 + timedelta(days=30))
        self.assertEqual(new.content, "use tabs in Go\n例外: not in YAML")

    async def test_a_failing_or_invalid_model_falls_back_to_the_rules(self):
        me = self.user()
        memory_id = self.seed_candidate(me.user_id, "k", "use tabs")
        for answer in (RuntimeError("down"), "not json", json.dumps({"scope": "x"})):
            with self.subTest(answer=str(answer)[:20]):
                service = self.new_preferences(interpreter=FakeInterpreter(answer))
                preview = await service.interpret(
                    me, MemoryCandidateRef(memory_id, 1), "すべてのProjectで"
                )
                self.assertIs(preview.interpreted_by, Interpreter.RULES)
                self.assertIs(preview.preference.scope, InterpretedScope.USER)

    async def test_an_expiry_in_the_past_is_refused(self):
        me = self.user()
        memory_id = self.seed_candidate(me.user_id, "k", "use tabs")
        expired = StructuredPreference(
            InterpretedScope.USER, "use tabs", expires_at=T0 - timedelta(days=1)
        )
        with self.assertRaises(InvalidMemoryInputError):
            await self.preferences.confirm(
                me, MemoryCandidateRef(memory_id, 1), Confirmation(preference=expired)
            )
        self.assertEqual(len(self.versions(memory_id)), 1)

    async def test_only_a_live_candidate_can_be_interpreted(self):
        me = self.user()
        memory_id = self.seed_candidate(me.user_id, "k", "use tabs", state="confirmed")
        with self.assertRaises(PreferenceCandidateChangedError):
            await self.preferences.interpret(
                me, MemoryCandidateRef(memory_id, 1), "このRepoだけ"
            )
        with self.assertRaises(InvalidMemoryInputError):
            await self.preferences.interpret(me, MemoryCandidateRef(memory_id, 1), "  ")
