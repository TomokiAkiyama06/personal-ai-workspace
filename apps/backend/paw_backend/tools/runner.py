"""Running an allowed call: the only place an executor is ever invoked.

The broker decides; the runner asks the broker (every time, with a fresh
decision: an earlier ``ALLOW`` is never reused) and, only for ``ALLOW``, hands
the normalised invocation to the injected :class:`ToolExecutor`. The runner then

* catches an executor failure and reports its exception **type only** (a
  message can hold a path, a URL or a secret),
* redacts the result (recognisable credentials, values under keys such as
  ``password`` / ``api_key``, anything that is not plain JSON data) before it is
  returned or logged,
* reports the call to the broker (audit row and budget charge).

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


class ToolRunner:
    def __init__(
        self,
        broker: ToolBroker,
        executor: ToolExecutor,
        *,
        execution_timeout: float | None = None,
    ) -> None:
        if not isinstance(broker, ToolBroker):
            raise TypeError("broker must be a ToolBroker")
        require_async_method(executor, "execute", 1)
        if execution_timeout is not None and not execution_timeout > 0:
            raise ValueError("execution_timeout must be positive")
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
        try:
            async with asyncio.timeout(self._execution_timeout):
                raw = await self._executor.execute(invocation)
        except Exception as error:
            error_type = type(error).__name__
            logger.error("Tool execution failed (%s) for %s", error_type, decision.tool)
            await self._broker.record_execution(decision, succeeded=False)
            return ToolOutcome(decision, ExecutionStatus.FAILED, error_type=error_type)
        result, redactions = redact_value(raw)
        await self._broker.record_execution(decision, succeeded=True)
        return ToolOutcome(
            decision, ExecutionStatus.COMPLETED, result=result, redactions=redactions
        )
