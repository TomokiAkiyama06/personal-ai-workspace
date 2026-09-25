"""The application role's privileges on the Memory schema (real PostgreSQL).

In the split-role deployment the migrations run as the owner and the backend
connects as an unprivileged role (``PAW_APP_DATABASE_ROLE``), so revision 0040
must grant that role exactly what the services need. These tests migrate with a
NON-superuser role configured (a unique name per run) and connect as it: the
reads, inserts, versioning flow and vector search of the other memory tests
must work, and everything the design keeps out of reach must be denied.

Skipped unless ``PAW_TEST_DATABASE_URL`` is set.
"""

import io
import unittest
import uuid

from alembic import command
from sqlalchemy import create_engine, delete, func, insert, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ProgrammingError

from paw_backend.memory.acl import Principal, readable_memory_versions
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
    MEMORY_TABLES,
    TEST_DATABASE_URL,
    MemoryDatabaseTestCase,
    migrate,
    requires_postgres,
    sync_database_url,
)
from .support import paw_environment
from .test_migrations import offline_config

_RUN = uuid.uuid4().hex[:10]
APP_ROLE = f"paw_memgrants_app_{_RUN}"
OTHER_ROLE = f"paw_memgrants_other_{_RUN}"
PASSWORD = "dummy-test-password-memgrants"
INSUFFICIENT_PRIVILEGE = "42501"

# What the application role may do per table: the table-level privileges and,
# for the tables it may update only in part, the updatable columns. The exact
# copy of the choices (and their reasons) in the migration.
EXPECTED = {
    "conversations": ({"SELECT", "INSERT", "DELETE"}, {"title", "updated_at"}),
    "messages": ({"SELECT", "INSERT"}, set()),
    "session_states": (
        {"SELECT", "INSERT"},
        {"summary", "state", "summarized_through_sequence", "updated_at"},
    ),
    "memories": ({"SELECT", "INSERT", "DELETE"}, set()),
    "memory_versions": (
        {"SELECT", "INSERT"},
        {"status", "stale_since", "pinned", "importance"},
    ),
    "memory_relations": ({"SELECT", "INSERT"}, set()),
    "memory_sources": ({"SELECT", "INSERT"}, {"source_deleted_at"}),
    "embedding_models": ({"SELECT", "INSERT"}, set()),
    "memory_embeddings": ({"SELECT", "INSERT", "DELETE"}, set()),
}
ALL_PRIVILEGES = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
)


def migrate_with_role(action: str, revision: str, role: str | None = APP_ROLE) -> None:
    """Run alembic as the owner with the application role configured."""
    environment = {"PAW_DATABASE_URL": TEST_DATABASE_URL}
    if role is not None:
        environment["PAW_APP_DATABASE_ROLE"] = role
    with paw_environment(**environment):
        getattr(command, action)(offline_config(io.StringIO()), revision)


def drop_test_roles(engine) -> None:
    with engine.begin() as connection:
        for role in (APP_ROLE, OTHER_ROLE):
            exists = connection.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}
            ).first()
            if exists:
                connection.execute(text(f"DROP OWNED BY {role}"))
                connection.execute(text(f"DROP ROLE {role}"))


def create_test_roles(engine) -> None:
    with engine.begin() as connection:
        for role in (APP_ROLE, OTHER_ROLE):
            connection.execute(
                text(
                    f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    f"PASSWORD '{PASSWORD}'"
                )
            )


def engine_as(role: str):
    url = make_url(sync_database_url()).set(username=role, password=PASSWORD)
    return create_engine(url)


@requires_postgres
class ApplicationRoleTestCase(MemoryDatabaseTestCase):
    """The memory tests' helpers, run as the unprivileged application role."""

    owner_engine = None
    other_engine = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.owner_engine = create_engine(sync_database_url())
        drop_test_roles(cls.owner_engine)
        migrate("downgrade", "base")
        create_test_roles(cls.owner_engine)
        migrate_with_role("upgrade", "head")
        cls.engine = engine_as(APP_ROLE)
        cls.other_engine = engine_as(OTHER_ROLE)

    @classmethod
    def tearDownClass(cls) -> None:
        for engine in (cls.engine, cls.other_engine):
            engine.dispose()
        migrate("downgrade", "base")
        drop_test_roles(cls.owner_engine)
        cls.owner_engine.dispose()

    def denied(self, sql: str) -> None:
        """Assert that ``sql`` is refused for lack of privilege (and nothing else)."""
        try:
            with self.session.begin_nested():
                self.session.execute(text(sql))
        except ProgrammingError as error:
            self.assertEqual(error.orig.sqlstate, INSUFFICIENT_PRIVILEGE, sql)
            return
        self.fail(f"the application role was allowed to run: {sql}")


class PrivilegeMatrixTest(ApplicationRoleTestCase):
    def test_the_role_holds_exactly_the_documented_privileges(self):
        self.assertEqual(set(EXPECTED), set(MEMORY_TABLES))
        with self.owner_engine.connect() as connection:
            for table, (granted, update_columns) in EXPECTED.items():
                with self.subTest(table):
                    held = {
                        privilege
                        for privilege in ALL_PRIVILEGES
                        if connection.execute(
                            text("SELECT has_table_privilege(:role, :table, :priv)"),
                            {"role": APP_ROLE, "table": table, "priv": privilege},
                        ).scalar_one()
                    }
                    columns = (
                        connection.execute(
                            text(
                                "SELECT column_name FROM information_schema.columns"
                                " WHERE table_schema = 'public' AND table_name = :table"
                            ),
                            {"table": table},
                        )
                        .scalars()
                        .all()
                    )
                    updatable = {
                        column
                        for column in columns
                        if connection.execute(
                            text(
                                "SELECT has_column_privilege("
                                ":role, :table, :column, 'UPDATE')"
                            ),
                            {"role": APP_ROLE, "table": table, "column": column},
                        ).scalar_one()
                    }
                    self.assertEqual(held, granted)
                    self.assertEqual(updatable, update_columns)

    def test_a_role_without_a_grant_can_read_nothing(self):
        # Every table was stripped of PUBLIC's privileges.
        with self.other_engine.connect() as connection:
            for table in MEMORY_TABLES:
                with self.subTest(table):
                    with self.assertRaises(ProgrammingError) as caught:
                        connection.execute(text(f"SELECT count(*) FROM {table}"))
                    self.assertEqual(
                        caught.exception.orig.sqlstate, INSUFFICIENT_PRIVILEGE
                    )
                    connection.rollback()


class ApplicationFlowTest(ApplicationRoleTestCase):
    """What the services do works with the granted privileges."""

    MODEL = "grants-test-model"

    def test_conversation_and_session_state_flow(self):
        conversation = self.add_conversation()
        self.add_message(conversation, 0)
        self.add_message(conversation, 1, role="assistant", content="hi")
        self.session.execute(
            insert(SessionState).values(
                conversation_id=conversation, summary="s", summarized_through_sequence=1
            )
        )

        self.session.execute(
            update(SessionState)
            .where(SessionState.conversation_id == conversation)
            .values(summary="s2", state={"topic": "x"}, summarized_through_sequence=2)
        )
        self.session.execute(
            update(Conversation)
            .where(Conversation.id == conversation)
            .values(title="Renamed", updated_at=func.now())
        )

        stored = self.session.execute(
            select(SessionState.summary, Conversation.title).join(
                Conversation, Conversation.id == SessionState.conversation_id
            )
        ).one()
        self.assertEqual(tuple(stored), ("s2", "Renamed"))
        contents = self.session.execute(
            select(Message.content).order_by(Message.event_sequence)
        ).scalars()
        self.assertEqual(list(contents), ["hello", "hi"])

    def test_versioning_flow_with_the_column_level_update(self):
        memory = self.add_memory()
        v1 = self.add_version(memory, version_number=1, content="Use tabs")

        # Supersede: the status changes in place, the new version is inserted.
        self.session.execute(
            update(MemoryVersion)
            .where(MemoryVersion.id == v1)
            .values(
                status="superseded", stale_since=func.now(), pinned=True, importance=80
            )
        )
        v2 = self.add_version(memory, version_number=2, content="Use spaces")
        self.session.execute(
            insert(MemoryRelation).values(
                from_version_id=v2, to_version_id=v1, relation_type="supersedes"
            )
        )

        history = self.session.execute(
            select(
                MemoryVersion.version_number,
                MemoryVersion.status,
                MemoryVersion.content,
            )
            .where(MemoryVersion.memory_id == memory)
            .order_by(MemoryVersion.version_number)
        ).all()
        self.assertEqual(
            [tuple(row) for row in history],
            [(1, "superseded", "Use tabs"), (2, "active", "Use spaces")],
        )

    def test_acl_filtered_vector_search(self):
        alice, bob, project = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        self.register_embedding_model(self.MODEL, 2)
        vectors = {
            "alice-private": ([1.0, 0.0], {"owner_user_id": alice}),
            "bob-private": ([0.9, 0.1], {"owner_user_id": bob}),
            "project-note": ([0.0, 1.0], {"scope": "project", "project_id": project}),
        }
        for title, (vector, columns) in vectors.items():
            version = self.add_version(self.add_memory(), title=title, **columns)
            self.session.execute(
                insert(MemoryEmbedding).values(
                    memory_version_id=version,
                    embedding_model_id=self.MODEL,
                    dimensions=2,
                    embedding=vector,
                )
            )

        def nearest(principal):
            rows = self.session.execute(
                select(MemoryVersion.title)
                .join(
                    MemoryEmbedding,
                    MemoryEmbedding.memory_version_id == MemoryVersion.id,
                )
                .where(
                    MemoryEmbedding.embedding_model_id == self.MODEL,
                    readable_memory_versions(principal),
                    MemoryVersion.status == "active",
                )
                .order_by(MemoryEmbedding.embedding.l2_distance([1.0, 0.0]))
            )
            return list(rows.scalars())

        self.assertEqual(
            nearest(Principal(alice, {project})), ["alice-private", "project-note"]
        )
        self.assertEqual(nearest(Principal(bob)), ["bob-private"])

    def test_deleting_a_conversation_cascades_without_delete_on_its_children(self):
        conversation = self.add_conversation()
        message = self.add_message(conversation, 0)
        self.session.execute(insert(SessionState).values(conversation_id=conversation))
        version = self.add_version(self.add_memory())
        self.session.execute(
            insert(MemorySource).values(
                memory_version_id=version,
                source_type="conversation",
                conversation_id=conversation,
                message_id=message,
            )
        )

        self.session.execute(
            delete(Conversation).where(Conversation.id == conversation)
        )
        # The deletion flow records the lost source (the one updatable column).
        self.session.execute(
            update(MemorySource)
            .where(MemorySource.memory_version_id == version)
            .values(source_deleted_at=func.now())
        )
        # What a COMMIT does: the deferred check of the sources (a trigger that
        # runs with the application role's rights) accepts the final state.
        self.session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

        counts = {
            model.__tablename__: self.session.execute(
                select(func.count()).select_from(model)
            ).scalar_one()
            for model in (Message, SessionState, MemoryVersion)
        }
        self.assertEqual(
            counts, {"messages": 0, "session_states": 0, "memory_versions": 1}
        )
        source = self.session.execute(
            select(
                MemorySource.conversation_id,
                MemorySource.message_id,
                MemorySource.source_deleted_at.is_not(None),
            )
        ).one()
        self.assertEqual(tuple(source), (None, None, True))

    def test_a_source_naming_a_message_without_its_conversation_is_refused_at_commit(
        self,
    ):
        conversation = self.add_conversation()
        message = self.add_message(conversation, 0)
        version = self.add_version(self.add_memory())

        def message_only():
            self.session.execute(
                insert(MemorySource).values(
                    memory_version_id=version,
                    source_type="conversation",
                    message_id=message,
                )
            )
            self.session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

        self.assertEqual(
            self.violation(message_only),
            "tr_memory_sources_message_requires_conversation",
        )

    def test_deleting_a_memory_removes_its_whole_history_by_cascade(self):
        memory = self.add_memory()
        v1 = self.add_version(memory, version_number=1, status="superseded")
        v2 = self.add_version(memory, version_number=2)
        self.session.execute(
            insert(MemoryRelation).values(
                from_version_id=v2, to_version_id=v1, relation_type="supersedes"
            )
        )
        self.session.execute(
            insert(MemorySource).values(
                memory_version_id=v2, source_type="task", source_ref="task-1"
            )
        )
        self.register_embedding_model(self.MODEL, 2)
        self.session.execute(
            insert(MemoryEmbedding).values(
                memory_version_id=v2,
                embedding_model_id=self.MODEL,
                dimensions=2,
                embedding=[0.5, 0.5],
            )
        )

        self.session.execute(delete(Memory).where(Memory.id == memory))

        remaining = {
            model.__tablename__: self.session.execute(
                select(func.count()).select_from(model)
            ).scalar_one()
            for model in (MemoryVersion, MemoryRelation, MemorySource, MemoryEmbedding)
        }
        self.assertEqual(set(remaining.values()), {0})

    def test_embeddings_can_be_regenerated_but_not_edited(self):
        version = self.add_version(self.add_memory())
        self.register_embedding_model(self.MODEL, 2)
        add = lambda vector: self.session.execute(  # noqa: E731
            insert(MemoryEmbedding).values(
                memory_version_id=version,
                embedding_model_id=self.MODEL,
                dimensions=2,
                embedding=vector,
            )
        )
        add([1.0, 0.0])

        self.session.execute(delete(MemoryEmbedding))
        add([0.0, 1.0])

        stored = self.session.execute(select(MemoryEmbedding.embedding)).scalar_one()
        self.assertEqual(stored, [0.0, 1.0])


class ApplicationDeniedTest(ApplicationRoleTestCase):
    """What the design keeps out of the application's reach."""

    def rows(self):
        """One row in each table, so a refused change would have something to change."""
        conversation = self.add_conversation()
        message = self.add_message(conversation, 0)
        self.session.execute(insert(SessionState).values(conversation_id=conversation))
        memory = self.add_memory()
        old = self.add_version(memory, version_number=1, status="superseded")
        new = self.add_version(memory, version_number=2, title="Original title")
        self.session.execute(
            insert(MemoryRelation).values(
                from_version_id=new, to_version_id=old, relation_type="supersedes"
            )
        )
        self.session.execute(
            insert(MemorySource).values(
                memory_version_id=new,
                source_type="conversation",
                conversation_id=conversation,
                message_id=message,
            )
        )
        self.register_embedding_model("denied-model", 2)
        self.session.execute(
            insert(MemoryEmbedding).values(
                memory_version_id=new,
                embedding_model_id="denied-model",
                dimensions=2,
                embedding=[1.0, 0.0],
            )
        )

    def test_history_and_the_registry_cannot_be_deleted_from(self):
        self.rows()
        for table in (
            "memory_versions",
            "memory_relations",
            "memory_sources",
            "messages",
            "session_states",
            "embedding_models",
        ):
            with self.subTest(table):
                self.denied(f"DELETE FROM {table}")
                remaining = self.session.execute(
                    text(f"SELECT count(*) FROM {table}")
                ).scalar_one()
                self.assertGreaterEqual(remaining, 1)

    def test_immutable_columns_cannot_be_updated(self):
        self.rows()
        immutable = {
            "memory_versions": [
                "memory_id",
                "version_number",
                "scope",
                "owner_user_id",
                "project_id",
                "project_group_id",
                "repo_id",
                "memory_type",
                "title",
                "content",
                "confirmation_state",
                "freshness_policy",
                "verified_at",
                "revalidate_after",
                "expires_at",
                "commit_sha",
                "attributes",
                "actor_type",
                "actor_user_id",
                "change_reason",
                "created_at",
            ],
            "conversations": ["owner_user_id", "project_id", "repo_id", "created_at"],
            "messages": [
                "content",
                "role",
                "event_sequence",
                "turn_id",
                "conversation_id",
            ],
            "memory_relations": ["relation_type", "from_version_id", "to_version_id"],
            "memory_sources": [
                "source_type",
                "conversation_id",
                "message_id",
                "source_ref",
            ],
            "memories": ["created_at"],
            "session_states": ["conversation_id"],
            "embedding_models": ["dimensions", "id"],
            "memory_embeddings": ["embedding", "dimensions", "embedding_model_id"],
        }
        for table, columns in immutable.items():
            for column in columns:
                with self.subTest(table=table, column=column):
                    self.denied(f"UPDATE {table} SET {column} = {column}")
        title = self.session.execute(
            select(MemoryVersion.title).where(MemoryVersion.version_number == 2)
        ).scalar_one()
        self.assertEqual(title, "Original title")

    def test_the_schema_cannot_be_truncated_or_altered(self):
        self.rows()
        for table in MEMORY_TABLES:
            with self.subTest(table):
                self.denied(f"TRUNCATE {table}")
                self.denied(f"ALTER TABLE {table} ADD COLUMN extra text")
                self.denied(f"DROP TABLE {table}")
                self.denied(f"CREATE INDEX ix_denied ON {table} ((1))")

    def test_the_registry_accepts_only_new_models(self):
        self.register_embedding_model("new-model", 4)  # a plain insert works
        self.denied("UPDATE embedding_models SET dimensions = 8")
        self.denied("DELETE FROM embedding_models")
        dimension = self.session.execute(
            text("SELECT dimensions FROM embedding_models WHERE id = 'new-model'")
        ).scalar_one()
        self.assertEqual(dimension, 4)


class DowngradeUnderGrantsTest(unittest.TestCase):
    @requires_postgres
    def test_downgrade_removes_the_tables_and_every_grant(self):
        engine = create_engine(sync_database_url())
        self.addCleanup(engine.dispose)
        self.addCleanup(drop_test_roles, engine)
        drop_test_roles(engine)
        migrate("downgrade", "base")
        create_test_roles(engine)
        self.addCleanup(migrate, "downgrade", "base")
        migrate_with_role("upgrade", "head")
        with engine.connect() as connection:
            granted = connection.execute(
                text(
                    "SELECT count(DISTINCT table_name)"
                    " FROM information_schema.role_table_grants"
                    " WHERE grantee = :role AND table_name = ANY (:tables)"
                ),
                {"role": APP_ROLE, "tables": list(MEMORY_TABLES)},
            ).scalar_one()
        self.assertEqual(granted, len(MEMORY_TABLES))

        migrate("downgrade", "base")  # as the owner, with the grants in place

        with engine.connect() as connection:
            left = connection.execute(
                text(
                    "SELECT count(*) FROM information_schema.role_table_grants"
                    " WHERE grantee = :role"
                ),
                {"role": APP_ROLE},
            ).scalar_one()
            tables = connection.execute(
                text(
                    "SELECT count(*) FROM pg_tables"
                    " WHERE schemaname = 'public' AND tablename = ANY (:tables)"
                ),
                {"tables": list(MEMORY_TABLES)},
            ).scalar_one()
        self.assertEqual((left, tables), (0, 0))
        # No privilege is left for DROP ROLE to trip over.
        with engine.begin() as connection:
            connection.execute(text(f"DROP ROLE {APP_ROLE}"))
            connection.execute(text(f"DROP ROLE {OTHER_ROLE}"))


if __name__ == "__main__":
    unittest.main()
