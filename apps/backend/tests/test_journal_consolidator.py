"""``Consolidator``: worker output becomes candidate memory versions, by the rules.

Real PostgreSQL, a scripted worker (no model, no GPU). What is checked is what ends
up in the Memory schema tables: scope and owner, confirmation state, versions and
relations, provenance, and what is NOT written (held, refused, stale).
"""

import unittest
from uuid import uuid4

from paw_backend.memory.acl import Principal, readable_memory_versions
from paw_backend.memory.journal import (
    ItemResult,
    Priority,
    RunOutcome,
)

from .journal_support import (
    OTHER_USER_ID,
    USER_ID,
    AsyncPostgresJournalTestCase,
    ScriptedWorker,
    memory,
    requires_postgres,
    worker_output,
)


class ConsolidatorTestCase(AsyncPostgresJournalTestCase):
    async def consolidate(self, worker, *, count: int = 1, **options):
        """Run ``count`` jobs with ``worker``; return the results."""
        consolidator = self.new_consolidator(worker, **options)
        return [await consolidator.run_once() for _ in range(count)]

    def outcome_of(self, entry_id):
        return self.entry_row(entry_id)["outcome"]

    def results_of(self, entry_id):
        return [item["result"] for item in self.outcome_of(entry_id)["items"]]


@requires_postgres
class CandidateVersionTest(ConsolidatorTestCase):
    async def test_a_memory_becomes_a_private_candidate_version_with_its_source(self):
        conversation = self.seed_conversation()
        receipt = await self.record("I prefer tabs.", conversation=conversation)
        worker = ScriptedWorker(
            worker_output(memory("indent_style", content="Use tabs."))
        )

        (result,) = await self.consolidate(worker)

        self.assertEqual(result.outcome, RunOutcome.COMPLETED)
        self.assertEqual(result.items, (ItemResult.CREATED,))
        self.assertEqual(worker.inputs, ["I prefer tabs."])
        (version,) = self.versions()
        self.assertEqual(
            (
                version["version_number"],
                version["scope"],
                version["owner_user_id"],
                version["project_id"],
                version["repo_id"],
                version["project_group_id"],
                version["memory_type"],
                version["title"],
                version["content"],
                version["status"],
                version["confirmation_state"],
                version["freshness_policy"],
                version["actor_type"],
                version["actor_user_id"],
            ),
            (
                1,
                "user",
                USER_ID,
                None,
                None,
                None,
                "worker_candidate",
                "indent_style",
                "Use tabs.",
                "active",
                "inferred",
                "permanent",
                "system",
                None,
            ),
        )
        journal = version["attributes"]["journal"]
        self.assertEqual(
            (
                journal["entry_id"],
                journal["conversation_id"],
                journal["event_sequence"],
                journal["base_memory_version"],
            ),
            (str(receipt.entry_id), str(conversation), 0, 0),
        )
        (source,) = self.rows(
            "SELECT * FROM memory_sources WHERE memory_version_id = :v",
            v=version["id"],
        )
        self.assertEqual(
            (source["source_type"], source["conversation_id"], source["message_id"]),
            ("conversation", conversation, receipt.message_id),
        )
        entry = self.entry_row(receipt.entry_id)
        self.assertEqual(entry["state"], "consolidated")
        self.assertIsNotNone(entry["consolidated_at"])
        self.assertEqual(
            self.job_row(self.jobs_of(receipt.entry_id)[0]["id"])["status"],
            "completed",
        )

    async def test_a_confirmed_claim_is_stored_as_observed_never_as_confirmed(self):
        await self.record("Always use uv, I decided.")
        worker = ScriptedWorker(worker_output(memory("tooling", state="confirmed")))

        await self.consolidate(worker)

        (version,) = self.versions()
        self.assertEqual(version["confirmation_state"], "observed")
        self.assertEqual(version["attributes"]["worker_state"], "confirmed")
        self.assertEqual(
            self.scalar(
                "SELECT count(*) FROM memory_versions"
                " WHERE confirmation_state = 'confirmed'"
            ),
            0,
        )

    async def test_an_inferred_claim_stays_inferred(self):
        await self.record("I usually use pytest.")
        await self.consolidate(
            ScriptedWorker(worker_output(memory("test_runner", state="inferred")))
        )
        self.assertEqual(self.versions()[0]["confirmation_state"], "inferred")

    async def test_project_and_repo_scopes_are_only_a_recommendation(self):
        project, repo = uuid4(), uuid4()
        conversation = self.seed_conversation(project_id=project, repo_id=repo)
        await self.record("This repo uses ruff.", conversation=conversation)
        await self.record("This project ships weekly.", conversation=conversation)
        worker = ScriptedWorker(
            worker_output(memory("lint", scope="repo")),
            worker_output(memory("cadence", scope="project")),
        )

        await self.consolidate(worker, count=2)

        versions = {v["key"]: v for v in self.versions()}
        for key, recommended in (("lint", "repo"), ("cadence", "project")):
            with self.subTest(key):
                version = versions[key]
                self.assertEqual(
                    (
                        version["scope"],
                        version["owner_user_id"],
                        version["project_id"],
                        version["repo_id"],
                    ),
                    ("user", USER_ID, None, None),
                )
                self.assertEqual(
                    version["attributes"]["recommended_scope"], recommended
                )

    async def test_shared_scope_is_refused_and_nothing_is_written(self):
        receipt = await self.record("Everyone should use tabs.")
        (result,) = await self.consolidate(
            ScriptedWorker(worker_output(memory("house_style", scope="shared")))
        )
        self.assertEqual(result.items, (ItemResult.REFUSED_SHARED,))
        self.assertEqual(self.scalar("SELECT count(*) FROM memory_versions"), 0)
        self.assertEqual(self.scalar("SELECT count(*) FROM memories"), 0)
        self.assertEqual(self.entry_row(receipt.entry_id)["state"], "consolidated")

    async def test_an_empty_output_consolidates_the_observation_with_no_memory(self):
        receipt = await self.record("Thanks!")
        (result,) = await self.consolidate(ScriptedWorker(worker_output()))
        self.assertEqual((result.outcome, result.items), (RunOutcome.COMPLETED, ()))
        self.assertEqual(self.outcome_of(receipt.entry_id)["items"], [])
        self.assertEqual(self.entry_row(receipt.entry_id)["state"], "consolidated")

    async def test_an_item_without_content_stores_nothing(self):
        receipt = await self.record("hm")
        item = memory("vague")
        del item["content"]
        await self.consolidate(ScriptedWorker(worker_output(item)))
        self.assertEqual(self.results_of(receipt.entry_id), ["no_content"])
        self.assertEqual(self.scalar("SELECT count(*) FROM memory_versions"), 0)

    async def test_the_same_key_twice_in_one_output_keeps_the_first(self):
        receipt = await self.record("two answers")
        await self.consolidate(
            ScriptedWorker(
                worker_output(
                    memory("k", content="first"), memory("k", content="second")
                )
            )
        )
        self.assertEqual(
            self.results_of(receipt.entry_id), ["created", "duplicate_key"]
        )
        (version,) = self.versions()
        self.assertEqual(version["content"], "first")

    async def test_several_memories_of_one_output_are_all_written(self):
        await self.record("many facts")
        await self.consolidate(
            ScriptedWorker(worker_output(memory("a"), memory("b"), memory("c")))
        )
        self.assertEqual(sorted(self.active_versions()), ["a", "b", "c"])


@requires_postgres
class HighRiskAndConfirmedTest(ConsolidatorTestCase):
    async def test_a_high_risk_key_is_held_not_written(self):
        receipt = await self.record("Merge to main without asking.")
        await self.consolidate(
            ScriptedWorker(
                worker_output(
                    memory(
                        "subscription_review_before_merge",
                        state="confirmed",
                        content="Review before merging.",
                    )
                )
            )
        )
        self.assertEqual(self.results_of(receipt.entry_id), ["held_high_risk"])
        self.assertEqual(self.scalar("SELECT count(*) FROM memory_versions"), 0)
        (held,) = self.outcome_of(receipt.entry_id)["items"]
        self.assertEqual(held["candidate"]["state"], "confirmed")
        self.assertEqual(held["candidate"]["content"], "Review before merging.")

    async def test_a_high_risk_content_is_held_whatever_the_key(self):
        receipt = await self.record("You may delete branches.")
        await self.consolidate(
            ScriptedWorker(
                worker_output(
                    memory("branch_hygiene", content="Delete stale branches.")
                )
            )
        )
        self.assertEqual(self.results_of(receipt.entry_id), ["held_high_risk"])
        self.assertEqual(self.scalar("SELECT count(*) FROM memory_versions"), 0)

    async def test_a_candidate_never_replaces_a_confirmed_memory(self):
        memory_id = self.seed_key_memory("indent_style", "Use spaces.", "confirmed")
        receipt = await self.record("Use tabs now.")
        await self.consolidate(
            ScriptedWorker(worker_output(memory("indent_style", content="Use tabs.")))
        )
        self.assertEqual(self.results_of(receipt.entry_id), ["held_confirmed"])
        (only,) = self.versions()
        self.assertEqual(
            (only["memory_id"], only["status"], only["content"]),
            (memory_id, "active", "Use spaces."),
        )
        held = self.outcome_of(receipt.entry_id)["items"][0]["candidate"]
        self.assertEqual(held["content"], "Use tabs.")

    async def test_the_same_content_as_the_confirmed_memory_is_a_duplicate(self):
        self.seed_key_memory("indent_style", "Use tabs.", "confirmed")
        receipt = await self.record("Use tabs.")
        await self.consolidate(
            ScriptedWorker(worker_output(memory("indent_style", content="Use tabs.")))
        )
        self.assertEqual(self.results_of(receipt.entry_id), ["duplicate"])
        self.assertEqual(len(self.versions()), 1)

    async def test_a_rejected_or_deleted_memory_is_not_brought_back(self):
        for status, confirmation in (
            ("deprecated", "confirmed"),
            ("history", "rejected"),
        ):
            with self.subTest(status=status):
                self.clean_tables()
                self.seed_key_memory("k", "old", confirmation, status=status)
                receipt = await self.record("again")
                await self.consolidate(
                    ScriptedWorker(worker_output(memory("k", content="new")))
                )
                self.assertEqual(self.results_of(receipt.entry_id), ["blocked_by_user"])
                (only,) = self.versions()
                self.assertEqual(only["content"], "old")


@requires_postgres
class VersioningTest(ConsolidatorTestCase):
    async def test_a_newer_statement_supersedes_the_previous_version(self):
        conversation = self.seed_conversation()
        await self.record("Use tabs.", conversation=conversation)
        await self.record("Actually spaces.", conversation=conversation)
        worker = ScriptedWorker(
            worker_output(memory("indent_style", content="Use tabs.")),
            worker_output(memory("indent_style", content="Use spaces.")),
        )

        first, second = await self.consolidate(worker, count=2)

        self.assertEqual(first.items, (ItemResult.CREATED,))
        self.assertEqual(second.items, (ItemResult.UPDATED,))
        versions = self.versions()
        self.assertEqual(
            [(v["version_number"], v["status"], v["content"]) for v in versions],
            [(1, "superseded", "Use tabs."), (2, "active", "Use spaces.")],
        )
        self.assertEqual(versions[0]["memory_id"], versions[1]["memory_id"])
        (relation,) = self.rows("SELECT * FROM memory_relations")
        self.assertEqual(
            (
                relation["from_version_id"],
                relation["to_version_id"],
                relation["relation_type"],
            ),
            (versions[1]["id"], versions[0]["id"], "supersedes"),
        )
        self.assertEqual(versions[1]["attributes"]["journal"]["base_memory_version"], 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM memories"), 1)

    async def test_the_same_content_again_writes_no_new_version(self):
        conversation = self.seed_conversation()
        await self.record("Use tabs.", conversation=conversation)
        await self.record("I said tabs.", conversation=conversation)
        worker = ScriptedWorker(
            worker_output(memory("indent_style", content="Use tabs.")),
            worker_output(memory("indent_style", content="  Use tabs.  ")),
        )
        first, second = await self.consolidate(worker, count=2)
        self.assertEqual(second.items, (ItemResult.DUPLICATE,))
        self.assertEqual(len(self.versions()), 1)
        # The guard moved forward to the newer entry.
        self.assertEqual(
            self.scalar("SELECT applied_event_sequence FROM memory_consolidation_keys"),
            1,
        )

    async def test_an_older_turn_that_finishes_later_does_not_overwrite_a_newer_one(
        self,
    ):
        conversation = self.seed_conversation()
        older = await self.record("Use tabs.", conversation=conversation)
        # The newer turn is HIGH, so a worker picks it up FIRST.
        newer = await self.record(
            "Actually spaces.", conversation=conversation, priority=Priority.HIGH
        )
        worker = ScriptedWorker(
            worker_output(memory("indent_style", content="Use spaces.")),
            worker_output(memory("indent_style", content="Use tabs.")),
        )

        first, second = await self.consolidate(worker, count=2)

        self.assertEqual(worker.inputs, ["Actually spaces.", "Use tabs."])
        self.assertEqual(first.items, (ItemResult.CREATED,))
        self.assertEqual(second.items, (ItemResult.STALE,))
        (only,) = self.versions()
        self.assertEqual((only["content"], only["status"]), ("Use spaces.", "active"))
        self.assertEqual(self.results_of(older.entry_id), ["stale"])
        self.assertEqual(self.results_of(newer.entry_id), ["created"])
        # Both observations are consolidated: the old one is not lost, only not applied.
        self.assertEqual(self.entry_row(older.entry_id)["state"], "consolidated")

    async def test_the_order_holds_across_conversations_by_the_recorded_time(self):
        first_conversation = self.seed_conversation()
        second_conversation = self.seed_conversation()
        older = await self.record("Use tabs.", conversation=first_conversation)
        newer = await self.record(
            "Actually spaces.", conversation=second_conversation, priority=Priority.HIGH
        )
        worker = ScriptedWorker(
            worker_output(memory("indent_style", content="Use spaces.")),
            worker_output(memory("indent_style", content="Use tabs.")),
        )
        first, second = await self.consolidate(worker, count=2)
        self.assertEqual(
            (first.items, second.items), ((ItemResult.CREATED,), (ItemResult.STALE,))
        )
        self.assertEqual(self.versions()[0]["content"], "Use spaces.")
        self.assertIsNotNone(older, newer)

    async def test_a_memory_it_supersedes_is_retired_and_related(self):
        conversation = self.seed_conversation()
        await self.record("old", conversation=conversation)
        await self.record("new", conversation=conversation)
        worker = ScriptedWorker(
            worker_output(memory("editor", content="Uses vim.")),
            worker_output(memory("ide", content="Uses VS Code.", supersedes="editor")),
        )
        await self.consolidate(worker, count=2)
        active = self.active_versions()
        self.assertEqual(sorted(active), ["ide"])
        editor = next(v for v in self.versions() if v["key"] == "editor")
        self.assertEqual(editor["status"], "superseded")
        (relation,) = self.rows("SELECT * FROM memory_relations")
        self.assertEqual(
            (
                relation["from_version_id"],
                relation["to_version_id"],
                relation["relation_type"],
            ),
            (active["ide"]["id"], editor["id"], "supersedes"),
        )

    async def test_a_supersession_guards_the_retired_key_against_an_older_turn(self):
        """The retired key's ordering guard moves to the superseding turn.

        ``editor`` is retired by the turn (sequence 2) that says ``ide`` replaces
        it. An observation about ``editor`` from an OLDER turn (sequence 1) that
        finishes afterwards must not bring ``editor`` back beside ``ide``.
        """
        conversation = self.seed_conversation()
        await self.record(
            "editor first", conversation=conversation, priority=Priority.HIGH
        )
        await self.record("editor again", conversation=conversation)  # sequence 1
        await self.record(
            "switch to ide", conversation=conversation, priority=Priority.HIGH
        )  # sequence 2: newer than "editor again", processed before it
        worker = ScriptedWorker(
            worker_output(memory("editor", content="Uses vim.")),
            worker_output(memory("ide", content="Uses VS Code.", supersedes="editor")),
            worker_output(memory("editor", content="Uses vim with plugins.")),
        )

        first, second, third = await self.consolidate(worker, count=3)

        self.assertEqual(
            worker.inputs, ["editor first", "switch to ide", "editor again"]
        )
        self.assertEqual(
            [first.items, second.items, third.items],
            [(ItemResult.CREATED,), (ItemResult.CREATED,), (ItemResult.STALE,)],
        )
        self.assertEqual(sorted(self.active_versions()), ["ide"])
        editor = [v for v in self.versions() if v["key"] == "editor"]
        self.assertEqual(
            [(v["version_number"], v["status"]) for v in editor], [(1, "superseded")]
        )
        # The guard of the retired key is the superseding turn, not the old one.
        self.assertEqual(
            self.scalar(
                "SELECT applied_event_sequence FROM memory_consolidation_keys"
                " WHERE key = 'editor'"
            ),
            2,
        )

    async def test_a_turn_newer_than_the_supersession_may_bring_the_key_back(self):
        conversation = self.seed_conversation()
        await self.record(
            "editor first", conversation=conversation, priority=Priority.HIGH
        )
        await self.record(
            "switch to ide", conversation=conversation, priority=Priority.HIGH
        )
        await self.record("back to vim", conversation=conversation)  # sequence 2
        worker = ScriptedWorker(
            worker_output(memory("editor", content="Uses vim.")),
            worker_output(memory("ide", content="Uses VS Code.", supersedes="editor")),
            worker_output(memory("editor", content="Uses vim again.")),
        )

        await self.consolidate(worker, count=3)

        active = self.active_versions()
        self.assertEqual(sorted(active), ["editor", "ide"])
        self.assertEqual(
            (active["editor"]["version_number"], active["editor"]["content"]),
            (2, "Uses vim again."),
        )

    async def test_a_supersession_by_an_older_turn_does_not_retire_a_newer_memory(self):
        conversation = self.seed_conversation()
        await self.record("editor", conversation=conversation, priority=Priority.HIGH)
        await self.record("switch to ide", conversation=conversation)  # sequence 1
        await self.record(
            "editor updated", conversation=conversation, priority=Priority.HIGH
        )  # sequence 2, processed before the older turn
        worker = ScriptedWorker(
            worker_output(memory("editor", content="Uses vim.")),
            worker_output(memory("editor", content="Uses neovim.")),
            worker_output(memory("ide", content="Uses VS Code.", supersedes="editor")),
        )

        first, second, third = await self.consolidate(worker, count=3)

        self.assertEqual(worker.inputs, ["editor", "editor updated", "switch to ide"])
        self.assertEqual(third.items, (ItemResult.CREATED,))
        active = self.active_versions()
        # ``ide`` is written (it is a statement of its own) but the newer editor stays.
        self.assertEqual(sorted(active), ["editor", "ide"])
        self.assertEqual(active["editor"]["content"], "Uses neovim.")
        self.assertEqual(self.scalar("SELECT count(*) FROM memory_relations"), 1)
        self.assertEqual(
            self.scalar("SELECT relation_type FROM memory_relations"),
            "supersedes",  # only editor v1 -> v2
        )
        self.assertIsNotNone((first, second))

    async def test_a_memory_written_in_the_same_output_can_be_retired_by_it(self):
        receipt = await self.record("one output")
        worker = ScriptedWorker(worker_output(memory("x"), memory("y", supersedes="x")))
        await self.consolidate(worker)
        self.assertEqual(self.results_of(receipt.entry_id), ["created", "created"])
        self.assertEqual(sorted(self.active_versions()), ["y"])
        self.assertEqual(self.scalar("SELECT count(*) FROM memory_relations"), 1)

    async def test_a_key_restated_and_retired_in_one_output_ends_retired_either_way(
        self,
    ):
        # ``editor`` exists; one output restates it AND retires it through ``ide``.
        # Listed either way round, the restatement is applied first (it is what
        # ``ide`` refers to) and then retired: ``ide`` is the only active memory.
        finals = []
        for name, order in (("retire first", (0, 1)), ("restate first", (1, 0))):
            with self.subTest(name):
                self.clean_tables()
                conversation = self.seed_conversation()
                await self.record("editor", conversation=conversation)
                receipt = await self.record("both at once", conversation=conversation)
                both = [
                    memory("ide", content="Uses VS Code.", supersedes="editor"),
                    memory("editor", content="Uses vim again."),
                ]
                worker = ScriptedWorker(
                    worker_output(memory("editor", content="Uses vim.")),
                    worker_output(*(both[i] for i in order)),
                )
                await self.consolidate(worker, count=2)
                results = {
                    i["key"]: i["result"]
                    for i in self.outcome_of(receipt.entry_id)["items"]
                }
                self.assertEqual(results, {"ide": "created", "editor": "updated"})
                self.assertEqual(sorted(self.active_versions()), ["ide"])
                finals.append(
                    sorted(
                        (v["key"], v["version_number"], v["status"])
                        for v in self.versions()
                    )
                )
        self.assertEqual(finals[0], finals[1])

    async def test_a_cycle_of_supersessions_retires_one_and_leaves_the_other_stale(
        self,
    ):
        # ``a`` retires ``b`` and ``b`` retires ``a`` in one output: a cycle, applied
        # in the listed order. The retired key's guard moved to this turn is seen by
        # the item that comes next, so ``b`` cannot then undo the retirement.
        conversation = self.seed_conversation()
        await self.record("a", conversation=conversation)
        await self.record("b", conversation=conversation)
        receipt = await self.record("cycle", conversation=conversation)
        worker = ScriptedWorker(
            worker_output(memory("a", content="A.")),
            worker_output(memory("b", content="B.")),
            worker_output(
                memory("a", content="A2.", supersedes="b"),
                memory("b", content="B2.", supersedes="a"),
            ),
        )
        await self.consolidate(worker, count=3)
        results = {
            i["key"]: i["result"] for i in self.outcome_of(receipt.entry_id)["items"]
        }
        self.assertEqual(results, {"a": "updated", "b": "stale"})
        self.assertEqual(sorted(self.active_versions()), ["a"])

    async def test_a_candidate_cannot_supersede_a_confirmed_memory_of_another_key(self):
        self.seed_key_memory("editor", "Uses vim.", "confirmed")
        receipt = await self.record("switch")
        await self.consolidate(
            ScriptedWorker(
                worker_output(
                    memory("ide", content="Uses VS Code.", supersedes="editor")
                )
            )
        )
        self.assertEqual(self.results_of(receipt.entry_id), ["held_confirmed"])
        self.assertEqual([v["key"] for v in self.versions()], ["editor"])

    async def test_a_conflict_is_a_relation_and_changes_neither_memory(self):
        conversation = self.seed_conversation()
        await self.record("a", conversation=conversation)
        await self.record("b", conversation=conversation)
        worker = ScriptedWorker(
            worker_output(memory("commit_style", content="Squash always.")),
            worker_output(
                memory(
                    "history_style",
                    content="Keep history.",
                    conflicts_with=["commit_style", "unknown_key"],
                )
            ),
        )
        await self.consolidate(worker, count=2)
        active = self.active_versions()
        self.assertEqual(sorted(active), ["commit_style", "history_style"])
        (relation,) = self.rows("SELECT * FROM memory_relations")
        self.assertEqual(
            (
                relation["from_version_id"],
                relation["to_version_id"],
                relation["relation_type"],
            ),
            (
                active["history_style"]["id"],
                active["commit_style"]["id"],
                "conflicts_with",
            ),
        )

    async def test_a_supersedes_naming_an_unknown_key_is_ignored(self):
        receipt = await self.record("x")
        await self.consolidate(
            ScriptedWorker(worker_output(memory("k", supersedes="never_stored")))
        )
        self.assertEqual(self.results_of(receipt.entry_id), ["created"])
        self.assertEqual(self.scalar("SELECT count(*) FROM memory_relations"), 0)


@requires_postgres
class WidenedMemoryTest(ConsolidatorTestCase):
    """After the user widens a memory (a new, wider version of the SAME memory) the
    consolidator still finds it by its key, and never changes it."""

    def widened(self, key="indent_style", content="Use spaces.", **kwargs):
        memory_id = self.seed_key_memory(key, content, "inferred")
        self.widen_key_memory(memory_id, **kwargs)
        return memory_id

    async def test_a_different_candidate_is_held_not_a_failed_write(self):
        memory_id = self.widened()
        receipt = await self.record("Use tabs now.")

        (result,) = await self.consolidate(
            ScriptedWorker(worker_output(memory("indent_style", content="Use tabs.")))
        )

        self.assertEqual(result.outcome, RunOutcome.COMPLETED)
        self.assertEqual(result.items, (ItemResult.HELD_WIDENED,))
        self.assertEqual(self.results_of(receipt.entry_id), ["held_widened"])
        held = self.outcome_of(receipt.entry_id)["items"][0]["candidate"]
        self.assertEqual(held["content"], "Use tabs.")
        versions = self.rows(
            "SELECT version_number, scope, status FROM memory_versions"
            " WHERE memory_id = :m ORDER BY version_number",
            m=memory_id,
        )
        self.assertEqual(
            [(v["version_number"], v["scope"], v["status"]) for v in versions],
            [(1, "user", "superseded"), (2, "project", "active")],
        )
        job = self.jobs_of(receipt.entry_id)[0]
        self.assertEqual((job["status"], job["attempts"]), ("completed", 0))

    async def test_the_same_content_is_a_duplicate(self):
        self.widened(content="Use spaces.")
        receipt = await self.record("spaces")
        await self.consolidate(
            ScriptedWorker(worker_output(memory("indent_style", content="Use spaces.")))
        )
        self.assertEqual(self.results_of(receipt.entry_id), ["duplicate"])
        self.assertEqual(self.scalar("SELECT count(*) FROM memory_versions"), 2)

    async def test_a_repo_scoped_widening_is_found_too(self):
        self.widened(scope="repo")
        receipt = await self.record("tabs")
        await self.consolidate(
            ScriptedWorker(worker_output(memory("indent_style", content="Use tabs.")))
        )
        self.assertEqual(self.results_of(receipt.entry_id), ["held_widened"])

    async def test_a_widened_memory_is_not_retired_by_another_keys_supersedes(self):
        memory_id = self.widened("editor", "Uses vim.")
        receipt = await self.record("switch")
        await self.consolidate(
            ScriptedWorker(
                worker_output(
                    memory("ide", content="Uses VS Code.", supersedes="editor")
                )
            )
        )
        self.assertEqual(self.results_of(receipt.entry_id), ["held_widened"])
        active = self.rows(
            "SELECT scope FROM memory_versions WHERE memory_id = :m"
            " AND status = 'active'",
            m=memory_id,
        )
        self.assertEqual([r["scope"] for r in active], ["project"])
        self.assertEqual(self.scalar("SELECT count(*) FROM memory_relations"), 0)

    async def test_another_owners_memory_is_still_not_read_or_written(self):
        # The widened version has no owner column; ownership comes from the memory's
        # private versions. A registry row pointing at SOMEONE ELSE's memory finds
        # nothing (the earlier corrupted-registry case still fails closed).
        foreign = self.seed_key_memory(
            "theirs", "not yours", "inferred", owner=OTHER_USER_ID, register=False
        )
        self.widen_key_memory(foreign)
        self.execute(
            "INSERT INTO memory_consolidation_keys (owner_user_id, key, memory_id,"
            " applied_conversation_id, applied_event_sequence, applied_recorded_at)"
            " VALUES (:o, 'second', :m, :c, 0, '2000-01-01T00:00:00+00:00')",
            o=USER_ID,
            m=foreign,
            c=uuid4(),
        )
        receipt = await self.record("mine")
        (result,) = await self.consolidate(
            ScriptedWorker(worker_output(memory("second", content="Mine.")))
        )
        self.assertEqual(result.outcome, RunOutcome.RETRY_SCHEDULED)
        self.assertEqual(self.entry_row(receipt.entry_id)["state"], "pending")
        self.assertEqual(
            self.scalar(
                "SELECT count(*) FROM memory_versions WHERE memory_id = :m", m=foreign
            ),
            2,
        )


@requires_postgres
class ItemOrderTest(ConsolidatorTestCase):
    """What an output does must not depend on the order its items are listed in."""

    async def test_a_memory_retired_in_the_same_output_whatever_the_order(self):
        outcomes = []
        for name, items in (
            ("listed first", ("y", "x")),
            ("listed last", ("x", "y")),
        ):
            with self.subTest(name):
                self.clean_tables()
                receipt = await self.record("one output")
                by_key = {
                    "x": memory("x", content="X."),
                    "y": memory("y", content="Y.", supersedes="x"),
                }
                await self.consolidate(
                    ScriptedWorker(worker_output(*(by_key[k] for k in items)))
                )
                results = {
                    item["key"]: item["result"]
                    for item in self.outcome_of(receipt.entry_id)["items"]
                }
                statuses = {v["key"]: v["status"] for v in self.versions()}
                relations = self.scalar("SELECT count(*) FROM memory_relations")
                self.assertEqual(results, {"x": "created", "y": "created"})
                self.assertEqual(statuses, {"x": "superseded", "y": "active"})
                self.assertEqual(relations, 1)
                outcomes.append((results, statuses, relations))
        self.assertEqual(outcomes[0], outcomes[1])

    async def test_the_outcome_lists_the_items_in_the_order_of_the_output(self):
        receipt = await self.record("one output")
        await self.consolidate(
            ScriptedWorker(
                worker_output(
                    memory("y", supersedes="x"),
                    memory("x"),
                    memory("y", content="again"),
                )
            )
        )
        items = self.outcome_of(receipt.entry_id)["items"]
        self.assertEqual(
            [(i["index"], i["key"], i["result"]) for i in items],
            [(0, "y", "created"), (1, "x", "created"), (2, "y", "duplicate_key")],
        )

    async def test_a_conflict_refers_to_a_key_listed_after_it(self):
        await self.record("one output")
        await self.consolidate(
            ScriptedWorker(
                worker_output(memory("one", conflicts_with=["two"]), memory("two"))
            )
        )
        active = self.active_versions()
        (relation,) = self.rows("SELECT * FROM memory_relations")
        self.assertEqual(
            (
                relation["from_version_id"],
                relation["to_version_id"],
                relation["relation_type"],
            ),
            (active["one"]["id"], active["two"]["id"], "conflicts_with"),
        )

    async def test_a_chain_of_supersessions_is_applied_from_its_end(self):
        await self.record("one output")
        by_key = {
            "a": memory("a", supersedes="b"),
            "b": memory("b", supersedes="c"),
            "c": memory("c"),
        }
        await self.consolidate(
            ScriptedWorker(worker_output(by_key["a"], by_key["b"], by_key["c"]))
        )
        self.assertEqual(sorted(self.active_versions()), ["a"])
        self.assertEqual(self.scalar("SELECT count(*) FROM memory_relations"), 2)


@requires_postgres
class PrivacyBetweenUsersTest(ConsolidatorTestCase):
    async def test_two_users_with_the_same_key_never_touch_each_others_memory(self):
        mine = await self.record("I like tabs.")
        theirs_conversation = self.seed_conversation(OTHER_USER_ID)
        theirs = await self.journal.record_user_message(
            self.other_user, theirs_conversation, "I like spaces."
        )
        worker = ScriptedWorker(
            worker_output(memory("indent_style", content="tabs")),
            worker_output(
                memory("indent_style", content="spaces", supersedes="indent_style")
            ),
        )
        await self.consolidate(worker, count=2)

        mine_versions = self.versions(USER_ID)
        theirs_versions = self.versions(OTHER_USER_ID)
        self.assertEqual([v["content"] for v in mine_versions], ["tabs"])
        self.assertEqual([v["content"] for v in theirs_versions], ["spaces"])
        self.assertNotEqual(
            mine_versions[0]["memory_id"], theirs_versions[0]["memory_id"]
        )
        self.assertEqual(mine_versions[0]["status"], "active")
        self.assertIsNotNone((mine, theirs))

    async def test_another_principal_reads_none_of_it_through_the_acl_condition(self):
        project = uuid4()
        conversation = self.seed_conversation(project_id=project)
        await self.record("Private thought.", conversation=conversation)
        await self.consolidate(
            ScriptedWorker(worker_output(memory("thought", scope="project")))
        )

        from sqlalchemy import select

        from paw_backend.memory.models import MemoryVersion

        def visible(principal):
            with self.engine.connect() as connection:
                return connection.execute(
                    select(MemoryVersion.id).where(readable_memory_versions(principal))
                ).all()

        self.assertEqual(len(visible(Principal(USER_ID))), 1)
        self.assertEqual(visible(Principal(OTHER_USER_ID)), [])
        # A member of the conversation's project does not see it either.
        self.assertEqual(visible(Principal(OTHER_USER_ID, project_ids={project})), [])

    async def test_the_owner_comes_from_the_conversation_never_from_the_worker(self):
        conversation = self.seed_conversation(OTHER_USER_ID)
        await self.journal.record_user_message(self.other_user, conversation, "mine")
        item = memory("k")
        item["owner_user_id"] = str(USER_ID)  # a field the contract does not have
        worker = ScriptedWorker(worker_output(item))
        (result,) = await self.consolidate(worker)
        # Unknown fields break the contract: the whole output is dropped.
        self.assertEqual(result.outcome, RunOutcome.RETRY_SCHEDULED)
        self.assertEqual(self.scalar("SELECT count(*) FROM memory_versions"), 0)


@requires_postgres
class ConversationDeletionTest(ConsolidatorTestCase):
    async def test_deleting_the_conversation_removes_the_journal_and_keeps_the_memory(
        self,
    ):
        conversation = self.seed_conversation()
        receipt = await self.record("I like tabs.", conversation=conversation)
        await self.consolidate(ScriptedWorker(worker_output(memory("indent_style"))))
        self.execute("DELETE FROM conversations WHERE id = :c", c=conversation)
        for table in (
            "messages",
            "memory_journal_entries",
            "memory_consolidation_queue",
        ):
            with self.subTest(table):
                self.assertEqual(self.scalar(f"SELECT count(*) FROM {table}"), 0)
        (version,) = self.versions()
        self.assertEqual(version["status"], "active")
        source = self.rows("SELECT * FROM memory_sources")[0]
        self.assertEqual(
            (source["conversation_id"], source["message_id"]), (None, None)
        )
        self.assertIsNotNone(receipt)


if __name__ == "__main__":
    unittest.main()
