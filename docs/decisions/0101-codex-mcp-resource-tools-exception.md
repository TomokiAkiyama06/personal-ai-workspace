# Codex CLI の Tool の検査で、MCP の Resource の 3 つの Tool（`list_mcp_resources`・`list_mcp_resource_templates`・`read_mcp_resource`）だけを例外として許す（許す Tool の集合、`paw` だけが MCP の Server であること、検査の仕方、版の確かめ方と上げ方、Session の中の呼び出しの扱い）

- Status: Proposed
- Approval: 未（Human の承認を待つ。例外として許す方針（#307 の報告の案 2）は Human が 2026-10-29 に選んだ。この Decision はその詳細と、方針が決めていない点の推奨）
- Date: 2026-10-29
- Scope: Issue [#305](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/305)（[#297](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/297) の 6 の CLI の Smoke で見つかった）。この Decision は設計だけで、コードは承認の後の PR で書く。関係する既存の口: `apps/backend/paw_backend/agents/codex_app_server.py`（`VERIFIED_VERSIONS`・`PASSING_ITEMS`・`mcpToolCall` の検出）、`agents/codex_cli.py`（`session_config`・`DISABLED_FEATURES`）、`deploy/cli-session/paw_mcp_relay.py`（`initialize`・`ping`・`tools/list`・`tools/call` だけに答え、他は `Method not found`）、`deploy/cli-session/Containerfile.codex`、PR [#307](https://github.com/TomokiAkiyama06/personal-ai-workspace/pull/307)（`omit_tools_from` と `[orchestrator.skills]`）。
- Supersedes（一部）:
  - [Decision 0089](0089-codex-cli-runtime-route.md)（Approved）の 2 の「推奨（確かめる）」のうち **「PAW の MCP の Tool … と過不足なく一致することを確かめる。一致した版だけを … 確かめた版の一覧に入れる」** を、下の 1 の「許す集合」との一致に置き換える。0089 の 2 の他の部分（止める設定、記録用の Server での取り方、確かめた版の一覧にない版は何も送らない、Item の検出を二重の守りにする）は **変えず**、下の 3・4 で具体化する。
  - 次は書き換えず、その中で作る: [Decision 0083](0083-agent-runtime-adapters.md)（Runtime の Adapter・Tool Broker）、[Decision 0087](0087-cli-secret-store-and-egress.md) の 4（Fail closed）、[Decision 0096](0096-codex-login-collect-after-unit-stop.md)（`auth.json` の回収）、[Decision 0099](0099-claude-planner-and-node-assignment.md) の 5（Codex の Review は必須。Human の回答 2026-10-29: 必須のまま）。[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。

## 背景

#297 の 6 の Smoke（2026-10-29）と #305 の調べ（証拠: `/data/results/paw-smoke-2026-10-29/STATUS.md`・`codex-305/STATUS.md`。どちらも `127.0.0.1` の記録用の Server と Dummy の `auth.json` で、OpenAI には何も送っていない）で、次が分かった。

- `codex-cli 0.156.1` は、main の設定では Model に `list_mcp_resources`・`list_mcp_resource_templates`・`read_mcp_resource`・`skills`（namespace）・`tool_search` を渡し、PAW の `submit` は `tool_search` の後ろ（遅延の読み込み）に隠れていた。
- PR #307 の 2 つの設定（`mcp_servers.paw` の `omit_tools_from = ["deferred", "code_mode"]` と `[orchestrator.skills] enabled = false`）で、0.156.1 が渡す Tool は **`list_mcp_resources`・`list_mcp_resource_templates`・`read_mcp_resource`・`mcp__paw__submit`** になった（Run 06）。`submit` は Node の結果まで通り、Turn の計上と送る前の Placement の記録も動いた。
- 残る 3 つは、MCP の Server が 1 つでもあれば `add_mcp_resource_tools`（`core/src/tools/spec_plan.rs`、Tag `rust-v0.156.1`・`rust-v0.165.0`）が足す。止める Feature・`[tools]` の Key・Server ごとの Key はない。`ToolPolicy.allowed_tools` は Extension（Guardian）だけが設定でき、config からは設定できない。古い版（0.80.0・0.100.0）はむしろ無条件に足していた。**PAW が MCP の Server である限り、設定でも版でも消せない。**
- 最新の安定版 `0.165.0` は、#307 の設定でも上の 3 つに加えて **`apply_patch`** を渡す（`environments: []` の条件がなくなった）。
- 3 つの Tool の呼び出しは、`codex app-server` の Item では `mcpToolCall`（`server` は引数の `server`、省くと `"codex"`。`tool` は Tool の名前）になる（`core/src/tools/handlers/mcp_resource/*.rs`）。宛先は設定した MCP の Server だけで、PAW の Relay は `resources` の Capability を出さず、`resources/*` には `Method not found` を返す。今の Item の検出は `server` が `paw` で `tool` が PAW の Tool のときだけ通すので、**今は 3 つのどれかを Model が呼ぶと Session は止まる**（`invalid_response`）。

0089 の 2 の「過不足なく一致」のままでは、Codex の CLI の段を有効にできず、0099 の 5 で必須の Codex の Review のため、書き込む Task はすべて失敗する。Human は 2026-10-29 に、この 3 つだけを例外として許す方針を選んだ。これは 0089 の 2 の承認された規則を変えるので、[AGENTS.md](../../AGENTS.md) の 14 に従い、この Decision で詳細と推奨を示して承認を求める。

## 提案

### 1. 許す Tool の集合

- **推奨:** Model への Request の `tools` から作る「Model が呼べる名前」（`namespace` の Tool は `<namespace>__<name>`、MCP の Server の namespace は `mcp__<server>`。他の Tool は `name`、`name` がなければ `type`。`tool_search`・`web_search` などの Hosted の Tool もここで名前になる）が、次をすべて満たすときだけ通す。
  1. PAW の Tool（Session の `enabled_tools` を `mcp__paw__<name>` にした集合）を **すべて、ちょうど 1 回ずつ** 含む。
  2. それ以外の名前は **`list_mcp_resources`・`list_mcp_resource_templates`・`read_mcp_resource` の 3 つのどれか** だけ（下の「Human の判断点」2 のとおり、3 つのうち一部だけでも通す）。同じ名前の重複は落とす。
  3. 3 つの Tool は **Top-level の `function`**（namespace の中ではない）で、引数の形（`parameters` の `properties` の Key と `required`）が下の「記録した形」と同じ（下の「Human の判断点」3）。
  4. Session の設定の MCP の Server が **`paw` だけ**（下の 2）。
- 記録した形（`rust-v0.156.1` の Run 06）: `list_mcp_resources` = `{cursor, server}`・必須なし、`list_mcp_resource_templates` = `{cursor, server}`・必須なし、`read_mcp_resource` = `{server, uri}`・必須 `[server, uri]`。説明の文は比べない（版ごとに文が変わっても意味は Item の検出で守る）。
- **それ以外の余分な Tool はすべて不合格**。例: `apply_patch`（0.165.0）、`tool_search`、`skills` の namespace、`web_search`、`exec_command`、`view_image`、PAW 以外の `mcp__<server>__*`、名前は同じでも namespace の中に置かれた 3 つ。PAW の Tool が 1 つでも欠ける（遅延の読み込みで隠れる場合を含む）のも不合格。

### 2. 「`paw` だけが MCP の Server」の守り方

- **推奨:** 3 つの Tool の宛先は設定した MCP の Server なので、例外は次の 3 つが揃うときだけ成り立つ。
  1. **設定:** `session_config` が書く `config.toml` の `mcp_servers` は `paw` だけ（今のまま）。`CODEX_HOME` は Session ごとの Directory で、User の `~/.codex/config.toml` は読まれない（0089 の 1）。Test で `mcp_servers` の Key がちょうど `["paw"]` であることを確かめる（今の Test のまま残す）。
  2. **組み込みの Server を止める:** `apps`（`codex_apps` の Server）・`plugins`・`remote_plugin`・`skill_mcp_dependency_install` などの Feature を `false`（今の `DISABLED_FEATURES`）。版を上げるときは 4 の Feature の照合で、MCP の Server を足しうる新しい Feature を足す。
  3. **Smoke で確かめる:** 記録用の Server の Request に `mcp__<paw 以外>` の namespace がないこと（1 の 2 で落ちる）。加えて `tool_search` の説明の「You have access to tools from the following sources」のような Server の一覧が出る版では、そこに `paw` 以外がないこと（`tool_search` 自体が不合格なので、出たら 1 で落ちる）。
- Server が 2 つ以上になる設定の変更（PAW 以外の MCP の Server を足すこと）は、この例外の外で、新しい Decision が要る。

### 3. Session の中の呼び出しの扱い（二重の守り）

- 今の Item の検出（0089 の 2）は `mcpToolCall` を `server == "paw"` かつ `tool` が PAW の Tool のときだけ通す。3 つの Tool の呼び出しも `mcpToolCall` で来る。
- **推奨（A）:** `mcpToolCall` で `server == "paw"`（引数で `paw` を名指し）かつ `tool` が 3 つのどれかなら **通す**。Relay は `resources/*` に `Method not found` を返すので、Model には Error が返るだけで、PAW の中のものは何も読めない（Tool Broker も通らない）。`server` が `paw` 以外（引数を省いた `"codex"` を含む）なら今のとおり止める（`invalid_response`、`escalate=True`）。`resources/*` を Relay が受けたことは Relay の 1 行の Log に残す（内容は書かない）。
- 代替（B）: 今のまま、3 つの Tool のどれかが呼ばれたら Session を止める。Request に並ぶことだけを許し、呼び出しは許さない。Fail closed で最も狭いが、Model が 1 回呼ぶだけで必須の Codex の Review が失敗し、書き込む Task が `failed` になる。
- どちらでも、Relay と Bridge は `resources` の Capability を出さないまま（PAW は Resource を作らない）。将来 PAW が Resource を出すなら新しい Decision が要る。

### 4. 版の確かめ方（確かめた版の一覧に入れる手順）

- **推奨:** 版ごとに次をすべて通した版だけを `codex_app_server.VERIFIED_VERSIONS` に入れる PR を作る（0089 の 3 と同じく「通った版を一覧に入れる PR」。1 つの版で 1 つの PR）。
  1. **Feature の一覧の照合:** その版の `codex features list`（Scratch の `CODEX_HOME`）と Tag の `features/src/lib.rs` を `DISABLED_FEATURES`（今は `rust-v0.162.1` の一覧）と比べ、既定で有効な Tool・外への通信・MCP の Server を足す Feature を足す（足した Feature の名前がその版にないと起動で失敗しうるので、Smoke で確かめる）。
  2. **Fake のやり直し:** Fake の `codex app-server` の通知の形（`tests/` の Fake）がその版の `app-server-protocol` と合うことを確かめ、Item の一覧（`PASSING_ITEMS` と止める Item）を見直す。
  3. **記録用の Server の Smoke:** `127.0.0.1` の記録用の Provider と Dummy の `auth.json` で、本物の `CodexCliRuntime` → `CodexSessionAdapter` → Bridge → Relay を動かし、**すべての Request** の `tools` が 1 の許す集合に入り、`submit` が Node の結果まで通ることを確かめる。Run の JSON と `STATUS.md` を `/data/results/` に残し、PR に Path を書く。
  4. 続けて 0089 の 3 の Token の内訳の照合と、Egress の Allowlist（0087 の 4）。これは一覧に入れる PR の条件ではなく、CLI の段を本番で有効にする前の条件（今のまま）。
- 比べる処理（1 の許す集合）は、Repository の中の関数（例: `codex_app_server.tool_check(tools, paw_tools)`）にして単体の Test を付け、Smoke の Script はそれを呼ぶ（下の「Human の判断点」4）。

### 5. 版を上げるとき

- 確かめた版の一覧にない版は、今のとおり何も送らずに止まる（`invalid_response`）。Image の版を上げるだけでは何も通らない。
- 上げるときは 4 の 1〜3 をその版でやり直し、通ったらその版を一覧に足す PR と、`Containerfile.codex` の `CODEX_VERSION` と Image の Digest（`/etc/paw/cli-session.toml` の `codex_image`）を変える Human の手順を出す。通らない版（例: `apply_patch` を渡す 0.165.0）は一覧に入れず、固定した版のままにする。
- 古い版は、新しい版が一覧に入り Image が切り替わった後の PR で一覧から外す（同時に 2 つの版を残すのは切り替えの間だけ）。
- この例外は **3 つの名前と記録した形** に限る。版が 3 つの Tool の名前・置き場所（namespace）・引数の形を変えたら、その版は不合格で、例外を広げるなら新しい Decision が要る。

## Human の判断点（それぞれ推奨つき）

1. **Session の中で 3 つの Tool が呼ばれたとき**（3）: (A) `server == "paw"` を名指した呼び出しは通し、Relay の `Method not found` を Model に返す。`server` を省いた・`paw` 以外は止める。(B) 今のまま、どの呼び出しでも止める。**推奨: A。** 宛先は PAW の Relay だけで、Relay は Resource を持たず何も読めない。B では Model の 1 回の呼び出しで必須の Codex の Review が失敗する。
2. **3 つが Request に揃っていなくてもよいか**（1 の 2）: (A) 3 つのうち 0〜3 つを許す（ない版はより狭いので通す）。(B) 3 つがちょうど揃っていることを求める。**推奨: A。** 少ない方は安全側で、B は版の小さな変更で不要に落ちる。
3. **3 つの Tool の形も比べるか**（1 の 3）: (A) 名前に加え、Top-level の `function` であることと、`parameters` の `properties` の Key と `required` を記録した形と比べる（説明の文は比べない）。(B) 名前だけ比べる。**推奨: A。** 同じ名前で意味の違う Tool（例: 宛先を引数で広げる）を見落とさない。説明の文は版ごとに変わるので比べない。
4. **検査の処理と Smoke の Script をどこに置くか**（4）: (A) 比べる処理を `codex_app_server.py` の関数にして単体の Test を付け、記録用の Server の Smoke の Script を `apps/backend/deploy/cli-session/` の下に入れる（Host で人か Agent が動かし、結果は `/data/results/`）。(B) 今のまま `/data/results/` の Script だけ。**推奨: A。** 版を上げるたびに同じ検査を同じ形で繰り返せ、検査の規則そのものが Review される。
5. **固定する版**（5）: (A) 4 を通れば `0.156.1` を確かめた版にし、Image をその版で Build して Digest で固定する（`0.162.1` より古いが、`0.165.0` は `apply_patch` で落ちる）。(B) 0.162.1〜0.164.x を先に調べ、通る中で最新の版を選ぶ。**推奨: A を今、B を後続。** 0.156.1 は #305 の Smoke で 1 の集合を満たした唯一の版で、CLI の段を早く有効にできる。より新しい版は 5 の手順で後から上げる。

## Test の方針

- 承認の後の実装の PR（「Refs #297」「Refs #305」）:
  - `tool_check` の単体の Test: PAW の Tool だけ（通る）、PAW と 3 つ（通る）、3 つのうち一部（推奨 A なら通る）、`apply_patch` が足された（落ちる）、`tool_search` と PAW が隠れた（落ちる）、`skills` の namespace（落ちる）、`mcp__other__x`（落ちる）、3 つの名前が namespace の中（落ちる）、`read_mcp_resource` の `required` が違う（落ちる）、`mcp_servers` が `paw` 以外も含む（落ちる）。
  - Item の検出の Test（推奨 A のとき）: `mcpToolCall` の `server = "paw"`・`tool = "list_mcp_resources"` は通る、`server = "codex"` は止める、`server = "other"` は止める、PAW の Tool でも 3 つでもない `tool` は止める。
  - Relay の Test: `resources/list`・`resources/templates/list`・`resources/read` は `Method not found` で、Socket には何も送らない。
- 版を一覧に入れる PR: 4 の 1〜3 の結果（Feature の照合の差分、Fake の Test、Smoke の JSON の Path と `tool_check` の結果）。

## PR の分け方

1. この Decision（docs だけ）。
2. 承認の後: `tool_check` と Item の検出の変更（1〜3）、Smoke の Script の置き場所（判断点 4）。`VERIFIED_VERSIONS` は空のまま。PR #307 の設定の上に作る。
3. 2 の後: `0.156.1` の Feature の照合・Fake のやり直し・記録用の Server の Smoke、`VERIFIED_VERSIONS` への追加、`Containerfile.codex` の `CODEX_VERSION` の記録。Image の Build と Digest の固定は Human の手順。

## 未決（この Decision では決めない）

- PAW の Tool を MCP ではなく `thread/start` の `dynamicTools` で渡す形（3 つの Tool が出なくなる）。設計の変更が大きいので、必要になれば新しい Decision。
- 0.162.1〜0.165.x のどの版まで上げるか（5 の手順で版ごとに）。

## リスク

- 3 つの Tool が Model の注意を引き、PAW の Tool の代わりに呼ばれる。推奨 A なら Error が返って続き、B なら Session が止まる。Smoke と本番の Log で頻度を見る。
- 将来の版が 3 つの Tool の宛先や意味を名前を変えずに広げる。1 の 3 の形の比較と、3 の `server` の検査で見つける。
- 設定の誤りで MCP の Server が増えると、3 つの Tool の宛先が増える。2 の Test と Smoke、Item の検出（`paw` 以外は止める）で守る。
- 記録用の Server の Provider は本番の Provider（ChatGPT の Login）と同じ既定の Capability（`namespace_tools` など）だが、本番の Request は見られない。0089 の 2 のとおり、Item の検出を本番の二重の守りにする。

## 承認後の扱い

承認されたら `Approval` に記録し、Status を Approved に改める。Supersede する 0089 の本文は書き換えず、この Decision が 0089 の 2 の一部を Supersede することを README の表に記す。
方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。
[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。
