"""``rules.py``: properties that must hold for every abstraction rule.

Each rule takes collapsed text and returns ``(text, count)``. On a fixed corpus
(and on hostile, maximum-size inputs) every rule must return quickly, never
lengthen the text, report 0 exactly when it changes nothing, and give the same
answer when it is applied to its own output. This module also checks, on the
source of ``rules.py``, the mistakes that weaker implementations tend to make.
"""

import ast
import pathlib
import time
import unittest

from paw_backend.research.privacy import rules
from paw_backend.research.privacy.contract import MAX_DRAFT_CHARS

LOOSE_DEADLINE_SECONDS = 30.0

TEXT_RULES = (
    "strip_credentials",
    "abstract_urls",
    "abstract_emails",
    "abstract_paths",
    "abstract_hosts",
    "abstract_ids",
    "drop_opaque_tokens",
    "generalize_versions",
)

CORPUS = (
    "",
    "python asyncio timeout",
    "read https://docs.python.org/3/library/asyncio.html for details",
    "open http://192.168.1.10:8080/admin and http://localhost:3000/x",
    "contact tom.k+dev@example.co.jp today",
    "open /etc/passwd now and see ~/notes.txt or ./run.sh",
    "edit src/app/main.py --config=/etc/app.conf C:\\Users\\tom\\x.txt",
    "connect to db.internal:5432 from 10.0.0.7, then use localhost:8080/health",
    "user 123e4567-e89b-12d3-a456-426614174000 ticket 1234567 commit deadbeefcafe12",
    "key " + "A" * 40 + " end and " + "b" * 45,
    "python 3.13.15 fastapi 0.141.1 v2.0.1 1.2.3.4 3.13",
    "use ghp_" + "a" * 36 + " with AKIAIOSFODNN7EXAMPLE and password=hunter2hunter2",
    "検索クエリ の 書き方 と 使い方",
    "a@b.c user@localhost @octocat 1/2 I/O TCP/IP and/or",
    "pi is 3.14159265 and 12345.678 and 0.12345678",
    "mixed https://a.io/x mail a@x.org path /a/b ip 1.2.3.4 id 99999 v1.2.3",
    "route fd12:3456:789a::/48 and [fe80::1%eth0/64], 10.0.0.0/8 ::/0",
    "not a network: a::b/c 10:30/12:00 fd12::/x aa:bb:cc:dd:ee:ff/48",
    "((( [[[ {{{ <<< ))) ]]] }}} >>>",
    ". , ; : ! ? \" ' - _ = + / \\",
)


def percent_shapes(size):
    """Hostile tokens for names that contain "%" (``rules._percent_host``).

    Each is one token of about ``size`` characters. The one that ends in ``^`` is
    rejected by the first pattern; the others reach the name and zone patterns, and
    are accepted or rejected only after the whole token was read.
    """
    return {
        "escaped label": "%41" * (size // 3),
        "letters around an escape": "a" * (size // 2) + "%41" + "a" * (size // 2 - 3),
        "escaped labels": "a." + "%41" * (size // 3) + "..",
        "escaped labels rejected": "a." + "%41" * (size // 3) + "^",
        "escaped dots": "a" + "%2e" * (size // 3),
        "double escaped dots": "a" + "%252e" * (size // 5),
        "escaped dots between labels": "a%2e%41" * (size // 7) + "..",
        "escaped names": "%41." * (size // 4) + ".",
        "percent labels": "a.%" * (size // 3),
        "percent dots": "%." * (size // 2),
        "zone names": "a.b%" + "a" * (size - 4),
        "dotted zone names": "a.b%" + "a." * (size // 2 - 2) + "%",
        "escaped zone names": "a.b" + "%41" * (size // 3 - 1) + "%zz",
        "escaped userinfo": "%41" * (size // 3 - 1) + "@",
        "escaped ports": "a.%41" * (size // 5 - 1) + ":1",
    }


HOSTILE = {
    "letters": "a" * MAX_DRAFT_CHARS,
    "digits": "1" * MAX_DRAFT_CHARS,
    "dots": "a." * (MAX_DRAFT_CHARS // 2),
    "ats": "a@" * (MAX_DRAFT_CHARS // 2),
    "slashes": "/" * MAX_DRAFT_CHARS,
    "segments": "a/" * (MAX_DRAFT_CHARS // 2),
    "schemes": "http://" * (MAX_DRAFT_CHARS // 7),
    "hyphens": "-" * MAX_DRAFT_CHARS,
    "hex tail": "0" * (MAX_DRAFT_CHARS - 1) + "x",
    "words": "ab " * (MAX_DRAFT_CHARS // 3),
    "brackets": "[::" * (MAX_DRAFT_CHARS // 3),
    "labels": "a.b" * (MAX_DRAFT_CHARS // 3),
    "openers": "(" * MAX_DRAFT_CHARS,
    "versions": "1.2." * (MAX_DRAFT_CHARS // 4),
    "colons": "a:" * (MAX_DRAFT_CHARS // 2),
    "mail-like": "a.b@c" * (MAX_DRAFT_CHARS // 5),
    "punctuation": ".,;:!?)]}>\"'" * (MAX_DRAFT_CHARS // 12),
    "open bracket colons": "[" + ":" * (MAX_DRAFT_CHARS - 1),
    "open bracket hex": "[" + "a:" * (MAX_DRAFT_CHARS // 2),
    "zone": "[::1%" + "a" * (MAX_DRAFT_CHARS - 5),
    "zones": "[::1%a]" * (MAX_DRAFT_CHARS // 7),
    "percents": "%" * MAX_DRAFT_CHARS,
    "bare colons": ":" * MAX_DRAFT_CHARS,
    "bare hex colons": "a:" * (MAX_DRAFT_CHARS // 2),
    "userinfo": "a" * (MAX_DRAFT_CHARS - 1) + "@",
    "userinfo dots": "a." * (MAX_DRAFT_CHARS // 2 - 1) + "@a.b",
    "absolute": "a." * (MAX_DRAFT_CHARS // 2 - 1) + ".:1",
    "ports": "a.b:1" * (MAX_DRAFT_CHARS // 5),
    "hex double colons": "a::" * (MAX_DRAFT_CHARS // 3),
    "hex then colons": "f" * (MAX_DRAFT_CHARS - 3) + ":::",
    "cidr groups": "a:" * (MAX_DRAFT_CHARS // 2 - 2) + "/64",
    "cidr open bracket": "[" + ":" * (MAX_DRAFT_CHARS - 5) + "/64]",
    "cidr zone": "[::1%" + "a" * (MAX_DRAFT_CHARS - 9) + "/64]",
    "cidr digits": "::/" + "1" * (MAX_DRAFT_CHARS - 3),
    "cidr slashes": "a::b" + "/1" * (MAX_DRAFT_CHARS // 2 - 2),
    "cidr words": "a::b/64 " * (MAX_DRAFT_CHARS // 8),
    **percent_shapes(MAX_DRAFT_CHARS),
}

# Each shape again as ONE token, far longer than a draft may be. Linear time is a
# few milliseconds here; quadratic time is minutes, so the deadline can be loose.
LONG_TOKEN_DEADLINE_SECONDS = 10.0
LONG = 400_000
LONG_TOKENS = {
    "open bracket colons": "[" + ":" * LONG,
    "open bracket hex": "[" + "a:" * (LONG // 2),
    "zone": "[::1%" + "a" * LONG,
    "bare colons": ":" * LONG,
    "bare hex colons": "a:" * (LONG // 2),
    "userinfo": "a" * LONG + "@",
    "userinfo dots": "a." * (LONG // 2) + "@a.b",
    "absolute": "a." * (LONG // 2) + ".:1",
    "labels": "a.b" * (LONG // 3),
    "hex then colons": "f" * LONG + ":::",
    "groups then colons": "a:" * (LONG // 2) + "::",
    "hex double colons": "a::" * (LONG // 3),
    "percents": "%" * LONG,
    "ats": "a@" * (LONG // 2),
    "cidr groups": "a:" * (LONG // 2) + "/64",
    "cidr groups without a prefix": "a:" * (LONG // 2) + "/",
    "cidr open bracket": "[" + ":" * LONG + "/64]",
    "cidr zone": "[::1%" + "a" * LONG + "/64]",
    "cidr digits": "::/" + "1" * LONG,
    "cidr bad digits": "::/" + "1" * LONG + "x",
    "cidr slashes": "a::b" + "/1" * (LONG // 2),
    "cidr brackets": "[" * LONG + "a::/64",
    **percent_shapes(LONG),
}


class RuleProperties(unittest.TestCase):
    def test_every_rule_returns_text_and_a_count(self):
        for name in TEXT_RULES:
            function = getattr(rules, name)
            for text in CORPUS:
                with self.subTest(rule=name, text=text[:40]):
                    result = function(text)
                    self.assertIsInstance(result, tuple)
                    self.assertEqual(len(result), 2)
                    out, count = result
                    self.assertIsInstance(out, str)
                    self.assertIsInstance(count, int)
                    self.assertNotIsInstance(count, bool)
                    self.assertGreaterEqual(count, 0)

    def test_a_rule_never_lengthens_the_text_and_returns_collapsed_text(self):
        for name in TEXT_RULES:
            function = getattr(rules, name)
            for text in CORPUS:
                with self.subTest(rule=name, text=text[:40]):
                    out, count = function(text)
                    self.assertLessEqual(len(out), len(text))
                    if count:
                        self.assertEqual(out, " ".join(out.split()))

    def test_the_count_is_zero_exactly_when_nothing_changed(self):
        for name in TEXT_RULES:
            function = getattr(rules, name)
            for text in CORPUS:
                with self.subTest(rule=name, text=text[:40]):
                    out, count = function(text)
                    self.assertEqual(count == 0, out == text)

    def test_a_rule_is_idempotent(self):
        for name in TEXT_RULES:
            function = getattr(rules, name)
            for text in CORPUS:
                with self.subTest(rule=name, text=text[:40]):
                    once = function(text)[0]
                    self.assertEqual(function(once), (once, 0))

    def test_no_rule_is_slow_on_maximum_size_hostile_input(self):
        for name in TEXT_RULES:
            function = getattr(rules, name)
            for label, text in HOSTILE.items():
                with self.subTest(rule=name, shape=label):
                    started = time.monotonic()
                    out, count = function(text)
                    elapsed = time.monotonic() - started
                    self.assertLess(elapsed, LOOSE_DEADLINE_SECONDS)
                    self.assertLessEqual(len(out), len(text))
                    self.assertGreaterEqual(count, 0)

    def test_the_host_rules_take_linear_time_on_one_very_long_token(self):
        for label, text in LONG_TOKENS.items():
            with self.subTest(shape=label):
                started = time.monotonic()
                rules.abstract_hosts(text)
                rules.abstract_urls("http://" + text)
                rules.is_private_host(text)
                self.assertLess(time.monotonic() - started, LONG_TOKEN_DEADLINE_SECONDS)

    def test_the_helpers_are_fast_on_hostile_input_too(self):
        for label, text in HOSTILE.items():
            with self.subTest(shape=label):
                started = time.monotonic()
                rules.normalize_text(text)
                rules.fold_for_match(text)
                rules.find_copied_spans(text, text, window=16)
                rules.truncate_query(" ".join(text.split()), 256)
                rules.is_private_host(text)
                self.assertLess(time.monotonic() - started, LOOSE_DEADLINE_SECONDS)


class RulesSourceTest(unittest.TestCase):
    """Static checks of the mistakes that are easy to make in ``rules.py``."""

    @classmethod
    def setUpClass(cls):
        path = pathlib.Path(rules.__file__)
        cls.tree = ast.parse(path.read_text(encoding="utf-8"))

    def test_no_broad_except(self):
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            self.assertIsNotNone(node.type, "a bare except hides bugs")
            names = (
                [n.id for n in ast.walk(node.type) if isinstance(n, ast.Name)]
                if node.type is not None
                else []
            )
            for name in ("Exception", "BaseException"):
                self.assertNotIn(name, names, f"except {name} hides bugs")

    def test_no_printing_logging_or_io(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(
                    node.func.id, {"print", "open", "input", "eval", "exec"}
                )
            if isinstance(node, ast.Import):
                modules = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                modules = {(node.module or "").split(".")[0]}
            else:
                continue
            self.assertFalse(
                modules
                & {"logging", "os", "sys", "subprocess", "socket", "requests", "httpx"},
                f"unexpected import {sorted(modules)}",
            )

    def test_no_mutable_default_arguments(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef):
                for default in node.args.defaults + node.args.kw_defaults:
                    if default is not None:
                        self.assertNotIsInstance(default, ast.List | ast.Dict | ast.Set)

    def test_the_specified_functions_keep_their_names_and_signatures(self):
        expected = {
            "normalize_text": ["text"],
            "fold_for_match": ["text"],
            "find_copied_spans": ["text", "source", "window"],
            "strip_credentials": ["text"],
            "is_private_host": ["host"],
            "abstract_urls": ["text"],
            "abstract_emails": ["text"],
            "abstract_paths": ["text"],
            "abstract_hosts": ["text"],
            "abstract_ids": ["text"],
            "drop_opaque_tokens": ["text"],
            "generalize_versions": ["text"],
            "truncate_query": ["text", "max_chars"],
            "query_fingerprint": ["query"],
        }
        found = {}
        for node in self.tree.body:
            if isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
                args = node.args
                found[node.name] = [
                    a.arg for a in args.posonlyargs + args.args + args.kwonlyargs
                ]
        for name, arguments in expected.items():
            self.assertEqual(found.get(name), arguments, name)


if __name__ == "__main__":
    unittest.main()
