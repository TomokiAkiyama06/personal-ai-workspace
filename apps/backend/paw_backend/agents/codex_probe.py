"""The health probe of the Codex connection: one short ``codex app-server``
session with the fixed prompts and no tool at all (Decision 0100, 10 and 11,
approved 2026-10-29; the prompts' wording: Decision 0102, 1; the verified
version and its tool restrictions: Decision 0089, 2 and Decision 0101; the
login's binding, fencing and write-back: Decision 0083, 6 and Decision 0096;
issue #306).

:class:`CodexProbeAdapter` is the Codex connection's ``ConnectionAdapter``
(``AdapterRegistry``). It is ``exclusive_sessions``, as the Codex session
adapter is: the service runs it inside the connection's session slot (the
ChatGPT login's refresh token is single-use) and hands it a
:class:`~paw_backend.connections.ProbeSession`; without one it starts nothing
(``unavailable``). In the slot it does what a node's session does
(``codex_cli.CodexSessionAdapter``, whose launch, version check, binding,
fencing and collection it reuses), on the same launcher, image and egress
(production: ``SystemdCodexLauncher``, ``/opt/paw/start-codex`` behind the
session's egress forwarder on ``127.0.0.1:3128``):

1. ``codex --version`` without any credential: a version that is not in the
   verified list (``codex_app_server.VERIFIED_VERSIONS``) is ``unavailable``
   (``invalid_response``) and nothing is sent (Decision 0100, 10);
2. the session is bound to the slot (Decision 0096, 2 B), its ``config.toml``
   written (:func:`~.codex_cli.probe_config`: a node session's without any MCP
   server, so no tool is offered at all), the login placed as ``auth.json``,
   each after the slot's check (Decision 0096, 2's fencing);
3. ``app-server``: ``initialize``, ``thread/start`` (ephemeral, approval
   ``never``, read-only, ``environments: []``, the fixed system prompt as
   ``baseInstructions``), ``turn/start`` with the fixed task. Its stream is
   read as a probe's (``CodexStream(probe=True)``): any MCP call, any item
   that can execute, a request from the server is refused (the CLI is
   stopped, ``invalid_response``);
4. the CLI is stopped, then the login is read back and a refreshed one written
   back through the session's ``write_back`` (compare-and-swap), also after a
   failure or a cancellation (a probe may refresh the login: Decision 0083, 6);
5. the session's directory is removed once the service released the slot and
   it was given back; a CLI that could not be stopped keeps the slot
   (``cli_running``) and its directory for the collection (Decision 0096,
   1 D), and so does a session whose slot was taken over.

The verdict (Decision 0100, 2, the Codex form of Claude's):

* ``connected`` only when the verified version answered, exactly one model call
  was reported (one growth of the thread's tokens), the turn completed, and
  its last agent message contains :data:`PROBE_ANSWER` (read for that check
  only: never kept, logged or returned);
* ``expired`` when Codex says ``unauthorized`` (an unretried ``error``: the
  CLI is stopped at once; or the turn's end);
* ``unavailable`` otherwise: ``rate_limited`` for the usage and rate limits
  (the status is kept), ``invalid_response`` for an unverified version, a
  refused stream, a second model call or an answer without the token,
  ``internal_error`` for a failed turn or a CLI that ended without its turn's
  end; ``unavailable`` with the status kept (``keeps_status``) when the slot
  could not be bound or stopped being the probe's, or the CLI could not be
  stopped.

A ``model/rerouted`` notification (the provider answered on another model) is
logged (``Codex probe model reroute``, model names only) and changes nothing
of the verdict, as Claude's fallback before Decision 0102, 3.

``run`` (the one-prompt call of ``ConnectionService.execute``) is never used
for a CLI: it fails at once (``internal_error``) and starts nothing.
"""

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Final

from paw_backend.agents.claude_probe import (
    PROBE_ANSWER,
    PROBE_PROMPT,
    PROBE_SYSTEM_PROMPT,
)
from paw_backend.agents.cli_session import (
    CliSessionSpec,
    create_session,
    log_session_start,
    remove_session,
)
from paw_backend.agents.codex_app_server import (
    CREDENTIAL_ERRORS,
    RATE_ERRORS,
    VERIFIED_VERSIONS,
    CodexStream,
    Ended,
    Initialized,
    Ready,
    StreamRefused,
    Turn,
    initialize,
    notification,
    thread_start,
    turn_start,
)
from paw_backend.agents.codex_cli import (
    CLIENT_VERSION,
    CONFIG_NAME,
    CodexCredentials,
    CodexLaunch,
    CodexLauncher,
    CodexSessionAdapter,
    _bind,
    _held,
    _send,
    _stop,
    _through_cancellation,
    _write,
    probe_config,
)
from paw_backend.agents.codex_credentials import CodexAuthFile
from paw_backend.connections import (
    AdapterFailure,
    AdapterRequest,
    AdapterResult,
    ConnectionKind,
    FailureCode,
    ProbeOutcome,
    ProbeReport,
    ProbeSession,
    Secret,
)
from paw_backend.connections.validation import validate_model

logger = logging.getLogger(__name__)

#: The session's start line names the CLI so (Decision 0091, 4).
PROBE_CLI: Final = "codex-probe"
#: ``create_session`` writes a relay command into a Claude MCP configuration
#: the Codex probe never reads: a command that does nothing.
_NO_RELAY: Final = ("/bin/false",)


@dataclass(slots=True)
class _ProbeState:
    """What a node session's ``CodexAttachment`` carries for the shared
    helpers of ``codex_cli`` (the launch, the stop, the binding and the
    fencing), without a bridge or tools."""

    spec: CliSessionSpec
    unstopped: list[bool] = field(default_factory=list)
    out_of_memory: list[bool] = field(default_factory=list)
    placed: list[bool] = field(default_factory=list)
    fenced: list[bool] = field(default_factory=list)
    refused: list[bool] = field(default_factory=list)
    bound: bool = False


@dataclass(slots=True)
class _Reading:
    failure: FailureCode | None = None
    turns: list[Turn] = field(default_factory=list)
    ended: Ended | None = None


class CodexProbeAdapter:
    """See the module. ``credentials``: how the login is placed and collected
    (``CodexAuthFile`` unless a test says otherwise)."""

    kind = ConnectionKind.CODEX
    # One session per connection at a time, the probe's included (Decision
    # 0100, 10): the service runs it in the connection's session slot.
    exclusive_sessions = True

    def __init__(
        self,
        launcher: CodexLauncher,
        *,
        model: str,
        session_base: Path,
        credentials: CodexCredentials | None = None,
        executable: tuple[str, ...] = ("codex",),
        proxy_url: str | None = None,
        verified_versions: frozenset[str] = VERIFIED_VERSIONS,
    ) -> None:
        credentials = credentials if credentials is not None else CodexAuthFile()
        # The node session's adapter, for its launch, version check and
        # collection (it never runs a session here).
        self._codex = CodexSessionAdapter(
            launcher,
            credentials=credentials,
            executable=executable,
            proxy_url=proxy_url,
            verified_versions=verified_versions,
        )
        self._credentials = credentials
        self._model = validate_model("model", model)
        self._base = Path(session_base)
        self._verified = frozenset(verified_versions)

    def __repr__(self) -> str:
        return f"CodexProbeAdapter(model={self._model!r})"

    async def run(self, secret: Secret, request: AdapterRequest) -> AdapterResult:
        """Never used for a CLI: fails at once, starts nothing."""
        raise AdapterFailure(FailureCode.INTERNAL_ERROR)

    def _report(
        self,
        outcome: ProbeOutcome,
        failure: FailureCode | None,
        tokens: tuple[int | None, int | None] = (None, None),
        *,
        keeps_status: bool = False,
    ) -> ProbeReport:
        return ProbeReport(
            outcome, failure, tokens[0], tokens[1], self._model, keeps_status
        )

    async def check_health(
        self, secret: Secret, session: ProbeSession | None = None
    ) -> ProbeReport:
        if type(session) is not ProbeSession:
            # Outside the connection's session slot (no binding, no
            # write-back) a refreshed login would be lost: nothing starts.
            logger.warning("Codex probe refused: not in the connection's slot")
            return self._report(ProbeOutcome.UNAVAILABLE, FailureCode.UNAVAILABLE)
        spec = create_session(self._base, _NO_RELAY)
        state = _ProbeState(spec)
        session.on_released(partial(_clean, state))
        try:
            return await self._probe(secret, session, state)
        except Exception:
            if not state.unstopped:
                raise
            # The CLI could not be stopped (its unit may still run with the
            # login): left to the collection; nothing was learnt about the
            # credential (Decision 0100, 10).
            return self._report(
                ProbeOutcome.UNAVAILABLE, FailureCode.INTERNAL_ERROR, keeps_status=True
            )
        finally:
            if state.unstopped:
                # Also when the deadline (a cancellation) came during the
                # stop that failed: the service's verdict keeps the status
                # whatever ended the probe (Codex review of #336).
                session.mark_unstopped()
                if state.placed:
                    # The slot stays held (Decision 0083, 6). A version check
                    # that could not be stopped had no login: the slot goes
                    # back.
                    session.cli_running()

    async def _probe(
        self, secret: Secret, session: ProbeSession, state: _ProbeState
    ) -> ProbeReport:
        spec = state.spec
        log_session_start(spec, PROBE_CLI, session.connection_id)
        version = await self._codex._version(state)
        if version is None or version not in self._verified:
            logger.warning("Codex probe refused: version")
            return self._report(ProbeOutcome.UNAVAILABLE, FailureCode.INVALID_RESPONSE)
        lost = self._report(
            ProbeOutcome.UNAVAILABLE, FailureCode.UNAVAILABLE, keeps_status=True
        )
        if not await _bind(session, state):
            return lost
        state.bound = True
        codex_home = spec.config_dir
        _write(codex_home / CONFIG_NAME, probe_config())
        if not await _held(session, state):
            return lost
        try:
            state.placed.append(True)
            await self._credentials.place(secret, codex_home)
            if not await _held(session, state):
                return lost
            stream, reading = await self._converse(state)
        finally:
            # After the CLI was stopped, run to its end even through a
            # cancellation (the deadline): a refreshed login is written back
            # while the probe still holds the slot, checked first (Decision
            # 0096, 2's fencing).
            if await _through_cancellation(
                self._codex._collect_if_held(secret, codex_home, session, state)
            ):
                raise asyncio.CancelledError
        if state.fenced:
            return lost
        for rerouted, to in stream.reroutes:
            logger.warning(
                "Codex probe model reroute: session=%s configured=%s from=%s to=%s",
                spec.session_id,
                self._model,
                rerouted,
                to,
            )
        return self._verdict(stream, reading)

    async def _converse(self, state: _ProbeState) -> tuple[CodexStream, _Reading]:
        process = await self._codex._start(
            CodexLaunch(
                self._codex.arguments(),
                self._codex.environment(state),
                state.spec,
            ),
            state,
        )
        stream = CodexStream((), self._verified, probe=True)
        reading = _Reading()
        try:
            await _send(process, initialize(CLIENT_VERSION))
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                try:
                    item = stream.feed(line)
                except StreamRefused as refused:
                    logger.warning("Codex probe refused: %s", refused.reason)
                    reading.failure = FailureCode.INVALID_RESPONSE
                    break
                if isinstance(item, Initialized):
                    await _send(process, notification("initialized"))
                    await _send(process, thread_start(self._model, PROBE_SYSTEM_PROMPT))
                elif isinstance(item, Ready):
                    await _send(process, turn_start(item.thread_id, PROBE_PROMPT))
                elif isinstance(item, Turn):
                    reading.turns.append(item)
                    if len(reading.turns) > 1:
                        # One model call at most: a second one is not the probe.
                        reading.failure = FailureCode.INVALID_RESPONSE
                        break
                elif isinstance(item, Ended):
                    reading.ended = item
                    break
                if stream.error in CREDENTIAL_ERRORS:
                    # The login was refused: stopped at once.
                    reading.failure = FailureCode.EXPIRED
                    break
        except (BrokenPipeError, ConnectionResetError):
            pass  # the CLI went away; what it wrote says the rest
        finally:
            await _stop(process, state)
        with contextlib.suppress(Exception):
            await process.wait()
        if getattr(process, "oom_killed", False):
            logger.error(
                "Codex probe's unit ran out of memory: session=%s",
                state.spec.session_id,
            )
        return stream, reading

    def _verdict(self, stream: CodexStream, reading: _Reading) -> ProbeReport:
        """See the module. Tokens: the thread's totals once a model call was
        reported; never estimated."""
        tokens = stream.totals if stream.reported else (None, None)

        def report(outcome: ProbeOutcome, failure: FailureCode | None) -> ProbeReport:
            return self._report(outcome, failure, tokens)

        failure = reading.failure
        if failure is FailureCode.EXPIRED:
            return report(ProbeOutcome.EXPIRED, FailureCode.EXPIRED)
        if failure is not None:
            return report(ProbeOutcome.UNAVAILABLE, failure)
        if not stream.initialized:
            return report(ProbeOutcome.UNAVAILABLE, FailureCode.INVALID_RESPONSE)
        ended = reading.ended
        if ended is None:
            return report(ProbeOutcome.UNAVAILABLE, FailureCode.INTERNAL_ERROR)
        if ended.status != "completed":
            if ended.error in CREDENTIAL_ERRORS:
                return report(ProbeOutcome.EXPIRED, FailureCode.EXPIRED)
            if ended.error in RATE_ERRORS:
                return report(ProbeOutcome.UNAVAILABLE, FailureCode.RATE_LIMITED)
            return report(ProbeOutcome.UNAVAILABLE, FailureCode.INTERNAL_ERROR)
        if len(reading.turns) != 1:
            return report(ProbeOutcome.UNAVAILABLE, FailureCode.INVALID_RESPONSE)
        answer = stream.answer
        stream.answer = None  # read for the check only
        if answer is None or PROBE_ANSWER not in answer:
            return report(ProbeOutcome.UNAVAILABLE, FailureCode.INVALID_RESPONSE)
        return report(ProbeOutcome.CONNECTED, None)


def _clean(state: _ProbeState, given_back: bool) -> None:
    """Once the service released the slot: the session's directory goes,
    unless its CLI could not be stopped or the slot (bound to the session)
    is the collection's now (Decision 0096, 1 D and 2's fencing)."""
    spec = state.spec
    if state.unstopped:
        logger.error(
            "A CLI session could not be stopped; its files stay: session=%s",
            spec.session_id,
        )
    elif state.bound and (state.fenced or not given_back):
        logger.error(
            "A CLI session's slot was taken over; its files are left"
            " to the collection: session=%s",
            spec.session_id,
        )
    else:
        remove_session(spec)


__all__ = ["PROBE_CLI", "CodexProbeAdapter"]
