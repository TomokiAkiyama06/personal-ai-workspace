"""The real agent runtimes of the orchestrator and their shared parts (#208).

Decision 0083 (Approved 2026-10-08) puts the runtimes that satisfy the
orchestrator's ``AgentRuntime`` here: the local main model's tool loop
(``LocalAgentRuntime``) and the Codex / Claude Code CLI sessions. They come in
stages (section 11). This package has the first stage, the shared parts:

* ``failures.py``: the closed table of what a runtime reports
  (:class:`ErrorClassifier`, section 4), on ``RUNTIME_ERROR_CLASSES``, with
  ``NodeOutcome.escalate`` for the failures a higher rung may solve;
* ``reasoning.py``: the seam for what of the model's reasoning goes back to it
  (:class:`ReasoningHistoryPolicy`, section 3), keep-all by default;
* ``chat.py``: one call of the local model server's OpenAI-compatible chat API
  (:class:`ChatCompletionsClient`), used only inside a compute lease;
* ``preferences.py`` and ``wiring.py``: the Inferred Preference interpreter (#38)
  on the local main model, behind ``compute.ScheduledInterpreter`` (an
  ``INTERACTIVE`` lease; without one the rule interpreter answers).

No tool, sandbox or CLI runs here yet (later stages).
"""

from paw_backend.agents.chat import (
    ChatCompletion,
    ChatCompletionsClient,
    ToolCall,
    Usage,
)
from paw_backend.agents.failures import (
    FAILURE_TABLE,
    ErrorClassifier,
    Failure,
    FailureRow,
    RuntimeFailure,
)
from paw_backend.agents.preferences import ModelPreferenceInterpreter
from paw_backend.agents.reasoning import (
    KeepAllReasoning,
    ReasoningHistoryPolicy,
    check_reasoning_policy,
    reasoning_chars,
)
from paw_backend.agents.wiring import LocalModelSetup, build_preference_interpreter

__all__ = [
    "FAILURE_TABLE",
    "ChatCompletion",
    "ChatCompletionsClient",
    "ErrorClassifier",
    "Failure",
    "FailureRow",
    "KeepAllReasoning",
    "LocalModelSetup",
    "ModelPreferenceInterpreter",
    "ReasoningHistoryPolicy",
    "RuntimeFailure",
    "ToolCall",
    "Usage",
    "build_preference_interpreter",
    "check_reasoning_policy",
    "reasoning_chars",
]
