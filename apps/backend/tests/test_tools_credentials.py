import time
import unittest

from paw_backend.tools import (
    contains_credential_plaintext,
    is_credential_handle,
    redact_value,
)
from paw_backend.tools.credentials import (
    MAX_TEXT_CHARS,
    REDACTED,
    TRUNCATED,
    UNSUPPORTED,
    redact_text,
)

from .tools_support import HANDLE

# Built at run time so that no secret-looking literal sits in the source.
GITHUB = "ghp_" + "a1B2" * 9
GITHUB_PAT = "github_pat_" + "A1b2C3d4E5" * 3
OPENAI = "sk-" + "Ab1" * 10
AWS = "AKIA" + "ABCDEFGH12345678"
SLACK = "xoxb-" + "1234567890" * 2
JWT = "eyJ" + "hbGciOiJI" + ".eyJ" + "zdWIiOiIxMjM0" + ".SflKxwRJSMeKKF2QT4fw"
PEM = (
    "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIabc\ndef==\n-----END RSA PRIVATE KEY-----"
)


class HandleTest(unittest.TestCase):
    def test_only_the_exact_backend_format(self):
        self.assertTrue(is_credential_handle(HANDLE))
        for value in (
            HANDLE.upper(),
            HANDLE + " ",
            " " + HANDLE,
            HANDLE + "\n",
            "cred_" + "a" * 31,
            "cred_" + "a" * 33,
            "cred_" + "g" * 32,
            "Cred_" + "a" * 32,
            GITHUB,
            "",
            None,
            5,
            b"cred_" + b"a" * 32,
        ):
            with self.subTest(value=value):
                self.assertFalse(is_credential_handle(value))


class DetectionTest(unittest.TestCase):
    def test_recognisable_credentials(self):
        for label, secret in {
            "github": GITHUB,
            "github pat": GITHUB_PAT,
            "openai": OPENAI,
            "aws": AWS,
            "slack": SLACK,
            "jwt": JWT,
            "pem": PEM,
            "bearer": "Authorization: Bearer abcdefghijklmnop1234",
            "basic": "basic dXNlcjpwYXNzd29yZA==abcdef",
            "url password": "https://user:hunter2hunter2@example.com/repo.git",
        }.items():
            with self.subTest(label=label):
                self.assertTrue(contains_credential_plaintext(secret))
                self.assertTrue(
                    contains_credential_plaintext(f"prefix {secret} suffix")
                )

    def test_hidden_inside_format_characters_or_look_alike_letters(self):
        zero_width = GITHUB[:6] + "​" + GITHUB[6:]
        self.assertTrue(contains_credential_plaintext(zero_width))
        fullwidth = "ｇｈｐ_" + "a1B2" * 9  # full-width "ghp"
        self.assertTrue(contains_credential_plaintext(fullwidth))

    def test_ordinary_source_code_and_prose_are_not_credentials(self):
        for text in (
            "def token(): return 1",
            "password = 'x'",
            "the api key is stored in the vault",
            "sk-learn and sk-short",
            "ghp_short",
            "See https://github.com/org/repo/issues/1",
            "git@github.com:org/repo.git",
            "Bearer",
            "-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----",
            HANDLE,
            "",
        ):
            with self.subTest(text=text):
                self.assertFalse(contains_credential_plaintext(text))

    def test_non_text_is_never_a_credential_match(self):
        for value in (None, 5, b"x", ["ghp"]):
            self.assertFalse(contains_credential_plaintext(value))


class RedactionTest(unittest.TestCase):
    def test_text_is_redacted_and_counted(self):
        text = f"clone with {GITHUB} then {OPENAI}"
        redacted, count = redact_text(text)
        self.assertEqual(redacted, f"clone with {REDACTED} then {REDACTED}")
        self.assertEqual(count, 2)

    def test_a_private_key_block_is_removed_through_its_end_line(self):
        redacted, count = redact_text(f"before\n{PEM}\nafter")
        self.assertEqual(redacted, f"before\n{REDACTED}\nafter")
        self.assertEqual(count, 1)

    def test_an_unterminated_private_key_is_removed_to_the_end(self):
        redacted, count = redact_text("x\n-----BEGIN " + "PRIVATE KEY-----\nMIIabc")
        self.assertEqual((redacted, count), (f"x\n{REDACTED}", 1))

    def test_assignments_are_redacted_in_results(self):
        for text in (
            "password = hunter2hunter2",
            'API_KEY: "abcd1234efgh"',
            "export TOKEN=abcdefgh12345",
            "Authorization: xyz12345678",
        ):
            with self.subTest(text=text):
                redacted, count = redact_text(text)
                self.assertGreaterEqual(count, 1)
                self.assertIn(REDACTED, redacted)
                self.assertNotIn("hunter2", redacted)
                self.assertNotIn("abcd1234", redacted)

    def test_a_handle_after_a_credential_word_is_not_a_secret(self):
        self.assertEqual(redact_text(f"token: {HANDLE}"), (f"token: {HANDLE}", 0))

    def test_a_secret_hidden_by_zero_width_characters_does_not_survive(self):
        hidden = GITHUB[:6] + "​" + GITHUB[6:]
        redacted, count = redact_text(f"x {hidden}")
        self.assertEqual((redacted, count), (f"x {REDACTED}", 1))
        self.assertNotIn(GITHUB[:6], redacted)

    def test_text_without_secrets_is_returned_unchanged(self):
        text = "café​ ﬁ nothing here"
        self.assertEqual(redact_text(text), (text, 0))

    def test_structures_are_redacted_recursively(self):
        value = {
            "output": f"using {GITHUB}",
            "password": "hunter2",
            "nested": {"api_key": "abc", "items": [f"a {OPENAI}", 3, None, True]},
            "count": 3,
            "tuple": ("x", GITHUB),
        }
        redacted, count = redact_value(value)
        self.assertEqual(
            redacted,
            {
                "output": f"using {REDACTED}",
                "password": REDACTED,
                "nested": {
                    "api_key": REDACTED,
                    "items": [f"a {REDACTED}", 3, None, True],
                },
                "count": 3,
                "tuple": ["x", REDACTED],
            },
        )
        self.assertEqual(count, 5)

    def test_sensitive_keys_are_recognised_by_their_ending(self):
        value = {
            "Password": "a",
            "db-password": "a",
            "CLIENT_SECRET": "a",
            "csrf_token": "a",
            "apiKey": "a",
            "authorization": "a",
            "ssh_key_path": "a",
            "credentials": {"user": "a"},
            "max_tokens": 5,
            "tokens_used": 7,
            "has_password": True,
            "credential_handle": HANDLE,
            "note": None,
            "password_hint": "none",
        }
        redacted, _ = redact_value(value)
        for key in ("Password", "db-password", "CLIENT_SECRET", "csrf_token", "apiKey"):
            self.assertEqual(redacted[key], REDACTED, key)
        self.assertEqual(redacted["authorization"], REDACTED)
        self.assertEqual(redacted["credentials"], REDACTED)
        self.assertEqual(redacted["max_tokens"], 5)
        self.assertEqual(redacted["tokens_used"], 7)
        self.assertIs(redacted["has_password"], True)
        self.assertEqual(redacted["credential_handle"], HANDLE)
        self.assertIsNone(redacted["note"])
        self.assertEqual(redacted["password_hint"], "none")

    def test_anything_that_is_not_json_data_becomes_a_marker(self):
        class Leaky:
            def __repr__(self):
                return "token=" + GITHUB

        redacted, count = redact_value(
            {
                "a": Leaky(),
                "b": b"bytes",
                "c": {1, 2},
                5: "int key",
                "d": bytearray(b"x"),
            }
        )
        self.assertEqual(redacted["a"], UNSUPPORTED)
        self.assertEqual(redacted["b"], UNSUPPORTED)
        self.assertEqual(redacted["c"], UNSUPPORTED)
        self.assertEqual(redacted["d"], UNSUPPORTED)
        self.assertEqual(redacted[UNSUPPORTED], UNSUPPORTED)
        self.assertEqual(count, 5)
        self.assertNotIn(GITHUB, repr(redacted))

    def test_scalars_pass_through(self):
        for value in (None, True, 0, 1.5, "plain"):
            self.assertEqual(redact_value(value), (value, 0))

    def test_deeply_nested_data_is_cut_off(self):
        value: object = "leaf"
        for _ in range(100):
            value = [value]
        redacted, count = redact_value(value)
        self.assertGreaterEqual(count, 1)
        depth = 0
        while isinstance(redacted, list):
            redacted = redacted[0]
            depth += 1
        self.assertEqual(redacted, REDACTED)
        self.assertEqual(depth, 33)


class BoundedWorkTest(unittest.TestCase):
    """Redaction and detection run on the event loop: their cost is bounded."""

    def test_a_text_longer_than_the_limit_is_cut_and_counted(self):
        text = "a" * (MAX_TEXT_CHARS + 10)
        redacted, count = redact_text(text)
        self.assertEqual(redacted, "a" * MAX_TEXT_CHARS + TRUNCATED)
        self.assertEqual(count, 1)
        exact = "b" * MAX_TEXT_CHARS
        self.assertEqual(redact_text(exact), (exact, 0))

    def test_a_secret_inside_a_text_that_is_cut_is_still_redacted(self):
        text = f"{GITHUB} " + "x" * (MAX_TEXT_CHARS + 10)
        redacted, count = redact_text(text)
        self.assertTrue(redacted.startswith(REDACTED + " x"))
        self.assertTrue(redacted.endswith(TRUNCATED))
        self.assertEqual(count, 2)

    def test_adversarial_texts_are_processed_in_bounded_time(self):
        texts = [
            "eyJ" * 300_000,
            "a." * 500_000,
            "sk-" * 300_000,
            "bearer " * 140_000,
            "a://" * 250_000,
            "-----BEGIN " + "PRIVATE KEY-----" * 60_000,
        ]
        started = time.monotonic()
        for text in texts:
            redact_text(text)
            contains_credential_plaintext(text)
        # Takes a fraction of a second; the deadline only catches a regression
        # to super-linear matching.
        self.assertLess(time.monotonic() - started, 30.0)


if __name__ == "__main__":
    unittest.main()
