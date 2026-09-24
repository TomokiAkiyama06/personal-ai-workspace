"""The two settings of the Owner setup tokens and the bounds the service checks."""

import unittest

from pydantic import ValidationError

from paw_backend.config import Settings
from paw_backend.identity import service

from .support import make_settings, paw_environment


class SettingsTest(unittest.TestCase):
    def test_defaults_are_a_short_ttl_and_five_attempts(self):
        settings = make_settings()

        self.assertEqual(settings.setup_token_ttl_seconds, 1800)
        self.assertEqual(settings.setup_token_max_attempts, 5)
        self.assertEqual(service.DEFAULT_TTL_SECONDS, 1800)
        self.assertEqual(service.DEFAULT_MAX_ATTEMPTS, 5)

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
                service.MIN_TTL_SECONDS,
                service.MAX_TTL_SECONDS,
            ),
            (
                "setup_token_max_attempts",
                service.MIN_MAX_ATTEMPTS,
                service.MAX_MAX_ATTEMPTS,
            ),
        ):
            with self.subTest(name):
                make_settings(**{name: low})
                make_settings(**{name: high})
                for outside in (low - 1, high + 1):
                    with self.assertRaises(ValidationError):
                        make_settings(**{name: outside})

    def test_the_documented_bounds(self):
        self.assertEqual(
            (service.MIN_TTL_SECONDS, service.MAX_TTL_SECONDS), (60, 86400)
        )
        self.assertEqual((service.MIN_MAX_ATTEMPTS, service.MAX_MAX_ATTEMPTS), (1, 20))

    def test_a_bad_value_is_not_echoed_in_the_error(self):
        with paw_environment(PAW_SETUP_TOKEN_TTL_SECONDS="hunter2-not-a-number"):
            with self.assertRaises(ValidationError) as caught:
                Settings()

        self.assertNotIn("hunter2", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
