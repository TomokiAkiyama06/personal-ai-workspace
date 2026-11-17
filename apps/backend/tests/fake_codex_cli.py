"""A fake ``codex`` for the Codex runtime's tests (no network, no provider).

    python fake_codex_cli.py --scenario <file> --report <file> <codex's args...>

``--version`` prints ``codex-cli <version>`` (the scenario's, ``0.162.1`` by
default) and counts the probe in ``<report>.probes``. ``app-server --listen
stdio://`` speaks the stdio protocol of ``codex app-server`` (``rust-v0.162.1``:
one JSON message per line, no ``"jsonrpc"`` field): it answers ``initialize``,
``thread/start`` and ``turn/start``, starts the MCP server of its
``$CODEX_HOME/config.toml`` (the real PAW relay) and talks MCP to it, and sends
the notifications the scenario says. The report file gets what the test
checks: the arguments, the environment's names, the config, the requests'
params, the tools the bridge listed and the answers of the tool calls, and
whether the credential file was there (its length, never its value), and
the model catalog the config's ``model_catalog_json`` names (where it is, and
each model's values that the override of Decision 0104 sets).

Without an MCP server in the config (the health probe's) no relay is started
and no tool is listed (and the scenario's ``probe_actions`` run, when given,
instead of its ``actions``). ``answer`` completes an agent message with the given
text; ``reroute`` sends ``model/rerouted``.
"""

import json
import os
import subprocess
import sys
import time
import tomllib


def main() -> int:
    argv = sys.argv[1:]
    scenario_path = argv[argv.index("--scenario") + 1]
    report_path = argv[argv.index("--report") + 1]
    args = argv[argv.index("--report") + 2 :]
    with open(scenario_path, encoding="utf-8") as handle:
        scenario = json.load(handle)
    version = scenario.get("version", "0.162.1")
    if args == ["--version"]:
        with open(report_path + ".probes", "a", encoding="utf-8") as handle:
            handle.write("probe\n")
        print(f"codex-cli {version}")
        return 0
    home = os.environ["CODEX_HOME"]
    with open(os.path.join(home, "config.toml"), "rb") as handle:
        config = tomllib.load(handle)
    auth = os.path.join(home, "auth.json")
    # The model catalog the config names (Decision 0104, 2 A): where it is,
    # and the values of each model's entry that the override sets.
    named = config.get("model_catalog_json")
    catalog = None
    if named is not None:
        catalog = {"path": named, "exists": os.path.isfile(named), "entries": {}}
        if catalog["exists"]:
            with open(named, encoding="utf-8") as handle:
                for entry in json.load(handle)["models"]:
                    catalog["entries"][entry["slug"]] = {
                        key: entry.get(key)
                        for key in (
                            "tool_mode",
                            "multi_agent_version",
                            "experimental_supported_tools",
                            "use_responses_lite",
                            "supports_search_tool",
                            "base_instructions",
                        )
                    }
    report = {
        "catalog": catalog,
        "args": args,
        "env": sorted(os.environ),
        "config": config,
        "auth_length": os.path.getsize(auth) if os.path.exists(auth) else None,
        "requests": {},
        "calls": [],
    }

    def save():
        # Atomically (a new file renamed over the report): the adapter may
        # stop this CLI (SIGTERM) right after the last line it sent, while
        # the last save runs; a report truncated in place was then empty
        # (CI run 44014182685). Killed mid-save, the previous report stays.
        partial = report_path + ".tmp"
        with open(partial, "w", encoding="utf-8") as handle:
            json.dump(report, handle)
        os.replace(partial, report_path)

    def out(message):
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()

    def receive():
        line = sys.stdin.readline()
        if not line:
            save()
            sys.exit(0)
        return json.loads(line)

    # initialize -> initialized -> thread/start -> turn/start
    message = receive()
    report["requests"][message["method"]] = message.get("params")
    out(
        {
            "id": message["id"],
            "result": {
                # ``user_agent_version``: a CLI whose two answers disagree.
                "userAgent": (
                    f"paw_test/{scenario.get('user_agent_version', version)}"
                    " (Linux 6; x86_64) paw"
                ),
                "codexHome": home,
                "platformFamily": "unix",
                "platformOs": "linux",
            },
        }
    )
    save()
    while True:
        message = receive()
        report["requests"][message["method"]] = message.get("params")
        save()
        if message["method"] == "thread/start":
            out({"id": message["id"], "result": {"thread": {"id": "thr_1"}}})
        elif message["method"] == "turn/start":
            out(
                {
                    "id": message["id"],
                    "result": {"turn": {"id": "turn_1", "status": "inProgress"}},
                }
            )
            break

    server = config.get("mcp_servers", {}).get("paw")
    relay = None
    if server is not None:
        relay = subprocess.Popen(
            [server["command"], *server["args"]],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
    next_id = [0]

    def rpc(method, params=None):
        next_id[0] += 1
        request = {"jsonrpc": "2.0", "id": next_id[0], "method": method}
        if params is not None:
            request["params"] = params
        relay.stdin.write(json.dumps(request).encode() + b"\n")
        relay.stdin.flush()
        while True:
            answer = json.loads(relay.stdout.readline())
            if answer.get("id") == next_id[0]:
                return answer

    def notify(method, params):
        out({"method": method, "params": {"threadId": "thr_1", **params}})

    def item(body):
        notify("item/started", {"turnId": "turn_1", "item": body})

    if relay is not None:
        rpc("initialize", {"protocolVersion": "2025-06-18"})
        report["listed"] = [t["name"] for t in rpc("tools/list")["result"]["tools"]]
    else:
        report["listed"] = []
    save()
    item({"type": "userMessage", "id": "item_0", "content": []})
    total = [0, 0]
    calls = [0]

    def usage(last):
        breakdown = {
            "totalTokens": sum(total),
            "inputTokens": total[0],
            "cachedInputTokens": 0,
            "cacheWriteInputTokens": 0,
            "outputTokens": total[1],
            "reasoningOutputTokens": 0,
        }
        notify(
            "thread/tokenUsage/updated",
            {
                "turnId": "turn_1",
                "tokenUsage": {
                    "total": breakdown,
                    "last": {
                        **breakdown,
                        "inputTokens": last[0],
                        "outputTokens": last[1],
                    },
                    "modelContextWindow": 200000,
                },
            },
        )

    # ``probe_actions``: what a run without an MCP server (the health probe's)
    # does instead, for a test that runs a probe and a node on one scenario.
    actions = scenario["actions"]
    if server is None and "probe_actions" in scenario:
        actions = scenario["probe_actions"]
    for action in actions:
        if "usage" in action:
            total[0] += action["usage"][0]
            total[1] += action["usage"][1]
            usage(action["usage"])
        elif "total" in action:
            total[:] = action["total"]
            usage(action["total"])
        elif "repeat_usage" in action:
            usage([0, 0])
        elif "call" in action:
            calls[0] += 1
            started = {
                "type": "mcpToolCall",
                "id": f"item_call_{calls[0]}",
                "server": "paw",
                "tool": action["call"],
                "status": "inProgress",
                "arguments": action.get("arguments", {}),
            }
            if not action.get("item_late"):
                item(started)
            answer = rpc(
                "tools/call",
                {"name": action["call"], "arguments": action.get("arguments", {})},
            )
            if action.get("item_late"):
                # The item's line comes after the call ended (a slow reader).
                time.sleep(0.3)
                item(started)
            report["calls"].append(answer["result"])
            save()
        elif "item" in action:
            item(action["item"])
        elif "answer" in action:
            body = {"type": "agentMessage", "id": "item_answer", "text": ""}
            item(body)
            notify(
                "item/completed",
                {"turnId": "turn_1", "item": {**body, "text": action["answer"]}},
            )
        elif "reroute" in action:
            notify(
                "model/rerouted",
                {
                    "turnId": "turn_1",
                    "fromModel": action["reroute"][0],
                    "toModel": action["reroute"][1],
                    "reason": "highRiskCyberActivity",
                },
            )
        elif "error" in action:
            notify(
                "error",
                {
                    "turnId": "turn_1",
                    "willRetry": action.get("will_retry", False),
                    "error": {"message": "x", "codexErrorInfo": action["error"]},
                },
            )
        elif "request" in action:
            out({"id": 100, "method": action["request"], "params": {}})
        elif "complete" in action:
            turn = {"id": "turn_1", "status": action["complete"], "items": []}
            if "turn_error" in action:
                turn["error"] = {"message": "x", "codexErrorInfo": action["turn_error"]}
            notify("turn/completed", {"turn": turn})
        elif "refresh" in action:
            # A refreshed login, saved as ``rust-v0.162.1`` saves it
            # (``login/src/auth/storage.rs``): truncated and rewritten in
            # place, never renamed over.
            with open(auth, "r+", encoding="utf-8") as handle:
                handle.truncate(0)
                handle.write(action["refresh"])
        elif "sleep" in action:
            time.sleep(action["sleep"])
        elif "exit" in action:
            save()
            return action["exit"]
    save()
    # Like the real server: stays until its stdin ends.
    while sys.stdin.readline():
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
