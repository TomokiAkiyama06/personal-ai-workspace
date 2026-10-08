"""Observation -> Candidate -> Confirmation -> Confirmed, end to end (PAW-044).

Unlike the other preference tests, this one goes through the Immediate Journal and
the consolidator of PAW-041 (with a scripted Memory Worker): the person's messages
are the observations, the consolidator writes or holds the candidates, and the
confirmation flow reads them, recommends a scope and writes the person's answer.
It then checks that the consolidator respects the answer (a later, different
candidate is held for the person instead of replacing the confirmed memory).
"""

from paw_backend.authz import ProjectRole
from paw_backend.memory.journal import ConsolidationQueue, Consolidator, MemoryJournal
from paw_backend.memory.journal.domain import RunOutcome
from paw_backend.memory.preferences import (
    CandidateKind,
    Confirmation,
    Consistency,
    HeldCandidateRef,
    MemoryCandidateRef,
    PreferenceHighRiskError,
    RiskLevel,
    TargetScope,
)

from .journal_support import ScriptedWorker, memory, worker_output
from .preference_support import PostgresPreferenceTestCase, requires_postgres


@requires_postgres
class EndToEndTest(PostgresPreferenceTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.worker = ScriptedWorker()
        self.journal = MemoryJournal(self._database(), self.authorizer)
        queue = ConsolidationQueue(self._database())
        self.consolidator = Consolidator(
            self._database(),
            queue,
            self.worker,
            worker_id="worker-1",
            worker_timeout_seconds=5,
        )

    async def say(self, actor, text, answer, *, project=None, repo=None):
        """One message of ``actor`` in a new conversation, consolidated with the
        worker's ``answer``."""
        conversation = self.execute(
            "INSERT INTO conversations (owner_user_id, project_id, repo_id)"
            " VALUES (:o, :p, :r) RETURNING id",
            o=actor.user_id,
            p=project,
            r=repo,
        ).scalar_one()
        await self.journal.record_user_message(actor, conversation, text)
        self.worker.responses.append(answer)
        result = await self.consolidator.run_once()
        self.assertEqual(result.outcome, RunOutcome.COMPLETED)
        return result

    async def test_a_repeated_preference_is_confirmed_and_then_respected(self):
        project = self.seed_project()
        me = self.actor(self.seed_member(project, role=ProjectRole.CONTRIBUTOR))
        repo = self.seed_repo(project)
        tabs = worker_output(memory("indent_style", content="use tabs"))

        # Observations: the same preference three times in one repository.
        for _ in range(3):
            await self.say(me, "tabs please", tabs, project=project, repo=repo)

        # Candidate: inferred, private, with its evidence and a Repo recommendation.
        (candidate,) = await self.preferences.candidates(me)
        self.assertIs(candidate.kind, CandidateKind.MEMORY)
        self.assertEqual(candidate.confirmation_state.value, "inferred")
        self.assertEqual(candidate.evidence.frequency, 3)
        self.assertEqual(candidate.recommendation.scope, TargetScope.REPO)
        self.assertEqual(candidate.recommendation.repo_id, repo)
        self.assertTrue(candidate.ready)

        # Confirmation: the person keeps it as their own (all projects).
        confirmed = await self.preferences.confirm(
            me,
            MemoryCandidateRef(candidate.memory_id, candidate.version_number),
            Confirmation(scope=TargetScope.USER),
        )
        self.assertEqual(confirmed.confirmation_state.value, "confirmed")
        self.assertEqual(await self.preferences.candidates(me), ())

        # A later, different candidate does not replace the confirmed memory: the
        # consolidator holds it, and it comes back as a question (a conflict).
        await self.say(
            me,
            "spaces please",
            worker_output(memory("indent_style", content="use spaces")),
        )
        (held,) = await self.preferences.candidates(me)
        self.assertIs(held.kind, CandidateKind.HELD)
        self.assertEqual(held.held_reason.value, "held_confirmed")
        self.assertIs(held.evidence.consistency, Consistency.CONFLICTING)
        self.assertEqual(held.memory_id, candidate.memory_id)
        active = self.rows(
            "SELECT content FROM memory_versions WHERE memory_id = :m"
            " AND status = 'active'",
            m=candidate.memory_id,
        )
        self.assertEqual([row.content for row in active], ["use tabs"])

        # [保存しない]: answered; the same words again are not asked again.
        await self.preferences.reject_candidate(
            me, HeldCandidateRef(held.entry_id, held.item_index)
        )
        self.assertEqual(await self.preferences.candidates(me), ())

    async def test_a_high_risk_inference_is_held_and_never_applied_on_a_guess(self):
        me = self.user()
        risky = worker_output(
            memory("merge_policy", content="merge to main without review")
        )
        for _ in range(3):
            await self.say(me, "just merge it", risky)
        # Never written as a memory, not even an inferred one.
        self.assertEqual(self.rows("SELECT id FROM memory_versions"), [])
        (held,) = await self.preferences.candidates(me)
        self.assertEqual(held.held_reason.value, "held_high_risk")
        self.assertIs(held.evidence.risk_level, RiskLevel.HIGH)
        ref = HeldCandidateRef(held.entry_id, held.item_index)
        with self.assertRaises(PreferenceHighRiskError):
            await self.preferences.confirm(
                me, ref, Confirmation(scope=TargetScope.USER)
            )
        self.assertEqual(self.rows("SELECT id FROM memory_versions"), [])
        written = await self.preferences.confirm(
            me,
            ref,
            Confirmation(scope=TargetScope.USER, acknowledge_high_risk=True),
        )
        self.assertEqual(written.confirmation_state.value, "confirmed")
