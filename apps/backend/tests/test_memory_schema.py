"""Memory / Conversation schema: constraints, versioning, provenance (real PostgreSQL).

Skipped unless ``PAW_TEST_DATABASE_URL`` is set (see ``test_postgres_integration``).
Every constraint is asserted by name: a violation that fires on the wrong
constraint would otherwise pass for the wrong reason.
"""

from datetime import timedelta
from functools import partial
from uuid import uuid4

from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from paw_backend.memory.models import (
    Conversation,
    Memory,
    MemoryEmbedding,
    MemoryRelation,
    MemorySource,
    MemoryVersion,
    Message,
    SessionState,
)

from .memory_support import (
    MemoryDatabaseTestCase,
    foreign_keys_without_index,
    requires_postgres,
    utc,
    version_values,
)

SHA1 = ("0123456789abcdef" * 3)[:40]
SHA256 = "0123456789abcdef" * 4


@requires_postgres
class LayerSeparationTest(MemoryDatabaseTestCase):
    def test_the_three_layers_are_separate_tables_linked_only_by_provenance(self):
        rows = self.connection.execute(
            text(
                "SELECT child.relname, parent.relname, con.confdeltype"
                " FROM pg_constraint con"
                " JOIN pg_class child ON child.oid = con.conrelid"
                " JOIN pg_class parent ON parent.oid = con.confrelid"
                " WHERE con.contype = 'f'"
                "   AND child.relnamespace = 'public'::regnamespace"
            )
        ).all()
        # Only the Conversation / Memory layers are in scope: other subsystems (Task,
        # audit) add their own tables and must not break this test. Links between a
        # layer table and a foreign table, in either direction, still fail it.
        layer_tables = {
            "conversations",
            "messages",
            "session_states",
            "memories",
            "memory_versions",
            "memory_relations",
            "memory_sources",
            "memory_embeddings",
        }
        edges = {
            (child, parent): action
            for child, parent, action in rows
            if child in layer_tables or parent in layer_tables
        }

        # 'c' = ON DELETE CASCADE, 'n' = ON DELETE SET NULL.
        self.assertEqual(
            edges,
            {
                # Raw Conversation and its Session state.
                ("messages", "conversations"): "c",
                ("session_states", "conversations"): "c",
                # Long-term Memory.
                ("memory_versions", "memories"): "c",
                ("memory_relations", "memory_versions"): "c",
                ("memory_sources", "memory_versions"): "c",
                ("memory_embeddings", "memory_versions"): "c",
                # A model's dimension cannot change or the model disappear
                # while embeddings use it (NO ACTION).
                ("memory_embeddings", "embedding_models"): "a",
                # The only bridge from Long-term Memory to Raw Conversation:
                # provenance, which must survive the conversation's deletion.
                ("memory_sources", "conversations"): "n",
                ("memory_sources", "messages"): "n",
            },
        )
        # Two edges appear once per column of memory_relations: they collapse in
        # the set above, so check the column count separately.
        relation_fks = self.connection.execute(
            text(
                "SELECT count(*) FROM pg_constraint"
                " WHERE contype = 'f' AND conrelid = 'memory_relations'::regclass"
            )
        ).scalar_one()
        self.assertEqual(relation_fks, 2)

    def test_user_project_and_repo_ids_have_no_foreign_keys(self):
        # Those tables do not exist yet (PAW-021 / 026 / 027).
        foreign_key_columns = self.connection.execute(
            text(
                "SELECT DISTINCT a.attname FROM pg_constraint con"
                " JOIN pg_attribute a ON a.attrelid = con.conrelid"
                "   AND a.attnum = ANY (con.conkey)"
                " WHERE con.contype = 'f'"
                "   AND con.conrelid::regclass::text = ANY (:tables)"
            ),
            {"tables": ["conversations", "memory_versions", "memory_sources"]},
        ).scalars()
        self.assertFalse(
            {"owner_user_id", "project_id", "repo_id", "actor_user_id"}
            & set(foreign_key_columns)
        )


@requires_postgres
class RawConversationTest(MemoryDatabaseTestCase):
    def test_messages_keep_their_logical_order_not_their_insert_order(self):
        conversation = self.add_conversation()
        turn = uuid4()
        for sequence, content in ((2, "third"), (0, "first"), (1, "second")):
            self.add_message(conversation, sequence, turn_id=turn, content=content)

        ordered = self.session.execute(
            select(Message.content)
            .where(Message.conversation_id == conversation)
            .order_by(Message.event_sequence)
        ).scalars()

        self.assertEqual(list(ordered), ["first", "second", "third"])

    def test_event_sequence_is_unique_per_conversation_only(self):
        first, second = self.add_conversation(), self.add_conversation()
        self.add_message(first, 0)

        duplicate = self.violation(lambda: self.add_message(first, 0))
        self.add_message(second, 0)  # the same number in another conversation is fine

        self.assertEqual(duplicate, "uq_messages_conversation_id")

    def test_every_kind_of_raw_event_can_be_stored(self):
        conversation = self.add_conversation()
        roles = ["user", "assistant", "tool", "agent", "task"]
        for sequence, role in enumerate(roles):
            self.add_message(conversation, sequence, role=role, attributes={"n": 1})

        stored = self.session.execute(
            select(Message.role).order_by(Message.event_sequence)
        ).scalars()
        self.assertEqual(list(stored), roles)

    def test_invalid_message_rows_are_rejected(self):
        conversation = self.add_conversation()
        cases = {
            "ck_messages_role_valid": lambda: self.add_message(
                conversation, 0, role="system"
            ),
            "ck_messages_event_sequence_not_negative": lambda: self.add_message(
                conversation, -1
            ),
            "ck_messages_attributes_object": lambda: self.add_message(
                conversation, 1, attributes=["not", "an", "object"]
            ),
            "fk_messages_conversation_id_conversations": lambda: self.add_message(
                uuid4(), 2
            ),
        }
        for expected, action in cases.items():
            with self.subTest(expected):
                self.assertEqual(self.violation(action), expected)

    def test_conversation_title_is_bounded(self):
        self.assertIsNone(
            self.violation(lambda: self.add_conversation(title="x" * 200))
        )
        self.assertEqual(
            self.violation(lambda: self.add_conversation(title="x" * 201)),
            "ck_conversations_title_length",
        )

    def test_conversation_belongs_to_its_owner_and_may_have_project_context(self):
        owner, project, repo = uuid4(), uuid4(), uuid4()
        conversation = self.add_conversation(
            owner_user_id=owner, project_id=project, repo_id=repo
        )

        row = self.session.execute(
            select(Conversation.owner_user_id, Conversation.project_id).where(
                Conversation.id == conversation
            )
        ).one()
        self.assertEqual(tuple(row), (owner, project))


@requires_postgres
class SessionStateTest(MemoryDatabaseTestCase):
    def test_session_state_is_one_row_per_conversation(self):
        conversation = self.add_conversation()
        add = lambda: self.session.execute(  # noqa: E731
            insert(SessionState).values(conversation_id=conversation, summary="s")
        )
        add()

        self.assertEqual(self.violation(add), "pk_session_states")

    def test_session_state_needs_an_existing_conversation(self):
        missing = lambda: self.session.execute(  # noqa: E731
            insert(SessionState).values(conversation_id=uuid4())
        )
        self.assertEqual(
            self.violation(missing), "fk_session_states_conversation_id_conversations"
        )

    def test_defaults_and_bounds(self):
        conversation = self.add_conversation()
        self.session.execute(insert(SessionState).values(conversation_id=conversation))

        row = self.session.execute(
            select(
                SessionState.summary,
                SessionState.state,
                SessionState.summarized_through_sequence,
            )
        ).one()
        self.assertEqual(tuple(row), (None, {}, None))
        for expected, values in {
            "ck_session_states_state_object": {"state": [1]},
            "ck_session_states_summarized_through_not_negative": {
                "summarized_through_sequence": -1
            },
        }.items():
            with self.subTest(expected):
                self.assertEqual(
                    self.violation(
                        lambda values=values: self.session.execute(
                            update(SessionState)
                            .where(SessionState.conversation_id == conversation)
                            .values(**values)
                        )
                    ),
                    expected,
                )

    def test_an_older_summary_cannot_overwrite_a_newer_one(self):
        # The guard the docs ask for ("old Turn completes late"): the writer
        # states the sequence it covers and the update is conditional on it.
        conversation = self.add_conversation()
        self.session.execute(
            insert(SessionState).values(
                conversation_id=conversation,
                summary="newer",
                summarized_through_sequence=10,
            )
        )

        def write(summary: str, through: int) -> int:
            return self.session.execute(
                update(SessionState)
                .where(
                    SessionState.conversation_id == conversation,
                    (SessionState.summarized_through_sequence.is_(None))
                    | (SessionState.summarized_through_sequence < through),
                )
                .values(summary=summary, summarized_through_sequence=through)
            ).rowcount

        self.assertEqual(write("older", 7), 0)
        self.assertEqual(write("newest", 11), 1)
        summary = self.session.execute(select(SessionState.summary)).scalar_one()
        self.assertEqual(summary, "newest")

    def test_deleting_a_conversation_deletes_messages_and_session_state_only(self):
        conversation = self.add_conversation()
        self.add_message(conversation, 0)
        self.session.execute(insert(SessionState).values(conversation_id=conversation))
        version = self.add_version(self.add_memory())
        self.session.execute(
            insert(MemorySource).values(
                memory_version_id=version,
                source_type="conversation",
                conversation_id=conversation,
            )
        )

        self.session.execute(
            delete(Conversation).where(Conversation.id == conversation)
        )

        counts = {
            model.__tablename__: self.session.execute(
                select(func.count()).select_from(model)
            ).scalar_one()
            for model in (Message, SessionState, MemoryVersion, MemorySource)
        }
        self.assertEqual(
            counts,
            {
                "messages": 0,
                "session_states": 0,
                "memory_versions": 1,
                "memory_sources": 1,
            },
        )


@requires_postgres
class MemoryScopeTest(MemoryDatabaseTestCase):
    def test_each_scope_stores_exactly_its_own_id_column(self):
        user, project, group, repo = uuid4(), uuid4(), uuid4(), uuid4()
        cases = [
            ("user", {"owner_user_id": user}),
            ("project", {"project_id": project}),
            ("project_group", {"project_group_id": group}),
            ("repo", {"repo_id": repo}),
            ("shared", {}),
        ]
        for scope, columns in cases:
            with self.subTest(scope):
                version = self.add_version(self.add_memory(), scope=scope, **columns)
                stored = self.session.execute(
                    select(
                        MemoryVersion.scope,
                        MemoryVersion.owner_user_id,
                        MemoryVersion.project_id,
                        MemoryVersion.project_group_id,
                        MemoryVersion.repo_id,
                    ).where(MemoryVersion.id == version)
                ).one()
                self.assertEqual(
                    tuple(stored),
                    (
                        scope,
                        columns.get("owner_user_id"),
                        columns.get("project_id"),
                        columns.get("project_group_id"),
                        columns.get("repo_id"),
                    ),
                )

    def test_a_scope_with_the_wrong_id_columns_is_rejected(self):
        user, project, group, repo = uuid4(), uuid4(), uuid4(), uuid4()
        cases = {
            "unknown scope": {"scope": "team", "project_id": project},
            "user without owner": {"scope": "user"},
            "user with project": {
                "scope": "user",
                "owner_user_id": user,
                "project_id": project,
            },
            "project without project": {"scope": "project"},
            "project with owner": {
                "scope": "project",
                "project_id": project,
                "owner_user_id": user,
            },
            "project with repo": {
                "scope": "project",
                "project_id": project,
                "repo_id": repo,
            },
            "project with group": {
                "scope": "project",
                "project_id": project,
                "project_group_id": group,
            },
            "project group without group": {"scope": "project_group"},
            "project group with project": {
                "scope": "project_group",
                "project_group_id": group,
                "project_id": project,
            },
            "project group with owner": {
                "scope": "project_group",
                "project_group_id": group,
                "owner_user_id": user,
            },
            "project group with repo": {
                "scope": "project_group",
                "project_group_id": group,
                "repo_id": repo,
            },
            "user with group": {
                "scope": "user",
                "owner_user_id": user,
                "project_group_id": group,
            },
            "repo with group": {
                "scope": "repo",
                "repo_id": repo,
                "project_group_id": group,
            },
            "shared with group": {"scope": "shared", "project_group_id": group},
            "repo without repo": {"scope": "repo"},
            "repo with project": {
                "scope": "repo",
                "repo_id": repo,
                "project_id": project,
            },
            "shared with owner": {"scope": "shared", "owner_user_id": user},
            "shared with repo": {"scope": "shared", "repo_id": repo},
        }
        for label, columns in cases.items():
            with self.subTest(label):
                memory = self.add_memory()
                self.assertEqual(
                    self.violation(partial(self.add_version, memory, **columns)),
                    "ck_memory_versions_scope_columns",
                )


@requires_postgres
class MemoryVersionRulesTest(MemoryDatabaseTestCase):
    def test_a_default_row_stores_the_documented_defaults(self):
        version = self.add_version(self.add_memory())

        row = self.session.execute(
            select(
                MemoryVersion.importance,
                MemoryVersion.pinned,
                MemoryVersion.on_stale,
                MemoryVersion.revalidate_triggers,
                MemoryVersion.attributes,
                MemoryVersion.stale_since,
            ).where(MemoryVersion.id == version)
        ).one()
        self.assertEqual(tuple(row), (50, False, "lower_priority", [], {}, None))

    def test_each_freshness_policy_keeps_its_own_fields(self):
        verified = utc(2026, 9, 16)
        accepted = {
            "permanent": {},
            "session_only": {},
            "revalidate": {
                "verified_at": verified,
                "revalidate_after": timedelta(days=90),
                "revalidate_triggers": ["related_setting_changed", "member_changed"],
            },
            "repo_commit": {"commit_sha": SHA1, "branch": "main"},
            "expiring": {"expires_at": utc(2026, 10, 31)},
        }
        for policy, fields in accepted.items():
            with self.subTest(policy):
                version = self.add_version(
                    self.add_memory(), freshness_policy=policy, **fields
                )
                stored = self.session.execute(
                    select(MemoryVersion).where(MemoryVersion.id == version)
                ).scalar_one()
                self.assertEqual(stored.freshness_policy, policy)
                for column, value in fields.items():
                    self.assertEqual(getattr(stored, column), value)

    def test_a_stale_candidate_is_marked_without_being_invalidated(self):
        version = self.add_version(
            self.add_memory(),
            freshness_policy="revalidate",
            verified_at=utc(2026, 1, 1),
            revalidate_after=timedelta(days=90),
        )

        self.session.execute(
            update(MemoryVersion)
            .where(MemoryVersion.id == version)
            .values(stale_since=utc(2026, 4, 1))
        )

        stored = self.session.execute(
            select(MemoryVersion.status, MemoryVersion.stale_since).where(
                MemoryVersion.id == version
            )
        ).one()
        self.assertEqual(tuple(stored), ("active", utc(2026, 4, 1)))

    def test_a_sha256_commit_id_is_accepted(self):
        version = self.add_version(
            self.add_memory(), freshness_policy="repo_commit", commit_sha=SHA256
        )
        self.assertEqual(len(SHA256), 64)
        self.assertIsNotNone(version)

    def test_invalid_values_are_rejected_by_the_named_constraint(self):
        cases = {
            "ck_memory_versions_version_number_positive": [{"version_number": 0}],
            "ck_memory_versions_memory_type_length": [
                {"memory_type": ""},
                {"memory_type": "t" * 65},
            ],
            "ck_memory_versions_title_length": [{"title": ""}, {"title": "t" * 201}],
            "ck_memory_versions_content_not_empty": [{"content": ""}],
            "ck_memory_versions_importance_range": [
                {"importance": -1},
                {"importance": 101},
            ],
            "ck_memory_versions_status_valid": [{"status": "archived"}],
            "ck_memory_versions_confirmation_state_valid": [
                {"confirmation_state": "maybe"}
            ],
            "ck_memory_versions_rejected_not_active": [
                {"confirmation_state": "rejected", "status": "active"}
            ],
            "ck_memory_versions_freshness_policy_valid": [
                {"freshness_policy": "forever"}
            ],
            "ck_memory_versions_freshness_fields": [
                {
                    "freshness_policy": "revalidate",
                    "revalidate_after": timedelta(days=1),
                },
                {"freshness_policy": "revalidate", "verified_at": utc(2026, 1, 1)},
                {"freshness_policy": "expiring"},
                {"freshness_policy": "repo_commit", "branch": "main"},
            ],
            "ck_memory_versions_revalidate_after_positive": [
                {"revalidate_after": timedelta(0)},
                {"revalidate_after": timedelta(days=-1)},
            ],
            "ck_memory_versions_on_stale_valid": [{"on_stale": "delete"}],
            "ck_memory_versions_commit_sha_format": [
                {"commit_sha": "abc123"},
                {"commit_sha": SHA1.upper()},
                {"commit_sha": SHA1 + "0"},
            ],
            "ck_memory_versions_attributes_object": [{"attributes": ["list"]}],
            "ck_memory_versions_actor_type_valid": [{"actor_type": "robot"}],
            "ck_memory_versions_user_actor_has_id": [{"actor_type": "user"}],
        }
        for expected, variants in cases.items():
            for overrides in variants:
                with self.subTest(expected, overrides=overrides):
                    memory = self.add_memory()
                    self.assertEqual(
                        self.violation(partial(self.add_version, memory, **overrides)),
                        expected,
                    )

    def test_a_version_needs_an_existing_memory(self):
        orphan = lambda: self.session.execute(  # noqa: E731
            insert(MemoryVersion).values(**version_values(uuid4()))
        )
        self.assertEqual(
            self.violation(orphan), "fk_memory_versions_memory_id_memories"
        )

    def test_a_rejected_preference_may_be_kept_as_history(self):
        version = self.add_version(
            self.add_memory(), confirmation_state="rejected", status="history"
        )
        self.assertIsNotNone(version)

    def test_a_user_actor_is_recorded_with_its_id(self):
        actor = uuid4()
        version = self.add_version(
            self.add_memory(),
            actor_type="user",
            actor_user_id=actor,
            change_reason="edited in the Memory UI",
        )
        row = self.session.execute(
            select(MemoryVersion.actor_user_id, MemoryVersion.change_reason).where(
                MemoryVersion.id == version
            )
        ).one()
        self.assertEqual(tuple(row), (actor, "edited in the Memory UI"))


@requires_postgres
class VersioningTest(MemoryDatabaseTestCase):
    def relation(self, newer, older, kind="supersedes"):
        return self.session.execute(
            insert(MemoryRelation).values(
                from_version_id=newer, to_version_id=older, relation_type=kind
            )
        )

    def history(self, memory):
        rows = self.session.execute(
            select(
                MemoryVersion.version_number,
                MemoryVersion.status,
                MemoryVersion.content,
            )
            .where(MemoryVersion.memory_id == memory)
            .order_by(MemoryVersion.version_number)
        )
        return [tuple(row) for row in rows]

    def test_a_new_version_supersedes_the_old_one_and_history_stays_queryable(self):
        memory = self.add_memory()
        v1 = self.add_version(memory, version_number=1, content="Use tabs")

        # Two active versions of one memory cannot exist.
        both_active = self.violation(
            lambda: self.add_version(memory, version_number=2, content="Use spaces")
        )
        self.assertEqual(both_active, "ix_memory_versions_one_active")

        self.session.execute(
            update(MemoryVersion)
            .where(MemoryVersion.id == v1)
            .values(status="superseded")
        )
        v2 = self.add_version(memory, version_number=2, content="Use spaces")
        self.relation(v2, v1)

        self.assertEqual(
            self.history(memory),
            [(1, "superseded", "Use tabs"), (2, "active", "Use spaces")],
        )
        active = self.session.execute(
            select(MemoryVersion.content).where(
                MemoryVersion.memory_id == memory, MemoryVersion.status == "active"
            )
        ).scalars()
        self.assertEqual(list(active), ["Use spaces"])
        superseded_by = self.session.execute(
            select(MemoryRelation.from_version_id).where(
                MemoryRelation.to_version_id == v1,
                MemoryRelation.relation_type == "supersedes",
            )
        ).scalar_one()
        self.assertEqual(superseded_by, v2)

    def test_restoring_an_old_version_creates_a_new_active_version(self):
        memory = self.add_memory()
        v1 = self.add_version(
            memory, version_number=1, content="Use tabs", status="superseded"
        )
        v2 = self.add_version(memory, version_number=2, content="Use spaces")
        self.relation(v2, v1)

        # Undo: v2 becomes superseded, v3 copies v1's content and becomes active.
        self.session.execute(
            update(MemoryVersion)
            .where(MemoryVersion.id == v2)
            .values(status="superseded")
        )
        v3 = self.add_version(
            memory,
            version_number=3,
            content="Use tabs",
            change_reason="restored from version 1",
        )
        self.relation(v3, v2)

        self.assertEqual(
            self.history(memory),
            [
                (1, "superseded", "Use tabs"),
                (2, "superseded", "Use spaces"),
                (3, "active", "Use tabs"),
            ],
        )
        self.assertIsNotNone(v3)

    def test_two_writers_cannot_both_create_the_same_version_number(self):
        memory = self.add_memory()
        self.add_version(memory, version_number=1)
        self.add_version(memory, version_number=2, status="deprecated")

        clash = self.violation(
            lambda: self.add_version(memory, version_number=2, status="deprecated")
        )

        self.assertEqual(clash, "uq_memory_versions_memory_id")

    def test_one_active_version_is_per_memory_not_global(self):
        first, second = self.add_memory(), self.add_memory()
        self.add_version(first)
        self.add_version(second)  # a second active version elsewhere is fine

        active = self.session.execute(
            select(func.count())
            .select_from(MemoryVersion)
            .where(MemoryVersion.status == "active")
        ).scalar_one()
        self.assertEqual(active, 2)

    def test_any_number_of_inactive_versions_are_kept(self):
        memory = self.add_memory()
        for number, status in enumerate(
            ["superseded", "superseded", "deprecated", "history"], start=1
        ):
            self.add_version(memory, version_number=number, status=status)
        self.add_version(memory, version_number=5)

        self.assertEqual(
            [status for _, status, _ in self.history(memory)],
            ["superseded", "superseded", "deprecated", "history", "active"],
        )

    def test_a_version_is_superseded_by_at_most_one_version(self):
        memory = self.add_memory()
        old = self.add_version(memory, version_number=1, status="superseded")
        first = self.add_version(memory, version_number=2)
        second = self.add_version(memory, version_number=3, status="history")
        self.relation(first, old)

        self.assertEqual(
            self.violation(lambda: self.relation(second, old)),
            "ix_memory_relations_one_successor",
        )
        # Other relation kinds towards the same version are not limited.
        self.assertIsNone(
            self.violation(lambda: self.relation(second, old, "conflicts_with"))
        )

    def test_a_version_never_relates_to_itself(self):
        version = self.add_version(self.add_memory())

        self.assertEqual(
            self.violation(lambda: self.relation(version, version)),
            "ck_memory_relations_not_self_referencing",
        )

    def test_every_relation_type_is_accepted_and_others_are_not(self):
        memory = self.add_memory()
        newer = self.add_version(memory, version_number=2)
        older = self.add_version(memory, version_number=1, status="superseded")
        kinds = [
            "supersedes",
            "extends",
            "conflicts_with",
            "confirmed_from",
            "revalidated_from",
            "merged_from",
        ]
        for kind in kinds:
            with self.subTest(kind):
                self.assertIsNone(
                    self.violation(partial(self.relation, newer, older, kind))
                )

        self.assertEqual(
            self.violation(lambda: self.relation(older, newer, "replaces")),
            "ck_memory_relations_relation_type_valid",
        )
        stored = self.session.execute(
            select(func.count()).select_from(MemoryRelation)
        ).scalar_one()
        self.assertEqual(stored, len(kinds))

    def test_the_same_relation_cannot_be_stored_twice_but_the_reverse_can(self):
        memory = self.add_memory()
        a = self.add_version(memory, version_number=1, status="history")
        b = self.add_version(memory, version_number=2, status="history")
        self.relation(a, b, "conflicts_with")

        self.assertEqual(
            self.violation(lambda: self.relation(a, b, "conflicts_with")),
            "uq_memory_relations_from_version_id",
        )
        self.assertIsNone(self.violation(lambda: self.relation(b, a, "conflicts_with")))

    def test_a_relation_needs_two_existing_versions(self):
        version = self.add_version(self.add_memory())

        self.assertEqual(
            self.violation(lambda: self.relation(version, uuid4())),
            "fk_memory_relations_to_version_id_memory_versions",
        )
        self.assertEqual(
            self.violation(lambda: self.relation(uuid4(), version)),
            "fk_memory_relations_from_version_id_memory_versions",
        )

    def test_a_relation_can_link_memories_of_different_scopes(self):
        # Inferred user preference -> confirmed project memory (history graph).
        inferred = self.add_version(
            self.add_memory(),
            confirmation_state="inferred",
            status="superseded",
        )
        confirmed = self.add_version(
            self.add_memory(), scope="project", project_id=uuid4()
        )
        self.relation(confirmed, inferred, "confirmed_from")

        kinds = self.session.execute(select(MemoryRelation.relation_type)).scalars()
        self.assertEqual(list(kinds), ["confirmed_from"])

    def test_deleting_a_memory_removes_all_of_its_versions_and_their_rows(self):
        memory = self.add_memory()
        v1 = self.add_version(memory, version_number=1, status="superseded")
        v2 = self.add_version(memory, version_number=2)
        self.relation(v2, v1)
        self.session.execute(
            insert(MemorySource).values(
                memory_version_id=v2, source_type="task", source_ref="task-7"
            )
        )
        self.register_embedding_model("model-a", 2)
        self.session.execute(
            insert(MemoryEmbedding).values(
                memory_version_id=v2,
                embedding_model_id="model-a",
                dimensions=2,
                embedding=[0.5, 0.5],
            )
        )

        self.session.execute(delete(Memory).where(Memory.id == memory))

        remaining = {
            model.__tablename__: self.session.execute(
                select(func.count()).select_from(model)
            ).scalar_one()
            for model in (
                MemoryVersion,
                MemoryRelation,
                MemorySource,
                MemoryEmbedding,
            )
        }
        self.assertEqual(
            remaining,
            {
                "memory_versions": 0,
                "memory_relations": 0,
                "memory_sources": 0,
                "memory_embeddings": 0,
            },
        )

    def test_deleting_one_version_removes_its_edges_and_keeps_the_others(self):
        memory = self.add_memory()
        old = self.add_version(memory, version_number=1, status="superseded")
        new = self.add_version(memory, version_number=2)
        self.relation(new, old)

        self.session.execute(delete(MemoryVersion).where(MemoryVersion.id == old))

        self.assertEqual(
            self.history(memory), [(2, "active", "Use tabs for indentation.")]
        )
        edges = self.session.execute(
            select(func.count()).select_from(MemoryRelation)
        ).scalar_one()
        self.assertEqual(edges, 0)


@requires_postgres
class ForeignKeyIndexTest(MemoryDatabaseTestCase):
    """Deleting a parent row must not scan the child table (referential actions)."""

    def test_every_foreign_key_has_an_index_that_leads_with_its_column(self):
        self.assertEqual(foreign_keys_without_index(self.connection), [])

    def test_the_check_reports_a_foreign_key_whose_index_is_missing(self):
        # Proves the check above can fail: remove the indexes that serve one
        # single-column foreign key each.
        for index, foreign_key in [
            ("ix_memory_sources_message_id", "fk_memory_sources_message_id_messages"),
            (
                "ix_memory_relations_to_version_id",
                "fk_memory_relations_to_version_id_memory_versions",
            ),
            (
                "ix_memory_sources_memory_version_id",
                "fk_memory_sources_memory_version_id_memory_versions",
            ),
        ]:
            with self.subTest(index), self.connection.begin_nested() as savepoint:
                self.connection.execute(text(f"DROP INDEX {index}"))
                self.assertEqual(
                    foreign_keys_without_index(self.connection), [foreign_key]
                )
                savepoint.rollback()

    def test_a_message_lookup_in_sources_uses_the_message_index(self):
        # The lookup a message deletion runs for ``ON DELETE SET NULL``, on a
        # table big enough that the planner does not just scan it.
        conversation = self.add_conversation()
        version = self.add_version(self.add_memory())
        for sequence in range(300):
            message = self.add_message(conversation, sequence)
            self.session.execute(
                insert(MemorySource).values(
                    memory_version_id=version,
                    source_type="conversation",
                    conversation_id=conversation,
                    message_id=message,
                )
            )
        self.connection.execute(
            text(
                "INSERT INTO memory_sources"
                " (memory_version_id, source_type, source_ref)"
                " SELECT :version, 'task', 'task-' || n"
                " FROM generate_series(1, 30000) n"
            ),
            {"version": version},
        )
        self.connection.execute(text("ANALYZE memory_sources"))
        target = self.session.execute(select(Message.id).limit(1)).scalar_one()

        plan = "\n".join(
            self.connection.execute(
                text("EXPLAIN SELECT 1 FROM memory_sources WHERE message_id = :id"),
                {"id": target},
            ).scalars()
        )

        self.assertIn("ix_memory_sources_message_id", plan)


@requires_postgres
class ProvenanceTest(MemoryDatabaseTestCase):
    def add_source(self, version, source_type, **values):
        return self.session.execute(
            insert(MemorySource).values(
                memory_version_id=version, source_type=source_type, **values
            )
        )

    def settle_deferred_checks(self):
        """Run the deferred checks now, as a COMMIT would, and defer them again."""
        self.session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        self.session.execute(text("SET CONSTRAINTS ALL DEFERRED"))

    def add_source_and_settle(self, version, source_type, **values):
        self.add_source(version, source_type, **values)
        self.settle_deferred_checks()

    def sources_of(self, version):
        rows = self.session.execute(
            select(
                MemorySource.source_type,
                MemorySource.conversation_id,
                MemorySource.source_ref,
                MemorySource.source_deleted_at,
            )
            .where(MemorySource.memory_version_id == version)
            .order_by(MemorySource.created_at, MemorySource.source_type)
        )
        return [tuple(row) for row in rows]

    def test_a_version_keeps_several_sources_of_different_kinds(self):
        c105, c144 = self.add_conversation(), self.add_conversation()
        version = self.add_version(self.add_memory())
        self.add_source(version, "conversation", conversation_id=c105)
        self.add_source(version, "conversation", conversation_id=c144)
        self.add_source(version, "project_decision", source_ref="decision-12")

        stored = self.sources_of(version)

        self.assertEqual(
            sorted(stored, key=lambda row: (row[0], str(row[1]))),
            sorted(
                [
                    ("conversation", c105, None, None),
                    ("conversation", c144, None, None),
                    ("project_decision", None, "decision-12", None),
                ],
                key=lambda row: (row[0], str(row[1])),
            ),
        )

    def test_a_source_can_point_at_the_message_that_was_the_evidence(self):
        conversation = self.add_conversation()
        message = self.add_message(conversation, 0)
        version = self.add_version(self.add_memory())
        self.add_source(
            version, "conversation", conversation_id=conversation, message_id=message
        )

        stored = self.session.execute(select(MemorySource.message_id)).scalar_one()
        self.assertEqual(stored, message)

    def test_a_source_message_must_belong_to_the_source_conversation(self):
        conversation_a, conversation_b = (
            self.add_conversation(),
            self.add_conversation(),
        )
        message_of_b = self.add_message(conversation_b, 0)
        version = self.add_version(self.add_memory())

        mismatched = partial(
            self.add_source,
            version,
            "conversation",
            conversation_id=conversation_a,
            message_id=message_of_b,
        )
        matching = partial(
            self.add_source,
            version,
            "conversation",
            conversation_id=conversation_b,
            message_id=message_of_b,
        )

        self.assertEqual(
            self.violation(mismatched), "fk_memory_sources_conversation_id_messages"
        )
        self.assertIsNone(self.violation(matching))
        stored = self.session.execute(
            select(MemorySource.conversation_id, MemorySource.message_id)
        ).all()
        self.assertEqual(
            [tuple(row) for row in stored], [(conversation_b, message_of_b)]
        )

    def test_a_source_naming_a_message_must_name_its_conversation(self):
        # ``MATCH SIMPLE`` skips the composite foreign key when the conversation
        # is NULL, and the standalone message key is satisfied by any message.
        conversation = self.add_conversation()
        message = self.add_message(conversation, 0)
        version = self.add_version(self.add_memory())

        message_only = partial(
            self.add_source_and_settle, version, "conversation", message_id=message
        )
        message_and_conversation = partial(
            self.add_source_and_settle,
            version,
            "conversation",
            conversation_id=conversation,
            message_id=message,
        )
        conversation_only = partial(
            self.add_source_and_settle,
            version,
            "conversation",
            conversation_id=conversation,
        )

        self.assertEqual(
            self.violation(message_only),
            "tr_memory_sources_message_requires_conversation",
        )
        self.assertIsNone(self.violation(message_and_conversation))
        self.assertIsNone(self.violation(conversation_only))
        stored = self.session.execute(
            select(MemorySource.conversation_id, MemorySource.message_id).order_by(
                MemorySource.message_id.is_(None)
            )
        ).all()
        self.assertEqual(
            [tuple(row) for row in stored],
            [(conversation, message), (conversation, None)],
        )

    def test_the_conversation_of_a_source_that_names_a_message_cannot_be_cleared(self):
        conversation = self.add_conversation()
        message = self.add_message(conversation, 0)
        version = self.add_version(self.add_memory())
        self.add_source(
            version, "conversation", conversation_id=conversation, message_id=message
        )

        def clear_the_conversation():
            self.session.execute(update(MemorySource).values(conversation_id=None))
            self.settle_deferred_checks()

        self.assertEqual(
            self.violation(clear_the_conversation),
            "tr_memory_sources_message_requires_conversation",
        )
        stored = self.session.execute(
            select(MemorySource.conversation_id, MemorySource.message_id)
        ).one()
        self.assertEqual(tuple(stored), (conversation, message))

    def test_deleting_a_conversation_passes_the_message_requires_conversation_check(
        self,
    ):
        # The foreign keys' SET NULL actions clear conversation_id and message_id
        # one after the other, so the row passes through (NULL, message). Only
        # the final state (both NULL) may be judged: a plain CHECK would refuse
        # the delete.
        conversation = self.add_conversation()
        message = self.add_message(conversation, 0)
        version = self.add_version(self.add_memory())
        self.add_source(
            version, "conversation", conversation_id=conversation, message_id=message
        )

        self.session.execute(
            delete(Conversation).where(Conversation.id == conversation)
        )
        self.settle_deferred_checks()

        stored = self.session.execute(
            select(MemorySource.conversation_id, MemorySource.message_id)
        ).one()
        self.assertEqual(tuple(stored), (None, None))

    def test_a_source_may_name_a_conversation_without_naming_a_message(self):
        conversation = self.add_conversation()
        version = self.add_version(self.add_memory())

        self.add_source(version, "conversation", conversation_id=conversation)

        stored = self.session.execute(
            select(MemorySource.conversation_id, MemorySource.message_id)
        ).one()
        self.assertEqual(tuple(stored), (conversation, None))

    def test_deleting_a_message_keeps_the_source_and_its_conversation(self):
        conversation = self.add_conversation()
        message = self.add_message(conversation, 0)
        version = self.add_version(self.add_memory())
        self.add_source(
            version, "conversation", conversation_id=conversation, message_id=message
        )

        self.session.execute(delete(Message).where(Message.id == message))

        stored = self.session.execute(
            select(MemorySource.conversation_id, MemorySource.message_id)
        ).one()
        self.assertEqual(tuple(stored), (conversation, None))

    def test_deleting_a_conversation_clears_both_references_of_its_sources(self):
        conversation = self.add_conversation()
        message = self.add_message(conversation, 0)
        version = self.add_version(self.add_memory())
        self.add_source(
            version, "conversation", conversation_id=conversation, message_id=message
        )

        self.session.execute(
            delete(Conversation).where(Conversation.id == conversation)
        )

        stored = self.session.execute(
            select(MemorySource.conversation_id, MemorySource.message_id)
        ).one()
        self.assertEqual(tuple(stored), (None, None))

    def test_the_message_reference_is_cleared_by_column_not_the_whole_pair(self):
        # ``ON DELETE SET NULL (message_id)``: a plain SET NULL would also null
        # conversation_id and lose which conversation the memory came from.
        cleared = self.connection.execute(
            text(
                "SELECT array_agg(a.attname)"
                " FROM pg_constraint con"
                " JOIN pg_attribute a ON a.attrelid = con.conrelid"
                "   AND a.attnum = ANY (con.confdelsetcols)"
                " WHERE con.conname = 'fk_memory_sources_conversation_id_messages'"
            )
        ).scalar_one()
        self.assertEqual(cleared, ["message_id"])

    def test_deleting_a_conversation_keeps_the_memory_and_its_other_sources(self):
        doomed, kept = self.add_conversation(), self.add_conversation()
        message = self.add_message(doomed, 0)
        version = self.add_version(self.add_memory())
        self.add_source(
            version, "conversation", conversation_id=doomed, message_id=message
        )
        self.add_source(version, "conversation", conversation_id=kept)
        self.add_source(version, "project_decision", source_ref="decision-12")

        self.session.execute(delete(Conversation).where(Conversation.id == doomed))
        # The deletion flow records what was lost (a foreign-key action cannot).
        self.session.execute(
            update(MemorySource)
            .where(
                MemorySource.memory_version_id == version,
                MemorySource.source_type == "conversation",
                MemorySource.conversation_id.is_(None),
            )
            .values(source_deleted_at=utc(2026, 9, 24))
        )

        survivors = self.session.execute(
            select(MemoryVersion.content).where(MemoryVersion.id == version)
        ).scalars()
        self.assertEqual(list(survivors), ["Use tabs for indentation."])
        stored = self.sources_of(version)
        self.assertEqual(len(stored), 3)
        self.assertEqual(
            sorted(
                (row[1] == kept, row[2] or "", row[3] is not None) for row in stored
            ),
            [
                (False, "", True),  # the deleted conversation
                (False, "decision-12", False),
                (True, "", False),  # the surviving conversation
            ],
        )
        message_left = self.session.execute(
            select(func.count()).select_from(Message)
        ).scalar_one()
        self.assertEqual(message_left, 0)

    def test_memories_derived_from_a_conversation_can_be_listed_for_the_deletion_flow(
        self,
    ):
        conversation = self.add_conversation()
        mine = self.add_version(self.add_memory(), title="mine")
        other = self.add_version(self.add_memory(), title="other")
        self.add_source(mine, "conversation", conversation_id=conversation)
        self.add_source(other, "task", source_ref="task-1")

        titles = self.session.execute(
            select(MemoryVersion.title)
            .join(MemorySource, MemorySource.memory_version_id == MemoryVersion.id)
            .where(MemorySource.conversation_id == conversation)
        ).scalars()

        self.assertEqual(list(titles), ["mine"])

    def test_source_rows_must_match_their_type(self):
        conversation = self.add_conversation()
        version = self.add_version(self.add_memory())
        cases = {
            "ck_memory_sources_source_type_valid": lambda: self.add_source(
                version, "rumour", source_ref="x"
            ),
            "ck_memory_sources_other_sources_have_reference": lambda: self.add_source(
                version, "task"
            ),
            "ck_memory_sources_conversation_reference_only_for_conversation": lambda: (
                self.add_source(
                    version, "task", source_ref="t", conversation_id=conversation
                )
            ),
            "ck_memory_sources_conversation_has_no_opaque_reference": lambda: (
                self.add_source(
                    version,
                    "conversation",
                    conversation_id=conversation,
                    source_ref="x",
                )
            ),
            "fk_memory_sources_conversation_id_conversations": lambda: self.add_source(
                version, "conversation", conversation_id=uuid4()
            ),
            "fk_memory_sources_message_id_messages": lambda: self.add_source(
                version, "conversation", message_id=uuid4()
            ),
            "fk_memory_sources_memory_version_id_memory_versions": lambda: (
                self.add_source(uuid4(), "task", source_ref="t")
            ),
        }
        for expected, action in cases.items():
            with self.subTest(expected):
                self.assertEqual(self.violation(action), expected)

    def test_every_source_type_is_accepted(self):
        conversation = self.add_conversation()
        version = self.add_version(self.add_memory())
        self.add_source(version, "conversation", conversation_id=conversation)
        for kind in ("task", "repo_analysis", "user_confirmation", "project_decision"):
            with self.subTest(kind):
                self.assertIsNone(
                    self.violation(
                        partial(self.add_source, version, kind, source_ref="r")
                    )
                )

    def test_sources_of_two_versions_of_one_memory_are_independent(self):
        memory = self.add_memory()
        v1 = self.add_version(memory, version_number=1, status="superseded")
        v2 = self.add_version(memory, version_number=2)
        self.add_source(v1, "task", source_ref="task-1")
        self.add_source(v2, "user_confirmation", source_ref="confirm-1")

        self.assertEqual(
            [row[0] for row in self.sources_of(v1)]
            + [row[0] for row in self.sources_of(v2)],
            ["task", "user_confirmation"],
        )


@requires_postgres
class CommittedProvenanceTest(MemoryDatabaseTestCase):
    """Provenance rules that only show at COMMIT, on data that is really committed.

    The other tests run in one rolled-back transaction. Here every step commits,
    because PostgreSQL treats a row inserted in the *same* transaction specially
    (its foreign key is re-checked even when the key did not change), which hides
    how a conversation delete behaves for rows that were committed earlier.
    """

    MESSAGE_REQUIRES_CONVERSATION = "tr_memory_sources_message_requires_conversation"

    def setUp(self) -> None:
        self.conversation = uuid4()
        self.message = uuid4()
        self.memory = uuid4()
        self.version = uuid4()
        self.addCleanup(self.remove_rows)

    def remove_rows(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                delete(Conversation).where(Conversation.id == self.conversation)
            )
            connection.execute(delete(Memory).where(Memory.id == self.memory))

    def commit_conversation_with_a_memory(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                insert(Conversation).values(id=self.conversation, owner_user_id=uuid4())
            )
            connection.execute(
                insert(Message).values(
                    id=self.message,
                    conversation_id=self.conversation,
                    turn_id=uuid4(),
                    event_sequence=0,
                    role="user",
                    content="hello",
                )
            )
            connection.execute(insert(Memory).values(id=self.memory))
            connection.execute(
                insert(MemoryVersion).values(
                    id=self.version, **version_values(self.memory)
                )
            )

    def commit_source(self, **values) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                insert(MemorySource).values(
                    memory_version_id=self.version,
                    source_type="conversation",
                    **values,
                )
            )

    def stored_sources(self) -> list[tuple]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(MemorySource.conversation_id, MemorySource.message_id).where(
                    MemorySource.memory_version_id == self.version
                )
            )
            return [tuple(row) for row in rows]

    def test_committing_a_source_that_names_a_message_without_its_conversation_fails(
        self,
    ):
        self.commit_conversation_with_a_memory()

        with self.assertRaises(IntegrityError) as caught:
            self.commit_source(message_id=self.message)

        self.assertEqual(
            caught.exception.orig.diag.constraint_name,
            self.MESSAGE_REQUIRES_CONVERSATION,
        )
        self.assertEqual(self.stored_sources(), [])

    def test_a_conversation_delete_commits_and_clears_both_references(self):
        self.commit_conversation_with_a_memory()
        self.commit_source(conversation_id=self.conversation, message_id=self.message)

        with self.engine.begin() as connection:
            connection.execute(
                delete(Conversation).where(Conversation.id == self.conversation)
            )

        self.assertEqual(self.stored_sources(), [(None, None)])

    def test_the_delete_does_not_depend_on_which_foreign_key_action_runs_first(self):
        # Referential actions of one table fire in the order of the triggers'
        # names, which come from object ids. Re-creating the messages' foreign
        # key gives it a newer id: the sources' conversation key now fires
        # first, and the row passes through (NULL, message) before the message
        # goes. A plain CHECK on the pair would refuse the delete here.
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE messages"
                    " DROP CONSTRAINT fk_messages_conversation_id_conversations"
                )
            )
            connection.execute(
                text(
                    "ALTER TABLE messages ADD CONSTRAINT"
                    " fk_messages_conversation_id_conversations"
                    " FOREIGN KEY (conversation_id) REFERENCES conversations (id)"
                    " ON DELETE CASCADE"
                )
            )
        self.commit_conversation_with_a_memory()
        self.commit_source(conversation_id=self.conversation, message_id=self.message)
        self.commit_source(conversation_id=self.conversation)

        with self.engine.begin() as connection:
            connection.execute(
                delete(Conversation).where(Conversation.id == self.conversation)
            )

        self.assertEqual(self.stored_sources(), [(None, None), (None, None)])
