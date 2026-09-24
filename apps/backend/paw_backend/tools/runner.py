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
  shielded from cancellation: a call that ran is recorded and charged even if the
  task around it is cancelled, or something after it fails.

The executor receives a credential only as an opaque handle in the arguments.
Resolving it, opening files confined to the scope's roots and enforcing the
network policy are the executor's job (sandboxing is out of scope of the
broker).
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
            # this task is cancelled meanwhile (the record is shielded), and
            # before anything below can fail.
            await asyncio.shield(
                self._broker.record_execution(decision, succeeded=succeeded)
            )
        if not succeeded:
            return ToolOutcome(decision, ExecutionStatus.FAILED, error_type=error_type)
        result, redactions = redact_value(raw)
        return ToolOutcome(
            decision, ExecutionStatus.COMPLETED, result=result, redactions=redactions
        )
