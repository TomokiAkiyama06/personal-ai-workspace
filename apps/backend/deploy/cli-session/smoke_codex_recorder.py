#!/usr/bin/env python3
"""The recorder smoke test of a Codex CLI version (Decision 0089, 2; Decision
0101, 4; issue #305). Nothing is sent to OpenAI.

The real ``CodexCliRuntime`` -> ``CodexSessionAdapter`` (``codex app-server``)
-> ``McpBridge`` -> ``paw_mcp_relay.py`` run one reviewer node with
``InteractiveSubprocessLauncher``. The session's ``config.toml`` (the
backend's own ``session_config``) gets one extra model provider: a recording
server on 127.0.0.1 speaking a minimal Responses API (SSE). It saves every
request's ``tools`` (what Codex offers the model) and answers: first a call of
the PAW ``submit``, then a short message. The placed ``auth.json`` is a dummy
(the custom provider needs no credential); the verified versions are widened
to the tested version for this run only.

The result passes when every request passes ``codex_app_server.tool_check``
(the PAW tools, and only the MCP resource tools besides, while ``paw`` is the
only MCP server), no request offers tools in an ``additional_tools`` input
item instead (the form of catalog models such as ``gpt-6-sol``: recorded,
never passed; Decision 0103, issue #341), and the submit's summary came back
as the node's result. ``--model`` picks the model of the run (the pair of a
version and a model, Decision 0103, 3 A).

The session's model catalog is the backend's own (Decision 0104, 2 A: the
adapters write the repository's catalog of the version with the model's
override, ``codex_catalog``, and ``model_catalog_json`` names it): this
script has no override of its own. ``--without-catalog`` drops the
``model_catalog_json`` line from the session's ``config.toml`` (Codex then
uses its own catalog), to show that the override is what makes a catalog
model such as ``gpt-6-sol`` pass. The result's ``catalog`` names the
repository's file (and its SHA-256) and the session model's overridden
values as the session's file had them.

With ``--serverless-list`` (Decision 0106, 3 A; issue #341) the recorder's
first answer is a call of ``list_mcp_resources`` without any argument (no
``server``: the item says ``codex``), and the ``submit`` comes in the answer
after it. The result's ``serverless_list`` then says what happened: the MCP
items the stream saw (type, server, tool), the bridge's lines of methods it
does not serve, and the call's output the model got back (its first 300
characters). It passes when the item came as ``mcpToolCall`` with ``server
= "codex"``, the bridge logged exactly one ``resources/list`` (and no other
unserved method), ``paw`` was the session's only MCP server, the model got an
error back (``list_call_output_kinds`` is ``["error"]``; Codex's empty list
``{"resources": []}`` is ``empty_list``, not an error), and the following
``submit`` came back as the node's result.

With ``--probe`` it runs the Codex connection's health probe instead (Decision
0100, 10: ``CodexProbeAdapter`` with the probe's own ``config.toml``, no MCP
server): the recorder answers ``PAW_PROBE_OK`` and the result passes when
there was exactly one request, it offered the model no tool at all, and the
probe's verdict is ``connected``.

Run it as the user who develops, never with a real login, never as ``paw``::

    python3 -I deploy/cli-session/smoke_codex_recorder.py \\
        --codex <codex executable> --out <result.json> [--sessions <dir>] \\
        [--probe | --serverless-list] [--model <model>] [--without-catalog]

The exit code is 0 when it passed, 1 when not. The result JSON holds the
requests (the tools, the model, the input item types; no prompt text).
"""

import argparse
import asyncio
import hashlib
import json
import logging
import sys
import tempfile
import threading
import time
import tomllib
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
RELAY = BACKEND / "deploy" / "cli-session" / "paw_mcp_relay.py"
MODEL = "gpt-6-sol"
#: The session's ``config.toml`` line that names its catalog (Decision 0104).
CATALOG_KEY = "model_catalog_json"
DUMMY_AUTH = '{"OPENAI_API_KEY": null, "tokens": null}'
#: The ``--serverless-list`` step's call (Decision 0106, 3 A).
LIST_CALL_ID = "call_list"
USAGE = {
    "input_tokens": 1000,
    "input_tokens_details": {"cached_tokens": 200},
    "output_tokens": 50,
    "output_tokens_details": {"reasoning_tokens": 10},
    "total_tokens": 1050,
}


def _sse(event: dict) -> bytes:
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()


class Recorder:
    """The 127.0.0.1 model provider: records the requests, calls submit once."""

    def __init__(
        self, model_tool_names, message: str = "done", *, serverless_list=False
    ) -> None:
        self.requests: list[dict] = []
        self._names = model_tool_names
        self._message = message
        self._serverless_list = serverless_list
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("content-length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                events = recorder.answer(body)
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()
                for event in events:
                    self.wfile.write(_sse(event))
                    self.wfile.flush()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self.port = self._server.server_address[1]

    def close(self) -> None:
        self._server.shutdown()

    def answer(self, body: dict) -> list[dict]:
        tools = body.get("tools") or []
        names = self._names(tools)
        self.requests.append(
            {
                "model": body.get("model"),
                # The request's top-level fields (names only, Decision 0104).
                "body_keys": sorted(body),
                "tools": tools,
                "model_tool_names": names,
                "input_item_types": [
                    i.get("type")
                    for i in body.get("input") or []
                    if isinstance(i, dict)
                ],
                # Decision 0103 (#341): a catalog model may offer its tools in
                # an ``additional_tools`` input item instead of ``tools``;
                # recorded (not checked) so that the run shows it.
                "additional_tools": [
                    i
                    for i in body.get("input") or []
                    if isinstance(i, dict) and i.get("type") == "additional_tools"
                ],
                # Decision 0106, 3 A: what the model got back for the step's
                # call (the recorder's own call: no prompt text).
                "list_call_outputs": [
                    str(i.get("output"))[:300]
                    for i in body.get("input") or []
                    if isinstance(i, dict)
                    and i.get("type") == "function_call_output"
                    and i.get("call_id") == LIST_CALL_ID
                ],
            }
        )
        rid = f"resp_{uuid.uuid4().hex[:12]}"
        submit_at = 2 if self._serverless_list else 1
        if (
            self._serverless_list
            and len(self.requests) == 1
            and "list_mcp_resources" in names
        ):
            # A top-level function call without ``server`` (Decision 0106).
            item = {
                "type": "function_call",
                "id": "fc_list",
                "call_id": LIST_CALL_ID,
                "name": "list_mcp_resources",
                "arguments": "{}",
            }
        elif len(self.requests) == submit_at and "mcp__paw__submit" in names:
            item = {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "submit",
                "namespace": "mcp__paw",
                "arguments": json.dumps({"summary": "ok"}),
            }
        else:
            item = {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "content": [{"type": "output_text", "text": self._message}],
            }
        return [
            {"type": "response.created", "response": {"id": rid}},
            {"type": "response.output_item.done", "item": item},
            {"type": "response.completed", "response": {"id": rid, "usage": USAGE}},
        ]


def with_recorder(text: str, port: int, *, catalog: bool = True) -> str:
    """The backend's ``config.toml`` with the recorder as its provider
    (top-level keys come before the first table; the provider's last),
    without its catalog line when ``catalog`` is false."""
    if not catalog:
        text = "".join(
            line
            for line in text.splitlines(keepends=True)
            if not line.startswith(CATALOG_KEY + " ")
        )
    return (
        'model_provider = "pawsmoke"\n' + text + "\n[model_providers.pawsmoke]\n"
        'name = "paw smoke recorder"\n'
        f'base_url = "http://127.0.0.1:{port}/v1"\n'
        'wire_api = "responses"\n'
        "requires_openai_auth = false\n"
    )


def catalog_seen(text: str, model: str, version: str) -> dict:
    """What the result says of the session's catalog (see the module)."""
    sys.path.insert(0, str(BACKEND))
    from paw_backend.agents.codex_catalog import (
        OVERRIDES,
        OVERRIDES_IF_PRESENT,
        catalog_path,
    )

    config = tomllib.loads(text)
    named = config.get(CATALOG_KEY)
    seen: dict = {"in_backend_config": named is not None}
    repository = catalog_path(version)
    seen["repository_file"] = repository.name
    if repository.is_file():
        seen["repository_sha256"] = hashlib.sha256(repository.read_bytes()).hexdigest()
    if named is not None:
        entries = json.loads(Path(named).read_text())["models"]
        (entry,) = [e for e in entries if e.get("slug") == model]
        seen["entry"] = {
            key: entry.get(key)
            for key in (
                *OVERRIDES,
                *(k for k in OVERRIDES_IF_PRESENT if k in entry),
                "supports_experimental_context",
            )
        }
    return seen


async def _version(codex: str) -> str:
    process = await asyncio.create_subprocess_exec(
        codex, "--version", stdout=asyncio.subprocess.PIPE
    )
    out, _ = await process.communicate()
    return out.decode().split()[-1]


class _Lines(logging.Handler):
    """The bridge's log lines (their fixed text, Decision 0101, 3)."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


async def smoke(
    codex: str,
    sessions: Path,
    model: str = MODEL,
    *,
    catalog: bool = True,
    serverless_list: bool = False,
) -> dict:
    sys.path.insert(0, str(BACKEND))
    import paw_backend.agents.codex_cli as codex_cli
    from paw_backend.agents.codex_app_server import model_tool_names, tool_check
    from paw_backend.connections import (
        AgentSessionRequest,
        Secret,
        SessionResult,
    )
    from paw_backend.orchestrator.domain import NodeRole
    from paw_backend.orchestrator.runtime import NodeAssignment

    recorder = Recorder(model_tool_names, serverless_list=serverless_list)
    sessions_seen: list[dict] = []
    catalogs: list[dict] = []
    original = codex_cli.session_config
    original_stream = codex_cli.CodexStream
    version = await _version(codex)
    mcp_items: list[dict] = []

    class RecordingStream(original_stream):
        """The adapter's own stream; records each MCP item's type, server and
        tool (nothing else of it) before the stream's check."""

        def _item(self, item):
            if isinstance(item, dict) and item.get("type") == "mcpToolCall":
                mcp_items.append(
                    {key: item.get(key) for key in ("type", "server", "tool", "status")}
                )
            return super()._item(item)

    bridge_log = logging.getLogger("paw_backend.agents.bridge")
    bridge_lines = _Lines()
    bridge_level = bridge_log.level

    def session_config(spec, tools, catalog_file):
        backend = original(spec, tools, catalog_file)
        catalogs.append(catalog_seen(backend, model, version))
        text = with_recorder(backend, recorder.port, catalog=catalog)
        config = tomllib.loads(text)
        sessions_seen.append(
            {
                "paw_tools": sorted(tools),
                "mcp_servers": sorted(config.get("mcp_servers", {})),
            }
        )
        return text

    class Credentials:
        collected = None

        async def place(self, secret, codex_home):
            (Path(codex_home) / "auth.json").write_text(secret.reveal())

        async def collect(self, secret, codex_home, write_back):
            Credentials.collected = (Path(codex_home) / "auth.json").exists()

    class Placement:
        recorded: list = []

        async def record(self, placement, *, agent, model):
            self.recorded.append([placement.value, agent, model])

    class Policy:
        async def allows(self, assignment):
            return True

    class Connections:
        turns: list = []
        raw = None

        def __init__(self, adapter):
            self.adapter = adapter

        async def execute_session(self, kind, request, *, gate=None):
            if gate is not None:
                await gate()

            async def on_usage(i, o):
                self.turns.append([i, o])
                return True

            raw = await self.adapter.run_session(
                Secret(DUMMY_AUTH),
                AgentSessionRequest(
                    request.model,
                    request.prompt,
                    request.timeout_seconds,
                    on_usage,
                    attachment=request.attachment,
                    connection_id=uuid.uuid4(),
                ),
            )
            Connections.raw = raw
            return SessionResult(uuid.uuid4(), raw.submission, 0, 0, raw.failure, None)

    codex_cli.session_config = session_config
    codex_cli.CodexStream = RecordingStream
    bridge_log.addHandler(bridge_lines)
    bridge_log.setLevel(logging.INFO)
    try:
        adapter = codex_cli.CodexSessionAdapter(
            codex_cli.InteractiveSubprocessLauncher(),
            credentials=Credentials(),
            executable=(codex,),
            verified_versions=frozenset({version}),
        )
        connections = Connections(adapter)
        placement = Placement()
        runtime = codex_cli.CodexCliRuntime(
            model=model,
            session_base=sessions,
            relay_command=(sys.executable, "-I", str(RELAY)),
            cloud_policy=Policy(),
            node_seconds=300.0,
        )
        assignment = NodeAssignment(
            task_id=uuid.uuid4(),
            node_key="smoke",
            role=NodeRole.REVIEWER,
            title="Smoke: answer ok",
            goal="Submit a result whose summary is exactly 'ok'.",
            input={},
            upstream={},
            agent="codex",
            attempt=1,
            approach=0,
            tools=None,
            budget=None,
            worktrees={},
            roots={},
            placement=placement,
            connections=connections,
        )
        started = time.monotonic()
        outcome = await asyncio.wait_for(runtime.run_node(assignment), 200)
    finally:
        codex_cli.session_config = original
        codex_cli.CodexStream = original_stream
        bridge_log.removeHandler(bridge_lines)
        bridge_log.setLevel(bridge_level)
        recorder.close()
    session = (
        sessions_seen[0] if sessions_seen else {"paw_tools": [], "mcp_servers": []}
    )
    checks = [
        list(tool_check(r["tools"], session["paw_tools"], session["mcp_servers"]))
        # Tools offered outside ``tools`` are not the checked form: fail.
        + (["additional_tools offered"] if r["additional_tools"] else [])
        for r in recorder.requests
    ]
    raw = Connections.raw
    submitted = outcome.result is not None and outcome.result.summary == "ok"
    passed = bool(checks) and not any(checks) and submitted
    step = None
    if serverless_list:
        step = serverless_list_seen(
            mcp_items, bridge_lines.lines, recorder.requests, session
        )
        passed = passed and step["passed"]
    return {
        "codex_version": version,
        "model": model,
        "catalog_override": catalog,
        "serverless_list": step,
        "catalog": catalogs[0] if catalogs else None,
        "seconds": round(time.monotonic() - started, 1),
        "session": session,
        "tool_check": checks,
        "tool_check_passed": bool(checks) and not any(checks),
        "submitted": submitted,
        "passed": passed,
        "mcp_items": mcp_items,
        "bridge_unserved_lines": [
            line for line in bridge_lines.lines if "is not served" in line
        ],
        "placement_recorded": placement.recorded,
        "turns_reported": connections.turns,
        "adapter_failure": None
        if raw is None or raw.failure is None
        else raw.failure.value,
        "adapter_tokens": None
        if raw is None
        else [raw.input_tokens, raw.output_tokens],
        "outcome": repr(outcome)[:500],
        "auth_file_present_at_collect": Credentials.collected,
        "requests": recorder.requests,
    }


def output_kind(output: str) -> str:
    """What the list call gave the model back: ``empty_list`` (Codex's
    ``{"resources": []}``), ``resources`` (a list with something in it) or
    ``error`` (anything that is not a resource list)."""
    try:
        value = json.loads(output)
    except ValueError:
        return "error"
    listed = value.get("resources") if isinstance(value, dict) else None
    if not isinstance(listed, list):
        return "error"
    return "resources" if listed else "empty_list"


def serverless_list_seen(
    items: list[dict], lines: list[str], requests: list[dict], session: dict
) -> dict:
    """The ``--serverless-list`` step's checks (Decision 0106, 3 A). The
    call's output is the one of the first request that carries it (every
    later request resends the conversation)."""
    unserved = [line for line in lines if "is not served" in line]
    listed = [line for line in unserved if "method resources/list " in line]
    item_seen = any(
        i["type"] == "mcpToolCall"
        and i["server"] == "codex"
        and i["tool"] == "list_mcp_resources"
        for i in items
    )
    outputs = next(
        (r["list_call_outputs"] for r in requests if r["list_call_outputs"]), []
    )
    kinds = [output_kind(o) for o in outputs]
    only_paw = session.get("mcp_servers") == ["paw"]
    one_line = len(listed) == 1 and len(unserved) == 1
    error_back = kinds == ["error"]
    return {
        "item_codex_list_mcp_resources": item_seen,
        "only_paw_configured": only_paw,
        "bridge_resources_list_lines": len(listed),
        "bridge_unserved_lines": len(unserved),
        "list_call_outputs": outputs,
        "list_call_output_kinds": kinds,
        "error_returned_to_model": error_back,
        "passed": item_seen and only_paw and one_line and error_back,
    }


async def probe_smoke(
    codex: str, sessions: Path, model: str = MODEL, *, catalog: bool = True
) -> dict:
    """The health probe's smoke (see the module)."""
    sys.path.insert(0, str(BACKEND))
    import paw_backend.agents.codex_probe as codex_probe
    from paw_backend.agents.codex_app_server import model_tool_names
    from paw_backend.agents.codex_cli import InteractiveSubprocessLauncher
    from paw_backend.connections import ProbeSession, Secret, WriteBack

    recorder = Recorder(model_tool_names, message="PAW_PROBE_OK")
    configs: list[dict] = []
    catalogs: list[dict] = []
    original = codex_probe.probe_config
    version = await _version(codex)

    def probe_config(catalog_file):
        backend = original(catalog_file)
        catalogs.append(catalog_seen(backend, model, version))
        text = with_recorder(backend, recorder.port, catalog=catalog)
        configs.append(
            {"mcp_servers": sorted(tomllib.loads(text).get("mcp_servers", {}))}
        )
        return text

    class Credentials:
        async def place(self, secret, codex_home):
            (Path(codex_home) / "auth.json").write_text(secret.reveal())

        async def collect(self, secret, codex_home, write_back):
            return None

    async def bind(session):
        return True

    async def held():
        return True

    async def write_back(value):
        return WriteBack.WRITTEN

    codex_probe.probe_config = probe_config
    try:
        adapter = codex_probe.CodexProbeAdapter(
            InteractiveSubprocessLauncher(),
            model=model,
            session_base=sessions,
            credentials=Credentials(),
            executable=(codex,),
            verified_versions=frozenset({version}),
        )
        session = ProbeSession(
            bind_session=bind,
            still_held=held,
            write_back=write_back,
            cli_running=lambda: None,
        )
        started = time.monotonic()
        try:
            report = await asyncio.wait_for(
                adapter.check_health(Secret(DUMMY_AUTH), session), 200
            )
        finally:
            session.released(True)
    finally:
        codex_probe.probe_config = original
        recorder.close()
    offered = [r["model_tool_names"] for r in recorder.requests]
    no_tools = (
        bool(offered)
        and all(names == [] for names in offered)
        and not any(r["additional_tools"] for r in recorder.requests)
    )
    connected = report.outcome.value == "connected"
    return {
        "mode": "probe",
        "codex_version": version,
        "model": model,
        "catalog_override": catalog,
        "catalog": catalogs[0] if catalogs else None,
        "seconds": round(time.monotonic() - started, 1),
        "config": configs[0] if configs else None,
        "requests_count": len(recorder.requests),
        "no_tools": no_tools,
        "outcome": report.outcome.value,
        "failure": None if report.failure is None else report.failure.value,
        "tokens": [report.input_tokens, report.output_tokens],
        "passed": no_tools and len(recorder.requests) == 1 and connected,
        "requests": recorder.requests,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="smoke_codex_recorder.py", allow_abbrev=False)
    parser.add_argument("--codex", required=True, help="the codex executable to test")
    parser.add_argument("--out", required=True, type=Path, help="the result JSON")
    parser.add_argument(
        "--sessions",
        type=Path,
        help="the session directories' base (short: the bridge socket is AF_UNIX)",
    )
    parser.add_argument(
        "--model",
        default=MODEL,
        help="the model of the run (Decision 0103, 3 A: a pair's smoke)",
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="run the connection's health probe instead of a node",
    )
    parser.add_argument(
        "--serverless-list",
        action="store_true",
        help="first call list_mcp_resources without server (Decision 0106, 3 A)",
    )
    parser.add_argument(
        "--without-catalog",
        action="store_true",
        help="drop the session's model_catalog_json (Codex's own catalog)",
    )
    arguments = parser.parse_args(argv)
    if arguments.probe and arguments.serverless_list:
        parser.error("--serverless-list is a node's step, not the probe's")
    with tempfile.TemporaryDirectory(prefix="pawsm") as scratch:
        sessions = (arguments.sessions or Path(scratch)).resolve()
        sessions.mkdir(parents=True, exist_ok=True)
        if arguments.probe:
            run = probe_smoke(
                arguments.codex,
                sessions,
                arguments.model,
                catalog=not arguments.without_catalog,
            )
        else:
            run = smoke(
                arguments.codex,
                sessions,
                arguments.model,
                catalog=not arguments.without_catalog,
                serverless_list=arguments.serverless_list,
            )
        result = asyncio.run(run)
        result["sessions_left"] = sorted(p.name for p in sessions.iterdir())
    arguments.out.write_text(json.dumps(result, indent=2, default=str))
    print(json.dumps({k: v for k, v in result.items() if k != "requests"}, default=str))
    for request in result["requests"]:
        print("request", request["model"], request["model_tool_names"])
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
