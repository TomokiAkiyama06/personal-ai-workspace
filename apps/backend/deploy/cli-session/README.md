# CLI の Session の Container の配備（Claude Code の Subscription）

更新日: 2026-10-13

[Decision 0083](../../../../docs/decisions/0083-agent-runtime-adapters.md) の 6 と [Decision 0087](../../../../docs/decisions/0087-cli-secret-store-and-egress.md) の 3・4（どちらも Approved）。
Backend の側は `paw_backend/agents/cli_session.py` の `SystemdLauncher`、`claude_cli.py` の `ClaudeSessionAdapter` / `ClaudeCliRuntime`、`bridge.py` の `McpBridge`。
**CLI の段は組み立てで無効のまま**です（`CloudPolicy` の Decision と Provider の規約の確認まで）。この Directory の File は、有効にする前に Human が配備して確かめるためのものです。

## 流れ

```text
Backend（User paw）
  ├─ /run/paw-cli/<uuid>/（0750、Group paw-cli-bridge: /run/paw-cli の setgid で継ぐ）
  │    bridge.sock（MCP の Bridge）・bridge.token・mcp.json・settings.json・config/
  │    launch.json（CLI の引数と許可した環境変数）・credential.env（CLAUDE_CODE_OAUTH_TOKEN の 1 行、0640）・prompt・stream.sock
  └─ systemctl start --no-block paw-cli-session@<uuid>.service（polkit がこの名前の start / stop だけを許す）
       └─ paw_cli_session_run.py <uuid>（User paw-agent-cli、硬くした Unit）
            └─ podman run --rm --network none --read-only --cap-drop all --userns keep-id
                 -v /run/paw-cli/<uuid>（同じ Path）-v /run/paw-egress/egress.sock --env-file credential.env
                 <Image@sha256:...> /opt/paw/start-claude <引数>   stdin = prompt、stdout = stream.sock
                   ├─ paw_egress.py forward（127.0.0.1:3128 → egress.sock）
                   └─ claude -p ...（MCP の Server = paw_mcp_relay.py → bridge.sock）
```

止めるのは `systemctl stop`（Unit の `TimeoutStopSec=5s`: SIGTERM → 5 秒 → SIGKILL、`ExecStopPost` で Container を消す）。Session の Directory は終わりに Backend が消す（Token の File も一緒に消える）。

## 配備（root）

1. Account と Group: `useradd --system --home-dir /var/lib/paw-agent-cli --create-home --shell /usr/sbin/nologin paw-agent-cli`、`groupadd --system paw-cli-bridge`（`paw-egress` の README と同じ Group）、`usermod -aG paw-cli-bridge paw-agent-cli`。Backend の User（`paw`）は Group に入れない（Directory の所有者なので要らない）。Rootless の Podman の `XDG_RUNTIME_DIR` は Unit の `RuntimeDirectory`（`/run/paw-cli-session/<uuid>`）にする（`ProtectHome=yes` が `/run/user` を隠すため。Linger は要らない）。
2. `/run/paw-cli`: `paw-backend.service` に `RuntimeDirectory=paw-cli` と `RuntimeDirectoryMode=2750` を足し、Group を `paw-cli-bridge` にする（`tmpfiles.d` の `d /run/paw-cli 2750 paw paw-cli-bridge -` でもよい）。
3. `install -m 0755 paw_cli_session_run.py /usr/local/lib/paw/`、`paw-cli-session@.service` を `/etc/systemd/system/`、`50-paw-cli-session.rules` を `/etc/polkit-1/rules.d/`、`cli-session.example.toml` を `/etc/paw/cli-session.toml`（Image の Digest を入れる）。
4. Image: `Containerfile` の Base の Digest と Claude Code の版を固定して `paw-agent-cli` で Build し、Digest を `cli-session.toml` に入れる。

## 確かめること（有効にする前）

- **Rootless の Podman が、この硬くした Unit（`NoNewPrivileges`・空の `CapabilityBoundingSet`・`ProtectSystem=strict`・`ProtectHome`・`RestrictSUIDSGID`）の下で動くこと**（Decision 0083 の承認の条件 (1)）。`newuidmap` は Setuid なので動かないことがある。そのときは Subuid の Mapping を使わない形（`/etc/subuid` に `paw-agent-cli` の行を置かず、`storage.conf` の `ignore_chown_errors = "true"`）で、硬さはそのまま保つ。
- Unit の `Delegate=yes` の下で、Podman の `--memory`・`--cpus`・`--pids-limit` が効くこと（cgroup v2 の Controller の委任）。
- CLI の Unit が止められなかった（`systemctl stop` が 3 回とも失敗した）ときは、Runtime は Session の Directory を消さずに残す（Unit が Mount しているため）。Log の `A CLI session could not be stopped` を見たら、Unit を止めてから `/run/paw-cli/<uuid>` を消す。
- `paw-egress` が動いていて、Container の中から `127.0.0.1:3128` 経由で `api.anthropic.com` に届き、他の Host は拒否されること（Proxy の Log）。
- `claude` の `system/init` に PAW の Tool と `EndConversation` だけが出ること（違えば Runtime が止める）。
- Provider の規約の確認（Human）。
