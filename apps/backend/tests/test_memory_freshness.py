"""The freshness jobs on a real PostgreSQL (PAW-042).

``revalidate`` and ``repo_commit`` memories become stale candidates (still active,
lower priority in retrieval), ``expiring`` ones are deprecated at their expiry,
``session_only`` ones when their session or task ends. Every change is recorded with the
``system`` actor, and running a job again changes nothing.
"""

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import text

from paw_backend.memory.versioning import (
    InvalidMemoryInputError,
    RevalidateTrigger,
    TriggerTarget,
)

from .retrieval_pg_support import titles
from .versioning_support import T0, PostgresVersioningTestCase, requires_postgres

QUERY = "deploy backend friday"
SHA_A, SHA_B = "a" * 40, "b" * 40


@requires_postgres
class RevalidateTest(PostgresVersioningTestCase):
    def revalidate(self, owner, *, verified_days_ago, triggers=None, title="fact"):
        seeded = self.seed(
            title,
            "deploy backend friday",
            owner=owner,
            freshness="revalidate",
            verified_at=T0 - timedelta(days=verified_days_ago),
            revalidate_after=timedelta(days=90),
        )
        if triggers:
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE memory_versions SET revalidate_triggers = :t"
                        " WHERE id = :v"
                    ),
                    {"t": triggers, "v": seeded.version_id},
                )
        return seeded

    async def test_a_due_memory_becomes_a_stale_candidate_not_invalid(self):
        me = self.user()
        due = self.revalidate(me.user_id, verified_days_ago=90, title="due")
        fresh = self.revalidate(me.user_id, verified_days_ago=89, title="fresh")
        self.assertEqual(await self.freshness.mark_revalidation_due(), 1)
        (row,) = self.versions(due.memory_id)
        self.assertEqual((row.status, row.stale_since), ("active", T0))
        self.assertIsNone(self.versions(fresh.memory_id)[0].stale_since)
        (change,) = self.changes(due.version_id)
        self.assertEqual((change.old_stale_since, change.new_stale_since), (None, T0))
        self.assertEqual((change.actor_type, change.actor_user_id), ("system", None))
        # Still offered, marked stale and lowered.
        result = await self.retrieve(me, QUERY)
        by_title = {hit.title: hit for hit in result.hits}
        self.assertEqual(by_title["due"].freshness.value, "stale")
        self.assertEqual(by_title["due"].stale_reason.value, "marked_stale")
        self.assertLess(by_title["due"].score, by_title["fresh"].score)
        # Running it again changes nothing.
        self.clock.advance(hours=1)
        self.assertEqual(await self.freshness.mark_revalidation_due(), 0)
        self.assertEqual(len(self.changes(due.version_id)), 1)

    async def test_an_event_marks_only_the_memories_that_wait_for_it(self):
        me, other = self.user(), self.user()
        waits = self.revalidate(
            me.user_id, verified_days_ago=1, triggers=["model_changed"], title="waits"
        )
        other_trigger = self.revalidate(
            me.user_id, verified_days_ago=1, triggers=["member_changed"], title="o"
        )
        someone_else = self.revalidate(
            other.user_id, verified_days_ago=1, triggers=["model_changed"]
        )
        marked = await self.freshness.mark_triggered(
            RevalidateTrigger.MODEL_CHANGED, TriggerTarget.user(me.user_id)
        )
        self.assertEqual(marked, 1)
        self.assertEqual(self.versions(waits.memory_id)[0].stale_since, T0)
        for untouched in (other_trigger, someone_else):
            self.assertIsNone(self.versions(untouched.memory_id)[0].stale_since)
        workspace = await self.freshness.mark_triggered(
            RevalidateTrigger.MODEL_CHANGED, TriggerTarget.workspace()
        )
        self.assertEqual(workspace, 1)  # the other user's; the first is marked already

    async def test_a_project_event_concerns_that_project(self):
        project, elsewhere = uuid4(), uuid4()
        for pid in (project, elsewhere):
            seeded = self.seed(
                "members",
                scope="project",
                project=pid,
                freshness="revalidate",
                verified_at=T0,
                revalidate_after=timedelta(days=90),
            )
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE memory_versions SET revalidate_triggers ="
                        " ARRAY['member_changed'] WHERE id = :v"
                    ),
                    {"v": seeded.version_id},
                )
        marked = await self.freshness.mark_triggered(
            RevalidateTrigger.MEMBER_CHANGED, TriggerTarget.project(project)
        )
        self.assertEqual(marked, 1)
        rows = self.rows(
            "SELECT project_id, stale_since FROM memory_versions ORDER BY project_id"
        )
        self.assertEqual(
            {row.project_id: row.stale_since for row in rows},
            {project: T0, elsewhere: None},
        )

    async def test_only_active_versions_are_marked(self):
        me = self.user()
        old = self.seed(
            "old",
            owner=me.user_id,
            status="superseded",
            freshness="revalidate",
            verified_at=T0 - timedelta(days=400),
            revalidate_after=timedelta(days=90),
        )
        self.assertEqual(await self.freshness.mark_revalidation_due(), 0)
        self.assertIsNone(self.versions(old.memory_id)[0].stale_since)

    async def test_the_batch_bounds_one_call(self):
        me = self.user()
        for n in range(3):
            self.revalidate(me.user_id, verified_days_ago=100, title=f"f{n}")
        small = self.new_freshness(batch=2)
        self.assertEqual(await small.mark_revalidation_due(), 2)
        self.assertEqual(await small.mark_revalidation_due(), 1)
        self.assertEqual(await small.mark_revalidation_due(), 0)


@requires_postgres
class RepoCommitTest(PostgresVersioningTestCase):
    def repo_memory(self, repo, sha, branch=None, title="repo fact"):
        seeded = self.seed(
            title, scope="repo", repo=repo, freshness="repo_commit", commit_sha=sha
        )
        if branch is not None:
            with self.engine.begin() as connection:
                connection.execute(
                    text("UPDATE memory_versions SET branch = :b WHERE id = :v"),
                    {"b": branch, "v": seeded.version_id},
                )
        return seeded

    async def test_a_moved_head_makes_the_repo_memory_stale(self):
        repo, other_repo = uuid4(), uuid4()
        behind = self.repo_memory(repo, SHA_A, "main")
        current = self.repo_memory(repo, SHA_B, "main")
        feature = self.repo_memory(repo, SHA_A, "feature")
        no_branch = self.repo_memory(repo, SHA_A)
        elsewhere = self.repo_memory(other_repo, SHA_A)
        marked = await self.freshness.mark_repo_head(repo, SHA_B, branch="main")
        self.assertEqual(marked, 2)
        stale = {
            seeded: self.versions(seeded.memory_id)[0].stale_since
            for seeded in (behind, current, feature, no_branch, elsewhere)
        }
        self.assertEqual(stale[behind], T0)
        self.assertEqual(stale[no_branch], T0)
        self.assertIsNone(stale[current])
        self.assertIsNone(stale[feature])
        self.assertIsNone(stale[elsewhere])

    async def test_a_malformed_commit_is_refused_before_anything_runs(self):
        with self.assertRaises(InvalidMemoryInputError):
            await self.freshness.mark_repo_head(uuid4(), "HEAD")


@requires_postgres
class ExpiryAndSessionTest(PostgresVersioningTestCase):
    async def test_an_expired_memory_is_deprecated_and_kept(self):
        me = self.user()
        expired = self.seed(
            "this month only",
            "deploy backend friday",
            owner=me.user_id,
            freshness="expiring",
            expires_at=T0,
        )
        later = self.seed(
            "next month",
            "deploy backend friday",
            owner=me.user_id,
            freshness="expiring",
            expires_at=T0 + timedelta(days=30),
        )
        self.assertEqual(await self.freshness.expire_due(), 1)
        self.assertEqual(self.versions(expired.memory_id)[0].status, "deprecated")
        self.assertEqual(self.versions(later.memory_id)[0].status, "active")
        (change,) = self.changes(expired.version_id)
        self.assertEqual(
            (change.new_status, change.actor_type), ("deprecated", "system")
        )
        self.assertEqual(titles(await self.retrieve(me, QUERY)), ["next month"])
        self.assertEqual(await self.freshness.expire_due(), 0)

    async def test_session_only_memories_end_with_their_session(self):
        me = self.user()
        conversation, other = uuid4(), uuid4()
        with self.engine.begin() as connection:
            for cid in (conversation, other):
                connection.execute(
                    text(
                        "INSERT INTO conversations (id, owner_user_id) VALUES (:c, :o)"
                    ),
                    {"c": cid, "o": me.user_id},
                )
        mine = self.seed("session note", owner=me.user_id, freshness="session_only")
        theirs = self.seed("other session", owner=me.user_id, freshness="session_only")
        long_term = self.seed("long term", owner=me.user_id)
        with self.engine.begin() as connection:
            for seeded, cid in (
                (mine, conversation),
                (theirs, other),
                (long_term, conversation),
            ):
                connection.execute(
                    text(
                        "INSERT INTO memory_sources (memory_version_id, source_type,"
                        " conversation_id) VALUES (:v, 'conversation', :c)"
                    ),
                    {"v": seeded.version_id, "c": cid},
                )
        self.assertEqual(await self.freshness.end_session(conversation), 1)
        statuses = {
            seeded: self.versions(seeded.memory_id)[0].status
            for seeded in (mine, theirs, long_term)
        }
        self.assertEqual(
            statuses,
            {mine: "deprecated", theirs: "active", long_term: "active"},
        )

    def task_source(self, seeded, ref, kind="task"):
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO memory_sources (memory_version_id, source_type,"
                    " source_ref) VALUES (:v, :k, :r)"
                ),
                {"v": seeded.version_id, "k": kind, "r": ref},
            )

    async def test_session_only_memories_end_with_their_task(self):
        # REQUIREMENTS.md: session_only is not kept after a Session or a Task ends.
        me = self.user()
        task, other = uuid4(), uuid4()
        mine = self.seed("task note", owner=me.user_id, freshness="session_only")
        theirs = self.seed("other task", owner=me.user_id, freshness="session_only")
        long_term = self.seed("long term", owner=me.user_id)
        # Only the canonical text of the task id names the task.
        near_miss = self.seed("near miss", owner=me.user_id, freshness="session_only")
        self.task_source(mine, str(task))
        self.task_source(theirs, str(other))
        self.task_source(long_term, str(task))
        self.task_source(near_miss, f"{task}-x")
        self.task_source(near_miss, str(task).upper())
        # Only a task source names a task.
        analysis = self.seed("analysis", owner=me.user_id, freshness="session_only")
        self.task_source(analysis, str(task), kind="repo_analysis")
        self.assertEqual(await self.freshness.end_task(task), 1)
        statuses = {
            seeded: self.versions(seeded.memory_id)[0].status
            for seeded in (mine, theirs, long_term, near_miss, analysis)
        }
        self.assertEqual(
            statuses,
            {
                mine: "deprecated",
                theirs: "active",
                long_term: "active",
                near_miss: "active",
                analysis: "active",
            },
        )
        (change,) = self.changes(mine.version_id)
        self.assertEqual(
            (change.old_status, change.new_status, change.actor_type),
            ("active", "deprecated", "system"),
        )
        # Nothing is erased, and running it again changes nothing.
        self.assertEqual(len(self.versions(mine.memory_id)), 1)
        self.assertEqual(await self.freshness.end_task(task), 0)
        self.assertEqual(len(self.changes(mine.version_id)), 1)

    async def test_a_session_end_does_not_retire_a_task_memory(self):
        # A task source has no conversation; a conversation source has no task.
        me = self.user()
        task = uuid4()
        seeded = self.seed("task note", owner=me.user_id, freshness="session_only")
        self.task_source(seeded, str(task))
        self.assertEqual(await self.freshness.end_session(task), 0)
        self.assertEqual(self.versions(seeded.memory_id)[0].status, "active")

    async def test_ending_a_task_is_batched_and_skips_locked_versions(self):
        me = self.user()
        task = uuid4()
        seeded = [
            self.seed(f"note {n}", owner=me.user_id, freshness="session_only")
            for n in range(3)
        ]
        for one in seeded:
            self.task_source(one, str(task))
        freshness = self.new_freshness(batch=2)
        with self.engine.connect() as locker:
            locker.execute(
                text("SELECT id FROM memory_versions WHERE id = :v FOR UPDATE"),
                {"v": seeded[0].version_id},
            )
            self.assertEqual(await freshness.end_task(task), 2)
            self.assertEqual(await freshness.end_task(task), 0)
            locker.rollback()
        self.assertEqual(await freshness.end_task(task), 1)
        self.assertEqual(
            {self.versions(one.memory_id)[0].status for one in seeded},
            {"deprecated"},
        )

    async def test_the_jobs_validate_their_arguments(self):
        with self.assertRaises(InvalidMemoryInputError):
            await self.freshness.end_session(str(uuid4()))
        with self.assertRaises(InvalidMemoryInputError):
            await self.freshness.end_task(str(uuid4()))
        with self.assertRaises(InvalidMemoryInputError):
            await self.freshness.mark_triggered(
                "model_changed", TriggerTarget.workspace()
            )
        with self.assertRaises(InvalidMemoryInputError):
            self.new_freshness(batch=0)
