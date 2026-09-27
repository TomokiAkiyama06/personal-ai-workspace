"""The Passkey settings and ``PasskeyConfig`` (no database)."""

import unittest

from pydantic import ValidationError

from paw_backend.auth.passkeys.config import PasskeyConfig

from .support import make_settings

GOOD = {
    "passkey_rp_id": "paw.example.test",
    "passkey_origins": ["https://paw.example.test"],
}


class AcceptedTest(unittest.TestCase):
    def test_unset_means_off(self):
        settings = make_settings()
        self.assertEqual((settings.passkey_rp_id, settings.passkey_origins), (None, []))
        self.assertIsNone(PasskeyConfig.from_settings(settings))

    def test_a_configuration_is_normalised(self):
        settings = make_settings(
            passkey_rp_id=" PAW.Example.TEST ",
            passkey_origins=[
                "https://PAW.example.test/",
                "https://app.paw.example.test:8443",
                "https://paw.example.test",  # a duplicate of the first
            ],
        )
        self.assertEqual(settings.passkey_rp_id, "paw.example.test")
        self.assertEqual(
            settings.passkey_origins,
            ["https://paw.example.test", "https://app.paw.example.test:8443"],
        )
        config = PasskeyConfig.from_settings(settings)
        self.assertEqual(
            (config.rp_id, config.rp_name, config.challenge_ttl_seconds),
            ("paw.example.test", "Personal AI Workspace", 300),
        )
        self.assertEqual(
            config.origins,
            ("https://paw.example.test", "https://app.paw.example.test:8443"),
        )

    def test_localhost_may_use_plain_http_for_development(self):
        settings = make_settings(
            passkey_rp_id="localhost", passkey_origins=["http://localhost:5173"]
        )
        self.assertEqual(settings.passkey_origins, ["http://localhost:5173"])

    def test_the_environment_spelling(self):
        from paw_backend.config import Settings

        from .support import paw_environment

        with paw_environment(
            PAW_PASSKEY_RP_ID="paw.example.test",
            PAW_PASSKEY_ORIGINS="https://paw.example.test, https://a.paw.example.test",
        ):
            settings = Settings()
        self.assertEqual(
            settings.passkey_origins,
            ["https://paw.example.test", "https://a.paw.example.test"],
        )

    def test_an_empty_rp_id_in_the_environment_is_unset(self):
        from paw_backend.config import Settings

        from .support import paw_environment

        with paw_environment(PAW_PASSKEY_RP_ID="", PAW_PASSKEY_ORIGINS=""):
            self.assertIsNone(Settings().passkey_rp_id)

    def test_the_challenge_lifetime_boundaries(self):
        for seconds in (30, 900):
            with self.subTest(seconds=seconds):
                settings = make_settings(passkey_challenge_ttl_seconds=seconds, **GOOD)
                self.assertEqual(
                    PasskeyConfig.from_settings(settings).challenge_ttl_seconds, seconds
                )


class RefusedTest(unittest.TestCase):
    def refused(self, **overrides):
        with self.assertRaises(ValidationError) as caught:
            make_settings(**overrides)
        # A configuration error never echoes what was configured.
        text = str(caught.exception).replace("input_value", "")
        for value in overrides.values():
            for item in value if isinstance(value, list) else [value]:
                if isinstance(item, str) and len(item) >= 8:
                    self.assertNotIn(item, text)
        return caught.exception

    def test_both_or_neither(self):
        self.refused(passkey_rp_id="paw.example.test")
        self.refused(passkey_origins=["https://paw.example.test"])

    def test_the_rp_id_is_a_domain_name(self):
        for bad in (
            "https://paw.example.test",
            "paw.example.test:8443",
            "paw.example.test/path",
            "192.0.2.7",
            "::1",
            "-paw.example.test",
            "paw..example.test",
            "paw.example.test.",
            "pä.example.test",
            "a" * 64 + ".test",
            ("a" * 60 + ".") * 5 + "test",
            "paw example.test",
        ):
            with self.subTest(rp_id=bad[:30]):
                self.refused(
                    passkey_rp_id=bad, passkey_origins=["https://paw.example.test"]
                )

    def test_an_origin_is_a_secure_origin_of_the_rp_id(self):
        for bad in (
            "http://paw.example.test",  # not a secure context
            "https://paw.example.test/path",
            "https://user@paw.example.test",
            "https://evil.example",  # not under the RP ID
            "https://notpaw.example.test",  # a suffix match is not a sub-domain
            "https://example.test",  # a parent of the RP ID
            "ftp://paw.example.test",
            "paw.example.test",
            "null",
            "https://[::1]",
            "",
        ):
            with self.subTest(origin=bad):
                self.refused(passkey_rp_id="paw.example.test", passkey_origins=[bad])

    def test_too_many_origins(self):
        origins = [f"https://a{index}.paw.example.test" for index in range(9)]
        self.refused(passkey_rp_id="paw.example.test", passkey_origins=origins)

    def test_the_name_and_the_lifetime_are_bounded(self):
        for name in ("", "  ", "x" * 65, "bad\nname", "bad\x00"):
            with self.subTest(name=repr(name)):
                self.refused(passkey_rp_name=name, **GOOD)
        for seconds in (0, 29, 901, -1):
            with self.subTest(seconds=seconds):
                self.refused(passkey_challenge_ttl_seconds=seconds, **GOOD)


class ConfigObjectTest(unittest.TestCase):
    def test_a_config_is_checked_when_it_is_built(self):
        good = dict(
            rp_id="paw.example.test",
            rp_name="PAW",
            origins=("https://paw.example.test",),
            challenge_ttl_seconds=300,
        )
        PasskeyConfig(**good)
        for name, value in (
            ("rp_id", ""),
            ("rp_id", None),
            ("rp_name", 5),
            ("origins", ["https://paw.example.test"]),
            ("origins", ()),
            ("origins", ("",)),
            ("challenge_ttl_seconds", 29),
            ("challenge_ttl_seconds", 901),
            ("challenge_ttl_seconds", True),
            ("challenge_ttl_seconds", 300.0),
        ):
            with self.subTest(field=name, value=repr(value)):
                with self.assertRaises(ValueError):
                    PasskeyConfig(**{**good, name: value})

    def test_settings_are_required_to_be_settings(self):
        for bad in (None, {}, "settings"):
            with self.assertRaises(TypeError):
                PasskeyConfig.from_settings(bad)
