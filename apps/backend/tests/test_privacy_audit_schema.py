"""Revision 0087: ``audit_events.details`` and what the database refuses in it.

The first classes need no server: the model and the migration repeat the same
CHECK constraints (a migration is a frozen snapshot) and must not drift, and the
SQL of the revision is small and grants nothing. The PostgreSQL classes (skipped
unless ``PAW_TEST_DATABASE_URL`` is set) run the revision up and down, compare it
with the model through Alembic's autogenerate, prove every clause of the
``research.external_send`` CHECK by violating it, and check that the append-only
protection of revision 0025 is untouched.
"""

import io
import json
import unittest
import uuid
from types import ModuleType

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, create_engine, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError, IntegrityError

from paw_backend.authz.models import (
    COUNT_PATTERN,
    DETAILS_ACTIONS,
    DETAILS_REGISTERED_CHECK,
    EXTERNAL_SEND_ACTION,
    EXTERNAL_SEND_DETAILS_KEYS,
    EXTERNAL_SEND_PROVIDER_KINDS,
    EXTERNAL_SEND_REASON,
    EXTERNAL_SEND_WITHHELD_KEYS,
    MAX_DETAILS_BYTES,
    PIECES_PATTERN,
    QUERY_CHARS_PATTERN,
    external_send_check_sql,
)
from paw_backend.db import Base
from paw_backend.research.privacy import (
    EXTERNAL_SEND_ACTION as AUDIT_ACTION,
)
from paw_backend.research.privacy import (
    EXTERNAL_SEND_REASON as AUDIT_REASON,
)
from paw_backend.research.privacy import (
    MAX_CONTEXT_PIECES,
    MAX_MINIMIZED_QUERY_CHARS,
    external_send_event,
)
from paw_backend.research.providers import ProviderKind

from .memory_support import migrate, requires_postgres, sync_database_url
from .privacy_audit_support import fingerprint_of, make_record
from .support import paw_environment
from .test_migrations import offline_config

REVISION = "0087"
PREVIOUS = "0026"
TABLE = "audit_events"
REGISTERED_CHECK = "ck_audit_events_details_registered"
SHAPE_CHECK = "ck_audit_events_external_send_details"
QUERY = "python asyncio"
# The largest a removal count may be (``MinimizedQuery`` allows ``10**6``).
MAX_COUNT = 10**6


def migration_module() -> ModuleType:
    scripts = ScriptDirectory.from_config(offline_config(io.StringIO()))
    revision = scripts.get_revision(REVISION)
    assert revision is not None
    return revision.module


def model_checks() -> dict[str, str]:
    """The text of every CHECK of the model's table, by its full name."""
    return {
        str(constraint.name): str(constraint.sqltext)
        for constraint in Base.metadata.tables[TABLE].constraints
        if isinstance(constraint, CheckConstraint)
    }


class ModelAndMigrationAgreeTest(unittest.TestCase):
    def test_the_revision_follows_0026_and_names_the_issue(self):
        module = migration_module()
        self.assertEqual((module.revision, module.down_revision), (REVISION, PREVIOUS))

    def test_the_migration_repeats_the_check_constraints_of_the_model(self):
        module = migration_module()
        checks = model_checks()
        self.assertEqual(
            {name: sql for name, sql in module._CONSTRAINTS},
            {
                REGISTERED_CHECK: checks[REGISTERED_CHECK],
                SHAPE_CHECK: checks[SHAPE_CHECK],
            },
        )
        self.assertEqual(checks[REGISTERED_CHECK], DETAILS_REGISTERED_CHECK)
        self.assertEqual(checks[SHAPE_CHECK], external_send_check_sql())

    def test_the_migration_repeats_the_literals_of_the_audit(self):
        module = migration_module()
        self.assertEqual(module._ACTION, EXTERNAL_SEND_ACTION)
        self.assertEqual(module._REASON, EXTERNAL_SEND_REASON)
        self.assertEqual(module._KEYS, EXTERNAL_SEND_DETAILS_KEYS)
        self.assertEqual(module._WITHHELD, EXTERNAL_SEND_WITHHELD_KEYS)
        self.assertEqual(module._MAX_DETAILS_BYTES, MAX_DETAILS_BYTES)
        # ... and those are the ones the sink writes.
        self.assertEqual(EXTERNAL_SEND_ACTION, AUDIT_ACTION)
        self.assertEqual(EXTERNAL_SEND_REASON, AUDIT_REASON)
        _, details = external_send_event(make_record())
        self.assertEqual(tuple(details), module._KEYS)
        self.assertEqual(tuple(details["withheld"]), module._WITHHELD)

    def test_the_provider_kinds_of_the_check_are_those_of_the_enum(self):
        # One source of truth (``ProviderKind``): the model's CHECK is built from it,
        # the migration repeats it as it was. Adding a kind without a migration that
        # registers it makes this fail (and every send to that kind would be refused).
        kinds = tuple(kind.value for kind in ProviderKind)
        self.assertEqual(EXTERNAL_SEND_PROVIDER_KINDS, kinds)
        self.assertEqual(migration_module()._PROVIDER_KINDS, kinds)
        self.assertEqual(kinds, ("web", "docs", "github", "opencode"))
        sql = external_send_check_sql()
        for kind in kinds:
            self.assertIn(f"{kind}", sql)
        self.assertIn(f"{{0,{len(kinds) - 1}}}", sql)

    def test_the_number_patterns_of_the_migration_are_those_of_the_model(self):
        module = migration_module()
        self.assertEqual(module._QUERY_CHARS, QUERY_CHARS_PATTERN)
        self.assertEqual(module._PIECES, PIECES_PATTERN)
        self.assertEqual(module._COUNT, COUNT_PATTERN)

    def test_the_literals_are_the_documented_ones(self):
        self.assertEqual(EXTERNAL_SEND_ACTION, "research.external_send")
        self.assertEqual(EXTERNAL_SEND_REASON, "send_authorized")
        self.assertEqual(MAX_DETAILS_BYTES, 2048)
        self.assertEqual(
            EXTERNAL_SEND_DETAILS_KEYS,
            (
                "query_fingerprint",
                "query_chars",
                "provider_kinds",
                "withheld",
                "credentials_removed",
                "pieces_matched",
                "abstractions",
                "truncated",
            ),
        )

    def test_the_column_is_a_nullable_jsonb_that_stores_sql_null(self):
        column = Base.metadata.tables[TABLE].columns["details"]
        self.assertIsInstance(column.type, JSONB)
        self.assertTrue(column.nullable)
        # Python None must be SQL NULL, not the JSON value null.
        self.assertTrue(column.type.none_as_null)

    def test_the_check_names_follow_the_convention_and_fit_postgres(self):
        for name in model_checks():
            with self.subTest(name=name):
                self.assertTrue(name.startswith("ck_audit_events_"))
                self.assertLessEqual(len(name), 63)

    def test_the_shape_check_names_every_key_and_no_free_text_type(self):
        sql = external_send_check_sql()
        for key in (*EXTERNAL_SEND_DETAILS_KEYS, *EXTERNAL_SEND_WITHHELD_KEYS):
            self.assertIn(f"'{key}'", sql)
        # A missing key is NULL, and a CHECK passes on NULL: it must not.
        self.assertTrue(sql.endswith(", false)"))
        self.assertIn("COALESCE(", sql)
        # Nothing is cast: PostgreSQL does not promise an order inside AND.
        self.assertNotIn("::int", sql)
        self.assertNotIn("::numeric", sql)


class OfflineMigrationTest(unittest.TestCase):
    def sql(self, action: str, span: str) -> str:
        output = io.StringIO()
        with paw_environment(PAW_DATABASE_URL="postgresql://u:p@db.invalid/paw"):
            getattr(command, action)(offline_config(output), span, sql=True)
        return output.getvalue()

    def test_upgrade_adds_one_column_and_two_not_valid_checks(self):
        sql = self.sql("upgrade", f"{PREVIOUS}:{REVISION}")
        self.assertIn("ALTER TABLE audit_events ADD COLUMN details JSONB", sql)
        for name in (REGISTERED_CHECK, SHAPE_CHECK):
            self.assertIn(f"ADD CONSTRAINT {name} CHECK", sql)
        self.assertEqual(sql.count("NOT VALID"), 2)
        # Not validated: a downgrade can leave rows of the action without details,
        # and the upgrade after it must still work.
        self.assertNotIn("VALIDATE", sql)
        self.assertNotIn("CREATE TABLE", sql)
        self.assertNotIn("DROP", sql)

    def test_upgrade_grants_nothing_and_leaves_the_triggers_alone(self):
        sql = self.sql("upgrade", f"{PREVIOUS}:{REVISION}")
        for word in ("GRANT", "REVOKE", "TRIGGER", "FUNCTION", "OWNER"):
            self.assertNotIn(word, sql)

    def test_downgrade_drops_the_checks_and_then_the_column_only(self):
        sql = self.sql("downgrade", f"{REVISION}:{PREVIOUS}")
        self.assertLess(
            sql.index(f"DROP CONSTRAINT {SHAPE_CHECK}"),
            sql.index(f"DROP CONSTRAINT {REGISTERED_CHECK}"),
        )
        self.assertLess(
            sql.index(f"DROP CONSTRAINT {REGISTERED_CHECK}"),
            sql.index("DROP COLUMN details"),
        )
        self.assertNotIn("DROP TABLE", sql)
        self.assertNotIn("DROP FUNCTION", sql)


def valid_details(**changes) -> dict:
    details = {
        "query_fingerprint": fingerprint_of(QUERY),
        "query_chars": len(QUERY),
        "provider_kinds": ["web", "docs"],
        "withheld": dict.fromkeys(EXTERNAL_SEND_WITHHELD_KEYS, 0),
        "credentials_removed": 0,
        "pieces_matched": 0,
        "abstractions": 0,
        "truncated": False,
    }
    details.update(changes)
    return details


def without(details: dict, *path: str) -> dict:
    out = json.loads(json.dumps(details))
    target = out
    for key in path[:-1]:
        target = target[key]
    del target[path[-1]]
    return out


def nested(details: dict, path: tuple[str, ...], value) -> dict:
    out = json.loads(json.dumps(details))
    target = out
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return out


INSERT = (
    "INSERT INTO audit_events (id, correlation_id, occurred_at, action, "
    "resource_kind, decision, reason, project_id, details) VALUES (:id, :cid, "
    "now(), :action, 'research_query', :decision, :reason, :project, "
    "CAST(:details AS jsonb))"
)


@requires_postgres
class DatabaseTestCase(unittest.TestCase):
    def setUp(self) -> None:
        migrate("downgrade", "base")
        self.addCleanup(migrate, "downgrade", "base")
        migrate("upgrade", "head")
        self.engine = create_engine(sync_database_url())
        self.addCleanup(self.engine.dispose)

    def scalars(self, sql: str, **params) -> list:
        with self.engine.connect() as connection:
            return list(connection.execute(text(sql), params).scalars())

    def rows(self, sql: str, **params) -> list[tuple]:
        with self.engine.connect() as connection:
            return [tuple(row) for row in connection.execute(text(sql), params)]

    def scalar(self, sql: str, **params):
        (value,) = self.scalars(sql, **params)
        return value

    def insert(
        self,
        details,
        *,
        action: str = EXTERNAL_SEND_ACTION,
        decision: str = "allow",
        reason: str = EXTERNAL_SEND_REASON,
        project: uuid.UUID | None = None,
        raw: bool = False,
    ) -> None:
        """Insert a row as the owner (``details``: a dict, or JSON text if ``raw``)."""
        if details is not None and not raw:
            details = json.dumps(details)
        with self.engine.begin() as connection:
            connection.execute(
                text(INSERT),
                {
                    "id": uuid.uuid4(),
                    "cid": uuid.uuid4(),
                    "action": action,
                    "decision": decision,
                    "reason": reason,
                    "project": project,
                    "details": details,
                },
            )

    def refused(self, details, constraint: str = SHAPE_CHECK, **kwargs) -> None:
        with self.assertRaises(IntegrityError) as caught:
            self.insert(details, **kwargs)
        self.assertEqual(caught.exception.orig.diag.constraint_name, constraint)


class MigrationTest(DatabaseTestCase):
    def columns(self) -> dict[str, tuple]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT column_name, data_type, is_nullable FROM "
                    "information_schema.columns WHERE table_name = 'audit_events'"
                )
            )
            return {name: (kind, nullable) for name, kind, nullable in rows}

    def constraints(self) -> dict[str, bool]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT conname, convalidated FROM pg_constraint "
                    "WHERE conrelid = 'audit_events'::regclass AND contype = 'c'"
                )
            )
            return dict(rows.all())

    def test_upgrade_adds_the_nullable_jsonb_column_and_two_enforced_checks(self):
        self.assertEqual(self.columns()["details"], ("jsonb", "YES"))
        # ``convalidated`` False: added NOT VALID (see the migration), yet enforced
        # for every new row: the tests below violate them.
        self.assertEqual(
            self.constraints(),
            {
                "ck_audit_events_decision_valid": True,
                REGISTERED_CHECK: False,
                SHAPE_CHECK: False,
            },
        )

    def test_downgrade_removes_the_column_and_the_checks_and_keeps_the_rows(self):
        project = uuid.uuid4()
        self.insert(valid_details(), project=project)
        self.insert(
            None, action="chat.use", decision="deny", reason="x", project=project
        )
        migrate("downgrade", PREVIOUS)
        self.assertNotIn("details", self.columns())
        self.assertEqual(set(self.constraints()), {"ck_audit_events_decision_valid"})
        # The append-only history stays; only the details are gone.
        self.assertEqual(
            self.scalar(
                "SELECT count(*) FROM audit_events WHERE project_id = :p", p=project
            ),
            2,
        )
        migrate("upgrade", REVISION)
        self.assertEqual(
            self.scalar(
                "SELECT count(details) FROM audit_events WHERE project_id = :p",
                p=project,
            ),
            0,
        )
        self.assertEqual(self.columns()["details"], ("jsonb", "YES"))

    def test_the_rows_of_before_the_revision_are_kept_and_not_rechecked(self):
        migrate("downgrade", PREVIOUS)
        project = uuid.uuid4()
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO audit_events (id, correlation_id, occurred_at, "
                    "action, resource_kind, decision, reason, project_id) VALUES "
                    "(:id, :cid, now(), 'chat.use', 'project', 'allow', 'granted', :p),"
                    " (:id2, :cid, now(), 'research.external_send', 'research_query',"
                    " 'allow', 'send_authorized', :p)"
                ),
                {
                    "id": uuid.uuid4(),
                    "id2": uuid.uuid4(),
                    "cid": uuid.uuid4(),
                    "p": project,
                },
            )
        # The second row is what a downgrade leaves behind: an external send whose
        # details are gone. The upgrade after it must still work ...
        migrate("upgrade", REVISION)
        self.assertEqual(
            self.scalar(
                "SELECT count(*) FROM audit_events WHERE project_id = :p", p=project
            ),
            2,
        )
        # ... and new rows are checked again.
        self.refused(None, project=uuid.uuid4())

    def test_the_models_and_the_migrated_table_do_not_drift(self):
        def only_audit_events(obj, name, type_, reflected, compare_to) -> bool:
            if type_ == "table":
                return name == TABLE
            table = getattr(obj, "table", None)
            return table is None or table.name == TABLE

        with self.engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={
                    "compare_type": True,
                    "compare_server_default": True,
                    "include_object": only_audit_events,
                },
            )
            self.assertEqual(compare_metadata(context, Base.metadata), [])

    def test_the_append_only_protection_is_untouched(self):
        project = uuid.uuid4()
        self.insert(valid_details(), project=project)
        triggers = self.scalar(
            "SELECT string_agg(tgname || ':' || tgenabled::text, ',' ORDER BY tgname) "
            "FROM pg_trigger WHERE tgrelid = 'audit_events'::regclass "
            "AND NOT tgisinternal"
        )
        self.assertEqual(
            triggers,
            "tr_audit_events_force_recorded_at:A,tr_audit_events_reject_truncate:A,"
            "tr_audit_events_reject_update_delete:A",
        )
        for sql in (
            "UPDATE audit_events SET details = '{}'::jsonb WHERE project_id = :p",
            "UPDATE audit_events SET reason = 'x' WHERE project_id = :p",
            "DELETE FROM audit_events WHERE project_id = :p",
            "TRUNCATE audit_events",
        ):
            with self.subTest(sql=sql):
                with self.assertRaises(DBAPIError) as caught:
                    with self.engine.begin() as connection:
                        connection.execute(text(sql), {"p": project})
                self.assertEqual(caught.exception.orig.sqlstate, "23001")
        self.assertEqual(
            self.scalar(
                "SELECT count(*) FROM audit_events WHERE project_id = :p", p=project
            ),
            1,
        )

    def test_the_database_clock_still_sets_recorded_at(self):
        project = uuid.uuid4()
        self.insert(valid_details(), project=project)
        self.assertLess(
            self.scalar(
                "SELECT abs(extract(epoch FROM now() - recorded_at)) FROM audit_events "
                "WHERE project_id = :p",
                p=project,
            ),
            60,
        )


class ExternalSendCheckTest(DatabaseTestCase):
    """One clause at a time: a row that breaks it is refused, its twin is not."""

    def test_a_valid_row_is_accepted_and_reads_back_unchanged(self):
        project = uuid.uuid4()
        details = valid_details(truncated=True, credentials_removed=3)
        self.insert(details, project=project)
        self.assertEqual(
            self.scalar(
                "SELECT details FROM audit_events WHERE project_id = :p", p=project
            ),
            details,
        )

    def test_the_shape_at_its_boundaries_is_accepted(self):
        for label, details in {
            "one provider kind": valid_details(provider_kinds=["web"]),
            "every provider kind": valid_details(
                provider_kinds=[kind.value for kind in ProviderKind]
            ),
            "a query of 1 character": valid_details(query_chars=1),
            "the longest query": valid_details(query_chars=MAX_MINIMIZED_QUERY_CHARS),
            "the largest counts": valid_details(
                credentials_removed=MAX_COUNT,
                pieces_matched=MAX_CONTEXT_PIECES,
                abstractions=MAX_COUNT,
                withheld=dict.fromkeys(EXTERNAL_SEND_WITHHELD_KEYS, MAX_CONTEXT_PIECES),
            ),
            "the smallest counts": valid_details(
                credentials_removed=0,
                pieces_matched=0,
                abstractions=0,
                withheld=dict.fromkeys(EXTERNAL_SEND_WITHHELD_KEYS, 0),
            ),
        }.items():
            with self.subTest(case=label):
                self.insert(details, project=uuid.uuid4())

    def test_a_row_without_a_project_or_with_another_decision_is_refused(self):
        self.refused(valid_details(), project=None)
        self.refused(valid_details(), project=uuid.uuid4(), decision="deny")
        self.refused(valid_details(), project=uuid.uuid4(), reason="granted")
        self.refused(valid_details(), project=uuid.uuid4(), reason="")

    def test_a_row_without_a_details_object_is_refused(self):
        project = uuid.uuid4()
        self.refused(None, project=project)  # SQL NULL: a CHECK would pass on NULL
        for label, raw in {
            "json null": "null",
            "an array": "[]",
            "a string": json.dumps(QUERY),
            "a number": "5",
            "true": "true",
            "an empty object": "{}",
        }.items():
            with self.subTest(details=label):
                # An array, a string and so on also break the general object rule.
                with self.assertRaises(IntegrityError) as caught:
                    self.insert(raw, project=project, raw=True)
                self.assertIn(
                    caught.exception.orig.diag.constraint_name,
                    {REGISTERED_CHECK, SHAPE_CHECK},
                )

    def test_a_missing_key_is_refused_for_every_key(self):
        for key in EXTERNAL_SEND_DETAILS_KEYS:
            with self.subTest(missing=key):
                self.refused(without(valid_details(), key), project=uuid.uuid4())
        for label in EXTERNAL_SEND_WITHHELD_KEYS:
            with self.subTest(missing=f"withheld.{label}"):
                self.refused(
                    without(valid_details(), "withheld", label), project=uuid.uuid4()
                )

    def test_an_extra_key_is_refused_whatever_it_holds(self):
        # The point of the constraint: no place for a query, or anything else.
        for label, details in {
            "the query": valid_details(query=QUERY),
            "a note": valid_details(note="x"),
            "an empty key": {**valid_details(), "": 1},
            "an extra withheld key": nested(
                valid_details(), ("withheld", "text"), "private"
            ),
            "a nested object": valid_details(extra={"a": {"b": 1}}),
        }.items():
            with self.subTest(case=label):
                self.refused(details, project=uuid.uuid4())

    def test_the_fingerprint_must_be_sha256_and_64_lowercase_hex_digits(self):
        good = "a" * 64
        for label, value in {
            "the query itself": QUERY,
            "no prefix": good,
            "upper case": "sha256:" + good.upper(),
            "63 digits": "sha256:" + good[:-1],
            "65 digits": "sha256:" + good + "a",
            "a trailing newline": "sha256:" + good + "\n",
            "a leading space": " sha256:" + good,
            "a non-hex digit": "sha256:" + "g" * 64,
            "another algorithm": "sha1:" + good,
            "a number": 5,
            "null": None,
            "a list": ["sha256:" + good],
            "an object": {"sha256": good},
            "a SQL statement": "'); DROP TABLE audit_events; --",
        }.items():
            with self.subTest(fingerprint=label):
                self.refused(
                    valid_details(query_fingerprint=value), project=uuid.uuid4()
                )

    def test_the_counts_must_be_plain_non_negative_json_integers(self):
        bad = {
            "a string": "1",
            "negative": -1,
            "a fraction": 1.5,
            "a whole float": 1.0,
            "an exponent": 1e3,
            "a leading zero": "01",
            "eight digits": 10**7,
            "true": True,
            "null": None,
            "a list": [1],
        }
        for name in ("credentials_removed", "pieces_matched", "abstractions"):
            for label, value in bad.items():
                with self.subTest(field=name, value=label):
                    self.refused(valid_details(**{name: value}), project=uuid.uuid4())
        for name in EXTERNAL_SEND_WITHHELD_KEYS:
            for label, value in bad.items():
                with self.subTest(field=f"withheld.{name}", value=label):
                    self.refused(
                        nested(valid_details(), ("withheld", name), value),
                        project=uuid.uuid4(),
                    )

    def test_the_query_length_must_be_between_1_and_the_maximum(self):
        for label, value in {
            "zero": 0,
            "negative": -5,
            "one above the maximum": MAX_MINIMIZED_QUERY_CHARS + 1,
            "300": 300,
            "999": 999,
            "1000": 1000,
            "a string": "14",
            "a fraction": 14.5,
            "null": None,
            "true": True,
        }.items():
            with self.subTest(query_chars=label):
                self.refused(valid_details(query_chars=value), project=uuid.uuid4())

    def test_every_count_is_bounded_as_the_code_bounds_it(self):
        # A number can carry data as well (nine digits are 30 bits): every count is
        # limited to what the record allows, not to what fits a column.
        for name, limit in (
            ("credentials_removed", MAX_COUNT),
            ("pieces_matched", MAX_CONTEXT_PIECES),
            ("abstractions", MAX_COUNT),
        ):
            for value in (limit + 1, limit * 10, 9_999_999):
                with self.subTest(field=name, value=value):
                    self.refused(valid_details(**{name: value}), project=uuid.uuid4())
        for label in EXTERNAL_SEND_WITHHELD_KEYS:
            for value in (MAX_CONTEXT_PIECES + 1, 100, 9_999_999):
                with self.subTest(field=f"withheld.{label}", value=value):
                    self.refused(
                        nested(valid_details(), ("withheld", label), value),
                        project=uuid.uuid4(),
                    )

    def test_the_provider_kinds_are_only_the_registered_values(self):
        # The point of the constraint: a token that is not a provider kind (a
        # credential, a fragment of a query) has no place in the row.
        for label, value in {
            "a credential-like token": ["secret_token_abc123"],
            "a GitHub token prefix": ["ghp_abc123"],
            "a lower case word": ["billing"],
            "a longer token that starts like a kind": ["web_search"],
            "a kind with a suffix": ["web2"],
            "a kind with a missing letter": ["we"],
            "a kind in upper case": ["Web"],
            "a valid kind and a token": ["web", "secret_token_abc123"],
            "a token and a valid kind": ["secret_token_abc123", "docs"],
            "a token of 32 characters": ["k" * 32],
            "a sentence, i.e. a query": ["how to retry payments in stripe"],
            "a space": ["web docs"],
            "a dot": ["web.search"],
            "an empty token": [""],
            "a leading digit": ["1web"],
            "a number": [1],
            "null": [None],
            "a nested list": [["web"]],
            "a string": "web",
            "null value": None,
            "an object": {"web": True},
            "a quote": ['we"b'],
            "a newline": ["web\n"],
            "non-ascii": ["w\u00e9b"],
            "empty": [],
            "one more than there are kinds": ["web"] * (len(ProviderKind) + 1),
            "nine": ["web"] * 9,
        }.items():
            with self.subTest(provider_kinds=label):
                self.refused(valid_details(provider_kinds=value), project=uuid.uuid4())

    def test_every_provider_kind_of_the_code_is_accepted_by_the_database(self):
        # Fails when a kind is added to ``ProviderKind`` without a migration that
        # registers it: every send to that kind would be refused.
        for kind in ProviderKind:
            with self.subTest(kind=kind.value):
                self.insert(
                    valid_details(provider_kinds=[kind.value]), project=uuid.uuid4()
                )
        self.insert(
            valid_details(provider_kinds=[kind.value for kind in ProviderKind]),
            project=uuid.uuid4(),
        )

    def test_truncated_is_a_boolean_and_withheld_an_object(self):
        for label, value in {
            "a string": "false",
            "zero": 0,
            "one": 1,
            "null": None,
        }.items():
            with self.subTest(truncated=label):
                self.refused(valid_details(truncated=value), project=uuid.uuid4())
        for label, value in {
            "a list": [0, 0, 0, 0],
            "a string": "none",
            "null": None,
            "a number": 0,
        }.items():
            with self.subTest(withheld=label):
                self.refused(valid_details(withheld=value), project=uuid.uuid4())

    def test_a_key_of_another_case_or_spelling_is_not_the_key(self):
        for key in ("Query_Fingerprint", "query-fingerprint", "queryFingerprint"):
            with self.subTest(key=key):
                details = valid_details()
                details[key] = details.pop("query_fingerprint")
                self.refused(details, project=uuid.uuid4())


# Every kind of action name: the ones that exist (authorization capabilities, tools,
# owner setup, shared memory), the one of this issue spelled a little differently, and
# invented ones. None of them has a registered schema for ``details``.
UNREGISTERED_ACTIONS = (
    "chat.use",
    "project.read",
    "project.repo.write",
    "shared_memory.delete",
    "tool.repo_read",
    "tool.unknown",
    "owner.token.redeem",
    "unknown",
    "research.something_else",
    "research.external_send2",
    "research.external_sen",
    "Research.External_Send",
    "RESEARCH.EXTERNAL_SEND",
    " research.external_send",
    "research.external_send ",
    "research.external_send\n",
    "research_external_send",
    "x",
)
HOSTILE_DETAILS = {
    "a query": {"query": "the billing service retries payment three times"},
    "private text under a schema key": valid_details(
        query_fingerprint="the billing service retries payment"
    ),
    "a note": {"note": "x"},
    "an empty object": {},
    "the valid shape of the registered action": valid_details(),
    "nested text": {"a": {"b": ["c", {"d": "private"}]}},
}


class UnregisteredActionTest(DatabaseTestCase):
    """``details`` is allowed only for registered actions (today: one).

    An action without a registered closed schema has no ``details``: a writer with
    INSERT (a bug, or an application that was taken over) cannot keep text in an
    existing or an invented action's row, whatever the object looks like.
    """

    def insert_other(self, action, details, **kwargs):
        self.insert(
            details, action=action, decision="allow", reason="x", raw=False, **kwargs
        )

    def test_details_is_refused_for_every_action_that_is_not_registered(self):
        for action in UNREGISTERED_ACTIONS:
            for label, details in HOSTILE_DETAILS.items():
                with self.subTest(action=action, details=label):
                    with self.assertRaises(IntegrityError) as caught:
                        self.insert_other(action, details, project=uuid.uuid4())
                    self.assertEqual(
                        caught.exception.orig.diag.constraint_name, REGISTERED_CHECK
                    )

    def test_a_json_value_that_is_not_an_object_is_refused_for_those_actions_too(self):
        for action in ("chat.use", "research.something_else"):
            for label, raw in {
                "json null": "null",
                "an array": "[]",
                "a string": '"text"',
                "a number": "5",
                "true": "true",
            }.items():
                with self.subTest(action=action, details=label):
                    with self.assertRaises(IntegrityError) as caught:
                        self.insert(
                            raw,
                            action=action,
                            decision="allow",
                            reason="x",
                            raw=True,
                        )
                    self.assertEqual(
                        caught.exception.orig.diag.constraint_name, REGISTERED_CHECK
                    )

    def test_every_such_action_is_accepted_without_details(self):
        # The column is NULL for every other row of the audit trail, as before.
        for action in UNREGISTERED_ACTIONS:
            with self.subTest(action=action):
                self.insert_other(action, None)

    def test_a_registered_action_needs_its_details_and_no_other_action_may_share_them(
        self,
    ):
        project = uuid.uuid4()
        self.insert(valid_details(), project=project)  # the registered action
        self.refused(None, project=project)  # ... always with its details
        with self.assertRaises(IntegrityError):
            self.insert_other("research.external_send2", valid_details())

    def test_the_registry_is_exactly_the_one_action_of_this_issue(self):
        self.assertEqual(DETAILS_ACTIONS, (EXTERNAL_SEND_ACTION,))
        self.assertEqual(migration_module()._ACTIONS, DETAILS_ACTIONS)
        definition = self.scalar(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = :n AND conrelid = 'audit_events'::regclass",
            n=REGISTERED_CHECK,
        )
        self.assertEqual(definition.count("research.external_send"), 1)
        for action in UNREGISTERED_ACTIONS:
            self.assertNotEqual(action, EXTERNAL_SEND_ACTION)

    def test_every_registered_action_has_a_closed_schema_constraint_of_its_own(self):
        # Registering an action without its own schema would allow any small object
        # for it: each action of the registry must be named by exactly one CHECK
        # other than the two general ones (the decision list and the registry).
        rows = self.rows(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'audit_events'::regclass AND contype = 'c' "
            "AND conname NOT IN (:decision, :registry)",
            decision="ck_audit_events_decision_valid",
            registry=REGISTERED_CHECK,
        )
        self.assertEqual(len(rows), len(DETAILS_ACTIONS))
        for action in DETAILS_ACTIONS:
            with self.subTest(action=action):
                named = [
                    name for name, definition in rows if f"'{action}'" in definition
                ]
                self.assertEqual(named, [SHAPE_CHECK])

    def test_a_registered_action_may_not_exceed_the_size_limit_or_be_a_non_object(self):
        project = uuid.uuid4()
        # Small and wrong: the closed schema of the action refuses it.
        self.refused(valid_details(note="x"), project=project)
        # Large: the general limit is what refuses it (checked first, by name).
        self.refused(
            valid_details(note="x" * MAX_DETAILS_BYTES),
            constraint=REGISTERED_CHECK,
            project=project,
        )
        for label, raw in {
            "an array": "[]",
            "a string": '"text"',
            "json null": "null",
        }.items():
            with self.subTest(details=label):
                with self.assertRaises(IntegrityError) as caught:
                    self.insert(raw, project=project, raw=True)
                self.assertEqual(
                    caught.exception.orig.diag.constraint_name, REGISTERED_CHECK
                )

    def test_the_size_limit_counts_bytes_not_characters(self):
        project = uuid.uuid4()
        base = len(json.dumps(valid_details(note="")).encode())
        self.assertLess(base, MAX_DETAILS_BYTES)
        # A 3-byte character: 679 fit under the limit as bytes, 680 do not, though
        # 689 characters would fit if characters were counted.
        exact = MAX_DETAILS_BYTES - base
        note = "\u3042" * (exact // 3 + 1)
        self.assertGreater(len(note.encode()), exact)
        self.assertLess(len(note), exact)  # fewer characters than the byte budget
        self.refused(
            valid_details(note=note), constraint=REGISTERED_CHECK, project=project
        )


if __name__ == "__main__":
    unittest.main()
