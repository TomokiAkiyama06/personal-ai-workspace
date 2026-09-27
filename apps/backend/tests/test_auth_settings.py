"""The settings of the login / session / password code (PAW-022)."""

import unittest

from pydantic import ValidationError

from paw_backend.auth.models import ThrottleScope
from paw_backend.auth.sessions import SessionLifetimes
from paw_backend.auth.throttle import OnSuccess, policies_from_settings

from .support import make_settings, paw_environment


class DefaultsTest(unittest.TestCase):
    def test_the_defaults_are_the_values_of_the_requirements_and_decision_0015(self):
        settings = make_settings()
        self.assertEqual(
            (
                settings.session_idle_days,
                settings.session_remember_days,
                settings.session_absolute_days,
                settings.session_touch_interval_seconds,
                settings.session_cookie_samesite,
            ),
            (30, 90, 90, 60, "strict"),
        )
        self.assertEqual(
            (
                settings.login_account_free_attempts,
                settings.login_source_free_attempts,
                settings.login_backoff_seconds,
                settings.login_decay_seconds,
            ),
            (5, 20, [30, 60, 300, 900, 3600], 86_400),
        )
        self.assertEqual(
            (
                settings.redeem_source_free_attempts,
                settings.redeem_global_free_attempts,
                settings.redeem_backoff_seconds,
                settings.redeem_decay_seconds,
            ),
            (5, 30, [60, 300, 900, 3600], 900),
        )
        self.assertEqual(settings.password_hash_concurrency, 2)

    def test_the_session_lifetimes_are_in_seconds(self):
        lifetimes = SessionLifetimes.from_settings(make_settings())
        self.assertEqual(lifetimes.idle_seconds, 30 * 86_400)
        self.assertEqual(lifetimes.remember_seconds, 90 * 86_400)
        self.assertEqual(lifetimes.absolute_seconds, 90 * 86_400)
        self.assertEqual(lifetimes.touch_interval_seconds, 60)

    def test_the_throttle_policies_follow_the_settings(self):
        policies = policies_from_settings(make_settings(login_account_free_attempts=3))
        self.assertEqual(policies[ThrottleScope.LOGIN_ACCOUNT].free_attempts, 3)
        self.assertEqual(
            policies[ThrottleScope.LOGIN_ACCOUNT].on_success, OnSuccess.RESET
        )
        self.assertEqual(
            policies[ThrottleScope.LOGIN_SOURCE].on_success, OnSuccess.REFUND
        )
        self.assertEqual(
            policies[ThrottleScope.REDEEM_SOURCE].on_success, OnSuccess.KEEP
        )
        self.assertEqual(
            policies[ThrottleScope.REDEEM_GLOBAL].on_success, OnSuccess.KEEP
        )
        self.assertEqual(
            policies[ThrottleScope.LOGIN_ACCOUNT].backoff_seconds,
            (30, 60, 300, 900, 3600),
        )


class ValidationTest(unittest.TestCase):
    def rejected(self, **overrides):
        with self.assertRaises(ValidationError) as caught:
            make_settings(**overrides)
        return caught.exception

    def test_argon2_parameters_have_bounds(self):
        for name, bad in (
            ("password_hash_time_cost", 0),
            ("password_hash_time_cost", 11),
            ("password_hash_memory_kib", 19_455),
            ("password_hash_memory_kib", 1_048_577),
            ("password_hash_parallelism", 0),
            ("password_hash_parallelism", 17),
            ("password_hash_concurrency", 0),
            ("password_hash_concurrency", 17),
        ):
            with self.subTest(name=name, value=bad):
                self.rejected(**{name: bad})

    def test_the_lowest_accepted_argon2_memory_is_the_owasp_minimum(self):
        self.assertEqual(
            make_settings(password_hash_memory_kib=19_456).password_hash_memory_kib,
            19_456,
        )

    def test_session_lifetimes_must_be_consistent(self):
        self.rejected(session_idle_days=60, session_absolute_days=30)
        self.rejected(session_idle_days=60, session_remember_days=30)
        self.rejected(session_idle_days=0)
        self.rejected(session_remember_days=366)
        self.rejected(session_touch_interval_seconds=0)
        self.rejected(session_touch_interval_seconds=3601)

    def test_the_cookie_samesite_is_strict_or_lax_only(self):
        self.assertEqual(
            make_settings(session_cookie_samesite="lax").session_cookie_samesite, "lax"
        )
        for bad in ("none", "None", "", "Strict ", "relaxed"):
            with self.subTest(bad=bad):
                self.rejected(session_cookie_samesite=bad)

    def test_a_backoff_schedule_is_finite_bounded_and_never_shrinks(self):
        for bad in ([], [0], [-5], [86_401], [60, 30], list(range(1, 14)), ["x"]):
            with self.subTest(schedule=bad):
                self.rejected(login_backoff_seconds=bad)
                self.rejected(redeem_backoff_seconds=bad)
        ok = make_settings(login_backoff_seconds=[1, 1, 86_400])
        self.assertEqual(ok.login_backoff_seconds, [1, 1, 86_400])

    def test_a_schedule_can_be_given_as_a_comma_separated_environment_value(self):
        with paw_environment(PAW_LOGIN_BACKOFF_SECONDS="10, 20,40"):
            from paw_backend.config import Settings

            self.assertEqual(Settings().login_backoff_seconds, [10, 20, 40])

    def test_thresholds_and_decay_have_bounds(self):
        for name, bad in (
            ("login_account_free_attempts", 1),
            ("login_account_free_attempts", 51),
            ("login_source_free_attempts", 1),
            ("login_decay_seconds", 59),
            ("redeem_source_free_attempts", 0),
            ("redeem_global_free_attempts", 0),
            ("redeem_decay_seconds", 59),
            ("redeem_decay_seconds", 86_401),
        ):
            with self.subTest(name=name, value=bad):
                self.rejected(**{name: bad})

    def test_a_rejected_value_is_never_echoed(self):
        error = self.rejected(session_cookie_samesite="secret-looking-value")
        self.assertNotIn("secret-looking-value", str(error))


if __name__ == "__main__":
    unittest.main()
