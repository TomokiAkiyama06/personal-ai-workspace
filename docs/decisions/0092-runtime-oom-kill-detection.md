# Runtime の OOM の検出の残り: Model の Server の Unit の `oom-kill` と、CLI・Tool の Sandbox の Container の cgroup の `oom_kill` をどこで・どう読むか

- Status: Proposed
- Date: 2026-10-16
- Scope: Issue [#251](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/251)（[#208](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/208) の後続、[#183](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/183) の残り）。`apps/backend/paw_backend/compute/`（`control.DeploymentCommands`・`ComputeScheduler`・`runtimes.HybridRuntime`）、`health/sources.ComputeSource`、`agents/cli_session.py`（`SystemdLauncher`・`_UnitProcess`）・`agents/claude_cli.py`・`agents/codex_cli.py`、`sandbox/wire.py`・`sandbox/tools.py`、`apps/backend/deploy/systemd/paw-llm-main.service`、`deploy/cli-session/`（`paw-cli-session@.service`・`paw_cli_session_run.py`）、`deploy/ssh-git-wrapper/paw_git_wrapper.py`（`SandboxCall`・`run_sandbox_exec`）
- Supersedes: なし。[Decision 0083](0083-agent-runtime-adapters.md)（Approved）の 4 の表の `AgentOutOfMemory` の行のうち、「Model の Server の Unit が OOM Killer で終わった（`systemctl show` の `Result=oom-kill`。Scheduler / System Health が持つ状態から読む）」と「CLI や Tool の Process が Container の cgroup の `memory.events` の `oom_kill` の増加とともに終わった」を **どう実装するか** を決める（行の意味は変えない）。[Decision 0071](0071-agent-oom-and-escalation-records.md)（Approved）の 1〜4（Runtime が見分けて `AgentOutOfMemory` で報告する、Orchestrator は推測しない、`agent_incidents` に数える、`task_queue` の閾値）はそのまま使う

## 背景

Decision 0071 は「OOM を見分けるのは Runtime（Adapter）」とし、Decision 0083 の 4 は見分ける 3 つの場合を決めた。#183 の確認（2026-10-16）で、次の 2 つが実装されていないことが分かった（Issue #251）。

1. **Model の Server の Unit が OOM Killer に殺されたとき。** `compute/`・`health/` に systemd の Unit の結果を読むものがない。今は `agents/chat.py` の HTTP Client が接続の切断（`httpx.ConnectError` / `HTTPError`）を `Failure.SERVER_UNAVAILABLE` にし、`ConnectionError` として記録されるので OOM に数えられない。`paw-llm-main.service` は `MemoryMax=32G`（Decision 0072）なので、Host の RAM の上限での OOM Kill は実際に起こりうる（2026-09-30 の JIT の Build の事故）。
2. **CLI・Tool の Sandbox の Container が cgroup の `oom_kill` で終わったとき。** `paw_cli_session_run.py` は `podman run --rm` の終了 Code だけを返し、`paw_git_wrapper.py` の `exec` は `paw-sandbox-status <nonce> exit=<code> truncated=<0|1>` だけを返す。Memory の合図を Backend に返す口がない。

**次のことは要件も既存の Decision も決めていない。** Unit の結果を誰が・いつ・どの権限で読むか、どの Unit を見るか。Container の OOM を Wrapper がどう知り、どの経路で Backend に返すか。Tool の Sandbox の OOM で Node をどう終えるか。
[AGENTS.md](../../AGENTS.md) の「仕様変更」に従い、下の推奨で提案し、承認を求める。

## 確かめた事実（2026-10-16、公式の Document と Source）

推奨はこれらに依る。実装の PR の Smoke で実機（Ubuntu の systemd 259・Podman 5.7.0）でも確かめる（7）。

- **systemd の `Result=oom-kill`**（`systemd.service(5)` の `OOMPolicy=`、`systemd.exec(5)` の `$SERVICE_RESULT` の表）: `OOMPolicy=` は `continue` / `stop` / `kill`。`stop` と `kill` では「Service は最後に `oom-kill` の失敗の状態になる」。`continue` では Log に残るだけで Unit は動き続ける。**既定は `DefaultOOMPolicy=`（system.conf の既定は `stop`）だが、`Delegate=` が有効な Unit では `continue`**。`oom-kill` は「Service の Process が OOM Killer に終わらされた」。`systemd-oomd` が殺したときも同じ設定が効く。
- **systemd が OOM を知る仕組み**（systemd の `src/core/cgroup.c` の `unit_check_oom`）: Unit の cgroup の `memory.events` の `oom_kill` の増加を inotify で見る。**Memory の Accounting が有効な Unit だけ**（`MemoryAccounting=`。`MemoryMax=` があれば有効）。Kernel の文書（`cgroup-v2`）のとおり `memory.events` の値は **子孫の cgroup を含む**（`memory_localevents` で Mount していないとき）ので、Unit の cgroup の下に作られた Container の cgroup の OOM Kill も Unit の `oom_kill` に入る。
- **失敗の状態はいつまで残るか**（`systemctl(1)` の `reset-failed`）: 失敗した Unit の終了の Code と状態は「Service が止められる・再び起動される・`reset-failed` されるまで」残る。つまり **Scheduler が Unit を再び `start` すると `Result` は消える**。Unit の cgroup は Unit が止まると消えるので、終わった後に `memory.events` を読むことはできない。
- **読む権限**: systemd の D-Bus の Policy（`org.freedesktop.systemd1.conf`）は、誰にでも `org.freedesktop.DBus.Properties` の `Get` / `GetAll` を許す。`systemctl show -p Result -p ActiveState -p InactiveEnterTimestampMonotonic <unit>` は Backend の User のまま読める（polkit も sudo も要らない）。D-Bus の Property は `Result`（文字列）、`InactiveEnterTimestampMonotonic`（CLOCK_MONOTONIC の µs）、`InvocationID` など（`org.freedesktop.systemd1(5)`）。`InvocationID` は「Unit の 1 回の実行ごとの 128 bit の ID で、Unit が inactive から activating / active に変わるたびに新しくなる」（`systemd.exec(5)` の `$INVOCATION_ID`）ので、`deactivating` の間も変わらない。`InactiveEnterTimestampMonotonic` は `inactive` / `failed` に入ったときにしか更新されない。
- **Podman の `State.OOMKilled`**（`podman-inspect(1)`、Podman 5.7.0 の `libpod/define/container_inspect.go`・`container_internal.go`、conmon の `src/cgroup.c`）: cgroup v2 では conmon が Container の cgroup の `memory.events` を見て、`oom_kill` **または `oom`**（上限に達して確保が失敗しかけた回数）が増えたら `oom` の File を作り、Podman は **Container が終わったとき**にその File の有無で `OOMKilled` を立てる。つまり (a) Container の中の PID 1 以外の Process が殺されただけでも立つ、(b) Kill のない `oom` でも立ちうる、(c) Container が動いている間は更新されない、(d) `--rm` だと終了と同時に消えて読めない。
- **Podman の `State.CgroupPath`** は Inspect の出力にある（`container_inspect.go`）。`--cgroups=split` は「呼び出した Process の cgroup を conmon 用と Container 用の 2 つの子に分ける」（`podman-run(1)` の `--cgroups`）。cgroup v2 の `--cgroupns` の既定は `private`。

## 提案

### 1. Model の Server の Unit: 状態は Compute Scheduler が持つ

- **見る Unit**: Compute Scheduler の Deployment のうち、Runtime が systemd の Unit のもの（今は `paw-llm-main.service`。Memory Worker・Embedding も Unit にしたら同じ）。`DeploymentCommands` に `unit`（Unit の名前、`[A-Za-z0-9@._-]+\.service`、任意）を足す。任意の argv（`pids` のような）にはしない（読む Command を Backend が固定でき、出力の形を 1 つに決められる）。`unit` がない Deployment はこの検出をしない（今までどおり `ConnectionError`）。
- **読み方**: `/usr/bin/systemctl show -p Result -p ActiveState -p InvocationID -- <unit>` を Shell なし・Timeout つき（既存の `CommandRunner`）で走らせ、`Key=Value` の 3 行だけを厳密に読む。出力は Log にも Error にも入れない（`probe.py` と同じ規則）。D-Bus の Library は足さない（新しい依存になり、`systemctl` は既に CLI の Launcher と `CommandModelControl` が使っている）。
- **いつ読むか（Polling ではなく「終わったと分かったとき」）**: `Result` は Unit が終わるまで変わらないので、動いている Unit を定期的に読む必要はない。次の 3 つのときだけ読む。
  1. Scheduler の 5 秒ごとの Probe（`_sample` → `_inspect`）で、GPU に置いているはずの Deployment の Process が **空になった**とき（今の `_reconcile` の前。Lease があっても読む）。
  2. Scheduler が Unit を `start`（`place`）する **前**（再起動で `Result` が消えるので、その前に一度だけ読んで残す）。
  3. 3 の `HybridRuntime` が問い合わせたとき（その場で読む）。
- **残すもの**（Memory の中だけ）: 1 つの「終わり」は **`InvocationID`** で見分ける（`deactivating` の間も変わらず、次の起動で必ず変わる。時刻で見分けると、`deactivating` の間に読むたびに別の終わりに見えたり、前の終わりの時刻と重なったりする。Codex review #253）。終わりを初めて見たときに、その時の monotonic の時刻を「見た時刻」として 1 回だけ付け、後で読み直しても変えない。Deployment ごとに 2 つを別に持つ。(a) 最後に見た終わり 1 つ: `InvocationID`・`result`（`oom-kill` かそれ以外か）・見た時刻。3 の問い合わせに使う。(b) `oom-kill` の終わりの一覧（`InvocationID` と見た時刻、直近 1 時間の分だけ、上限 64 件。古いものから捨てる。同じ `InvocationID` は 1 回だけ）。後の普通の終わり（Unload・正常な停止）で (a) が上書きされても (b) は残るので、2 の 1 時間の窓と回数が早く消えず、1 つの終わりを 2 回数えない（Codex review #253）。DB には書かない（Process の再起動で消えてよい。数えるのは 3 の Agent の失敗と 2 の Health で足りる）。
- **Unit の設定**: `paw-llm-main.service` に `OOMPolicy=stop` を明示する（既定の `DefaultOOMPolicy=` を誰かが変えても `oom-kill` で終わるように。Kernel が子の Process だけを殺したときも Unit 全体が止まり、半分壊れた Server が残らない）。`MemoryMax=` があるので Accounting は有効。
- 他の案: (a) 動いている Unit を定期的に読む（`Result` は終わるまで変わらないので無駄。5 秒の Probe で Process が消えたのは分かる）。(b) Unit の `OnFailure=` で別の Unit を起こして Backend に知らせる（Backend への入口が増え、Root の設定が要る）。(c) `journalctl` の `SD_MESSAGE_UNIT_OUT_OF_MEMORY` を読む（Backend の User に Journal を読む Group が要り、文の形に依存する）。(d) Unit の cgroup の `memory.events` を読む（Unit が止まると cgroup ごと消えるので、終わった後には読めない）。

### 2. System Health: `compute` の Component に Model の Server の OOM を出す

- Decision 0071 の 1 のとおり、Model の Load / Unload の失敗は `compute` の Component で扱う。1 の状態から、**直近 1 時間に `oom-kill` で終わった Deployment があれば `compute` を `WARNING`**、理由の Code は `model_server_oom_killed`、数値は `model_server_oom_kills_last_hour`（1 の (b) の、Deployment ごとの `oom-kill` の時刻の一覧から数える）。Agent が動いていないとき（Memory Worker だけが使っていたときなど）も見えるようにするため。
- `agent_incidents`（Decision 0071 の 3）には書かない（あれは Agent の試行の数。Agent の失敗は 3 で別に数える）。
- 他の案: `ERROR` にする（1 回の OOM Kill は Scheduler が次の Load で回復させるので、Decision 0071 と同じく「連発」でなければ `WARNING`）。`health_events` だけに残す（Severity の変化の記録で、件数ではない）。

### 3. Agent の失敗の分類: `HybridRuntime` が `ConnectionError` を `AgentOutOfMemory` に直す

- Local の Model を使う Node は `HybridRuntime` が Scheduler の Lease を取って走らせる（`compute/runtimes.py`）。Local の Runtime が `ConnectionError`（`SERVER_UNAVAILABLE`）で失敗したとき、`HybridRuntime` は Scheduler に「この Deployment の Unit が、この Lease を取った後に `oom-kill` で終わったか」を問い合わせる（`await scheduler.model_server_end(deployment, since=<Lease の開始の monotonic>)`。終わりの「見た時刻」が `since` より後かで決める。Scheduler は終わった Unit の Deployment に Lease を出さないので、Lease の前の終わりを取り違えない）。
- 問い合わせは、Unit が止まりきるのを待たない: `ActiveState` が `deactivating` / `failed` / `inactive` で `Result=oom-kill` なら「はい」。まだ `active` なら最大 10 秒（0.5 秒ごと）読み直す（Kernel が子を殺してから systemd が `stop` に入るまでの間、接続が先に切れることがあるため）。10 秒で決まらなければ「いいえ」（今までどおり `ConnectionError`）。
- 「はい」なら `NodeOutcome.failed("AgentOutOfMemory", <固定の文>)` にする。`escalate` は付けない（Decision 0083 の 4 の表のとおり）。直すのは Class だけで、文は読まない（Decision 0071 の 1: Orchestrator は推測しない。ここは Runtime の側）。
- 同じ OOM Kill で同時に走っていた Node が複数あれば、それぞれが `AgentOutOfMemory` になり、`agent_incidents` に Node の数だけ入る（それぞれの Node が実際に OOM で失敗したので）。直近 1 時間の 3 件で `task_queue` が `ERROR` になりうる（6 の判断点）。
- 他の案: (a) `LocalAgentRuntime` に Probe を注入する（Lease の開始の時刻と Deployment の名前を持つのは `HybridRuntime` なので、二重に持つことになる）。(b) Orchestrator が `ConnectionError` の後に System Health を見る（Decision 0071 の 1 に反する）。

### 4. CLI の Session の Container: systemd の Unit の `Result` で返す

- **経路**: Backend は今も `_UnitProcess.wait()` で `systemctl show <unit> -p ExecMainStatus` を読んでいる。これを `-p Result -p ExecMainStatus` にし、`Result=oom-kill` なら Session の終わりを OOM とする。新しい返す口（File・終了 Code の予約）は作らない。
- **Unit の設定**（`paw-cli-session@.service`）: `OOMPolicy=stop` と `MemoryAccounting=yes` を明示する（`Delegate=yes` の Unit は既定が `continue` で、そのままでは `oom-kill` にならない）。Container の cgroup が **Unit の cgroup の下** にあることが前提（`memory.events` は子孫を含むので、Container の中の OOM Kill が Unit に見える）。Rootless の Podman はそのままでは User の systemd（`user@.service`）の下に cgroup を作りうるので、`paw_cli_session_run.py` の `podman run` に `--cgroups=split` を付ける（呼び出した Process ＝ Unit の cgroup を分けて、その下に Container を置く）。Smoke で `podman inspect --format '{{.State.CgroupPath}}'` が Unit の cgroup の下であることを確かめる（7）。
- **読む順序**: `Result` は Unit が `stop` されると消えうる（`systemctl(1)`）。今の `claude_cli.py`・`codex_cli.py` は `finally` で `_stop`（`_UnitProcess.stop()`）を呼んでから `wait()` を呼ぶので、`wait()` で読むのでは遅い（Codex review #253）。さらに `paw_cli_session_run.py` は Container が終わると `ExecStart` の Process が返る前に Stream を閉じるので、Adapter が EOF を見た時点では Unit はまだ `active` でありうる。そこで **`_UnitProcess.stop()` 自身が、`systemctl stop` を出す前に** 次をする。(1) Adapter が EOF（CLI が自分で終わった）を見た後の `stop()` なら、Unit が `active` から離れるのを最大 10 秒（0.2 秒ごと。今の `settle_seconds` の読み方と同じ）待つ。Adapter が自分で止める `stop()`（Timeout・取り消し・Bridge の終わりの後で CLI がまだ動いている）では待たない。(2) `systemctl show -p ActiveState -p Result -p ExecMainStatus` を読み、Unit が終わった・終わりかけ（`deactivating` / `failed` / `inactive`）なら `Result` と `ExecMainStatus` を `_UnitProcess` に覚える。(3) それから `systemctl stop` を出す。`wait()` は覚えた値があればそれを返し、なければ今までどおり読む。10 秒たっても `active` なら覚えず、その後の `Result` は OOM として扱わない（Backend が止めたものなので）。`OOMPolicy=stop` の Unit は OOM Kill の後に systemd 自身が止めに入るので、10 秒の中で `deactivating` になる見込み（7 の Smoke で確かめる）。実装の PR で、「OOM で終わった Unit を、EOF の直後（まだ `active`）に Adapter が `stop()` してから `wait()` しても `oom-kill` が返る」ことを Test に固定する。
- **Adapter の分類**: `Result=oom-kill` で、Session が `submit` で結果を出していなければ `Failure.OUT_OF_MEMORY`（`AgentOutOfMemory`）。これは他の失敗（`no_submission`・`INVALID_RESPONSE` など。OOM の結果として起きるもの）より優先する。`submit` の後に起きた OOM は Node の結果を変えない（結果は出ている）。
- 他の案: (a) Wrapper が `--rm` をやめ、終了の後に `podman inspect --format '{{.State.OOMKilled}}'` を読んでから `podman rm` し、予約した終了 Code（例: 3）で返す。CLI 自身の終了 Code と重なり、`OOMKilled` は Kill のない `oom` でも立つ（確かめた事実）ので推奨しない（Smoke で `--cgroups=split` が使えなかったときの代わり）。(b) Session の Directory に結果の File を書く（`paw-agent-cli` は Directory に書けず、Mode の変更が要る。Decision 0088 の前提が変わる）。(c) `sd_notify` の `STATUS=`（`NotifyAccess=` の変更が要り、`StatusText` が終わった後に残るかが文書にない）。

### 5. Tool の Sandbox の Container: Wrapper が `exec` の前後で `memory.events` の `oom_kill` を比べ、状態の行で返す

- Tool の Sandbox は Node の間ずっと動く Container（`sleep infinity`）で、Command は `podman exec` で走る。Command の Process が OOM Kill されても Container は動き続けるので、Podman の `OOMKilled` は使えない（終わるまで更新されない）。
- **読み方**: `run_sandbox_exec` が今も最初に読んでいる `podman container inspect --format '{{.State.Running}}'` を `'{{.State.Running}} {{.State.CgroupPath}}'` にし、Host の側の `/sys/fs/cgroup/<CgroupPath>/memory.events` の `oom_kill` を `exec` の **前と後** に読む（Wrapper は Account の User のまま。cgroup の File は読み取り専用で、Account の Rootless の Container の cgroup はその User のもの）。Path は正規化し、`/sys/fs/cgroup/` の下で `..` を含まないことを確かめる。Container の中から読む（`podman exec … cat`）のは、中の `cat` を Model が置き換えられるので使わない。
- **返し方（Backend が求めたときだけ、新しい形）**: 状態の行に `oom=<0|1|->` を足した形 `paw-sandbox-status <nonce> exit=<code> truncated=<0|1> oom=<0|1|->` は、Backend が `exec` を **新しい Tag `paw-sandbox/v2`**（`exec` の語の並びは `paw-sandbox/v1` と同じ）で送ったときだけ返す。`1` は後の値が前より大きい、`-` は読めなかった（File がない・形が違う）。`paw-sandbox/v1` の `exec` には今までどおり `oom=` のない行を返す。Wrapper は Account ごとに Deploy され、Application とは別に版が進む・戻るので、両方の向きで壊さないため（Codex review #253）: (a) **Application を戻したとき**（新しい Wrapper・古い Backend）: 古い Backend は `v1` を送り、古い形の行を受け取る（今の `wire.py` の `_STATUS` は `fullmatch` で `oom=` のある行を受け付けないので、無条件に足すと全ての `exec` が失敗に見える）。(b) **Wrapper が古いとき**（古い Wrapper・新しい Backend）: Backend は設定 `PAW_SANDBOX_OOM_STATUS`（既定 `false`）が `true` のときだけ `v2` を送る。運用は、全ての Account の Wrapper を入れ替えてから `true` にする（`docs/` の Deploy の手順に書く）。古い Wrapper は `v2` を `bad_protocol` で拒むので、設定を誤っても黙って OOM を見逃すのではなく、Sandbox が使えない失敗として見える。`start` / `stop` は `v1` のまま変えない。`wire.py` は `v2` の行に `oom=` がなければ拒む（形の取り違えを見逃さない）。
- **同じ Sandbox で同時に 2 つの `exec`** があると、片方の OOM をもう片方の Command のせいにしうる。Sandbox は Node ごとで、Tool 呼び出しは Node の中で 1 つずつなので、今は起きない（実装の PR で、同じ Sandbox への同時の `exec` がないことを確かめる）。
- **Runtime の扱い**（Decision 0083 の 4 の表のとおり）: `oom=1` の Tool の結果を受けた Runtime（Local の Tool loop と CLI の MCP の Bridge の両方。どちらも Backend の Toolkit を通る）は、その Node の試行を `AgentOutOfMemory` で終える（`escalate` なし）。`oom=0` / `-` は今までどおり（終了 Code を Model に返す）。
- 他の案: (a) OOM を Model に伝え（Tool の結果に `out_of_memory: true`）、Node は続け、`DagStore.record_incident` で数だけ残す。Model が小さく分けて走り直せるが、Decision 0083 の 4 を一部置き換えることになり（Supersedes が要る）、OOM のたびに Model が同じ重い Command を繰り返すと Sandbox の上限を何度も叩く。6 の判断点に挙げる。(b) Sandbox の Container ごと止める（次の Tool 呼び出しが `missing` になるだけで、原因が残らない）。

### 6. 権限のまとめ

- Backend の User: `systemctl show`（読むだけ。D-Bus の既定の Policy で許される）。新しい polkit の規則・sudo・Group は要らない。
- CLI の Session: 新しい権限なし（Unit の設定と `podman run` の Option だけ）。
- Tool の Sandbox の Wrapper: 自分の Account の Container の cgroup の `memory.events` を読むだけ。

### 7. Smoke（実装の PR で、GPU の Server の上で）

- Model の Server: `MemoryMax=` を小さくした試験用の Unit（`paw-llm-main.service` ではない）で Memory を食う Process を走らせ、`systemctl show` が `Result=oom-kill` になること、Scheduler が Process の消えたのを見て `oom-kill` を残し、`HybridRuntime` が `AgentOutOfMemory` を返すこと（Fake の Model の Server で）。
- CLI の Session: `--memory` の小さい Session の Container で Memory を食わせ、`--cgroups=split` で Container の cgroup が Unit の下にあり、Unit が `Result=oom-kill` になること。`split` が使えなければ 4 の他の案 (a) に替える（その時は新しい Decision）。
- Tool の Sandbox: `exec` で Memory を食う Command を走らせ、状態の行が `oom=1`、普通の Command が `oom=0` になること。

### 8. PR の分け方

1. Scheduler の Unit の終わりの状態（`DeploymentCommands.unit`、`systemctl show` の読み取り、1 の 3 つの時点、`ComputeStatus` の欄）と `compute` の Health の理由（2）、`paw-llm-main.service` の `OOMPolicy=stop`。Fake の Runner で Test。
2. `HybridRuntime` の分類の直し（3）。1 の後。
3. CLI の Session（4）: Unit の設定、`--cgroups=split`、`_UnitProcess.stop()` が止める前に `Result` を覚えること、Adapter の分類と優先順位。CLI の段は組み立てでは無効のまま。
4. Tool の Sandbox（5）: Wrapper の前後の読み取りと `paw-sandbox/v2` の `exec`（`v1` の行は変えない）、`sandbox/wire.py` の `v2` の解析と `PAW_SANDBOX_OOM_STATUS`（既定 `false`）、Deploy の手順、Toolkit と Runtime の分類。
5. 7 の Smoke の報告（`docs/benchmarks/` または Issue のコメント）。

## 決めてほしいこと

1. **Model の Server の Unit の終わり（`Result`）は Compute Scheduler が持ち、`systemctl show`（D-Bus の Library を足さない、読むだけで権限の追加なし）で、Process が消えたと分かったとき・Unit を再び起動する前・Runtime が問い合わせたときだけ読む（定期的には読まない）**（1）でよいか。推奨: **はい**。
2. **見る Unit は Deployment の設定に `unit` で名前を書いたものだけとし（今は `paw-llm-main.service`）、その Unit に `OOMPolicy=stop` を明示する**（1）でよいか。推奨: **はい**。
3. **Model の Server の OOM Kill は `compute` の Component で、直近 1 時間に 1 回でも `WARNING`（`model_server_oom_killed`）。`agent_incidents` には書かない**（2）でよいか。推奨: **はい**（数値は暫定値）。
4. **Local の Node の `ConnectionError` は、Lease の後に Unit が `oom-kill` で終わっていたら `HybridRuntime` が `AgentOutOfMemory` に直す（最大 10 秒待つ）。同時に走っていた Node はそれぞれ数える**（3）でよいか。推奨: **はい**（1 回の Kill で 3 件以上になり `task_queue` が `ERROR` になりうるが、閾値は Decision 0071 の暫定値で、運用で見直す）。
5. **CLI の Session は Unit の `Result=oom-kill` で知る（`OOMPolicy=stop`・`MemoryAccounting=yes`・`podman run --cgroups=split`、`Result` は `_UnitProcess.stop()` が、CLI が自分で終わった後なら Unit が `active` から離れるのを最大 10 秒待ってから、`systemctl stop` を出す前に読んで覚える）。`submit` の前の OOM は他の失敗より優先して `AgentOutOfMemory`**（4）でよいか。推奨: **はい**（Smoke で `split` が使えなければ、Wrapper が `OOMKilled` を読んで終了 Code で返す案に、新しい Decision で替える）。
6. **Tool の Sandbox は Wrapper が `exec` の前後で Host の側から Container の `memory.events` の `oom_kill` を比べ、Backend が新しい Tag `paw-sandbox/v2` で `exec` を送ったときだけ状態の行に `oom=<0|1|->` を足す（`v1` には今までどおりの行。Backend は全ての Wrapper を入れ替えた後に `PAW_SANDBOX_OOM_STATUS=true` で `v2` を使い始める。Application を戻しても Wrapper を戻さなくても壊れない）。`oom=1` なら Node の試行を `AgentOutOfMemory` で終える（Decision 0083 の 4 のとおり）**（5）でよいか。推奨: **はい**。代わりの案: Model に OOM を伝えて Node を続け、数だけ残す（Decision 0083 の 4 を一部置き換える）。
7. **PR を 8 の 5 つに分ける**でよいか。推奨: **はい**。

## リスク

- **Unit の `Result` が読む前に消える**: Scheduler が先に再起動すると消える。1 の「起動する前に読む」で防ぐが、人が手で `systemctl start` / `reset-failed` したときは見逃す（その時は `ConnectionError` のまま。数え漏れで、誤検知ではない）。
- **10 秒の待ち**: OOM でない接続の切断（Server の Bug・手で止めた）でも、`ConnectionError` の Node が最大 10 秒遅れて終わる。CLI の Session も、CLI が自分で終わった後の `stop()` が最大 10 秒遅れうる（普通は Unit がすぐ `inactive` になるので待たない）。Unit が `active` のままなら待つだけで、Node の Timeout より十分短い。
- **Rootless の Podman の cgroup の置き場所**: `--cgroups=split` が Rootless・`paw-agent-cli`（Login なし）の環境で動くかは文書だけでは決まらない。Smoke で確かめ、だめなら代わりの案にする（5 の判断点）。`split` が動かないまま `OOMPolicy=stop` だけ入れても、害はない（OOM が見えないだけ）。
- **`OOMPolicy=stop` で CLI の Session が止まる範囲が広がる**: Container の中の子の Process（CLI が起こした Test など）が殺されただけでも Session 全体が止まる。今は CLI がそのまま続けうる。Memory の上限（既定 4g）に達した Session は続けても不安定なので、止めて OOM と記録するほうを推奨する。
- **Sandbox の cgroup の Path の読み違い**: `CgroupPath` が予想と違う形（cgroupfs の Manager の入れ子など）だと `oom=-` になり、検出されない（今と同じ）。Path は `/sys/fs/cgroup/` の下に限り、`..` を拒むので、他の cgroup の File を読むことはない。
- **1 回の OOM Kill で複数の Node が数えられ `ERROR` になる**（4 の判断点）。

## 承認後の扱い

承認されたら `Approval` に記録して Status を Approved にし、8 の PR（Refs #251、Refs #208）で実装する。方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。
[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。
