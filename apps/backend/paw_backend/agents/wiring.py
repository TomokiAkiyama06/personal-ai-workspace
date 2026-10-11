"""The local main model in the application (#208, Decision 0083, 1 and 11).

A deployment that serves the main model gives ``create_app`` a
:class:`LocalModelSetup` (with ``compute``: the model is used only under the
scheduler's leases). There is no environment setting, like ``ComputeSetup``
(Decision 0058, 1). For now it wires one caller: the Inferred Preference
interpreter (#38), as ``app.state.preference_interpreter``
(:func:`build_preference_interpreter`). The local agent runtime of a later stage
uses the same setup, with its ``reasoning_history`` policy (Decision 0076, 1:
the older steps' reasoning is dropped by default; ``KeepAllReasoning`` keeps
it, as the benchmark).
"""

from dataclasses import dataclass, field

import httpx

from paw_backend.agents.chat import ChatCompletionsClient
from paw_backend.agents.preferences import ModelPreferenceInterpreter
from paw_backend.agents.reasoning import (
    MAIN_REASONING_POLICY,
    ReasoningHistoryPolicy,
    check_reasoning_policy,
)
from paw_backend.compute.domain import ResourceClass
from paw_backend.compute.runtimes import ScheduledInterpreter
from paw_backend.compute.scheduler import ComputeScheduler

# The main model's deployment in the scheduler and its served name
# (``paw-llm-main.service``: ``--served-model-name main``).
DEFAULT_MAIN_DEPLOYMENT = "main"
DEFAULT_SERVED_MODEL = "main"


@dataclass(frozen=True, slots=True)
class LocalModelSetup:
    """Where the main model's OpenAI-compatible API is.

    ``base_url``: the API's root with its version (``http://127.0.0.1:8000/v1``).
    ``deployment``: the model's name in the scheduler's ``ComputeConfig``.
    ``served_model``: the server's ``--served-model-name``.
    ``reasoning_history``: what of the model's reasoning the local agent gives
    back to it (Decision 0076, 1: :data:`MAIN_REASONING_POLICY` for the main
    model, Qwen3.8-27B-FP8; ``KeepAllReasoning()`` is the benchmark's
    behaviour). ``transport``: tests pass an ``httpx.MockTransport``."""

    base_url: str
    deployment: str = DEFAULT_MAIN_DEPLOYMENT
    served_model: str = DEFAULT_SERVED_MODEL
    reasoning_history: ReasoningHistoryPolicy = MAIN_REASONING_POLICY
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        check_reasoning_policy(self.reasoning_history)

    def client(self) -> ChatCompletionsClient:
        return ChatCompletionsClient(
            self.base_url, model=self.served_model, transport=self.transport
        )


def build_preference_interpreter(
    setup: LocalModelSetup, scheduler: ComputeScheduler
) -> tuple[ScheduledInterpreter, ChatCompletionsClient]:
    """The interpreter for ``PreferenceConfirmationService`` and the client to
    close at shutdown. ``TypeError`` / ``ValueError`` for a wrong setup or a
    deployment the scheduler does not know (at start, not at the first call)."""
    if not isinstance(setup, LocalModelSetup):
        raise TypeError("local_model must be a LocalModelSetup")
    client = setup.client()
    model = ModelPreferenceInterpreter(client)
    # A client that was never used holds no connection: nothing to close when
    # the deployment is refused here.
    scheduled = ScheduledInterpreter(
        model,
        scheduler,
        deployment=setup.deployment,
        resource_class=ResourceClass.INTERACTIVE,
        prompt_bytes=model.prompt_bytes(),
        output_tokens=model.output_tokens,
    )
    return scheduled, client


__all__ = [
    "DEFAULT_MAIN_DEPLOYMENT",
    "DEFAULT_SERVED_MODEL",
    "LocalModelSetup",
    "build_preference_interpreter",
]
