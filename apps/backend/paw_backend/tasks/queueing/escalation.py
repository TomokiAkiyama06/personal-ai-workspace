"""The next action after budget and loop verdicts (PAW-033)."""

from paw_backend.tasks.queueing.domain import (
    BudgetVerdict,
    Decision,
    LoopVerdict,
)


def decide_next_action(
    budget: BudgetVerdict,
    loop: LoopVerdict,
    *,
    can_escalate: bool,
) -> Decision:
    """Combine a budget verdict and a loop verdict into one ``Decision``.

    A pure function. ``budget`` must be a ``BudgetVerdict``, ``loop`` a
    ``LoopVerdict`` member and ``can_escalate`` a real ``bool`` (whether a
    stronger agent than the current one is still available); otherwise
    ``InvalidQueueingArgumentError`` naming ``budget`` / ``loop`` /
    ``can_escalate``. The rules, first match wins:

    1. The budget is EXCEEDED (whatever the loop verdict is; the loop verdict is
       never allowed to hide it): action ``FAIL`` when ``BudgetKind.RETRIES`` is
       among the exceeded kinds, else ``WAIT_FOR_USER`` (stop at a safe boundary;
       a human raises the budget or ends the task). Reason ``BUDGET_EXCEEDED``.
       Escalating would spend more of an exhausted budget, so it is not done.
    2. ``loop`` is ESCALATE: ``ESCALATE_AGENT`` (reason ``LOOP_ESCALATE``) when
       ``can_escalate``, else ``WAIT_FOR_USER`` (reason
       ``LOOP_ESCALATION_UNAVAILABLE``).
    3. ``loop`` is TRY_ALTERNATIVE: ``TRY_ALTERNATIVE`` (``LOOP_TRY_ALTERNATIVE``).
    4. Otherwise ``CONTINUE`` (reason ``NONE``).

    ``Decision.exceeded`` is ``budget.exceeded`` and ``Decision.loop_verdict`` is
    ``loop``, in every case. The preset plays no role: an Unlimited task whose
    budget verdict is OK still escalates on a loop.
    """
    raise NotImplementedError("PAW-033 stub")
