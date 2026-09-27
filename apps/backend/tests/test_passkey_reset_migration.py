"""Migration 0108: the reset of another account's Passkeys and password (#108).

The first class needs no server. The PostgreSQL class runs the migration up, down
and up again, checks the constraints on both sides of the revision, what the
downgrade removes and what it leaves, and that the offline SQL renders. The drift
between the models and the head (autogenerate and the catalog) is checked by
``test_passkey_migration`` and ``test_identity_migration`` at the head, which
includes this revision.
"""

import io
import unittest
import uuid
from pathlib import Path

from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from paw_backend.auth.passkeys.models import PasskeyRevokeReason
from paw_backend.identity.models import TokenPurpose

from .memory_support import migrate, requires_postgres, sync_database_url
from .support import paw_environment
from .test_migrations import offline_config

REVISION = "0108"
VERSIONS = Path(__file__).resolve().parents[1] / "migrations" / "versions"
SOURCE = VERSIONS / "0108_passkey_owner_reset.py"
CHECK_VIOLATION = "23514"


def previous_revision() -> str:
    scripts = ScriptDirectory.from_config(offline_config(io.StringIO()))
    previous = scripts.get_revision(REVISION).down_revision
    assert isinstance(previous, str)
    return previous


class ModelsTest(unittest.TestCase):
    def test_the_revision_follows_the_passkey_and_identity_revisions(self):
        scripts = ScriptDirectory.from_config(offline_config(io.StringIO()))
        ancestors = {
            script.revision
            for script in scripts.iterate_revisions(previous_revision(), "base")
        }
        self.assertTrue({"0021", "0022", "0023"} <= ancestors)

    def test_the_migration_repeats_the_new_values_of_the_code(self):
        source = SOURCE.read_text()
        self.assertIn(PasskeyRevokeReason.ADMIN_RESET.value, source)
        self.assertIn(TokenPurpose.PASSWORD_RESET.value, source)
        for value in PasskeyRevokeReason:
            self.assertIn(f"'{value.value}'", source)
        for value in TokenPurpose:
            self.assertIn(f"'{value.value}'", source)

    def test_the_function_pins_its_search_path_and_refuses_the_owner(self):
        source = SOURCE.read_text()
        self.assertIn("SECURITY DEFINER SET search_path = pg_catalog, pg_temp", source)
        self.assertIn("v_role NOT IN ('admin', 'user')", source)
        self.assertIn("REVOKE ALL ON FUNCTION", source)


@requires_postgres
class DatabaseTest(unittest.TestCase):
    def setUp(self) -> None:
        migrate("downgrade", "base")
        self.addCleanup(migrate, "downgrade", "base")
        self.engine = create_engine(sync_database_url())
        self.addCleanup(self.engine.dispose)
        self.previous = previous_revision()

    def scalars(self, sql: str, **params) -> list:
        with self.engine.connect() as connection:
            return list(connection.execute(text(sql), params).scalars())

    def run_sql(self, sql: str, **params) -> None:
        with self.engine.begin() as connection:
            connection.execute(text(sql), params)

    def refused(self, sql: str, **params) -> str:
        with self.assertRaises(DBAPIError) as caught:
            self.run_sql(sql, **params)
        return caught.exception.orig.sqlstate

    def function_exists(self) -> bool:
        return bool(
            self.scalars(
                "SELECT count(*) FROM pg_proc "
                "WHERE proname = 'paw_issue_password_reset_token'"
            )[0]
        )

    def insert_user(self, role: str = "admin") -> uuid.UUID:
        user = uuid.uuid4()
        self.run_sql(
            "INSERT INTO users (id, login_name, system_role, status, "
            "passkey_required, created_at, updated_at) VALUES (:u, :n, :r, 'active', "
            ":p, now(), now())",
            u=user,
            n=f"user-{user.hex[:8]}",
            r=role,
            p=role != "user",
        )
        self.run_sql(
            "INSERT INTO password_credentials (user_id, hash, created_at, changed_at) "
            "VALUES (:u, '$argon2id$placeholder', now(), now())",
            u=user,
        )
        return user

    def insert_passkey(self, user: uuid.UUID, reason: str | None) -> None:
        self.run_sql(
            "INSERT INTO user_passkeys (id, user_id, credential_id, public_key, "
            "sign_count, name, backup_eligible, backed_up, created_at, revoked_at, "
            "revoked_reason) VALUES (gen_random_uuid(), :u, :c, :k, 0, 'phone', "
            "false, false, now(), CASE WHEN CAST(:r AS text) IS NULL THEN NULL "
            "ELSE now() END, :r)",
            u=user,
            c=uuid.uuid4().bytes * 2,
            k=b"k" * 16,
            r=reason,
        )

    def issue(self, user: uuid.UUID) -> bool:
        with self.engine.begin() as connection:
            return connection.execute(
                text(
                    "SELECT paw_issue_password_reset_token(:u, gen_random_uuid(), "
                    "gen_random_uuid(), :s, :h, now(), now() + interval '1 hour')"
                ),
                {"u": user, "s": b"s" * 16, "h": b"h" * 32},
            ).scalar()

    def test_upgrade_adds_and_downgrade_removes(self):
        migrate("upgrade", self.previous)
        self.assertFalse(self.function_exists())
        user = self.insert_user()
        self.assertEqual(
            self.refused(
                "INSERT INTO user_passkeys (id, user_id, credential_id, public_key, "
                "sign_count, name, backup_eligible, backed_up, created_at, "
                "revoked_at, revoked_reason) VALUES (gen_random_uuid(), :u, :c, :k, "
                "0, 'x', false, false, now(), now(), 'admin_reset')",
                u=user,
                c=b"c" * 16,
                k=b"k" * 16,
            ),
            CHECK_VIOLATION,
        )

        migrate("upgrade", REVISION)
        self.assertTrue(self.function_exists())
        self.insert_passkey(user, "admin_reset")
        self.assertIs(self.issue(user), True)
        self.assertEqual(
            self.scalars("SELECT purpose FROM setup_tokens WHERE user_id = :u", u=user),
            ["password_reset"],
        )
        # The function deleted the password with it.
        self.assertEqual(
            self.scalars(
                "SELECT count(*) FROM password_credentials WHERE user_id = :u", u=user
            ),
            [0],
        )

        migrate("downgrade", self.previous)
        self.assertFalse(self.function_exists())
        self.assertEqual(self.scalars("SELECT count(*) FROM setup_tokens"), [0])
        self.assertEqual(
            self.scalars("SELECT revoked_reason FROM user_passkeys"), ["recovery"]
        )
        self.assertEqual(self.scalars("SELECT count(*) FROM users"), [1])
        self.assertEqual(
            self.refused(
                "UPDATE user_passkeys SET revoked_reason = 'admin_reset' "
                "WHERE user_id = :u",
                u=user,
            ),
            CHECK_VIOLATION,
        )

    def test_the_downgrade_keeps_the_owners_tokens_and_other_reasons(self):
        migrate("upgrade", REVISION)
        owner = self.insert_user("owner")
        self.run_sql(
            "INSERT INTO setup_tokens (id, audit_ref, user_id, purpose, salt, "
            "secret_hash, created_at, expires_at, attempts) VALUES "
            "(gen_random_uuid(), gen_random_uuid(), :u, 'recovery', :s, :h, now(), "
            "now() + interval '1 hour', 0)",
            u=owner,
            s=b"s" * 16,
            h=b"h" * 32,
        )
        self.insert_passkey(owner, "revoked_by_user")
        self.insert_passkey(owner, None)
        migrate("downgrade", self.previous)
        self.assertEqual(self.scalars("SELECT purpose FROM setup_tokens"), ["recovery"])
        self.assertEqual(
            sorted(
                self.scalars("SELECT coalesce(revoked_reason, '-') FROM user_passkeys")
            ),
            ["-", "revoked_by_user"],
        )

    def test_it_can_be_applied_again(self):
        migrate("upgrade", REVISION)
        migrate("downgrade", self.previous)
        migrate("upgrade", REVISION)
        self.assertTrue(self.function_exists())
        migrate("downgrade", "base")
        migrate("upgrade", "head")
        self.assertTrue(self.function_exists())

    def test_the_function_refuses_the_owner_under_the_migration_role_too(self):
        migrate("upgrade", REVISION)
        owner = self.insert_user("owner")
        self.assertIs(self.issue(owner), False)
        self.assertEqual(self.scalars("SELECT count(*) FROM setup_tokens"), [0])
        self.assertEqual(
            self.scalars(
                "SELECT count(*) FROM password_credentials WHERE user_id = :u", u=owner
            ),
            [1],
        )

    def test_the_function_revokes_the_outstanding_token(self):
        migrate("upgrade", REVISION)
        user = self.insert_user("user")
        self.assertIs(self.issue(user), True)
        self.assertIs(self.issue(user), True)
        self.assertEqual(
            self.scalars(
                "SELECT count(*) FROM setup_tokens WHERE revoked_at IS NULL "
                "AND used_at IS NULL"
            ),
            [1],
        )
        self.assertEqual(self.scalars("SELECT count(*) FROM setup_tokens"), [2])

    def test_the_offline_sql_of_the_migration_is_rendered(self):
        output = io.StringIO()
        with paw_environment(PAW_DATABASE_URL="postgresql://u:p@db.invalid/paw"):
            command.upgrade(
                offline_config(output), f"{self.previous}:{REVISION}", sql=True
            )
        sql = output.getvalue()
        for expected in (
            "CREATE FUNCTION paw_issue_password_reset_token",
            "public.setup_tokens",
            "ck_user_passkeys_revoked_reason_valid",
            "ck_setup_tokens_purpose_valid",
            "REVOKE ALL ON FUNCTION paw_issue_password_reset_token",
        ):
            self.assertIn(expected, sql)
