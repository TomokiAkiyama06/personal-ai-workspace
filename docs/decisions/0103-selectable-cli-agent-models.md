# Codex / Claude の Model を `runtime.toml` の固定の 1 つでなく、Task ごとに選べるようにする（どこで選ぶか、誰が選べるか、選べる一覧の出どころと検証した組、送る前の検査、Probe の Model、Planner との関係、使用量と Audit の記録、Reasoning の強さ、Migration、UI、PR の分け方）

- Status: Proposed
- Date: 2026-11-08
- Scope: Issue [#341](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/341)（Human の要望: Codex（と Claude）の Model を、IDE の拡張機能のように **いつでも選べる** ようにしたい）。この Decision は設計だけで、コードは承認の後の下の「PR の分け方」の PR で書く。関係する既存の口: `apps/backend/paw_backend/runtime_config.py`（`[agents.*]` の `model`・`session_base`・`relay_command`。`model` は `MODEL_PATTERN` の形だけを確かめる）、`agents/cli_wiring.py`（`CliAgentSetup.model` を `ClaudeCliRuntime` / `CodexCliRuntime` と `ClaudeProbeAdapter` / `CodexProbeAdapter` の組み立てに 1 回だけ渡す）、`agents/claude_cli.py`（`self._model` を `--model` と `SessionRequest`・`CloudGate.session(model=…)` に使う）、`agents/codex_cli.py`（同じく `self._model`）、`agents/codex_app_server.py`（`thread_start(model, …)` の `thread/start` の `model`、`VERIFIED_VERSIONS = {"0.156.1"}`、`model/rerouted` の通知、`tool_check`）、`agents/claude_probe.py`・`agents/codex_probe.py`（Probe の Model）、`agents/cloud.py`（`CloudGate`: `placement.record(CLOUD, agent=…, model=…)`）、`orchestrator/runtime.py`（`NodeAssignment`。今は `agent` だけで Model を持たない）、`orchestrator/plan.py`（`NODE_FIELDS`）、`orchestrator/models.py`（`dag_node_attempts.placement_model`・`agent_plan_calls.placement_model`）、`connections/models.py`（`connection_usage.model`・`connection_probes.model`）、`connections/session.py`（`SessionRequest.model`）、`api/v1/tasks.py`（`CreateTaskRequest`）、`apps/web/src/tasks/CreateTaskForm.tsx`、`apps/backend/deploy/runtime/runtime.example.toml`。
- Supersedes（一部。下の推奨どおりに承認されたとき）:
  - [Decision 0099](0099-claude-planner-and-node-assignment.md)（Approved）の 7 の `[agents.claude]` / `[agents.codex]` の `model` の意味（「その CLI の Runtime が使う唯一の Model」）を、「**DB の Model の Policy がないときの既定で、ただ 1 つの許す Model**」に置き換える（下の 3・9）。Key の名前・形・起動の検査（0093 の 2 の Fail closed）は変えない。DB の Policy がない限り、今とまったく同じ動きになる。
  - 次は書き換えず、その中で作る: 0099 の 4（Plan の `agent` と候補の表。Planner は Model を選ばない: 下の 6）、[Decision 0100](0100-cli-connection-health-probe.md)・[Decision 0102](0102-probe-prompt-wording-and-model-fallback.md)（Probe は「Node が使う既定の Model と Credential の組」を確かめる。下の 5）、[Decision 0101](0101-codex-mcp-resource-tools-exception.md)（Codex の Tool の検査と `VERIFIED_VERSIONS`。下の 3 で「版と Model の組」に広げる）、[Decision 0089](0089-codex-cli-runtime-route.md)（Codex の Token の数え方）、[Decision 0016](0016-shared-connection-adapter-policy.md) の 5〜7（Quota は Connection の種類ごと、使用量の行の Model 名は形だけを確かめる、Audit に Model 名を書かない）、[Decision 0083](0083-agent-runtime-adapters.md)・[Decision 0095](0095-codex-auth-file-owner-and-mode.md)・[Decision 0096](0096-codex-login-collect-after-unit-stop.md)（CLI の Session・Codex の Login の File と枠。Model に依らない）、[Decision 0094](0094-task-create-entry-open-points.md)（Task の作成の入口）。[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。

## 背景

- 今の Model は、`/etc/paw/runtime.toml` の `[agents.codex].model` と `[agents.claude].model` の 1 つずつで決まる。`runtime_config.py` が読んだ値（`CliAgentSetup.model`）を、`cli_wiring.py` が起動の時に `ClaudeCliRuntime` / `CodexCliRuntime` と 2 つの Probe の Adapter へ 1 回だけ渡す。Runtime はその値を、Claude なら `--model`、Codex なら `thread/start` の `model` に使い、同じ値を `SessionRequest.model`（→ `connection_usage.model`）と `CloudGate.session(model=…)`（→ `dag_node_attempts.placement_model` / `agent_plan_calls.placement_model`）にも書く。変えるには root で File を書き換え、Backend を再起動するしかない。
- 本番の最初の Codex の Probe（2026-11-08 15:00）は `internal_error`・Token 0 だった。設定の `gpt-5.5-codex`（`runtime.example.toml` の例の値）は、codex-cli 0.156.1 の Model の一覧（`codex debug models`）にない。Human の判断待ちの暫定として、本番の `[agents.codex].model` を `gpt-6-sol` にした（2026-11-08）。Claude は `claude-opus-5-5`。
- `codex debug models`（0.156.1、Login なし）の一覧: `gpt-6-astra`（最上位）、`gpt-6-sol`（Coding の主力）、`gpt-6-luna`（速く安い）、`gpt-5.6-sol`・`gpt-5.6-terra`・`gpt-5.6-luna`（1 つ前）、`gpt-5.5`（古い）。隠れた物: `gpt-5.4`・`codex-auto-review`・`gpt-daybreak-*`。一覧は CLI の版に付いていて、版が変われば変わる。
- **Model は Codex の Tool の一覧に効く。** Codex は Model の Catalog の `apply_patch_tool_type` などで組み込みの Tool を足す（0089 の背景）。0101 の Tool の検査（Model への Request の `tools` が PAW の Tool と 3 つの例外だけ）は「版」ごとに確かめたもので、別の Model で同じ結果になる保証はない。しかも 0.156.1 の記録用の Server の Smoke（`apps/backend/deploy/cli-session/smoke_codex_recorder.py` の `MODEL`）は `gpt-5.5-codex`、つまり Catalog にない名前で記録しており、Catalog にある Model（`gpt-6-sol` など）で同じ `tools` になるかは確かめていない。今の検査は Session の中でも毎回走る（Fail closed）ので、検査を通らない Model を選ぶと、その Session は何も実行せずに止まる（`invalid_response`）。
- Claude の CLI には `codex debug models` に当たる一覧の命令がない（`--model` は Alias と完全な ID を受ける）。Alias（`opus` など）は CLI の版と Provider の都合で指す先が変わる。
- 要件との関係: REQUIREMENTS.md は Codex / Claude を Adapter 経由で任意の Role に割り当てられるとし、Global Policy は Admin だけが変えられる（[AGENTS.md](../../AGENTS.md) の 1 の 9）。Owner / Admin は User ごとに Quota を置ける。[docs/UI_DESIGN.md](../UI_DESIGN.md) の Chat の見出しには `Agent: Auto` / `Model Router: Auto` の選択があり、Agent / Task の画面の表示候補に `Model` がある。Model の選択の場所・権限・一覧の出どころは要件にもどの Decision にもなく、[AGENTS.md](../../AGENTS.md) の 14 に従い、ここで推奨を置いて承認を求める。

## 提案（全体の形。推奨どおりに承認されたとき）

```text
Admin（Global Policy）
  CLI の種類ごとに「許す Model」と「既定の Model」を DB に置く（再起動なし）
  ただし Repository の「検証した組」（CLI の版 × Model）の中だけ
        │
User（Task の作成）
  許す Model の中から Codex と Claude を 1 つずつ選ぶ（省けば既定）
  → 作成の時に具体的な名前に決めて Task に保存（後の既定の変更で変わらない）
        │
Orchestrator / Runtime
  Task の Model を NodeAssignment で Runtime に渡す（Planner・Node・Retry・Escalation で同じ）
  送る前にもう一度「許す」「検証した組」を確かめ、外れていれば何も送らずに断る
        │
記録
  connection_usage.model・placement_model に実際の名前（今の列のまま）
```

## 判断点（Human に答えてほしいこと）

各点に案と推奨を置く。番号で「1 は A」のように答えてほしい。

### 1. どこで選ぶか

- **A（推奨）: Task の作成の時に、Task ごとに選ぶ。** 作成の Form（今の `CreateTaskForm`。将来の Chat の入口も同じ）で、Codex の Model と Claude の Model を 1 つずつ選ぶ（省けば Admin の既定）。選んだ値は作成の時に **具体的な名前に決めて Task に保存** し、その Task の間（Planner の呼び出し・すべての Node・Retry・Escalation の段）は変えない。走っている Task の Model は変えない。Admin が既定や許す一覧を変えても、作成済みの Task には効かない（ただし許す一覧から外れた Model は 4 のとおり送る前に断る）。
- B: A に加えて、Node ごとに Model を上書きできる（Task の詳細の画面か Plan の中で）。悪い点: Node の割り当ての表（0099 の 4）と Review の独立性（0099 の 5）に Model の軸が増え、検査と画面が大きくなる。今の要望（IDE のように選ぶ）には A で足りる。
- C: User の設定の既定（個人の既定）だけを置き、Task の作成では選ばない。悪い点: Task ごとに変えられない。
- D: Admin の既定だけ（今の `runtime.toml` を DB に移すだけ）。悪い点: User は選べず、要望を満たさない。
- 「User の個人の既定」（C の部分）は、有効な User が 1 人の今の配備（0099 の 2）では Admin の既定と同じになるので、**今回は入れない**（未決）。

### 2. 誰が選べるか・誰が一覧を決めるか

- **A（推奨）: 許す Model の一覧と既定は Admin（Owner を含む）が Global Policy として決め、Task を作れる人（`project.task.run`）はその中から選ぶ。** Policy の変更は既存の `admin.config.manage` と Step-up（Connection の管理と同じ）。新しい Capability は足さない。
- B: Admin だけが Task の Model を選べる（User は既定だけ）。悪い点: 1 人の配備では A と同じで、複数の User になったときに User が選べない。
- C: User ごとに許す一覧を変えられる（User ごとの Policy）。悪い点: Quota（0016 の 5）と重なる仕組みが増える。費用の違いは Quota（Token）で抑えられる。今は入れない。

### 3. 選べる一覧の出どころと、新しい Model を選べるようにする手順

- **A（推奨）: 2 段にする。**
  1. **Repository の「検証した組」**（Code の定数。`VERIFIED_VERSIONS` と同じ考え）: Codex は `(CLI の版, Model)` の組の集合（例: `("0.156.1", "gpt-6-sol")`）。組を足すのは Code の PR で、その版とその Model で 0101 の記録用の Server の Smoke（Model への Request の `tools` の検査）と Probe（0100）が通ったことを PR に残す。Claude は `(Claude Code の版, 完全な Model の ID)` の組で、Alias は受けない（`claude-opus-5-5` の形だけ）。足す条件は Probe と `system/init` の Tool の検査（0100 の 2）が通ること。
  2. **Admin の Policy**（DB の新しい表）: CLI の種類ごとに、検証した組の中から「許す Model」と「既定の Model」（許す物の 1 つ）を選ぶ。再起動は要らない。Admin の画面には、今の CLI の版で検証した Model だけを候補に出す。
  - 新しい Model を選べるようにする手順: Smoke → 検証した組を足す PR（Merge は Human）→ Release → Admin が Policy で許す。
- B: Admin の Policy だけ（形の検査だけで、どの名前でも許す）。悪い点: 背景のとおり、Codex は Model で Tool の一覧が変わりうるので、検証していない Model は Session ごとに止まるか、検査の外の Tool が出る。Claude は Alias の先が黙って変わる。
- C: 起動の時に `codex debug models` を読んで、その一覧をそのまま候補にする。悪い点: 一覧にあることは Tool の検査を通ることを示さない。CLI の版が変わると候補が黙って変わる。Claude には同じ命令がない。
- D: `runtime.toml` に一覧を書く。悪い点: 変えるのに root と再起動が要り、要望（いつでも）に合わない。
- 隠れた Model（`gpt-5.4`・`codex-auto-review`・`gpt-daybreak-*`）は検証した組に入れない（推奨）。

### 4. 送る前の検査（知らない Model を送らない）

- **A（推奨）: 3 か所で確かめ、どこでも外れたら何も送らない。**
  1. **Task の作成**: 選んだ Model が今の Policy で許されていなければ 422（`model_not_allowed`）。Task を作らない。
  2. **Session の開始**（`CloudGate` の前、Placement の記録の前）: Task の Model が今の Policy で許され、かつ今の CLI の版と検証した組にあること。Codex は `initialize` の `userAgent` で版を読んだ後、`thread/start` の前に確かめる（今の版の検査と同じ場所）。外れたら送らず、その試行は新しい失敗の理由 `model_not_allowed` で終える（Retry は同じ結果になるので同じ段では繰り返さない。Ladder の次の段への Escalation は 0021 の 3 のまま）。
  3. **Probe**: 5 のとおり既定の Model で、同じ検査を通らなければ送らない。
  - **黙って既定の Model に置き換えない**（User が選んだ Model と違う Model で動くことを、知らせずに起こさない）。
- B: 2 で外れたら、その時の既定の Model に置き換えて続け、Log に残す。悪い点: Task の記録と実際の Model がずれ、Review の独立性などの判断の前提も変わる。
- 0016 の 6 の「使用量の行の Model 名は形だけを確かめ、一覧と照合しない」は変えない（照合は送る前の Admission の層で行い、記録の層は今のまま）。
- Codex の `model/rerouted`（Provider が別の Model で答えた）と Claude の Fallback（0102）は、今のまま Log の 1 行だけにする（Node の側の扱いは 0102 の未決のまま）。

### 5. Probe はどの Model で確かめるか

- **A（推奨）: 既定の Model だけ（今と同じ 1 回）。** Probe が確かめるのは Credential と既定の組で、Connection の状態（`connected` など）は Credential の状態である（0100 の 1・5）。既定を変えたときは、Admin の Policy の保存の後に自動で 1 回 Probe を走らせる（attach と replace の後と同じ扱い。0100 の 6 に trigger `policy` を足す。抑えは 0100 の 8 のまま）。
- B: 許す Model ごとに Probe を走らせる。悪い点: 費用と時間が Model の数だけ増え、1 つの Model の失敗で Connection の状態をどう書くかが決まらない。
- C: A に加えて、Admin が Model を許す一覧に足すときに、その Model で 1 回だけの確認の Probe を走らせ、結果を見せる（状態は変えない）。良い点: 許す前に通ることが分かる。悪い点: 状態を変えない Probe の種類が増える。**今回は入れず未決**（3 の A の検証した組で、足す前に Smoke を通しているため）。

### 6. Planner（0099）が Node に Model まで割り当てるか

- **A（推奨）: 割り当てない。** Plan の Node の Field は今のまま（`agent` まで。`model` は `NODE_FIELDS` にないので未知の Field として Plan ごと拒否される）。Node の Model は、その Node の Agent の Task の Model（1 の A）で決まる。理由: Planner の出力は Prompt Injection の影響を受けうる（0099 の 6）ので、費用と Tool の一覧に効く値を Planner に決めさせない。
- B: Planner が許す一覧の中から Node ごとに Model を提案でき、Backend が許す一覧の外を拒否する。悪い点: 1 の B と同じく軸が増える。Planner の判断の質を測ってから別の Decision で決める。
- **Planner の呼び出し自身の Model**: Planner は Claude の CLI なので、Task の Claude の Model を使う（推奨）。代わりの案: Planner だけはいつも Admin の既定。推奨しない理由: Task の中で Claude の Model が 2 つになり、記録と費用の説明が分かれる。

### 7. 使用量・上限・Audit での Model の記録

- **A（推奨）:**
  - 呼び出しの記録は今の列のまま: `connection_usage.model`（`SessionRequest.model`）、`dag_node_attempts.placement_model`・`agent_plan_calls.placement_model`（`CloudGate`）、`connection_probes.model`。今の Runtime が持つ `self._model` を、Task の Model（`NodeAssignment` に足す）に置き換えるだけで、実際に使った Model が入る。Schema の変更は要らない。
  - Task の選択は Task に保存し（9 の Migration）、Task の詳細（0098 の読み取りだけの API と画面）に出す。
  - Quota は今のまま Connection の種類ごと（0016 の 5）。Model ごとの Quota は置かない（費用の違いは Token の Quota に出る）。
  - Audit: Admin の Policy の変更は新しい Action `admin.agent_models.update`（`admin.config.manage` の Authorizer の行と同じ Transaction、Fail closed）。今の規則（0016 の 7・0100 の 9）に合わせ、Audit には Model 名を書かず、ID と Enum だけにする（どの名前にしたかは Policy の表の `updated_by`・`updated_at` と、表の変更の履歴の行に残す）。Task の作成の Audit も Model 名を書かない。
  - 使用量の画面（#187）の「Model 別」の集計（UI_DESIGN の Admin の表示候補）は、この Decision では足さない（0016 の 6 の「Report に Model 名を出さない」との関係を、別の Decision で決める。未決）。
- B: Audit の `details` に Model 名を書く。悪い点: 0016 の 7・0100 の 9 の規則を変えることになる（Model 名は Secret ではないが、規則を 1 つにしておく）。

### 8. Reasoning の強さ（Codex の reasoning の段・Claude の effort）を選べるようにするか

- **A（推奨）: 今回は入れない。** CLI の既定の強さのまま。理由: Codex の Model ごとに受ける段が違い（Catalog の値）、`turn/start` の `effort` の扱いと Token（`reasoningOutputTokens`、0089）への影響を版ごとに確かめる必要がある。Claude の effort の Flag の有無も CLI の版で確かめる必要がある。Model の選択が動いた後に、検証した組を `(版, Model, 強さ)` に広げる別の Decision で決める。Task の保存の形（9）は、後で強さの列を足せる形にしておく。
- B: 今回 Model と一緒に入れる（許す一覧を Model と強さの組にする）。悪い点: 検証の組が増え、最初の PR が大きくなる。

### 9. Migration と `runtime.toml`

- **A（推奨）:**
  - 新しい表 `agent_model_policies`（CLI の種類・Model・既定かどうか・`updated_by`・`updated_at`。種類ごとに既定はちょうど 1 つ）と、変更の履歴の表（追記だけ）。Revision は Issue の番号 `0341`（`paw_compatibility = "expand"`）。
  - Task に `codex_model`・`claude_model`（NULL 可。その CLI の種類がその配備で無効なら NULL）を足すか、Task と種類ごとの行の小さな表にする（後で強さを足しやすい後者を推奨）。既存の Task は NULL のままで、NULL の Task は Runtime の組み立ての Model（`runtime.toml` の値）で動く（今と同じ）。
  - `runtime.toml` の `[agents.*].model` は残し、「DB の Policy がないときの既定で、ただ 1 つの許す Model」にする（冒頭の Supersedes）。DB の Policy の Model が今の CLI の版の検証した組にないとき、その Model は許されない物として扱い（4）、起動は止めない（Policy は DB の値で、起動の Fail closed は File の検査のまま）。ただし `runtime.toml` の `model` 自身が検証した組にないときの扱いは、10 の 2 で決める。
  - `runtime.example.toml` の Codex の例の値を `gpt-5.5-codex`（Catalog にない）から `gpt-6-sol` に直す（PR の分け方の 1）。
- B: `runtime.toml` の `model` を消し、DB の Policy だけにする。悪い点: 0093 の手順と本番の File の変更が要り、Policy を書くまで CLI が動かない。

### 10. 本番の暫定の値と、起動の検査

1. 本番の暫定の既定 `[agents.codex].model = "gpt-6-sol"`（2026-11-08）をこのまま既定にするか。**推奨: はい**（Coding の主力の Model。最上位の `gpt-6-astra` は許す一覧に入れて Task ごとに選べるようにする）。ただし 3 の A の検証した組（0.156.1 × `gpt-6-sol` の Tool の検査の Smoke と Probe）を通すまでは「検証した組」に入れない。
2. `runtime.toml` の `model` が検証した組にないときの起動: **A（推奨）: 起動は止めず、その種類の CLI の段は送らない（Probe も送らない）。起動の Log の 1 行に `model: unverified` を出す。** B: 起動を止める（0093 の 2 の Fail closed）。悪い点: 検証した組を足す PR の前に CLI の版を上げると Backend 全体が起動しない。

### 11. UI（PAW-060 の Design canvas に沿う）

- **A（推奨）:** Task の作成の Form に、有効な CLI の種類ごとに Model の選択（既定を選んだ状態。許す Model だけ。名前と、Admin が付けた短い説明）を置く。Task の詳細に選んだ Model を出す。Admin の Connection の画面に、種類ごとの許す Model と既定の編集（Step-up）を置く。見た目は [docs/UI_DESIGN.md](../UI_DESIGN.md) と PAW-060 の Design canvas に従い、UI の PR は Human の目視の確認の後に Merge する。Chat の見出しの `Model Router: Auto`（UI_DESIGN の 4）の場所の選択は、Chat の入口を作る時に同じ API で足す。
- B: Admin の画面だけを先に作り、Task の作成の選択は後にする。悪い点: 要望の中心（User が選ぶ）が後になる。

## Test の方針（実装の PR）

- 検証した組: 定数に `(0.156.1, gpt-6-sol)` などが入り、隠れた Model と Alias が入らないこと。Policy の保存は検証した組の外を 422 で拒否し、種類ごとの既定がちょうど 1 つであること。`admin.config.manage` と Step-up がない保存は拒否され、Audit が同じ Transaction で書けなければ保存しないこと。
- Task の作成: 許す Model は保存され、許さない Model は 422（`model_not_allowed`）で Task を作らないこと。省いたときは作成の時の既定の名前が保存されること。既定を後で変えても作成済みの Task の Model は変わらないこと。
- Runtime: Fake の CLI で、Claude の `--model` と Codex の `thread/start` の `model` に Task の Model が渡り、`connection_usage.model`・`placement_model` に同じ名前が入ること。Policy から外した Model の Task の Session は、何も送らず（Fake の CLI が起動しない、または `thread/start` の前に止まる）`model_not_allowed` で終わること。Codex は検証した組にない版では今のまま版の理由で止まること。Planner・Retry・Escalation で同じ Model を使うこと。
- Probe: 既定の Model で走り、Policy の既定の変更の後に 1 回走ること。
- Plan: Node の `model` の Field は今のまま Plan ごと拒否されること（6 の A）。
- Migration の Test（上げ下げと ORM との一致）。既存の Test は弱めない。

## PR の分け方

1. この Decision と `runtime.example.toml` の Codex の例の値の修正（`gpt-5.5-codex` → `gpt-6-sol`）と README の表。Refs #341。
2. Backend: 検証した組の定数と送る前の検査（4 の 2）、Policy の表と Migration `0341`、Admin の API（`GET`/`PUT /admin/agent-models`）と Audit、Probe の既定の解決（5）。Policy がない間は今と同じ動き。Refs #341。
3. Backend: Task の Model の保存と作成の API（4 の 1）、`NodeAssignment` に Model を足して Runtime・`CloudGate`・`SessionRequest` へ渡す、Task の詳細の API。Refs #341。
4. Web: Task の作成の選択・Task の詳細の表示・Admin の Policy の画面（Human の目視の確認の後に Merge）。Refs #341。
5. 本番での確かめ: 0.156.1 × 選びたい Model ごとの Smoke（Tool の検査）と Probe、検証した組を足す小さな PR（Model ごと）。Human が Release を入れ、Admin の画面で許す。

## 未決（この Decision では決めない）

- User の個人の既定（1 の C）、Node ごとの上書き（1 の B）、Planner による Model の提案（6 の B）。
- Reasoning の強さ（8）。
- 許す前の確認の Probe（5 の C）。
- 使用量の画面の Model 別の集計と、0016 の 6 との関係（7）。
- 走っている Task の Model の途中の変更、失敗した Task を別の Model でやり直す操作。
- Codex の `model/rerouted` と Claude の Fallback を Node の側でどう扱うか（0102 の未決のまま）。

## Human に確かめてほしいこと（Decision の外の前提）

1. 本番の Codex の Probe を `gpt-6-sol` で走らせ直し、`connected` になるか（#306 の 6 の Probe）。`internal_error` のままなら、Tool の検査（Model で `tools` が変わる）か Login の問題かを journal で分けたい。
   あわせて、記録用の Server の Smoke を `gpt-6-sol` で取り直す（今の記録は Catalog にない `gpt-5.5-codex` で取ったもの。背景）。Catalog の Model で `apply_patch` などの組み込みの Tool が足されるなら、Node の Session は 0101 の検査で止まり、Model の選択より先にその扱いの Decision が要る。
2. 許す一覧に最初に入れたい Model（推奨: Codex は `gpt-6-sol`（既定）・`gpt-6-astra`・`gpt-6-luna`、Claude は `claude-opus-5-5`（既定）だけ）。それぞれ 3 の A の Smoke が要る。

## リスク

- 検証した組を足すたびに Smoke と PR が要り、「いつでも」の範囲は「検証した組の中でいつでも」になる。CLI の版を上げるときは、許している Model ごとに組を確かめ直す必要がある。
- 費用の高い Model（`gpt-6-astra` など）を選ぶと Token が増える。Quota（Token）で抑えるが、Model ごとの単価の違いは今の Quota に表れない。
- 4 の A では、Admin が Policy から Model を外すと、その Model の作成済みの Task の CLI の段は送れなくなる（黙って置き換えない代わりに、失敗として見える）。
- Codex の Provider が別の Model で答える（`model/rerouted`）と、記録の Model 名（選んだ名前）と実際が違うことがある。今は Log の 1 行だけで分かる。

## 承認後の扱い

承認されたら、Status を Approved とし、承認の内容を Approval の行と末尾の「承認時の決定」に記録する。0099 の 7 の本文は書き換えず、この Decision が `model` の意味を Supersede することを README の表に記す。
実装は「PR の分け方」の 2〜5 の PR で行う（すべて「Refs #341」）。本番の Release と Policy の設定は Human / Admin が行う。
方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。
[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。
