"""Revision 0154: the approved pairing's one Passkey registration (issue #154).

The first classes need no server: the model repeats the migration's rule, and the
rendered SQL adds the column, its constraint and the application role's UPDATE
privilege on it (and the downgrade removes both). The PostgreSQL class (skipped
unless ``PAW_TEST_DATABASE_URL`` is set) runs the revision up and down and checks
the constraint: only a ``completed`` pairing with ``approval_required`` can have
its allowance ended. Nothing here assumes 0154 is the head: the previous revision
is read from the script directory. The drift between the models and the head is
checked by ``test_onboarding_migration`` at the head, which includes this revision.
"""

import io
import unittest
import uuid

from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from paw_backend.auth.onboarding.models import DevicePairingRow

from .memory_support import migrate, requires_postgres, sync_database_url
from .support import paw_environment
from .test_migrations import offline_config

REVISION = "0154"
COLUMN = "passkey_allowance_ended_at"
CONSTRAINT = "ck_device_pairings_allowance_needs_approval"
RULE = f"{COLUMN} IS NULL OR (state = 'completed' AND approval_required)"
CHECK_VIOLATION = "23514"


def previous_revision() -> str:
    scripts = ScriptDirectory.from_config(offline_config(io.StringIO()))
    previous = scripts.get_revision(REVISION).down_revision
    assert isinstance(previous, str)
    return previous


class ModelTest(unittest.TestCase):
    def test_the_model_declares_the_column_and_the_constraint(self):
        table = DevicePairingRow.__table__
        self.assertTrue(table.c[COLUMN].nullable)
        checks = {
            constraint.name: str(constraint.sqltext)
            for constraint in table.constraints
            if constraint.name and constraint.name.startswith("ck_")
        }
        self.assertEqual(checks[CONSTRAINT], RULE)

    def test_the_revision_follows_the_pairing_revision(self):
        scripts = ScriptDirectory.from_config(offline_config(io.StringIO()))
        ancestors = {
            script.revision
            for script in scripts.iterate_revisions(previous_revision(), "base")
        }
        self.assertIn("0124", ancestors)


class OfflineMigrationTest(unittest.TestCase):
    def sql(self, action: str, revisions: str, **environment: str) -> str:
        output = io.StringIO()
        with paw_environment(
            PAW_DATABASE_URL="postgresql://u:p@db.invalid/paw", **environment
        ):
            getattr(command, action)(offline_config(output), revisions, sql=True)
        return output.getvalue()

    def test_upgrade_adds_the_column_the_rule_and_the_update_privilege(self):
        sql = self.sql(
            "upgrade",
            f"{previous_revision()}:{REVISION}",
            PAW_APP_DATABASE_ROLE="paw_app",
        )
        self.assertIn(
            f"ALTER TABLE device_pairings ADD COLUMN {COLUMN} TIMESTAMP WITH TIME ZONE",
            sql,
        )
        self.assertIn(
            f"ALTER TABLE device_pairings ADD CONSTRAINT {CONSTRAINT} CHECK ({RULE})",
            sql,
        )
        self.assertIn(f'GRANT UPDATE ({COLUMN}) ON device_pairings TO "paw_app"', sql)
        for forbidden in ("GRANT DELETE", "GRANT INSERT", "GRANT ALL", "TRUNCATE"):
            with self.subTest(forbidden):
                self.assertNotIn(forbidden, sql)

    def test_without_a_role_nothing_is_granted(self):
        sql = self.sql("upgrade", f"{previous_revision()}:{REVISION}")
        self.assertNotIn("GRANT", sql)

    def test_downgrade_drops_the_constraint_and_the_column(self):
        sql = self.sql("downgrade", f"{REVISION}:{previous_revision()}")
        self.assertIn(f"ALTER TABLE device_pairings DROP CONSTRAINT {CONSTRAINT}", sql)
        self.assertIn(f"ALTER TABLE device_pairings DROP COLUMN {COLUMN}", sql)
        self.assertNotIn("DROP TABLE", sql)


@requires_postgres
class DatabaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_engine(sync_database_url())

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()
        migrate("downgrade", "base")

    def setUp(self) -> None:
        migrate("downgrade", "base")
        migrate("upgrade", REVISION)

    def run_sql(self, sql: str, **params) -> None:
        with self.engine.begin() as connection:
            connection.execute(text(sql), params)

    def refused(self, sql: str, **params) -> str:
        with self.assertRaises(DBAPIError) as caught:
            self.run_sql(sql, **params)
        return caught.exception.orig.sqlstate

    def column_exists(self) -> bool:
        with self.engine.connect() as connection:
            return bool(
                connection.execute(
                    text(
                        "SELECT count(*) FROM information_schema.columns "
                        "WHERE table_name = 'device_pairings' AND column_name = :c"
                    ),
                    {"c": COLUMN},
                ).scalar_one()
            )

    def pairing(self, *, state: str, approval: bool) -> uuid.UUID:
        """A pairing row of a fresh admin in ``state`` (claimed / completed)."""
        user = uuid.uuid4()
        self.run_sql(
            "INSERT INTO users (id, login_name, system_role, status, "
            "passkey_required, created_at, updated_at) VALUES (:u, :n, 'admin', "
            "'active', true, now(), now())",
            u=user,
            n=f"admin-{user.hex[:8]}",
        )
        pairing = uuid.uuid4()
        claimed = state != "issued"
        self.run_sql(
            "INSERT INTO device_pairings (id, audit_ref, user_id, state, "
            "approval_required, salt, secret_hash, claim_id, claim_salt, claim_hash, "
            "confirm_salt, confirm_hash, created_at, expires_at, claimed_at, "
            "decided_at, completed_at, attempts) VALUES (:id, gen_random_uuid(), :u, "
            ":state, :approval, :salt, :hash, "
            "CASE WHEN :claim THEN gen_random_uuid() END, "
            "CASE WHEN :claim THEN :salt END, CASE WHEN :claim THEN :hash END, "
            "CASE WHEN :claim THEN :salt END, CASE WHEN :claim THEN :hash END, "
            "now(), now() + interval '10 minutes', "
            "CASE WHEN :claim THEN now() END, CASE WHEN :claim THEN now() END, "
            "CASE WHEN :state = 'completed' THEN now() END, 0)",
            id=pairing,
            u=user,
            state=state,
            approval=approval,
            salt=b"s" * 16,
            hash=b"h" * 32,
            claim=claimed and approval,
        )
        return pairing

    def end_allowance(self, pairing: uuid.UUID) -> None:
        self.run_sql(
            f"UPDATE device_pairings SET {COLUMN} = now() WHERE id = :id", id=pairing
        )

    def test_an_approved_completed_pairing_can_end_its_allowance(self):
        self.end_allowance(self.pairing(state="completed", approval=True))

    def test_a_users_completed_pairing_has_no_allowance(self):
        pairing = self.pairing(state="completed", approval=False)
        self.assertEqual(
            self.refused(
                f"UPDATE device_pairings SET {COLUMN} = now() WHERE id = :id",
                id=pairing,
            ),
            CHECK_VIOLATION,
        )

    def test_a_pairing_that_did_not_complete_has_no_allowance(self):
        for state in ("issued", "claimed"):
            with self.subTest(state):
                pairing = self.pairing(state=state, approval=state == "claimed")
                self.assertEqual(
                    self.refused(
                        f"UPDATE device_pairings SET {COLUMN} = now() WHERE id = :id",
                        id=pairing,
                    ),
                    CHECK_VIOLATION,
                )

    def test_the_downgrade_drops_the_column(self):
        self.end_allowance(self.pairing(state="completed", approval=True))
        migrate("downgrade", previous_revision())
        self.assertFalse(self.column_exists())
        migrate("upgrade", REVISION)
        self.assertTrue(self.column_exists())
