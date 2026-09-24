"""The two settings of the Owner setup tokens and the bounds the service checks."""

import unittest

from pydantic import ValidationError

from paw_backend.config import Settings
from paw_backend.identity import limits

from .support import make_settings, paw_environment


class SettingsTest(unittest.TestCase):
    def test_defaults_are_a_short_ttl_and_five_attempts(self):
        settings = make_settings()

        self.assertEqual(settings.setup_token_ttl_seconds, 1800)
        self.assertEqual(settings.setup_token_max_attempts, 5)
        self.assertEqual(limits.DEFAULT_TTL_SECONDS, 1800)
        self.assertEqual(limits.DEFAULT_MAX_ATTEMPTS, 5)

    def test_values_come_from_the_environment(self):
        with paw_environment(
            PAW_SETUP_TOKEN_TTL_SECONDS="600", PAW_SETUP_TOKEN_MAX_ATTEMPTS="3"
        ):
            settings = Settings()

        self.assertEqual(settings.setup_token_ttl_seconds, 600)
        self.assertEqual(settings.setup_token_max_attempts, 3)

    def test_the_bounds_of_the_settings_match_the_bounds_of_the_service(self):
        for name, low, high in (
            (
                "setup_token_ttl_seconds",
                limits.MIN_TTL_SECONDS,
                limits.MAX_TTL_SECONDS,
            ),
            (
                "setup_token_max_attempts",
                limits.MIN_MAX_ATTEMPTS,
                limits.MAX_MAX_ATTEMPTS,
            ),
        ):
            with self.subTest(name):
                make_settings(**{name: low})
                make_settings(**{name: high})
                for outside in (low - 1, high + 1):
                    with self.assertRaises(ValidationError):
                        make_settings(**{name: outside})

    def test_the_documented_bounds(self):
        self.assertEqual((limits.MIN_TTL_SECONDS, limits.MAX_TTL_SECONDS), (60, 14400))
        self.assertEqual((limits.MIN_MAX_ATTEMPTS, limits.MAX_MAX_ATTEMPTS), (1, 20))

    def test_a_bad_value_is_not_echoed_in_the_error(self):
        with paw_environment(PAW_SETUP_TOKEN_TTL_SECONDS="hunter2-not-a-number"):
            with self.assertRaises(ValidationError) as caught:
                Settings()

        self.assertNotIn("hunter2", str(caught.exception))


class OperatorSettingsTest(unittest.TestCase):
    def test_the_operator_url_and_role_are_optional(self):
        settings = make_settings()

        self.assertIsNone(settings.operator_database_url)
        self.assertIsNone(settings.operator_database_role)

    def test_the_operator_url_is_normalised_like_the_database_url(self):
        settings = make_settings(operator_database_url="postgresql://op:pw@h/db")

        self.assertEqual(
            settings.operator_database_url.get_secret_value(),
            "postgresql+psycopg://op:pw@h/db",
        )

    def test_an_empty_value_means_unset(self):
        with paw_environment(
            PAW_OPERATOR_DATABASE_URL="", PAW_OPERATOR_DATABASE_ROLE=""
        ):
            settings = Settings()

        self.assertIsNone(settings.operator_database_url)
        self.assertIsNone(settings.operator_database_role)

    def test_a_bad_operator_url_is_rejected_without_echoing_it(self):
        for value in ("mysql://u:hunter2-pw@h/d", "not a url hunter2-pw"):
            with self.subTest(value):
                with paw_environment(PAW_OPERATOR_DATABASE_URL=value):
                    with self.assertRaises(ValidationError) as caught:
                        Settings()
                self.assertNotIn("hunter2", str(caught.exception))

    def test_the_operator_role_is_validated_like_the_application_role(self):
        for name in ("public", "pg_write_all_data", "postgres", 'x"; DROP', "a b"):
            with self.subTest(name):
                with self.assertRaises(ValidationError):
                    make_settings(operator_database_role=name)
        self.assertEqual(
            make_settings(operator_database_role="paw_operator").operator_database_role,
            "paw_operator",
        )


if __name__ == "__main__":
    unittest.main()
