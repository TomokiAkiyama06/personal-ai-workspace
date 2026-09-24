import io
import unittest
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from .support import paw_environment

BACKEND_DIR = Path(__file__).resolve().parents[1]
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"
PASSWORD = "s3cr3t-pw"


def offline_config(output: io.StringIO) -> Config:
    """Alembic config for offline runs.

    Built without the ini file's path so that ``env.py`` does not reconfigure
    the logging of the whole test process; the script location still comes
    from ``alembic.ini``.
    """
    ini = Config(str(ALEMBIC_INI))
    config = Config(output_buffer=output)
    config.set_main_option("script_location", ini.get_main_option("script_location"))
    return config


class MigrationConfigTest(unittest.TestCase):
    def test_alembic_ini_points_at_the_migrations_directory(self):
        config = Config(str(ALEMBIC_INI))
        script_location = Path(config.get_main_option("script_location"))
        self.assertEqual(script_location.resolve(), BACKEND_DIR / "migrations")
        self.assertTrue((script_location / "env.py").is_file())

    def test_alembic_ini_holds_no_database_url(self):
        self.assertIsNone(Config(str(ALEMBIC_INI)).get_main_option("sqlalchemy.url"))
        self.assertNotIn("postgres", ALEMBIC_INI.read_text().lower())

    def test_history_starts_at_the_baseline_and_has_one_head(self):
        # Later migrations extend the chain; the tests must not name the head.
        scripts = ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))
        self.assertEqual(scripts.get_bases(), ["0001"])
        self.assertEqual(len(scripts.get_heads()), 1)


class MigrationEnvironmentTest(unittest.TestCase):
    def test_offline_upgrade_renders_sql_using_the_url_from_the_environment(self):
        output = io.StringIO()
        url = f"postgresql://paw:{PASSWORD}@db.internal/paw"
        with paw_environment(PAW_DATABASE_URL=url):
            # The baseline only, so that later revisions do not change this test.
            command.upgrade(offline_config(output), "0001", sql=True)

        sql = output.getvalue()
        self.assertIn("CREATE TABLE alembic_version", sql)
        self.assertIn("INSERT INTO alembic_version (version_num) VALUES ('0001')", sql)
        self.assertNotIn(PASSWORD, sql)

    def test_missing_database_url_stops_with_a_clear_message(self):
        with paw_environment():
            with self.assertRaises(SystemExit) as caught:
                command.upgrade(offline_config(io.StringIO()), "head", sql=True)
        self.assertIn("PAW_DATABASE_URL", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
