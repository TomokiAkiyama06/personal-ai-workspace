"""The budget seam. PAW-033 implements it; the broker only asks and reports.

Task execution budgets (max tool calls, runtime, tokens, ...) are PAW-033's.
The broker asks before a call that needs a budget may run, and reports a call
that ran. The default provider knows no budget, so a call that requires one is
**denied**: fail closed until a real provider is installed.

``check`` does not reserve anything: two calls that check at the same moment
can both pass. PAW-033 must make ``charge`` (or a reservation added then)
atomic if a hard limit matters; the broker treats a provider failure, a timeout
or an unexpected answer exactly like ``UNKNOWN``.
"""

import uuid
from enum import StrEnum
from typing import Protocol


class BudgetStatus(StrEnum):
    WITHIN_BUDGET = "within_budget"
    EXCEEDED = "exceeded"
    UNKNOWN = "unknown"  # no budget known for the task: treated as a denial


class BudgetProvider(Protocol):
    async def check(self, task_id: uuid.UUID, tool: str) -> BudgetStatus:
        """May the task make one more call of ``tool``? Must not consume anything."""
        ...

    async def charge(self, task_id: uuid.UUID, tool: str) -> None:
        """Record that a call of ``tool`` ran (after it was executed)."""
        ...


class FailClosedBudgetProvider:
    """The default: no budget is known, so nothing that needs one may run."""

    async def check(self, task_id: uuid.UUID, tool: str) -> BudgetStatus:
        return BudgetStatus.UNKNOWN

    async def charge(self, task_id: uuid.UUID, tool: str) -> None:
        return None
