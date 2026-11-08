# CLI の Session の Container の配備（Claude Code・Codex の Subscription）

更新日: 2026-11-04

[Decision 0083](../../../../docs/decisions/0083-agent-runtime-adapters.md) の 6 と [Decision 0087](../../../../docs/decisions/0087-cli-secret-store-and-egress.md) の 3・4（どちらも Approved）。
Backend の側は `paw_backend/agents/cli_session.py` の `SystemdLauncher`、`claude_cli.py` の `ClaudeSessionAdapter` / `ClaudeCliRuntime`、`codex_cli.py` の `SystemdCodexLauncher` / `CodexSessionAdapter` / `CodexCliRuntime`（[Decision 0089](../../../../docs/decisions/0089-codex-cli-runtime-route.md)）、`bridge.py` の `McpBridge`。
CLI の段は `/etc/paw/runtime.toml` の Ladder が `claude` / `codex` を名指したときだけ組み立てます（Decision 0099 の「PR の分け方」6。手順は `docs/DEPLOYMENT_UPDATE.md` の「The CLI runtimes」）。この Directory の File は、有効にする前に Human が配備して確かめるためのものです。Codex は確かめた版（`VERIFIED_VERSIONS`。今は `0.156.1`）以外では何も送りません。

## 流れ

```text
Backend（User paw）
  ├─ /run/paw-cli/<uuid>/（0750、Group paw-cli-bridge: /run/paw-cli の setgid で継ぐ）
  │    bridge.sock（MCP の Bridge）・bridge.token・mcp.json・settings.json・config/
  │    launch.json（CLI の引数と許可した環境変数）・credential.env（CLAUDE_CODE_OAUTH_TOKEN の 1 行、0640）・prompt・stream.sock
  └─ systemctl start --no-block paw-cli-session@<uuid>.service（polkit がこの名前の start / stop だけを許す）
       ├─ Requires= / After= paw-egress-session@<uuid>.socket（/run/paw-egress/session/<uuid>.sock。Session の Unit と一緒に止まる: ../egress/README.md）
       └─ paw_cli_session_run.py <uuid>（User paw-agent-cli、硬くした Unit）
            └─ podman --events-backend none run --rm --log-driver none（Issue #324）
                 --cgroups=split --network none --read-only --cap-drop all --userns keep-id
                 --sysctl net.ipv4.ping_group_range=<gid> <gid> --group-add keep-groups（Issue #322）
                 -v /run/paw-cli/<uuid>（同じ Path）-v /run/paw-egress/session/<uuid>.sock:/run/paw-egress.sock --env-file credential.env
                 <Image@sha256:...> /opt/paw/start-claude <引数>   stdin = prompt、stdout = stream.sock
                   ├─ paw_egress.py forward（127.0.0.1:3128 → 自分の Session の Egress の Socket）
                   └─ claude -p ...（MCP の Server = paw_mcp_relay.py → bridge.sock）
```

Codex の Session（`launch.json` の `cli` が `codex`）は `codex app-server`（stdio の JSON-RPC の対話）なので、Program は `stream.sock` を Container の stdin と stdout の両方にする。Image は `codex_image`（`Containerfile.codex`、入口は `/opt/paw/start-codex`）。`credential.env` と `prompt` は書かない（Codex の Login は `CODEX_HOME` の `auth.json`。`credential.env` があれば拒否する）。版の確認（`codex --version`）と `app-server` は同じ Session の Unit を順に起動する。

Codex の `CODEX_HOME`（Issue #239、Decision 0083 の 6、[Decision 0095](../../../../docs/decisions/0095-codex-auth-file-owner-and-mode.md)（Approved））: `launch.json` の `CODEX_HOME` は Session の Directory の `config` でなければならない（違えば拒否）。Program はそこに Container の tmpfs（`--mount type=tmpfs,destination=<dir>/config,tmpfs-mode=0700,chown=true,notmpcopyup`。所有者は Container の User = `paw-agent-cli`。`notmpcopyup` は下の Session の `config` の File を tmpfs に写さない: 写すと Podman が `paw` の File を chown しようとし、Subuid の Mapping なし（Issue #322）では crun が `chown ... Invalid argument`（exit 126）で止まる。Issue #339）を重ね、その中へ Session の `config.toml`（`:ro`）と `auth.json`（`:rw`）だけを Bind Mount する（Backend が書いたものだけ。普通の File でなければ拒否）。`auth.json` は Backend が作る 0660・所有者 `paw`・Group `paw-cli-bridge` の File で、Codex は Refresh した Login をこの File の Truncate と書き直しで保存し、Backend は Session の終わりに読み戻して Secret Store に書き戻す（Compare-and-swap）。Codex がその他に `CODEX_HOME` へ書く物（Log・状態）は Container の tmpfs に残り、Container と一緒に消える。Codex の Session は Connection ごとに同時に 1 つ（Backend の DB の Lease。`../../README.md` の「Codex の ChatGPT の Login」）。

Container の出力は Backend だけのもの（Issue #324）: Program は `podman run --log-driver none` で Container の stdout / stderr をどこにも記録させない（Podman の既定の Log Driver は journald で、conmon が CLI の Stream（`stream-json` の本文: 応答・Tool の呼び出しと結果・Session の Id・利用量）を 1 行ずつ journal に書いていた。識別子は `paw-cli-<uuid>`）。`podman run` は Attach したままなので、stdout は今までどおり `stream.sock` に届き、stderr は Program が読む（Podman 自身の失敗の時だけ、その末尾を書く）。`podman --events-backend none` で Podman の Event（`create`・`start`・`attach` など）も記録しない（journald の Event の 1 件ごとに、その Entry の `_CMDLINE` に `podman run` の Command Line 全部、つまり CLI の引数（`--system-prompt` の本文・Session の Path）が残っていた）。

Egress（[Decision 0091](../../../../docs/decisions/0091-egress-per-session-socket.md)）: Container に Mount するのは自分の Session の Socket（`<egress_session_dir>/<uuid>.sock`、既定 `/run/paw-egress/session`）だけで、Proxy はどの Socket で受けたかで Session を知る（Header は使わない。`PAW_SESSION_ID` も渡さない）。その Socket がなければ Program は起動を拒む（共有の Socket に戻らない）。設定に古い `egress_socket` が残っていても拒む。Backend は Session を起動するとき 1 行の Log（`CLI session started: session=<uuid> task=<id> node=<key> connection=<id> cli=<claude|codex>`）を書き、Egress の Log の `session=` を Task・Node・Connection に結び付ける。

`credential.env` は 0640・所有者 Backend・Group `paw-cli-bridge`（Decision 0088 で承認。Decision 0087 の「0400・所有者 `paw-agent-cli`」の代わり。Backend は別の Account に File を渡せないため）。

止めるのは `systemctl stop`（Unit の `TimeoutStopSec=5s`: SIGTERM → 5 秒 → SIGKILL、`ExecStopPost` で Container を消す）。

Memory 不足（[Decision 0092](../../../../docs/decisions/0092-runtime-oom-kill-detection.md) の 4、Issue #251）: Unit は `OOMPolicy=stop`・`MemoryAccounting=yes` で、Program は `podman run --cgroups=split` で Container の cgroup を Unit の cgroup の下に置く。Container の中の OOM Kill で Unit 全体が止まり、`Result=oom-kill` で終わる。`systemctl stop` でその `Result` は消えうるので、Backend（`_UnitProcess.stop()`）は止める**前に** `ActiveState`・`Result`・`ExecMainStatus` を読んで覚える（CLI が自分で終わった後なら、Unit が `active` から離れるのを最大 10 秒待つ）。`submit` の前に OOM で終わった Session は `AgentOutOfMemory`。Session の Directory は終わりに Backend が消す（Token の File も一緒に消える）。

## Codex の版を確かめる（[Decision 0101](../../../../docs/decisions/0101-codex-mcp-resource-tools-exception.md) の 4）

`smoke_codex_recorder.py` は、本物の `CodexCliRuntime` → `CodexSessionAdapter`（`codex app-server`）→ Bridge → Relay を 1 つの Reviewer の Node で動かし、Session の `config.toml`（Backend の `session_config`）に `127.0.0.1` の記録用の Model Provider を 1 つ足す。`auth.json` は Dummy で、**OpenAI には何も送らない**。本物の Login（`~/.codex/auth.json` など）は使わない。`paw` ではなく開発する User で、確かめる版の `codex` を Scratch に置いて動かす（User の `codex` を上書きしない）:

```sh
python3 -I deploy/cli-session/smoke_codex_recorder.py --codex <codex> --out <result.json> --sessions <短い Directory>
```

すべての Request の `tools` が `codex_app_server.tool_check` を通り（PAW の Tool と、MCP の Resource の 3 つの Tool だけ。MCP の Server は `paw` だけ）、`submit` の `summary` が Node の結果になれば Exit 0（`passed: true`）。結果の JSON（Request の Tool・Model・入力の Item の種類だけ。Prompt の文は入れない）を `/data/results/` に残し、その版を `VERIFIED_VERSIONS` に入れる PR に Path を書く。Feature の一覧の照合と Fake のやり直しも同じ PR で行う（0101 の 4）。

`--probe` を付けると、同じ記録用の Provider で Codex の Connection の Health の Probe（`CodexProbeAdapter`、[Decision 0100](../../../../docs/decisions/0100-cli-connection-health-probe.md) の 10。`config.toml` は MCP の Server のない `probe_config`）を 1 回走らせる。記録用の Provider は `PAW_PROBE_OK` と答え、Request がちょうど 1 つで、その `tools` が空（Model に Tool を 1 つも見せない）で、Probe の判定が `connected` なら Exit 0。Codex の Probe を本番で使う前に、同じ版の `codex` で 1 回走らせ、結果の JSON を同じく残す。

## 配備（root）

1. Account と Group: `useradd --system --home-dir /var/lib/paw-agent-cli --create-home --shell /usr/sbin/nologin paw-agent-cli`、`groupadd --system paw-cli-bridge`（`paw-egress` の README と同じ Group）、`usermod -aG paw-cli-bridge paw-agent-cli`。Backend の User（`paw`）は Group に入れない（Directory の所有者なので要らない）。Unit は `paw-agent-cli` の主 Group を変えない（`Group=` を書かず、`SupplementaryGroups=paw-cli-bridge`。Issue #322）: Subuid の Mapping なしでは Container の gid 0 が Process の主 Group に写り、Podman の Storage は主 Group `paw-agent-cli` のものなので、主 Group が違うと Podman は Layer の ID-mapped copy を作ろうとして `chown ... invalid argument`（exit 125）で止まる。Session の Directory（setgid、Group `paw-cli-bridge`）は補助 Group で読み書きし、`podman run --group-add keep-groups`（crun が必要）で Container の中にも残す。Rootless の Podman の `XDG_RUNTIME_DIR` は Unit の `RuntimeDirectory`（`/run/paw-cli-session/<uuid>`）にする（`ProtectHome=yes` が `/run/user` を隠すため。Linger は要らない）。
2. `/run/paw-cli`: `tmpfiles.d` で作る（`/etc/tmpfiles.d/paw-cli.conf` に `d /run/paw-cli 2750 paw paw-cli-bridge -` を書き、`systemd-tmpfiles --create /etc/tmpfiles.d/paw-cli.conf`）。`paw-backend.service` の `RuntimeDirectory=` は使わない（Group が Unit の `Group=`（`paw`）になり、`paw-cli-bridge` にできない。Unit の停止で消えもする）。
3. `install -m 0755 paw_cli_session_run.py /usr/local/lib/paw/`、`paw-cli-session@.service` を `/etc/systemd/system/`、`50-paw-cli-session.rules` を `/etc/polkit-1/rules.d/`、`cli-session.example.toml` を `/etc/paw/cli-session.toml`（Image の Digest を入れる）。Egress の Session ごとの Unit（`paw-egress-session@.socket` など）は [`../egress/README.md`](../egress/README.md) の配備で先に置く（`paw-cli-session@.service` が `Requires=` で引き込む。無いと Session の Unit は起動しない）。共有の `paw-egress.socket` を配備した Host は、同じ README の移行の手順を先に行う。
4. Image: `Containerfile` の Base の Digest と Claude Code の版を固定して `paw-agent-cli` で Build し、Digest を `cli-session.toml` に入れる。Codex は `Containerfile.codex`（Codex の版は Backend の `VERIFIED_VERSIONS` にあるもの）を同じく Build して `codex_image` に入れる。

## 確かめること（有効にする前）

- **Rootless の Podman が、この硬くした Unit（`NoNewPrivileges`・空の `CapabilityBoundingSet`・`ProtectSystem=strict`・`ProtectHome`・`RestrictSUIDSGID`）の下で動くこと**（Decision 0083 の承認の条件 (1)）。`newuidmap` は Setuid なので動かないことがある。そのときは Subuid の Mapping を使わない形（`/etc/subuid` に `paw-agent-cli` の行を置かず、`storage.conf` の `ignore_chown_errors = "true"`）で、硬さはそのまま保つ。
  - Issue #322 の組み合わせ（主 Group `paw-agent-cli` + `SupplementaryGroups=paw-cli-bridge`、`--userns keep-id` + `--sysctl net.ipv4.ping_group_range=<gid> <gid>`（keep-id では gid 0 が写らないので Podman の既定の `0 0` は書けない）+ `--group-add keep-groups`）が、本物の Session の Directory（setgid の 2750、File は 0640・Group `paw-cli-bridge`）を Container の中から読めること。下の「Unit の Group と Podman を確かめる」。
  - Podman 自身の失敗（exit 125、126 / 127）は Unit の journal に出る: `paw-cli-session: podman failed (exit <code>)` と、Podman の stderr の最後の 20 行（2000 文字まで。Credential・Bridge の Token・Codex の Login の文字列と Token の形は `[redacted]`）。それ以外の終わりは CLI のもので、`paw-cli-session: the CLI ended (exit <code>)` の 1 行だけ（CLI の stderr は Task の中身を含みうるので書かない）。`journalctl -u 'paw-cli-session@*' -n 50`。
- **CLI の出力が journal に残らないこと**（Issue #324）。下の「Container の出力を journal に残さない」。
- Unit の `Delegate=yes` の下で、Podman の `--memory`・`--cpus`・`--pids-limit` が効くこと（cgroup v2 の Controller の委任）。
- **`--cgroups=split` で Container の cgroup が Unit の cgroup の下にあること**（`podman inspect --format '{{.State.CgroupPath}}' paw-cli-<uuid>` が `/system.slice/system-paw\x2dcli\x2dsession.slice/paw-cli-session@<uuid>.service/` の下）。`--memory` を小さくした Session で Memory を食わせ、Unit が `systemctl show -p Result` で `oom-kill` になり、Node が `AgentOutOfMemory` になること（Decision 0092 の 7）。`split` が使えなければ、Decision 0092 の 4 の他の案 (a)（Wrapper が `OOMKilled` を読む）に新しい Decision で替える。
- CLI の Unit が止められなかった（`systemctl stop` が 3 回とも失敗した）ときは、Runtime は Session の Directory を消さずに残す（Unit が Mount しているため）。Log の `A CLI session could not be stopped` を見たら、Unit を止める。
  - **Codex の Session（Issue #280、[Decision 0096](../../../../docs/decisions/0096-codex-login-collect-after-unit-stop.md)）では `/run/paw-cli/<uuid>` を手で消さない**: Login（`config/auth.json`）がまだ回収されていない。Operator がするのは `systemctl stop paw-cli-session@<uuid>.service`（root）だけ。Backend（枠を保つ Process か、どの Backend の Process でも 60 秒ごとに回る回収の Loop）が、Directory に Fence を置き（`launch.json` を消し、Directory と `config` を 0550）、Unit が `inactive` / `failed`・Job なし・cgroup（`/sys/fs/cgroup/system.slice/system-paw\x2dcli\x2dsession.slice/paw-cli-session@<uuid>.service`）が空になったのを確かめてから、`auth.json` を読み戻して書き戻し（変わっていれば次の版。Compare-and-swap）、Directory を空の Tombstone（0550、`/run` なので次の Boot で消える）にして枠を返す。それまで Codex の Connection は `connection_busy`（System Health の `connections` が `WARNING` の `unit_running:codex`、Log に UUID と止める Command）。
  - 同じ Session の Egress の Unit（`paw-egress-session@<uuid>.socket` / `.service`）が残っていたら（Log の ERROR と System Health の `egress_unit_left`）、Backend からは止められないので、root で `systemctl stop paw-egress-session@<uuid>.socket paw-egress-session@<uuid>.service` する。
  - 回収で Login が読み戻せなかった（Host の再起動で `/run` が消えた・手で消された・JSON でない）ときは、Connection に印が付き System Health が `ERROR`（`collect_failed:codex`）。Health Check（Owner の「再接続」）が `connected` を返すか、Admin が Credential を置き換えると消える。`expired` なら Device Code の Login で入れ直す。
- Container の中から `127.0.0.1:3128` 経由で `api.anthropic.com` に届き、他の Host は拒否されること（Proxy の Log）。Egress の Session ごとの Socket と Proxy の確認（polkit の追加なしで Socket が作られる・Session の終わりで止まり File が消える・Container から見える Socket が自分の 1 つだけ・Log の `session=` と集計の 1 行）は [`../egress/README.md`](../egress/README.md) の「確かめること」。
- `claude` の `system/init` に PAW の Tool と `EndConversation` だけが出ること（違えば Runtime が止める）。
- **Codex の `CODEX_HOME`**（Issue #239）: この硬くした Unit の下の Rootless の Podman が `--mount type=tmpfs,...,tmpfs-mode=0700,chown=true,notmpcopyup` と、その tmpfs の中への File の Bind Mount（`config.toml:ro`・`auth.json:rw`）を受け付けること。Container の中で `stat -c '%a %U' "$CODEX_HOME"` が `700 paw-agent-cli`（の Container の中の名前）、`codex` が `CODEX_HOME` に Log などを書けること、`auth.json` を書けること（Host 側の File の持ち主・Mode が `paw paw-cli-bridge 0660` のまま変わらないこと）。Session の後の `/run/paw-cli/<uuid>` が Backend によって消えること。
- **Codex の Login の書き戻し**（Smoke。Human の規約の確認の後）: Session の間に Codex が Login を Refresh したとき、Backend の Log に `Codex refreshed its login; written back` が出て、Secret Store の版が 1 つ上がり、次の Session が新しい版で動くこと。2 つ目の Session が同じ Connection で 60 秒待ってから `connection_busy` になること。
- Provider の規約の確認（Human）。

## Unit の Group と Podman を確かめる（Issue #322。root、Human）

Repository の `paw-cli-session@.service` か `paw_cli_session_run.py` が変わった Release の後は、配備の 3 を入れ直す（Release は `/etc` と `/usr/local/lib/paw` を変えない）:

```sh
sudo install -o root -g root -m 0755 apps/backend/deploy/cli-session/paw_cli_session_run.py /usr/local/lib/paw/
sudo install -o root -g root -m 0644 apps/backend/deploy/cli-session/paw-cli-session@.service /etc/systemd/system/
sudo systemctl daemon-reload
systemctl cat paw-cli-session@00000000-0000-0000-0000-000000000000.service | grep -E '^(User|Group|SupplementaryGroups)='
```

最後の行は `User=paw-agent-cli` と `SupplementaryGroups=paw-cli-bridge` だけ（`Group=` がない）。走っている Session の Unit はそのまま（次の Session から新しい Unit になる）。

Claude に何も送らずに（`mkdir` は `chmod` しない: `paw` は Group の外なので `chmod` で setgid が外れる。Issue #319）、Unit と同じ硬さと同じ `podman run` の Option で、本物の Session の Directory と同じ形の File を Container から読む（`<image>` は `cli-session.toml` の `image`）:

```sh
sudo -u paw sh -c 'umask 027; mkdir /run/paw-cli/check-322 && echo readable > /run/paw-cli/check-322/probe'
sudo stat -c '%a %U %G' /run/paw-cli/check-322 /run/paw-cli/check-322/probe   # 2750 paw paw-cli-bridge / 640 paw paw-cli-bridge
gid=$(id -g paw-agent-cli)
sudo systemd-run --wait --pipe --collect \
  -p User=paw-agent-cli -p SupplementaryGroups=paw-cli-bridge \
  -p Environment=HOME=/var/lib/paw-agent-cli \
  -p RuntimeDirectory=paw-cli-check-322 -p RuntimeDirectoryMode=0700 \
  -p Environment=XDG_RUNTIME_DIR=/run/paw-cli-check-322 \
  -p Delegate=yes -p NoNewPrivileges=yes -p ProtectSystem=strict -p ProtectHome=yes \
  -p PrivateTmp=yes -p CapabilityBoundingSet= -p RestrictSUIDSGID=yes \
  -p 'ReadWritePaths=/var/lib/paw-agent-cli /run/paw-cli' -p UMask=0077 \
  /usr/bin/podman run --rm --cgroups=split --init --network none --read-only \
    --cap-drop all --security-opt no-new-privileges --userns keep-id \
    --sysctl "net.ipv4.ping_group_range=$gid $gid" --group-add keep-groups \
    --volume /run/paw-cli/check-322:/run/paw-cli/check-322:rw \
    '<image>' /bin/sh -c 'id; cat /run/paw-cli/check-322/probe'
sudo rm -r /run/paw-cli/check-322
```

`uid=<paw-agent-cli> gid=<paw-agent-cli>` の行と `readable` が出て Exit 0 なら良い（補助 Group は Container の中では名前のない gid に見える）。その後、Connection の Health Check（`docs/DEPLOYMENT_UPDATE.md` の「The CLI runtimes」の 4）を 1 回行い、失敗したら `journalctl -u 'paw-cli-session@*' -n 50` で Podman の stderr を見る。

## Container の出力を journal に残さない（Issue #324。root、Human）

`paw_cli_session_run.py` を配備の 3 で入れ直す（上の「Unit の Group と Podman を確かめる」の最初の `install`。Unit の File は変わらないが、入れ直しても良い）。走っている Session はそのまま（次の Session から新しい Program になる）。

入れ直した後、Connection の Health Check（`docs/DEPLOYMENT_UPDATE.md` の「The CLI runtimes」の 4）を 1 回行い、その Session の分が journal に Unit の行（Podman の失敗・`the CLI ended` の 1 行・systemd の Start / Stop）しかないことを確かめる:

```sh
since=$(date '+%Y-%m-%d %H:%M:%S')   # Health Check の直前に
# ……Health Check……
sudo journalctl --since "$since" --no-pager -o verbose _UID=$(id -u paw-agent-cli) | grep -cE '"type":|session_id|--system-prompt'   # 0
sudo journalctl --since "$since" --no-pager -u 'paw-cli-session@*' -o short-iso   # systemd と paw-cli-session: の行だけ
```

### 既に残った分

journald は Entry を 1 件ずつ消せない。Issue #324 の前の Program が動いた Host（本番は 2026-11-04 22:15 の Health Check から）では、CLI の Stream の行（識別子 `paw-cli-<uuid>`）と Podman の Event（`SYSLOG_IDENTIFIER=podman`、`_CMDLINE` に CLI の引数）が、`paw-agent-cli`（System の Account なので System の journal）の Entry として残っている。量を確かめる:

```sh
sudo journalctl --no-pager -o cat _UID=$(id -u paw-agent-cli) | wc -l
sudo journalctl --no-pager -o short-iso _UID=$(id -u paw-agent-cli) | head -n 3   # 最初の時刻
ls /var/log/journal 2>/dev/null   # 無ければ journal は /run（Volatile）で、Host の再起動で消える
```

消すには、その時刻より前の journal を File ごと消すしかない（**同じ時刻までの Host の他の Log もすべて消える**。どこまで消すかは Human が決める）:

```sh
sudo journalctl --rotate                # 今の File を閉じて Archive にする
sudo journalctl --vacuum-time=<期間>     # 最後の Entry がこれより古い Archive を消す（例: 1s で今までの全部）
sudo journalctl --no-pager -o cat _UID=$(id -u paw-agent-cli) | wc -l   # 0（または Program を入れ直した後の Unit の行だけ）
```

journal の写しが他にもあれば同じく扱う: rsyslog などの転送先（`sudo grep -l 'paw-cli-' /var/log/syslog* /var/log/messages* 2>/dev/null`）、journal を含む Backup、`systemd-journal-upload` などの転送。
