"""An answer is returned whole or refused: it is never silently shortened.

``redact_text`` cuts a text at its own limit (``tools.credentials.MAX_TEXT_CHARS``,
1,000,000 characters) and appends ``[TRUNCATED]``. The adapter answer limit used to be
2,000,000, so an answer between the two passed the validation and came back cut, as a
successful call. The two limits are now one: an answer that the redaction could not
read completely (as the adapter returned it, or after the exact credential value was
replaced) is an ``invalid_response``.
"""

import logging
import unittest

from paw_backend.connections import (
    AdapterResult,
    ConnectionCallError,
    FailureCode,
    InvalidConnectionInputError,
    limits,
)
from paw_backend.tools.credentials import MAX_TEXT_CHARS, TRUNCATED

from .connections_fakes import CANARY, handle
from .connections_support import requires_postgres
from .test_connections_execute import ExecuteCase

LIMIT = 1_000_000


class LimitsTest(unittest.TestCase):
    def test_the_answer_limit_is_the_limit_of_the_redaction(self):
        self.assertEqual(limits.MAX_RESULT_CHARS, MAX_TEXT_CHARS)
        self.assertEqual(limits.MAX_RESULT_CHARS, LIMIT)

    def test_an_adapter_result_over_the_limit_cannot_be_built(self):
        self.assertEqual(len(AdapterResult("x" * LIMIT).text), LIMIT)
        with self.assertRaises(InvalidConnectionInputError) as caught:
            AdapterResult("x" * (LIMIT + 1))
        self.assertEqual(caught.exception.field, "text")


@requires_postgres
class ResultLimitTest(ExecuteCase):
    async def refused(self):
        with self.assertLogs("paw_backend.connections", logging.WARNING):
            with self.assertRaises(ConnectionCallError) as caught:
                await self.call()
        return caught.exception

    async def test_an_answer_of_exactly_the_limit_is_returned_whole(self):
        self.codex.text = "x" * LIMIT
        result = await self.call()
        self.assertEqual(len(result.text), LIMIT)
        self.assertEqual(result.text, "x" * LIMIT)
        self.assertNotIn(TRUNCATED, result.text)
        self.assertEqual(result.redactions, 0)

    async def test_an_answer_over_the_limit_is_an_invalid_response_never_cut(self):
        answer = AdapterResult("ok", 1, 1)
        object.__setattr__(answer, "text", "x" * (LIMIT + 1))  # forced in
        self.codex.result = answer
        error = await self.refused()
        self.assertEqual(error.failure, FailureCode.INVALID_RESPONSE)
        (row,) = self.usage_rows()
        self.assertEqual(
            (row["status"], row["failure_code"]), ("failed", "invalid_response")
        )

    async def test_an_answer_that_the_credential_scrub_makes_too_long_is_refused(self):
        # A one-character credential is replaced by ten characters each time: an
        # answer that was within the limit is no longer, and would have been cut.
        self.resolver.secrets[handle(1)] = "a"
        self.codex.text = "a" * 200_000
        error = await self.refused()
        self.assertEqual(error.failure, FailureCode.INVALID_RESPONSE)

    async def test_an_answer_that_ends_in_the_marker_text_is_not_mistaken_for_a_cut(
        self,
    ):
        self.codex.text = "the end " + TRUNCATED
        result = await self.call()
        self.assertEqual(result.text, "the end " + TRUNCATED)

    async def test_a_large_answer_with_a_credential_in_it_is_scrubbed_whole(self):
        self.codex.text = "x" * 400_000 + CANARY + "y" * 400_000
        result = await self.call()
        self.assertNotIn(CANARY, result.text)
        self.assertEqual(len(result.text), 800_000 + len("[REDACTED]"))
        self.assertNotIn(TRUNCATED, result.text)


if __name__ == "__main__":
    unittest.main()
