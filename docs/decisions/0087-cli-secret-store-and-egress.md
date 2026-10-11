# CLI の Runtime の前提: Secret Store（製品・保存方式・書き込みの口）と外への通信（Egress の Proxy・Allowlist・nftables）と CLI ごとの認証の形

- Status: Proposed
- Date: 2026-10-12
- Scope: Issue [#208](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/208) の PR 4b（[Decision 0083](0083-agent-runtime-adapters.md) の 11 の 4 のうち、`CodexCliRuntime` / `ClaudeCliRuntime`・Credential の Proxy・`rotate_credential`・PAW の Tool の MCP の Bridge）の前提。この Decision は設計だけで、コードは承認の後に PR 4b で書く。関係する既存の口: `apps/backend/paw_backend/connections/secret.py`（`Secret`・`SecretResolver`。Store はまだない）、`connections/service.py`（`register` / `replace_credential` は Handle だけを受ける）、`apps/backend/deploy/systemd/`。
- Supersedes（一部）:
  - [Decision 0083](0083-agent-runtime-adapters.md)（Approved）の 6 のうち「nftables の `meta skuid paw-egress` の規則でその Account の外への通信を Provider の宛先と DNS だけに限る」を、下の 3 の推奨を選んだ場合に限って置き換える（宛先の IP の集合ではなく「Private の範囲を除いた TCP 443 と、決まった DNS」に絞り、宛先の Host 名の判定は Proxy に置く）。3 で代替 (a) を選べば置き換えない。
  - 同じく 6 の「認証情報は、できる限り Credential を付ける Proxy で扱う（推奨）。Backend が専用の Account で動かす Proxy が…」を、下の 4 の Credential の種類ごとの表で具体化する（Subscription の OAuth は Proxy で付けない。Proxy は専用の Account ではなく、Secret Store を読める Backend の Process の中に置く）。Proxy で付けない Credential の扱い（tmpfs・Connection ごとに 1 Session・Compare-and-swap の書き戻し）は 0083 の 6 のまま。
  - 次は書き換えず、その中で作る: [Decision 0016](0016-shared-connection-adapter-policy.md)（Approved）の 1（平文は Secret Store の Resolver の内側と 1 回の呼び出しの間の `Secret` だけ、DB・Audit・Log には Handle だけ）・7（Credential の置き換えは `admin.config.manage`）、0083 のその他（CLI の Container・`paw-agent-cli`・systemd の Template Unit・polkit・Bridge・1 接続 1 Session の DB Lease）、[Decision 0054](0054-recovery-repository-projection-restore.md)（Recovery に Credential を入れず、復旧の後に再登録する）、[Decision 0079](0079-deploy-update-rollback.md)（Deploy と Restore Point）。

## 背景

[REQUIREMENTS.md](../../REQUIREMENTS.md) と [SECURITY_RBAC_AUDIT.md](../SECURITY_RBAC_AUDIT.md) は次を確定している。

- Credential の平文を DB に置かない。Agent と一般 User に平文を見せない。Log に出さない。Secret を不要に LLM の Context に入れない。
- Codex / Claude は Workspace 全体で共有する System-level の Connection。Owner / Admin が接続・更新・削除し、Secret は Backend の Secret Store / Tool Broker の内側だけで使う。
- Secret の Rotation の能力は必須（`[FIXED REQUIREMENT]`）。
- Recovery は「Credential 類のみ再登録」（Credential は Backup から戻さない）。
- Credential Vault の具体的な製品と保存方式は `[IMPLEMENTATION_CHOICE]`。

承認済みの Decision が決めていること:

- Decision 0016: DB・Audit・Log・Error には Handle（`cred_` + 32 桁の 16 進）だけ。平文は Secret Store の Resolver の内側にだけあり、Adapter の 1 回の呼び出しの間だけ `Secret` として渡す。「Secret Store の製品・方式」と「Network の Policy」が決まるまで実 Adapter は作らない。
- Decision 0083 の 6: CLI の Process は Worktree を Mount しない別の Container（`--network none`）を専用の System Account `paw-agent-cli` で、硬くした systemd の Template Unit から動かす。外へは Bridge の Unix Socket と Egress の Proxy の Unix Socket だけ。Egress の Proxy は専用の Account（例 `paw-egress`）で Host に置き、Provider の Host 名の Allowlist で `CONNECT` を絞り、nftables の `meta skuid` でその Account の外への通信を絞る。認証情報は Credential を付ける Proxy を推奨し、Proxy で動かない CLI は File の Credential を tmpfs に置き、Connection ごとに同時に 1 Session、取り消しの時も Compare-and-swap で書き戻す（`rotate_credential`）。CLI は組み込みの Tool を止め、止められない CLI・版は有効にしない。Turn ごとの Token を報告しない CLI・版は有効にしない。
- 0083 の承認時の P3: 「nftables の宛先 IP は保守が要る」「Credential を足す Proxy は `CONNECT` の Tunnel では足せず、CLI の Endpoint の上書きが要る」。0083 の「リスク」: CLI の認証の形と Option は版で変わるので、実装の PR でその時点の公式の情報を確かめる。

**次のことは要件も既存の Decision も決めていない。** Secret Store の製品と保存方式、平文を Store に入れる口（Admin がどこから入れるか）と Rotation の書き戻しの形、Egress の Proxy を何で作るか・何を許すか・nftables の規則の形、CLI ごと・Credential の種類ごとの認証の渡し方。[AGENTS.md](../../AGENTS.md) の「仕様変更」に従い、選択肢と推奨を下に置き、承認を求める。

この Decision を書くために、2026-10-12 に Codex CLI と Claude Code CLI の公式の Document を確かめた（末尾の「出典」）。CLI の事実はその日の Document によるもので、PR 4b でもう一度確かめる。

## CLI の公式の情報（2026-10-12 に確認）

### Claude Code（`claude`）

- **Headless**: `-p` / `--print`。`--output-format stream-json`（`--verbose` と一緒に）で 1 行 1 つの JSON。`system` の `init`（使える Tool・MCP の Server と接続の失敗）、`assistant` / `user` の Message、`system` の `api_retry`（`rate_limit`・`authentication_failed` などの分類）、最後に `result`（`usage`・`total_cost_usd`・`modelUsage`・`subtype` が `success` か `error_*`）。
- **Turn ごとの Token**: `assistant` の Message の `message.usage` に `input_tokens`・`cache_creation_input_tokens`・`cache_read_input_tokens`。並列の Tool 呼び出しでは同じ `message.id` の Message が同じ `usage` を持つので、`id` で重複を除く。**Message ごとの `output_tokens` は応答の始めの仮の値**で、本当の出力の Token は `result` の `usage`（または `modelUsage`）にだけある。途中の出力の数を見るには `--include-partial-messages` の `message_delta` の `usage` を読む。
- **組み込みの Tool を止める**: `--tools ""` で組み込みの Tool をすべて止める（MCP の Tool は止まらない）。`--strict-mcp-config` と `--mcp-config` で、渡した MCP の設定だけを使う。`--restricted`（v2.1.248 以降）は Command を走らせる Tool と WebFetch を外し、ファイルの Tool を閉じ込め、Managed の設定と `--settings` だけを読む。`--permission-mode dontAsk` / `--permission-prompts none` で確認を出さない。`--no-session-persistence`、`--setting-sources`、`--max-turns`、`--model`、`--system-prompt`。
- **認証の順**: Cloud Provider → `ANTHROPIC_AUTH_TOKEN`（`Authorization: Bearer`）→ `ANTHROPIC_API_KEY`（`X-Api-Key`）→ `apiKeyHelper` → `CLAUDE_CODE_OAUTH_TOKEN` → Profile → `/login` の OAuth。`claude setup-token` は **1 年の OAuth の Token**（Pro / Max / Team / Enterprise の Subscription、Model の Request だけ）を作り、どこにも保存しない。`--bare` はこの Token を読まない（API Key か `apiKeyHelper` が要る）。Linux の `/login` の Credential は `~/.claude/.credentials.json`（0600、`CLAUDE_CONFIG_DIR` の下）。
- **Gateway と Subscription**: `ANTHROPIC_BASE_URL` で Gateway を指せる。Gateway の Credential（`ANTHROPIC_AUTH_TOKEN` など）を使う間は Subscription の Login は送られず、従量の課金になる。`ANTHROPIC_BASE_URL` だけを変えると、Client が持つ claude.ai の Login が引き続き送られる。
- **Network**: `HTTPS_PROXY` / `HTTP_PROXY` / `NO_PROXY` を読む。**SOCKS は使えない**。必要な宛先は `api.anthropic.com`（Model・Feature Flag）、`platform.claude.com`（OAuth の交換・Refresh）、`claude.ai`（Login）など。`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` で Telemetry（Datadog の 2 つの Host）を止め、`ENABLE_CLAUDEAI_MCP_SERVERS=false` で claude.ai の Connector（`mcp-proxy.anthropic.com`）を止める。

### Codex CLI（`codex`）

- **Headless**: `codex exec`。`--json` で JSON Lines（`thread.started`・`turn.started`・`turn.completed`・`turn.failed`・`item.*`・`error`）。Item の種類に Agent の Message・Reasoning・Command の実行・ファイルの変更・MCP の Tool 呼び出し・Web 検索・計画。`-o` / `--output-last-message`、`--output-schema`、`--ephemeral`（Session の File を残さない）、`--ignore-user-config`（`$CODEX_HOME/config.toml` を読まない。認証は `CODEX_HOME` を使う）、`--ignore-rules`、`--skip-git-repo-check`、`-m`、`-C`、`-c key=value`。必須の MCP の Server が起動しなければ `codex exec` は失敗で終わる。
- **Turn ごとの Token**: `turn.completed` の `usage` に `input_tokens`・`cached_input_tokens`・`output_tokens`・`reasoning_output_tokens`。
- **組み込みの Tool を止める**: `features.shell_tool = false`（既定の `shell` の Tool）、`features.unified_exec = false`（PTY の exec の Tool）、`web_search = "disabled"`、`tools.view_image` を無効に。`--disable <feature>` は `-c features.<name>=false` と同じ。**ファイルを書き換える組み込みの Tool（`apply_patch`）を止める設定は、Configuration Reference に見当たらない。** `--sandbox read-only`（既定）、`approval_policy = "never"`（非対話）。MCP の Server は `mcp_servers.<id>`（`command`・`args`・`env`・`enabled_tools`・`required`・`startup_timeout_sec`・`tool_timeout_sec`）。
- **認証**: ChatGPT の Login（Subscription）か API Key。Login は `~/.codex/auth.json`（平文、Access Token を含む。`CODEX_HOME` の下）か OS の Credential Store に置き、`cli_auth_credentials_store` は `file` / `keyring` / `auto` / `ephemeral`。ChatGPT の Session は期限の前に自動で Refresh する。Headless の機械には Device Code の Login か `auth.json` の Copy。API Key は `codex login --with-api-key`（stdin）か、`codex exec` の 1 回の起動の環境の `CODEX_API_KEY`。`forced_login_method`（`chatgpt` / `api`）、`forced_chatgpt_workspace_id`。
- **Telemetry**: `analytics.enabled = false`、`otel.metrics_exporter = "none"`、`otel.trace_exporter = "none"`、`history.persistence`。
- **Network**: Codex の `features.network_proxy` は Sandbox の中の Command の通信だけを絞り、Codex 自身の Model・認証の通信や MCP は対象外。Codex 自身の通信が `HTTPS_PROXY` に従うかは公式の Document に書かれていない（PR 4b で確かめる）。ChatGPT の Login の時の宛先の Host の公式の一覧も見つからなかった。

## 提案

### 1. Secret Store の製品と保存方式

- **推奨: Backend が持つ暗号化した File の Store（新しい製品を入れない）。**
  - 置き場所は `/var/lib/paw/secrets/`（所有者は Backend の User `paw`、Mode 0700。Release の Directory・Recovery Repository・Restore Point の Directory の外。それらの中や上は起動時に拒否する）。Credential ごとに `cred_<32 桁>/v<版>.bin`（0600）。
  - 中身は AES-256-GCM（既存の依存の `cryptography`）。Nonce は版ごとに乱数、追加の認証データ（AAD）に Handle・版・Connection の種類を入れ、別の Handle の File と入れ替えても復号できないようにする。
  - **主鍵は systemd の `LoadCredentialEncrypted=`**（`systemd-creds encrypt` で Host の鍵と、あれば TPM2 に結び付けて暗号化した 32 Byte。`/etc/paw/credentials/paw-secret-key.cred`）で `paw-backend.service` にだけ渡し、Backend は `$CREDENTIALS_DIRECTORY` から読む。`/etc/paw/backend.env` には入れない。Host の root は鍵を取り出せる（root は Admin の側として受け入れる。0083 と同じ）。
  - 書き込みは一時名に書いて `fsync` の後に `rename`。版は増えるだけで、`rotate_credential` は「今の版が渡した版のときだけ次の版を書く」（Compare-and-swap。Handle ごとの `flock` の中で）。古い版は Rotation の成功の後に消す（1 つ前の版だけを、書き戻しの失敗への備えとして 24 時間（暫定）残す）。
  - **DB と Store の食い違いの回収**: Handle は DB（`shared_connections`）に、中身は File の Store にあり、両者を 1 つの Transaction では書けない。順序を「Store に書く → DB に Handle を登録・置き換え」「DB から Handle を外す（Connection の削除・置き換え）→ Store から消す」に固定し、途中で落ちても**参照されない File が残るだけ**（参照された Handle の File が先に消えることはない）にする。残った File は定期の回収（Backend の Reaper の 1 つ。`connection_reaper.py` と同じ Timer）が「どの Connection の行からも参照されず、作られてから 24 時間（暫定）たった Handle」だけを消す（`put` の後に登録されなかった Handle も同じ）。回収は DB の行を読んでから消すまでの間に登録された Handle を消さないよう、24 時間の猶予を超えた File だけを対象にし、消す直前にもう一度その Handle の参照を DB で確かめる。回収の結果は件数だけを System の Audit に残す。
  - Backup と Recovery には入れない（Recovery の後は Credential を再登録する。要件と Decision 0054 のとおり）。主鍵を失くしたときも同じく再登録。
  - Store の異常は System Health の `CRITICAL`（要件の「Secret Store 異常」）。読めない Handle は今の `SecretResolver` の約束どおり `FailureCode.UNAVAILABLE`。
- 代替:
  - (a) HashiCorp Vault / OpenBao の Server を置く: 1 台の Host の Workspace には運用（Unseal・Backup・Upgrade・監視）が重く、Unseal の鍵を結局 Host に置くので得るものが少ない。推奨しない。
  - (b) DB に暗号文を置く（主鍵は上と同じ systemd の Credential）: 要件の「平文を DB に置かない」には反しないが、`pg_dump` の Restore Point（Decision 0079）と Migration の Role に暗号文が渡り、Decision 0016 が「Secret Store の方式が未決」として避けた形に戻る。Compare-and-swap は Transaction で書けて楽になる。推奨しない（決めれば Migration が要る）。
  - (c) 暗号化せず Mode 0600 の File だけ（CLI 自身の `.credentials.json` / `auth.json` と同じ）: Disk の Image・誤った Backup から平文が漏れる。推奨しない。
  - (d) OS の Keyring（libsecret）: System Service には D-Bus の Session がなく使いにくい。推奨しない。
  - (e) 平文を Backend ではなく専用の Account の Process（例 `paw-secrets`）だけが持ち、Unix Socket で 1 回ずつ渡す: Backend の侵害から平文の一覧を守れるが、今の `ConnectionService.execute` は Backend の中の Adapter に `Secret` を渡すので、結局 Backend が平文を受け取る。PR 4b では作らず、後で Credential を付ける Proxy をこの Account にまとめるときに見直す。

### 2. 平文を Store に入れる口

- **推奨: Server の上の管理 Command だけで入れる（Web からは入れない）。** `python -m paw_backend.cli connection-credential put --kind codex|claude`（Backend の User で実行。平文は stdin からだけ読み、引数・環境変数・履歴に置かない。Echo しない）が Store に書いて Handle を返し、Owner / Admin は既存の `register` / `replace_credential`（`admin.config.manage`）にその Handle を渡す。Codex の ChatGPT の Login は `auth.json` を（Device Code の Login をした別の機械か、同じ Host の使い捨ての `CODEX_HOME` で作り）この Command に流し込む。Command は `connection.credential_store` の Audit（Handle・種類・版。値なし）を System の Actor で残す。
  - 理由: 平文が Web の経路（Request の Log・Proxy・Browser）を通らない。要件の Owner の初期設定・Recovery の Command と同じ置き場所（Decision 0005）。Step-up（Passkey）がなくても、Server の上の操作であること自体が強い確認になる。
- 代替: (a) Admin の画面の Form で平文を送る（Passkey の Step-up の後、HTTPS で 1 回だけ、Response にも Log にも出さない）: 使いやすいが、Step-up の対象の Policy と Redact の範囲を広げる別の作業になる。後続の Issue にする。(b) Store の Directory に Admin が File を手で置く: 形の検証・Audit・版の管理ができない。推奨しない。

### 3. Egress の Proxy と nftables

- **推奨: Repository の中の小さな `CONNECT` だけの Proxy（`paw-egress`）。**
  - `paw_backend` とは別の小さな Python の Module（`apps/backend/paw_egress/`。asyncio）を、専用の System Account `paw-egress` の systemd の Service（`NoNewPrivileges`・`ProtectSystem=strict`・`ProtectHome`・`PrivateTmp`・空の `CapabilityBoundingSet`）で動かす。
  - 受けるのは Unix Socket だけ（`/run/paw-egress/egress.sock`、Group は `paw-cli-bridge`、Mode 0660）。HTTP の `CONNECT host:443` だけを受け、TLS は中継するだけで開かない（中身を見ない・Credential を足さない）。
  - 宛先は **Host 名の完全一致の Allowlist**（下の 4 の表。設定は `/etc/paw/egress.toml`、root の持ち物）と Port 443 だけ。Proxy は自分で名前を引き、得た IP が Private・Loopback・Link-local・CGNAT・Multicast の範囲なら拒否する（DNS の書き換えで LAN に入らせない）。
  - 1 つの接続ごとに「Session の ID（Socket の前に置く小さな Header か、Session ごとの Socket）・宛先・Byte 数・結果」を Log に残す（値・中身なし）。拒否は Session の失敗として CLI の `system/api_retry` などに出るので、Runtime はそれを `ConnectionError` に分類する。
  - CLI の Container の中では、`127.0.0.1:<Port>` → Mount した `egress.sock` の小さな転送（Image に入れた固定の Script）を動かし、CLI には `HTTPS_PROXY=http://127.0.0.1:<Port>` を渡す（Claude Code は HTTP の Proxy を読み、SOCKS は使えない）。
  - **nftables**: `meta skuid paw-egress` の外への通信は「Host の設定の DNS の Resolver への 53 番」と「Private の範囲を除いた宛先の TCP 443」だけを許し、他は落とす。Provider の宛先の IP の集合は持たない（CDN の IP が変わるので保守できない: 0083 の P3）。Host 名の判定は Proxy に置く。`paw-agent-cli` の Container は `--network none` なので、nftables はそれに規則を要しない。
- 代替:
  - (a) 0083 の 6 のとおり nftables でも Provider の宛先の IP だけを許す（Timer で Host 名を引き直して `set` を更新）: Proxy が侵害されたときの外への出口を狭められるが、CDN の共有の IP では効果が小さく、引き直しの遅れで正しい通信が落ちる。推奨しない。
  - (b) Squid / tinyproxy を使う: Unix Socket で受けられない（`127.0.0.1` の Port になり、Host の他の User も使える）、Private の範囲の拒否・Session ごとの Log を設定で作る手間がある。推奨しない。
  - (c) Codex の `features.network_proxy` に任せる: Codex 自身の Model・認証の通信を絞らない（公式）。Claude Code に使えない。推奨しない。

### 4. CLI ごと・Credential の種類ごとの認証の渡し方

0083 の 6 の「Credential を付ける Proxy」は、`ANTHROPIC_BASE_URL` のような Endpoint の上書きがあって、**Provider が Gateway として認めている Credential の種類**のときだけ使う。Subscription の OAuth を Proxy が Client の代わりに付けるのは、公式の Gateway の形（Gateway の Credential を使う間は Subscription は使われない）から外れ、Provider の規約の確認（0083 の「Human に確かめてほしいこと」）も済んでいないので、行わない。

| Connection | Credential の種類 | 推奨の渡し方 | 同時の Session | Rotation | Allowlist（Proxy） |
| --- | --- | --- | --- | --- | --- |
| Claude | Subscription（`claude setup-token` の 1 年の Token） | Session ごとの Directory の File（0400、所有者 `paw-agent-cli`）から、起動の Wrapper が `CLAUDE_CODE_OAUTH_TOKEN` に入れて `claude -p` を起動（Unit の `Environment=` や Command 行には書かない）。`--bare` は使わない（この Token を読まないため） | 制限なし（Refresh がないので互いを失効させない） | なし。期限の 30 日（暫定）前から Health Check が Admin に通知し、Admin が 2 の口で入れ替える | `api.anthropic.com`（必要なら `platform.claude.com`。PR 4b の Smoke で拒否の Log から確かめる） |
| Claude | API Key（Console） | **Credential を付ける Proxy（Backend の中）**: Proxy は `paw-egress` ではなく **Backend の Process の中**に置く（Secret Store を読めるのは Backend だけなので）。Backend は Session ごとの Directory（0083 の 6。Group `paw-cli-bridge`、0750）に `anthropic.sock`（0660）を作り、そこで平文の HTTP を受ける。CLI の Container の中の転送が `127.0.0.1:<Port>` → `anthropic.sock` をつなぎ、CLI には `ANTHROPIC_BASE_URL=http://127.0.0.1:<Port>` と Session ごとの使い捨ての値の `ANTHROPIC_API_KEY` を渡す。Backend はその値を Session の Token として確かめ（違えば 401）、取り除いてから本物の Key の `X-Api-Key` を付け、決まった宛先 `https://api.anthropic.com` の決まった Path（`/v1/messages`・`/v1/messages/count_tokens` など、PR 4b で一覧を固定）にだけ TLS で送る。Backend 自身の外への通信なので `paw-egress` を通らない。Container に Key を置かない。`--bare` を使える | 制限なし | Admin が 2 の口で入れ替える | （`paw-egress` は使わない。Backend から `api.anthropic.com`） |
| Codex | ChatGPT の Login（`auth.json`） | 0083 の 6 の File の形: Session ごとの tmpfs の `CODEX_HOME`（0700）に `auth.json` を置き、`cli_auth_credentials_store = "file"`・`forced_login_method = "chatgpt"`。終わりに `auth.json` が変わっていれば `rotate_credential`（Compare-and-swap） | **Connection ごとに 1 つ**（0083 の 6 の DB の Lease。Refresh が回転するため） | CLI の自動 Refresh を書き戻す | PR 4b の Smoke で拒否の Log から決める（公式の一覧がない）。決まるまで ChatGPT の Login の Codex は有効にしない |
| Codex | API Key | 起動の Wrapper が `CODEX_API_KEY` を `codex exec` の 1 回の起動の環境にだけ入れる（公式の推奨の形）。`forced_login_method = "api"`。Credential を付ける Proxy（`model_providers.<id>.base_url`）は、その Provider の設定で Key なしの Request を送れるかを PR 4b で確かめてから選ぶ | 制限なし | Admin が 2 の口で入れ替える | `api.openai.com` |

- どの形でも、Credential は Log・DB・Error・`NodeResult`・Tool の出力に出さず、CLI の出力は Decision 0016 の 1 の Redact を通す（0083 の 6 のまま）。Session ごとの Directory と tmpfs は Session の終わり（取り消しを含む）に消す。
- **Codex の組み込みの Tool**: `features.shell_tool = false`・`features.unified_exec = false`・`web_search = "disabled"`・`tools.view_image` を無効にし、`--sandbox read-only`・`approval_policy = "never"`・`--ignore-user-config`・`--ignore-rules`・`--ephemeral`・`analytics.enabled = false`・`otel` の Exporter を `none`。**ファイルを書き換える組み込みの Tool（`apply_patch`）を止める公式の設定は見当たらないので、0083 の 6 のとおり（止められない CLI・版は有効にしない）、`apply_patch` を最初から止められることを PR 4b の Smoke で確かめるまで Codex の Runtime は有効にしない**（Fail closed。0083 を置き換えない）。止める方法が見つかった後も、JSONL に Command の実行・ファイルの変更の Item が 1 つでも出たら Runtime はその場で CLI を止めて Session を失敗にする（`escalate=True`）検出を、二重の守りとして残す。
- **Claude の組み込みの Tool と PAW の Tool の許可**: `--tools ""`（組み込みの Tool をすべて外す）・`--strict-mcp-config --mcp-config <Session の設定>`（PAW の Bridge の MCP の Server だけ。名前は固定の `paw`）・**`--allowedTools "mcp__paw__*"`**（Session の PAW の MCP の Tool だけを確認なしで許す。`--strict-mcp-config` は Server を絞るだけで Tool を許可しないため）・`--permission-mode dontAsk`（または `--permission-prompts none`。許可の規則にない呼び出しは確認せずに拒否）・`--no-session-persistence`・User / Project の設定を読まない（`--setting-sources` と `--settings` で Session の設定だけ）・`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`・`ENABLE_CLAUDEAI_MCP_SERVERS=false`。`--restricted` が使える版では加える。`system/init` の Tool の一覧に `mcp__paw__` で始まる Tool 以外があれば、Runtime は起動の直後に止めて失敗にする（同じく `escalate=True`）。Fake の CLI の Test で、許可の指定がないと PAW の Tool と `submit` が拒否されることも固定する。
- **Turn ごとの Token の読み方**（0083 の 5 の `on_usage`）: Claude は `assistant` の Message を `message.id` で重複を除き、`input_tokens + cache_creation_input_tokens + cache_read_input_tokens` を入力として Turn ごとに報告する。**出力は Message の `output_tokens` が仮の値なので、Turn ごとの出力は `--include-partial-messages` の `message_delta` の `usage` の最後の値で報告する**。終わりの合計は `result` の `modelUsage`（Subagent を含む）を使い、`result` が欠けた・0 の Crash のときは「合計が分からない」として 0083 の 5 (d) の見積もりに回す。Codex は `turn.completed` の `usage`（`input_tokens`（`cached_input_tokens` を含むかは Smoke で確かめる）・`output_tokens`（`reasoning_output_tokens` を含むかも同じ））を Turn ごとに報告する。どちらも数え方を Fake の CLI の固定の Stream で Test する。

### 5. PR 4b の中の順序

1. Secret Store（1）と管理 Command（2）、`rotate_credential`（Compare-and-swap）。
2. `paw-egress`（3）と nftables の規則の例（Deploy の Document。適用は Human）。
3. Claude の Subscription と API Key の Runtime（4）。Fake の CLI（2026-10-12 の Document の Stream の形）で Test。Codex の Runtime のコードも Fake の CLI で作ってよいが、`apply_patch` を止められることを Smoke で確かめるまで組み立てで有効にしない。
4. Codex の ChatGPT の Login は、Smoke で Allowlist と `HTTPS_PROXY` への従い方を確かめた後の別の小さな PR。

どの CLI の段も、`CloudPolicy` の Decision の承認までは組み立てで有効にならない（0083 の 11 の 4 のまま）。CLI の Smoke は Human が規約と使用量への影響を確かめた後（0083 の 9）。

## 決めてほしいこと

1. **Secret Store は Backend の暗号化した File の Store（`/var/lib/paw/secrets/`、AES-256-GCM、Handle・版の AAD、主鍵は systemd の `LoadCredentialEncrypted=`（Host の鍵と TPM2）で `paw-backend.service` にだけ渡す、版ごとの File と Compare-and-swap の Rotation、Backup・Recovery に入れない、Store に書いてから DB に登録・DB から外してから Store から消す順序と、参照されない古い File の定期の回収）**（1）でよいか。推奨: はい。
2. **平文は Server の上の管理 Command（stdin だけ）で Store に入れて Handle を受け取り、Owner / Admin は既存の `register` / `replace_credential` に Handle を渡す。Web の Form からの登録は Step-up の後の後続の Issue**（2）でよいか。推奨: はい。
3. **Egress は Repository の中の `CONNECT` だけの Proxy（`paw-egress` の Account、Unix Socket だけで受ける、Host 名の完全一致の Allowlist と 443 だけ、引いた IP が Private などの範囲なら拒否、TLS は開かない）。nftables は `paw-egress` に DNS と「Private を除く TCP 443」だけを許し、Provider の IP の集合は持たない（0083 の 6 のこの部分を置き換える）**（3）でよいか。推奨: はい。
4. **認証の渡し方は 4 の表のとおり（Claude の Subscription は `setup-token` の Token を Session ごとの File から `CLAUDE_CODE_OAUTH_TOKEN` で、Claude の API Key は Backend の中の Credential を付ける Proxy（Session の Directory の Unix Socket、Session の Token を確かめて本物の Key に差し替え、決まった宛先と Path だけ）で、Codex の ChatGPT の Login は 0083 の File の形で Connection ごとに 1 Session、Codex の API Key は `CODEX_API_KEY` で）。Subscription の OAuth を Proxy が付けることはしない**（4）でよいか。推奨: はい。
5. **組み込みの Tool は 4 の Option で止める。Codex は `apply_patch` を最初から止められることを Smoke で確かめるまで有効にしない（0083 の 6 のまま Fail closed）。Claude は `--tools ""` と `--allowedTools "mcp__paw__*"` で PAW の MCP の Tool だけを許す。どちらも Stream（Codex の JSONL の Item、Claude の `system/init` の Tool の一覧）の検出を二重の守りにする**（4）でよいか。推奨: はい。
6. **Turn ごとの Token は 4 の読み方（Claude は `message.id` の重複を除いた入力と `message_delta` の出力、終わりは `modelUsage`。Codex は `turn.completed` の `usage`）で報告する**（4）でよいか。推奨: はい。
7. **PR 4b は 5 の順で進め、Codex の ChatGPT の Login は Smoke の後の別の PR にする**（5）でよいか。推奨: はい。

## Human に確かめてほしいこと（Decision の外の前提）

- **Provider の規約**（0083 から変わらず）: 共有の Subscription を Backend が複数の User のために Headless で使うこと。特に Claude の `setup-token` の Token と Codex の ChatGPT の Login を 1 つの Workspace で共有すること。Smoke の前に。
- **どの Credential の種類を使うか**: Subscription（定額、Usage の上限）か API Key（従量）か。4 の表はどちらも書いたが、PR 4b は Human が使うと決めた種類から作る。
- **Host の準備**: `podman` はこの Host にまだ入っていない（0083 の承認の条件 (1) の確認は PR 4b で行う）。`systemd-creds` と TPM2 はある（systemd 259、`/sys/class/tpm/tpm0`）。

## リスク

- CLI の Option・Stream の形・認証の形は版で変わる（2026-10-12 の Document でも、`--restricted`・`--permission-prompts` は最近の版で入った）。CLI の版を Image の Digest で固定し、上げるときは Fake の CLI の Stream と Smoke をやり直す。
- Codex の `apply_patch` を止める方法が見つからなければ、Codex の CLI の段は使えない（Claude の CLI の段と Local だけになる）。
- Claude の Message ごとの `output_tokens` が仮の値であることを見落とすと、Turn ごとの計上が少なくなる。`message_delta` が来ない版では、Turn ごとの出力は 0 として、終わりの精算（0083 の 5 (c)）で埋まる（途中の Budget の停止が遅れる）。
- `setup-token` の Token は 1 年で切れ、Refresh がない。期限の通知を見落とすと Connection が `expired` になる（Admin が入れ替える）。
- 主鍵を Host の鍵と TPM2 に結び付けると、Host の入れ替え・TPM の Clear で Store が読めなくなる。そのときは Credential の再登録（Recovery と同じ）。
- nftables を Provider の IP に絞らないので、`paw-egress` が侵害されると Public の任意の 443 に出られる。Proxy は小さく保ち、Log を見る。

## 承認後の扱い

承認されたら `Approval` に記録し、Status を Approved に改める。
方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。
[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。

## 出典（2026-10-12 に確認）

- Claude Code: [CLI reference](https://code.claude.com/docs/en/cli-reference)、[Run Claude Code programmatically（headless）](https://code.claude.com/docs/en/headless)、[Environment variables](https://code.claude.com/docs/en/env-vars)、[Authentication](https://code.claude.com/docs/en/authentication)、[Enterprise network configuration](https://code.claude.com/docs/en/network-config)、[Other LLM gateways](https://code.claude.com/docs/en/llm-gateway)、[Track cost and usage（Agent SDK。`stream-json` と同じ Message の形）](https://code.claude.com/docs/en/agent-sdk/cost-tracking)
- Codex CLI（`developers.openai.com/codex/...` は `learn.chatgpt.com/docs/...` へ転送される）: [Non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)、[Developer commands（CLI）](https://learn.chatgpt.com/docs/developer-commands?surface=cli)、[Configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)、[Authentication](https://learn.chatgpt.com/docs/auth)
- systemd: `systemd-creds(1)`、`systemd.exec(5)` の `LoadCredentialEncrypted=`（Host の systemd 259 の man page）
