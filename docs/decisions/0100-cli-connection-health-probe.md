# CLI の Connection（Claude・Codex）の Health check を、本物の短い Probe の Session にする（固定の Prompt と受け入れる答え、Timeout、記録と Audit、`CloudPolicy` との関係、同時実行、状態の変化の影響、Admin の「再確認」の Route、繰り返しの抑え、Test、PR の分け方）

- Status: Approved
- Approval: 2026-10-29、Humanが作業Session内で、判断点ごとの説明（推奨つき）を受けたうえで「両方推奨で」と回答して承認（PR #308 の [comment](https://github.com/TomokiAkiyama06/personal-ai-workspace/pull/308#issuecomment-6487666748) に記録。approved as recommended, all 7 points: 1 は A（Probe の記録は新しい `connection_probes`。User の Quota と `connection_usage` には入れない）、2 は A（成功の `result` で答えが `PAW_PROBE_OK` を含むときだけ `connected`）、3 は A（Admin が無効にした Connection は Probe しない。409 `connection_disabled`）、4 は A（0099 の「有効な User がちょうど 1 人」は当てず、`[cloud].enabled` と `[agents.*]` と Connection の `enabled` だけを条件にする）、5 は A（再確認の Route は 202 の非同期で、結果は `last_probe` で読む）、6 は A（成功の後 60 秒、続けて失敗するたびに倍、上限 15 分。成功か差し替えで戻す。自動の Probe は抑えない）、7 は A（Codex の枠が 60 秒で空かなければ `busy` で、状態を変えない）。「Human に確かめてほしいこと」の 3 つの前提（Provider の規約、Probe ごとに数百 Token、step 4 の推奨の順番）も同じ日に問題なしと確認（PR #308 の comment）。方針の案 A は Human が同じ日に Issue #306 で選んだもの。Codex review（f4a4aab）は指摘なし。末尾の「承認時の決定」）
- Date: 2026-10-29（承認 2026-10-29）
- Scope: Issue [#306](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/306)（PR #303 の Codex review（0ad6a7f、P1）で見つかった。`runtime.toml` の step 4（CLI の段）を有効にする前に必須）。この Decision は設計だけで、コードは承認の後の下の「PR の分け方」の PR で書く。関係する既存の口: `apps/backend/paw_backend/connections/adapter.py`（`ConnectionAdapter.check_health`・`AdapterRegistry`。「No adapter ships in this package」）、`connections/service.py`（`check_health`・`_probe`・`_store_health`・`connect`・`replace_credential`・`execute_session` の Session の枠）、`connections/store.py`（`record_health`）、`connections/wiring.py`（HTTP 側の `ConnectionService`。空の `AdapterRegistry()` と `NoSecretStore`）、`connections/limits.py`（`DEFAULT_HEALTH_TIMEOUT_SECONDS = 30`）、`connections/session_slot.py`、`api/v1/connections.py`（`/admin/connections` の Route・`_require_step_up`・`_audit_refusal`）、`agents/claude_cli.py`（`ClaudeSessionAdapter`）、`agents/claude_stream.py`（`ClaudeStream.check_init`）、`agents/cli_session.py`（`SystemdLauncher`）、PR #303 の `agents/cli_wiring.py`（Orchestrator 側の `ConnectionService`。これも空の `AdapterRegistry()`）、`orchestrator/connection_reaper.py`
- Refines / Supersedes（一部）:
  - [Decision 0016](0016-shared-connection-adapter-policy.md)（Approved）の 8 の最後の点「Health Check を動かす頻度 … は Orchestrator / 通知の Issue」のうち **頻度** を、下の 6（attach と replace の直後に 1 回と、Admin の「再確認」。定期の Loop はしない）で決める。通知の規則は決めない（未決）。0016 の 8 の状態の意味（新しい・差し替えた Connection は確認まで `unavailable`、Health Check が `connected` にする、`expired`、`enabled` は Health Check が変えない、User には利用可 / 不可だけ）は変えない。
  - 0016 の 7 と `ConnectionService` の Docstring の「`check_health` は Backend 内部で Actor なし」に、**Actor のある Admin の再確認**（`admin.config.manage`・Step-up・Audit）を足す（下の 7）。Actor のない `check_health(kind)` は残す。
  - [Decision 0099](0099-claude-planner-and-node-assignment.md)（Approved）の 1 の「外への送信はすべて `CloudGate` を通る」に、**Task の内容を何も含まない Probe** という例外を 1 つ足す（下の 4）。Probe は Assignment を持たないので `CloudGate` を通らないが、`[cloud].enabled` と「送る前に記録し、記録できなければ送らない」（0048 の 4・0099 の 2）は同じく守る。
  - 次は書き換えず、その中で作る: [Decision 0083](0083-agent-runtime-adapters.md) の 6（CLI の Container・Egress・Session の枠・Refresh の書き戻し）、[Decision 0087](0087-cli-secret-store-and-egress.md)（Secret Store と CLI の Flag）、[Decision 0089](0089-codex-cli-runtime-route.md)（Codex の確かめた版）、[Decision 0091](0091-egress-per-session-socket.md)（Session ごとの Egress）、[Decision 0093](0093-production-runtime-config.md)（`runtime.toml`。この Decision は Key を足さない）、[Decision 0096](0096-codex-login-collect-after-unit-stop.md)（Codex の枠の結び付けと回収）。[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。

## 背景

Issue #306 に書かれたとおり、今の Backend では Claude と Codex の Connection が `connected` にならない。

- Health の Adapter（`ConnectionAdapter.check_health`）がどこにもない。HTTP 側（`connections/wiring.py`）と Orchestrator 側（PR #303 の `agents/cli_wiring.py`）の 2 つの `ConnectionService` は、どちらも空の `AdapterRegistry()` で組み立てられ、`_probe` は Adapter がなければ `unavailable` を返す。
- 本番で `check_health` を呼ぶ所がない（HTTP の Route も Loop も CLI の Command もない）。
- 0016 の 8 で、新しく付けた・差し替えた Connection は確認まで `unavailable` で、Admission は `connected` 以外を `connection_unavailable` で拒否する。
- そのため、登録した Claude の Connection はいつまでも使えず、Claude の Planner は毎回 Local に戻り（0099 の 3）、Claude と Codex の必須の Review は Gate で断られ、書き込む Task はすべて `failed` になる（0099 の 5）。この穴は #303 より前からある。

Human は 2026-10-29 に Issue #306 で **案 A** を選んだ:

1. `check_health` は CLI の Session を 1 回だけ、Node の Session と **同じ Container と Egress の経路** で走らせる。Prompt は Backend の固定の物で、Tool はなく、User のデータは送らない。
2. 答えが返れば `connected`、認証エラーなら `expired`、それ以外は `unavailable`。
3. HTTP 側と Orchestrator 側の **両方** の `ConnectionService` に登録する。
4. いつ走らせるか: attach と replace の直後に自動で 1 回と、Admin の「再確認」の Route（Actor・Step-up・Audit つき）から。定期の Loop は今はしない。
5. Claude を先に作る。Codex は #305 で確かめた版ができてから足し、単一の Session の枠と `auth.json` の書き戻し（0083 の 6、0096）を使う。

これは 0016 の 8 が残した「Health check をいつ走らせるか」を決め、Provider への固定の Probe と Admin の Route を新しく足すので、[AGENTS.md](../../AGENTS.md) の 14 に従い、この Decision で詳細と、案 A が決めていない点の推奨を示して承認を求める。

## 提案

### 1. 全体の流れ

```text
Admin: POST /admin/connections/{kind}（attach）・PUT …/credential（replace）── 成功（commit 済み）
   └→ 自動の Probe を 1 回、背景で始める（trigger = attach / replace）
Admin: POST /admin/connections/{kind}/health-check（再確認）── Step-up・抑え（下の 8）
   └→ Probe を 1 回、背景で始める（trigger = manual）。202 を返す
Probe:
  前提の確認（Adapter がある・[cloud].enabled・Connection が enabled・同じ Credential の Probe が走っていない）
  → 記録（connection_probes の行と Audit connection.probe を 1 つの Transaction で。できなければ送らない）
  → Credential を Secret Store から解決 → CLI の Session（固定の Prompt、Tool なし、同じ Container と Egress）
  → 判定（connected / expired / unavailable）→ 行を閉じる
  → 状態を保存（Probe の開始の後に別の判定が記録されていなければ。下の 5）→ 変化があれば connection.status の Audit
```

- Probe は **Task にも User にも属さない**（Task の内容も User のデータも送らない）。Quota・Task の Budget・Queue の Lease の検査はしない（下の 3）。
- Probe の結果は状態（`shared_connections.status`・`checked_at`）だけを変える。Admin の `enabled` は変えない（0016 の 8）。

### 2. 固定の Prompt・受け入れる答え・Timeout（Claude）

- **推奨（起動）:** Node の Session と同じ `SystemdLauncher`（`paw-cli-session@<uuid>.service`、Image の `/opt/paw/start-claude`、Container の中の Egress の転送 `127.0.0.1:3128` → Session ごとの Egress の Socket: 0091）で、Session の Directory を `create_session` で作る（Worktree・Repository を Mount しない。Bridge の Socket を作らない）。Credential は Node と同じく `CLAUDE_CODE_OAUTH_TOKEN` で Launcher に渡し、引数・Log に出さない（0087 の 4）。環境変数（`CLAUDE_CONFIG_DIR` は Session の空の Directory、`DISABLE_TELEMETRY` など）は `ClaudeSessionAdapter.environment` と同じ。
- **推奨（Flag）:** `ClaudeSessionAdapter.arguments` から MCP の部分を除いた形:
  `-p --output-format stream-json --verbose --include-partial-messages --tools "" --strict-mcp-config --mcp-config <空の {"mcpServers": {}}> --permission-mode dontAsk --no-session-persistence --setting-sources user --settings <Session の空の設定> --max-turns 1 --model <[agents.claude].model> --system-prompt <固定の System Prompt> [--restricted]`。
  `--allowedTools` は渡さない。Flag の名前と振る舞い（特に `--max-turns`）は、0083 の 6 と同じく実装の PR で公式の情報と Fake の CLI・Smoke で確かめる。
- **推奨（固定の Prompt）:** Backend の定数で、設定・User・Agent から変えられない。
  - System Prompt: `You are a connectivity check for a backend service. Do not use any tools. Reply with exactly the single line PAW_PROBE_OK and nothing else.`
  - 標準入力（User の Turn）: `Reply with exactly: PAW_PROBE_OK`
  - どちらにも Task・Project・User・Repository・時刻・Host 名・Connection の ID を入れない。
- **推奨（受け入れる答え）:** 次をすべて満たすときだけ `connected`。
  1. `system/init` の Tool が **空**（`EndConversation` もない。MCP の Tool がなければ CLI は付けない）で、MCP の Server が **ない**（`ClaudeStream` に Probe 用の期待値「Tool なし・Server なし」を足す。今の Node 用の「PAW の Tool と `paw` の Server」の検査は変えない）。違えば `unavailable`（`invalid_response`。Log に `unsupported` の印）。
  2. Model の呼び出し（`message_start` … `message_stop`）が **ちょうど 1 回** 報告される。2 回以上なら CLI を止めて `unavailable`（`invalid_response`）。
  3. `result` が `subtype = success`・`is_error = false`。
  4. `result` の答えの文が `PAW_PROBE_OK` を部分文字列として含む（大文字・小文字を区別。前後の空白・句読点・改行は問わない）。答えの文は判定にだけ使い、保存・Log・応答に出さない（今の `ClaudeStream` と同じく会話の中身は残さない。判定の後すぐ捨てる）。含まなければ `unavailable`（`invalid_response`）。下の「Human の判断点」2。
- **推奨（`expired`）:** `system/api_retry` の `error` が `CREDENTIAL_ERRORS`（`authentication_failed`・`oauth_org_not_allowed`・`account_on_hold`）のどれかなら、その時点で CLI を止めて `expired`（今の `ClaudeSessionAdapter` と同じ分類）。`rate_limit`（Provider の Rate Limit・Subscription の Window）は **`unavailable`** だが、下の 5 のとおり `connected` だった Connection を落とさないよう「状態を変えない」結果として扱う。
- **推奨（それ以外）:** Timeout・CLI の異常終了・Container が起動しない・Egress が通らない・Stream の形の誤りは `unavailable`。
- **推奨（Timeout）:** Probe 全体の期限を **120 秒**（新しい定数 `PROBE_TIMEOUT_SECONDS`。Container の起動・CLI の初期化・1 回の短い Model の呼び出し）にし、`ConnectionService(health_timeout_seconds=...)` の Probe の Adapter の側で使う（今の既定 30 秒は Container の起動を含むには短い。上限 `MAX_HEALTH_TIMEOUT_SECONDS = 600` の中）。期限を過ぎたら CLI を止め（今の SIGTERM・5 秒・SIGKILL）、`unavailable`（`timeout`）。Unit の停止にかかる時間は期限の外で、止めの終わりを待ってから行を閉じる（`stop_through_cancellation` と同じ）。止められなかった Unit は今の `unstopped` と同じく Directory を残して Log に出す。`runtime.toml` の Key にはしない（0093 の Schema を変えない）。

### 3. 使用量と記録: 新しい `connection_probes`（`connection_usage` には入れない）

- **推奨:** Probe の 1 回ごとに新しい Table `connection_probes`（Migration。番号は実装の PR で Issue の番号から）の行を 1 つ作る。列:
  `id`・`connection_id`（`shared_connections` の ID）・`kind`・`secret_handle`（どの Credential を確かめたか。Handle は 0016 の 1 の DB に置いてよい形。Audit・応答には出さない）・`credential_version`（Secret Store の版。Codex の書き戻しの後でも、どの版を確かめたかが分かる）・`trigger`（`attach` / `replace` / `manual`）・`actor_id`（その Probe を起こした Admin。自動の Probe は attach / replace をした Admin）・`model`・`state`（`running` / `finished` / `abandoned`）・`outcome`（`connected` / `expired` / `unavailable` / `busy` / `cancelled`。`running` の間は NULL）・`failure_code`（今の `FailureCode` の閉じた集合）・`input_tokens` / `output_tokens`（CLI が報告した値。分からなければ NULL で、推定はしない）・`started_at` / `finished_at`（DB の時計）・`duration_ms`。**Prompt・答え・Credential の値を持つ列はない。** DELETE は誰にも与えない（`connection_usage` と同じ長期保持）。
  - `UNIQUE (connection_id, secret_handle) WHERE state = 'running'` の部分 Index で、同じ Credential の Probe は同時に 1 つだけ（下の 8 の `probe_running`）。
- **`connection_usage` に入れない理由:** `connection_usage` の行は Task と User に帰属する呼び出しで（`task_id`・`user_id`・`project_id` は NOT NULL、0016 の 6）、User ごとの Quota（`requests` / `tasks` / `tokens` / `runtime_seconds`）を数える元である。Probe は配備の保守の送信で、どの User の Quota にも数えない。下の「Human の判断点」1。
- **Quota と Budget:** Probe は Admission（`ConnectionStore.admit`）・Quota・Task の Budget・Session の最低の枠（50,000）・Queue の Lease を **通らない**。使う量は 1 回で数百 Token の見込みで、下の 8 の抑えで回数が限られる。Provider 側の Subscription の Window には数えられる（Human に確かめてほしいこと）。
- **Audit:** 下の 9。`orchestrator.cloud_send`（0048 の 3・0099 の 3）は **使わない**: それは Task の内容を外へ出したことの記録（`resource_kind` `task`、内容の指紋）で、Probe は Task を持たず内容は固定の文だけである。
- **Placement:** Probe は DAG の Node でも Planner の呼び出しでもないので、`agent_dag_node_attempts` と `agent_plan_calls` の Placement の列には書かない。外へ出たことは `connection_probes` の行と `connection.probe` の Audit で分かる。

### 4. `CloudPolicy` と 0099 の「有効な User が 1 人」との関係

- **推奨:** Probe は `CloudPolicy.allows(assignment)`（Assignment の Agent・Role・Project・User の数を見る）を **呼ばない**。Assignment がなく、Project の内容も User のデータも送らないので、0099 の 2 の 2〜4（Ladder・有効な User が 1 人・Project）は当てはまらない。
- 代わりに次をすべて満たすときだけ Probe を走らせる（どれかが分からなければ走らせない）:
  1. `[cloud].enabled = true`（全体の Switch。0099 の 2 の 1。false の配備は外へ何も送らない）。
  2. `[agents.claude]`（Codex は `[agents.codex]`）が書かれている（Model・Session の Directory の置き場所が決まっている）。**Ladder がその CLI を名指ししているかは問わない**: Admin が Ladder に `claude` を入れる前に Connection を確かめられるようにする（PR #303 の `build_cli_services` は Ladder が名指さない `[agents.*]` を許す）。
  3. Connection が `enabled`（Admin が無効にした Connection は Kill Switch なので、Probe も送らない。0099 の 2 の止め方）。下の「Human の判断点」3。
- 満たさないとき: 自動の Probe は行を作らず走らない（Log に固定の 1 行。状態は `unavailable` のまま）。再確認の Route は 409（下の 7）。
- 1 と 2 は Probe の Adapter の登録の条件にする（満たさなければ Adapter を登録しない）。3 は Probe の開始の Transaction の中で確かめる。
- **有効な User の数は見ない**（下の「Human の判断点」4）。2 人目の User が有効になった配備でも、Probe は User のデータを送らないので、Connection の状態は正しく保てる（Node の送信は 0099 の 2 の 3 のとおり Gate で断られる）。

### 5. 状態の書き方と、走っている Session への影響

- **推奨（どの判定が残るか）:** 今の `record_health` は「Connection がまだ確かめた Handle を指していれば」書く（差し替えの後の古い判定を捨てる）。Probe ではさらに、**Probe の開始の後に別の判定が記録されていないこと**（`shared_connections.checked_at IS NULL OR checked_at < connection_probes.started_at`。どちらも DB の時計）を同じ Statement の条件にする。そうでなければ書かず、行の `outcome` だけを残す。理由: Probe が認証を通った後で、走っている Session が `expired`（0016 の 8）を記録したとき、遅れて終わった Probe の `connected` で上書きしない（新しい判定が勝つ。保守的に `expired` を残し、Admin が再確認する）。
- **推奨（状態を変えない結果）:** `busy`（Codex の枠が空かない: 下の 10）・`cancelled`（Backend の停止）・`rate_limited` の `unavailable` は、Credential について何も分からないので **状態を変えない**（`checked_at` も変えない）。行の `outcome` / `failure_code` にだけ残す。それ以外の `unavailable`・`expired`・`connected` は状態に書く。
- **走っている Session への影響:** 状態は Admission の時にだけ見る（0016 の 3・8）。そのため:
  - `connected` → `expired` / `unavailable`: **走っている Session は止めない**（Admin の無効化と同じく終わるまで走る）。新しい Session は Admission で `connection_unavailable` になり、Orchestrator は今の規則で扱う（Planner は 2 回目を Local: 0099 の 3。Claude の Reviewer は Gate で断られ、0083 の 4 の `escalate` で次の段がなければ Node が失敗し、書き込む Task は 0099 の 5 で `failed`）。
  - `unavailable` / `expired` → `connected`: 次の Admission から通る。待っている Task を起こす仕組みはない（`waiting` の Task は今の規則のまま）。
  - Session 自身が `EXPIRED` で終われば、今までどおり `expired` にする（Probe の結果を待たない）。
- **同時実行（Claude）:** Claude の Session Adapter は `exclusive_sessions` を持たない（`setup-token` は長く使え、Session が Credential を書き換えない）ので、**Probe は走っている Session と並んで走ってよい**。Session の枠は取らない。

### 6. いつ走らせるか

- **推奨（自動）:** `POST /admin/connections/{kind}`（attach）と `PUT /admin/connections/{kind}/credential`（replace）が成功した（Commit した）後、Route が `ConnectionService.start_probe(principal, kind, trigger)` を呼び、Probe を背景の Task で始める。応答（201 / 200）は今までどおり `unavailable` の状態を返し、Probe を待たない。自動の Probe は Admin の Step-up の後の操作の続きなので、新しい Step-up は求めない。下の 8 の抑えも当てない（新しい Credential の最初の確認）。
- **推奨（手動）:** 下の 7 の「再確認」の Route。
- **推奨（その他）:** `enable`（Admin の再有効化）・Backend の起動・Session の `EXPIRED` の後には **自動で走らせない**（Human の回答: 定期の Loop は今はしない）。Admin は再確認を使う。Codex の回収の Loop（0096）が Login を回収した後の確認も、今は再確認で行う。
- **背景の Task:** Probe は HTTP の要求の外で、Backend の Process の中の Task として走る（Process ごとの集合に持ち、Shutdown で取り消す。取り消された Probe は CLI を止めてから `cancelled` で閉じる）。Process が落ちて閉じられなかった `running` の行は、今の `connection_reaper`（0016 の 9）が **10 分**（Probe の期限 120 秒 + Unit の停止の最悪 約 200 秒より十分長い）を過ぎたものを `abandoned` にする（状態は変えない）。

### 7. Admin の「再確認」の Route と RBAC

- **推奨（Route）:** `POST /admin/connections/{kind}/health-check`（Body なし）。
  - 認可: `admin.config.manage`（Owner / Admin。今の `/admin/connections` の Route と同じ `_MANAGER`）。Service の `start_probe` が同じ Capability で **もう一度** 認可し、Authorizer の行を書く（0016 の 7 と同じ二重の確認）。新しい Capability は足さない。
  - **Step-up:** 今の `_require_step_up`（Passkey の Step-up、0069 の 5）を求める。拒否は `_audit_refusal` で `connection.probe` の Deny（`step_up_required` / `step_up_method_insufficient`）。
  - 成功: **202 Accepted**、`{"probe": {"id", "trigger": "manual", "state": "running", "started_at"}}`。Probe の終わりは待たない（120 秒は HTTP の要求には長い。下の「Human の判断点」5）。
  - 失敗: 404 `not_found`（Connection がない）、409 `connection_disabled`（Admin が無効にしている）、409 `probe_not_configured`（4 の 1・2 を満たさない。Adapter が登録されていない）、409 `probe_running`（同じ Credential の Probe が走っている）、429 `probe_cooldown`（下の 8。`Retry-After` の Header に秒）、503 `secret_store_not_configured` / `service_unavailable`（今の Route と同じ）、403（Step-up・認可）。
- **推奨（読み取り）:** `GET /admin/connections` の各 Connection に `last_probe`（`{"trigger", "state", "outcome", "started_at", "finished_at"}`。なければ null。答えの文・Token・Handle・`failure_code` は出さない）と `probe_available_at`（次に再確認できる時刻。抑えがなければ null）を足す。Admin の画面はこれを読み直して結果を知る。General User には今までどおり `availability` の利用可 / 不可だけを見せる（0016 の 8）。
- **推奨（Service）:** `ConnectionService.start_probe(principal, kind, trigger)`（Actor あり。`admin.config.manage`）を足す。Actor のない `check_health(kind)` は残し、内部で同じ Probe の手順（記録・状態の条件）を使う（Test と将来の Loop のため。今は本番で呼ぶ所を作らない）。
- Web の画面（Admin の Connection の画面の「再確認」の Button）は、この Decision の範囲外（未決）。今は Admin の Connection の画面がなく、作るときは PAW-060 の Design canvas に従い、Human の画面の確認を待つ。

### 8. 繰り返しの再確認の抑え（失敗と Back-off）

- **推奨:** 手動の再確認の間隔を、同じ Connection の **直前の Probe の終わり** から次の値だけ空ける（DB の `connection_probes` から計算する。Process の中の状態を持たない）:
  - 直前の Probe が `connected`、または Credential が変わった（attach / replace の後の最初）: **60 秒**。
  - 直前から続けて `expired` / `unavailable` だった回数を `n`（同じ Credential の手動と自動の Probe を数える。`busy` / `cancelled` / `abandoned` は数えない）とすると、`60 × 2^(n-1)` 秒、上限 **15 分**（60・120・240・480・900・900 …）。
  - `connected` になるか、Credential が変わる（replace）と `n` は 0 に戻る。
- 間隔の中の再確認は 429 `probe_cooldown`（`Retry-After`）で、Probe の行は作らない。拒否は `connection.probe` の Deny（`probe_cooldown`）として Audit に 1 行書く（Admin だけが叩ける Route で、Step-up の後なので量は小さい）。
- 自動の Probe（attach / replace の直後）は抑えの対象にしない（Credential の差し替え自体が Step-up つきの操作で、新しい Credential の確認を待たせない）。ただし同じ Credential の Probe が走っていれば始めない（`probe_running`。差し替えの直後に別の Handle なら別の行で走る）。
- 下の「Human の判断点」6。

### 9. Audit の Event

ID と Enum だけ（Prompt・答え・Model 名・Credential・Handle を書かない。0016 の 7 と同じ）:

| Action | いつ | Actor | Decision / Reason | 書き方 |
| --- | --- | --- | --- | --- |
| `connection.probe` | Probe を始める前。`connection_probes` の行と **同じ Transaction** | 起こした Admin（自動は attach / replace をした Admin） | allow / `attach`・`replace`・`manual`（trigger） | Fail closed: 書けなければ行も作らず、送らない（0048 の 4 と同じ「記録できなければ送らない」） |
| `connection.probe` | 再確認の拒否 | Admin | deny / `step_up_required`・`step_up_method_insufficient`・`probe_cooldown`・`probe_running`・`probe_not_configured`・`connection_disabled` | Best effort（今の `_audit_refusal` と同じ） |
| `connection.probe_result` | Probe の行を閉じた後 | なし（System） | allow / `connected`・`expired`・`unavailable`・`busy`・`cancelled`（outcome） | Best effort（今の Service の Event と同じ） |
| `connection.status` | 状態が変わったとき（今のまま） | なし（System） | allow / 新しい状態 | 今の `_store_health` のまま |

- `resource_kind` は `connection`、`resource_id` は Connection の ID、`project_id` は NULL、`details` は NULL。`connection_probes` の ID は相関のために `correlation_id` に入れる。
- Authorizer の `admin.config.manage` の行（再確認の Route と `start_probe`）は今の規則で書かれる。

### 10. Codex（#305 の後）

- **推奨:** Codex の Probe の Adapter は、#305 で `CodexCliRuntime` の Tool の検査に通る版（`codex_app_server.VERIFIED_VERSIONS`、0089 の 2）が決まった後の別の PR で足す。確かめた版でない CLI は Probe も `unavailable`（`invalid_response`）。
- Session の枠: Codex の Session Adapter は `exclusive_sessions` なので、Probe も **Connection の Session の枠** を取る（`SessionSlot.acquire`、最大 60 秒待つ。0083 の 6）。空かなければ `busy`（状態を変えない。5）で行を閉じ、Admin は後で再確認する。下の「Human の判断点」7。
- 枠を取った後は Node の Session と同じ手順: Login を置く前に枠を結び付け（`SessionSlot.bind`、0096 の 2 B）、副作用ごとに `confirm`、終わりに Refresh した `auth.json` を Compare-and-swap で書き戻す（`rotate_credential`、0083 の 6）。止められなかった Unit は 0096 の 1 D の回収の Loop に任せる（Probe の行は `unavailable`、状態は変えない）。`connection_probes.credential_version` は渡した版。
- 受け入れる答えと Prompt は Claude と同じ文（`PAW_PROBE_OK`）。Tool なし（Codex の組み込みの Shell と MCP を止める設定は #305 の版で確かめる）。`expired` は Codex の認証の失敗の分類（`CodexSessionAdapter` の今の分類）に従う。
- Probe が `connected` を書くと、今の `record_health` のとおり 0096 の 5 の `suspect_at` の印も消える。

### 11. 両方の `ConnectionService` への登録

- **推奨:** 新しい `ClaudeProbeAdapter`（`agents/claude_probe.py`。`ConnectionAdapter` の形: `kind = CLAUDE`、`check_health(secret)`、`run(secret, request)`）を、4 の 1・2 を満たす配備で、HTTP 側の `build_connection_service` と Orchestrator 側の `build_cli_services`（PR #303）の両方の `AdapterRegistry` に登録する。Launcher は同じ `SystemdLauncher` の 1 つを共有する。
  - `run`（1 回の呼び出しの口）は Probe の Adapter では **常に失敗する**（何も起動せず `AdapterFailure(INTERNAL_ERROR)`）。Probe の Adapter を登録しても `execute` の経路で CLI が動くことはない。
  - Token を記録するため、`check_health` の結果に Token と失敗の種類を足す形（例: `ProbeReport(status, failure, input_tokens, output_tokens)`。今の `ConnectionStatus` だけを返す Adapter も受ける）を実装の PR で決める。`validate_adapter` の検査は今のまま通る形にする。
- **推奨（HTTP 側の Secret の解決）:** 今の HTTP 側の Service の Resolver は `NoSecretStore`（何も解決しない）。Probe の Adapter を登録するときだけ、Resolver を配備の Secret Store（`FileSecretStore`）にする。HTTP 側の Service には今までどおり Session の Adapter・Budget・Queue の Lease を渡さないので、`execute` / `execute_session` は今の Fail closed（`FailClosedLeaseVerifier`・Session の Adapter なし）のまま。
- 両方の Service は同じ DB の行（`connection_probes` の部分 Index・状態の条件）で調停されるので、どちらから Probe を始めても同じ Credential の Probe は 1 つだけ。

### 12. 採らなかった案（Issue #306 の案 A 以外）

- 認証だけを確かめる Provider の API（Token の Introspection など）を叩く: Subscription の CLI の Token に公開の確認の口がなく、Container・Egress の経路を確かめられない（Issue #306 で Human が A を選んだ）。
- 確認をせず、attach / replace の時点で `connected` にする（0016 の 8 を緩める）: 誤った Credential が `connected` になり、最初の Task で失敗する。
- 定期の Loop: 今はしない（Human の回答）。後続で足すときは、この Decision の `check_health(kind)` と抑えを使う。

## Human の判断点（承認の時の説明。決定は上の「提案」と Approval の行）

案 A が決めていない、本当に選ぶ点だけを挙げる。それ以外（2 の Flag と Prompt の文、5 の状態の条件、6 の背景の Task と掃除、7 のエラーの形、9 の Event、11 の登録）は、上の推奨のとおりでよいかを一括で確かめる。

1. **Probe の使用量をどこに記録するか**（3）。
   - **A（推奨）**: 新しい `connection_probes` に記録し、User の Quota と `connection_usage` には入れない。Probe は配備の保守で、どの User にも帰属しない。
   - B: `connection_usage` の `task_id` / `user_id` / `project_id` を NULL にできるようにし、`purpose` に `health` を足して同じ表に入れる。悪い点: 0016 の 6 の「呼び出しは User と Task に帰属する」と Quota の集計の SQL を変える Migration になり、User 別の利用量に保守の分が混ざる。
   - C: Audit だけに残し、Token は記録しない。悪い点: Subscription の消費が見えない。
2. **受け入れる答えの厳しさ**（2）。
   - **A（推奨）**: `result` が成功で、答えが `PAW_PROBE_OK` を含むときだけ `connected`。Model が本当に答えたこと（Error の文や空の答えでないこと）まで確かめ、言い回しの揺れ（句読点・改行）には強い。
   - B: 成功の `result` で、Model の呼び出しが 1 回報告されれば答えの文は見ない（Issue の「答えが返れば」を文字どおりに読む）。悪い点: 空の答え・拒否の文でも `connected` になる。
   - C: 前後の空白を除いて `PAW_PROBE_OK` と完全に一致。悪い点: 句読点 1 つで `unavailable` になり、Admin を迷わせる。
3. **Admin が無効にした（`enabled = false`）Connection を Probe するか**（4 の 3）。
   - **A（推奨）**: しない（409 `connection_disabled`）。無効化は 0099 の 2 の「すぐ止める」Kill Switch で、外へ何も送らない意味を保つ。再有効化の後に再確認する。
   - B: する（状態だけを確かめ、`enabled` は変えない）。良い点: 有効にする前に確かめられる。悪い点: Kill Switch の後も外へ送る経路が残る。
4. **Probe に 0099 の「有効な User がちょうど 1 人」の条件を当てるか**（4）。
   - **A（推奨）**: 当てない。`[cloud].enabled = true` と `[agents.*]` だけを条件にする。Probe は User のデータを送らず、2 人目の User を足した配備でも Connection の状態を正しく保てる。
   - B: 当てる（`ConfiguredCloudPolicy` と同じ数え直し）。悪い点: 2 人目の User がいると Connection を確かめられず、Admin が条件を戻したときに `unavailable` から始まる。
5. **再確認の Route を非同期にするか**（7）。
   - **A（推奨）**: 202 で Probe の ID を返し、結果は `GET /admin/connections` の `last_probe` で読む。最大 120 秒の Probe で HTTP の要求を待たせない（Reverse Proxy の Timeout にかからない）。
   - B: 同期（結果が出るまで最大 120 秒待って、`status` を返す）。良い点: 画面が簡単。悪い点: Proxy・Browser の Timeout と、切断のときの扱いが要る。
6. **繰り返しの再確認の抑えの値**（8）。
   - **A（推奨）**: 成功の後は 60 秒、続けて失敗するたびに倍（上限 15 分）、成功か Credential の差し替えで戻す。自動の Probe は抑えない。
   - B: 失敗に関わらず一律 60 秒。悪い点: 期限切れの Credential を何度も確かめ、Provider に失敗の認証を繰り返し送りうる。
   - C: 抑えない（Step-up だけ）。悪い点: 同上で、Subscription の Window を少しずつ使う。
7. **Codex の Probe が Session の枠を取れないとき**（10。#305 の後の PR で使う）。
   - **A（推奨）**: 60 秒待って空かなければ `busy` で終わり、状態を変えない（Credential について何も分からない）。
   - B: 枠が空くまで待ち続ける（期限まで）。悪い点: 長い Review の Session の間、Probe の行と Admin の待ちが続く。
   - C: `unavailable` にする。悪い点: 動いている Connection を Probe が落とし、新しい Session が Admission で断られる。

## Test の方針（実装の PR）

- CI では本物の CLI・Network・Provider を使わない（0083 の 9 と同じ: Fake の CLI・Fake の Launcher・PostgreSQL）。
- 固定するもの:
  - **Probe の Adapter（Claude）**: 引数に `--tools ""`・空の MCP の設定・`--max-turns 1`・固定の System Prompt があり、`--allowedTools` と MCP の Server がないこと。標準入力が固定の文で、Task・User の値を含まないこと。Credential が環境の Token にだけあり、引数・Log にないこと。Fake の CLI の各出力で: 成功 + `PAW_PROBE_OK` を含む → `connected`。成功 + 含まない・空 → `unavailable`。`authentication_failed` などの `api_retry` → `expired`。`rate_limit` → 状態を変えない `unavailable`。Model の呼び出しが 2 回 → `unavailable` と CLI の停止。`system/init` に Tool・MCP の Server がある → `unavailable`。異常終了・Stream の誤り・期限切れ（120 秒を Test では短く）→ `unavailable` と CLI の停止。答えの文が Log・行・応答・例外の文に出ないこと。`run` が何も起動せず失敗すること。
  - **記録**: 行と `connection.probe` の Audit が同じ Transaction で Commit されること。Audit が書けなければ行も作らず CLI を起動しないこと。同じ Credential の 2 つ目の Probe が `probe_running` になること（部分 Index。HTTP 側と Orchestrator 側の 2 つの Service から同時に）。Token・時間・`credential_version` が残り、Prompt・答え・Handle が Audit にないこと。`connection_usage` に行が増えず、User の Quota・Task の Budget が変わらないこと。
  - **状態の条件**: Probe の途中で Session が `expired` を記録したら、後から終わった Probe の `connected` が書かれないこと。Probe の途中で replace されたら古い判定が書かれないこと（今の Handle の条件）。`busy` / `cancelled` / `rate_limited` で `status` と `checked_at` が変わらないこと。状態が変わったときだけ `connection.status` が書かれること。
  - **前提**: `[cloud].enabled = false`・`[agents.claude]` がない配備で Adapter が登録されず、自動の Probe が行を作らず、再確認が 409 `probe_not_configured` になること。`enabled = false` の Connection で 409 `connection_disabled`。Ladder が `claude` を名指していなくても `[agents.claude]` があれば Probe できること。有効な User が 2 人でも Probe できること（4 の A）。
  - **Route**: attach / replace の成功の後に自動の Probe が 1 回始まり、応答はそれを待たないこと（失敗した attach / replace では始まらないこと）。再確認の 202・404・409・429（`Retry-After`）・Step-up の 403 と `connection.probe` の Deny の Audit・`admin.config.manage` のない User の 403。`GET /admin/connections` の `last_probe` と `probe_available_at`。抑えの計算（60・120・240・…・900、成功と差し替えで戻る、自動は抑えない）。
  - **走っている Session への影響**: Probe が `expired` を書いた後、走っている Fake の Session が終わるまで走り、新しい `execute_session` が `connection_unavailable` で断られること。`connected` を書いた後に Admission が通ること。
  - **背景の Task と掃除**: Shutdown で取り消された Probe が CLI を止めて `cancelled` で閉じること。10 分を過ぎた `running` の行を Reaper が `abandoned` にし、状態を変えないこと。
  - **HTTP 側の Service**: Probe の Adapter があっても `execute` / `execute_session` が今までどおり断られること。
  - **Migration**: `connection_probes` の Upgrade / Downgrade、CHECK（`trigger`・`state`・`outcome`・`failure_code` の集合、`running` なら `outcome` が NULL）、部分 Index、DELETE の権限がないこと。
  - **Codex（後の PR）**: 枠が取られている間は 60 秒（Test では短く）で `busy`、状態を変えないこと。枠の結び付け・`confirm`・書き戻しの Compare-and-swap・止められなかった Unit の回収への引き渡しを、Node の Session の既存の Test と同じ形で。
- **本物の CLI の Smoke は Human の許可の後**（0083 の 9）: 本番の Container と Egress で Claude の Connection を attach し、自動の Probe で `connected` になること、間違った Token で `expired` になること、`connection_probes`・`audit_events`（`connection.probe`・`connection.probe_result`・`connection.status`）が残ることを見る。この Smoke が step 4 を有効にする前提になる。

## PR の分け方

どれも「Refs #306」（Codex の PR で Closes）。この Decision の承認の後に始める。

1. この Decision（docs だけ）。
2. Probe の記録と Service: Migration（`connection_probes`）・Store（開始と Audit の 1 つの Transaction、閉じる、部分 Index、抑えの計算、Probe の開始の後の判定を上書きしない状態の条件）・`ConnectionService.start_probe` と `check_health` の作り替え・`ProbeReport`・`connection_reaper` の `abandoned`。Fake の Adapter で Test。
3. Claude の Probe の Adapter: `agents/claude_probe.py`・`ClaudeStream` の Probe 用の期待値・両方の Service への登録（4 の 1・2 の条件、HTTP 側の Resolver）。PR #303（`agents/cli_wiring.py`）の Merge の後。
4. Route: attach / replace の後の自動の Probe（背景の Task と Shutdown）・`POST /admin/connections/{kind}/health-check`・`GET /admin/connections` の `last_probe` / `probe_available_at`・Deploy の文書（step 4 の前に Probe で `connected` を確かめる手順）。
5. Smoke（Human の許可の後）。この後に Human が `runtime.toml` の step 4 を有効にする（0093 の 5）。
6. Codex の Probe の Adapter（#305 の後）: 枠・結び付け・書き戻し・確かめた版（10）。
7. （後続・この Decision の外）Admin の Connection の Web の画面と「再確認」の Button（Design canvas と Human の画面の確認）。

2 → 3 → 4 は順に依存する。Migration は今の鎖の頭（`0282`）の後に、Merge の順で鎖を組み直す。

## 未決（この Decision では決めない）

- 定期の Health check の Loop（Human の回答: 今はしない）。足すときは `check_health(kind)` と 8 の抑えを使う新しい Decision。
- 状態の変化（`expired` など）を Owner へ通知する規則（0016 の 8・[NOTIFICATION_POLICY](../NOTIFICATION_POLICY.md)）。
- Session の `EXPIRED` の後や `enable` の後に自動で再確認するか。
- Admin の Connection の Web の画面（PR の分け方の 7）。
- Probe の使用量を利用量の画面・`workspace_usage_report` に出すか。

## Human に確かめてほしいこと（Decision の外の前提）

- **Provider の規約**: Probe は Subscription の CLI を Headless で、Tool なしの固定の Prompt で動かす。0083・0099 の「Human に確かめてほしいこと」の確認（Headless・組み込みの Tool を止めた使い方）に含まれるかを確かめてほしい。
- **Subscription の消費**: 1 回の Probe は数百 Token の見込み（固定の短い System Prompt。CLI の既定の System Prompt を置き換える）。attach / replace ごとに 1 回と、手動の再確認だけ。
- **step 4 の順序**: 本番では (1) `[cloud].enabled = true` と `[agents.claude]` を書き、Ladder は Local のまま再起動、(2) Claude の Connection を attach（自動の Probe で `connected` を確かめる）、(3) Ladder に `claude` を入れて再起動、の順を推奨する（4 の 2 で Ladder の前に確かめられる）。

## リスク

- Probe は Task の外の新しい外への送信の経路である。送るのは固定の文だけで、`[cloud].enabled`・Connection の `enabled`・記録と Audit の Fail closed で絞るが、`CloudGate` を通らない例外を作る。固定の文が変わらないことを Test で固定する。
- 答えの判定（2 の A）は Model の振る舞いに依る。Model が指示どおり答えないと `unavailable` になる。そのときは Smoke と `failure_code`（`invalid_response`）で分かる。
- 120 秒の期限は Container の起動が遅い Host で足りないかもしれない。Smoke で測り、必要なら定数を変える（Decision は要らない値の調整として扱う）。
- HTTP 側の Service が Secret Store を解決できるようになる（11）。HTTP 側には Session の Adapter・Budget・Lease がないので CLI の Session は始まらないが、解決できる Process が増える。Probe の Adapter の `run` を常に失敗させ、Test で固定する。
- Probe の `expired` / `unavailable` は走っている Session を止めないので、期限切れの Credential の Session が終わるまで走る（今の Admin の無効化と同じ）。
- 5 の「新しい判定が勝つ」条件で、Session の `expired` の直後の Probe の `connected` が捨てられることがある。Admin は再確認で直せる（保守的な向き）。

## 承認後の扱い

2026-10-29 に承認され、Status を Approved に改め、承認の内容を Approval の行と「承認時の決定」に記録した。Refine / Supersede する Decision（0016・0099）の本文は書き換えず、README の表に記す。
実装は「PR の分け方」の 2〜6 の PR で行う（どれも「Refs #306」）。step 4 を有効にするのは 5 の Smoke の後で、本番の `/etc/paw/runtime.toml` の変更は Human が行う。
方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。
[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。

## 承認時の決定（2026-10-29）

Human は、作業 Session で判断点ごとの説明（推奨つき）を受け、「両方推奨で」と回答して承認した（PR #308 の [comment](https://github.com/TomokiAkiyama06/personal-ai-workspace/pull/308#issuecomment-6487666748)。すべての判断点を推奨どおり。設計の変更はない）。上の「提案」の各節の「推奨」が承認された方針で、「代替」と「採らなかった案」は採らなかった案として残す。

1. **Probe の使用量の記録**: A。新しい `connection_probes` に記録し、User の Quota・`connection_usage`・`orchestrator.cloud_send` には入れない（3）。
2. **受け入れる答え**: A。`result` が成功で、Model の呼び出しがちょうど 1 回で、答えが `PAW_PROBE_OK` を含むときだけ `connected`（2）。
3. **無効な Connection**: A。Admin が無効にした Connection は Probe しない（自動は走らせず、再確認は 409 `connection_disabled`。4 の 3）。
4. **有効な User の数**: A。0099 の「有効な User がちょうど 1 人」は当てず、`[cloud].enabled = true` と `[agents.*]` と Connection の `enabled` だけを条件にする（4）。
5. **再確認の Route**: A。`POST /admin/connections/{kind}/health-check` は 202 の非同期で、結果は `GET /admin/connections` の `last_probe` で読む（7）。
6. **繰り返しの抑え**: A。成功の後 60 秒、続けて `expired` / `unavailable` のたびに倍、上限 15 分。`connected` か Credential の差し替えで戻す。自動の Probe は抑えない（8）。
7. **Codex の枠が空かないとき**: A。60 秒待って空かなければ `busy` で、状態を変えない（10。#305 の後の PR で使う）。

「Human に確かめてほしいこと」の 3 つの前提も同じ日に確認された: Provider の規約（Headless・Tool なしの固定の Prompt の使い方）は問題なし、Probe ごとに数百 Token の消費は問題なし、step 4 は推奨の順番（`[cloud]` と `[agents.claude]` を書いて Ladder は Local のまま再起動 → attach して自動の Probe で `connected` を確かめる → Ladder に `claude` を入れて再起動）で行う。
