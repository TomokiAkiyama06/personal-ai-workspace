"""A fake ``claude`` for the CLI runtime's tests (no network, no provider).

    python fake_claude_cli.py --scenario <file> --report <file> <claude's flags...>

It reads the task from stdin, starts the MCP server of ``--mcp-config`` (the
real PAW relay) and talks MCP to it, and writes ``stream-json`` lines as the
scenario says. The report file gets what the test checks: the flags, whether
the token arrived in the environment (and its length, never its value), the
tools the bridge listed and the answers of the tool calls.
"""

import json
import os
import subprocess
import sys
import time
import uuid


def main() -> int:
    argv = sys.argv[1:]
    scenario_path = argv[argv.index("--scenario") + 1]
    report_path = argv[argv.index("--report") + 1]
    flags = argv[argv.index("--report") + 2 :]
    with open(scenario_path, encoding="utf-8") as handle:
        scenario = json.load(handle)
    task = sys.stdin.read()
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    report = {
        "flags": flags,
        "task": task,
        "token_length": len(token),
        "env": sorted(k for k in os.environ if k != "CLAUDE_CODE_OAUTH_TOKEN"),
        "calls": [],
    }

    def save():
        with open(report_path, "w", encoding="utf-8") as handle:
            json.dump(report, handle)

    config_path = flags[flags.index("--mcp-config") + 1]
    with open(config_path, encoding="utf-8") as handle:
        server = json.load(handle)["mcpServers"]["paw"]
    relay = subprocess.Popen(
        [server["command"], *server["args"]],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    next_id = [0]

    def rpc(method, params=None):
        next_id[0] += 1
        message = {"jsonrpc": "2.0", "id": next_id[0], "method": method}
        if params is not None:
            message["params"] = params
        relay.stdin.write(json.dumps(message).encode() + b"\n")
        relay.stdin.flush()
        while True:
            answer = json.loads(relay.stdout.readline())
            if answer.get("id") == next_id[0]:
                return answer

    def out(message):
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()

    rpc("initialize", {"protocolVersion": "2025-06-18"})
    listed = [t["name"] for t in rpc("tools/list")["result"]["tools"]]
    report["listed"] = listed
    save()
    tools = scenario.get("init_tools") or [
        *(f"mcp__paw__{name}" for name in listed),
        "EndConversation",
    ]
    out(
        {
            "type": "system",
            "subtype": "init",
            "tools": tools,
            "mcp_servers": scenario.get(
                "mcp_servers", [{"name": "paw", "status": "connected"}]
            ),
        }
    )
    for action in scenario["actions"]:
        if "turn" in action:
            message_id = "msg_" + uuid.uuid4().hex
            input_tokens, output_tokens = action["turn"]
            start = {
                "type": "message_start",
                "message": {
                    "id": message_id,
                    "usage": {
                        "input_tokens": input_tokens,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                        "output_tokens": 1,
                    },
                },
            }
            out({"type": "stream_event", "event": start})
            if not action.get("no_delta"):
                delta = {
                    "type": "message_delta",
                    "usage": {"output_tokens": output_tokens},
                }
                out({"type": "stream_event", "event": delta})
            out({"type": "stream_event", "event": {"type": "message_stop"}})
        elif "call" in action:
            answer = rpc(
                "tools/call",
                {"name": action["call"], "arguments": action.get("arguments", {})},
            )
            report["calls"].append(answer["result"])
            save()
        elif "retry" in action:
            out({"type": "system", "subtype": "api_retry", "error": action["retry"]})
        elif "result" in action:
            out({"type": "result", **action["result"]})
        elif "sleep" in action:
            time.sleep(action["sleep"])
        elif "exit" in action:
            save()
            return action["exit"]
    save()
    return 0


if __name__ == "__main__":
    sys.exit(main())
