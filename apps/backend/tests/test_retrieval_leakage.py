"""Permission leakage = 0 (PAW-043): explicit cases and randomised universes.

The claim: nothing a caller may not read ever influences what they get back.
Not only "no invisible memory in the hits", but no invisible memory in a
conflict group, a duplicate list, a Reranker's input, a rank, a score or a
flag. It is proved two ways:

* **Explicit attacks**: a memory of someone else whose embedding is exactly the
  query, a conflict with an invisible memory, an invisible copy that would win
  the deduplication, an invisible successor, a foreign project named in the
  query, ...
* **Randomised universes** (``random.Random(seed)``, so a failure reproduces): users,
  projects (in every lifecycle state), memberships (accepted and invited), repositories
  with ACL overrides, project groups, memories of every scope and status, freshness
  policies, relations. For every caller and query:

  1. **Oracle**: every returned id (hits, conflict groups, duplicates) is in the
     set of memories that an independent, plain-Python implementation of the
     permission rules says the caller may read.
  2. **Non-interference**: the result equals the result on the same database with
     every memory the oracle says the caller may NOT read switched off
     (``status = 'history'``, which every stage ignores, i.e. as if it did not
     exist). Equal means every field: order, ranks, scores, flags, groups and
     counts. So no count, score or flag can carry information about a hidden row.
  3. **Reranker**: every text a Reranker was shown belongs to a readable memory.

The tests are not vacuous: each universe is checked to contain readable and
unreadable memories that match the query, and the totals are asserted.
"""

import random
import unittest
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import UUID, uuid4

from paw_backend.authz import (
    Principal,
    ProjectRole,
    RepoAcl,
    RepoPermission,
    SystemRole,
)
from paw_backend.memory.retrieval import RetrievalQuery, RetrievalResult
from paw_backend.memory.shared import StaticPolicySource, SystemPolicyItem
from paw_backend.projects import MemberStatus, ProjectStatus

from .retrieval_pg_support import (
    T0,
    FixedEmbedder,
    PostgresRetrievalTestCase,
    RecordingReranker,
    StaticGroups,
    StaticRepoAcls,
    requires_postgres,
)

VOCABULARY = "alpha beta gamma delta epsilon zeta eta theta iota kappa".split()
STATES = [
    ProjectStatus.ACTIVE,
    ProjectStatus.ACTIVE,
    ProjectStatus.ARCHIVED,
    ProjectStatus.PENDING_DELETION,
]
READABLE_STATES = {ProjectStatus.ACTIVE, ProjectStatus.ARCHIVED}


def repo_acls_of(universe):
    """A RepoAclSource answer: the repositories of the projects it is asked about."""

    def answer(_user, project_ids):
        return [a for a in universe.acls if a.project_id in project_ids]

    return answer


@dataclass
class Memory:
    version_id: UUID
    marker: str
    scope: str
    status: str
    owner: UUID | None = None
    project: UUID | None = None
    repo: UUID | None = None
    group: UUID | None = None
    freshness: str = "permanent"
    expires_at: object = None
    text: str = ""
    subjects: tuple[str, ...] = ()
    marked_stale: bool = False
    verified_days_ago: int | None = None
    commit_sha: str | None = None


# What the strict variant asks of the retrieval (see ``Universe.ineligible``).
POLICY_SUBJECT = "merge"
SUBJECT_CHOICES = ["merge", "merge.permission", "docs.api", "deploy", "mergeable"]
CURRENT, MOVED = "c" * 40, "d" * 40


@dataclass
class Universe:
    callers: list[Principal] = field(default_factory=list)
    projects: dict[UUID, ProjectStatus] = field(default_factory=dict)
    members: dict[tuple[UUID, UUID], MemberStatus] = field(default_factory=dict)
    acls: list[RepoAcl] = field(default_factory=list)
    groups: dict[UUID, set[UUID]] = field(default_factory=dict)
    memories: list[Memory] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    relations: list[tuple[UUID, UUID, str]] = field(default_factory=list)
    heads: dict[UUID, str] = field(default_factory=dict)

    def oracle(self, caller: Principal) -> set[UUID]:
        """The version ids ``caller`` may read: the rules, written out plainly."""
        if caller.system_role is SystemRole.SYSTEM:
            return set()
        projects = {
            project
            for project, state in self.projects.items()
            if state in READABLE_STATES
            and self.members.get((project, caller.user_id)) is MemberStatus.ACTIVE
        }
        repos = {
            acl.repo_id
            for acl in self.acls
            if acl.project_id in projects
            and (acl.allowed is None or RepoPermission.READ in acl.allowed)
        }
        groups = self.groups.get(caller.user_id, set())
        readable: set[UUID] = set()
        for m in self.memories:
            if m.status != "active" or m.freshness == "session_only":
                continue
            if m.freshness == "expiring" and m.expires_at <= T0:
                continue
            if (
                (m.scope == "shared")
                or (m.scope == "user" and m.owner == caller.user_id)
                or (m.scope == "project" and m.project in projects)
                or (m.scope == "repo" and m.repo in repos)
                or (m.scope == "project_group" and m.group in groups)
            ):
                readable.add(m.version_id)
        return readable


def _covered(subjects: tuple[str, ...]) -> bool:
    return any(
        subject == POLICY_SUBJECT or subject.startswith(POLICY_SUBJECT + ".")
        for subject in subjects
    )


def ineligible(u: Universe, readable: set[UUID]) -> set[UUID]:
    """Readable memories that must not be candidates in the strict variant.

    Written out plainly, independent of the SQL: stale ones (marked, ``revalidate``
    past 90 days, ``repo_commit`` of another commit than the repository's head),
    shared ones the System Policy covers, and ones a readable successor supersedes.
    """
    found: set[UUID] = set()
    for m in u.memories:
        if m.version_id not in readable:
            continue
        stale = (
            m.marked_stale
            or (m.freshness == "revalidate" and m.verified_days_ago >= 90)
            or (
                m.freshness == "repo_commit"
                and m.repo in u.heads
                and u.heads[m.repo] != m.commit_sha
            )
        )
        if stale or (m.scope == "shared" and _covered(m.subjects)):
            found.add(m.version_id)
    for newer, older, kind in u.relations:
        if kind == "supersedes" and newer in readable and older in readable:
            found.add(older)
    return found


def successors_of_readable(u: Universe, readable: set[UUID]) -> set[UUID]:
    return {
        newer
        for newer, older, kind in u.relations
        if kind == "supersedes" and newer in readable and older in readable
    }


@requires_postgres
class LeakageTestCase(PostgresRetrievalTestCase):
    """Helpers shared by the explicit and the randomised tests."""

    def assert_only(self, result: RetrievalResult, allowed: set[UUID]) -> None:
        shown = {h.version_id for h in result.hits}
        for hit in result.hits:
            shown.update(hit.duplicates)
        for group in result.conflicts:
            shown.update(group.version_ids)
        self.assertEqual(shown - allowed, set())


@requires_postgres
class ExplicitLeakageTest(LeakageTestCase):
    async def test_a_query_equal_to_a_private_embedding_returns_nothing_of_it(
        self,
    ):
        bob, alice = self.user(), self.user()
        embedder = FixedEmbedder({"q": [1.0, 0.0, 0.0]}, model_id="fixed-test-model")
        secret = self.seed(
            "bob secret",
            "hidden text",
            owner=bob.user_id,
            embedding=[1.0, 0.0, 0.0],
            model_id="fixed-test-model",
        )
        mine = self.seed(
            "alice note",
            "other",
            owner=alice.user_id,
            embedding=[0.0, 1.0, 0.0],
            model_id="fixed-test-model",
        )
        retriever = self.new_retriever(embedder=embedder)
        result = await self.retrieve(alice, "q", retriever=retriever)
        self.assertEqual([h.version_id for h in result.hits], [mine.version_id])
        # The nearest memory of the whole database is Bob's (distance 0), yet
        # Alice's own memory is the first, and only, vector candidate.
        self.assertEqual(result.hits[0].vector_rank, 1)
        self.assertEqual(
            secret.version_id in {h.version_id for h in result.hits}, False
        )

    async def test_hidden_memories_never_change_a_rank_a_score_or_a_flag(self):
        alice, bob = self.user(), self.user()
        first = self.seed("first", "alpha beta gamma delta", owner=alice.user_id)
        second = self.seed("second", "alpha beta", owner=alice.user_id)
        before = await self.retrieve(alice, "alpha beta gamma")
        # A hidden memory that beats both on every axis: a verbatim copy of the
        # query, a confirmed, pinned, important one, next to the same embedding.
        self.seed(
            "bob", "alpha beta gamma", owner=bob.user_id, importance=100, pinned=True
        )
        self.seed("bob2", "alpha beta gamma delta", owner=bob.user_id)
        after = await self.retrieve(alice, "alpha beta gamma")
        self.assertEqual(after, before)
        self.assertEqual(
            [h.version_id for h in after.hits], [first.version_id, second.version_id]
        )
        self.assertEqual([h.keyword_rank for h in after.hits], [1, 2])
        self.assertEqual([h.vector_rank for h in after.hits], [1, 2])

    async def test_a_conflict_with_a_hidden_memory_is_not_a_group_and_not_a_partner(
        self,
    ):
        alice, bob = self.user(), self.user()
        mine = self.seed("mine", "alpha beta gamma", owner=alice.user_id)
        theirs = self.seed("theirs", "the opposite of alpha", owner=bob.user_id)
        before = await self.retrieve(alice, "alpha beta")
        self.seed_relation(theirs.version_id, mine.version_id, "conflicts_with")
        reranker = RecordingReranker()
        retriever = self.new_retriever(reranker=reranker)
        after = await self.retrieve(alice, "alpha beta", retriever=retriever)
        self.assertEqual(after.conflicts, ())
        self.assertFalse(after.conflicts_incomplete)
        self.assertEqual(after.dropped_conflict_groups, 0)
        self.assertEqual([h.version_id for h in after.hits], [mine.version_id])
        self.assertIsNone(after.hits[0].conflict_group)
        self.assertNotIn("the opposite of alpha", reranker.seen_texts)
        without_reranker = await self.retrieve(alice, "alpha beta")
        self.assertEqual(without_reranker, before)

    async def test_a_hidden_copy_that_would_win_dedup_does_not_swallow_a_visible_one(
        self,
    ):
        alice, bob = self.user(), self.user()
        text = "deploy the backend every friday after the merge is green"
        mine = self.seed(
            "Rule", text, owner=alice.user_id, confirmation="inferred", importance=0
        )
        # Bob's copy is confirmed, important and pinned: it would win every tie.
        self.seed("Rule", text, owner=bob.user_id, importance=100, pinned=True)
        result = await self.retrieve(alice, "deploy backend friday")
        self.assertEqual([h.version_id for h in result.hits], [mine.version_id])
        self.assertEqual(result.hits[0].duplicates, ())

    async def test_hidden_conflict_partners_are_not_counted_not_even_as_incomplete(
        self,
    ):
        from paw_backend.memory.retrieval import limits

        alice, bob = self.user(), self.user()
        hub = self.seed("hub", "alpha beta gamma", owner=alice.user_id)
        before = await self.retrieve(alice, "alpha beta")
        for n in range(limits.MAX_CONFLICT_PARTNERS + 5):
            hidden = self.seed(f"hidden {n}", "unrelated", owner=bob.user_id)
            # Both directions: the hub is the newer end, then the older one.
            if n % 2:
                self.seed_relation(hub.version_id, hidden.version_id)
            else:
                self.seed_relation(hidden.version_id, hub.version_id)
        after = await self.retrieve(alice, "alpha beta")
        self.assertFalse(after.conflicts_incomplete)
        self.assertEqual(after, before)

    async def test_a_hidden_successor_does_not_hide_the_visible_memory(self):
        alice, bob = self.user(), self.user()
        old = self.seed("old", "alpha beta gamma", owner=alice.user_id)
        new = self.seed("new", "alpha beta gamma delta", owner=bob.user_id)
        self.seed_relation(new.version_id, old.version_id, "supersedes")
        result = await self.retrieve(alice, "alpha beta gamma")
        self.assertEqual([h.version_id for h in result.hits], [old.version_id])

    async def test_the_reranker_and_the_embedder_never_see_hidden_text(self):
        alice, bob = self.user(), self.user()
        self.seed("mine", "alpha beta", owner=alice.user_id)
        self.seed("hidden-title", "hidden-body alpha beta", owner=bob.user_id)
        reranker = RecordingReranker()
        embedded = []

        class Spy(type(self.embedder)):
            async def embed(inner, texts):
                embedded.extend(texts)
                return await super().embed(texts)

        retriever = self.new_retriever(reranker=reranker, embedder=Spy())
        await self.retrieve(alice, "alpha beta", retriever=retriever)
        joined = " ".join(reranker.seen_texts)
        self.assertNotIn("hidden", joined)
        self.assertEqual(embedded, ["alpha beta"])

    async def test_naming_a_foreign_project_is_indistinguishable_from_naming_none(self):
        alice = self.user()
        theirs = self.seed_project()
        self.member_of(theirs)
        self.seed("theirs", "alpha beta", scope="project", project=theirs)
        self.seed("mine", "alpha beta", owner=alice.user_id)
        foreign = await self.retrieve(alice, "alpha beta", project_ids=[theirs])
        nonexistent = await self.retrieve(alice, "alpha beta", project_ids=[uuid4()])
        self.assertEqual(foreign, nonexistent)
        self.assertEqual([h.title for h in foreign.hits], ["mine"])

    async def test_a_foreign_repository_stays_hidden_even_if_the_source_lists_it(
        self,
    ):
        alice = self.user()
        theirs = self.seed_project()
        repo = self.new_repo_id()
        self.seed("repo secret", "alpha beta", scope="repo", repo=repo)
        retriever = self.new_retriever(
            repo_acls=StaticRepoAcls([RepoAcl.inherit(repo, theirs)])
        )
        result = await self.retrieve(alice, "alpha beta", retriever=retriever)
        self.assertEqual(result.hits, ())

    async def test_an_old_private_version_stays_private_after_the_memory_was_widened(
        self,
    ):
        project = self.seed_project()
        alice, bob = self.member_of(project), self.member_of(project)
        private = self.seed(
            "draft",
            "alpha beta secret-draft-wording",
            owner=alice.user_id,
            status="superseded",
        )
        self.seed(
            "published",
            "alpha beta",
            scope="project",
            project=project,
            memory_id=private.memory_id,
            version_number=2,
        )
        for who in (alice, bob):
            result = await self.retrieve(who, "alpha beta secret-draft-wording")
            self.assertEqual([h.title for h in result.hits], ["published"])
            self.assertNotIn("secret-draft-wording", repr(result))

    async def test_no_field_of_a_result_is_about_something_that_was_filtered_out(self):
        alice, bob = self.user(), self.user()
        self.seed("mine", "alpha", owner=alice.user_id)
        for n in range(30):
            self.seed(f"hidden {n}", "alpha alpha alpha", owner=bob.user_id)
        result = await self.retrieve(alice, "alpha")
        allowed = {
            "hits",
            "conflicts",
            "dropped_conflict_groups",
            "conflicts_incomplete",
            "degraded",
        }
        self.assertEqual(set(result.__dataclass_fields__), allowed)
        self.assertEqual(len(result.hits), 1)
        self.assertEqual(result.hits[0].keyword_rank, 1)
        self.assertEqual(result.hits[0].vector_rank, 1)
        self.assertEqual(result.dropped_conflict_groups, 0)


@requires_postgres
class RandomisedLeakageTest(LeakageTestCase):
    SEEDS = range(1, 9)
    MEMORIES = 70

    def build(self, seed: int) -> Universe:
        rng = random.Random(seed)
        u = Universe()
        roles = [SystemRole.USER] * 3 + [SystemRole.ADMIN, SystemRole.OWNER]
        u.callers = [self.user(role) for role in roles]
        u.callers.append(Principal(uuid4(), SystemRole.SYSTEM))
        real = u.callers[:-1]
        for status in STATES:
            u.projects[self.seed_project(status)] = status
        project_ids = list(u.projects)
        for project in project_ids:
            for caller in real:
                roll = rng.random()
                if roll < 0.45:
                    role = rng.choice(list(ProjectRole))
                    self.seed_member(project, caller.user_id, role=role)
                    u.members[(project, caller.user_id)] = MemberStatus.ACTIVE
                elif roll < 0.6:
                    self.seed_member(
                        project, caller.user_id, status=MemberStatus.INVITED
                    )
                    u.members[(project, caller.user_id)] = MemberStatus.INVITED
        repos = []
        for project in project_ids:
            for _ in range(2):
                repo = uuid4()
                choice = rng.choice(["inherit", "read", "none", "write"])
                if choice == "inherit":
                    acl = RepoAcl.inherit(repo, project)
                elif choice == "read":
                    acl = RepoAcl.override(repo, project, {RepoPermission.READ})
                elif choice == "write":
                    acl = RepoAcl.override(repo, project, {RepoPermission.WRITE})
                else:
                    acl = RepoAcl.override(repo, project, set())
                u.acls.append(acl)
                repos.append(repo)
        groups = [uuid4(), uuid4()]
        for caller in real:
            u.groups[caller.user_id] = {g for g in groups if rng.random() < 0.4}
        u.queries = [" ".join(rng.sample(VOCABULARY, 3)) for _ in range(2)]
        real_ids = [c.user_id for c in real]
        for index in range(self.MEMORIES):
            scope = rng.choice(
                [
                    "user",
                    "user",
                    "project",
                    "project",
                    "repo",
                    "shared",
                    "project_group",
                ]
            )
            words = rng.sample(VOCABULARY, rng.randint(3, 6))
            text = " ".join(words)
            if rng.random() < 0.15:  # a decoy: verbatim one of the queries
                text = rng.choice(u.queries)
            marker = f"mk{seed}x{index}z"
            status = rng.choices(
                ["active", "superseded", "deprecated", "history"], [8, 1, 1, 1]
            )[0]
            freshness = rng.choices(
                ["permanent", "revalidate", "expiring", "session_only", "repo_commit"],
                [6, 2, 2, 1, 1],
            )[0]
            extra = {}
            expires_at = None
            verified_days_ago = None
            if freshness == "revalidate":
                verified_days_ago = rng.choice([10, 200])
                extra = {
                    "verified_at": T0 - timedelta(days=verified_days_ago),
                    "revalidate_after": timedelta(days=90),
                }
            elif freshness == "expiring":
                expires_at = T0 + timedelta(days=rng.choice([-2, 5]))
                extra = {"expires_at": expires_at}
            elif freshness == "repo_commit":
                extra = {"commit_sha": "c" * 40}
            memory = Memory(
                version_id=uuid4(),
                marker=marker,
                scope=scope,
                status=status,
                freshness=freshness,
                expires_at=expires_at,
                text=text,
                verified_days_ago=verified_days_ago,
                commit_sha=extra.get("commit_sha"),
            )
            if rng.random() < 0.12:
                memory.marked_stale = True
                extra["stale_since"] = T0 - timedelta(days=1)
            if scope == "shared" and rng.random() < 0.5:
                memory.subjects = tuple(rng.sample(SUBJECT_CHOICES, rng.randint(1, 2)))
                extra["subjects"] = list(memory.subjects)
            columns = {}
            if scope == "user":
                memory.owner = rng.choice(real_ids)
                columns["owner"] = memory.owner
            elif scope == "project":
                memory.project = rng.choice(project_ids)
                columns["project"] = memory.project
            elif scope == "repo":
                memory.repo = rng.choice(repos)
                columns["repo"] = memory.repo
            elif scope == "project_group":
                memory.group = rng.choice(groups)
                columns["group"] = memory.group
            seeded = self.seed(
                f"{marker} title",
                text,
                scope=scope,
                status=status,
                freshness=freshness,
                confirmation=rng.choice(["confirmed", "inferred", "observed"]),
                importance=rng.randint(0, 100),
                pinned=rng.random() < 0.1,
                **columns,
                **extra,
            )
            memory.version_id = seeded.version_id
            u.memories.append(memory)
        # Relations: conflicts (many), supersedes and extends (a few), anywhere. A
        # version takes part in at most one supersedes relation, so that "switch off
        # what is not eligible" cannot revive a version through a chain.
        versions = [m.version_id for m in u.memories]
        in_a_supersedes: set[UUID] = set()
        for _ in range(40):
            a, b = rng.sample(versions, 2)
            kind = rng.choices(["conflicts_with", "supersedes", "extends"], [6, 3, 1])[
                0
            ]
            if kind == "supersedes" and {a, b} & in_a_supersedes:
                continue
            try:
                self.seed_relation(a, b, kind)
            except Exception:  # a duplicate or a second successor: skip it
                continue
            u.relations.append((a, b, kind))
            if kind == "supersedes":
                in_a_supersedes.update((a, b))
        for repo in repos:
            if rng.random() < 0.6:
                u.heads[repo] = rng.choice([CURRENT, MOVED])
        return u

    def set_status(self, version_ids, status):
        from sqlalchemy import text

        with self.engine.begin() as connection:
            connection.execute(
                text("UPDATE memory_versions SET status = :s WHERE id = ANY(:ids)"),
                {"s": status, "ids": list(version_ids)},
            )

    def retriever_for(self, u, caller, reranker, *, strict):
        options = {}
        if strict:
            # Short candidate lists and a System Policy: ineligible rows above the
            # limits would crowd the eligible ones out if they were filtered late.
            options = {
                "policies": StaticPolicySource(
                    (SystemPolicyItem("p", POLICY_SUBJECT, "rule"),)
                ),
                "keyword_candidates": 4,
                "vector_candidates": 4,
                "rerank_candidates": 4,
            }
        return self.new_retriever(
            reranker=reranker,
            repo_acls=StaticRepoAcls(repo_acls_of(u)),
            project_groups=StaticGroups(u.groups.get(caller.user_id, set())),
            **options,
        )

    def query_for(self, u, text, *, strict):
        if strict:
            return RetrievalQuery(
                text, limit=50, stale_policy="exclude", repo_heads=dict(u.heads)
            )
        return RetrievalQuery(text, limit=50)

    async def check_universe(self, seed, *, strict):
        """Returns (hits seen, hidden matches seen, ineligible matches seen)."""
        hits = hidden_matches = ineligible_matches = 0
        self.clean_tables()
        u = self.build(seed)
        markers = {m.version_id: m.marker for m in u.memories}
        everyone_hidden = lambda readable: {  # noqa: E731
            m.version_id for m in u.memories if m.version_id not in readable
        }
        for caller in u.callers:
            readable = u.oracle(caller)
            hidden_active = {
                m.version_id
                for m in u.memories
                if m.status == "active" and m.version_id not in readable
            }
            skipped = ineligible(u, readable) if strict else set()
            revivable = successors_of_readable(u, readable)
            # What is switched off for the reference run: hidden rows always; in the
            # strict variant also the ineligible ones (but a readable successor stays,
            # or the version it supersedes would come back).
            switched_off = hidden_active | (skipped - revivable)
            for query in u.queries:
                words = set(query.split())
                hidden_matches += sum(
                    1
                    for m in u.memories
                    if m.version_id in hidden_active and words & set(m.text.split())
                )
                ineligible_matches += sum(
                    1
                    for m in u.memories
                    if m.version_id in skipped and words & set(m.text.split())
                )
                reranker = RecordingReranker()
                retriever = self.retriever_for(u, caller, reranker, strict=strict)
                ask = self.query_for(u, query, strict=strict)
                full = await retriever.retrieve(caller, ask)
                # 1. Oracle: only readable, and (strict) only eligible memories.
                self.assert_only(full, readable - skipped)
                # 3. The Reranker never sees the text of a memory that is hidden.
                for text in reranker.seen_texts:
                    for version_id in everyone_hidden(readable):
                        self.assertNotIn(markers[version_id], text)
                # 2. Non-interference: the same answer, in every field, when the
                # hidden (and, strict, the ineligible) rows do not exist.
                self.set_status(switched_off, "history")
                try:
                    plain = await retriever.retrieve(caller, ask)
                finally:
                    self.set_status(switched_off, "active")
                self.assertEqual(full, plain, f"seed {seed}, strict={strict}")
                hits += len(full.hits)
        return hits, hidden_matches, ineligible_matches

    async def test_no_caller_reads_or_is_influenced_by_a_memory_they_may_not_read(self):
        total_hits = total_hidden_matches = 0
        for seed in self.SEEDS:
            with self.subTest(seed=seed):
                hits, hidden, _ = await self.check_universe(seed, strict=False)
                total_hits += hits
                total_hidden_matches += hidden
        self.assertGreater(total_hits, 100)
        self.assertGreater(total_hidden_matches, 100)

    async def test_ineligible_rows_never_spend_a_candidate_place(
        self,
    ):
        # Strict variant: stale ones excluded, a System Policy, candidate lists of 4.
        # Besides never returning a hidden or ineligible memory, the answer must be
        # exactly the answer of a database in which those rows do not exist: an
        # ineligible row never spends a candidate place.
        total_hits = total_hidden = total_ineligible = 0
        for seed in self.SEEDS:
            with self.subTest(seed=seed):
                hits, hidden, skipped = await self.check_universe(seed, strict=True)
                total_hits += hits
                total_hidden += hidden
                total_ineligible += skipped
        self.assertGreater(total_hits, 60)
        self.assertGreater(total_hidden, 100)
        self.assertGreater(total_ineligible, 100)


if __name__ == "__main__":
    unittest.main()
