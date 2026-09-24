"""``PAW_SCRATCH_PURGE_INTERVAL_SECONDS``: how often expired items are purged."""

import unittest

from pydantic import ValidationError

from paw_backend.config import Settings
from paw_backend.research.scratch import janitor

from .support import make_settings, paw_environment


class PurgeIntervalSettingTest(unittest.TestCase):
    def test_the_default_is_one_hour(self):
        self.assertEqual(make_settings().scratch_purge_interval_seconds, 3600)

    def test_the_value_comes_from_the_environment(self):
        with paw_environment(PAW_SCRATCH_PURGE_INTERVAL_SECONDS="600"):
            settings = Settings()

        self.assertEqual(settings.scratch_purge_interval_seconds, 600)

    def test_zero_turns_the_janitor_off(self):
        self.assertEqual(
            make_settings(
                scratch_purge_interval_seconds=0
            ).scratch_purge_interval_seconds,
            0,
        )
        with paw_environment(PAW_SCRATCH_PURGE_INTERVAL_SECONDS="0"):
            self.assertEqual(Settings().scratch_purge_interval_seconds, 0)

    def test_the_bounds_are_a_minute_and_a_day(self):
        for value in (60, 61, 3600, 86_399, 86_400):
            with self.subTest(value):
                self.assertEqual(
                    make_settings(
                        scratch_purge_interval_seconds=value
                    ).scratch_purge_interval_seconds,
                    value,
                )
        for value in (-1, 1, 30, 59, 86_401, 10**9):
            with self.subTest(value):
                with self.assertRaises(ValidationError):
                    make_settings(scratch_purge_interval_seconds=value)

    def test_the_maximum_is_what_the_janitor_accepts(self):
        settings = make_settings(scratch_purge_interval_seconds=86_400)

        self.assertEqual(
            settings.scratch_purge_interval_seconds, janitor.MAX_INTERVAL_SECONDS
        )

    def test_a_non_integer_is_rejected(self):
        for value in ("hourly", "1e3", "60.5", "", " ", "-"):
            with self.subTest(value):
                with paw_environment(PAW_SCRATCH_PURGE_INTERVAL_SECONDS=value):
                    with self.assertRaises(ValidationError):
                        Settings()

    def test_a_bad_value_is_not_echoed_in_the_error(self):
        for value in ("hunter2-not-a-number", "59"):
            with self.subTest(value):
                with paw_environment(PAW_SCRATCH_PURGE_INTERVAL_SECONDS=value):
                    with self.assertRaises(ValidationError) as caught:
                        Settings()
                self.assertNotIn(value, str(caught.exception))

    def test_the_error_names_the_setting(self):
        with self.assertRaises(ValidationError) as caught:
            make_settings(scratch_purge_interval_seconds=59)

        self.assertIn("scratch_purge_interval_seconds", str(caught.exception))
        self.assertIn("0 (off) or 60 to 86400", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
