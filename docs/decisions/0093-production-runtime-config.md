# 本番の起動で Compute Scheduler・Local の Model・Sandbox・Orchestrator・git の Runner を組み立てる設定（1 つの TOML の File `PAW_RUNTIME_CONFIG_FILE`、File の保護、無いときの既定、部分ごとの有効化、この Host の最初の配備の例）

- Status: Proposed
- Date: 2026-10-16
- Scope: Issue [#208](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/208) の残り（最初の本番の Install、2026-10-16 に見つかった問題）。この Decision は設計だけで、コードは承認の後の下の 8 の PR で書く。関係する既存の口: `apps/backend/paw_backend/server.py`（`main()` → `create_app(settings)`）、`paw_backend/app.py`（`create_app(compute=..., local_model=..., sandbox=..., orchestrator_config=..., agent_runtimes=..., local_runtimes=..., git_runner=...)`）、`paw_backend/config.py`（`Settings`、`PAW_*`）、`compute/wiring.py`（`ComputeSetup`）・`compute/config.py`（`ComputeConfig`・`DeploymentSpec`）・`compute/control.py`（`CommandModelControl`・`DeploymentCommands`）・`compute/probe.py`（`NvidiaSmiProbe`）、`agents/wiring.py`（`LocalModelSetup`）、`sandbox/wiring.py`（`SandboxSetup`）・`sandbox/runner.py`（`SshSandboxRunner`・`LocalWrapperSandboxRunner`）、`orchestrator/config.py`（`OrchestratorConfig`）・`orchestrator/composition.py`（`build_task_execution`）、`repositories/ssh.py`（`SshGitRunner`・`SshGitRunnerPolicy`・`TemplateSshKeyDirectory`）、`apps/backend/deploy/systemd/paw-backend.service`・`paw-llm-main.service`。Migration はない
- Supersedes（一部）: [Decision 0058](0058-compute-scheduler-application-wiring.md)（Approved）の 1 のうち「環境変数・設定 File は作らない」の部分だけ。0058 の 1 は、`ModelControl` の Command を File から読む形式とその保護（所有者・権限の確認）を Runtime の Adapter の Issue（#208）で決めるとしていた。この Decision はそれを決め、`PAW_RUNTIME_CONFIG_FILE` の File を読んで同じ `create_app(compute=ComputeSetup(...))` などの注入を `server.py` が組み立てる形にする。0058 の 1 の「`ComputeSetup` を渡したときだけ有効になり、渡さなければ何も起動しない」と 2〜8 は変えない。次は書き換えず、その中で値を選ぶ: [Decision 0029](0029-per-user-git-runner-ssh.md) / [Decision 0056](0056-production-worktree-wiring.md) の 3（git の Runner は `create_app(git_runner=...)` で渡す、`SshGitRunner` への差し替え）、[Decision 0039](0039-compute-scheduler-calibration.md) / [Decision 0072](0072-runtime-jit-host-memory-guard.md)（Model の起動の上限・ホストの `MemAvailable` 40 GiB）、[Decision 0075](0075-coexistence-footprints-qwen38.md)（Footprint・`restore_margin_bytes` 4 GiB）、[Decision 0083](0083-agent-runtime-adapters.md)（Runtime の Adapter。CLI の段は `CloudPolicy` が注入されるまで組み立てが拒否する）、[Decision 0086](0086-orchestrator-worker-start.md)（`PAW_ORCHESTRATOR_WORKERS`）、[Decision 0087](0087-cli-secret-store-and-egress.md) / [Decision 0091](0091-egress-per-session-socket.md)（Secret Store と Egress）、[Decision 0092](0092-runtime-oom-kill-detection.md)（`DeploymentCommands.unit`・`PAW_SANDBOX_OOM_STATUS`）

## 背景

- 最初の本番の Install（2026-10-16）で、本番の入口 `server.py` の `main()` が `create_app(settings)` だけを呼び、`compute`・`local_model`・`sandbox`・`orchestrator_config`・`agent_runtimes`・`local_runtimes`・`git_runner` のどれも渡していないことが分かった。そのため本番では認証と Project / Repository の HTTP の経路だけが動き、`/api/v1/admin/compute/full-gpu` などの Scheduler の経路は `503`（`compute_not_configured`）を返し、Orchestrator は組み立てられず（Decision 0086 の Worker も起動せず）、Task の Node はどれも走らない。git は既定の `SubprocessGitRunner` で、Backend の User（`paw`）と違う Account の Checkout は `identity_mismatch` で拒否される（Decision 0056 の 3）。
- これは設計どおりの状態である。Decision 0058 の 1 は「環境変数・設定 File は作らない」とし、`ModelControl` の Command（`systemctl` など）を File から読むと、それを書ける人が Backend に任意の Command を実行させられるので、形式と保護を #208 で決めるとした。`SandboxSetup`（`sandbox/wiring.py`）と `LocalModelSetup` も同じ考えで、環境変数を持たない。Test と GPU の Smoke（`/data/results/paw-smoke-2026-10-15`）は Python のコードでこれらを組み立てていた。
- 今の `Settings`（`PAW_*` の環境変数、`/etc/paw/backend.env`）は値（Port・DB の URL・Timeout・SSH の Host など）を持つが、Command の列・Deployment の一覧・Ladder のような構造は持たない。
- Repository には TOML の設定を読む既存の口が 3 つある（どれも Python 標準の `tomllib`）: `deploy/release/paw_release.py`（`/etc/paw/release.toml`。`[commands]` は argv の List の List で、型の違いは固定の文で拒否、`env_file` は `st_mode & 0o077` なら拒否）、`deploy/egress/paw_egress.py`（`/etc/paw/egress.toml`。絶対 Path と Host 名の検査）、`deploy/cli-session/paw_cli_session_run.py`（`/etc/paw/cli-session.toml`。絶対 Path・Digest の検査、古い Key は明示して拒否）。Backend の中には設定 File の Loader はない。`paw_backend/recovery/files.py` と `deploy/restore_points.py` に、`lstat`・`st_uid`・Mode を確かめる型がある。

## 提案

### 1. 1 つの TOML の File を `PAW_RUNTIME_CONFIG_FILE` で指し、`server.py` が読んで `create_app` に注入する

- **推奨:** 新しい設定 `PAW_RUNTIME_CONFIG_FILE`（`Settings.runtime_config_file: Path | None`、既定 `None`）に File の絶対 Path を書く。本番の推奨の置き場所は `/etc/paw/runtime.toml`。`server.main()` は `Settings()` の後にこの File を読み（下の 2 の検査）、`RuntimeWiring`（`create_app` の引数の組）に変え、`create_app(settings, **wiring)` を呼ぶ。読むのは起動の時の 1 回だけで、変えたら再起動する（Reload はしない。`paw-release` の更新でも再起動で読み直す）。
- 形式は **TOML**（`tomllib`。Library を足さない。既存の 3 つの Loader と同じ）。最上位に `version = 1`（必須。違う値は拒否）を置く。
- 新しい Module `paw_backend/runtime_config.py` に Parser を置く。Parser は File の内容を **既存の Dataclass の Constructor に渡すだけ**で、値の検査はまず Parser が型と範囲と相互参照を確かめ、さらに各 Dataclass の `__post_init__`（`DeploymentSpec`・`ComputeConfig`・`DeploymentCommands`・`OrchestratorConfig`・`SshGitRunnerPolicy` など）が今までどおり確かめる。既存の Loader の型（不明な Key の拒否、argv は文字列の List、固定の文の Error）を使う。
- Section と Dataclass の対応:

| Section | 作るもの | Key（File の側） | 備考 |
| --- | --- | --- | --- |
| `[compute]` | `ComputeSetup(config=ComputeConfig(...), probe=NvidiaSmiProbe(executable=...), control=CommandModelControl(...))` | `nvidia_smi`（既定 `/usr/bin/nvidia-smi`）、`gpu_index`（既定 0）、`restore_margin_mib`（既定なし = Code の既定。この Host は Decision 0075 の 4,096） | Headroom・`kv_safety`・Class の上限・各 Timeout など、Decision 0037 / 0039 / 0042 / 0076 が値を決めたものは **File から変えられない**（Code の既定のまま。下の決めてほしいこと 4） |
| `[[compute.deployments]]` | `DeploymentSpec` | `name`・`role`（`main` / `memory_worker` / `embedding` / `reranker`）・`weights_mib`・`kv_pool_mib`・`runtime_mib`・`workspace_mib`・`kv_bytes_per_token`・`max_sequences`・`max_context_tokens`・`residency`（`always` / `if_room`）・`cpu_fallback`・`initial`（`gpu` / `cpu` / `unloaded`） | Byte の量は MiB の整数で書き、Parser が `× 1,048,576` する（Byte の桁の書き間違いを避ける）。`kv_bytes_per_token` だけ Byte |
| `[compute.deployments.commands]` | `DeploymentCommands` → `CommandModelControl({name: ...})` | `gpu`・`unload`・`cpu`・`pids`（どれも argv の List）、`metrics_url`、`unit` | どの Deployment にも `commands` が無ければ `control=None`（Scheduler は見て Admission するだけ。Full GPU Mode は開始できない: Decision 0037 の 7）。`min_host_available_bytes`（40 GiB、Decision 0072）と Timeout は File から変えない |
| `[local_model]` | `LocalModelSetup` | `base_url`・`deployment`（既定 `main`）・`served_model`（既定 `main`）・`model_id`（既定 `Qwen3.8-27B-FP8`）・`agent_label`（既定 `local`） | `reasoning_history`（Decision 0076 の 1 の `MAIN_REASONING_POLICY`）と `limits` は File から変えない。`[compute]` が要る |
| `[sandbox]` | `SandboxSetup(runner=..., shim_base=..., shim_group=..., idle_seconds=...)` | `runner`（`ssh` / `local_wrapper`）、`shim_base`、`shim_group`（Group の **名前**。起動時に `grp.getgrnam` で gid にする）、`idle_seconds`（既定 900）。`local_wrapper` のときだけ `wrapper`（絶対 Path）と `wrapper_options`（文字列の List） | `ssh` は `SshSandboxRunner(TemplateSshKeyDirectory([ssh] の identity_template), policy=SshGitRunnerPolicy.from_settings(settings))` |
| `[git]` | `git_runner` | `runner`（`subprocess` / `ssh`。既定 `subprocess`） | `ssh` は `SshGitRunner(TemplateSshKeyDirectory(...), policy=SshGitRunnerPolicy.from_settings(settings))`。`[sandbox] runner = "ssh"` なら `ssh` でなければ拒否（Sandbox の git の Shim は Host 側で `git_runner` を使い、他の Account の Checkout を `subprocess` では読めない） |
| `[ssh]` | `TemplateSshKeyDirectory(identity_template)` | `identity_template`（既定 `/etc/paw/ssh-keys/{user}.key`） | `[git]` と `[sandbox]` の `ssh` が共有する。SSH の接続先（Host・Port・Timeout・`known_hosts`）は今の `PAW_REPOSITORY_SSH_*` のまま（Decision 0029）。鍵の Path のひな型は Decision 0029 の 6 / 0056 の 3 が「設定にまだない」とした値 |
| `[orchestrator]` | `OrchestratorConfig` | `max_parallel_nodes`（既定 4）・`node_timeout_seconds`（既定 1,800）、`[orchestrator.ladders]` に `planner`・`worker`・`researcher`・`reviewer` の 4 つ（それぞれ Label の List、全部必須） | Ladder の Label は **有効な Runtime の Label**（今は `[local_model].agent_label` だけ）でなければ拒否。`poll_seconds`・Retry の回数などは Code の既定 |
| `[agents.codex]` / `[agents.claude]` | （予約） | — | 今は書けば拒否（`cli runtimes are not available yet`）。下の 3 |

- **環境変数に残すもの**（`/etc/paw/backend.env`）: `Settings` がすでに持つ値はすべて今のまま。とくに `PAW_HOST`・`PAW_PORT`・`PAW_DATABASE_URL` などの DB の URL（Secret）・`PAW_SECRET_STORE_DIR`（Decision 0087）・`PAW_REPOSITORY_SSH_*`（Decision 0029）・`PAW_REPOSITORY_*` の Policy・`PAW_ORCHESTRATOR_WORKERS`（Decision 0086）・`PAW_SANDBOX_OOM_STATUS`（Decision 0092 の 5）・`PAW_HEALTH_GPU_PROBE`。新しく足すのは `PAW_RUNTIME_CONFIG_FILE` だけ。**この File には Secret を書かない**（Schema に Secret の Key が無く、不明な Key は拒否するので書けない）。同じ値を環境変数と File の両方に置かない（`PAW_REPOSITORY_SSH_*` を File に写さない）。
- 代替: (a) すべてを環境変数にする（`PAW_COMPUTE_MAIN_GPU_COMMAND` のような Key の列になり、argv・Deployment の一覧・Ladder を 1 行の文字列で表すので、Quote と区切りの規則が増える。推奨しない）。(b) JSON にする（Comment が書けない。運用の File として TOML を使う既存の 3 つと揃わない）。(c) Python の Module を `import` して組み立てる（File を書ける人が任意の Python を動かせる。推奨しない）。(d) Section ごとに別の File（`compute.toml` など）にする（相互参照の検査 — Ladder の Label・Deployment の名前 — が File をまたぎ、保護の検査も File の数だけ要る。推奨しない）。

### 2. File の保護と、どの誤りでも起動しない（Fail closed）

この File は Backend（`paw`）が実行する Command を決めるので、**書ける人は Backend の User として任意の Command を実行できる**。そこで root だけが書ける File にし、確かめられなければ起動しない。

- **File の検査**（`open(path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC)` で開き、開いた Descriptor の `fstat` で確かめ、同じ Descriptor から読む。検査と読み取りの間に差し替えられない）:
  - `PAW_RUNTIME_CONFIG_FILE` は絶対 Path で、`os.path.realpath` が同じ Path（途中に Symbolic Link がない）。
  - 通常の File（`S_ISREG`）。Symbolic Link は拒否（`O_NOFOLLOW`）。
  - 所有者は root（`st_uid == 0`）。
  - Group と Others が書けない（`st_mode & 0o022 == 0`）。推奨の Mode は `0640`、Group は Backend の User の Group（`root:paw`）。`0644` も受け付ける（Secret は無い）。
  - 大きさは 64 KiB まで。UTF-8 の TOML として読めること。
- **上の Directory の検査**: File から `/` までの各 Directory が root の持ち物で、Group と Others が書けない（`/etc/paw` は `root:root 0755`）。Directory を書ける人は File を `rename` で差し替えられるため。
- **Command の検査**（`gpu`・`unload`・`cpu`・`pids`。今の `DeploymentCommands._argv` の検査 — 1〜64 個の空でない文字列、NUL なし、4,096 文字まで — に足す）:
  - argv の List だけ（文字列 1 つは拒否。**Shell は使わない**: `CommandModelControl` はもともと `create_subprocess_exec` で Shell を通さない）。
  - `argv[0]` は正規化した絶対 Path（`..`・`.`・二重の `/` なし）で、`os.path.realpath` が同じ Path（途中に Symbolic Link がない。`/usr/bin/systemctl` のように書く）、起動の時に存在する通常の File、所有者 root、Group と Others が書けない、実行できる。
  - **`argv[0]` から `/` までの各 Directory も root の持ち物で、Group と Others が書けない**（設定 File の上の Directory の検査と同じ規則）。File 自身が root の持ち物でも、Directory を Backend の User（またはその Group）が書ければ、起動の検査の後に `rename` で差し替えられ、`CommandModelControl` は Path 名で実行するので差し替えた File が動くため（Codex review #263）。Sticky の付いた Directory（`/tmp` など）も拒否する。
  - `argv[0]` が Shell と汎用の起動役（`sh`・`bash`・`dash`・`zsh`・`env`・`sudo`・`su`・`python*`・`perl`・`busybox`）の名前なら拒否する。これは境界ではなく（境界は root だけが書ける File）、`/bin/sh -c '…'` の形で Shell を持ち込む書き間違いを防ぐためである。
  - 読み取りの Command（`pids`）も同じ規則。
- **URL の検査**: `[local_model].base_url` と `metrics_url` は `http://127.0.0.1:<port>/…` か `http://[::1]:<port>/…` だけ（Loopback）。Host 名（`localhost` を含む）・外の Address は拒否する（Prompt と Metrics を Host の外へ送る設定を作らない）。
- **Path の検査**: `shim_base`・`wrapper`・`nvidia_smi`・`identity_template` は絶対 Path（既存の Dataclass の検査に加えて）。`nvidia_smi` と `wrapper` は `argv[0]` と同じ所有者・権限の検査（File と、`/` までの各 Directory）。
- **Fail closed**: 次のどれでも、Backend は起動しない（`server.main()` が `Invalid runtime configuration: <section>.<key>: <理由>` を標準 Error に出し、終了 Code 2。今の `Invalid configuration` と同じ扱い）。
  - `PAW_RUNTIME_CONFIG_FILE` が設定されているのに File が無い・読めない・上の保護を満たさない。
  - TOML として読めない、`version` が無い・違う。
  - 不明な Section・不明な Key・型の違い・範囲外・必須の Key の欠け・重複した Deployment の名前。
  - 相互参照の誤り: `[local_model]` が `[compute]` なしにある、`[local_model].deployment` が `[[compute.deployments]]` に無い、Ladder の Label が有効な Runtime に無い、`[orchestrator]` があるのに Runtime が 1 つも無い、`[sandbox] runner = "ssh"` で `[git] runner` が `ssh` でない、`shim_group` の Group が無い。
  - 既存の Dataclass の `__post_init__` と `create_app` / `build_task_execution` の `TypeError` / `ValueError`。
  - **一部だけ有効にして起動することはしない**（File 全体を受け入れるか、起動しないか）。誤りのある File を「無いもの」として扱って起動を続けることもしない。
- Error の文は Section と Key の名前と固定の理由だけで、**値（Path・Command・URL）を含めない**（今の `Settings` の Error と同じ）。起動に成功したときは、有効にした部分（`compute: deployments=main control=yes`、`local_model: yes`、`sandbox: ssh`、`git: ssh`、`orchestrator: ladders=local`）を INFO の Log に 1 行出す。Command と Path は Log に出さない。
- 代替: (a) 所有者は root か Backend の User のどちらでもよいとする（Backend の Process が乗っ取られたとき、File を書き換えて次の起動で別の Command を動かせる。推奨しない）。(b) Mode を `0600 root` にする（Backend の User が読めない。推奨しない）。(c) `argv[0]` の名前の拒否の一覧を持たない（root の File が境界なので不要とも言えるが、書き間違いの防止として安い。推奨は持つ）。

### 3. File が無いときの既定と、部分ごとに有効にする方法

- **推奨: `PAW_RUNTIME_CONFIG_FILE` が設定されていなければ、今と同じ**（何も組み立てない: 認証・Project / Repository・System Health だけ。Scheduler の経路は `503`、Orchestrator なし、git は `SubprocessGitRunner`）。既定の置き場所（`/etc/paw/runtime.toml`）を自動では探さない（置いただけで Command が動き出さないように、環境変数で明示したときだけ読む）。ただし起動の時に WARNING を 1 回出す（`runtime configuration not set: compute, local model, sandbox and orchestrator are disabled`）。最初の Install で気付けなかったことへの対策。
- **Section は独立に有効にできる**。書いた Section だけを組み立てて、それぞれ今の `create_app` の引数に渡す。依存は `create_app` の今の規則と同じ:

| 書いた Section | 動くもの |
| --- | --- |
| なし（File なし） | 今と同じ |
| `[git] runner = "ssh"` だけ | Repository の登録・Worktree の git が `SshGitRunner`（他の Section なしでもよい） |
| `[compute]` | Scheduler・Full GPU Mode の経路・System Health の `compute`（`control` があれば Model の起動・停止） |
| `[compute]` + `[local_model]` | 上に加えて Inferred Preference の Interpreter（#38）が Main の Model を `INTERACTIVE` の Lease で使う |
| `[compute]` + `[local_model]` + `[sandbox]` + `[git] ssh` + `[orchestrator]` | 上に加えて Local の Agent の Runtime（Label `local`）が Node を走らせる。Orchestrator が組み立てられ、`PAW_ORCHESTRATOR_WORKERS`（既定 1）の Worker が起動する（Decision 0086） |
| `[sandbox]` だけ（`[orchestrator]` なし） | 受け付ける（Tool Broker に Repository の Tool が登録されるだけで、呼ぶ Runtime はない） |

- **CLI の Runtime（Codex / Claude Code）はこの Decision では有効にできない。** Decision 0083 の 1 のとおり `CloudPolicy` が注入されるまで CLI の段は Ladder に置けず、本番の `CloudPolicy` はまだ無い（その Decision も、CLI の Smoke も、Provider の規約の確認もまだ）。`[agents.codex]` / `[agents.claude]` の名前は予約し、書けば起動しない。CLI を有効にするときは、`CloudPolicy` の Decision と CLI の Smoke の後に、この File の Schema に Section を足す新しい Decision を作る（`session_base`・`relay_command`・`model` など、`CodexCliRuntime` / `ClaudeCliRuntime` の引数）。
- 推奨の有効にする順（下の 5 の Human の確認と合わせる）: (1) `[git] ssh` + `[compute]`（Model は Scheduler が起動）→ (2) `[local_model]` → (3) `[sandbox]` + `[orchestrator]`。各段で再起動して確かめる。
- 代替: (a) File が無ければ起動しない（今の本番の Deploy がすべて止まる。Test 用・最小の構成でも File が要る。推奨しない）。(b) Section ごとに `enabled = true/false` を持つ（Section を消せば無効になるので同じことを 2 通りに書ける。推奨しない）。

### 4. この Host の最初の配備の値（**例**。承認の対象は形で、値は配備のときに Human が確かめる）

前提（例）: Backend は Linux User `paw`、Port 8400（環境変数）。Workspace の User は `tomoki` 1 人。Main の Model は `paw-llm-main.service`（vLLM、Port 18080、`--served-model-name main`、Qwen3.8-27B-FP8、`--gpu-memory-utilization 0.51 --max-model-len 131072 --max-num-seqs 8`）だけで、Memory Worker・Embedding・Reranker はまだ配備しない。Sandbox は SSH の Wrapper（`tomoki@127.0.0.1`）を通す。

`/etc/paw/backend.env`（抜粋。今の形に足す・確かめる行だけ）:

```sh
PAW_PORT=8400
PAW_RUNTIME_CONFIG_FILE=/etc/paw/runtime.toml
PAW_REPOSITORY_SSH_HOST=127.0.0.1
PAW_REPOSITORY_SSH_KNOWN_HOSTS_PATH=/etc/paw/ssh_known_hosts
PAW_ORCHESTRATOR_WORKERS=1
# Wrapper を paw-sandbox/v2 の版に入れ替えた後だけ（Decision 0092 の 5）
PAW_SANDBOX_OOM_STATUS=false
```

`/etc/paw/runtime.toml`（`root:paw`、`0640`）:

```toml
# 例: この Host の最初の配備（2026-10-16）。値は配備のときに確かめる。
version = 1

[compute]
nvidia_smi = "/usr/bin/nvidia-smi"
gpu_index = 0
restore_margin_mib = 4096            # Decision 0075 の 2

[[compute.deployments]]
name = "main"
role = "main"
# Footprint 54,152 MiB（Decision 0075 の 1、Load の途中を含む Peak + 2 GiB）
# = 重み等 34,286 + KV の Pool 19,866（2026-10-15 の Smoke の vLLM の Log:
# "Available KV cache memory: 19.4 GiB"、"GPU KV cache size: 305,081 tokens"）
weights_mib = 34286
kv_pool_mib = 19866
kv_bytes_per_token = 68279           # 19.4 GiB / 305,081 token
max_sequences = 8                    # --max-num-seqs 8
max_context_tokens = 131072          # --max-model-len 131072
residency = "always"
initial = "gpu"

[compute.deployments.commands]
gpu = ["/usr/bin/systemctl", "start", "paw-llm-main.service"]
unload = ["/usr/bin/systemctl", "stop", "paw-llm-main.service"]
# 止まっている Unit には cgroup が無く、cat では失敗する（下の注）。
pids = ["/usr/local/lib/paw/paw-unit-pids", "paw-llm-main.service"]
metrics_url = "http://127.0.0.1:18080/metrics"
unit = "paw-llm-main.service"        # Decision 0092 の 1

[local_model]
base_url = "http://127.0.0.1:18080/v1"
deployment = "main"
served_model = "main"
model_id = "Qwen3.8-27B-FP8"
agent_label = "local"

[ssh]
identity_template = "/etc/paw/ssh-keys/{user}.key"   # tomoki は /etc/paw/ssh-keys/tomoki.key

[git]
runner = "ssh"

[sandbox]
runner = "ssh"                       # tomoki@127.0.0.1 の Forced Command の Wrapper
shim_base = "/run/paw-sandbox"
shim_group = "paw-sandbox"
idle_seconds = 900

[orchestrator]
max_parallel_nodes = 4               # Decision 0076（変えない）
node_timeout_seconds = 1800

[orchestrator.ladders]
planner = ["local"]
worker = ["local"]
researcher = ["local"]
reviewer = ["local"]
```

- Reviewer も `local` なので、Review は Worker と同じ Model で、REQUIREMENTS の「Review の独立性」は CLI の段が有効になるまで満たさない（`OrchestratorConfig` の説明のとおり Ladder の書き方の問題で、Orchestrator は強制しない）。これは今の状態の確認で、この Decision で変えない。
- **`pids` に `cat …/cgroup.procs` を使わない**（Codex review #263）。Unit を Enable せず Scheduler が起動する形では、止まっている（または落ちた）Unit に cgroup の Directory が無いので `cat` が失敗し、`CommandModelControl.processes()` が `ModelControlError` になる。Scheduler は PID を「分からない」として扱い、`initial = "gpu"` を `unloaded` に直さないので、`always` の Main の `gpu` の Command が走らず、Model がいつまでも起動しない。そこで小さな読み取り専用の Helper `paw-unit-pids <unit>` を配備に足す（下の 8 の PR 3。root の持ち物 `0755` で `/usr/local/lib/paw/` に置く）。Helper は Unit の名前を `^[A-Za-z0-9@._-]+\.service$` で確かめ、`/usr/bin/systemctl show -p LoadState -p ControlGroup -- <unit>` を読み、`LoadState` が `loaded` でなければ失敗（名前の書き間違いを「止まっている」と取り違えない）、`ControlGroup` が空なら何も出さずに成功（止まっている）、あれば `/sys/fs/cgroup<ControlGroup>/cgroup.procs` と子の cgroup の `cgroup.procs` の PID を出す（読めなければ失敗）。Shell を使わず、Unit の `start` / `stop` はしない。
- `paw-llm-main.service` の `--port 18080` と `base_url`・`metrics_url` の Port、`--served-model-name main` と `served_model` は一致させる（Parser は Unit の File を読まないので、一致は Human が確かめる）。

### 5. Human が配備で確かめること（Agent は他の Linux User・systemd・GPU を操作しない）

- [ ] `ls -l /etc/paw/runtime.toml` が `root paw` で `-rw-r-----`、`/etc/paw` が `root root drwxr-xr-x`。
- [ ] `paw` が `paw-llm-main.service` の `start` / `stop` だけを許される polkit の規則が入っている（`paw-llm-main.service` の注釈）。`sudo -u paw /usr/bin/systemctl show -p Result -p ActiveState -p InvocationID -- paw-llm-main.service` が読める（Decision 0092 の 1）。
- [ ] `sudo -u paw /usr/local/lib/paw/paw-unit-pids paw-llm-main.service` が、Unit が動いている間は vLLM の全 Process の PID を返し、止まっている間は何も出さずに終了 Code 0、存在しない Unit の名前では 0 以外になる。
- [ ] `namei -l /usr/bin/systemctl /usr/local/lib/paw/paw-unit-pids /usr/bin/nvidia-smi` のどの段も root の持ち物で、Group / Others が書けない。
- [ ] `tomoki` の `authorized_keys` に Wrapper の `command=`（`--sandbox-image=…@sha256:…`・`--sandbox-shim-base=/run/paw-sandbox`・`--sandbox-keep-groups=yes`）と `from="127.0.0.1"`、`/etc/paw/ssh-keys/tomoki.key`（`paw` の持ち物、`0600`）、`/etc/paw/ssh_known_hosts`（Decision 0029、`deploy/ssh-git-wrapper/README.md`、`deploy/sandbox/README.md` のチェックリスト）。
- [ ] `/run/paw-sandbox`（`paw:paw-sandbox 0711`、`tmpfiles.d`）と Group `paw-sandbox`（`paw`・`tomoki`）。
- [ ] `python -m paw_backend.cli runtime-config-check /etc/paw/runtime.toml`（下の 8 の PR 2）が通り、有効な部分の一覧が期待どおり。
- [ ] 3 の順に有効にして、各段で: `GET /api/v1/system/health` の `compute`、`GET /api/v1/admin/compute/full-gpu` が `503` でない、Main が `gpu` に見える。(2) の後に Preference の確認が Model で答える。(3) の後に小さな Task（読むだけの Repository の調べもの）が Local の Node で走り、`agent_dag_node_attempts` に `local_gpu` が記録される。
- [ ] (3) の Task を GPU で走らせることは GPU の Smoke と同じく Human の許可を得てから（Decision 0083 の 9）。

### 6. Test の方針

- Parser の Unit Test は File の内容（TOML の文字列）と、検査の対象（所有者・Mode・Directory）を **差し替えられる口**（`stat` の関数・期待する uid を引数にする）で書き、root の File を作らずに所有者の検査を確かめる。実際の `systemctl`・`nvidia-smi`・SSH・GPU は使わない。
- 拒否の Test は 2 の各項目について 1 つ以上（Symbolic Link、`0o666`、所有者が root でない、Directory が書ける、不明な Key、文字列の argv、相対 Path の `argv[0]`、`/bin/sh` の `argv[0]`、Loopback でない URL、未知の Label、`[local_model]` だけ、`[sandbox] ssh` と `[git] subprocess`、予約の `[agents.codex]`）。Error の文に値が出ないことも確かめる。
- `server.main()` の Test: 環境変数が無ければ `create_app` に何も足さない（今と同じ）、正しい File なら引数が組み立てられる、誤りなら終了 Code 2。

### 7. 起動の Command で事前に確かめる

- **推奨:** `python -m paw_backend.cli runtime-config-check <path>` を足す。同じ Parser で検査し、有効にした部分の一覧（値なし）を出して終わる（Backend を起動せず、DB にも繋がない）。`argv[0]` などの存在の検査も行うので、配備の Host で `paw` として動かす。File を変えた後、再起動の前に使う。

### 8. PR の分け方（どれも「Refs #208」）

1. `paw_backend/runtime_config.py` の Parser と Unit Test（2 の検査・1 の対応・3 の依存の規則。`create_app` も `server.py` も変えない）。
2. `Settings.runtime_config_file`（`PAW_RUNTIME_CONFIG_FILE`）、`server.main()` の配線、起動の Log（有効な部分と、無いときの WARNING）、`runtime-config-check` の Command、その Test。`sandbox/wiring.py`・`compute/wiring.py` の「環境変数の設定はない（Decision 0058 の 1）」の Docstring をこの Decision に合わせる。
3. 配備と文書: `apps/backend/deploy/runtime/paw-unit-pids`（4 の注の Helper。Fake の `systemctl` と一時の cgroup の木で Unit Test）、`apps/backend/deploy/runtime/runtime.example.toml`（4 の例）、`docs/DEPLOYMENT_UPDATE.md` の配備の手順（5 のチェックリスト）、`apps/backend/README.md`、`paw-backend.service` の注釈（`backend.env` の `PAW_RUNTIME_CONFIG_FILE`）。
4. （PR ではない）Human が 5 の順に本番で有効にし、結果を Issue #208 に記録する。

## リスク

- **File を書ける人は Backend の User として Command を実行できる**: root だけが書けるようにし（2）、起動の時に確かめる。root が誤った Command を書いた場合は防げない（root は Admin の側として受け入れる）。
- **起動しない範囲が広がる**: File の 1 か所の誤りで Backend 全体が起動しない（Login もできない）。誤った部分だけ無効にして起動すると、Compute なしで Task が待ち続けるなど気付きにくい壊れ方になるので、起動しないほうを推奨する。`runtime-config-check`（7）で再起動の前に確かめ、`paw-release` の更新の Health Check（Decision 0079）が失敗を見つける。
- **Unit と File の値のずれ**: Port・`served-model-name`・Footprint は `paw-llm-main.service` と File の 2 か所にある。Parser は Unit を読まないので、ずれは起動しても見つからない（Model に繋がらなければ `ConnectionError`、Footprint がずれれば Admission が誤る）。5 の確認で見る。
- **`argv[0]` の存在の検査**: 起動の時だけ確かめるので、後で消された Command は実行の時に失敗する（今の `ModelControlError` のまま）。root が後で Directory の権限を緩めた場合も、次の起動まで見つからない。
- **Footprint の値の出どころ**: 4 の KV の Pool は 2026-10-15 の Smoke の 1 回の Log の値で、vLLM の版や引数が変われば変わる。Footprint の合計は Decision 0075 の値（Peak + 2 GiB）を守る。
- **Orchestrator を有効にすると Worker がすぐ Queue の Task を取る**（Decision 0086、`PAW_ORCHESTRATOR_WORKERS=1`）。(3) の前に Queue に古い Task が残っていれば走り出す。有効にする前に Queue を確かめる（または `PAW_ORCHESTRATOR_WORKERS=0` で組み立てだけ確かめる）。

## 承認が要る点（決めてほしいこと）

1. **本番の組み立ては 1 つの TOML の File（`PAW_RUNTIME_CONFIG_FILE`、推奨の置き場所 `/etc/paw/runtime.toml`、`version = 1`）を `server.py` が起動の時に 1 回読んで `create_app` の引数にする。Section は `[compute]`（+ `[[compute.deployments]]` と `commands`）・`[local_model]`・`[ssh]`・`[git]`・`[sandbox]`・`[orchestrator]`（+ `ladders`）で、1 の表のとおり既存の Dataclass に写す。Secret と今の `PAW_*` の値は環境変数に残す**（1）でよいか。推奨: **はい**。Decision 0058 の 1 の「設定 File は作らない」だけをこの形で Supersede する。
2. **File は root の持ち物・Group と Others が書けない（推奨 `root:paw 0640`）・Symbolic Link でない・上の Directory も root だけが書ける、を開いた Descriptor で確かめ、Command は argv の List で `argv[0]` は root の持ち物の絶対 Path・`argv[0]` から `/` までの Directory も root だけが書ける・Shell の名前は拒否、URL は Loopback だけ。どの誤りでも起動しない（一部だけの起動はしない）、Error に値を出さない**（2）でよいか。推奨: **はい**。
3. **File が無ければ今と同じ（何も組み立てない。WARNING を 1 回）で、既定の Path を自動では探さない。Section は独立に有効にでき、依存は今の `create_app` の規則のとおり。`[sandbox] ssh` には `[git] ssh` を求める**（3）でよいか。推奨: **はい**。
4. **Decision が値を決めた Scheduler の数値（Headroom・`kv_safety`・Class の上限・Timeout・`min_host_available_bytes` 40 GiB・`reasoning_history`）は File から変えられないようにし、Deployment ごとの値と `gpu_index`・`restore_margin_mib`・Ladder・並列数・Node の Timeout だけを書けるようにする**（1）でよいか。推奨: **はい**（値を変えるときは Decision と Code の既定を変える）。代替: すべての Field を File で上書きできるようにする（承認した値と本番の値がずれうる）。
5. **Byte の量は MiB の整数（`weights_mib` など）で書き、`kv_bytes_per_token` だけ Byte で書く**（1）でよいか。推奨: **はい**。代替: Dataclass と同じ Byte の Key（`weights_bytes`）。
6. **SSH の鍵の Path のひな型 `identity_template` はこの File の `[ssh]` に置き、接続先は今の `PAW_REPOSITORY_SSH_*` のままにする**（1）でよいか。推奨: **はい**。代替: `PAW_REPOSITORY_SSH_IDENTITY_TEMPLATE` を環境変数に足す（SSH の設定が 1 か所にまとまるが、Runner を選ぶ設定は File にあるので、片方だけ書いて動かない組み合わせが生まれる）。
7. **CLI の Runtime（`[agents.codex]` / `[agents.claude]`）は名前を予約して、今は書けば起動しない。`CloudPolicy` の Decision と CLI の Smoke の後に新しい Decision で Schema に足す**（3）でよいか。推奨: **はい**。
8. **4 の値をこの Host の最初の配備の例として載せる（Main だけ、`pids` は止まった Unit でも成功する Helper `paw-unit-pids`、Ladder はすべて `local`、Port 8400、`tomoki`、SSH の Wrapper の Sandbox）。値そのものは配備のときに 5 で Human が確かめ、例は承認の対象に含めない**（4）でよいか。推奨: **はい**。
9. **`runtime-config-check` の Command を足し、8 の 3 つの PR（Parser と Test → `server.py` の配線と Command → 文書）で実装する。本番での有効化は Human が 3 の順に行う**（7・8）でよいか。推奨: **はい**。

## 承認後の扱い

承認されたら `Approval` に記録して Status を Approved にし、8 の PR（Refs #208）で実装する。方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。
[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。
