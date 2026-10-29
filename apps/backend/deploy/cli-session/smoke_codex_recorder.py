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
only MCP server) and the submit's summary came back as the node's result.

Run it as the user who develops, never with a real login, never as ``paw``::

    python3 -I deploy/cli-session/smoke_codex_recorder.py \\
        --codex <codex executable> --out <result.json> [--sessions <dir>]

The exit code is 0 when it passed, 1 when not. The result JSON holds the
requests (the tools, the model, the input item types; no prompt text).
"""

import argparse
import asyncio
import json
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
MODEL = "gpt-5.5-codex"
DUMMY_AUTH = '{"OPENAI_API_KEY": null, "tokens": null}'
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

    def __init__(self, model_tool_names) -> None:
        self.requests: list[dict] = []
        self._names = model_tool_names
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
                "tools": tools,
                "model_tool_names": names,
                "input_item_types": [
                    i.get("type")
                    for i in body.get("input") or []
                    if isinstance(i, dict)
                ],
            }
        )
        rid = f"resp_{uuid.uuid4().hex[:12]}"
        if len(self.requests) == 1 and "mcp__paw__submit" in names:
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
                "content": [{"type": "output_text", "text": "done"}],
            }
        return [
            {"type": "response.created", "response": {"id": rid}},
            {"type": "response.output_item.done", "item": item},
            {"type": "response.completed", "response": {"id": rid, "usage": USAGE}},
        ]


async def _version(codex: str) -> str:
    process = await asyncio.create_subprocess_exec(
        codex, "--version", stdout=asyncio.subprocess.PIPE
    )
    out, _ = await process.communicate()
    return out.decode().split()[-1]


async def smoke(codex: str, sessions: Path) -> dict:
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

    recorder = Recorder(model_tool_names)
    sessions_seen: list[dict] = []
    original = codex_cli.session_config

    def session_config(spec, tools):
        # Top-level keys come before the first table; the provider's last.
        text = (
            'model_provider = "pawsmoke"\n'
            + original(spec, tools)
            + "\n[model_providers.pawsmoke]\n"
            'name = "paw smoke recorder"\n'
            f'base_url = "http://127.0.0.1:{recorder.port}/v1"\n'
            'wire_api = "responses"\n'
            "requires_openai_auth = false\n"
        )
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
    try:
        version = await _version(codex)
        adapter = codex_cli.CodexSessionAdapter(
            codex_cli.InteractiveSubprocessLauncher(),
            credentials=Credentials(),
            executable=(codex,),
            verified_versions=frozenset({version}),
        )
        connections = Connections(adapter)
        placement = Placement()
        runtime = codex_cli.CodexCliRuntime(
            model=MODEL,
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
        recorder.close()
    session = (
        sessions_seen[0] if sessions_seen else {"paw_tools": [], "mcp_servers": []}
    )
    checks = [
        list(tool_check(r["tools"], session["paw_tools"], session["mcp_servers"]))
        for r in recorder.requests
    ]
    raw = Connections.raw
    submitted = outcome.result is not None and outcome.result.summary == "ok"
    return {
        "codex_version": version,
        "seconds": round(time.monotonic() - started, 1),
        "session": session,
        "tool_check": checks,
        "tool_check_passed": bool(checks) and not any(checks),
        "submitted": submitted,
        "passed": bool(checks) and not any(checks) and submitted,
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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="smoke_codex_recorder.py", allow_abbrev=False)
    parser.add_argument("--codex", required=True, help="the codex executable to test")
    parser.add_argument("--out", required=True, type=Path, help="the result JSON")
    parser.add_argument(
        "--sessions",
        type=Path,
        help="the session directories' base (short: the bridge socket is AF_UNIX)",
    )
    arguments = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="pawsm") as scratch:
        sessions = (arguments.sessions or Path(scratch)).resolve()
        sessions.mkdir(parents=True, exist_ok=True)
        result = asyncio.run(smoke(arguments.codex, sessions))
        result["sessions_left"] = sorted(p.name for p in sessions.iterdir())
    arguments.out.write_text(json.dumps(result, indent=2, default=str))
    print(json.dumps({k: v for k, v in result.items() if k != "requests"}, default=str))
    for request in result["requests"]:
        print("request", request["model"], request["model_tool_names"])
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
