import unittest

from pydantic import ValidationError

from paw_backend.config import Settings

from .support import make_settings, paw_environment

PASSWORD = "s3cr3t-pw"


def settings_from_env(**variables: str) -> Settings:
    """Parse Settings from exactly ``variables`` as the process environment."""
    with paw_environment(**variables):
        return Settings()


class SettingsTest(unittest.TestCase):
    def test_defaults_are_safe_and_secret_free(self):
        settings = settings_from_env()
        self.assertEqual(settings.host, "127.0.0.1")
        self.assertEqual(settings.port, 8000)
        self.assertIsNone(settings.database_url)
        self.assertFalse(settings.tls_enabled)
        self.assertFalse(settings.allow_plaintext_http)
        self.assertTrue(settings.binds_loopback_only)

    def test_reads_prefixed_environment_variables(self):
        settings = settings_from_env(
            PAW_HOST="0.0.0.0",
            PAW_PORT="9443",
            PAW_TLS_CERTFILE="/etc/paw/tls.crt",
            PAW_TLS_KEYFILE="/etc/paw/tls.key",
            PAW_EVENT_HEARTBEAT_SECONDS="2.5",
            PAW_LOG_LEVEL="DEBUG",
        )
        self.assertEqual(settings.host, "0.0.0.0")
        self.assertEqual(settings.port, 9443)
        self.assertTrue(settings.tls_enabled)
        self.assertEqual(settings.event_heartbeat_seconds, 2.5)
        self.assertEqual(settings.log_level, "debug")

    def test_ignores_unprefixed_variables(self):
        settings = settings_from_env(
            PORT="1", HOST="example.org", DATABASE_URL="postgresql://u:p@h/d"
        )
        self.assertEqual(settings.port, 8000)
        self.assertEqual(settings.host, "127.0.0.1")
        self.assertIsNone(settings.database_url)

    def test_database_url_selects_the_psycopg_driver(self):
        for scheme in ("postgresql", "postgresql+psycopg"):
            with self.subTest(scheme=scheme):
                settings = settings_from_env(
                    PAW_DATABASE_URL=f"{scheme}://paw:{PASSWORD}@db.internal:5432/paw"
                )
                self.assertEqual(
                    settings.database_url.get_secret_value(),
                    f"postgresql+psycopg://paw:{PASSWORD}@db.internal:5432/paw",
                )

    def test_empty_optional_values_mean_unset(self):
        settings = settings_from_env(PAW_DATABASE_URL="", PAW_TLS_CERTFILE="")
        self.assertIsNone(settings.database_url)
        self.assertFalse(settings.tls_enabled)

    def test_database_url_is_not_exposed_by_repr_or_dump(self):
        settings = make_settings(
            database_url=f"postgresql://paw:{PASSWORD}@db.internal/paw"
        )
        self.assertNotIn(PASSWORD, repr(settings))
        self.assertNotIn(PASSWORD, str(settings))
        self.assertNotIn(PASSWORD, str(settings.model_dump()))
        self.assertNotIn(PASSWORD, settings.model_dump_json())

    def test_invalid_database_url_error_does_not_echo_the_value(self):
        for value in (
            f"mysql://paw:{PASSWORD}@db.internal/paw",
            f"postgresql+asyncpg://paw:{PASSWORD}@db.internal/paw",
            f"not a url {PASSWORD}",
        ):
            with self.subTest(value=value.split(":")[0]):
                with self.assertRaises(ValidationError) as caught:
                    settings_from_env(PAW_DATABASE_URL=value)
                self.assertIn("database_url", str(caught.exception))
                self.assertNotIn(PASSWORD, str(caught.exception))

    def test_tls_files_must_be_configured_together(self):
        for variables in (
            {"PAW_TLS_CERTFILE": "/etc/paw/tls.crt"},
            {"PAW_TLS_KEYFILE": "/etc/paw/tls.key"},
        ):
            with self.subTest(variables=variables):
                with self.assertRaises(ValidationError):
                    settings_from_env(**variables)

    def test_rejects_out_of_range_values(self):
        for name, value in (
            ("PAW_PORT", "0"),
            ("PAW_PORT", "70000"),
            ("PAW_EVENT_HEARTBEAT_SECONDS", "0"),
            ("PAW_DATABASE_TIMEOUT_SECONDS", "-1"),
            ("PAW_HSTS_MAX_AGE_SECONDS", "-1"),
            ("PAW_LOG_LEVEL", "verbose"),
        ):
            with self.subTest(name=name, value=value):
                with self.assertRaises(ValidationError):
                    settings_from_env(**{name: value})

    def test_loopback_detection(self):
        for host, loopback in (
            ("127.0.0.1", True),
            ("::1", True),
            ("localhost", True),
            ("0.0.0.0", False),
            ("::", False),
            ("192.168.1.10", False),
            ("paw.example.org", False),
        ):
            with self.subTest(host=host):
                self.assertEqual(make_settings(host=host).binds_loopback_only, loopback)

    def test_new_defaults_are_restrictive(self):
        settings = settings_from_env()
        self.assertEqual(settings.allowed_hosts, ["localhost", "127.0.0.1", "[::1]"])
        self.assertEqual(settings.allowed_origins, [])
        self.assertEqual(settings.event_max_subscribers, 100)
        self.assertEqual(settings.shutdown_timeout_seconds, 5)

    def test_hosts_and_origins_are_comma_separated_and_normalized(self):
        settings = settings_from_env(
            PAW_ALLOWED_HOSTS="Workspace.Example.org, localhost,[::1]",
            PAW_ALLOWED_ORIGINS="https://App.example.org:443,http://localhost:5173/",
        )
        self.assertEqual(
            settings.allowed_hosts, ["workspace.example.org", "localhost", "[::1]"]
        )
        self.assertEqual(
            settings.allowed_origins,
            ["https://app.example.org", "http://localhost:5173"],
        )

    def test_invalid_hosts_and_origins_are_rejected(self):
        for name, value in (
            ("PAW_ALLOWED_HOSTS", ""),
            ("PAW_ALLOWED_HOSTS", "*"),
            ("PAW_ALLOWED_HOSTS", "https://workspace.example.org"),
            ("PAW_ALLOWED_HOSTS", "workspace.example.org:443"),
            ("PAW_ALLOWED_HOSTS", "workspace.example.org/path"),
            ("PAW_ALLOWED_ORIGINS", "workspace.example.org"),
            ("PAW_ALLOWED_ORIGINS", "https://a.example.org/x"),
            ("PAW_ALLOWED_ORIGINS", "null"),
        ):
            with self.subTest(name=name, value=value):
                with self.assertRaises(ValidationError):
                    settings_from_env(**{name: value})

    def test_rejects_out_of_range_limits(self):
        for name, value in (
            ("PAW_EVENT_MAX_SUBSCRIBERS", "0"),
            ("PAW_SHUTDOWN_TIMEOUT_SECONDS", "0"),
            ("PAW_SHUTDOWN_TIMEOUT_SECONDS", "1000"),
        ):
            with self.subTest(name=name, value=value):
                with self.assertRaises(ValidationError):
                    settings_from_env(**{name: value})

    def test_settings_are_immutable(self):
        with self.assertRaises(ValidationError):
            make_settings().port = 1


if __name__ == "__main__":
    unittest.main()
