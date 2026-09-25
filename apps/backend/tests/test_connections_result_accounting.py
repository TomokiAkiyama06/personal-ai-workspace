"""An answer that cannot be returned is still accounted, and the limit is on what is
returned.

Two findings of the review of ``execute`` (PAW-030):

* ``redact_text`` can EXPAND a text (``token=abcdef`` becomes ``token=[REDACTED]``),
  so an answer within the limit as the adapter returned it can be over it after the
  patterns were redacted. The length is checked again after the redaction: an answer is
  returned whole or refused (``invalid_response``), never longer than the limit.
* When the answer is refused for its body (too long after the exact credential value
  was replaced, or after the patterns were), the provider still consumed the tokens the
  adapter reported. The counts are validated on their own and kept: the usage row and
  the task's token budget use them. Counts that are not valid are not kept.
"""

import logging
import unittest

from sqlalchemy import text

from paw_backend.connections import (
    AdapterResult,
    ConnectionCallError,
    FailureCode,
    QuotaMetric,
    QuotaPeriod,
)
from paw_backend.tasks.queueing import BudgetKind, BudgetPreset
from paw_backend.tools.credentials import MAX_TEXT_CHARS, TRUNCATED, redact_text

from .connections_fakes import handle
from .connections_support import requires_postgres
from .test_connections_budget import TaskBudgetCase
from .test_connections_execute import ExecuteCase

LIMIT = 1_000_000
UNIT = "token=abcdef "  # 13 characters; 17 once its value is redacted
REDACTED_UNIT = "token=[REDACTED] "
SHORT_ANSWER_TOKENS = (10, 5)


def expanding_answer(units: int) -> str:
    return UNIT * units


class PreconditionTest(unittest.TestCase):
    """The examples below mean what they say."""

    def test_the_redaction_expands_the_example_unit_by_four_characters(self):
        redacted, count = redact_text(UNIT)
        self.assertEqual((redacted, count), (REDACTED_UNIT, 1))
        self.assertEqual(len(REDACTED_UNIT) - len(UNIT), 4)

    def test_the_limit_of_the_answer_is_the_limit_of_the_redaction(self):
        self.assertEqual(MAX_TEXT_CHARS, LIMIT)

    def test_an_answer_within_the_limit_can_be_over_it_after_the_redaction(self):
        text = expanding_answer(76_923)
        self.assertLessEqual(len(text), LIMIT)
        redacted, _ = redact_text(text)
        self.assertGreater(len(redacted), LIMIT)
        self.assertNotIn(TRUNCATED, redacted)  # the input was within redact_text's own


@requires_postgres
class ExpansionByRedactionTest(ExecuteCase):
    async def refused(self):
        with self.assertLogs("paw_backend.connections", logging.WARNING):
            with self.assertRaises(ConnectionCallError) as caught:
                await self.call()
        return caught.exception

    async def test_an_answer_that_the_redaction_expands_past_the_limit_is_refused(self):
        self.codex.text = expanding_answer(76_923)  # 999,999 characters
        error = await self.refused()
        self.assertEqual(error.failure, FailureCode.INVALID_RESPONSE)
        (row,) = self.usage_rows()
        self.assertEqual(
            (row["status"], row["failure_code"]), ("failed", "invalid_response")
        )

    async def test_the_boundary_is_the_length_of_the_redacted_text(self):
        # 58,823 units are 999,991 characters redacted; 58,824 are 1,000,008.
        self.codex.text = expanding_answer(58_823)
        result = await self.call()
        self.assertEqual(len(result.text), 58_823 * len(REDACTED_UNIT))
        self.assertLessEqual(len(result.text), LIMIT)
        self.assertEqual(result.text, REDACTED_UNIT * 58_823)
        self.assertEqual(result.redactions, 58_823)
        self.assertNotIn(TRUNCATED, result.text)

        self.codex.text = expanding_answer(58_824)
        error = await self.refused()
        self.assertEqual(error.failure, FailureCode.INVALID_RESPONSE)

    async def test_an_expansion_within_the_limit_is_returned_whole(self):
        self.codex.text = expanding_answer(1000)
        result = await self.call()
        self.assertEqual(result.text, REDACTED_UNIT * 1000)
        self.assertEqual(result.redactions, 1000)


@requires_postgres
class TokenCountsAreKeptTest(ExecuteCase):
    async def refused(self, code=FailureCode.INVALID_RESPONSE):
        with self.assertLogs("paw_backend.connections", logging.WARNING):
            with self.assertRaises(ConnectionCallError) as caught:
                await self.call()
        self.assertEqual(caught.exception.failure, code)

    def tokens_of_the_row(self):
        (row,) = self.usage_rows()
        self.assertEqual(
            (row["status"], row["failure_code"]), ("failed", "invalid_response")
        )
        return row["input_tokens"], row["output_tokens"]

    async def test_a_body_that_the_credential_replacement_makes_too_long(self):
        self.resolver.secrets[handle(1)] = "a"  # ten characters for each occurrence
        self.codex.text = "a" * 200_000
        await self.refused()
        self.assertEqual(self.tokens_of_the_row(), SHORT_ANSWER_TOKENS)

    async def test_a_body_that_the_pattern_redaction_makes_too_long(self):
        self.codex.text = expanding_answer(76_923)
        await self.refused()
        self.assertEqual(self.tokens_of_the_row(), SHORT_ANSWER_TOKENS)

    async def test_a_body_that_is_over_the_limit_as_returned(self):
        answer = AdapterResult("ok", 7, 3)
        object.__setattr__(answer, "text", "x" * (LIMIT + 1))  # forced in
        self.codex.result = answer
        await self.refused()
        self.assertEqual(self.tokens_of_the_row(), (7, 3))

    async def test_a_body_that_is_not_text(self):
        answer = AdapterResult("ok", 7, 3)
        object.__setattr__(answer, "text", 123)
        self.codex.result = answer
        await self.refused()
        self.assertEqual(self.tokens_of_the_row(), (7, 3))

    async def test_counts_that_are_unknown_stay_unknown(self):
        self.codex.input_tokens = self.codex.output_tokens = None
        self.codex.text = expanding_answer(76_923)
        await self.refused()
        self.assertEqual(self.tokens_of_the_row(), (None, None))

    async def test_each_count_is_kept_or_dropped_on_its_own(self):
        # Every combination of valid and invalid: the valid one is kept, the invalid
        # one is NULL, and the response is invalid whenever either is.
        valid = {"input": 1000, "output": 300}
        bad_values = (-1, 10**9 + 1, True, 1.5, "7")
        for bad in bad_values:
            for label, input_tokens, output_tokens, expected in (
                ("both valid", 1000, 300, (1000, 300)),
                ("input invalid", bad, 300, (None, 300)),
                ("output invalid", 1000, bad, (1000, None)),
                ("both invalid", bad, bad, (None, None)),
            ):
                with self.subTest(label, bad=repr(bad)):
                    self.clear_usage_rows()
                    answer = AdapterResult("ok", valid["input"], valid["output"])
                    object.__setattr__(answer, "input_tokens", input_tokens)
                    object.__setattr__(answer, "output_tokens", output_tokens)
                    self.codex.result = answer
                    if label == "both valid":
                        result = await self.call()
                        self.assertEqual(
                            (result.input_tokens, result.output_tokens), expected
                        )
                        (row,) = self.usage_rows()
                        self.assertEqual(row["status"], "succeeded")
                        self.assertEqual(
                            (row["input_tokens"], row["output_tokens"]), expected
                        )
                    else:
                        await self.refused()
                        self.assertEqual(self.tokens_of_the_row(), expected)

    async def test_an_unknown_count_next_to_an_invalid_one_stays_unknown(self):
        answer = AdapterResult("ok", None, 5)
        object.__setattr__(answer, "output_tokens", -5)
        self.codex.result = answer
        await self.refused()
        self.assertEqual(self.tokens_of_the_row(), (None, None))

    async def test_a_valid_unknown_count_and_a_valid_known_one_are_both_kept(self):
        self.codex.input_tokens, self.codex.output_tokens = None, 5
        self.codex.text = expanding_answer(76_923)  # refused for its body
        await self.refused()
        self.assertEqual(self.tokens_of_the_row(), (None, 5))

    async def test_an_answer_that_is_not_a_result_keeps_nothing(self):
        self.codex.result = {"text": "x", "input_tokens": 7}
        await self.refused()
        self.assertEqual(self.tokens_of_the_row(), (None, None))

    async def test_the_kept_counts_are_used_by_the_tokens_quota(self):
        self.seed_quota(self.user, 1000, metric="tokens")
        self.codex.text = expanding_answer(76_923)
        await self.refused()
        statuses = await self.service.quota_status(self.principal(self.user), self.user)
        (status,) = [s for s in statuses if s.metric is QuotaMetric.TOKENS]
        self.assertEqual((status.period, status.used), (QuotaPeriod.DAY, 15))

    def clear_usage_rows(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM connection_usage"))


@requires_postgres
class BudgetIsChargedForARefusedAnswerTest(TaskBudgetCase):
    async def refused(self):
        with self.assertLogs("paw_backend.connections", logging.WARNING):
            with self.assertRaises(ConnectionCallError) as caught:
                await self.call()
        self.assertEqual(caught.exception.failure, FailureCode.INVALID_RESPONSE)

    async def test_a_body_that_the_credential_replacement_makes_too_long(self):
        await self.with_budget(BudgetPreset.STANDARD)
        self.resolver.secrets[handle(1)] = "a"
        self.codex.text = "a" * 200_000
        await self.refused()
        self.assertEqual(await self.consumed(BudgetKind.TOKENS), 15)
        (row,) = self.usage_rows()
        self.assertEqual((row["input_tokens"], row["output_tokens"]), (10, 5))

    async def test_a_body_that_the_pattern_redaction_makes_too_long(self):
        await self.with_budget(BudgetPreset.STANDARD)
        self.codex.text = expanding_answer(76_923)
        await self.refused()
        self.assertEqual(await self.consumed(BudgetKind.TOKENS), 15)

    async def test_the_valid_count_is_charged_when_the_other_is_invalid(self):
        await self.with_budget(BudgetPreset.STANDARD)
        answer = AdapterResult("ok", 1000, 1)
        object.__setattr__(answer, "output_tokens", -1)
        self.codex.result = answer
        await self.refused()
        self.assertEqual(await self.consumed(BudgetKind.TOKENS), 1000)
        (row,) = self.usage_rows()
        self.assertEqual((row["input_tokens"], row["output_tokens"]), (1000, None))

    async def test_the_other_valid_count_is_charged_too(self):
        await self.with_budget(BudgetPreset.STANDARD)
        answer = AdapterResult("ok", 1, 300)
        object.__setattr__(answer, "input_tokens", 10**9 + 1)
        self.codex.result = answer
        await self.refused()
        self.assertEqual(await self.consumed(BudgetKind.TOKENS), 300)

    async def test_two_invalid_counts_charge_nothing(self):
        await self.with_budget(BudgetPreset.STANDARD)
        answer = AdapterResult("ok", 1, 1)
        object.__setattr__(answer, "input_tokens", -5)
        object.__setattr__(answer, "output_tokens", True)
        self.codex.result = answer
        await self.refused()
        self.assertEqual(await self.consumed(BudgetKind.TOKENS), 0)


if __name__ == "__main__":
    unittest.main()
