"""A fake ``claude`` for the health probe's tests (no network, no provider,
no MCP server).

    python fake_claude_probe_cli.py --scenario <file> --report <file> <flags...>

It reads the task from stdin and writes ``stream-json`` lines as the scenario
says. The report file gets the flags, the task, the MCP configuration it was
given, whether the token arrived in the environment (its length, never its
value) and the names of the environment's variables.
"""

import json
import os
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
    with open(flags[flags.index("--mcp-config") + 1], encoding="utf-8") as handle:
        mcp = json.load(handle)
    report = {
        "flags": flags,
        "task": task,
        "mcp": mcp,
        "token_length": len(token),
        "env": sorted(k for k in os.environ if k != "CLAUDE_CODE_OAUTH_TOKEN"),
    }
    with open(report_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle)

    def out(message):
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()

    out(
        {
            "type": "system",
            "subtype": "init",
            "tools": scenario.get("init_tools", []),
            "mcp_servers": scenario.get("mcp_servers", []),
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
                    "usage": {"input_tokens": input_tokens, "output_tokens": 1},
                },
            }
            out({"type": "stream_event", "event": start})
            delta = {"type": "message_delta", "usage": {"output_tokens": output_tokens}}
            out({"type": "stream_event", "event": delta})
            out({"type": "stream_event", "event": {"type": "message_stop"}})
        elif "retry" in action:
            out({"type": "system", "subtype": "api_retry", "error": action["retry"]})
        elif "result" in action:
            out({"type": "result", **action["result"]})
        elif "sleep" in action:
            time.sleep(action["sleep"])
        elif "exit" in action:
            return action["exit"]
    return 0


if __name__ == "__main__":
    sys.exit(main())
