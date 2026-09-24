"""``rules.py``: credential stripping and the abstraction rules.

Every test calls a stubbed function, so against the stubs each one fails with
``NotImplementedError``. Inputs are always collapsed text (the gate normalises
first) and only behaviour written in the docstrings is asserted.
"""

import unittest

from paw_backend.research.privacy import rules
from paw_backend.tools.credentials import REDACTED, redact_text

TOKEN = "ghp_" + "a" * 36
AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r"


class Table:
    """A mixin: run ``function`` on each ``(input, (expected text, count))`` row."""

    function_name = ""

    def check(self, rows):
        function = getattr(rules, self.function_name)
        for text, expected in rows:
            with self.subTest(text=text):
                self.assertEqual(function(text), expected)


class StripCredentialsTest(unittest.TestCase):
    def test_docstring_examples(self):
        self.assertEqual(rules.strip_credentials(f"use {TOKEN} here"), ("use here", 1))
        self.assertEqual(
            rules.strip_credentials("how to reset a password"),
            ("how to reset a password", 0),
        )
        text, count = rules.strip_credentials("DB_PASSWORD=hunter2hunter2 in .env")
        self.assertNotIn("hunter2hunter2", text)
        self.assertIn("DB_PASSWORD=", text)
        self.assertGreaterEqual(count, 1)

    def test_several_kinds_of_credential_are_removed(self):
        for credential in (
            TOKEN,
            AWS_KEY,
            "sk-" + "A1b2C3d4" * 4,
            "xoxb-1234567890-abcdefghij",
            JWT,
            "Bearer abcdefghijklmnopqrstuvwxyz012345",
            "AIza" + "B" * 35,
        ):
            with self.subTest(credential=credential[:8]):
                text, count = rules.strip_credentials(
                    f"call api with {credential} today"
                )
                self.assertNotIn(credential, text)
                self.assertNotIn(credential[10:24], text)
                self.assertEqual(
                    count, redact_text(f"call api with {credential} today")[1]
                )
                self.assertGreaterEqual(count, 1)
                self.assertTrue(text.startswith("call api with"))
                self.assertTrue(text.endswith("today"))

    def test_a_private_key_block_is_removed(self):
        key = (
            "-----BEGIN RSA PRIVATE KEY----- "
            "MIIBOgIBAAJBAKj34GkxFhD90vcNLYLInFEX6Ppy1tPf9Cnzj4p4WGeKLs1Pt8Qu "
            "-----END RSA PRIVATE KEY-----"
        )
        text, count = rules.strip_credentials(f"my key {key} is here")
        self.assertEqual(text, "my key is here")
        self.assertEqual(count, redact_text(f"my key {key} is here")[1])

    def test_a_password_in_a_url_is_removed(self):
        text, count = rules.strip_credentials(
            "clone https://alice:s3cretpw@example.com/repo.git now"
        )
        self.assertNotIn("s3cretpw", text)
        self.assertNotIn("alice", text)
        self.assertGreaterEqual(count, 1)

    def test_a_removed_credential_never_glues_its_neighbours(self):
        # The token is delimited by non-ASCII letters, which are not "word" text
        # for the credential patterns: what is left on both sides stays apart.
        self.assertEqual(rules.strip_credentials(f"日本{TOKEN}語"), ("日本 語", 1))

    def test_the_marker_never_survives(self):
        text, _ = rules.strip_credentials(f"a {TOKEN} b {AWS_KEY} c")
        self.assertNotIn(REDACTED, text)
        self.assertNotIn("REDACTED", text)
        self.assertEqual(text, "a b c")

    def test_the_count_is_the_one_redact_text_reports(self):
        for text in (
            f"a {TOKEN} b",
            f"a {TOKEN} b {AWS_KEY} c",
            f"password={TOKEN}",
            f"--token {AWS_KEY}",
            "plain words only",
        ):
            with self.subTest(text=text):
                self.assertEqual(rules.strip_credentials(text)[1], redact_text(text)[1])

    def test_plain_text_is_unchanged(self):
        for text in (
            "token bucket algorithm",
            "how do I rotate an api key safely",
            "secret sharing scheme",
            "python 3.13 asyncio",
            "",
        ):
            with self.subTest(text=text):
                self.assertEqual(rules.strip_credentials(text), (text, 0))

    def test_the_result_is_collapsed(self):
        text, _ = rules.strip_credentials(f"{TOKEN} first {TOKEN} second")
        self.assertEqual(text, "first second")


class IsPrivateHostTest(unittest.TestCase):
    def test_private_and_unknown_hosts(self):
        for host in (
            "",
            "localhost",
            "LOCALHOST",
            "localhost.",
            "db",
            "intranet-app",
            "db.corp",
            "printer.local",
            "wiki.internal",
            "nas.lan",
            "router.home",
            "svc.intranet",
            "host.localdomain",
            "x.private",
            "1.0.0.127.in-addr.arpa",
            "192.168.0.1",
            "10.0.0.5",
            "127.0.0.1",
            "8.8.8.8",
            "::1",
            "[::1]",
            "[2001:db8::1]",
            "[::ffff:1.2.3.4]",
            "fe80::1",
            "SERVER.CORP",
            "a.b.local.",
        ):
            with self.subTest(host=host):
                self.assertTrue(rules.is_private_host(host))

    def test_public_hosts(self):
        for host in (
            "docs.python.org",
            "EXAMPLE.COM.",
            "example.com",
            "corp.example.com",
            "local.example.com",
            "internal.example.org",
            "github.com",
            "3.13",
            "1.2.3.999",
            "a.b",
            "localhost.example.com",
            "xn--e1afmkfd.xn--p1ai",
            "my-host.co.uk",
        ):
            with self.subTest(host=host):
                self.assertFalse(rules.is_private_host(host))


class AbstractUrlsTest(Table, unittest.TestCase):
    function_name = "abstract_urls"

    def test_docstring_examples(self):
        self.check(
            (
                (
                    "read https://docs.python.org/3/library/asyncio.html for details",
                    ("read docs.python.org for details", 1),
                ),
                ("open http://192.168.1.10:8080/admin", ("open", 1)),
                (
                    "http://localhost:3000/x and https://Example.COM/A?q=1#f",
                    ("and example.com", 2),
                ),
                ("(see https://python.org/downloads).", ("(see python.org).", 1)),
                (
                    "git clone ssh://git@github.com/org/repo.git",
                    ("git clone github.com", 1),
                ),
                ("file:///etc/passwd", ("", 1)),
                ("see http:// here", ("see http:// here", 0)),
            )
        )

    def test_hosts_userinfo_ports_and_case(self):
        self.check(
            (
                ("HTTP://Example.com/x", ("example.com", 1)),
                ("https://user:pw@example.com:8443/p", ("example.com", 1)),
                ("ftp://ftp.example.org/pub/file.zip", ("ftp.example.org", 1)),
                (
                    "https://sub.domain.example.co.jp:443",
                    ("sub.domain.example.co.jp", 1),
                ),
                ("https://corp.example.com/", ("corp.example.com", 1)),
                ("go to https://a.io/x?y=1&z=2", ("go to a.io", 1)),
            )
        )

    def test_private_hosts_are_removed_whole(self):
        self.check(
            (
                ("http://[::1]:8080/x", ("", 1)),
                ("http://[2001:db8::1]/x rest", ("rest", 1)),
                ("http://intranet/wiki page", ("page", 1)),
                ("http://wiki.corp/x", ("", 1)),
                ("https://db.internal:5432/mydb ok", ("ok", 1)),
                ("http://8.8.8.8/dns", ("", 1)),
                ("http://[::1/x", ("", 1)),
            )
        )

    def test_trailing_punctuation_stays_in_the_text(self):
        self.check(
            (
                ("see https://python.org/, and", ("see python.org, and", 1)),
                ("is it https://python.org/x?", ("is it python.org?", 1)),
                (
                    "(https://python.org/a) and [https://python.org/b]",
                    ("(python.org) and [python.org]", 2),
                ),
                ('"https://python.org/a"', ('"python.org"', 1)),
                ("http://localhost/x;", (";", 1)),
            )
        )

    def test_text_without_a_url_is_unchanged(self):
        self.check(
            (
                ("", ("", 0)),
                ("no url here", ("no url here", 0)),
                ("://x is not a url", ("://x is not a url", 0)),
                ("mailto:a@b.org", ("mailto:a@b.org", 0)),
                ("a/b/c and d.e", ("a/b/c and d.e", 0)),
            )
        )

    def test_the_url_ends_at_the_next_space(self):
        self.check((("https://a.io/x b.io/y", ("a.io b.io/y", 1)),))


class AbstractEmailsTest(Table, unittest.TestCase):
    function_name = "abstract_emails"

    def test_docstring_examples(self):
        self.check(
            (
                ("contact tom.k+dev@example.co.jp today", ("contact today", 1)),
                ("a@x.org and b@y.org", ("and", 2)),
                ("mailto:bob@x.org", ("mailto:", 1)),
                ("a@b.c user@localhost @octocat", ("a@b.c user@localhost @octocat", 0)),
            )
        )

    def test_a_removed_address_never_glues_its_neighbours(self):
        self.check(
            (
                ("x,a@b.org,y", ("x, ,y", 1)),
                ("see:a@x.org:next", ("see: :next", 1)),
            )
        )

    def test_more_addresses(self):
        self.check(
            (
                ("john@corp.internal is out", ("is out", 1)),
                ("first.last_name%tag@sub.example-site.com!", ("!", 1)),
                ("write to A.B@EXAMPLE.ORG or C@d.io", ("write to or", 2)),
                ("x@y.zz", ("", 1)),
                ("ab@c.d1", ("ab@c.d1", 0)),
            )
        )

    def test_texts_that_are_not_addresses(self):
        self.check(
            (
                ("", ("", 0)),
                ("me @ example.com", ("me @ example.com", 0)),
                ("@handle and user@host", ("@handle and user@host", 0)),
                ("email address format", ("email address format", 0)),
            )
        )


class AbstractPathsTest(Table, unittest.TestCase):
    function_name = "abstract_paths"

    def test_docstring_examples(self):
        self.check(
            (
                ("open /etc/passwd now", ("open now", 1)),
                ("see ~/notes.txt", ("see", 1)),
                ("run ./build.sh", ("run", 1)),
                ("edit src/app/main.py please", ("edit please", 1)),
                ("read (C:\\Users\\tom\\x.txt)", ("read", 1)),
                ("start --config=/etc/app.conf", ("start", 1)),
                ("use TCP/IP and/or", ("use TCP/IP and/or", 0)),
                ("a=b file.txt", ("a=b file.txt", 0)),
            )
        )

    def test_the_start_rules(self):
        self.check(
            (
                ("cd ../up now", ("cd now", 1)),
                ("cd ../ now", ("cd now", 1)),
                ("share \\\\server\\share ok", ("share ok", 1)),
                ("open 'x' and '/tmp' done", ("open 'x' and done", 1)),
                ('use "~/x" now', ("use now", 1)),
                ("see (/etc/hosts)", ("see", 1)),
                ("see [/var] and {./x} <../y>", ("see and", 3)),
                ("D:/data now", ("now", 1)),
                ("C:\\x", ("", 1)),
                ("a lone / slash", ("a lone slash", 1)),
            )
        )

    def test_the_separator_count_rule(self):
        self.check(
            (
                ("a/b/c here", ("here", 1)),
                ("a\\b\\c here", ("here", 1)),
                ("a/b\\c here", ("here", 1)),
                ("1/2 is half", ("1/2 is half", 0)),
                ("I/O and TCP/IP", ("I/O and TCP/IP", 0)),
                ("key=value/1", ("key=value/1", 0)),
                ("x=a/b/c", ("", 1)),
            )
        )

    def test_the_assignment_rule(self):
        self.check(
            (
                ('log="/var/log" x', ("x", 1)),
                ('log="/var" x', ("x", 1)),
                ("(dir='./x') y", ("y", 1)),
                ("dir=~/tmp x", ("x", 1)),
                ("dir=./tmp x", ("x", 1)),
                ("mode=fast level=2", ("mode=fast level=2", 0)),
                ("a=b=/x", ("a=b=/x", 0)),
            )
        )

    def test_several_paths_are_counted_one_by_one(self):
        self.check(
            (
                ("/a /b/c ~/d keep", ("keep", 3)),
                ("keep only words here", ("keep only words here", 0)),
                ("", ("", 0)),
                ("file.txt main.py config.yaml", ("file.txt main.py config.yaml", 0)),
            )
        )


class AbstractHostsTest(Table, unittest.TestCase):
    function_name = "abstract_hosts"

    def test_docstring_examples(self):
        self.check(
            (
                ("connect to db.internal:5432 now", ("connect to now", 1)),
                ("ping 192.168.1.5, then", ("ping then", 1)),
                ("use localhost:8080/health", ("use", 1)),
                ("see (printer.local)", ("see", 1)),
                ("[::1]:8080", ("", 1)),
                ("docs at example.com.", ("docs at example.com.", 0)),
                ("python 3.13 server1 file.py", ("python 3.13 server1 file.py", 0)),
            )
        )

    def test_more_private_hosts(self):
        self.check(
            (
                ("10.0.0.1:22", ("", 1)),
                ("redis.local:6379 up", ("up", 1)),
                ("db.internal/health ok", ("ok", 1)),
                ("SERVER.CORP is down", ("is down", 1)),
                ("[fe80::1] x", ("x", 1)),
                ("(localhost)", ("", 1)),
                ("localhost. next", ("next", 1)),
                ("1.2.3.4 and 8.8.8.8", ("and", 2)),
                ('"my.app.internal"', ("", 1)),
                ("wiki.corp, then", ("then", 1)),
                ("localhost.localdomain up", ("up", 1)),
                ("[::ffff:1.2.3.4]:80 up", ("up", 1)),
            )
        )

    def test_public_hosts_and_words_stay(self):
        self.check(
            (
                ("example.com:8080 up", ("example.com:8080 up", 0)),
                ("example.com/a up", ("example.com/a up", 0)),
                ("corp.example.com", ("corp.example.com", 0)),
                ("v1.2.3 is out", ("v1.2.3 is out", 0)),
                ("1.2.3 is out", ("1.2.3 is out", 0)),
                ("server1 db intranet", ("server1 db intranet", 0)),
                ("[docs] and (notes)", ("[docs] and (notes)", 0)),
                ("[dead] [beef.cafe]", ("[dead] [beef.cafe]", 0)),
                ("host:8080", ("host:8080", 0)),
                ("", ("", 0)),
            )
        )

    def test_the_whole_core_must_be_a_host(self):
        self.check(
            (
                ("db.internal:123456 x", ("db.internal:123456 x", 0)),
                ("db.internal:abc x", ("db.internal:abc x", 0)),
                ("see db.internal=x", ("see db.internal=x", 0)),
                ("prefix-db.internal:80x", ("prefix-db.internal:80x", 0)),
            )
        )


class AbstractIdsTest(Table, unittest.TestCase):
    function_name = "abstract_ids"

    def test_docstring_examples(self):
        self.check(
            (
                (
                    "user 123e4567-e89b-12d3-a456-426614174000 failed",
                    ("user failed", 1),
                ),
                ("ticket 1234567 vs 1234", ("ticket vs 1234", 1)),
                ("commit deadbeefcafe12 pushed", ("commit pushed", 1)),
                ("deadbeefcaf", ("deadbeefcaf", 0)),
                ("abc12345", ("abc12345", 0)),
                ("12345abc", ("12345abc", 0)),
                ("pi is 3.14159265", ("pi is 3.14159265", 0)),
                ("id 12345.", ("id .", 1)),
                ("cred_" + "0" * 32, ("", 1)),
            )
        )

    def test_the_digit_boundary(self):
        self.check(
            (
                ("2026", ("2026", 0)),
                ("9999", ("9999", 0)),
                ("10000", ("", 1)),
                ("99999", ("", 1)),
                ("x 1234 y 12345 z", ("x 1234 y z", 1)),
                ("1234567890123456", ("", 1)),
                ("12345-67890", ("-", 2)),
                ("x_12345", ("x_", 1)),
                ("(12345)", ("( )", 1)),
            )
        )

    def test_decimals_stay(self):
        self.check(
            (
                ("12345.678", ("12345.678", 0)),
                ("0.12345678", ("0.12345678", 0)),
                ("3.14159265", ("3.14159265", 0)),
                ("v1.12345", ("v1.12345", 0)),
            )
        )

    def test_the_hex_boundary(self):
        self.check(
            (
                ("deadbeefcafe", ("", 1)),
                ("deadbeefcaf", ("deadbeefcaf", 0)),
                ("ABCDEF123456", ("", 1)),
                ("aBcDeF0123456789abcdef", ("", 1)),
                ("x deadbeefcafe1 y", ("x y", 1)),
                ("deadbeefcafexyz", ("deadbeefcafexyz", 0)),
                ("xdeadbeefcafe", ("xdeadbeefcafe", 0)),
                ("hash-deadbeefcafe", ("hash-", 1)),
            )
        )

    def test_uuids_and_handles(self):
        uuid_text = "123e4567-e89b-12d3-a456-426614174000"
        self.check(
            (
                (uuid_text.upper(), ("", 1)),
                (f"a {uuid_text} b {uuid_text} c", ("a b c", 2)),
                # A last group of 11 digits is no UUID, but it is a digit run.
                (
                    "123e4567-e89b-12d3-a456-42661417400",
                    ("123e4567-e89b-12d3-a456-", 1),
                ),
                (f"id={uuid_text}", ("id=", 1)),
                # Not bounded on the left: no UUID; the last group is a digit run.
                (f"x{uuid_text}", ("x123e4567-e89b-12d3-a456-", 1)),
                ("cred_" + "0f" * 16 + " here", ("here", 1)),
                # 31 digits are a digit run, not a handle.
                ("cred_" + "0" * 31, ("cred_", 1)),
                # Only lowercase digits make a handle; 32 hex digits are a hex run.
                ("cred_" + "0F" * 16, ("cred_", 1)),
            )
        )

    def test_nothing_to_remove(self):
        self.check(
            (
                ("", ("", 0)),
                ("plain words only", ("plain words only", 0)),
                ("python 3.13 and 0.141.1", ("python 3.13 and 0.141.1", 0)),
            )
        )


class DropOpaqueTokensTest(Table, unittest.TestCase):
    function_name = "drop_opaque_tokens"

    def test_docstring_examples(self):
        self.check(
            (
                ("key " + "A" * 40 + " end", ("key end", 1)),
                ("x " + "a-b_" * 10, ("x", 1)),
                ("A" * 45 + ".", (".", 1)),
            )
        )

    def test_the_length_boundary(self):
        self.check(
            (
                ("A" * 39, ("A" * 39, 0)),
                ("A" * 40, ("", 1)),
                ("A" * 41, ("", 1)),
                ("w " + "9" * 39 + " w", ("w " + "9" * 39 + " w", 0)),
            )
        )

    def test_the_allowed_characters(self):
        self.check(
            (
                ("k " + "aB3+/=_-" * 5, ("k", 1)),
                (
                    "k " + "a" * 20 + "." + "a" * 20,
                    ("k " + "a" * 20 + "." + "a" * 20, 0),
                ),
                (
                    "k " + "a" * 20 + " " + "a" * 20,
                    ("k " + "a" * 20 + " " + "a" * 20, 0),
                ),
                ("k " + "a" * 39 + "é", ("k " + "a" * 39 + "é", 0)),
            )
        )

    def test_separate_runs_are_counted_separately(self):
        self.check(
            (
                ("a " + "B" * 40 + " b " + "c" * 50 + " d", ("a b d", 2)),
                ("", ("", 0)),
                ("short words only", ("short words only", 0)),
            )
        )


class GeneralizeVersionsTest(Table, unittest.TestCase):
    function_name = "generalize_versions"

    def test_docstring_examples(self):
        self.check(
            (
                ("python 3.13.15", ("python 3.13", 1)),
                ("fastapi 0.141.1 and v2.0.1", ("fastapi 0.141 and v2.0", 2)),
                ("1.2.3.4", ("1.2", 1)),
                ("1.2.3-beta", ("1.2-beta", 1)),
                ("3.13 abc1.2.3 1.2.3rc1", ("3.13 abc1.2.3 1.2.3rc1", 0)),
            )
        )

    def test_more_versions(self):
        self.check(
            (
                ("10.0.19045.3803", ("10.0", 1)),
                ("1.2.3.4.5", ("1.2", 1)),
                ("version 1.2.3, then 4.5.6", ("version 1.2, then 4.5", 2)),
                ("1.2.3_beta", ("1.2_beta", 1)),
                ("1.2.3.", ("1.2.", 1)),
                ("(1.2.3)", ("(1.2)", 1)),
                ("v10.20.30", ("v10.20", 1)),
                ("0.0.0", ("0.0", 1)),
            )
        )

    def test_texts_that_are_not_versions(self):
        self.check(
            (
                ("", ("", 0)),
                ("3.13", ("3.13", 0)),
                ("1.2", ("1.2", 0)),
                ("x1.2.3", ("x1.2.3", 0)),
                ("1.2.34x", ("1.2.34x", 0)),
                ("1a.2.3", ("1a.2.3", 0)),
                ("plain words", ("plain words", 0)),
                ("1.x.3", ("1.x.3", 0)),
            )
        )


if __name__ == "__main__":
    unittest.main()
