"""Running an allowed call: the only place an executor is ever invoked.

The broker decides; the runner asks the broker (every time, with a fresh
decision: an earlier ``ALLOW`` is never reused) and, only for ``ALLOW``, hands
the normalised invocation to the injected :class:`ToolExecutor`. The runner then

* catches an executor failure and reports its exception **type only** (a
  message can hold a path, a URL or a secret),
* redacts the result (recognisable credentials, values under keys such as
  ``password`` / ``api_key``, anything that is not plain JSON data) before it is
  returned or logged,
* reports the call to the broker (audit row and budget charge) in a ``finally``,
  as a task that ``run`` keeps and waits for (``_account``): a call that ran is
  recorded and charged even if the task around it is cancelled (also while the
  audit or budget adapter is still working: the cancellation is raised only
  after the accounting has ended), or something after it fails.

The executor receives a credential only as an opaque handle in the arguments.
Resolving it, opening files confined to the scope's roots (no symlink out of
them), connecting only to the URL's host (no automatic redirect, and a check of
the IP it resolves to: no loopback / private / link-local target) and
enforcing the ACLs of what it reads are the executor's job (sandboxing is out
of scope of the broker). See "Executor の契約" in ``apps/backend/README.md``.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from paw_backend.tools.broker import ToolBroker
from paw_backend.tools.calls import ToolCall, ToolInvocation
from paw_backend.tools.credentials import redact_value
from paw_backend.tools.decisions import BrokerDecision
from paw_backend.tools.interfaces import require_async_method

logger = logging.getLogger(__name__)


class ToolExecutor(Protocol):
    """Runs one already-authorised invocation and returns its (JSON-like) result."""

    async def execute(self, invocation: ToolInvocation) -> object: ...


class ExecutionStatus(StrEnum):
    NOT_EXECUTED = "not_executed"  # denied, or waiting for an approval
    COMPLETED = "completed"
    FAILED = "failed"  # the executor raised (or timed out)


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    decision: BrokerDecision
    status: ExecutionStatus
    # The executor's result after redaction; ``None`` unless COMPLETED.
    result: object | None = field(default=None, repr=False)
    redactions: int = 0
    # The class name of the executor's exception; never its message.
    error_type: str | None = None


# A call may not run for ever: this bounds it until PAW-033's runtime budget
# takes over (a shorter limit is passed per runner).
DEFAULT_EXECUTION_TIMEOUT = 600.0
MAX_EXECUTION_TIMEOUT = 86_400.0


class ToolRunner:
    def __init__(
        self,
        broker: ToolBroker,
        executor: ToolExecutor,
        *,
        execution_timeout: float = DEFAULT_EXECUTION_TIMEOUT,
    ) -> None:
        if not isinstance(broker, ToolBroker):
            raise TypeError("broker must be a ToolBroker")
        require_async_method(executor, "execute", 1)
        if (
            isinstance(execution_timeout, bool)
            or not isinstance(execution_timeout, int | float)
            or not 0 < execution_timeout <= MAX_EXECUTION_TIMEOUT
        ):
            raise ValueError("execution_timeout must be between 0 and 24 hours")
        self._broker = broker
        self._executor = executor
        self._execution_timeout = execution_timeout

    async def run(
        self, call: ToolCall, *, approval_id: uuid.UUID | None = None
    ) -> ToolOutcome:
        decision = await self._broker.request(call, approval_id=approval_id)
        invocation = decision.invocation
        if not decision.allowed or invocation is None:
            return ToolOutcome(decision, ExecutionStatus.NOT_EXECUTED)
        succeeded = False
        error_type: str | None = None
        try:
            try:
                async with asyncio.timeout(self._execution_timeout):
                    raw = await self._executor.execute(invocation)
            except Exception as error:
                error_type = type(error).__name__
                logger.error(
                    "Tool execution failed (%s) for %s", error_type, decision.tool
                )
            else:
                succeeded = True
        finally:
            # The call ran (or was cut short): it is recorded and charged even if
            # this task is cancelled meanwhile, and before anything below can
            # fail.
            await self._account(decision, succeeded=succeeded)
        if not succeeded:
            return ToolOutcome(decision, ExecutionStatus.FAILED, error_type=error_type)
        result, redactions = redact_value(raw)
        return ToolOutcome(
            decision, ExecutionStatus.COMPLETED, result=result, redactions=redactions
        )

    async def _account(self, decision: BrokerDecision, *, succeeded: bool) -> None:
        """Record and charge an executed call; finish before any cancel passes.

        The accounting runs as a task of its own that this coroutine keeps a
        reference to and waits for. A cancellation that arrives meanwhile (the
        audit or budget adapter is slow) is held back until the accounting has
        ended, and then raised: the caller still learns that it was cancelled
        (an ``asyncio.timeout`` around ``run`` still becomes ``TimeoutError``),
        but never before the audit row and the charge exist. ``asyncio.shield``
        alone returned at once and left the accounting as a background task that
        nothing waited for or kept alive. The wait is bounded by the broker's own
        timeouts (one for the audit write, one for the charge). A task that the
        event loop itself cancels while it is closing (``asyncio.run`` cancels
        every task) cancels the accounting too; that cannot be prevented here.
        """
        accounting = asyncio.create_task(
            self._broker.record_execution(decision, succeeded=succeeded)
        )
        cancelled: asyncio.CancelledError | None = None
        while not accounting.done():
            try:
                # Unlike awaiting the task, asyncio.wait() does not cancel it
                # when this caller is cancelled.
                await asyncio.wait({accounting})
            except asyncio.CancelledError as error:
                cancelled = error  # raised below, once the accounting is done
        accounting.result()  # its own failure, if any (it never raises for a store)
        if cancelled is not None:
            raise cancelled
