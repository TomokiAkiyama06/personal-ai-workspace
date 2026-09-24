import time
import unittest

from paw_backend.tools import (
    contains_credential_plaintext,
    is_credential_handle,
    redact_value,
)
from paw_backend.tools.credentials import (
    MAX_RESULT_NODES,
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
        # A text under a key that names a secret is redacted, whatever it says.
        self.assertEqual(redacted["password_hint"], REDACTED)

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


GITLAB = "glpat-" + "aB3_" * 6
GOOGLE = "AIza" + "Sy" + "aB3-" * 8 + "x"
STRIPE = "sk_" + "live_" + "a1B2" * 6
NPM = "npm_" + "aB3d" * 9
SLACK_WEBHOOK = "https://hooks.slack.com/services/T0123456/B0123456/" + "a1B2" * 6
HUGGING_FACE = "hf_" + "aB3d" * 9
SENDGRID = "SG." + "a" * 22 + "." + "b" * 43
AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


class PrefixedNamesTest(unittest.TestCase):
    """A token glued to a variable name must not slip through (``_`` is a word
    character, which used to hide exactly these spellings)."""

    def test_the_scan_sees_tokens_behind_a_name(self):
        for label, text in {
            "github": f"MYTOKEN_{GITHUB}",
            "aws": "key_" + AWS,
            "openai": f"OPENAI_API_KEY_{OPENAI}",
            "hyphen": f"my-{GITHUB_PAT}",
            "slack": f"SLACK_TOKEN_{SLACK}",
            "assigned": f"GITHUB_TOKEN={GITHUB}",
            "quoted": f'"api_key":"{OPENAI}"',
            "after a colon": f"token:{GITHUB}",
        }.items():
            with self.subTest(label=label):
                self.assertTrue(contains_credential_plaintext(text))

    def test_redaction_keeps_the_name_and_removes_the_token(self):
        cases = {
            f"MYTOKEN_{GITHUB}": f"MYTOKEN_{REDACTED}",
            f"OPENAI_API_KEY_{OPENAI}": f"OPENAI_API_KEY_{REDACTED}",
            "key_" + AWS: f"key_{REDACTED}",
        }
        for text, expected in cases.items():
            with self.subTest(text=expected):
                self.assertEqual(redact_text(text), (expected, 1))

    def test_words_that_merely_end_in_a_prefix_are_not_tokens(self):
        for text in (
            "disk-usage-monitoring-report-final-v2",
            "risk-management-and-mitigation-strategies",
            "task-runner-configuration-management-service",
            "ask-anything-about-the-repository-layout",
            "xghp_" + "a" * 36,  # letters glued to the prefix: not a GitHub token
            "AKIAABCDEFGH12345678X",  # longer than a key id
        ):
            with self.subTest(text=text):
                self.assertFalse(contains_credential_plaintext(text))

    def test_more_formats(self):
        for label, secret in {
            "gitlab": GITLAB,
            "google": GOOGLE,
            "stripe": STRIPE,
            "npm": NPM,
            "slack webhook": SLACK_WEBHOOK,
            "hugging face": HUGGING_FACE,
            "sendgrid": SENDGRID,
            "pgp": "-----BEGIN PGP PRIVATE KEY BLOCK-----\nabc\n"
            "-----END PGP PRIVATE KEY BLOCK-----",
        }.items():
            with self.subTest(label=label):
                self.assertTrue(contains_credential_plaintext(secret))
                self.assertEqual(redact_text(f"x {secret} y")[0], f"x {REDACTED} y")


class SecretFilesTest(unittest.TestCase):
    """What ``read_file(".env")`` and its cousins return."""

    def test_an_env_file(self):
        text = (
            "# production\n"
            "DEBUG=false\n"
            "DB_HOST=db.internal\n"
            "DB_PASSWORD=hunter2hunter2\n"
            f"AWS_SECRET_ACCESS_KEY={AWS_SECRET}\n"
            "GITHUB_TOKEN=abcdefgh12345678\n"
            "export API_TOKEN='abcdefgh12345678'\n"
            'MYSQL_ROOT_PASSWORD="correct horse battery"\n'
            "dbPassword=hunter2hunter2\n"
            "PORT=8080\n"
        )
        redacted, count = redact_text(text)
        self.assertEqual(
            redacted,
            "# production\n"
            "DEBUG=false\n"
            "DB_HOST=db.internal\n"
            f"DB_PASSWORD={REDACTED}\n"
            f"AWS_SECRET_ACCESS_KEY={REDACTED}\n"
            f"GITHUB_TOKEN={REDACTED}\n"
            f"export API_TOKEN={REDACTED}\n"
            f"MYSQL_ROOT_PASSWORD={REDACTED}\n"
            f"dbPassword={REDACTED}\n"
            "PORT=8080\n",
        )
        self.assertEqual(count, 6)

    def test_json_yaml_and_ini_text(self):
        cases = {
            '{"db_password": "hunter2hunter2", "port": 5432}': (
                f'{{"db_password": {REDACTED}, "port": 5432}}'
            ),
            '{"API_TOKEN":"abcdefgh12345678"}': f'{{"API_TOKEN":{REDACTED}}}',
            '{"password": "correct horse battery staple"}': (
                f'{{"password": {REDACTED}}}'
            ),
            "api_token: abcdefgh12345678\nname: demo": (
                f"api_token: {REDACTED}\nname: demo"
            ),
            "[db]\npassword = hunter2hunter2\nuser = bob": (
                f"[db]\npassword = {REDACTED}\nuser = bob"
            ),
            "client_secret = 'abc def ghi'": f"client_secret = {REDACTED}",
            "curl --password hunter2hunter2 --user bob": (
                f"curl --password {REDACTED} --user bob"
            ),
            "docker login --password-stdin --token abcdefgh12345678": (
                f"docker login --password-stdin --token {REDACTED}"
            ),
        }
        for text, expected in cases.items():
            with self.subTest(text=text[:40]):
                self.assertEqual(redact_text(text)[0], expected)

    def test_ordinary_configuration_is_left_alone(self):
        for text in (
            "DEBUG=false\nPORT=8080\nname: demo\nmax_tokens = 4096",
            "token: cred_" + "a1" * 16,
            'api_key: ""',
            "the password is stored in the vault",
            "PWD=/home/user/project",
        ):
            with self.subTest(text=text[:30]):
                self.assertEqual(redact_text(text), (text, 0))

    def test_a_secret_in_a_nested_result_is_redacted_at_every_depth(self):
        result = {
            "files": [
                {"path": ".env", "content": f"DB_PASSWORD=hunter2hunter2\nX={GITHUB}"},
                {"path": "a.py", "content": "print(1)"},
            ],
            "meta": {"env": {"GITHUB_TOKEN": "abcdefgh12345678", "N": 3}},
        }
        redacted, count = redact_value(result)
        self.assertEqual(
            redacted,
            {
                "files": [
                    {
                        "path": ".env",
                        "content": f"DB_PASSWORD={REDACTED}\nX={REDACTED}",
                    },
                    {"path": "a.py", "content": "print(1)"},
                ],
                "meta": {"env": {"GITHUB_TOKEN": REDACTED, "N": 3}},
            },
        )
        self.assertEqual(count, 3)


class RedactedKeysTest(unittest.TestCase):
    def test_a_secret_used_as_a_key_is_redacted(self):
        redacted, count = redact_value({GITHUB: "x", "plain": 1})
        self.assertEqual(redacted, {REDACTED: "x", "plain": 1})
        self.assertEqual(count, 1)
        self.assertNotIn(GITHUB, repr(redacted))

    def test_keys_that_redact_alike_do_not_overwrite_each_other(self):
        other = "ghp_" + "Z9y8" * 9
        redacted, count = redact_value({GITHUB: "first", other: "second"})
        self.assertEqual(sorted(redacted.values()), ["first", "second"])
        self.assertEqual(len(redacted), 2)
        self.assertEqual(count, 2)
        self.assertNotIn(other, repr(redacted))

    def test_env_style_keys_hide_their_values(self):
        redacted, _ = redact_value(
            {
                "AWS_SECRET_ACCESS_KEY": AWS_SECRET,
                "GITHUB_TOKEN": "abc",
                "dbPassword": "x",
                "DB_PASSWORD": {"nested": "x"},
                "API_KEYS": REDACTED,
                "PORT": "8080",
                "max_tokens": 4096,
                "token_count": 12,
                "has_secret": False,
                "credential_handle": HANDLE,
            }
        )
        self.assertEqual(
            redacted,
            {
                "AWS_SECRET_ACCESS_KEY": REDACTED,
                "GITHUB_TOKEN": REDACTED,
                "dbPassword": REDACTED,
                "DB_PASSWORD": REDACTED,
                "API_KEYS": REDACTED,
                "PORT": "8080",
                "max_tokens": 4096,
                "token_count": 12,
                "has_secret": False,
                "credential_handle": HANDLE,
            },
        )


class ResultBudgetTest(unittest.TestCase):
    def test_a_huge_list_is_cut_quickly_with_a_marker(self):
        started = time.monotonic()
        redacted, count = redact_value(["x"] * 2_000_000)
        self.assertLess(time.monotonic() - started, 20.0)
        self.assertEqual(len(redacted), MAX_RESULT_NODES)
        self.assertEqual(redacted[-1], TRUNCATED)
        self.assertEqual(count, 1)

    def test_a_huge_dict_is_cut_with_a_marker(self):
        redacted, count = redact_value({f"k{i}": i for i in range(300_000)})
        self.assertEqual(len(redacted), MAX_RESULT_NODES)
        self.assertIn(TRUNCATED, redacted)
        self.assertEqual(count, 1)

    def test_a_result_with_too_many_characters_is_cut(self):
        chunk = "a" * 100_000
        redacted, count = redact_value([chunk] * 60)
        self.assertEqual(redacted.count(TRUNCATED), 1)
        self.assertEqual(count, 1)
        self.assertEqual(len(redacted), 41)
        self.assertEqual(redacted[0], chunk)

    def test_a_small_result_is_untouched(self):
        result = {"a": [1, 2, {"b": "c"}], "d": None}
        self.assertEqual(redact_value(result), (result, 0))


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
