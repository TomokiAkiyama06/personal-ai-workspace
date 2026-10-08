# Task の一覧の並列の上限と VRAM（何を「並列の上限」とするか、VRAM を誰に見せるか）

- Status: Approved
- Approval: 2026-10-08、Humanが作業Session内で、判断点ごとの説明（推奨つき）を受けたうえで直接回答して承認（すべての判断点を推奨どおり承認（画面の目視確認も OK）。末尾の「承認時の決定」）
- Date: 2026-10-08
- Scope: Issue [#185](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/185) の 1 の残り（Task の一覧の Scheduler の並列の上限と VRAM）。`apps/backend/paw_backend/compute/scheduler.py`（`ComputeScheduler.coding_capacity()`）、`apps/backend/paw_backend/api/v1/tasks.py`（`GET /api/v1/tasks` の `capacity`）、`apps/web/src/tasks/apiSource.ts`
- Supersedes: なし。[Decision 0067](0067-task-pr-http-api.md)（Approved）の 6・7 で「何を並列の上限とするかを決めてから足す」とした値を決める（0067 は書き換えない）

## 背景

PAW-062 の Task の画面（`/agents`）は、一覧の見出しの右に「並列上限 2 · VRAM 18.2 / 48 GB」を出す（Design canvas の Tasks Board、PR #174）。Web の `TaskSource` は `capacity`（`parallelLimit`、任意の `vramUsedGb` / `vramTotalGb`）を受け、値がないときは何も出さない。[Decision 0067](0067-task-pr-http-api.md) の 6 は、Backend に「Task の並列の上限」に当たる値がないため `capacity` を返さず、何を並列の上限とするかを決めてから足すとした（判断点 7、Approved）。

承認済みの要件と Decision が決めていること:

- 並列数は固定値にしない。Resource Scheduler が VRAM・KV Cache・Context の長さなどを見て動的に決める。Context が長い Task が多ければ Agent 数を減らし、短い Task 中心なら増やす（REQUIREMENTS「Parallel execution」「Dynamic concurrency」）。
- Compute Scheduler は KV Cache の予約で受け付けを決め、`parallelism(deployment, context_tokens, class)` が「この長さの要求をいまあと何件受け付けるか」を返す（PAW-036、Decision 0037）。
- System Health の詳細（VRAM の used / reserved / available を含む）は Owner / Admin（`admin.system_health.view`）、一般の User には全体の Severity と Codex / Claude の可否だけ（Decision 0059 の 3、Approved。UI_DESIGN.md の「General User」）。

**次のことは要件も既存の Decision も決めていない。** 画面の 1 つの数の「並列の上限」を Scheduler のどの値にするか（`parallelism` は Context の長さを与えないと決まらない）。Cloud の Agent を数えるか。Main が受け付けられないときに何を出すか。VRAM を Task の一覧で誰に見せるか。AGENTS.md の「仕様変更」に従い、実装は下の推奨で置き、この Decision で承認を求める。

## 提案

### 1. 並列の上限 = 動いている Coding の Agent ＋ 最も長い Context の Agent をあと何件受け付けるか

`parallel_limit = running + more`。

- `running`: Main Model（Role が `main` の Deployment）の GPU で動いている Coding の Lease の数。
- `more`: Main が **最も長い Context の Coding の要求**をいまあと何件受け付けるか（`ComputeScheduler.parallelism`）。最も長い Context は Main の `max_context_tokens` で、KV Pool の Coding の取り分（`kv_safety` × Coding の Ceiling）がそれより小さければ取り分まで（どちらも Config の値）。
- 動いている Agent も最も長い Context まで伸びるとして数える（いまの予約が短くても、最も長い Context との差を予約に足してから `more` を数える）。短い Agent が 1 件動いただけで、伸びきった 2 件が Pool に入らないのに上限が 2 になる、ということを避ける（Codex の P2、PR #212）。
- 理由: Coding Agent の会話は Task の間に伸び、Main の最も長い Context に近づく（Issue #200 の KV の不足）。「最後まで並べて動かせる Agent の数」は 1 件を最も長い Context として数えた数で、Chat などほかの要求の Context が Pool を埋めるほど下がる（要件の「Context が長い Task が多ければ減らす」）。動いている Agent の数より小さくはならない。
- 読むだけで、何も受け付けず何も予約しない（`coding_capacity()` は `status()` と同じくメモリ上の値だけを読む）。
- 代案 A: 動いている Lease の平均の Context の長さで数える。短い要求が 1 件動いただけで 1 から 14 のように跳ね、会話が伸びると守れない数を出す。
- 代案 B: Main の `max_sequences`（Runtime が同時に動かす要求の数）。KV を見ない固定値で、要件の「固定値にしない」に合わない。
- 代案 C: Task の Worker 数の設定を足す。同じく固定値で、Scheduler の判断と別の数になる。

### 2. 数えるのは Local の GPU の Agent だけ

`running` と `more` は Main Model の GPU のものだけで、Cloud（Codex / Claude）の Agent は数えない。Cloud の並列は Quota と Provider の制限で決まり、Scheduler の KV とは別の数である。Main の Deployment が複数あれば合計する。Compute Scheduler のない構成（`create_app(compute=None)`）と Main のない構成では `capacity` を返さない（`null`。画面は何も出さない）。

### 3. Main が受け付けられないときは `running` だけ（0 もありうる）を出す

Main が GPU にいない、Probe の読み取りが古い、Exclusive（Kaggle / Full GPU Mode）が GPU を持つ・待つ、縮退の 4 段目（新しい Local の要求を止める）以降のときは、`more` が 0 で、上限は `running` だけになる（Main がいなければ 0）。隠さずに出す（「並列上限 0」は、いま Local の Agent が始まらないことの事実）。

- 代案: そのときは `capacity` を出さない。理由のない空白になり、Task が待つ理由が画面から消える。

### 4. VRAM は System Health の詳細を見られる人（Owner / Admin）にだけ出す

`vram_used_bytes` / `vram_total_bytes` は、`admin.system_health.view` を持つ人（Owner / Admin）にだけ返し、他の人には `null`（並列の上限だけ）を返す。判定は Policy だけで行い Audit しない（`tasks.list` で許した読み取りの中の絞り込み。Decision 0070 の通知の Audience と同じ）。Decision 0059 の 3（VRAM は詳細で Owner / Admin）と UI_DESIGN.md（一般の User には可否だけ）に合わせる。並列の上限は、自分の Task が待つ理由を知るための値なので `tasks.list` の全員に出す（数だけで、他の User の Task も Model の名前も出さない）。

- 代案 A: VRAM も全員に出す（Design canvas の見た目どおり）。Decision 0059 の 3 を `Supersedes` する新しい判断が要る。
- 代案 B: VRAM はどこにも出さず、System Health だけで見る。Owner / Admin が Task の画面で GPU の詰まりに気づけない。

### 5. VRAM の値は Probe の最新の読み取りの used / total、新しいときだけ

`vram_used_bytes` は Probe が見た使用量（`VramView.actual`。他の Workload の分も含む、`nvidia-smi` の used）、`vram_total_bytes` は GPU の総量。Scheduler の予約（reserved）は出さない（System Health の詳細にある）。読み取りが古い（`probe_max_age_seconds` を過ぎた）ときは両方 `null` で、画面は並列の上限だけを出す。Web は System Health の画面と同じく GiB を小数 1 桁にして「GB」と表示する（18.2 / 48 GB）。

### 6. Migration と経路

Migration は不要（メモリ上の Scheduler の値だけ）。予約された Revision `0193` は使わない。新しい経路は作らず、`GET /api/v1/tasks` の応答に `capacity`（`parallel_limit`・`running`・`vram_used_bytes`・`vram_total_bytes`）を足す。一覧は Web が 5 秒ごとに読み直すので、値もその周期で新しくなる。

## 決めてほしいこと

1. **並列の上限 = Main の GPU で動いている Coding の Agent の数 ＋ 最も長い Context（Main の `max_context_tokens`、KV の Coding の取り分まで）の Agent をあと何件受け付けるか（動いている Agent も最も長い Context まで伸びるとして数える）**（1）でよいか。推奨: はい。代案 A: 動いている Lease の平均の長さで数える。代案 B: Main の `max_sequences`。代案 C: Worker 数の設定。
2. **Local の GPU の Agent だけを数え、Cloud は数えない。Scheduler / Main がない構成では `capacity` を返さない**（2）でよいか。推奨: はい。
3. **Main が受け付けられないとき（GPU にいない・Probe が古い・Exclusive・縮退の 4 段目以降）は `running` だけ（0 もありうる）を出す**（3）でよいか。推奨: はい。代案: 出さない。
4. **VRAM は `admin.system_health.view`（Owner / Admin）にだけ返し、並列の上限は `tasks.list` の全員に返す。Audit はしない**（4）でよいか。推奨: はい（Decision 0059 の 3 のまま）。代案 A: 全員に VRAM（0059 の 3 を Supersedes）。代案 B: VRAM を出さない。
5. **VRAM は Probe の used / total（予約ではない）、新しい読み取りのときだけ、GiB の小数 1 桁を「GB」と表示**（5）でよいか。推奨: はい。
6. **Migration なし、`GET /api/v1/tasks` の応答に `capacity` を足す**（6）でよいか。推奨: はい。

## 承認後の扱い

承認されたら Status を Approved に改め、承認の内容を記録する。
方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。
[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。

## 承認時の決定（2026-10-08）

Human は、作業 Session で判断が必要な点について推奨つきの説明を受け、直接回答して承認した（すべての判断点を推奨どおり承認（画面の目視確認も OK））。
