# Orchestrator の Worker（Queue を Claim して Task を動かす `Orchestrator.serve`）を Application の起動で動かす方針（起動する条件、数、Worker の ID、終了時の取り消し）

- Status: Proposed
- Date: 2026-10-11
- Scope: Issue [#208](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/208)（Decision 0083 の 11 の 3: 「Orchestrator の Worker（`Orchestrator.serve`）を起動するかどうかは、Decision 0047 の 5 のとおりこの段階の PR で提案する」）。`paw_backend/app.py`（Lifespan）、`paw_backend/config.py`（`PAW_ORCHESTRATOR_WORKERS`）。Migration はない
- Supersedes（一部）: [Decision 0047](0047-task-execution-composition-and-task-end-effects.md)（Approved）の 5 のうち「Queue を Claim する Worker（`Orchestrator.serve`）は起動しない」を、下の 1 の条件の範囲で置き換える。0047 のその他（組み立ての範囲、定期実行）は変えない

## 背景

- `Orchestrator.serve(worker_id, stop)` は、Queue の Entry を Claim して Task の DAG を動かし、`stop` で止まる（取り消されたら持っている Entry を返す）。Queue の Lease（Decision 0046 / 0057）で Fencing され、複数の Process の Worker が同じ Queue を使ってよい作りになっている。
- Decision 0047 の 5 は、Backend に本番の `AgentRuntime` がなかったので Worker を起動しないとした。#208 の段階 3（PR #224）で Local の Main Model の `LocalAgentRuntime` ができ、`create_app(compute=..., local_model=..., sandbox=..., orchestrator_config=...)` で Orchestrator が組み立てられるようになった。しかし Worker が動かないので、`queued` の Task は誰にも Claim されない。
- **次のことはどの Decision も決めていない。** 起動する条件、Process ごとの Worker の数、Worker の ID、終了時の扱い。

## 提案（番号は「決めてほしいこと」の番号）

### 1. Orchestrator が組み立てられたときだけ、起動の時に Worker を動かす

- `create_app` が `Orchestrator` を組み立てたとき（Agent の Runtime か Local の Runtime と `orchestrator_config` を Deployment が渡したとき、Decision 0047 の 5・0058 の 7・0083）だけ、Lifespan が `orchestrator.serve(...)` を Background の Task として動かす。Orchestrator がなければ何もしない（今の本番の起動と同じ）。
- 理由: Runtime を渡すのは Deployment の明示の選択で、渡したのに Task が動かないのは誤り。逆に Runtime を渡さない配備（今の本番）には何も変わらない。
- 代替: (a) 別の Command（`python -m paw_backend.worker`）で Worker の Process を分ける（Web の Process と Lifespan を分けられるが、Compute Scheduler がプロセスに 1 つ（Decision 0037 の 1）なので、Worker の Process にも Scheduler が要り、2 つの Scheduler が同じ GPU を別々に数えてしまう。推奨しない）。(b) Worker を起動しないまま（Task を動かす手段がない）。

### 2. Process ごとの Worker の数は設定 `PAW_ORCHESTRATOR_WORKERS`（既定 1、0 で止める、最大 8）

- 1 つの Worker は 1 つの Task を順に動かし、Task の中の Node の並列は `OrchestratorConfig.max_parallel_nodes` と Compute Scheduler の Lease（KV）が決める。Task をまたいだ並列が要る配備は数を増やす。
- `0` は Orchestrator を組み立てても Worker を動かさない（障害の調査、Update の保守。`deploy_maintenance` の間は Task が始まらないので、通常はそちらで足りる）。
- 代替: 数を固定の 1 にする（設定を増やさない）。

### 3. Worker の ID は `<host 名>:<process id>:<番号>`

- `check_worker_id` の文字（`[A-Za-z0-9._:@/-]`、100 文字まで）に合わない Host 名の文字は `-` に置き換え、長すぎれば切る。Queue の Lease は Worker の ID と Claim の世代で Fencing されるので、同じ ID の再起動も別の Claim として扱われる（Decision 0046）。
- 代替: 起動ごとの UUID（Log から Host と Process が分からない）。

### 4. 終了時は Worker を最初に取り消す（取り消しの後の待ちは Shutdown の Timeout まで）

- Lifespan の終了の最初に Worker を取り消す。`serve` は持っている Entry を Queue に返し、走っていた Node を `ready` に戻す（PAW-034 の取り消し。Lease の Fencing もある）。次の起動か他の Process の Worker が続ける。Claim の途中で止まった Worker は、取れた Entry を始めずに返す。
- 取り消しの後の待ちは `settings.shutdown_timeout_seconds` まで。取り消しを無視する Worker（取り消しを握りつぶす Runtime、止まった Database）は Log を出して諦める（Database の後始末を止めない）。
- 理由: Node の試行は最大 30 分で、Shutdown の Timeout（既定 5 秒）の間に終わることはまずない。待つ間に Worker が次の Node や Entry を始めると、その作業も結局捨てられる。取り消しは安全に作られている（上の返却と Fencing）。
- 代替: 止まる合図を出して今の Node の試行の終わりまで待つ（待つ間に下流の Node が始まらないよう、Orchestrator の DAG の進行にも合図を通す必要がある。終わらなければ結局取り消す）。

## 決めてほしいこと

1. **Orchestrator が組み立てられたときだけ Lifespan が `orchestrator.serve` を動かす（Decision 0047 の 5 の「起動しない」をこの条件で置き換える）**（1）でよいか。推奨: はい。
2. **Worker の数は `PAW_ORCHESTRATOR_WORKERS`（既定 1、0 で止める、最大 8）**（2）でよいか。推奨: はい。
3. **Worker の ID は `<host>:<pid>:<n>`**（3）でよいか。推奨: はい。
4. **終了時は Worker を最初に取り消し（Entry は返り、Node は `ready` に戻る。Claim の途中なら始めずに返す）、取り消しの後の待ちは Shutdown の Timeout まで**（4）でよいか。推奨: はい。

## 承認後の扱い

承認されたら `Approval` に記録し、Status を Approved に改める。
方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。
