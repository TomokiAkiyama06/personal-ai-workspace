# Backend

Personal AI Workspace の Core Backend です。
[PAW-020](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/17) で、後続の Issue が載る最小の Application Skeleton を実装しました。
認証と User はまだ実装していません（PAW-021 以降）。
RBAC と Audit（PAW-025）、Task の Lifecycle と永続化（[PAW-032](#agent-task-lifecycle)、HTTP の Endpoint はまだありません）、Tool Broker と Capability Policy（[PAW-031](#tool-broker--capability-policy)、HTTP の Endpoint はまだありません）、Memory の PostgreSQL Schema（[PAW-040](#memory--conversation-schema)）を実装済みです。Memory の保存・整理・検索の処理は PAW-041 以降です。

[Architecture](../../docs/ARCHITECTURE.md) に基づき、最終的に以下の機能を Backend 側で扱います。

- Core API と Session、Project / Repository の管理
- Authentication、RBAC、Capability による権限判定
- Agent Orchestrator と Task の状態・実行管理
- Memory の保存・検索・アクセス制御
- Tool Broker を通じた Tool 実行と Credential の分離

[Web](../web/README.md) と [CLI](../cli/README.md) は同じ Backend API を利用します。
権限の最終判定は Backend が行います。
Core Backend は GPU 非依存とし、Local Model Runtime を停止できる構造にします。

## 技術構成

[Decision 0003](../../docs/decisions/0003-backend-cli-web-implementation-stack.md) で承認された構成です。

| 領域 | 選択 |
| --- | --- |
| 言語 / Framework | Python 3.13、FastAPI + Uvicorn、Pydantic v2 |
| DB | PostgreSQL、SQLAlchemy 2.x（async）+ psycopg 3、Alembic |
| Test / Lint | 標準 `unittest`、Ruff |
| Package 管理 | uv + `pyproject.toml`（依存は完全一致で固定） |

pgvector は Memory Schema（PAW-040）の Migration が `vector` extension として有効にします。
Python 側の Package（`pgvector-python`）は使わず、`paw_backend/memory/vector.py` の Column 型だけで扱います。

## 構成

```text
apps/backend/
├─ pyproject.toml          # 依存（完全一致で固定）と Ruff 設定
├─ alembic.ini             # Alembic 設定（DB URL は持たない）
├─ migrations/             # env.py と Revision（0001 は空の Baseline、0031 は Tool Approval、0040 は Memory Schema）
├─ paw_backend/
│  ├─ app.py               # create_app(settings)
│  ├─ config.py            # PAW_ 環境変数から読む Settings
│  ├─ server.py            # Uvicorn 起動（TLS 設定）
│  ├─ db.py                # 非同期 Engine / Session、Readiness 確認
│  ├─ events.py            # プロセス内 Event Bus と Heartbeat
│  ├─ errors.py            # 共通の Error Response
│  ├─ middleware.py        # Request ID、Host 検証、Security Header
│  ├─ security.py          # Host / Origin の判定
│  ├─ authz/               # Role・Capability・認可の判定と Audit Event（PAW-025）
│  ├─ tasks/               # Agent Task の状態遷移と永続化（PAW-032）
│  ├─ memory/              # Memory / Conversation の Model、ACL 条件、vector 型（PAW-040）
│  ├─ tools/               # Tool Broker、Capability Policy、Approval（PAW-031）
│  └─ api/
│     ├─ deps.py           # FastAPI Dependency
│     └─ v1/               # /api/v1 の Router（health、events）
└─ tests/                  # unittest
```

新しい機能は `api/v1/` に Router を追加して `api/v1/__init__.py` へ登録します。
ORM Model は `paw_backend.db.Base` を継承し、Alembic Revision で Schema を変更します。

## 開発

`uv` と Python 3.13 が必要です。`.venv` は `.gitignore` 済みです。

```bash
cd apps/backend
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -e . --group dev

export PAW_DATABASE_URL=postgresql://USER:PASSWORD@localhost:5432/paw
.venv/bin/alembic upgrade head
.venv/bin/python -m paw_backend          # または .venv/bin/paw-backend
```

Test と Lint は Repository の CI と同じコマンドです。

```bash
.venv/bin/python -m unittest discover -s tests -t .
.venv/bin/ruff format --check . && .venv/bin/ruff check .
```

Repository 全体の検証は `python .github/scripts/run_ci.py` です（[CI](../../.github/CI.md) を参照）。
実 PostgreSQL に対する Test は `PAW_TEST_DATABASE_URL` を設定した場合だけ実行し、未設定では Skip します。
GitHub Actions は使い捨ての PostgreSQL を起動してこの変数を渡すため、CI ではこれらの Test も実行されます。
この Test は Migration を `head` へ上げて `base` へ戻すため、ローカルでも使い捨ての Database を指定してください。
Database には pgvector が必要です（CI は `pgvector/pgvector:pg18` を使います）。Migration の実行には `CREATE EXTENSION` の権限が要ります。

## 設定

設定はすべて `PAW_` から始まる環境変数で渡します。Repository に Secret や既定の認証情報は置きません。
不正な値による起動エラーには、値（DB URL のパスワード等）を含めません。

| 変数 | 既定値 | 内容 |
| --- | --- | --- |
| `PAW_HOST` / `PAW_PORT` | `127.0.0.1` / `8000` | Listen する Address |
| `PAW_TLS_CERTFILE` / `PAW_TLS_KEYFILE` | なし | 両方を指定すると Uvicorn が HTTPS を終端する |
| `PAW_ALLOW_PLAINTEXT_HTTP` | `false` | Loopback 以外で TLS なしの起動を許可する（下記） |
| `PAW_HSTS_MAX_AGE_SECONDS` | `31536000` | `Strict-Transport-Security` の max-age。HTTPS の Response にだけ付ける。`0` で付けない |
| `PAW_ALLOWED_HOSTS` | `localhost,127.0.0.1,[::1]` | 許可する `Host`（Comma 区切り、Port なし）。Reverse Proxy 経由では公開 Host 名を含める |
| `PAW_ALLOWED_ORIGINS` | 空 | WebSocket を開いてよい Origin（Comma 区切り、例 `https://paw.example.org`） |
| `PAW_SHUTDOWN_TIMEOUT_SECONDS` | `5` | 停止時に開いたままの SSE / WebSocket を待つ秒数。超えると切断する |
| `PAW_DATABASE_URL` | なし | `postgresql://` または `postgresql+psycopg://`。未設定でも起動する |
| `PAW_DATABASE_TIMEOUT_SECONDS` | `3` | 接続と Readiness 確認の Timeout |
| `PAW_DATABASE_POOL_SIZE` | `5` | Connection Pool のサイズ |
| `PAW_MIGRATION_DATABASE_URL` | なし | Migration 専用の接続先（Schema の Owner の Role）。設定すると Alembic は `PAW_DATABASE_URL` の代わりにこれを使う。認可・Audit の節を参照 |
| `PAW_APP_DATABASE_ROLE` | なし | Application が接続する PostgreSQL の Role 名（英数字と `_`、63 文字まで。`public`、`pg_` で始まる名前、`postgres` などの予約名は拒否）。Audit Table の Migration が、実在するこの Role に INSERT と SELECT だけを与える（存在しなければ Migration が失敗する） |
| `PAW_DATABASE_READINESS_CACHE_SECONDS` | `1` | Readiness の結果（失敗を含む）を再利用する秒数。`0` で再利用しない |
| `PAW_EVENT_HEARTBEAT_SECONDS` | `15` | `system.heartbeat` の間隔 |
| `PAW_EVENT_QUEUE_SIZE` | `100` | 接続ごとの Event Queue。溢れた場合は古い Event を捨てる |
| `PAW_EVENT_MAX_SUBSCRIBERS` | `100` | 同時に接続できる SSE / WebSocket の数。超えた接続は SSE が 503、WebSocket が Close Code 1013 |
| `PAW_LOG_LEVEL` | `info` | Uvicorn の Log Level |

## HTTPS

[要件](../../REQUIREMENTS.md)どおり、Backend の Port を Public Internet へ直接公開しません。
TLS の終端は次のどちらかで行います。

1. Uvicorn が終端する: `PAW_TLS_CERTFILE` と `PAW_TLS_KEYFILE` を指定します。
2. Reverse Proxy（`tailscale serve`、Caddy など）が終端する: Backend は `127.0.0.1` だけで Listen します。

Loopback 以外の Address で TLS なしに起動しようとすると、`PAW_ALLOW_PLAINTEXT_HTTP=true` がない限り起動を拒否します。
証明書の発行と更新は Deployment の課題で、この Skeleton では扱いません。

`Host` Header は全 Request で検証し、許可していない値には 400（`invalid_host`）を返します（DNS Rebinding の対策）。
既定では Loopback の名前だけを許可します。**Reverse Proxy 経由で公開する場合は、公開 Host 名を `PAW_ALLOWED_HOSTS` に追加してください。**
Health Check が別の `Host`（Container の IP など）で Request する場合も、その値が必要です。

`Strict-Transport-Security` は、HTTPS で受けた Request（Uvicorn が TLS を終端している、または信頼した Proxy が `X-Forwarded-Proto: https` を渡している）にだけ付けます。
TLS を終端する Reverse Proxy の背後で Backend が HTTP を受ける場合は、Proxy 側で HSTS を送ってください。

## API

Endpoint は `/api/v1` 以下です。OpenAPI Schema は `/api/v1/openapi.json` で取得できます（Swagger UI は配信しません）。

| Endpoint | 内容 |
| --- | --- |
| `GET /api/v1/health` | Liveness。DB を確認しない。常に `{"status": "ok"}` |
| `GET /api/v1/health/ready` | Readiness。DB へ `SELECT 1` を実行する |
| `GET /api/v1/events/stream` | Server-Sent Events |
| `WebSocket /api/v1/events/ws` | WebSocket |

Readiness は 200 または 503 で、Body の形は同じです。

```json
{"status": "unavailable", "checks": {"database": "unavailable"}}
```

`checks.database` は `ok`、`unavailable`、`not_configured` のいずれかです。
接続文字列、Host、認証情報は Response にも Log にも出しません。
Readiness の失敗時に Log へ残すのは例外の型名だけです。

エラーはすべて次の形式です。`code` は機械可読な固定値、`request_id` は `X-Request-ID` Header と同じ値です。

```json
{"error": {"code": "not_found", "message": "Not Found", "request_id": "..."}}
```

Validation Error は `details`（位置、Message、型）を加えますが、送信された値は返しません。
予期しない例外は Traceback を Log にだけ残し、Client には `internal_error` を返します。

全 HTTP Response に `X-Request-ID`（妥当な入力値は引き継ぎ、それ以外は新規に生成）と、
`X-Content-Type-Options`、`X-Frame-Options`、`Content-Security-Policy`、`Referrer-Policy` を付けます。
`Cache-Control: no-store` は既定値で、Endpoint が自分で設定した値（SSE の `no-cache`）は上書きしません。
`Strict-Transport-Security` の条件は上の「HTTPS」を参照してください。
CORS は有効にしていません。Web Client の配信 Origin が決まってから設定します。

## Event 経路

`paw_backend.events.EventBus` はプロセス内の Fan-out です。永続化と再送はなく、接続後に発行された Event だけを受け取ります。
この Skeleton が発行する Event は `system.connected`（接続直後に 1 回）と `system.heartbeat`（一定間隔）だけです。
User、Project、Task、Memory のデータは含みません。

**現在この 2 つの Endpoint は認証なしです。** Session が存在しないためです。
そのため、システム Event 以外は配信しません。
認証の代わりに、次の制限を入れています。

- `Host` の検証（全 Endpoint）
- WebSocket の `Origin` 検査: Browser は WebSocket に CORS を適用しないため、Server 側で検査します。
  `Origin` が Request 自身の `Host` と同じ、または `PAW_ALLOWED_ORIGINS` にある場合だけ受け付けます。
  それ以外の Browser からの接続は Handshake で拒否します（Close Code 1008）。`Origin` を送らない Client（CLI、Script）は対象外です。
- 同時接続数の上限（`PAW_EVENT_MAX_SUBSCRIBERS`）と、遅い Client の古い Event の破棄

PAW-022（Login / Session）は、システム Event 以外を配信する前に次を実装する必要があります。

- 認証済み Session の要求（`Origin` 検査は Session Cookie を使う WebSocket に必須だが、認証の代わりにはならない）
- Event 種別ごとの認可

該当箇所には `TODO(PAW-022)` を置いています。

## Database と Migration

Engine は最初に使うときに作られ、その時点でも接続はしません。
そのため PostgreSQL が停止していても Process は起動し、Liveness に応答します。
Readiness は Pool を使わず、専用の接続で `SELECT 1` を実行し、`PAW_DATABASE_TIMEOUT_SECONDS` で必ず応答します。
`/api/v1/health/ready` は到達できる誰でも呼べるため、開く接続数を制限しています。
同時の呼び出しは 1 つの Probe を共有し（Single Flight）、結果は `PAW_DATABASE_READINESS_CACHE_SECONDS` の間再利用します。
そのため Readiness が開く接続は、最大でこの間隔ごとに 1 つです。応答しない Server への再確認も、前の Probe が Timeout した後です。
呼び出し元の Request が途中で切れても、共有している Probe は他の呼び出しのために続きます。
URL の Query（`connect_timeout`、`application_name` など）はそのまま使い、Probe 自身の指定（`connect_timeout`、`autocommit`）が優先されます。
Timeout した Probe は、Query の取消（psycopg が Server の確認を待つ、最大約 10 秒）を行わず、接続の Socket を閉じて即座に失敗させます。
libpq 17 未満（`psycopg[c]` とシステムの libpq など）では、取消が Thread で実行され、`asyncio.run` の終了が長時間止まるためです。
`Database.dispose()`（Application の終了時）は、実行中の Probe を同じ方法で止め、`PAW_SHUTDOWN_TIMEOUT_SECONDS` の範囲で完了を待ちます。
今後 Session を使う Endpoint を追加する場合、終了時に実行中だった Query の取消は psycopg の取消経路に入ります。
その経路が終了を遅らせないことは、その Issue で確認してください。
`Database.session()` と `paw_backend.api.deps.get_session` が Session を提供します。

Alembic は `PAW_MIGRATION_DATABASE_URL`（設定されていれば）または `PAW_DATABASE_URL` から接続先を読み、`alembic.ini` には DB URL を書きません。
Schema を変更する Role と Application が使う Role は分けてください（[Audit](#追記専用について保証すること・しないこと)）。
どのディレクトリからでも実行できます。

```bash
alembic -c apps/backend/alembic.ini upgrade head          # 適用
alembic -c apps/backend/alembic.ini upgrade head --sql    # SQL の出力のみ（DB 接続は不要）
alembic -c apps/backend/alembic.ini revision -m "説明"    # 新しい Revision
```

## Agent Task Lifecycle

PAW-032 で実装しました。`paw_backend/tasks/` は Task の状態遷移（`domain.py`、DB なしの純粋な規則）と、その永続化（`models.py`、`service.py`、Migration `0032`）です。
**HTTP の Endpoint はありません。** 認証と RBAC（PAW-022 / PAW-025）が先に必要なためです。
`TaskService` は認可を行いません。Endpoint を作る側が、権限を確認してから認証済み User を `Actor` として渡します。
Queue、Budget、Loop 検知（PAW-033）と DAG Orchestration（PAW-034）は含みません。

### 状態

| 状態 | 意味 |
| --- | --- |
| `queued` | 実行待ち |
| `running` | 実行中 |
| `waiting` | 判断・承認・Resource の待ち。理由は `wait_reason`（`user` / `approval` / `resource`）で、`waiting` のときだけ値を持つ（DB の CHECK 制約） |
| `paused` | 安全な区切りで停止中 |
| `evaluating` | Test / Evaluator / Review 中 |
| `completed` | 完了（何も受け付けない） |
| `failed` | 実行不能または評価失敗 |
| `cancelled` | User または Policy により中止 |

### Command と遷移

Command は、Operator の 6 操作（Pause / Resume / Cancel / Retry / Restart / Stop Now）と、Orchestrator・Worker・Policy が進行を報告する Event（Start / Wait / Unblock / Begin evaluation / Complete / Fail）です。
遷移表は `domain.TRANSITIONS` の 1 か所にあり、表にない組み合わせは `IllegalTransitionError` になります。

| 状態 | 受け付ける Command（遷移先） |
| --- | --- |
| `queued` | start（running）、fail（failed）、cancel（cancelled） |
| `running` | wait（waiting）、begin_evaluation（evaluating）、fail（failed）、pause（paused）、cancel（cancelled）、stop_now（cancelled） |
| `waiting` | unblock（running）、fail（failed）、cancel（cancelled）、stop_now（cancelled） |
| `paused` | resume（running）、cancel（cancelled） |
| `evaluating` | complete（completed）、fail（failed）、cancel（cancelled）、stop_now（cancelled） |
| `completed` | なし |
| `failed` | retry（queued）、restart（queued） |
| `cancelled` | restart（queued） |

Operator の 6 操作は次のように解釈しています（[要件](../../REQUIREMENTS.md)の「Task pause / cancel / retry / restart」）。

- **Pause / Resume**: Pause は実行中の Task だけが対象で、現在の安全な区切りでの停止です。Step は Backend が閉じず、Worker が区切りで `finish_step` を呼びます。Resume は `paused` から `running` へ戻します。
- **Cancel（graceful）**: Task を終了します。branch / worktree / 途中成果は保持します（削除は別の操作）。実行中の Step は Worker が自分で閉じます。
- **Stop Now（immediate）**: 緊急停止です。Cancel と同じ `cancelled` になりますが、実行中の Step を即座に `interrupted` にし、停止理由と中断した Step を Task Log と履歴 Event へ残します。実行中に何かが動きうる状態（running / waiting / evaluating）だけが対象で、queued と paused には Cancel を使います。成果物は削除しません。
  Cancel と Stop Now の違いは Event の Command（`TaskEvent.interruption` が `graceful` / `immediate`）で判別できます。
- **Retry**: `failed` の Task を、失敗した Step から同じ試行（同じ branch / worktree / Log）で再実行します。`queued` へ戻り、`retry_count` を 1 増やし、Agent / Model を切り替えられます（履歴 Event の `detail` に旧値と新値を残す）。
- **Restart**: `failed` または `cancelled` の Task を、元の `starting_commit` と Task `input` から最初からやり直します。試行番号（`attempt`）を 1 増やし、新しい branch / worktree / Review / PR の状態を持つ空の試行を作ります。旧試行は `task_attempts` と Step・Log に残り、`TaskSnapshot.previous_attempts` から見えます。

### 永続化

| Table | 内容 |
| --- | --- |
| `tasks` | 現在の状態、`wait_reason`、`version`、試行番号、`retry_count`、Agent / Model、`starting_commit`、`input`（Restart の基準） |
| `task_attempts` | 試行ごとの branch / worktree / head commit、Review 状態、Evaluator 結果、PR の番号・URL・状態 |
| `task_steps` | Step の実行記録。試行内で最新の行が current step。試行内で `running` は高々 1 つ（Partial Unique Index） |
| `task_logs` | 試行ごとの Log（`debug` / `info` / `warning` / `error`） |
| `task_events` | Append-only の履歴。全遷移について、Command、遷移前後の状態、`wait_reason`、Actor（`user` / `system` / `policy` と User の UUID）、理由、その時点の Step 名、`task_version` |

- `project_id`、`created_by`、`actor_id` は UUID だけを持ち、外部キーはありません。users と projects の Table がまだ存在しないためです（Table が入るときに外部キーを追加します）。
- `task_events` は DB の Trigger が UPDATE と DELETE を拒否します。Application からも履歴は書き換えられません。
- 列挙値は Text と CHECK 制約で保持します。Migration に値の一覧を直接書くため、値を増やすときは新しい Revision を追加してください。

### 同時実行と復元

- 状態を変える Command は 1 Transaction です。`tasks.version` を使った `UPDATE ... WHERE version = <読んだ値>` で更新するため、同じ Version を読んだ 2 つの Command は片方しか成功せず、もう片方は Event も含めて Rollback され `TaskConflictError` になります。
  呼び出し側が以前に見た Version を `expected_version` に渡すと、古い判断は後から届いても拒否されます。Step / Log / 試行状態の記録は Version を変えません。
- `TaskService.restore(task_id)` は DB だけから Snapshot（状態、current step、直近の Log、worktree / review / PR の状態、直近の Event）を作ります。1 つの Repeatable Read Transaction で読むため、同じ時点の値です。
  状態は Process のメモリに持たないので、Client が切断しても、Backend が再起動しても、別の Process が同じ値を返します。
- `TaskService(database, listeners=[...])` の Listener は Commit 後に、書き込まれた `TaskEvent` を受け取ります。Audit（PAW-025）の接続点です。Listener の失敗は Command を失敗させず、例外の型名だけを Log に残します。
  取りこぼしを避けたい Consumer は `task_events` を `seq` で読んでください（`TaskService.history(task_id, after_seq=...)`）。
- 実行中 Task の Runtime 状態（実行中 Process など）の復旧は、要件どおり V1 では保証しません。

## 認可（RBAC / Capability）と Audit

権限の判定は Backend だけが行います。Frontend の表示、Client が送る Header・Query・Body、Prompt、Model の出力は判定の入力になりません。
判定は型付きの入力（`Principal`、`Capability`、`Resource`）だけから決まる純粋関数で、**既定は拒否**です
（[設計](../../docs/SECURITY_RBAC_AUDIT.md)、[Tool 権限](../../docs/SECURITY_TOOL_PERMISSIONS.md)、[Decision 0004](../../docs/decisions/0004-rbac-capability-and-audit-policy.md)）。
呼び出す側は `Authorizer` を使います。判定だけを行う `policy.decide` などは Audit を書かないため、`paw_backend.authz` から公開していません。

**[Decision 0004](../../docs/decisions/0004-rbac-capability-and-audit-policy.md) は Proposed（Human の承認前）です。**
この節の Owner / Admin の権限、Agent への委任、Audit Mode、Fail-closed の選択は、承認されるまで暫定です。

| 層 | Role | 内容 |
| --- | --- | --- |
| System | Owner / Admin / User | Owner は Admin の全権限を含み、Admin は User の権限を含む。Owner 専用は Admin の追加・削除、Owner 権限の移譲、全体復旧、削除待ち User の復元、Backup 設定 |
| System | System | Backend 内部の Identity。人間はログインできず、Capability を持たない |
| Project | Manager / Contributor / Viewer | Viewer は閲覧、Contributor は Chat・Task・Repository 編集・Agent・PR、Manager は Member・Repository 追加・設定・Agent Policy・Project Memory・Archive / Delete 開始 |

- ID（User、Project、Repository、Agent など）はすべて UUID です。`uuid.UUID` か正規形の文字列（小文字、ハイフン区切り）だけを受け付け、`uuid.UUID` に正規化します。
  Audit には不透明な ID だけが残ります。
- Project の Role は Project ごとです。Manager である Project A の権限は Project B に及びません。
  System Role だけでは所属していない Project を閲覧・利用できません（Owner / Admin が持つ Project の権限は、Archive / Delete 開始などの管理操作だけです）。
- Project の状態（`Resource.project_state`）は必須です。Archived は読み取りと Archive の解除・Delete 開始だけ、Pending deletion は Archive の解除・Delete 開始だけ許可します。
- `Resource.repo_id` を持つ判定は、Repository の ACL Override が実装されるまで**常に拒否**します（Repository 単位で access denied にできる要件のため）。
- 自分のデータ（Chat、Workspace、GitHub、Memory）の Capability は、`Resource.owner_id` が本人のときだけ許可します。Owner でも他の User の Private Data は使えません。
- User の Role 変更・削除は `Authorizer.authorize_role_change(actor, target_user_id, target_role, new_role)` で判定します。`target_user_id` は必須です。
  Admin は他の Admin を管理できず（Admin の追加・削除は Owner のみ）、自分自身の Role は誰も変更できません。
  Audit の行は対象の User（`resource_kind="user"`、`resource_id`）を指し、変更前後の Role（`old_role`、`new_role`）を持ちます。
- **Owner の Role は `Authorizer.authorize_ownership_transfer(actor, new_owner_id, new_owner_role)` だけが動かします。**
  Role の変更では Owner に関わる操作を常に拒否します。移譲は 1 回の判定・1 件の Audit で、現在の Owner だけが、自分以外の User か Admin へ行えます
  （新しい Owner を持ち、移譲した人は Admin になる。Owner が 0 人にも 2 人にもならない。呼び出す側が 2 つの変更を 1 つの Transaction で適用します）。
- 判定の全体表は `paw_backend/authz/policy.py` にあり、`tests/test_authz_policy.py` が全 Role × 全 Capability を文字列で列挙して固定しています。

### Endpoint への適用

```python
from paw_backend.authz import Capability, ProjectState, Resource, require_capability


# Workspace 全体の操作
@router.get(
    "/admin/audit",
    dependencies=[Depends(require_capability(Capability.ADMIN_AUDIT_VIEW))],
)
async def audit_log(): ...


# Project の操作: Path Parameter と保存済みの状態から、Backend が Resource を作る（async でもよい）
async def project_of(connection: HTTPConnection) -> Resource:
    project = await load_project(connection.path_params["project_id"])
    return Resource.project(project.id, ProjectState(project.state))


@router.post(
    "/projects/{project_id}/tasks",
    dependencies=[Depends(require_capability(Capability.PROJECT_TASK_RUN, project_of))],
)
async def run_task(project_id: str): ...
```

`require_capability` は `Principal` を返すので、Endpoint の引数で受け取れます。WebSocket の Route にも使えます（Accept 前に閉じます）。
Capability は構築時に検査します（文字列は受け付けません）。Resource を作る関数が例外を出した場合は、500 ではなく拒否（と Audit）になります。

| 状況 | HTTP | WebSocket |
| --- | --- | --- |
| 認証されていない | 401 `unauthorized` | Close Code 1008 |
| 認証済みだが権限がない（Role 不足、Project の非 Member、他人のデータ、不正な ID、Project の状態） | 403 `forbidden`。Body は固定で、どの規則で拒否したかは含めない | 1008 |
| Audit を書けない Capability で Audit が書けない | 503 `service_unavailable` | 1013 |

`/api/v1` のすべての Route は `require_capability` で守るか、`tests/test_authz_routes.py` の公開一覧に理由付きで載せる必要があります（載せ忘れると Test が失敗します）。

**現在、認証は未実装（PAW-022）なので、`require_capability` を付けた Endpoint はすべて 401 を返します。**
既定の `UnauthenticatedProvider` が誰も認証しないためです。PAW-022 は `PrincipalProvider`（Request から有効な User の `Principal` を返す）を実装し、
`install_authz(app, ..., principal_provider=...)` で差し替えます。Provider は保存済みのデータから `Principal` を作ることが必須で、Client が申告した Role を使ってはいけません。
`/api/v1/events` の 2 つの Endpoint は現在も認証なしです（`TODO(PAW-022)`。公開一覧に載っています）。

### Agent と LLM

Agent の操作は、委任した人間の User の操作として判定します（`Authorizer.authorize_agent_action`）。

- 許可されるのは、**User 本人が許可される** かつ **`AgentGrant` に含まれる** かつ **委任可能（`delegable`）な Capability** の操作だけです（積集合）。
  Grant は権限を狭めるだけで、User の権限を超えることはありません。
- 委任できる Capability は許可リストです（Chat、Workspace、GitHub、Memory、PR、Shared Memory の閲覧、Project の閲覧・Chat・Task・Repository 編集・PR・Memory 利用）。
  `CapabilityInfo.delegable` には既定値がなく、Capability を追加するときは必ず決める必要があります。
  Project 設定・Repository 追加・Project Memory 管理を含む管理系、`admin.*`、`owner.*`、`shared_memory.manage`、Member / Agent Policy / Lifecycle は Grant に書いてあっても拒否します（自己権限昇格の禁止）。
  **`agent.use` と `project.agent.use`（Agent を起動する操作）も委任できません。** 子 Agent の Grant を親の部分集合として導く仕組み（PAW-032）ができるまで、Agent が自分より強い Agent を作れないようにするためです。
- `AgentGrant.project_ids` は**必須**です。Agent が触れる Project の集合か、明示的な `ALL_PROJECTS` を渡します（既定の「User の全 Project」はありません）。
  Project を限定した Grant は、その外の Resource（個人のデータを含む）に及びません。文字列 1 つを渡すと `TypeError` です。
- User の Principal は**判定のたびに** `PrincipalDirectory` から引き直します。Role を外す、User を削除する、といった変更は Agent の次の操作から効きます。
  Directory が User を返さない、例外を出す、`PAW_DATABASE_TIMEOUT_SECONDS` を超える、別の User を返す、または委任元 ID が正規の UUID でないときは、
  Audit を書いたうえで `delegator_not_active` で拒否します（エラーにはしません）。既定の `NoPrincipalDirectory` は誰も返さないので、User Store ができるまで Agent の操作は許可されません。
- Agent の判定は、Capability の Audit Mode に関わらず**常に `REQUIRED`** です（許可した読み取りも記録し、記録できなければ拒否します）。
- Grant は Backend が Task の範囲から作ります。保存済みの名前から作るときは境界用の `AgentGrant.from_names` を使い、Model が書いた文字列は使いません。
- 判定 API は `Capability` だけを受け取ります。文字列は（正確な名前でも）解釈せず `unknown_capability` で拒否します。名前から変換するときは `parse_capability` を使います。
  Audit にも入力の文字列は残しません。
- `Decision` は `bool(decision)` が `decision.allowed` です（`if await authorizer.authorize(...)` で拒否を通しません）。

Tool Broker の Capability（read / write / execute / network / credential-use / destructive）と Approval は [Tool Broker / Capability Policy](#tool-broker--capability-policy)（PAW-031）で、ここで決めた権限をさらに狭める方向にだけ働きます。

### Audit Event

判定を `AuditEvent` として `AuditSink` へ渡します。項目は `event_id`、`correlation_id`（Server が Request ごとに生成。同じ Request の判定で共通）、
`occurred_at`（Application の時計）、`actor_id`（人間の User。Agent の操作では委任元）、`actor_role`、`agent_id`、`action`（Capability 名）、
`resource_kind` / `resource_id` / `project_id` / `repo_id`、`decision`（`allow` / `deny`）、`reason`（固定の Reason Code）、
`old_role` / `new_role`（User の Role 変更のときだけ）、
`client_request_id`（Client が送った `X-Request-ID`。検証済みで 64 文字以内だが**偽造できる**ので、Request の識別には使わない）です。
Table は加えて `recorded_at`（Database の時計。INSERT 時に Trigger が `now()` へ上書きするので、INSERT できる Role も指定できない）を持ちます。Secret、Prompt、本文は持ちません。

**Audit Mode**（`CAPABILITIES[...].audit`）は Capability ごとに決まり、既定は `REQUIRED` です。

| Mode | 対象 | 記録 | Audit を書けないとき |
| --- | --- | --- | --- |
| `REQUIRED`（既定） | 上記以外のすべて（副作用のある操作、管理系、`admin.audit.view` / `admin.usage.view` も含む） | 許可も拒否も記録する | **許可を拒否に変える**（`audit_unavailable`、HTTP 503）。拒否は拒否のまま |
| `DENIED_ONLY` | 読み取り専用の許可リスト（`project.read`、`shared_memory.read`）だけ | 拒否だけを Best Effort で記録し、許可した読み取りは記録しない | 読み取りは止めない |

- **認証されていない Request の拒否は Database に書きません。** 誰でも作れる行になり、Table は削除できないためです。
  代わりに `INFO` の Log（Reason、Action、Resource の種類、`correlation_id`、`client_request_id`。例外の文は含めない）に出します。
- 保存先は `audit_events` Table（Migration `0025`）で、`PostgresAuditSink` が Request の Transaction とは別の短い Transaction で INSERT します。Test 用に `InMemoryAuditSink` があります。
- Audit の Write は `PAW_DATABASE_TIMEOUT_SECONDS` で打ち切り、失敗は Log（例外の型名だけ）に残します。
- 保存するのは不透明な UUID だけです。User の削除後の匿名化（`Deleted User`）は、Audit の行を書き換えず、User Store 側で ID と個人の対応を消して行います。

#### 追記専用について保証すること・しないこと

Table には 2 つの防御があります。

1. Trigger が UPDATE、DELETE、TRUNCATE を拒否します（`restrict_violation`。`session_replication_role = replica` でも有効）。
   別の Trigger が INSERT のとき `recorded_at` を Database の時計に上書きします。
2. 権限: `PUBLIC` から全権限を外し、`PAW_APP_DATABASE_ROLE` があればその Role に **INSERT と SELECT だけ**を与えます
   （Role は実在する必要があり、`public` や `pg_*` などは設定の時点で拒否します。`public` を通すと全員が INSERT できてしまうためです）。

| 構成 | 保証 |
| --- | --- |
| Migration を `PAW_MIGRATION_DATABASE_URL`（Owner の Role）で実行し、Application は `PAW_APP_DATABASE_ROLE` の Role で接続する（**推奨**） | Application の Role は行を追加・参照するだけで、UPDATE / DELETE / TRUNCATE、Trigger の無効化・削除、列の変更、Rule の作成、権限の付与ができない。Application の Bug や侵害では履歴を書き換えられない（PostgreSQL 18 の実 DB で Test 済み） |
| 上記の分離をしない（開発の既定。Migration と Application が同じ Role） | **Application の誤った DML から守るだけ。** Owner は Trigger を無効化・削除できる。起動時に WARNING を Log に出す |

どちらの構成でも**守れないもの**: Database の Superuser と Table の Owner（Migration の Role）による改ざん、Database Server や Backup の侵害、
改ざんの検知（Hash Chain などは実装していません）、
**INSERT できる Role による偽の行の追加**（`actor_id`、`decision`、`occurred_at` などを自由に決めて INSERT できる。`recorded_at` だけは Trigger が固定します）。
真の行を書くのは Application だけ、という信頼が前提です。Owner の Role の資格情報は Application に置かないでください。

Application 起動時に一度、接続 User の権限を確認し、**`WARNING`** を Log に出します（起動は止めません。起動時に PostgreSQL に接続できなければ確認しません）。
- Table の Owner か、UPDATE / DELETE / TRUNCATE 権限を持つ（追記専用を Application が外せる）。
- INSERT 権限がない（Audit を書けず、`REQUIRED` の操作がすべて 503 になる）。`PAW_MIGRATION_DATABASE_URL` を設定して `PAW_APP_DATABASE_ROLE` を設定しなかった場合が典型で、
  Migration も、その構成であることを WARNING で Log に出します。

**`downgrade` は Table ごと監査履歴を破棄します。** 開発・Test 用で、本番では実行しないでください。

#### 残っているリスクと既知の制限

- 許可した読み取り（`DENIED_ONLY`。人間の `project.read`、`shared_memory.read`）は記録しません。誰が何を読んだかは Audit から分かりません（Agent の読み取りは記録します）。
- 認証済みの User の拒否は、1 回ごとに 1 行を書きます。回数制限は PAW-022（Rate Limit、Lockout）までありません。未認証の拒否は Log だけです。
- 保存期間・Partition・古い行の退避は未実装です（Table は削除できないため、行数は増え続けます）。
- `Scope.SELF` の Capability（`chat.use`、`memory.use` など）は `Project` の状態と Member 資格を見ません
  （たとえば Pending deletion の Project の Chat、Member から外された後の Memory）。Project との関係のモデル化は PAW-026 で行います。
- `tests/test_authz_routes.py` が調べるのは `/api/v1` の Route だけで、FastAPI の内部（`effective_route_contexts`）に依存します。
- `create_app` は既定の Provider と Directory を組み込みます。PAW-022 が `install_authz` を呼んで差し替えるまで、全 Endpoint が 401 です。
- 重要操作の Step-up 認証の項目は Audit にありません（PAW-023 で追加します）。
- Migration `0025`、`0032`、`0040` は今は同じ `down_revision="0001"` を持ちます。統合時に 1 本の鎖へつなぎ直します。

## Tool Broker / Capability Policy

[PAW-031](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/27) で実装しました（`paw_backend/tools/`、Migration `0031`）。
設計は [Tool 権限](../../docs/SECURITY_TOOL_PERMISSIONS.md) と [要件](../../REQUIREMENTS.md) の「Tool Broker / Capability Policy / Secret Isolation」「Tool approval boundary」に従います。
**HTTP の Endpoint はありません**（承認の Endpoint は認証済みの Session が必要なため PAW-022 以降）。`create_app` にも組み込んでいません。呼び出すのは後続の Orchestrator（PAW-034）と API です。
Tool の実装、Sandbox、Task Budget（PAW-033）、Step-up 認証（PAW-023）は含まず、それぞれ差し込み口（Protocol）だけを持ちます。

Agent の Tool 呼び出しは `ToolBroker.request(call)` を通り、`ALLOW` / `NEEDS_APPROVAL` / `DENY` と固定の理由コードを返します。
**Broker は何も実行しません。** 許可された呼び出しの実行は、注入する `ToolExecutor` を呼ぶ `ToolRunner.run` だけが行います（Broker に実行の入口はありません）。
Broker は [認可（PAW-025）](#認可rbac--capabilityと-audit) の判定に**上乗せして狭めるだけ**で、置き換えも拡張もしません。Approval で委任不可の操作が許可されることもありません。

```python
broker = ToolBroker(
    registry,
    authorizer,
    PostgresApprovalStore(database),
    audit_sink,
    budget=budget_provider,
)  # 既定は Budget 不明 = 拒否
runner = ToolRunner(broker, executor)  # executor: ToolExecutor
outcome = await runner.run(
    ToolCall(name, arguments, task_context)
)  # 毎回、新しく判定する
```

`ToolCall` の `tool` と `arguments` だけが Model の出力です。Task、委任元 User、`AgentGrant`、Task Scope は Backend が `TaskContext` に解決します。

### Capability と Approval level

Tool は自分の Capability class を `ToolSpec` で宣言します（Backend のコードだけが宣言し、生成後は変更できません）。
Level は `ToolPolicy` が「Capability class × Environment × Scope の状態」から決め、Tool の Level は class ごとの Level のうち**最も厳しいもの**です（`ToolSpec.min_level` は引き上げだけができ、下げられません）。
表にない組み合わせは `DENY` です（既定拒否）。`DEFAULT_TOOL_POLICY` の 36 マスは `tests/test_tools_policy.py` が 1 マスずつ文字列で固定しています。

| Capability | 範囲内（Project-local） | 範囲外の Host | 範囲外（Path / Project / Credential） | Host 全体の環境 |
| --- | --- | --- | --- | --- |
| `read` | `AUTO` | `DENY` | `DENY` | `AUTO` |
| `write` | `SCOPED_AUTO` | `APPROVAL`（Task Scope を超える外部 write） | `DENY` | `APPROVAL` |
| `execute` | `SCOPED_AUTO` | `DENY` | `DENY` | `APPROVAL` |
| `network` | `SCOPED_AUTO` | `APPROVAL` | `DENY` | `SCOPED_AUTO`（範囲外の Host は `APPROVAL`） |
| `credential-use` | `SCOPED_AUTO` | `DENY` | `DENY` | `STRONG_APPROVAL` |
| `destructive` | `APPROVAL` | `DENY` | `DENY` | `STRONG_APPROVAL` |

- 例: `web.fetch`（read + network）は範囲外の Host なら `DENY`、Issue 作成（write + network）は範囲外の Host なら `APPROVAL`、`git.merge` は `min_level=STRONG_APPROVAL` で `STRONG_APPROVAL`。
- **Task Scope の範囲外は Policy で許可できません。** `ToolPolicy` は、範囲外（`out_of_scope`）を `DENY` 以外にする表を作れません（`ValueError`）。Host だけは、Task の Host を超える外部 write を `APPROVAL` にします（要件の「通常 Task scope を超える外部 write」）。
- Host 全体の環境（package、service、firewall、mount）を変える Tool は `environment=Environment.HOST` を宣言します。宣言は Tool 側で、呼び出しでは選べません。
- **Credential plaintext の取得は常に `DENY` です**（下の Credential）。

要件の表との違い（いずれも**より厳しい方向**）:
build / test / lint などの実行は `SCOPED_AUTO` としています（実行は Project のコードを走らせ、書き込みもできるため。人の確認が要らない点は `AUTO` と同じです）。
Task Scope 内の一時ファイルの削除も `destructive` として `APPROVAL` にしています（`SCOPED_AUTO` にするには「一時ファイルだけを消す」Tool を別に宣言する必要があり、まだ決めていません）。

### 判定の順序

前の段階が通った場合だけ次へ進み、各段階は**狭める方向にしか**働きません。

1. 呼び出しの形。`ToolCall` でなければ `invalid_call`。Tool 名は登録済みの名前と**完全一致**だけ（大文字小文字、空白、Zero-width、Unicode の見た目が近い文字は別の名前）。なければ `unknown_tool`。
2. `returns_credential_plaintext` の Tool は `credential_plaintext_denied`。引数を Tool の宣言（`ArgumentSpec`）と照合します。宣言にない引数、足りない必須の引数、型の違い（`"true"` は bool でなく、`True` は int でない）、長さや範囲の超過は `invalid_arguments`。Path / Host / URL / Project / Credential handle は正規化して `invalid_target` または下の理由で拒否します。文字列に Credential の平文があれば `credential_plaintext_in_arguments`。
3. 対象を Task Scope と比べ（Symlink を解決）、Level を決めます。`DENY` なら `path_out_of_scope` / `host_out_of_scope` / `project_out_of_scope` / `credential_out_of_scope` / `policy_denied`。
4. 認可（PAW-025 の `authorize_agent_action`）。委任元 User の権限と `AgentGrant` の積集合で、拒否は `authz_denied`（`authz_reason` に PAW-025 の理由）。Authorizer が失敗または想定外の答えなら `authz_unavailable`。
5. Task Budget（`BudgetProvider`）。超過は `budget_exceeded`、不明は `budget_unknown`、Provider の失敗・Timeout・想定外の答えは `budget_unavailable`。
6. `AUTO` / `SCOPED_AUTO` は `ALLOW`（`auto` / `scoped_auto`）。`APPROVAL` / `STRONG_APPROVAL` は下の Approval。

判定は `AuditSink` へ記録します（下の Audit）。**記録できない `ALLOW` は `DENY`（`audit_unavailable`）になります。**
Approval の要求（`NEEDS_APPROVAL`）を作る前に、Path・認可・Budget の判定が済んでいるため、実行できない呼び出しの承認要求は作りません。

### Tool Registry

`ToolRegistry` は起動時に一度だけ `ToolSpec` の一覧から作り、追加・置換・削除の方法がありません。`ToolSpec` は Tool 名、Capability class、対応する PAW-025 の Capability（必須）、引数の宣言、Environment、`min_level`、`requires_budget`（既定 `True`）を持ちます。
宣言の整合性は生成時に検査します。**Host / URL の引数を持つ Tool は `network`、Credential handle の引数を持つ Tool は `credential-use` でなければならず**、Project-local の `write` / `destructive` は触る対象（Path / Host / URL / Project の引数）を宣言しなければなりません
（対象のない書き込みは、常に「範囲内」に見えるため）。Tool 名は `unknown` と `approval*` を使えません（Audit の action と衝突するため）。

### Task Scope と正規化の契約

`TaskScope` は、Task が触れる Path の Root（先頭が相対 Path の基準）、Host、Project（Project の状態つき）、使える Credential handle を持ちます。比較は**正規化した形どうしの完全一致**で、Prefix や Wildcard の一致はありません（`scope.py` の docstring が契約の正本です）。

- **Path**: 絶対 Path、または先頭の Root からの相対。`.` と空の Segment は取り除き、**`..`（と `...`、空白や点だけの Segment）は畳み込まず拒否**します（Symlink の先の `..` は File System と字句上で食い違うため）。
  `\`、`~` で始まる名前、`%2e` `%2f` `%5c`、制御・書式・Separator 文字、NFKC で変わる文字（全角の `．．／`、合字、分解形）、1024 文字超は拒否。
  範囲内の判定は `path == root or path.startswith(root + "/")`（`/srv/w/task-evil` は `/srv/w/task` の外）。**大文字小文字は区別します**（大文字小文字を区別しない File System では誤拒否になるだけで、逸脱にはなりません）。
- **Symlink**: `PathResolver`（既定は `os.path.realpath` を別 Thread で）で Path と Root を解決し、解決後の Path が解決後の Root の中にあることを確認します。解決が失敗・Timeout・不正な答えなら `path_resolution_unavailable`。
  **これは呼び出しの前の確認です。** 確認の後に File System が変わる（TOCTOU）ため、Executor は Root の外へ Symlink をたどらずに開く必要があります（`openat2` の `RESOLVE_BENEATH` など）。
- **Host / URL**: ASCII のみ（国際化 Domain は `xn--`）、小文字化、末尾の `.` を 1 つ除去、Label を検査。数字で終わる Host は厳密な Dotted-quad の IPv4 だけ（`127.1`、`0x7f.1`、`2130706433` は拒否）。
  URL は `http` / `https` の既定 Port だけで、ユーザー情報（`user@`）、`\`、空白は拒否します。Scope の Host と**完全に一致**したときだけ範囲内です（`github.com.evil.com` も `gist.github.com` も外）。
- **Project** は正規形の UUID、**Credential handle** は `cred_` + 32 桁の 16 進だけ。

### Credential

- **平文は Agent の引数にも結果にも入れません。** 使うときは不透明な handle だけで、handle を解決して Credential を付けるのは Executor（Backend 内部）です。Tool の引数が handle であり、その handle が Task の使える handle に入っていることを Broker が確認します（`credential_out_of_scope`）。
- 引数の文字列に Credential の平文があれば `DENY` します（GitHub Token、`github_pat_`、`sk-` の Key、AWS Access Key ID、Slack Token、JWT、Bearer / Basic、PEM の秘密鍵、`user:password@` 付きの URL）。Zero-width 文字や全角文字で隠した形も検出します。
  ソースコードで普通に出る `password = "..."` は引数では拒否しません（Agent がコードを書けなくなるため）。
- **結果は、返す前と Log へ出す前に Redact します。** 検出した Credential、`password` / `token` / `api_key` / `secret` / `authorization` などの Key の値、JSON のデータでない Object（`repr` に何が入るか分からないため）は固定の Marker（`[REDACTED]` / `[UNSUPPORTED]`）になります。
- 平文を返す Tool（`returns_credential_plaintext=True`）は、登録しても**常に** `credential_plaintext_denied` です。Approval を渡しても変わりません。
- **限界:** 検出は形のわかる Format だけの Best Effort で、すべての Secret を見つけることはできません。本来の防御は、Credential を Agent の Context に入れない構造（handle のみ）です。

### Approval

`APPROVAL` / `STRONG_APPROVAL` の呼び出しは、まず `NEEDS_APPROVAL`（`approval_required` / `strong_approval_required`）を返し、**Approval の要求を作ります。** 同じ呼び出しの要求がすでに開いていれば、それを返します（`approval_pending`）。
人が承認したあと、`request(call, approval_id=...)` で使います。

| 段階 | 内容 |
| --- | --- |
| Hash | `call_hash` は Tool、**正規化した**引数、Task、Requester（User と Agent）の SHA-256。同じ呼び出しの別の書き方は同じ Hash、引数を 1 つ変えれば別の Hash |
| 単回 | 承認は 1 回だけ使えます（実行が失敗しても消費済みです）。`UPDATE ... WHERE status = 'approved' AND expires_at > now AND`（Task、Agent、User、Tool、Level、Hash がすべて一致）の 1 文で消費するため、同時に何個の呼び出しが来ても 1 つだけが成功します（再利用は `approval_already_used`） |
| 期限 | 作成から `approval_ttl`（既定 1 時間、1 分〜24 時間）。期限ちょうども期限切れです。期限切れは `approval_expired` |
| 別の呼び出し | 引数・Tool・Task・Agent・User・Level のどれかが違えば `approval_mismatch`（承認は消費されません） |
| 承認できる人 | Agent が働いている **User 本人だけ**（`ApprovalService.approve / reject`、引数は人間の `Principal`）。Agent 自身の ID は `self_approval`、他の人は Admin / Owner でも `not_authorised`。DB の CHECK 制約も、承認者が委任元 User であること、Agent が User と別であることを保証します |
| `STRONG_APPROVAL` | 承認のとき `StepUpVerifier.verify(user_id, approval_id)` が**明示的な `True`** を返す必要があります（PAW-023 が実装）。Verifier がない、`False`、例外、Timeout、`True` 以外の答えは `step_up_required` で、承認は保留のままです |
| 使うとき | 認可・Scope・Budget を**もう一度**判定します。承認は権限を広げません。拒否された使用は承認を消費しません |

`ApprovalService` は Broker と**別の Object**です。Agent の Runtime へは Broker（または Runner）だけを渡し、`ApprovalService` は渡さないでください（渡さなくても上の規則が守られますが、それが最初の防御です）。

**永続化（決定）: PostgreSQL に保存します。** 理由: 承認は Task が `waiting`（承認待ち）の間、Backend の再起動をまたいで残る必要があり（要件は Client の切断後も状態を保持）、
単回・期限・二重承認の保証は複数の Process が同じ行を更新できる Database でこそ成り立つためです。Audit Sink だけに書く案は、状態の読み出しも排他もできないため採りませんでした。

| Table | 内容 |
| --- | --- |
| `tool_approvals` | 承認の現在の状態（`pending` / `approved` / `rejected` / `consumed` / `expired`）、Task・Project・Agent・User、Tool、Level、`call_hash`、承認者に見せる対象（正規化した Path / Host / Project。**内容は持たない**）、期限 |
| `tool_approval_events` | Append-only の履歴（`requested` / `approved` / `rejected` / `consumed` / `expired`）。Trigger が UPDATE と DELETE を拒否（`task_events` と同じ） |

- 開いている承認は Exact な呼び出しごとに 1 つ（`call_hash` の Partial Unique Index）。Task、Project、Agent、User の ID に Foreign Key はありません（`task_events` と同じ方針）。
- 状態の変更と履歴の行は同じ Transaction です。期限切れは、変更しようとした時に `expired` へ移し、履歴に残します。
- `ApprovalListeners`（`ToolBroker(listeners=[...])`、`ApprovalService(listeners=[...])`）は、要求・承認・却下・消費が保存された**後**に `ApprovalEvent`（ID と Enum だけ）を受け取ります。Task を `waiting` にする Orchestrator の接続点です。Listener の失敗や遅延は承認を失敗させず、例外の型名だけを Log に残します。
- テスト用に `InMemoryApprovalStore`（同じ規則。件数の上限つき。本番用ではありません）があります。

### Audit

すべての判定を既存の `AuditSink` に記録します。**ID と Enum だけ**で、引数、対象、結果、Model が書いた文字列は入りません。

| 項目 | 値 |
| --- | --- |
| `action` | `tool.<登録済みの Tool 名>`。登録されていない名前は `tool.unknown`（名前は保存しません）。承認は `tool.approval.approve` / `tool.approval.reject` |
| `resource_kind` / `resource_id` | Tool の呼び出しは `task` / Task の ID、承認は `tool_approval` / 承認の ID |
| `actor_id` / `agent_id` | 委任元 User / Agent（承認の記録では承認した人、`agent_id` なし） |
| `decision` / `reason` | `allow` / `deny` と `BrokerReason` の値。`AuditEvent.decision` は 2 値のため、承認待ちは `deny` + `approval_required`、承認を使った実行は `allow` + `approval_consumed`。実行後に `executed` / `execution_failed` の行を追加 |
| `correlation_id` | 呼び出しごと。同じ呼び出しの PAW-025 の認可の行と共通 |

`ToolCall` でない入力（帰属する Task も User も Agent もない）は、Audit の行を作らず Log（型を含まない固定の文）だけに残します。

Tool の実行を伴う記録（許可と実行後）は Fail-closed で、許可を記録できなければ拒否します。承認の承認・却下は外部の状態を変えないため、記録の失敗は Log（型名だけ）に残し、承認は有効なままです（変更と同じ Transaction の `tool_approval_events` が消えない記録です）。

### 差し込み口

| Protocol | 実装 | 既定 |
| --- | --- | --- |
| `ToolExecutor.execute(invocation)` | 各 Tool の実装（別 Issue） | なし（`ToolRunner` に必須） |
| `BudgetProvider.check / charge` | PAW-033 | `FailClosedBudgetProvider`（予算なし = 予算が必要な Tool は拒否）。`check` は何も消費せず、同時の呼び出しは上限を少し超えうる。厳密な上限には PAW-033 が原子的な予約を追加する |
| `StepUpVerifier.verify` | PAW-023 | `FailClosedStepUp`（Step-up の承認はできない） |
| `PathResolver.resolve` | Deployment | `RealpathResolver`。`LexicalPathResolver` は Symlink のない環境の Test 用 |

Adapter は Broker / Runner / Service の生成時に検査します（Async Method か、必要な引数の数か）。間違った Adapter は生成時に `TypeError` です。

### 既知の制限と判断

- **承認が広げるのは Level だけです。** 委任できない Capability（`admin.*` など）を持つ Tool は、承認があっても `authz_denied` です（PAW-025 の許可リストのまま。Decision 0004 が PAW-031 に残した点として、この実装は「Approval で委任不可の操作を Agent に許す仕組みは作らない」を選んでいます）。
- 承認できるのは委任元 User だけで、Admin / Owner が他の User の Task を承認する仕組みはありません。
- 外部 write の許可は `TaskScope.hosts` だけで表しています（Issue 作成や PR 作成といった「目的」の単位ではありません）。Web の読み取りも、Task の Host に含まれない Host は `DENY` です。
- 承認の対象として見せるのは正規化した Path / Host / Project です。File の内容や Command の引数の全文は承認 UI に出せません（Audit と同じく内容を保存しないため）。
- Approval の期限は 1 つです（承認してから使うまでの猶予は別にありません）。承認が遅れると、使える時間は残りだけです。
- `ToolRunner` は Task Cancel などの `CancelledError` では実行後の記録を書きません（Cancel は例外のまま伝わります）。
- `check` と `charge` の間の競合、Symlink の確認と使用の間の競合（TOCTOU）、Credential 検出が Best Effort であることは上に書いたとおりです。
- Model の出力から `ToolCall` を作る Adapter は JSON を `benchmarks/json_input.decode_json` と同じ厳密さ（重複 Key、`NaN` を拒否）で読んでください。Broker は Mapping を受け取り、Key と値の型を上の規則で検査します。

## Memory / Conversation Schema

[PAW-040](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/34)（Revision `0040`）で実装した Schema です。
設計は [Memory Architecture](../../docs/MEMORY_ARCHITECTURE.md) と [要件](../../REQUIREMENTS.md) の Memory の節に従います。
Repository / Service は含みません。

| 層 | Table | 内容 |
| --- | --- | --- |
| Raw Conversation | `conversations`、`messages` | 発言、Tool 結果、Agent 結果、Task の経緯。無期限に保持し、LLM Context へ全文は入れない |
| Session state | `session_states` | Conversation ごとの要約と作業状態（1 Conversation に 1 行） |
| Long-term Memory | `memories`、`memory_versions`、`memory_relations`、`memory_sources`、`memory_embeddings` | 確定した知識。Version、関係、出典、Embedding |

3 つの層は別の Table で、Foreign Key でつながるのは出典（`memory_sources`）だけです。
Conversation を消しても Memory と他の出典は残り（`ON DELETE SET NULL`）、Session state と Message は一緒に消えます。

**Scope と ACL。** 各 Version が `scope`（`user` / `project` / `repo` / `shared`）を持ち、Scope に対応する ID を 1 つだけ持ちます
（`owner_user_id` / `project_id` / `repo_id`、`shared` は無し）。CHECK 制約が組み合わせを強制します。
権限の判定は SQL で行います。`paw_backend.memory.acl` の `readable_memory_versions(principal)` を、
`memory_versions`（と、それを Join する `memory_embeddings`、`memory_sources`、`memory_relations`）を読む全ての Query に付けます。
Vector 検索でも、順位付けの前に付けるため、見えない行が順位に入ることはありません。
`Principal` は User の実効的な権限（読める Project と Repo の ID）で、RBAC と Membership から Backend が決めます。
Repo は既定で Project の権限を継承し、Repo 単位の ACL override で外された Repo は `repo_ids` に入れません。
Memory ごとの権限の写しは持ちません（Member の変更で古くなり、漏れの原因になるため）。
Scope 別の Index が `status = 'active'` の絞り込みとあわせて ACL 条件を支えます。
Raw Conversation は所有者だけが読めます（`readable_conversations`）。Admin にも本文は見せません。

**Version と履歴。** 編集は新しい `memory_versions` の行です。旧 Version は消さず `status`（`active` / `superseded` / `deprecated` / `history`）を変えます。
1 つの `memories` に `active` は最大 1 行（Partial Unique Index）で、`(memory_id, version_number)` の Unique が楽観ロックを兼ねます。
`memory_relations` が Version の関係（`supersedes`、`extends`、`conflicts_with`、`confirmed_from`、`revalidated_from`、`merged_from`）を新しい側から古い側へ持ちます。
自分自身への関係と、同じ Version を複数の Version が supersede することは DB が拒否します。
Scope を広げる編集は新しい Version で行うため、旧 Version は元の Scope のまま非公開です。
`confirmation_state`（`observed` / `inferred` / `confirmed` / `rejected`）、`freshness_policy`（`permanent` / `revalidate` / `repo_commit` / `expiring` / `session_only`）と、
方針ごとの必須項目（`verified_at`、`revalidate_after`、`commit_sha`、`expires_at`）、`actor`、`change_reason` も Version が持ちます。

**User / Project / Repo の ID は Foreign Key なし。** User、Project、Repo の Table はまだありません（PAW-021 / 026 / 027）。
`owner_user_id`、`project_id`、`repo_id`、`actor_user_id` は素の UUID Column で、DB は存在を確認しません。
Backend は検証した ID だけを書いてください。Table ができた後の Migration で Foreign Key を追加できます。
Task、Repo 解析、Project Decision の出典も、Table がないため `memory_sources.source_ref` の不透明な文字列です。

**pgvector。** Migration が `CREATE EXTENSION IF NOT EXISTS vector` を実行します（Migration の Role に権限が必要。管理者が先に作成済みでもよい）。
`memory_embeddings` は `(memory_version_id, embedding_model_id)` が Key で、`embedding` は **次元を固定しない** `vector` です。
Embedding Model と次元は Benchmark（PAW-019）で決めるため、まだ決めていません。`dimensions` と実際の次元は CHECK で一致させます。
次元の異なる Vector 同士の距離は計算できないため、近傍検索は先に 1 つの `embedding_model_id` に絞ります。
ANN Index（HNSW / IVFFlat）はまだありません。Model が決まった後に PAW-043 が追加します。

Model と Migration の一致は Test が検証します（Alembic の autogenerate の差分が空であること、Model から作った Schema と Migration の Catalog が同じであること）。
制約名は `paw_backend.db.Base` の命名規則に従います。

## 依存 Package

依存は `pyproject.toml` で完全一致に固定しています。
CI は pre-commit の専用環境で Test を実行するため、同じ Version を
[.pre-commit-config.yaml](../../.pre-commit-config.yaml) の `additional_dependencies` と
[requirements-ci.txt](../../.github/requirements-ci.txt) にも書きます。
3 か所の一致と、Backend が import する Package の宣言漏れは
[test_dependency_pins.py](../../.github/scripts/test_dependency_pins.py) が検査します。
依存を追加・更新する場合は 3 か所を同時に変更してください。

## 今後の Issue

[PAW-021](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/18)（Owner Setup）、PAW-022（Login / Session）、
RBAC（PAW-025）、Task Lifecycle（PAW-032）、Tool Broker（PAW-031）、Memory Schema（PAW-040）は、この Skeleton の上に実装済みです。
Memory の保存・整理・検索は PAW-041 以降で、Memory Schema の上に実装します。
受け入れ基準は [Implementation Backlog](../../docs/IMPLEMENTATION_BACKLOG.md)、
実装時に選択できる事項は [Requirements Freeze Review](../../docs/REQUIREMENTS_FREEZE_REVIEW.md) を参照してください。
