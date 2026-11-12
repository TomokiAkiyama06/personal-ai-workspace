"""The health probe of the Claude connection: one short Claude Code session
with a fixed prompt and no tools (Decision 0100, 2 and 11, approved
2026-10-29; issue #306; the prompts' wording and the model fallback: Decision
0102, 1 and 3, approved 2026-11-05; issue #325).

:class:`ClaudeProbeAdapter` is the connection's ``ConnectionAdapter``
(``AdapterRegistry``): ``check_health(secret)`` starts the CLI on the same
launcher, image and egress as a node's session (production: ``SystemdLauncher``,
``/opt/paw/start-claude`` behind the session's egress forwarder on
``127.0.0.1:3128``, Decision 0091) with the subscription token in
``CLAUDE_CODE_OAUTH_TOKEN`` (handed to the launcher, never in an argument or a
log) and returns a :class:`~paw_backend.connections.ProbeReport`:

* the model (Decision 0103, 5 A): with the gate of the CLI agents' models,
  the default model it resolves (the Admin's policy's, else the runtime
  configuration's), before anything starts: none (not a verified model) is
  ``unavailable`` (``invalid_response``) with the connection's status kept,
  and nothing is started; a policy that cannot be read is ``unavailable``
  with the status kept too. With the verified pairs (Decision 0103, 3 A),
  ``system/init``'s ``claude_code_version`` and the model must be one of
  them, or the CLI is stopped there (``unavailable``, ``invalid_response``,
  the status kept);
* the flags of a node's session without its MCP server: ``--tools ""``, an
  EMPTY MCP configuration with ``--strict-mcp-config``, no ``--allowedTools``,
  ``--max-turns 1``, the fixed system prompt (:data:`PROBE_SYSTEM_PROMPT`) and
  the fixed task on stdin (:data:`PROBE_PROMPT`). Neither holds a task, a
  project, a user, a repository, a time, a host or an id. The session's
  directory holds no worktree and no bridge;
* ``connected`` only when ``system/init`` shows no tool and no MCP server,
  exactly one model call is reported, the ``result`` is a success that is not
  an error, the CLI reported no fallback and no safety stop, and its answer
  contains :data:`PROBE_ANSWER` (Decision 0100's answer 2 A). The answer is
  read for that check only: never kept, logged or returned;
* ``expired`` when an ``api_retry`` says the credential is refused
  (``authentication_failed`` and the like: the CLI is stopped at once);
* ``unavailable`` with ``model_fallback`` (Decision 0102, 3; the service keeps
  the connection's status) when the configured model was refused and another
  one answered or was about to (#325): the CLI is stopped as soon as its
  fallback notice (``system/model_refusal_fallback``) or an ``assistant``
  ``fallback`` block is read (the lines after it are never read), and a
  ``result`` with ``safety_stops`` of 1 or more is one too, whatever else it
  says. It comes before every other failure but ``expired`` (a refused
  credential stops the CLI first). The fallback is also logged (``Claude probe
  model fallback``, model names and counts only); ``model`` stays the
  configured one;
* ``unavailable`` otherwise, with its failure code (``rate_limited`` for the
  provider's limit, which does not change the connection's status;
  ``invalid_response`` for a stream or an answer that is not the expected
  one; ``internal_error`` for a CLI that ended without a result).

The deadline is the service's (``health_timeout_seconds``: the deployment
builds the probing services with ``PROBE_TIMEOUT_SECONDS``, 120 s): when it
passes, the CLI is stopped (SIGTERM, five seconds, SIGKILL; the unit's stop in
production) before the cancellation goes on. The session's directory is
removed afterwards, unless the CLI could not be stopped (then it stays for the
operator, as a node's session's does).

``run`` (the one-prompt call of ``ConnectionService.execute``) is never used
for a CLI: it fails at once (``internal_error``) and starts nothing.
"""

import asyncio
import contextlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Final

from paw_backend.agents.claude_stream import (
    CREDENTIAL_ERRORS,
    MODEL_REFUSAL,
    RATE_ERRORS,
    ClaudeStream,
    Retry,
    StreamRefused,
    Totals,
    Turn,
    fallback_note,
)
from paw_backend.agents.cli_session import (
    CliLaunch,
    CliLauncher,
    UnitNotStoppedError,
    create_session,
    log_session_start,
    remove_session,
    stop_through_cancellation,
)
from paw_backend.connections import (
    AdapterFailure,
    AdapterRequest,
    AdapterResult,
    ConnectionKind,
    FailureCode,
    ProbeOutcome,
    ProbeReport,
    Secret,
)
from paw_backend.connections.validation import validate_model
from paw_backend.tools.interfaces import require_async_method

logger = logging.getLogger(__name__)

TOKEN_ENV: Final = "CLAUDE_CODE_OAUTH_TOKEN"
#: Decision 0102, 1 (superseding the wording of Decision 0100, 2): the fixed
#: prompts of the probe (backend constants: no setting, user or agent changes
#: them; they hold nothing of anybody). Plain words: nothing that tells the
#: model what the probe is for or how the backend is built (the configured
#: model's safeguard refused the old wording, #325). No tool is enforced by the
#: flags and the ``system/init`` check, not by the text.
PROBE_SYSTEM_PROMPT: Final = "Answer the user's message briefly."
PROBE_PROMPT: Final = "Please reply with the word PAW_PROBE_OK."
#: The answer must contain this (case-sensitive; whatever is around it).
PROBE_ANSWER: Final = "PAW_PROBE_OK"
#: The session's own MCP configuration: no server at all.
PROBE_MCP_CONFIG_NAME: Final = "probe-mcp.json"
#: ``create_session`` writes a relay command into the session's default MCP
#: configuration, which the probe never names: a command that does nothing.
_NO_RELAY: Final = ("/bin/false",)
#: What a code from the CLI must look like to be logged (``_code``).
_CODE: Final = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")


class ClaudeProbeAdapter:
    """See the module."""

    kind = ConnectionKind.CLAUDE

    def __init__(
        self,
        launcher: CliLauncher,
        *,
        model: str,
        session_base: Path,
        executable: tuple[str, ...] = ("claude",),
        proxy_url: str | None = None,
        restricted: bool = True,
        verified_models: frozenset[tuple[str, str]] | None = None,
        models: object | None = None,
    ) -> None:
        """``verified_models``: the verified ``(Claude Code version, model)``
        pairs (Decision 0103, 3 A; ``None``: no such check). ``models``: an
        ``AgentModelGate`` whose ``default_model`` names the model to check
        (``None``: ``model``)."""
        require_async_method(launcher, "start", 1)
        if models is not None:
            require_async_method(models, "default_model", 1)
        if not executable or not all(isinstance(p, str) and p for p in executable):
            raise ValueError("executable")
        self._launcher = launcher
        self._pairs = None if verified_models is None else frozenset(verified_models)
        self._models = models
        self._model = validate_model("model", model)
        self._base = Path(session_base)
        self._executable = tuple(executable)
        self._proxy_url = proxy_url
        self._restricted = restricted

    def __repr__(self) -> str:
        return f"ClaudeProbeAdapter(model={self._model!r})"

    def arguments(
        self, mcp_config: Path, settings: Path, model: str | None = None
    ) -> tuple[str, ...]:
        """The CLI's command line: a node session's flags (Decision 0087, 4)
        without its MCP server and tools, and one turn at most, on ``model``
        (the configured one by default)."""
        argv = [
            *self._executable,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--tools",
            "",
            "--strict-mcp-config",
            "--mcp-config",
            str(mcp_config),
            "--permission-mode",
            "dontAsk",
            "--no-session-persistence",
            "--setting-sources",
            "user",
            "--settings",
            str(settings),
            "--max-turns",
            "1",
            "--model",
            model or self._model,
            "--system-prompt",
            PROBE_SYSTEM_PROMPT,
        ]
        if self._restricted:
            argv.append("--restricted")
        return tuple(argv)

    def environment(self, directory: Path, config_dir: Path) -> dict[str, str]:
        """The environment of a node's session (``ClaudeSessionAdapter``)."""
        env = {
            "HOME": str(directory),
            "CLAUDE_CONFIG_DIR": str(config_dir),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_ERROR_REPORTING": "1",
            "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
        }
        if self._proxy_url is not None:
            env["HTTPS_PROXY"] = self._proxy_url
        return env

    async def run(self, secret: Secret, request: AdapterRequest) -> AdapterResult:
        """Never used for a CLI: fails at once, starts nothing."""
        raise AdapterFailure(FailureCode.INTERNAL_ERROR)

    async def _model_to_check(self) -> str | ProbeReport:
        """The model of this probe (see the module), or the report that ends
        it before anything starts."""
        if self._models is None:
            return self._model
        try:
            model = await self._models.default_model(ConnectionKind.CLAUDE)
        except Exception as error:
            logger.warning(
                "Claude probe refused: model policy unreadable (%s)",
                type(error).__name__,
            )
            return ProbeReport(
                ProbeOutcome.UNAVAILABLE,
                FailureCode.UNAVAILABLE,
                model=self._model,
                keeps_status=True,
            )
        if model is None:
            logger.warning("Claude probe refused: model")
            return ProbeReport(
                ProbeOutcome.UNAVAILABLE,
                FailureCode.INVALID_RESPONSE,
                model=self._model,
                keeps_status=True,
            )
        return model

    async def check_health(self, secret: Secret) -> ProbeReport:
        model = await self._model_to_check()
        if isinstance(model, ProbeReport):
            return model
        spec = create_session(self._base, _NO_RELAY)
        mcp_config = spec.directory / PROBE_MCP_CONFIG_NAME
        _write_private(mcp_config, json.dumps({"mcpServers": {}}))
        unstopped = False
        try:
            log_session_start(spec, "claude-probe", None)
            try:
                process = await self._launcher.start(
                    CliLaunch(
                        self.arguments(mcp_config, spec.settings_path, model),
                        self.environment(spec.directory, spec.config_dir),
                        secret,
                        TOKEN_ENV,
                        PROBE_PROMPT.encode("utf-8"),
                        spec,
                    )
                )
            except UnitNotStoppedError:
                unstopped = True
                raise
            stream = ClaudeStream(
                frozenset(), probe=True, verified_models=self._pairs, model=model
            )
            reading = _Reading()
            try:
                await _read(process, stream, reading)
            finally:
                failed, cancelled = await stop_through_cancellation(process)
                if failed is not None:
                    unstopped = True
                if cancelled:
                    raise asyncio.CancelledError
                if failed is not None:
                    raise failed
            with contextlib.suppress(Exception):
                await process.wait()
            note = fallback_note(stream, model)
            if note is not None:
                # #325: another model answered (or one refused). The model
                # names are in this line only (Decision 0102, 2).
                logger.warning(
                    "Claude probe model fallback: session=%s %s",
                    spec.session_id,
                    note,
                )
            if reading.refused == MODEL_REFUSAL:
                # Decision 0103, 3 A: not a verified pair; nothing was learnt
                # about the credential.
                return ProbeReport(
                    ProbeOutcome.UNAVAILABLE,
                    FailureCode.INVALID_RESPONSE,
                    model=model,
                    keeps_status=True,
                )
            return _report(stream, reading, model, spec.session_id)
        finally:
            if unstopped:
                logger.error(
                    "the probe's CLI could not be stopped: session=%s",
                    spec.session_id,
                )
            else:
                remove_session(spec)


class _Reading:
    """What the read of the stream found (besides the stream's own state)."""

    __slots__ = (
        "eof_in_turn",
        "failure",
        "refused",
        "retry",
        "retry_status",
        "totals",
        "turns",
    )

    def __init__(self) -> None:
        self.failure: FailureCode | None = None
        self.retry: str | None = None
        self.totals: Totals | None = None
        self.turns: list[Turn] = []
        # For the verdict's log line only (issue #342).
        self.refused: str | None = None
        self.retry_status: int | None = None
        self.eof_in_turn = False


async def _read(process, stream: ClaudeStream, reading: _Reading) -> None:
    while True:
        line = await process.stdout.readline()
        if not line:
            if stream.turn_open:
                reading.failure = FailureCode.INVALID_RESPONSE
                reading.eof_in_turn = True
            return
        try:
            item = stream.feed(line)
        except StreamRefused as refused:
            logger.warning("Claude probe refused: %s", refused.reason)
            reading.failure = FailureCode.INVALID_RESPONSE
            reading.refused = refused.reason
            return
        if stream.fallbacks:
            # Decision 0102, 3 (1): the configured model was refused. Stop
            # here: the fallback model is not let finish its answer, and no
            # later line (its call, its answer, the result) is read.
            reading.failure = FailureCode.MODEL_FALLBACK
            return
        if isinstance(item, Turn):
            if not stream.initialized:
                reading.failure = FailureCode.INVALID_RESPONSE
                return
            reading.turns.append(item)
            if len(reading.turns) > 1:
                # One turn at most: a second model call is not the probe.
                reading.failure = FailureCode.INVALID_RESPONSE
                return
        elif isinstance(item, Retry):
            reading.retry = item.error
            reading.retry_status = item.status
            if item.error in CREDENTIAL_ERRORS:
                reading.failure = FailureCode.EXPIRED
                return
        elif isinstance(item, Totals):
            reading.totals = item


def _report(
    stream: ClaudeStream, reading: _Reading, model: str, session_id: object = None
) -> ProbeReport:
    """The probe's verdict (see the module). Tokens: the result's totals when
    known, else the reported turns; never estimated. An ``internal_error`` or
    ``invalid_response`` is logged once with why (:func:`_log_failure`)."""
    totals = reading.totals
    if totals is not None and totals.input_tokens is not None:
        input_tokens, output_tokens = totals.input_tokens, totals.output_tokens
    elif reading.turns:
        input_tokens = sum(turn.input_tokens for turn in reading.turns)
        output_tokens = sum(turn.output_tokens for turn in reading.turns)
    else:
        input_tokens = output_tokens = None

    def report(
        outcome: ProbeOutcome, failure: FailureCode | None, why: str = ""
    ) -> ProbeReport:
        if failure in (FailureCode.INTERNAL_ERROR, FailureCode.INVALID_RESPONSE):
            _log_failure(failure, why, stream, reading, session_id)
        return ProbeReport(outcome, failure, input_tokens, output_tokens, model)

    failure = reading.failure
    if failure is FailureCode.EXPIRED:
        return report(ProbeOutcome.EXPIRED, FailureCode.EXPIRED)
    if failure is FailureCode.MODEL_FALLBACK or stream.fell_back:
        # Decision 0102, 3 (2 and 4): a safety stop counted by the result is
        # a fallback too, and it comes before the other failures.
        stream.answer = None  # never checked
        return report(ProbeOutcome.UNAVAILABLE, FailureCode.MODEL_FALLBACK)
    if failure is not None:
        if reading.refused is not None:
            why = "refused"
        elif reading.eof_in_turn:
            why = "ended_in_turn"
        elif len(reading.turns) > 1:
            why = "second_model_call"
        else:
            why = "turn_before_init"
        return report(ProbeOutcome.UNAVAILABLE, failure, why)
    if reading.retry in RATE_ERRORS and (totals is None or totals.is_error):
        # The provider's limit, whether or not a model call completed: it
        # says nothing about the credential (the status is kept; Codex review
        # of #317).
        return report(ProbeOutcome.UNAVAILABLE, FailureCode.RATE_LIMITED)
    if not stream.initialized or len(reading.turns) != 1:
        why = (
            "not_initialized"
            if not stream.initialized
            else "no_model_call"
            if not reading.turns
            else "second_model_call"
        )
        return report(ProbeOutcome.UNAVAILABLE, FailureCode.INVALID_RESPONSE, why)
    if totals is None:
        return report(ProbeOutcome.UNAVAILABLE, FailureCode.INTERNAL_ERROR, "no_result")
    if totals.is_error:
        return report(
            ProbeOutcome.UNAVAILABLE, FailureCode.INTERNAL_ERROR, "result_error"
        )
    answer = stream.answer
    stream.answer = None  # read for the check only
    if answer is None or PROBE_ANSWER not in answer:
        return report(
            ProbeOutcome.UNAVAILABLE,
            FailureCode.INVALID_RESPONSE,
            "no_answer" if answer is None else "answer_without_token",
        )
    return report(ProbeOutcome.CONNECTED, None)


def _code(value: object) -> str:
    """A code from the CLI (a result's subtype, an ``api_retry`` category) as
    it may be logged: a plain identifier, else ``other``; ``-`` when absent."""
    if value is None:
        return "-"
    if isinstance(value, str) and _CODE.fullmatch(value):
        return value
    return "other"


def _log_failure(
    failure: FailureCode,
    why: str,
    stream: ClaudeStream,
    reading: _Reading,
    session_id: object,
) -> None:
    """Issue #342: one line saying why the probe was ``internal_error`` /
    ``invalid_response``. Fixed codes, counts and the HTTP status class only:
    never the prompt, the answer, the token or an error's text."""
    totals = reading.totals
    status = reading.retry_status
    logger.warning(
        "Claude probe failed: session=%s failure=%s why=%s result=%s"
        " is_error=%s retry=%s http=%s initialized=%s turns=%d",
        session_id,
        failure.value,
        why,
        _code(totals.subtype if totals is not None else None),
        "-" if totals is None else "yes" if totals.is_error else "no",
        _code(reading.retry),
        f"{status // 100}xx" if status is not None else "-",
        "yes" if stream.initialized else "no",
        len(reading.turns),
    )


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o640)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


__all__ = [
    "PROBE_ANSWER",
    "PROBE_PROMPT",
    "PROBE_SYSTEM_PROMPT",
    "ClaudeProbeAdapter",
]
