"""``Secret.scrub`` and ``Secret.visible_in``: the value must not survive any order of
normalisation and redaction.

``redact_text`` normalises the WHOLE text (removes format characters such as the
zero-width space, then NFKC) whenever it finds a recognisable credential pattern. A
credential that the exact-value scrub could not see (``ＡＢＣ１２３`` in full-width
characters) can become the exact value (``ABC123``) in that step and would then be
returned. The scrub therefore works on the normalised, case-folded view of the text as
well, and is applied again after the redaction; ``visible_in`` is the check that
nothing is left. The test at the end applies random obfuscations, in every order, to
random credentials.
"""

import random
import unicodedata
import unittest

from paw_backend.connections.secret import REDACTED_SECRET, Secret, fold
from paw_backend.tools.credentials import redact_text

from .connections_fakes import CANARY

FULL_WIDTH_SHIFT = 0xFEE0
ZERO_WIDTH = ("​", "‌", "‍", "⁠", "﻿", "­")


def full_width(text: str) -> str:
    """The printable ASCII characters of ``text`` as their full-width forms."""
    return "".join(
        chr(ord(c) + FULL_WIDTH_SHIFT) if "!" <= c <= "~" else c for c in text
    )


def sanitise(secret: Secret, text: str) -> str:
    """What the service does with an answer: scrub, redact patterns, scrub again."""
    text = secret.scrub(text)
    text, _ = redact_text(text)
    return secret.scrub(text)


class FoldTest(unittest.TestCase):
    def test_full_width_and_case_and_zero_width_characters_fold_together(self):
        for obfuscated in (
            "ABC123",
            "abc123",
            "ＡＢＣ１２３",
            "ａｂｃ１２３",
            "AB​C‍123",
            "﻿ＡBC１23",
            "a­bc123",
        ):
            with self.subTest(obfuscated=repr(obfuscated)):
                self.assertEqual(fold(obfuscated), "abc123")

    def test_the_fold_is_idempotent(self):
        for text in ("ＡＢＣ１２３", "Straße", "ﬁnal", "ｶﾞｷﾞ", "é", "한"):
            with self.subTest(text=text):
                self.assertEqual(fold(fold(text)), fold(text))

    def test_the_format_characters_of_the_table_are_all_of_them(self):
        from paw_backend.connections.secret import format_characters

        every = {cp for cp in range(0x110000) if unicodedata.category(chr(cp)) == "Cf"}
        self.assertEqual(set(format_characters()), every)


class ScrubExamplesTest(unittest.TestCase):
    def setUp(self):
        self.secret = Secret("ABC123")

    def test_the_exact_value_is_replaced(self):
        self.assertEqual(
            self.secret.scrub("a ABC123 b ABC123"),
            f"a {REDACTED_SECRET} b {REDACTED_SECRET}",
        )

    def test_the_full_width_form_is_replaced_in_place(self):
        self.assertEqual(
            self.secret.scrub("key: ＡＢＣ１２３ (copy)"),
            f"key: {REDACTED_SECRET} (copy)",
        )

    def test_mixed_case_and_compatibility_characters_are_replaced(self):
        for form in ("abc123", "AbC123", "ＡbＣ1２3", "ａｂｃ123"):
            with self.subTest(form=form):
                self.assertEqual(self.secret.scrub(f"<{form}>"), f"<{REDACTED_SECRET}>")

    def test_zero_width_characters_inside_the_value_do_not_hide_it(self):
        for form in ("AB​C123", "A‍B⁠C﻿123", "ＡＢ​Ｃ１２３"):
            with self.subTest(form=repr(form)):
                self.assertEqual(self.secret.scrub(f"[{form}]"), f"[{REDACTED_SECRET}]")

    def test_a_text_without_the_value_is_returned_unchanged(self):
        for text in ("nothing", "ＡＢＣ１２", "ABC12 3", "", "日本語のテキスト"):
            with self.subTest(text=text):
                self.assertEqual(self.secret.scrub(text), text)

    def test_the_value_in_japanese_text_is_replaced_and_the_text_is_kept(self):
        text = "これは ＡＢＣ１２３ です。ＡＢＣ　のまま。"
        self.assertEqual(
            self.secret.scrub(text), f"これは {REDACTED_SECRET} です。ＡＢＣ　のまま。"
        )

    def test_overlapping_occurrences_are_all_replaced(self):
        secret = Secret("ABA")
        result = secret.scrub("ABABA")
        self.assertFalse(secret.visible_in(result))
        self.assertEqual(result, REDACTED_SECRET)

    def test_a_secret_with_a_compatibility_character_is_found_in_its_plain_form(self):
        secret = Secret("ＸＹＺ９８７")
        self.assertEqual(
            secret.scrub("xyz987 XYZ987"), f"{REDACTED_SECRET} {REDACTED_SECRET}"
        )

    def test_the_canary_of_the_tests_in_every_form(self):
        secret = Secret(CANARY)
        for form in (
            CANARY,
            CANARY.upper(),
            full_width(CANARY),
            CANARY.replace("-", "​-"),
        ):
            with self.subTest(form=form[:30]):
                self.assertNotIn("canary", fold(secret.scrub(f"x {form} y")))


class ThePipelineTest(unittest.TestCase):
    """The failure that was found: the redaction CREATES the exact value."""

    def test_the_full_width_value_next_to_a_pattern_does_not_come_back_exact(self):
        secret = Secret("ABC123")
        answer = "token=abcdef ＡＢＣ１２３"
        # The redaction alone normalises the whole text: the value appears.
        redacted, count = redact_text(answer)
        self.assertGreaterEqual(count, 1)
        self.assertIn("ABC123", redacted)
        # The scrub before AND after it removes it.
        out = sanitise(secret, answer)
        self.assertNotIn("ABC123", out)
        self.assertFalse(secret.visible_in(out))
        self.assertEqual(out, f"token=[REDACTED] {REDACTED_SECRET}")

    def test_a_value_split_by_format_characters_is_joined_by_the_redaction_and_removed(
        self,
    ):
        secret = Secret("ABC123")
        answer = "password=hunter22 AB​C‍1​23"
        out = sanitise(secret, answer)
        self.assertFalse(secret.visible_in(out))
        self.assertNotIn("ABC123", out)

    def test_the_answer_without_the_value_only_gets_the_pattern_redaction(self):
        secret = Secret("ABC123")
        out = sanitise(secret, "token=abcdef fine")
        self.assertEqual(out, "token=[REDACTED] fine")

    def test_the_scrub_is_idempotent(self):
        secret = Secret("ABC123")
        for text in (
            "a ABC123 b",
            "ＡＢＣ１２３ ＡＢＣ１２３",
            "token=abcdef ＡＢＣ１２３",
            "AB​C123 abc123",
            "no secret",
            "",
        ):
            with self.subTest(text=text):
                once = secret.scrub(text)
                self.assertEqual(secret.scrub(once), once)
                twice = sanitise(secret, once)
                self.assertEqual(sanitise(secret, twice), twice)


class VisibleInTest(unittest.TestCase):
    def test_every_form_is_visible_and_the_scrubbed_text_is_not(self):
        secret = Secret("ABC123")
        for form in ("ABC123", "abc123", "ＡＢＣ１２３", "A​BC123", "xx ABC123 yy"):
            with self.subTest(form=repr(form)):
                self.assertTrue(secret.visible_in(form))
                self.assertFalse(secret.visible_in(secret.scrub(form)))

    def test_other_text_is_not_visible(self):
        secret = Secret("ABC123")
        for text in ("", "ABC12", "ABC 123", "BC123", "hello"):
            with self.subTest(text=text):
                self.assertFalse(secret.visible_in(text))

    def test_a_marker_that_contains_the_value_is_reported_not_hidden(self):
        # "RED" is inside "[REDACTED]": the scrub cannot make it invisible, and says so.
        secret = Secret("RED")
        self.assertTrue(secret.visible_in(secret.scrub("a RED b")))


class RandomObfuscationTest(unittest.TestCase):
    """Random credentials, hidden in random ways, in every order of the steps.

    Deterministic (a fixed seed), stdlib only. Whatever the order of the scrub, the
    redaction of patterns and the scrub again, the credential is not visible in the
    result, in any of the forms that a normalisation can turn into it.
    """

    ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
    FILLER = (
        "the answer is: ",
        "これは答えです。",
        "token=abcdef ",
        "Authorization: Bearer abcdefghijklmnop1234 ",
        "password = hunter22 ",
        "ＡＢＣ　１２３ ",
        "​",
        "\n",
        "```\n",
    )

    def obfuscate(self, rng: random.Random, value: str) -> str:
        pieces = []
        for char in value:
            choice = rng.random()
            if choice < 0.25 and "!" <= char <= "~":
                char = chr(ord(char) + FULL_WIDTH_SHIFT)
            elif choice < 0.5:
                char = char.swapcase()
            pieces.append(char)
            if rng.random() < 0.15:
                pieces.append(rng.choice(ZERO_WIDTH))
        return "".join(pieces)

    def random_case(self, rng: random.Random):
        value = "".join(rng.choice(self.ALPHABET) for _ in range(rng.randint(8, 24)))
        secret = Secret(value)
        parts = []
        for _ in range(rng.randint(1, 4)):
            parts.append(
                "".join(rng.choice(self.FILLER) for _ in range(rng.randint(0, 3)))
            )
            parts.append(self.obfuscate(rng, value) if rng.random() < 0.8 else value)
        parts.append(rng.choice(self.FILLER))
        return secret, "".join(parts)

    def test_the_credential_is_never_visible_in_the_result_of_any_order(self):
        rng = random.Random(30)
        for case in range(400):
            secret, answer = self.random_case(rng)
            with self.subTest(case=case, answer=answer[:60]):
                orders = {
                    "scrub": secret.scrub(answer),
                    "scrub-redact-scrub": sanitise(secret, answer),
                    "scrub-scrub": secret.scrub(secret.scrub(answer)),
                    "redact-scrub": secret.scrub(redact_text(answer)[0]),
                    "redact-scrub-redact-scrub": secret.scrub(
                        redact_text(secret.scrub(redact_text(answer)[0]))[0]
                    ),
                }
                for order, out in orders.items():
                    self.assertFalse(secret.visible_in(out), (order, out[:80]))
                    self.assertNotIn(secret.reveal(), out, order)
                    self.assertNotIn(fold(secret.reveal()), fold(out), order)

    def test_the_result_is_stable_under_a_second_application(self):
        rng = random.Random(31)
        for case in range(300):
            secret, answer = self.random_case(rng)
            with self.subTest(case=case):
                once = sanitise(secret, answer)
                self.assertEqual(sanitise(secret, once), once)
                scrubbed = secret.scrub(answer)
                self.assertEqual(secret.scrub(scrubbed), scrubbed)

    def test_text_that_never_held_the_credential_is_left_alone_by_the_scrub(self):
        rng = random.Random(32)
        for case in range(200):
            secret, _ = self.random_case(rng)
            clean = "".join(rng.choice(self.FILLER) for _ in range(rng.randint(1, 6)))
            if secret.visible_in(clean):  # a filler that happens to hold it
                continue
            with self.subTest(case=case):
                self.assertEqual(secret.scrub(clean), clean)


if __name__ == "__main__":
    unittest.main()
