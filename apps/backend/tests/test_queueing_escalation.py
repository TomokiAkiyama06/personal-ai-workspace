"""``decide_next_action``: the next step after budget and loop verdicts."""

import itertools
import unittest
from types import SimpleNamespace

from paw_backend.tasks.queueing import (
    BudgetKind,
    BudgetStatus,
    BudgetUsage,
    BudgetVerdict,
    Decision,
    DecisionReason,
    InvalidQueueingArgumentError,
    LoopAssessment,
    LoopVerdict,
    NextAction,
    decide_next_action,
)

K = BudgetKind
L = LoopVerdict


def verdict(*exceeded: BudgetKind, limit: int | None = 100) -> BudgetVerdict:
    """A verdict whose ``exceeded`` kinds are given (limit 100, consumed 101)."""
    usage = tuple(
        BudgetUsage(kind, 101 if kind in exceeded else 1, limit) for kind in BudgetKind
    )
    return BudgetVerdict(
        BudgetStatus.EXCEEDED if exceeded else BudgetStatus.OK, tuple(exceeded), usage
    )


class WithoutExceededBudgetTest(unittest.TestCase):
    def test_no_loop_means_continue(self):
        for can_escalate in (True, False):
            with self.subTest(can_escalate=can_escalate):
                decision = decide_next_action(
                    verdict(), L.CONTINUE, can_escalate=can_escalate
                )
                self.assertEqual(
                    decision,
                    Decision(NextAction.CONTINUE, DecisionReason.NONE, (), L.CONTINUE),
                )

    def test_a_first_loop_tries_an_alternative_approach(self):
        for can_escalate in (True, False):
            with self.subTest(can_escalate=can_escalate):
                decision = decide_next_action(
                    verdict(), L.TRY_ALTERNATIVE, can_escalate=can_escalate
                )
                self.assertEqual(
                    decision,
                    Decision(
                        NextAction.TRY_ALTERNATIVE,
                        DecisionReason.LOOP_TRY_ALTERNATIVE,
                        (),
                        L.TRY_ALTERNATIVE,
                    ),
                )

    def test_a_loop_that_survived_the_alternative_escalates_the_agent(self):
        decision = decide_next_action(verdict(), L.ESCALATE, can_escalate=True)
        self.assertEqual(
            decision,
            Decision(
                NextAction.ESCALATE_AGENT,
                DecisionReason.LOOP_ESCALATE,
                (),
                L.ESCALATE,
            ),
        )

    def test_without_a_stronger_agent_a_human_decides(self):
        decision = decide_next_action(verdict(), L.ESCALATE, can_escalate=False)
        self.assertEqual(
            decision,
            Decision(
                NextAction.WAIT_FOR_USER,
                DecisionReason.LOOP_ESCALATION_UNAVAILABLE,
                (),
                L.ESCALATE,
            ),
        )

    def test_a_loop_is_never_turned_into_a_failure(self):
        for loop, can_escalate in itertools.product(LoopVerdict, (True, False)):
            with self.subTest(loop=loop, can_escalate=can_escalate):
                action = decide_next_action(
                    verdict(), loop, can_escalate=can_escalate
                ).action
                self.assertNotEqual(action, NextAction.FAIL)

    def test_the_unlimited_preset_does_not_disable_loop_handling(self):
        unlimited_ok = verdict(limit=None)
        self.assertEqual(unlimited_ok.status, BudgetStatus.OK)
        self.assertEqual(
            decide_next_action(unlimited_ok, L.ESCALATE, can_escalate=True).action,
            NextAction.ESCALATE_AGENT,
        )
        self.assertEqual(
            decide_next_action(
                unlimited_ok, L.TRY_ALTERNATIVE, can_escalate=True
            ).action,
            NextAction.TRY_ALTERNATIVE,
        )
        self.assertEqual(
            decide_next_action(unlimited_ok, L.ESCALATE, can_escalate=False).action,
            NextAction.WAIT_FOR_USER,
        )


class WithExceededBudgetTest(unittest.TestCase):
    def test_every_exceeded_kind_stops_the_task_for_a_human_except_retries(self):
        for kind, loop, can_escalate in itertools.product(
            BudgetKind, LoopVerdict, (True, False)
        ):
            expected = (
                NextAction.FAIL if kind is K.RETRIES else NextAction.WAIT_FOR_USER
            )
            with self.subTest(kind=kind, loop=loop, can_escalate=can_escalate):
                decision = decide_next_action(
                    verdict(kind), loop, can_escalate=can_escalate
                )
                self.assertEqual(
                    decision,
                    Decision(expected, DecisionReason.BUDGET_EXCEEDED, (kind,), loop),
                )

    def test_several_exceeded_kinds_are_all_reported(self):
        decision = decide_next_action(
            verdict(K.STEPS, K.TOKENS, K.GPU_SECONDS), L.CONTINUE, can_escalate=True
        )
        self.assertEqual(decision.exceeded, (K.STEPS, K.TOKENS, K.GPU_SECONDS))
        self.assertEqual(decision.action, NextAction.WAIT_FOR_USER)

    def test_exceeded_retries_win_over_other_exceeded_kinds(self):
        decision = decide_next_action(
            verdict(K.STEPS, K.RETRIES), L.CONTINUE, can_escalate=True
        )
        self.assertEqual(decision.action, NextAction.FAIL)
        self.assertEqual(decision.exceeded, (K.STEPS, K.RETRIES))

    def test_an_exceeded_budget_is_never_ignored_for_any_combination(self):
        """Every non-empty set of exceeded kinds x loop verdict x can_escalate."""
        kinds = list(BudgetKind)
        checked = 0
        for size in range(1, len(kinds) + 1):
            for subset in itertools.combinations(kinds, size):
                for loop, can_escalate in itertools.product(LoopVerdict, (True, False)):
                    decision = decide_next_action(
                        verdict(*subset), loop, can_escalate=can_escalate
                    )
                    checked += 1
                    self.assertIn(
                        decision.action, {NextAction.WAIT_FOR_USER, NextAction.FAIL}
                    )
                    self.assertEqual(decision.reason, DecisionReason.BUDGET_EXCEEDED)
                    self.assertEqual(decision.exceeded, subset)
                    self.assertEqual(decision.loop_verdict, loop)
        self.assertEqual(checked, 63 * 3 * 2)

    def test_exceeding_the_budget_wins_over_escalation(self):
        # Escalating would spend more of an exhausted budget.
        decision = decide_next_action(verdict(K.TOKENS), L.ESCALATE, can_escalate=True)
        self.assertEqual(decision.action, NextAction.WAIT_FOR_USER)
        self.assertEqual(decision.loop_verdict, L.ESCALATE)


class InputTest(unittest.TestCase):
    def test_the_decision_is_deterministic(self):
        first = decide_next_action(
            verdict(K.TOKENS), L.TRY_ALTERNATIVE, can_escalate=True
        )
        second = decide_next_action(
            verdict(K.TOKENS), L.TRY_ALTERNATIVE, can_escalate=True
        )
        self.assertEqual(first, second)

    def test_the_arguments_are_type_checked(self):
        good = verdict()
        cases = [
            ("budget", (None, L.CONTINUE), {"can_escalate": True}),
            ("budget", ({"status": "ok"}, L.CONTINUE), {"can_escalate": True}),
            ("loop", (good, "continue"), {"can_escalate": True}),
            ("loop", (good, None), {"can_escalate": True}),
            ("can_escalate", (good, L.CONTINUE), {"can_escalate": 1}),
            ("can_escalate", (good, L.CONTINUE), {"can_escalate": None}),
            ("can_escalate", (good, L.CONTINUE), {"can_escalate": "yes"}),
        ]
        for parameter, args, kwargs in cases:
            with self.subTest(parameter=parameter, args=args, kwargs=kwargs):
                with self.assertRaises(InvalidQueueingArgumentError) as caught:
                    decide_next_action(*args, **kwargs)
                self.assertEqual(caught.exception.parameter, parameter)

    def test_look_alikes_of_the_verdicts_are_rejected(self):
        look_alike = SimpleNamespace(
            status=BudgetStatus.OK, exceeded=(), usage=verdict().usage
        )
        with self.assertRaises(InvalidQueueingArgumentError) as caught:
            decide_next_action(look_alike, L.CONTINUE, can_escalate=True)
        self.assertEqual(caught.exception.parameter, "budget")
        # The assessment of the loop detector is not its verdict.
        assessment = LoopAssessment(L.ESCALATE, "a" * 64, 1, 3)
        with self.assertRaises(InvalidQueueingArgumentError) as caught:
            decide_next_action(verdict(), assessment, can_escalate=True)
        self.assertEqual(caught.exception.parameter, "loop")

    def test_the_decision_is_immutable_and_reports_the_given_verdicts(self):
        decision = decide_next_action(verdict(K.TOKENS), L.ESCALATE, can_escalate=False)
        with self.assertRaises(AttributeError):
            decision.action = NextAction.CONTINUE  # type: ignore[misc]
        self.assertIsInstance(decision.exceeded, tuple)

    def test_can_escalate_is_required(self):
        with self.assertRaises(TypeError):
            decide_next_action(verdict(), L.CONTINUE)  # type: ignore[call-arg]


if __name__ == "__main__":
    unittest.main()
