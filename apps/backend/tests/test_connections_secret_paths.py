"""The credential in every form, through ``execute`` and every place text can go.

Answer path: the answer is scrubbed of the value, redacted, scrubbed again, and refused
(``invalid_response``, the token counts kept) when the value can still be read in it.
The scrub follows a normalisation that works on single characters and clusters; a
composition ACROSS starter characters (the two Oriya vowel signs that NFC joins into
one, or a Hangul syllable written as jamo) is followed by the whole-text check, not by
the scrub. Other paths (log, audit, usage row, errors): none of them carries the text of
an answer or of an exception, so no obfuscated form can reach them; the canary tests
below look for the value in every form in all of them.
"""

import logging
import unicodedata
import unittest

from paw_backend.connections import (
    AdapterFailure,
    ConnectionCallError,
    FailureCode,
    Secret,
)
from paw_backend.connections.secret import fold

from .connections_fakes import CANARY
from .connections_support import requires_postgres
from .test_connections_execute import ExecuteCase

# "key" + U+0B4B (ORIYA VOWEL SIGN O) + "99": NFC joins U+0B47 U+0B3E into U+0B4B.
SPLIT_SECRET = "keyୋ99"
SPLIT_FORM = "keyୋ99"
FULL_WIDTH = "ＡＢＣ１２３"


class PreconditionTest(unittest.TestCase):
    def test_the_example_is_a_composition_across_starter_characters(self):
        self.assertEqual(unicodedata.normalize("NFKC", SPLIT_FORM), SPLIT_SECRET)
        self.assertEqual(fold(SPLIT_FORM), fold(SPLIT_SECRET))
        # ... which the single-cluster view of the scrub does not see.
        secret = Secret(SPLIT_SECRET)
        self.assertEqual(secret.scrub(f"a {SPLIT_FORM} b"), f"a {SPLIT_FORM} b")
        self.assertTrue(secret.visible_in(SPLIT_FORM))


@requires_postgres
class AnswerPathTest(ExecuteCase):
    async def answer(self, text: str, secret: str):
        self.resolver.secrets[self.handle_of_codex()] = secret
        self.codex.text = text
        return await self.call()

    def handle_of_codex(self) -> str:
        return self.scalar("SELECT secret_handle FROM shared_connections")

    async def refused(self, text: str, secret: str) -> None:
        self.resolver.secrets[self.handle_of_codex()] = secret
        self.codex.text = text
        with self.assertLogs("paw_backend.connections", logging.WARNING):
            with self.assertRaises(ConnectionCallError) as caught:
                await self.call()
        self.assertEqual(caught.exception.failure, FailureCode.INVALID_RESPONSE)

    async def test_the_full_width_value_next_to_a_pattern_is_not_returned(self):
        result = await self.answer("token=abcdef " + FULL_WIDTH, "ABC123")
        self.assertNotIn("ABC123", result.text)
        self.assertFalse(Secret("ABC123").visible_in(result.text))
        self.assertEqual(result.text, "token=[REDACTED] [REDACTED]")

    async def test_the_full_width_value_alone_is_removed_in_place(self):
        result = await self.answer(f"my key: {FULL_WIDTH}.", "ABC123")
        self.assertEqual(result.text, "my key: [REDACTED].")

    async def test_mixed_case_and_compatibility_characters_are_removed(self):
        for form in ("abc123", "AbC123", "ＡbＣ1２3", "AB​C‍123"):
            with self.subTest(form=repr(form)):
                result = await self.answer(f"[{form}] token=abcdef", "ABC123")
                self.assertFalse(Secret("ABC123").visible_in(result.text))
                self.assertEqual(result.text, "[[REDACTED]] token=[REDACTED]")

    async def test_a_composition_across_characters_next_to_a_pattern_is_scrubbed_again(
        self,
    ):
        # The redaction normalises the text and JOINS the two vowel signs into the
        # value: the scrub that runs after the redaction removes it.
        result = await self.answer(f"token=abcdef {SPLIT_FORM}", SPLIT_SECRET)
        self.assertFalse(Secret(SPLIT_SECRET).visible_in(result.text))
        self.assertNotIn(SPLIT_SECRET, result.text)
        self.assertEqual(result.text, "token=[REDACTED] [REDACTED]")

    async def test_a_composition_the_scrub_cannot_follow_is_refused_not_returned(self):
        await self.refused(f"the key is {SPLIT_FORM}", SPLIT_SECRET)
        (row,) = self.usage_rows()
        self.assertEqual(
            (row["status"], row["failure_code"]), ("failed", "invalid_response")
        )
        # The provider consumed the tokens: they are kept.
        self.assertEqual((row["input_tokens"], row["output_tokens"]), (10, 5))

    async def test_a_marker_that_contains_the_value_is_refused(self):
        # "RED" is inside "[REDACTED]": the answer cannot be made free of it.
        await self.refused("a RED b", "RED")

    async def test_an_answer_without_the_value_is_returned_unchanged(self):
        result = await self.answer("nothing to hide ＡＢＣ １２３", "ABC123")
        self.assertEqual(result.text, "nothing to hide ＡＢＣ １２３")


@requires_postgres
class OtherPathsTest(ExecuteCase):
    """The value, in every form, is nowhere but in the adapter."""

    FORMS = (
        CANARY,
        CANARY.upper(),
        "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else c for c in CANARY),
    )

    def seen(self, secret: Secret, haystack: str) -> bool:
        return secret.visible_in(haystack) or any(f in haystack for f in self.FORMS)

    async def everything(self) -> str:
        parts = [self.everything_stored()]
        parts += [repr(event) for event in self.sink.events]
        parts.append(repr(await self.service.availability(self.principal(self.user))))
        parts.append(
            repr(await self.service.list_usage(self.principal(self.user), self.user))
        )
        return "\n".join(parts)

    async def test_an_adapter_that_raises_the_value_in_any_form_leaves_no_trace(self):
        secret = Secret(CANARY)
        for form in self.FORMS:
            with self.subTest(form=form[:24]):
                self.codex.error = RuntimeError(f"provider said {form}")
                with self.assertLogs("paw_backend", logging.DEBUG) as logs:
                    logging.getLogger("paw_backend").debug("start")
                    with self.assertRaises(ConnectionCallError) as caught:
                        await self.call()
                haystack = "\n".join(logs.output) + repr(caught.exception)
                haystack += str(caught.exception) + repr(caught.exception.args)
                haystack += await self.everything()
                self.assertFalse(self.seen(secret, haystack))
                self.assertIsNone(caught.exception.__cause__)
                self.assertIsNone(caught.exception.__context__)

    async def test_a_resolver_that_fails_with_the_value_in_any_form_leaves_no_trace(
        self,
    ):
        secret = Secret(CANARY)
        for form in self.FORMS:
            with self.subTest(form=form[:24]):
                self.resolver.error = KeyError(f"no such handle {form}")
                with self.assertLogs("paw_backend", logging.DEBUG) as logs:
                    logging.getLogger("paw_backend").debug("start")
                    with self.assertRaises(ConnectionCallError):
                        await self.call()
                haystack = "\n".join(logs.output) + await self.everything()
                self.assertFalse(self.seen(secret, haystack))

    async def test_a_classified_failure_carries_a_code_and_nothing_else(self):
        self.codex.error = AdapterFailure(FailureCode.RATE_LIMITED)
        with self.assertLogs("paw_backend", logging.DEBUG) as logs:
            logging.getLogger("paw_backend").debug("start")
            with self.assertRaises(ConnectionCallError) as caught:
                await self.call()
        self.assertEqual(str(caught.exception), "The call failed: rate_limited")
        self.assertFalse(self.seen(Secret(CANARY), "\n".join(logs.output)))

    async def test_the_answer_with_the_value_in_any_form_is_not_in_the_usage_or_audit(
        self,
    ):
        secret = Secret(CANARY)
        for form in self.FORMS:
            with self.subTest(form=form[:24]):
                self.codex.text = f"echo {form} token=abcdef"
                result = await self.call()
                self.assertFalse(self.seen(secret, result.text + repr(result)))
                self.assertFalse(self.seen(secret, await self.everything()))


if __name__ == "__main__":
    unittest.main()
