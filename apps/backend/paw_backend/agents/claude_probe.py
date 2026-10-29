"""The health probe of the Claude connection: one short Claude Code session
with a fixed prompt and no tools (Decision 0100, 2 and 11, approved
2026-10-29; issue #306).

:class:`ClaudeProbeAdapter` is the connection's ``ConnectionAdapter``
(``AdapterRegistry``): ``check_health(secret)`` starts the CLI on the same
launcher, image and egress as a node's session (production: ``SystemdLauncher``,
``/opt/paw/start-claude`` behind the session's egress forwarder on
``127.0.0.1:3128``, Decision 0091) with the subscription token in
``CLAUDE_CODE_OAUTH_TOKEN`` (handed to the launcher, never in an argument or a
log) and returns a :class:`~paw_backend.connections.ProbeReport`:

* the flags of a node's session without its MCP server: ``--tools ""``, an
  EMPTY MCP configuration with ``--strict-mcp-config``, no ``--allowedTools``,
  ``--max-turns 1``, the fixed system prompt (:data:`PROBE_SYSTEM_PROMPT`) and
  the fixed task on stdin (:data:`PROBE_PROMPT`). Neither holds a task, a
  project, a user, a repository, a time, a host or an id. The session's
  directory holds no worktree and no bridge;
* ``connected`` only when ``system/init`` shows no tool and no MCP server,
  exactly one model call is reported, the ``result`` is a success that is not
  an error, and its answer contains :data:`PROBE_ANSWER` (Decision 0100's
  answer 2 A). The answer is read for that check only: never kept, logged or
  returned;
* ``expired`` when an ``api_retry`` says the credential is refused
  (``authentication_failed`` and the like: the CLI is stopped at once);
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
from pathlib import Path
from typing import Final

from paw_backend.agents.claude_stream import (
    CREDENTIAL_ERRORS,
    RATE_ERRORS,
    ClaudeStream,
    Retry,
    StreamRefused,
    Totals,
    Turn,
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
#: Decision 0100, 2: the fixed prompts of the probe (backend constants: no
#: setting, user or agent changes them; they hold nothing of anybody).
PROBE_SYSTEM_PROMPT: Final = (
    "You are a connectivity check for a backend service. Do not use any tools."
    " Reply with exactly the single line PAW_PROBE_OK and nothing else."
)
PROBE_PROMPT: Final = "Reply with exactly: PAW_PROBE_OK"
#: The answer must contain this (case-sensitive; whatever is around it).
PROBE_ANSWER: Final = "PAW_PROBE_OK"
#: The session's own MCP configuration: no server at all.
PROBE_MCP_CONFIG_NAME: Final = "probe-mcp.json"
#: ``create_session`` writes a relay command into the session's default MCP
#: configuration, which the probe never names: a command that does nothing.
_NO_RELAY: Final = ("/bin/false",)


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
    ) -> None:
        require_async_method(launcher, "start", 1)
        if not executable or not all(isinstance(p, str) and p for p in executable):
            raise ValueError("executable")
        self._launcher = launcher
        self._model = validate_model("model", model)
        self._base = Path(session_base)
        self._executable = tuple(executable)
        self._proxy_url = proxy_url
        self._restricted = restricted

    def __repr__(self) -> str:
        return f"ClaudeProbeAdapter(model={self._model!r})"

    def arguments(self, mcp_config: Path, settings: Path) -> tuple[str, ...]:
        """The CLI's command line: a node session's flags (Decision 0087, 4)
        without its MCP server and tools, and one turn at most."""
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
            self._model,
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

    async def check_health(self, secret: Secret) -> ProbeReport:
        spec = create_session(self._base, _NO_RELAY)
        mcp_config = spec.directory / PROBE_MCP_CONFIG_NAME
        _write_private(mcp_config, json.dumps({"mcpServers": {}}))
        unstopped = False
        try:
            log_session_start(spec, "claude-probe", None)
            try:
                process = await self._launcher.start(
                    CliLaunch(
                        self.arguments(mcp_config, spec.settings_path),
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
            stream = ClaudeStream(frozenset(), probe=True)
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
            return _report(stream, reading, self._model)
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

    __slots__ = ("failure", "retry", "totals", "turns")

    def __init__(self) -> None:
        self.failure: FailureCode | None = None
        self.retry: str | None = None
        self.totals: Totals | None = None
        self.turns: list[Turn] = []


async def _read(process, stream: ClaudeStream, reading: _Reading) -> None:
    while True:
        line = await process.stdout.readline()
        if not line:
            if stream.turn_open:
                reading.failure = FailureCode.INVALID_RESPONSE
            return
        try:
            item = stream.feed(line)
        except StreamRefused as refused:
            logger.warning("Claude probe refused: %s", refused.reason)
            reading.failure = FailureCode.INVALID_RESPONSE
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
            if item.error in CREDENTIAL_ERRORS:
                reading.failure = FailureCode.EXPIRED
                return
        elif isinstance(item, Totals):
            reading.totals = item


def _report(stream: ClaudeStream, reading: _Reading, model: str) -> ProbeReport:
    """The probe's verdict (see the module). Tokens: the result's totals when
    known, else the reported turns; never estimated."""
    totals = reading.totals
    if totals is not None and totals.input_tokens is not None:
        input_tokens, output_tokens = totals.input_tokens, totals.output_tokens
    elif reading.turns:
        input_tokens = sum(turn.input_tokens for turn in reading.turns)
        output_tokens = sum(turn.output_tokens for turn in reading.turns)
    else:
        input_tokens = output_tokens = None

    def report(outcome: ProbeOutcome, failure: FailureCode | None) -> ProbeReport:
        return ProbeReport(outcome, failure, input_tokens, output_tokens, model)

    failure = reading.failure
    if failure is FailureCode.EXPIRED:
        return report(ProbeOutcome.EXPIRED, FailureCode.EXPIRED)
    if failure is not None:
        return report(ProbeOutcome.UNAVAILABLE, failure)
    if reading.retry in RATE_ERRORS and (totals is None or totals.is_error):
        # The provider's limit, whether or not a model call completed: it
        # says nothing about the credential (the status is kept; Codex review
        # of #317).
        return report(ProbeOutcome.UNAVAILABLE, FailureCode.RATE_LIMITED)
    if not stream.initialized or len(reading.turns) != 1:
        return report(ProbeOutcome.UNAVAILABLE, FailureCode.INVALID_RESPONSE)
    if totals is None:
        return report(ProbeOutcome.UNAVAILABLE, FailureCode.INTERNAL_ERROR)
    if totals.is_error:
        return report(ProbeOutcome.UNAVAILABLE, FailureCode.INTERNAL_ERROR)
    answer = stream.answer
    stream.answer = None  # read for the check only
    if answer is None or PROBE_ANSWER not in answer:
        return report(ProbeOutcome.UNAVAILABLE, FailureCode.INVALID_RESPONSE)
    return report(ProbeOutcome.CONNECTED, None)


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
