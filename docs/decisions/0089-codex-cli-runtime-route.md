# Codex CLI の Runtime の経路（`codex exec` の代わりに `codex app-server`、Turn ごとの Token の読み方、組み込みの Tool の止め方と確かめ方、版の固定）

- Status: Proposed
- Date: 2026-10-14
- Scope: Issue [#208](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/208) の PR 4c（[Decision 0083](0083-agent-runtime-adapters.md) の 11 の 4 のうち `CodexCliRuntime`）の前提。この Decision は設計だけで、コードは承認の後の PR で書く。関係する既存の口: `apps/backend/paw_backend/agents/claude_cli.py`・`claude_stream.py`・`cli_session.py`・`bridge.py`（Claude の経路。#235 で Merge 済み）、`deploy/cli-session/`（#236 で作業中）、`deploy/egress/`。
- Supersedes（一部、承認された場合）:
  - [Decision 0087](0087-cli-secret-store-and-egress.md)（Approved）の「CLI の公式の情報」の Codex の節と 4 の「Codex の組み込みの Tool」の段落のうち、**`codex exec` を前提にした Option**（`--sandbox read-only`・`--ignore-user-config`・`--ignore-rules`・`--ephemeral`、JSONL の Item の検出）を、下の 1〜3 の `codex app-server` の形で置き換える。0087 の 4 の「Codex は Model に渡す Tool の一覧全体が PAW の MCP の Tool だけであることを Smoke で確かめた版だけを有効にする（Fail closed）」「Stream の検出を二重の守りにする」「呼び出しごとの使用量を途中で得られる経路を Smoke で確かめるまで有効にしない」は**変えず**、その確かめ方を下で具体化する。
  - 次は変えない: 0083 の 1・5・6・8（`CloudGate`、途中の計上と残りだけの精算、Connection ごとに 1 Session の DB Lease と Compare-and-swap の書き戻し、Timeout と取り消し）、0087 の 4 の表の Codex の ChatGPT の Login の行（Session ごとの `CODEX_HOME` の `auth.json`・`cli_auth_credentials_store = "file"`・`forced_login_method = "chatgpt"`）と 0087 の 5 の 4（ChatGPT の Login は Smoke の後の別の PR）、0088。

## 背景

0087 は 2026-10-12 の公式の Document を元に、Codex を `codex exec --json` で動かす前提で、未確認の 3 点を「PR 4b で確かめるまで有効にしない」とした: (1) 呼び出しごとの Token を Session の途中で得る経路、(2) `apply_patch` を含む組み込みの Tool の止め方と Model に渡る Tool の一覧の確かめ方、(3) Codex 自身の通信の `HTTPS_PROXY` への従い方と ChatGPT の Login の宛先。

PR 4c を始めるにあたり、[AGENTS.md](../../AGENTS.md) の 10 のとおり、2026-10-14 に最新の安定版 **Codex CLI `rust-v0.162.1`（2026-10-09 公開）** の Source と公式の Document を確かめた（末尾の「出典」。Repository の `docs/` の `exec.md`・`config.md`・`authentication.md` は今は公式 Site への案内だけなので、細部は Tag の Source で確かめた）。結果、**`codex exec --json` では 0083 の 5 と 6 の条件を満たせない**ことが分かった。経路の選び直しは 0087 が決めていない重要な判断なので、[AGENTS.md](../../AGENTS.md) の「仕様変更」に従い、選択肢と推奨を下に置き、承認を求める。

## Codex CLI の公式の情報（2026-10-14、`rust-v0.162.1` で確認）

### Turn ごとの Token

- `codex exec --json` の Event は `thread.started`・`turn.started`・`turn.completed`（`usage`）・`turn.failed`・`item.started` / `item.updated` / `item.completed`・`error` だけ（`exec/src/exec_events.rs`）。`usage` は `input_tokens`・`cached_input_tokens`・`cache_write_input_tokens`・`output_tokens`・`reasoning_output_tokens`。
- `exec` は内部で受けた `thread/tokenUsage/updated` を **保存するだけで出さず**（`event_processor_with_jsonl_output.rs` の `ServerNotification::ThreadTokenUsageUpdated` → `last_total_token_usage = Some(...)`）、`turn.completed` に Thread の**累計**（`usage.total.*`、`usage_from_last_total`）を載せる。1 つの Prompt（その中の MCP の Tool の Loop 全部）の終わりまで何も分からない。**0083 の 5 (b)・(d) の条件（Turn ごとに報告しない CLI・版は有効にしない）を満たさない。**
- `codex app-server`（stdio の JSON-RPC。`codex app-server --listen stdio://` が既定）は、Model の呼び出しごとに通知 **`thread/tokenUsage/updated`**（安定の API、`app-server-protocol/src/protocol/common.rs`）を送る。中身は `{threadId, turnId, tokenUsage: {total, last, modelContextWindow}}`。`last` は直近の 1 回の応答、`total` は Thread の累計で、どちらも `totalTokens`・`inputTokens`・`cachedInputTokens`・`cacheWriteInputTokens`・`outputTokens`・`reasoningOutputTokens`（`v2/thread.rs` の `ThreadTokenUsage` / `TokenUsageBreakdown`）。送るのは Core の `TokenCount` の Event（`core/src/session/mod.rs` の `send_token_count_event`）で、Rate limit の更新と一緒にも送られる（同じ値が 2 回来うる）。
- `codex mcp-server` はこの版にはない（`cli/src/main.rs` の Subcommand）。

### 組み込みの Tool

- `--enable` / `--disable <feature>` は `-c features.<name>=true|false` と同じ（`cli/src/main.rs`）。**既定で有効な Feature が 0087 の時より大幅に多い**（`features/src/lib.rs`。Tool に関わるもの: `shell_tool`・`unified_exec`・`view_image`・`sleep_tool`・`multi_agent`・`apps`・`plugins`・`tool_suggest`・`image_generation`・`browser_use`・`computer_use`・`in_app_browser`・`skill_search`・`skill_mcp_dependency_install`・`hooks`・`goals`・`guardian_approval`・`tool_call_mcp_elicitation` など）。`web_search` は Top-level の `web_search = "disabled"`（既定 `cached`）、計画の Tool は `tools.update_plan.enabled`（既定で無効）、`tools.experimental_request_user_input.enabled`（既定で有効）。
- **`apply_patch` を止める設定は今もない。** Tool は Model の Catalog の `apply_patch_tool_type` があるときに足される（`core/src/tools/spec_plan.rs`: `if advertise_environment_tools && context.model_info.apply_patch_tool_type.is_some()`）。`include_apply_patch_tool` の Key はもうない。
- ただし `advertise_environment_tools`（Shell・`apply_patch`・`view_image`・`request_permissions` の Tool の条件）は「その Turn に実行の環境があるか」で決まり、**`codex app-server` の `thread/start` の `environments` を空にすると「環境へのアクセスを無効にする」**（`v2/thread.rs`: "Empty disables environment access for turns that do not provide a turn override"）。この Field は **実験的な API**（`#[experimental("thread/start.environments")]`。`initialize` の `capabilities.experimentalApi = true` で使える）。`codex exec` からは指定できない。
- **Model に渡る Tool の一覧を出す公式の口はない**（`codex debug prompt-input` は入力の Item だけ、`codex debug models` は Catalog）。
- `exec` も `app-server` も非対話では `approval_policy = never`。そのとき MCP の Tool の呼び出しは、Tool の `approval_mode = "approve"` か、Sandbox が無効・書き込み可か、Tool の Annotation が承認を要さない（`readOnlyHint` など）ときだけ通り、ほかは **「MCP tool call requires approval, but approval policy is never」で拒否される**（`core/src/mcp_tool_call.rs`）。MCP の Server ごとに `default_tools_approval_mode`・`enabled_tools`・`required`・`tools.<tool>.approval_mode` を設定できる（`config/src/mcp_types.rs`）。

### 認証と Network

- `auth.json`（`$CODEX_HOME` の下）は `tokens{id_token, access_token, refresh_token, account_id}`・`last_refresh` など。Access Token の JWT の `exp` の 5 分前か、`exp` がなければ `last_refresh` から 8 日で Refresh し、401 では Disk から読み直してから Refresh する。Refresh した Token は `auth.json` に書き戻す。Refresh Token は使い捨て（`refresh_token_reused` の Error）。宛先は `https://auth.openai.com/oauth/token`（`login/src/auth/manager.rs`）。0083 の 6・0087 の 4 の「Connection ごとに 1 Session と Compare-and-swap の書き戻し」の前提は今も正しい。
- Codex 自身の HTTP Client は `HTTPS_PROXY` / `ALL_PROXY` / `HTTP_PROXY` / `NO_PROXY` を読む（`http-client/src/outbound_proxy.rs`）。Source に見える宛先は `chatgpt.com`（`/backend-api/codex/responses` など）・`auth.openai.com`・`ab.chatgpt.com`（Statsig の Metrics。`otel.metrics_exporter = "none"` で止まる）。**公式の宛先の一覧は今もない**（0087 の 4 の表のとおり Smoke の拒否の Log から決める）。
- JSONL / 通知の Error は文だけで、分類の Code は付かない（Context の上限・Usage の上限・Rate limit・Refresh の失敗は文で見分ける。`protocol/src/error.rs`）。

## 提案

### 1. Codex は `codex app-server`（stdio）で動かす

- **推奨:** 1 つの Session を、Session ごとの Container の中の `codex app-server`（stdio）1 つと、Backend の Adapter が話す JSON-RPC で動かす: `initialize`（`experimentalApi: true`）→ `thread/start`（`ephemeral: true`、`approvalPolicy: "never"`、`sandbox: "read-only"`、`environments: []`、`baseInstructions` / `developerInstructions` に PAW の System Prompt、`config` に下の 2 の設定）→ `turn/start`（Task の文）→ 通知を読む → `turn/completed` か Bridge の終わりか停止で終える。`CODEX_HOME` は Session ごとの Directory（0087 の 4 の表）で、`config.toml` は Backend が書いた Session の設定だけ。
- 代替: (a) `codex exec --json` のまま: 上のとおり Turn ごとの Token が出ず、`apply_patch` も止められないので、0083 / 0087 により**有効にできない**（Codex の CLI の段は使わない）。(b) 0087 の 4 の候補 (b) の API Key と Backend の中の Proxy での読み取り: Human は Subscription を選んだ（0087 の承認時の決定）ので、今は採らない。

### 2. 組み込みの Tool を止める設定と、Model に渡る Tool の確かめ方

- **推奨（止める）:** `environments: []`（Shell・`unified_exec`・`apply_patch`・`view_image`・`request_permissions` を出さない）に加え、Tool に関わる既定で有効な Feature をすべて `false`（上の一覧。版を上げるたびに `features/src/lib.rs` と見比べて足す）、`web_search = "disabled"`、`tools.update_plan.enabled = false`、`tools.experimental_request_user_input.enabled = false`、`analytics.enabled = false`、`otel` の 3 つの Exporter を `none`、`history.persistence = "none"`、`check_for_update_on_startup = false`。MCP の Server は `paw` だけ（`required = true`、`enabled_tools` を Bridge の固定の一覧に、`default_tools_approval_mode = "approve"`。`approval_policy = never` でも PAW の Tool が拒否されないように。PAW の Tool の承認は Tool Broker が 1 回ずつ行う: 0083 の 6・7）。
- **推奨（確かめる）:** Model に渡る Tool の一覧は、**Smoke で Provider に送らずに取る**: Smoke 用の Session の設定で `model_providers.<id>.base_url` を Host の上の記録だけをする Local の Server に向け、`codex app-server` が送った最初の Request の `tools` を保存して、PAW の MCP の Tool（`mcp__paw__<name>` の形。Bridge の固定の一覧と `submit`）と過不足なく一致することを確かめる。一致した版だけを、Adapter の中の**確かめた版の一覧**（Code の定数。初めは空）に入れる。Adapter は Session の始めに `initialize` の `userAgent` から版を読み、一覧にない版では**何も送らずに止める**（`invalid_response`）。
- **推奨（二重の守り）:** Session の間、`item/started` / `item/completed` の Item が `paw` の Server の MCP の Tool 呼び出しと Agent の Message・Reasoning 以外（Command の実行・ファイルの変更・協調の Tool・Web 検索・画像の生成など）なら、その場で CLI を止めて Session を失敗にする（0087 の 4 のとおり `escalate=True` の側）。
- 代替: (a) Model の Catalog を Session ごとに差し替えて `apply_patch_tool_type` を消す（`model_catalog_json`）: 公式の Document に書かれていない Source からの推測で、Catalog の他の値も固定することになる。推奨しない。(b) 確かめる版の一覧を持たず、毎回の Stream の検出だけに頼る: 0087 の 4 の Fail closed に反する。推奨しない。

### 3. Turn ごとの Token の読み方（0083 の 5 の `on_usage`）

- **推奨:** `thread/tokenUsage/updated` の **`total` の増えた分**を、その Thread の 1 回の報告として `on_usage(入力の増分, 出力の増分)` で渡す（入力 = `inputTokens`、出力 = `outputTokens`。`cachedInputTokens` / `cacheWriteInputTokens` / `reasoningOutputTokens` は `inputTokens` / `outputTokens` に含まれる内訳として足さない。含まれることを Smoke で `rawResponse/completed` の `usage` と照らして確かめ、含まれなければこの版は有効にしない）。`total` の差を使うのは、同じ値の通知が Rate limit の更新と一緒に 2 回来ても 2 回数えないため（増えていなければ報告しない。`total` が減ったら `invalid_response` で止める）。`last` は照合にだけ使う。Session の終わりの合計は最後の `total`。通知が 1 つもないまま `turn/completed` になり Token を使っていたら、0083 の 5 (d) のとおり `invalid_response`（Claude と同じ）。
- 代替: `last` をそのまま足す: 同じ通知の重複を見分ける手がかり（ID）がないので、数え過ぎうる。推奨しない。

### 4. 進め方

1. この Decision の承認の後、PR 4c で `codex_app_server.py`（JSON-RPC の読み書きと 2・3 の確認）・`codex_cli.py`（`CodexSessionAdapter` / `CodexCliRuntime`。Claude の経路と同じ `CloudGate`・Bridge・`SubprocessLauncher`）を書き、**Fake の `codex app-server`**（この版の通知の形を固定した Script）で Test する。確かめた版の一覧は空のままで、組み立てでも有効にしない。
2. Connection ごとに 1 Session の DB Lease と `auth.json` の書き戻し（0083 の 6）、Egress の Allowlist の Codex の宛先は、0087 の 5 の 4 のとおり Smoke の後の ChatGPT の Login の PR。
3. Smoke（Human の規約の確認の後）: 2 の Tool の一覧の取り方、3 の数え方の照合、`HTTPS_PROXY` と拒否の Log からの Allowlist。通った版を一覧に入れる PR。

## 決めてほしいこと

1. **Codex の CLI の段は `codex exec` ではなく `codex app-server`（stdio の JSON-RPC）で動かす。`thread/start` の実験的な `environments: []` を使う**（1。0087 の Codex の `exec` の Option を置き換える）でよいか。推奨: はい。実験的な API なので版ごとに Smoke でやり直す。いいえなら、`apply_patch` と Turn ごとの Token の条件を満たせないので Codex の CLI の段は作らない（Claude の CLI の段と Local だけ）。
2. **組み込みの Tool は 2 の設定で止め、Model に渡る Tool の一覧は Smoke で Local の記録用の Server に向けて取り、確かめた版の一覧（初めは空）にない版では何も送らない。Session の間は Item の検出を二重の守りにする。PAW の Server だけ `default_tools_approval_mode = "approve"`**（2）でよいか。推奨: はい。
3. **Turn ごとの Token は `thread/tokenUsage/updated` の `total` の増分で報告し、内訳が `inputTokens` / `outputTokens` に含まれることを Smoke で確かめる**（3）でよいか。推奨: はい。
4. **4 の順で進める（Runtime のコードは Fake で作って組み立てでは無効、Lease・書き戻し・Allowlist は Smoke の後の ChatGPT の Login の PR）**（4）でよいか。推奨: はい。

## Human に確かめてほしいこと（Decision の外の前提）

- **Provider の規約**（0083・0087 から変わらず）: Codex の ChatGPT の Login を、Backend が複数の User のために Headless で（`codex app-server` を自前の Client から使う形で）共有すること。Smoke の前に。
- 実験的な API に依ることの受け入れ（版を Image の Digest で固定し、上げるときは Fake と Smoke をやり直す）。

## リスク

- `environments` は実験的な API で、名前・意味が予告なく変わりうる。確かめた版の一覧と Smoke で守る。
- 既定で有効な Feature が版ごとに増える（0087 の時より多い）。止める一覧の漏れは、2 の Request の `tools` の確認と Item の検出で見つける。
- `app-server` は IDE 向けの口で、`exec` より変更が多い。版を上げる手間が Claude より大きい。
- Token の内訳の含み方を取り違えると、計上が多すぎ・少なすぎになる。3 の Smoke の照合で確かめる。

## 承認後の扱い

承認されたら `Approval` に記録し、Status を Approved に改める。
方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。
[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。

## 出典（2026-10-14 に確認）

- Release: [GitHub の Releases（API）](https://api.github.com/repos/openai/codex/releases?per_page=8)（最新の安定版 `rust-v0.162.1`、2026-10-09）
- Source（Tag `rust-v0.162.1`）: [`codex-rs/exec/src/exec_events.rs`](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/exec/src/exec_events.rs)、[`codex-rs/exec/src/event_processor_with_jsonl_output.rs`](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/exec/src/event_processor_with_jsonl_output.rs)、[`codex-rs/exec/src/lib.rs`](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/exec/src/lib.rs)、[`codex-rs/app-server-protocol/src/protocol/common.rs`](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/app-server-protocol/src/protocol/common.rs)、[`codex-rs/app-server-protocol/src/protocol/v2/thread.rs`](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/app-server-protocol/src/protocol/v2/thread.rs)、[`codex-rs/app-server-protocol/src/protocol/v1.rs`](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/app-server-protocol/src/protocol/v1.rs)、[`codex-rs/core/src/tools/spec_plan.rs`](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/core/src/tools/spec_plan.rs)、[`codex-rs/core/src/mcp_tool_call.rs`](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/core/src/mcp_tool_call.rs)、[`codex-rs/features/src/lib.rs`](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/features/src/lib.rs)、[`codex-rs/config/src/mcp_types.rs`](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/config/src/mcp_types.rs)、[`codex-rs/login/src/auth/manager.rs`](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/login/src/auth/manager.rs)、[`codex-rs/http-client/src/outbound_proxy.rs`](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/http-client/src/outbound_proxy.rs)、[`codex-rs/cli/src/main.rs`](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/cli/src/main.rs)
- Document（`developers.openai.com/codex/...` は `learn.chatgpt.com/docs/...` へ転送される）: [Non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)、[Authentication](https://learn.chatgpt.com/docs/auth)、[Configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
