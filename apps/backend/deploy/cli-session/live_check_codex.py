#!/usr/bin/env python3
"""The live check of a Codex pair of a CLI version and a model, outside the
production backend (Decision 0104, 3 A; issue #341). It sends to the real
provider (OpenAI): only a Human runs it, explicitly; no agent does.

As the recorder smoke (``smoke_codex_recorder.py``) it builds the real
``CodexProbeAdapter``, ``CodexSessionAdapter``, ``CodexCliRuntime`` and
``McpBridge`` (with ``paw_mcp_relay.py``) and the backend's own
``probe_config`` / ``session_config`` (unchanged: no catalog override yet,
Decision 0104, "PR の分け方" 2), with ``InteractiveSubprocessLauncher``. The
verified pairs are widened to the one ``(--cli-version, --model)`` given, in
this process only: the adapters are handed that pair, the runtime and the
probe a gate that allows that model alone. The repository's
``VERIFIED_MODELS`` and production's wiring are never changed, and the CLI
version must already be a verified one (``codex_app_server.VERIFIED_VERSIONS``)
and be what ``codex --version`` answers. It checks, in turn:

1. the Codex connection's health probe (Decision 0100) is ``connected``;
2. one reviewer node over the fixed diff ``live_check_codex_review.diff`` (in
   this directory) passes the stream's checks (the item detection of Decision
   0089, 2 and the tools of Decision 0101: the stream was not refused) and its
   ``submit`` came back as the node's result (run only after 1 passed).

The credential is the ``auth.json`` of a login dedicated to this check (made
with ``CODEX_HOME=<dir> codex login``; never production's, never
``~/.codex``'s) that the Human passes with ``--auth``. It is read into memory
and placed (``CodexAuthFile``, as the backend places it) as ``auth.json`` in
each session's ``CODEX_HOME``, a directory under a new temporary directory
(0700) that is deleted at the end, also after a failure or an interruption.
A login Codex refreshed during a session (its refresh token is single-use) is
carried to the next session and written back to the ``--auth`` file at once,
atomically and keeping the file's mode and owner (again before the temporary
directory goes, when that failed), so the same file serves the next run; the
result says ``credential_refreshed`` and ``credential_written_back``. It
refuses to run as the
backend's user ``paw`` (or as root), without ``--auth``, and with an ``--auth``
under the production secret store (``/var/lib/paw/secrets`` and
``PAW_SECRET_STORE_DIR``). It reads no database, orchestrator, policy or
runtime configuration of production.

    python3 -I deploy/cli-session/live_check_codex.py \\
        --codex <codex executable> --cli-version 0.156.1 --model gpt-5.5 \\
        --auth <the dedicated auth.json> --out <result.json> [--sessions <dir>]

The exit code is 0 when both checks passed, 1 when not, 2 when it refused to
run (nothing was sent), 3 when a refreshed login could not be written back
(the ``--auth`` file may then be stale). The result JSON holds the checks' verdicts, the
versions, the model, the times and the review's answer (its summary and
findings); never the credential, and never the prompts (the fixture is named
by its path and SHA-256).
"""

import argparse
import asyncio
import contextlib
import datetime
import hashlib
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
HERE = BACKEND / "deploy" / "cli-session"
RELAY = HERE / "paw_mcp_relay.py"
FIXTURE = HERE / "live_check_codex_review.diff"
#: The production secret store's directory (Decision 0087, 1;
#: ``deploy/secret-store/README.md``); ``PAW_SECRET_STORE_DIR`` names it too.
SECRET_STORE = Path("/var/lib/paw/secrets")
SECRET_STORE_ENV = "PAW_SECRET_STORE_DIR"
#: The backend's user (Decision 0104, 3 A: never run as it).
BACKEND_USER = "paw"
#: The most of ``--auth`` that is read (a login is a few KiB).
MAX_AUTH_BYTES = 64 * 1024
PROBE_SECONDS = 300.0
NODE_SECONDS = 900.0
VERSION_SECONDS = 30.0
TITLE = "Live check: review a small diff"
GOAL = (
    "Review the diff given as the input's `diff` for correctness bugs. It"
    " changes `page`, whose docstring says pages are 1-based. Submit a short"
    " summary of what you found (each problem with the line it is on, or that"
    " there is none)."
)
_VERSION = re.compile(r"\b([0-9]+\.[0-9]+\.[0-9]+[0-9A-Za-z.+-]*)")


class Refused(Exception):
    """The check does not run (nothing was sent): ``code`` says why."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def _under(path: Path, directory: Path) -> bool:
    return path == directory or path.is_relative_to(directory)


def secret_stores(environ) -> list[Path]:
    stores = [SECRET_STORE]
    named = environ.get(SECRET_STORE_ENV)
    if named:
        stores.append(Path(named))
    return stores


def check_runner(users: set[str], euid: int) -> None:
    """Refuse the backend's user (the real or the effective one) and root
    (Decision 0104, 3 A)."""
    if BACKEND_USER in users:
        raise Refused("backend_user")
    if euid == 0:
        raise Refused("root")


def check_auth_path(auth: Path | None, environ) -> Path:
    """The ``--auth`` file to read: given, outside the secret store, a
    regular file. Checked on the path as given and on the resolved one."""
    if auth is None or not str(auth):
        raise Refused("no_credential")
    given = Path(os.path.abspath(auth))
    resolved = given.resolve(strict=False)
    for store in secret_stores(environ):
        absolute = Path(os.path.abspath(store))
        for directory in {absolute, absolute.resolve(strict=False)}:
            if _under(given, directory) or _under(resolved, directory):
                raise Refused("secret_store_path")
    if not resolved.is_file():
        raise Refused("no_credential")
    return resolved


def read_login(path: Path) -> str:
    """The login's text: a JSON object of at most ``MAX_AUTH_BYTES``."""
    with path.open("rb") as handle:
        data = handle.read(MAX_AUTH_BYTES + 1)
    if len(data) > MAX_AUTH_BYTES:
        raise Refused("credential_not_a_login")
    try:
        text = data.decode("utf-8")
        value = json.loads(text)
    except ValueError:
        raise Refused("credential_not_a_login") from None
    if not isinstance(value, dict):
        raise Refused("credential_not_a_login")
    return text


def check_pair(cli_version: str, model: str) -> None:
    """The version must be a verified one already (its recorder smoke passed,
    Decision 0101, 4); the model a valid model name."""
    from paw_backend.agents.codex_app_server import VERIFIED_VERSIONS
    from paw_backend.connections.validation import validate_model

    if cli_version not in VERIFIED_VERSIONS:
        raise Refused("version_not_verified")
    try:
        validate_model("model", model)
    except Exception:
        raise Refused("model_invalid") from None


async def codex_version(executable: tuple[str, ...], scratch: Path) -> str | None:
    """``codex --version``'s version, run without any login."""
    home = scratch / "version-home"
    home.mkdir(mode=0o700)
    process = await asyncio.create_subprocess_exec(
        *executable,
        "--version",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin", "HOME": str(home), "CODEX_HOME": str(home)},
    )
    try:
        async with asyncio.timeout(VERSION_SECONDS):
            out, _ = await process.communicate()
    except TimeoutError:
        process.kill()
        await process.wait()
        return None
    match = _VERSION.search(out.decode("ascii", "replace"))
    return match.group(1) if match else None


def write_file_back(path: Path, value: str) -> None:
    """Replace ``path`` with ``value`` atomically (a file aside in the same
    directory, ``fsync``, ``rename``, the directory's ``fsync``), keeping the
    file's mode and owner. Raises ``OSError`` when it could not."""
    status = os.stat(path)
    directory = path.parent
    aside = directory / f".{path.name}.{uuid.uuid4().hex}"
    mode = stat.S_IMODE(status.st_mode)
    fd = os.open(
        aside, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode
    )
    try:
        try:
            os.fchmod(fd, mode)  # whatever the umask
            if (status.st_uid, status.st_gid) != (os.geteuid(), os.getegid()):
                os.fchown(fd, status.st_uid, status.st_gid)
            data = value.encode("utf-8")
            while data:
                data = data[os.write(fd, data) :]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(aside, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(aside)
        raise
    dfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


class Login:
    """The login of this run, in memory: a refreshed one (the adapter's
    write-back, made only when Codex changed its ``auth.json``) replaces it
    for the next session and is written back to the ``--auth`` file at once
    (:func:`write_file_back`), so that the dedicated login stays usable for
    the next run. A write-back that failed is tried again by :meth:`settle`
    (before the temporary directory goes)."""

    def __init__(self, text: str, path: Path) -> None:
        from paw_backend.connections import Secret

        self._secret_type = Secret
        self._path = path
        self.secret = Secret(text)
        self.refreshed = False
        self.written = False
        self.error: str | None = None

    async def write_back(self, value: str):
        from paw_backend.connections import WriteBack

        self.secret = self._secret_type(value)
        self.refreshed = True
        self.written = False
        self._write()
        # The session goes on with the refreshed login either way.
        return WriteBack.WRITTEN if self.written else WriteBack.FAILED

    def settle(self) -> None:
        """Write a refreshed login back if that is still to do."""
        if self.refreshed and not self.written:
            self._write()

    def _write(self) -> None:
        try:
            write_file_back(self._path, self.secret.reveal())
        except Exception as error:
            # Only the class: the text may hold a path or anything.
            self.error = type(error).__name__
        else:
            self.written = True
            self.error = None


class OnePair:
    """The model gate of this process (Decision 0104, 3 A): the one model
    given, for Codex only."""

    def __init__(self, model: str) -> None:
        self.model = model

    async def allows(self, kind, model) -> bool:
        from paw_backend.connections import ConnectionKind

        return kind is ConnectionKind.CODEX and model == self.model

    async def default_model(self, kind):
        from paw_backend.connections import ConnectionKind

        return self.model if kind is ConnectionKind.CODEX else None


def _user_name(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def _seconds_since(seconds_from: float) -> float:
    return round(time.monotonic() - seconds_from, 1)


async def run_probe(
    executable: tuple[str, ...],
    sessions: Path,
    pairs: frozenset[tuple[str, str]],
    login: Login,
    launcher=None,
) -> dict:
    from paw_backend.agents.codex_cli import InteractiveSubprocessLauncher
    from paw_backend.agents.codex_credentials import CodexAuthFile
    from paw_backend.agents.codex_probe import CodexProbeAdapter
    from paw_backend.connections import ProbeSession

    (model,) = {m for _, m in pairs}
    adapter = CodexProbeAdapter(
        launcher or InteractiveSubprocessLauncher(),
        model=model,
        session_base=sessions,
        credentials=CodexAuthFile(),
        executable=executable,
        verified_models=pairs,
        models=OnePair(model),
    )
    kept: list[bool] = []

    async def bind(session_id):
        return True

    async def held():
        return True

    session = ProbeSession(
        bind_session=bind,
        still_held=held,
        write_back=login.write_back,
        cli_running=lambda: kept.append(True),
        connection_id=uuid.uuid4(),
    )
    started = time.monotonic()
    try:
        report = await asyncio.wait_for(
            adapter.check_health(login.secret, session), PROBE_SECONDS
        )
    finally:
        session.released(True)
    connected = report.outcome.value == "connected"
    return {
        "passed": connected and not kept,
        "outcome": report.outcome.value,
        "failure": None if report.failure is None else report.failure.value,
        "model": report.model,
        "tokens": [report.input_tokens, report.output_tokens],
        "cli_left_running": bool(kept),
        "seconds": _seconds_since(started),
    }


async def run_node(
    executable: tuple[str, ...],
    sessions: Path,
    pairs: frozenset[tuple[str, str]],
    login: Login,
    diff: str,
    *,
    node_seconds: float = NODE_SECONDS,
    launcher=None,
) -> dict:
    import paw_backend.agents.codex_cli as codex_cli
    from paw_backend.agents.codex_credentials import CodexAuthFile
    from paw_backend.connections import AgentSessionRequest, SessionResult
    from paw_backend.orchestrator.domain import NodeRole
    from paw_backend.orchestrator.runtime import NodeAssignment

    (model,) = {m for _, m in pairs}
    adapter = codex_cli.CodexSessionAdapter(
        launcher or codex_cli.InteractiveSubprocessLauncher(),
        credentials=CodexAuthFile(),
        executable=executable,
        verified_models=pairs,
    )
    seen: dict = {}

    class Placement:
        async def record(self, placement, *, agent, model):
            seen["placement"] = [placement.value, agent, model]

    class Policy:
        async def allows(self, assignment):
            return True

    class Connections:
        async def execute_session(self, kind, request, *, gate=None):
            if gate is not None:
                await gate()
            attachment = request.attachment
            seen["attachment"] = attachment
            seen["session"] = {
                "paw_tools": sorted(attachment.tools),
                "mcp_servers": sorted(
                    tomllib.loads(
                        codex_cli.session_config(attachment.spec, attachment.tools)
                    ).get("mcp_servers", {})
                ),
            }

            async def on_usage(i, o):
                seen["tokens"] = [i, o]
                return True

            async def bind(session_id):
                return True

            async def held():
                return True

            raw = await adapter.run_session(
                login.secret,
                AgentSessionRequest(
                    request.model,
                    request.prompt,
                    request.timeout_seconds,
                    on_usage,
                    attachment=attachment,
                    connection_id=uuid.uuid4(),
                    write_back=login.write_back,
                    cli_running=lambda: seen.setdefault("cli_left_running", True),
                    bind_session=bind,
                    still_held=held,
                ),
            )
            seen["adapter_failure"] = None if raw.failure is None else raw.failure.value
            return SessionResult(uuid.uuid4(), raw.submission, 0, 0, raw.failure, None)

    runtime = codex_cli.CodexCliRuntime(
        model=model,
        session_base=sessions,
        relay_command=(sys.executable, "-I", str(RELAY)),
        cloud_policy=Policy(),
        node_seconds=node_seconds,
        models=OnePair(model),
    )
    assignment = NodeAssignment(
        task_id=uuid.uuid4(),
        node_key="live-check",
        role=NodeRole.REVIEWER,
        title=TITLE,
        goal=GOAL,
        input={"diff": diff},
        upstream={},
        agent="codex",
        attempt=1,
        approach=0,
        tools=None,
        budget=None,
        worktrees={},
        roots={},
        placement=Placement(),
        connections=Connections(),
    )
    started = time.monotonic()
    outcome = await asyncio.wait_for(runtime.run_node(assignment), node_seconds + 60)
    attachment = seen.get("attachment")
    refused = attachment is not None and bool(attachment.refused)
    model_refused = attachment is not None and bool(attachment.model_refused)
    result = outcome.result
    submitted = result is not None and bool(result.summary.strip())
    tool_check = attachment is not None and not refused and not model_refused
    return {
        "passed": tool_check and submitted and "cli_left_running" not in seen,
        "tool_check_passed": tool_check,
        "stream_refused": refused,
        "model_refused": model_refused,
        "submitted": submitted,
        "error_class": outcome.error_class,
        "adapter_failure": seen.get("adapter_failure"),
        "session": seen.get("session"),
        "placement_recorded": seen.get("placement"),
        "tokens": seen.get("tokens"),
        "cli_left_running": "cli_left_running" in seen,
        "seconds": _seconds_since(started),
        # The review's answer (Decision 0104, 3 A: the Human reads it).
        "review": None
        if result is None
        else {
            "summary": result.summary,
            "discovered_facts": list(result.discovered_facts),
            "unresolved_questions": list(result.unresolved_questions),
        },
    }


def _backend_commit() -> str | None:
    try:
        answer = subprocess.run(
            ["git", "-C", str(BACKEND), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = answer.stdout.strip()
    return commit if answer.returncode == 0 and commit else None


async def live_check(
    executable: tuple[str, ...],
    cli_version: str,
    model: str,
    login: Login,
    scratch: Path,
    result: dict,
    *,
    node_seconds: float = NODE_SECONDS,
    launcher=None,
) -> dict:
    """Both checks in ``scratch`` (the caller deletes it), written into
    ``result``. ``Refused`` when the CLI is not the version given (before any
    login is placed)."""
    diff = FIXTURE.read_text(encoding="utf-8")
    result.update(
        {
            "started_at": _now(),
            "backend_commit": _backend_commit(),
            "fixture": {
                "path": str(FIXTURE.relative_to(BACKEND)),
                "sha256": hashlib.sha256(diff.encode()).hexdigest(),
            },
        }
    )
    version = await codex_version(executable, scratch)
    result["codex_version"] = version
    if version != cli_version:
        raise Refused("version_mismatch")
    # Decision 0104, 3 A: the verified pairs of this process only.
    pairs = frozenset({(cli_version, model)})
    sessions = scratch / "sessions"
    sessions.mkdir(mode=0o700)
    result["probe"] = await run_probe(executable, sessions, pairs, login, launcher)
    if result["probe"]["passed"]:
        result["node"] = await run_node(
            executable,
            sessions,
            pairs,
            login,
            diff,
            node_seconds=node_seconds,
            launcher=launcher,
        )
    else:
        result["node"] = {"passed": False, "skipped": "probe_not_connected"}
    result["finished_at"] = _now()
    result["passed"] = bool(result["probe"]["passed"] and result["node"]["passed"])
    return result


def run(
    arguments: argparse.Namespace,
    *,
    environ=os.environ,
    users: set[str] | None = None,
    euid: int | None = None,
    executable: tuple[str, ...] | None = None,
    launcher=None,
) -> tuple[int, dict]:
    """The check with its refusals and its cleanup: ``(exit code, result)``."""
    sys.path.insert(0, str(BACKEND))
    euid = os.geteuid() if euid is None else euid
    if users is None:
        users = {_user_name(euid), _user_name(os.getuid())}
    result: dict = {
        "check": "codex_live_check",
        "decision": "0104",
        "cli_version": arguments.cli_version,
        "model": arguments.model,
    }
    scratch: Path | None = None
    login: Login | None = None
    code = 1
    try:
        check_runner(users, euid)
        auth = check_auth_path(arguments.auth, environ)
        check_pair(arguments.cli_version, arguments.model)
        login = Login(read_login(auth), auth)
        base = arguments.sessions.resolve() if arguments.sessions else None
        if base is not None:
            base.mkdir(parents=True, exist_ok=True)
        scratch = Path(tempfile.mkdtemp(prefix="pawlc", dir=base))
        asyncio.run(
            live_check(
                executable or (str(arguments.codex),),
                arguments.cli_version,
                arguments.model,
                login,
                scratch,
                result,
                node_seconds=arguments.node_seconds,
                launcher=launcher,
            )
        )
        code = 0 if result["passed"] else 1
    except Refused as refused:
        result.update({"passed": False, "refused": refused.code, "finished_at": _now()})
        code = 2
    except Exception as error:
        # Only the exception's class: its text may hold anything.
        result.update(
            {"passed": False, "error": type(error).__name__, "finished_at": _now()}
        )
        code = 1
    finally:
        if login is not None:
            # Also after a failure or an interruption, before the temporary
            # directory goes: the refreshed login must reach ``--auth``.
            login.settle()
            if login.refreshed and not login.written:
                print(
                    "the refreshed login could not be written back"
                    f" ({login.error}): the --auth file may now be stale",
                    file=sys.stderr,
                )
        if scratch is not None:
            # The copies of the login and every CODEX_HOME are in it.
            shutil.rmtree(scratch, ignore_errors=True)
    if login is not None:
        result["credential_refreshed"] = login.refreshed
        result["credential_written_back"] = login.written
        if login.refreshed and not login.written:
            result["passed"] = False
            result["credential_write_back_error"] = login.error
            code = 3
    if scratch is not None:
        result["cleaned"] = not scratch.exists()
        if not result["cleaned"]:
            result["passed"] = False
            result["left"] = str(scratch)
            code = 1
    return code, result


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="live_check_codex.py", allow_abbrev=False)
    parser.add_argument("--codex", required=True, help="the codex executable")
    parser.add_argument(
        "--cli-version", required=True, help="the version of the pair (0.156.1)"
    )
    parser.add_argument("--model", required=True, help="the model of the pair")
    parser.add_argument(
        "--auth",
        type=Path,
        help="the auth.json of a login dedicated to this check (updated in"
        " place when Codex refreshes it; never production's or ~/.codex's)",
    )
    parser.add_argument("--out", required=True, type=Path, help="the result JSON")
    parser.add_argument(
        "--sessions",
        type=Path,
        help="where the temporary directory goes (short: the bridge socket is"
        " AF_UNIX; default: the system's temporary directory)",
    )
    parser.add_argument(
        "--node-seconds",
        type=float,
        default=NODE_SECONDS,
        help="the reviewer node's time limit",
    )
    return parser


def main(argv=None) -> int:
    arguments = parser().parse_args(argv)
    try:
        code, result = run(arguments)
    except KeyboardInterrupt:
        print("interrupted; the temporary directory was deleted", file=sys.stderr)
        return 130
    arguments.out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
