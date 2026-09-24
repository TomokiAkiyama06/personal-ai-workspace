# Backend

Personal AI Workspace の Core Backend です。
[PAW-020](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/17) で、後続の Issue が載る最小の Application Skeleton を実装しました。
Login と Session はまだ実装していません（PAW-022 以降）。
RBAC と Audit（PAW-025）、Task の Lifecycle と永続化（[PAW-032](#agent-task-lifecycle)、HTTP の Endpoint はまだありません）、Task Queue・Budget・Loop 検知（[PAW-033](#task-queue--budget--loop-検知)）、Tool Broker と Capability Policy（[PAW-031](#tool-broker--capability-policy)、HTTP の Endpoint はまだありません）、Memory の PostgreSQL Schema（[PAW-040](#memory--conversation-schema)）、
最小の `users` Table と Owner の初期設定・復旧のコマンド（[PAW-021](#owner-の初期設定と復旧)）を実装済みです。Memory の保存・整理・検索の処理は PAW-041 以降です。

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
├─ migrations/             # env.py と Revision（0001 は空の Baseline、0021 は users / setup_tokens、0031 は Tool Approval、0033 は Queue / Budget / Loop、0040 は Memory Schema）
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
│  ├─ identity/            # 最小の users、One-time Token。`redeemer.py` は Web 側、`operator.py`（Owner の作成・Token の発行）は cli だけが使う（PAW-021）
│  ├─ cli/                 # server-local の管理コマンド `python -m paw_backend.cli`（PAW-021）
│  ├─ tasks/               # Agent Task の状態遷移と永続化（PAW-032）
│  │  └─ queueing/         # Task Queue、Budget、Loop 検知、Escalation の判断（PAW-033）
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
| `PAW_APP_DATABASE_ROLE` | なし | Web の Application が接続する PostgreSQL の Role 名（英数字と `_`、63 文字まで。`public`、`pg_` で始まる名前、`postgres` などの予約名は拒否）。Audit Table の Migration が、実在するこの Role に INSERT と SELECT だけを与える（存在しなければ Migration が失敗する）。Migration `0021` は `users` / `setup_tokens` について Token の使用に必要な最小限の権限だけを与える |
| `PAW_DATABASE_READINESS_CACHE_SECONDS` | `1` | Readiness の結果（失敗を含む）を再利用する秒数。`0` で再利用しない |
| `PAW_OPERATOR_DATABASE_URL` | なし | Owner の管理コマンド（`python -m paw_backend.cli`）の接続先（Token を作れる Role）。未設定のときだけ `PAW_DATABASE_URL` を使い、警告する。[Owner の初期設定と復旧](#owner-の初期設定と復旧) |
| `PAW_OPERATOR_DATABASE_ROLE` | なし | 上の Role 名（`PAW_APP_DATABASE_ROLE` と同じ検証）。Migration `0021` が、実在するこの Role に管理コマンドの権限を与える |
| `PAW_SETUP_TOKEN_TTL_SECONDS` | `1800` | Owner の Setup / Recovery Token の有効期間（60〜14400 秒） |
| `PAW_SETUP_TOKEN_MAX_ATTEMPTS` | `5` | 1 つの Token に許す試行回数（1〜20）。使い切った Token は無効になる |
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
  - 空き枠の確認と確保は 1 つの同期処理（`EventBus.reserve()`）で行います。最後の 1 枠を同時に争っても、Stream になるのは 1 件だけです。
  - 負けた接続は、Response の Header を送る前に SSE が 503（`event_capacity_reached`）、WebSocket が Close Code 1013 になります。
  - 枠は、Stream の終了、切断（Stream の開始前を含む）、エラー、キャンセルのどの場合も必ず解放されます。

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
Queue、Budget、Loop 検知（PAW-033、[別の節](#task-queue--budget--loop-検知)）と DAG Orchestration（PAW-034）は、この節の対象外です。

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
- **Cancel（graceful）**: Task を終了します。branch / worktree / 途中成果は保持します（削除は別の操作）。実行中の Step は Worker が自分で閉じます（`finish_step`）。Worker が来ないまま Restart されたときは、Restart が旧試行の Step を `interrupted` にします。
- **Stop Now（immediate）**: 緊急停止です。Cancel と同じ `cancelled` になりますが、実行中の Step を即座に `interrupted` にし、停止理由と中断した Step を Task Log と履歴 Event へ残します。Step が既に終わっていた場合（Worker が先に閉じた場合を含む）は何も中断していないため、Step 名を記録せず、Log も `Stop Now: no step was running` とします（Fail も、自分が終わらせた Step だけを Event に記録します）。実行中に何かが動きうる状態（running / waiting / evaluating）だけが対象で、queued と paused には Cancel を使います。成果物は削除しません。
  Cancel と Stop Now の違いは Event の Command（`TaskEvent.interruption` が `graceful` / `immediate`）で判別できます。
- **Retry**: `failed` の Task を、失敗した Step から同じ試行（同じ branch / worktree / Log）で再実行します。`queued` へ戻り、`retry_count` を 1 増やし、Agent / Model を切り替えられます（履歴 Event の `detail` に旧値と新値を残す）。
- **Restart**: `failed` または `cancelled` の Task を、元の `starting_commit` と Task `input` から最初からやり直します。試行番号（`attempt`）を 1 増やし、新しい branch / worktree / Review / PR の状態を持つ空の試行を作ります。旧試行は `task_attempts` と Step・Log に残り、`TaskSnapshot.previous_attempts` から見えます。

### 永続化

| Table | 内容 |
| --- | --- |
| `tasks` | 現在の状態、`wait_reason`、`version`、試行番号、`retry_count`、Agent / Model、`starting_commit`、`input`（Restart の基準） |
| `task_attempts` | 試行ごとの branch / worktree / head commit、Review 状態、Evaluator 結果、PR の番号・URL・状態 |
| `task_steps` | Step の実行記録。試行内で最新の行が current step。試行内で `running` は高々 1 つ（Partial Unique Index） |
| `task_tool_invocations` | Step が呼んだ Tool の実行状態（下記）。ID、Tool 名、状態（`started` / `succeeded` / `failed` / `interrupted`）、開始・終了時刻だけを持つ |
| `task_logs` | 試行ごとの Log（`debug` / `info` / `warning` / `error`） |
| `task_events` | Append-only の履歴。全遷移について、Command、遷移前後の状態、`wait_reason`、Actor（`user` / `system` / `policy` と User の UUID）、理由、その時点の Step 名、`task_version` |

- `project_id`、`created_by`、`actor_id` は UUID だけを持ち、外部キーはありません。projects の Table がまだ存在せず、`users`（PAW-021、Revision `0021`）は Migration の順序が統合後に決まるためです（両方が揃った後の Revision で外部キーを追加します）。
- `task_events` は DB の Trigger が UPDATE と DELETE を拒否します。Application からも履歴は書き換えられません。
- **Application の Role の権限**（Role を分ける構成、`PAW_APP_DATABASE_ROLE`）: Migration `0032` は、すべての Table に [`grant_app_privileges`](#migration-は-application-の-role-に権限を与えるcontributor-向けの規則) で `TaskService` が必要とする最小の権限だけを与えます。DELETE はどこにも与えません（`PUBLIC` の権限は外します）。

  | Table | Application の Role の権限 |
  | --- | --- |
  | `task_events`、`task_logs` | SELECT、INSERT だけ（履歴と Log は追記のみ。UPDATE / DELETE / TRUNCATE は `permission denied`） |
  | `tasks` | SELECT、INSERT、UPDATE は `state`、`wait_reason`、`agent`、`model`、`attempt`、`retry_count`、`version`、`updated_at` の列だけ（`project_id`、`created_by`、`title`、`input`、`starting_commit` は変更できない） |
  | `task_attempts` | SELECT、INSERT、UPDATE は `branch`、`worktree_path`、`head_commit`、`review_status`、`evaluation_result`、`pr_number`、`pr_url`、`pr_state`、`updated_at` の列だけ |
  | `task_steps`、`task_tool_invocations` | SELECT、INSERT、UPDATE は `status`、`finished_at` の列だけ（Step 名や Tool 名は変更できない） |

  行の Lock（`SELECT ... FOR NO KEY UPDATE`）には UPDATE 権限が要るため、Lock する Table は列単位の UPDATE を持ちます。主キーは UUID か Identity で、Sequence の権限は要りません。
  `tests/test_task_grants.py` が、Migration を実際にこの構成で実行し、Superuser でない Role で `TaskService` の Test（状態遷移の全組み合わせ、Step、Tool、Log、Restore など）を実行します。あわせて、この表と Role の権限が一致すること、履歴の書き換えと削除、Task の識別情報の変更、Schema の変更が拒否されることを確認します。
- 列挙値は Text と CHECK 制約で保持します。Migration に値の一覧を直接書くため、値を増やすときは新しい Revision を追加してください。

### 同時実行と復元

- 状態を変える Command は 1 Transaction です。Command と、Step / Tool / 試行状態を書く操作は、どれも最初に Task 行の Lock（`SELECT ... FOR NO KEY UPDATE`）を取り、それから状態や最新の Step を読みます。同じ Task への書き込みは 1 つずつ実行されるため、次のことが保証されます。
  - Stop Now / Fail は、並行する `begin_step` が Commit しようとしている Step を見落としません（Lock を待ち、Commit 後の Step を中断・失敗にします）。
  - Task が終了した後に Step や Tool の呼び出しを開始できません。`begin_step` は Lock を取った後に状態を読み直します。
  - `complete` は、Step が実行中の間は `TaskStepError` になります（Step が残ったまま Completed になりません）。
  - 例外は graceful な Cancel です。Cancel の時点で既に実行中だった Step は、Worker が `finish_step` で閉じるまで `running` のままです。Cancel の後に新しい Step を始めることはできません。
  `tasks.version` を使った `UPDATE ... WHERE version = <読んだ値>` は、これに加えた安全策です。
  呼び出し側が以前に見た Version を `expected_version` に渡すと、古い判断は Lock を待った後でも `TaskConflictError` で拒否されます。`expected_version` を渡さない Command は、待った後の最新の状態で判定されます。
- Worker の記録（Step、Tool、Log、試行状態）は、担当する試行を明示します。`begin_step(task_id, name, attempt=...)` は `StepInfo`（`id` と `attempt` を持つ）を返し、`finish_step` は `step_id` で Step を指定します。
  Restart で新しい試行が始まった後に旧試行の Worker が書き込むと `StaleAttemptError` になり、何も書き込まれません（新しい試行の Step、Log、worktree の状態は変わりません）。同じ試行の中でも、Step の ID を指定するため、Retry より前の Worker が後から新しい Step を閉じることはできません。
- **Tool の実行状態**: 要件の「Tool execution state」のうち、Backend が再接続後に再開または中断を判断するのに必要な最小の記録だけを持ちます。
  `begin_tool_invocation` / `finish_tool_invocation` が Tool の ID（Tool Broker が UUID を渡すこともできる）、Tool 名、状態、時刻を記録し、`restore` は現在の Step の Tool（直近 100 件）を `TaskSnapshot.tool_invocations` で返します。
  **引数と出力は保存しません。** 権限判定、承認、引数と結果の扱いは Tool Broker（PAW-031）の責務です。Step が終わる（Stop Now / Fail / Restart / `finish_step`）と、`started` のままの Tool は `interrupted` になります。
- `TaskService.restore(task_id)` は DB だけから Snapshot（状態、current step、直近の Log、worktree / review / PR の状態、直近の Event）を作ります。1 つの Repeatable Read Transaction で読むため、同じ時点の値です。
  状態は Process のメモリに持たないので、Client が切断しても、Backend が再起動しても、別の Process が同じ値を返します。
- `TaskService(database, listeners=[...])` の Listener は Commit 後に、書き込まれた `TaskEvent` を受け取ります。Audit（PAW-025）の接続点です。Listener の失敗は Command を失敗させず、例外の型名だけを Log に残します。
  取りこぼしを避けたい Consumer は `task_events` を `seq` で読んでください（`TaskService.history(task_id, after_seq=...)`）。
- 実行中 Task の Runtime 状態（実行中 Process など）の復旧は、要件どおり V1 では保証しません。`tool_invocations` が `started` のままの Task は、Backend が再開または中断を判断するための記録で、Process が生きている保証ではありません。

### Migration は Application の Role に権限を与える（Contributor 向けの規則）

Migration と Application が別の Role のとき（推奨の構成）、Table の Owner は Migration の Role です。
Migration が何も与えなければ、Application は `permission denied` になります。
**Table を作るすべての Migration は、その Table ごとに `grant_app_privileges` を呼びます**。必要最小限の権限だけを選んでください。

```python
from paw_backend.db_roles import grant_app_privileges


def upgrade() -> None:
    op.create_table("tasks", ...)
    # 既定は SELECT のみ。INSERT / UPDATE / DELETE は必要なときだけ指定する。
    grant_app_privileges(
        op, "tasks", select=True, insert=True, update_columns=("status", "updated_at")
    )
```

- 引数: `grant_app_privileges(op, table, *, select=True, insert=False, update=False, delete=False, update_columns=None)`。
  `update_columns` を指定すると、その列だけに `UPDATE (列, ...)` を与えます（`update=True` とは併用できません）。
- `PAW_APP_DATABASE_ROLE` を環境から読みます。**未設定（Role を分けない開発）のときは何も与えず、エラーにもしません**（`PUBLIC` の権限だけは外します）。
  設定されているときは名前を検証し（`public`、`pg_*`、`postgres` などは拒否）、Role が存在しなければ Migration を失敗させます（Table も残りません）。
- DELETE は `delete=True` のときだけ、TRUNCATE、ALTER、DROP、GRANT OPTION は与えません。識別子は検証したうえで Quote し、SQL の文字列に埋め込みません。
- UUID の主キーを使う Table に Sequence の権限は要りません。Sequence を使う Table を作る場合は、その Migration で明示的に権限を与えてください。
- 例外: Migration の Role だけが使う Table は、Module に `NO_APP_GRANTS = {"table": "理由（15 文字以上）"}` を書きます。
- `tests/test_migration_grants.py` が、`migrations/versions/` のすべての Migration を調べます。
  `op.create_table`（または生の `CREATE TABLE`）で作った Table に `grant_app_privileges` も `NO_APP_GRANTS` もない Migration があると失敗します。

## Task Queue / Budget / Loop 検知

PAW-033（Revision `0033`）で実装しました。`paw_backend/tasks/queueing/` は、PAW-032 の Task Lifecycle（`paw_backend/tasks/`）を変更せずにその上へ載せた部品です。
**HTTP の Endpoint も Orchestration（PAW-034）もありません。** Orchestrator が呼ぶ部品だけです。認可も行いません（Endpoint を作る側が権限を確認してください）。
状態は全て PostgreSQL にあり、Process のメモリには何も持たないため、複数の Worker Process が同じ Table を同時に使えます。

| Module | 内容 |
| --- | --- |
| `domain.py` | Priority、Budget の種類と Preset（データ）、Verdict / Decision の値、Loop の閾値。DB なしの純粋な定義 |
| `task_queue.py` | `TaskQueue`: 追加、優先度順の取得（Claim）、Lease、返却・完了・取消 |
| `budget.py` | `BudgetTracker`: 6 種類の消費の記録と、上限の判定 |
| `loop.py` | 失敗の Signature、Loop 判定（`evaluate_loop`）、履歴を持つ `LoopDetector` |
| `escalation.py` | `decide_next_action`: Budget と Loop の判定から次の行動を 1 つ決める |
| `validation.py`、`errors.py`、`sql.py` | 引数の検証（型を変換せず拒否する）、固定文言の Error、DB Error の判別（SQLSTATE と制約名だけを見る） |
| `models.py`、Migration `0033` | `queue_entries`、`budget_usages`、`loop_failure_signatures`。どれも `tasks.id` への実 Foreign Key を持つ |

Table 名は `task` で始めません。PAW-032 の Test が `task` で始まる Table を全て検査するためです。

**Application の Role の権限**（Role を分ける構成、`PAW_APP_DATABASE_ROLE`）: Migration `0033` は 3 つの Table のそれぞれに [`grant_app_privileges`](#migration-は-application-の-role-に権限を与えるcontributor-向けの規則) で、Service が実行する文に必要な最小の権限だけを与えます。TRUNCATE と、Table 単位の UPDATE はどこにも与えません（`PUBLIC` の権限は外します）。

| Table | Application の Role の権限 | 理由 |
| --- | --- | --- |
| `queue_entries` | SELECT、INSERT、UPDATE は `status`、`claimed_by`、`claimed_at`、`lease_expires_at`、`claim_count`、`finished_at` の列だけ。DELETE なし | 追加は INSERT（生成された `id` を読み戻すので SELECT）。Claim・Heartbeat・返却・完了・取消は Lease と状態の列だけを更新する（`FOR UPDATE SKIP LOCKED` も UPDATE 権限が要る）。`task_id`、`priority`、`priority_rank`、`enqueued_at`、`id` は変更できないため、侵害された Application でも、待っている Task の優先度や順序を書き換えられない。取消は状態の変更で、終わった Entry は履歴として残る |
| `budget_usages` | SELECT、INSERT、UPDATE は `preset`、`limit_value`、`consumed`、`running_since` の列だけ。DELETE なし | `set_preset` は INSERT ... ON CONFLICT DO UPDATE（`preset`、`limit_value`）。`record` と `stop_runtime` は `consumed` への原子的な加算、`start_runtime` / `stop_runtime` は `running_since`。キー（`task_id`、`kind`）と `created_at` は変更できず、Budget は削除されない |
| `loop_failure_signatures` | SELECT、INSERT、DELETE。UPDATE なし | `record_failure` が追加し、Window から外れた行を削除する。`clear`（Restart）も削除する。この Table は Hash の Window で履歴ではない（何が起きたかの記録は `task_events`）。保存された失敗は編集できない。PAW-033 で Application が行を削除するのは、ここだけ |

`budget_usages` の `preset` と `limit_value` は Application が更新できます（Owner / Admin が Preset を上げる操作のため）。Preset を変えてよいかの認可は、Endpoint を作る側の責務です。
`tests/test_queueing_grants.py` が、Migration を実際にこの構成で実行し、Superuser でない Role で Queue・Budget・Loop・Flow の Test（同時 Claim を含む）を全て実行します。あわせて、この表と Role の権限が一致すること、優先度や Key の変更、削除、Schema の変更が拒否されることを確認します。

### Queue（HIGH / NORMAL / LOW）

- 優先度は `HIGH` > `NORMAL` > `LOW` です。通常の User Task は `NORMAL`、Background の Memory 整理・Research 更新は `LOW` を想定します（[要件](../../REQUIREMENTS.md)の「Task Queue / Priority / Preemption」）。
- 次に開始する Entry は、優先度、`enqueued_at`（先着順）、`id` の順で決まります。要件に Aging / 飢餓防止の規則はないため、**ありません**。`HIGH` が続く間、`LOW` は待ち続けます。
- 優先度は開始の順序にだけ影響します。`HIGH` の到着で実行中の Entry は中断されません（Preemption は Queue の責務ではありません）。
- 1 つの Task が持てる有効な Entry（`queued` または `claimed`）は 1 つだけです（Partial Unique Index）。完了・取消済みの Entry は残り、Retry / Restart の後で新しい Entry を追加できます。
- Queue は `tasks.state` を読まず、変更もしません。Orchestrator が `claim_next` と PAW-032 の `start` を組み合わせます。
- Owner / Admin による優先度の引き上げ操作は、この Issue の範囲外です。

**Claim と Lease。** `claim_next(worker_id)` は、次の Entry を 1 つ、その Worker へ Lease します。
Claim できるのは `queued` の Entry と、Lease が切れた（`lease_expires_at <= now`）`claimed` の Entry です。後者は元の優先度・`enqueued_at` の位置のまま再び Claim されます（自動の再取得。定期実行の Sweeper は不要です）。
Lease が有効なのは `lease_expires_at > now` の間だけで、期限の瞬間に失われます。`heartbeat`、`release`、`complete` ができるのは有効な Lease を持つ Worker だけで、それ以外（未 Claim、他の Worker、取消済み、期限切れ、存在しない Entry）は全て同じ `LeaseLostError` です。
そのため、同じ瞬間に有効な Lease を持つ Worker は最大 1 人です。期限を過ぎた Worker が完了を報告しても拒否されるので、Worker は期限より十分短い間隔で `heartbeat` してください。
**時計。** Queue が信頼する時計は **Database の時計だけ**です。`enqueued_at`、`claimed_at`、`lease_expires_at`、`finished_at` と「Lease が切れたか」の判定は、全て SQL の中で PostgreSQL の `clock_timestamp()`（評価した瞬間の壁時計）を使います。`now()` は Transaction の開始時刻で固定されるため、行の Lock を待った後の判定に、待つ前の古い時刻が使われてしまいます。ただし、`UPDATE` は行の Lock を待つ**前**に `WHERE` を判定し、Lock を持っていた側が Rollback した場合は判定し直さないので、`clock_timestamp()` だけでは不十分です。そこで `heartbeat` / `release` / `complete` は、まず行を `SELECT ... FOR UPDATE` で Lock し（待つのはここ）、次の文で Lease の期限を `clock_timestamp()` で判定して更新します（1 つの文の中で複数回評価される値は、マイクロ秒の差があり得ます）。
Worker が各自の時計を渡す方式では、時計が進んでいる Worker や誤って未来の時刻を渡した呼び出しが、まだ有効な Lease を「切れた」と判定して Entry を奪い、同じ Task を 2 つの Worker で始めさせられます（同様に過去の時刻で待ち行列の先頭へ割り込めます）。Database の時計なら、全ての Process が 1 つの基準を共有します。
各 Method（`enqueue`、`claim_next`、`heartbeat`、`release`、`complete`、`cancel`）の `now` は省略でき、省略（`None`）が Database の時計です。**本番のコードは `now` を渡してはいけません。** 明示の `now`（Timezone 付きの `datetime`）は Test のための継ぎ目で、`TaskQueue(database, allow_explicit_now=True)` で作った Queue だけが受け取ります。それ以外の Queue は `InvalidQueueingArgumentError("now")` で拒否するので、既定の Queue では呼び出し側が時刻を差し込めません（[Decision 0007](../../docs/decisions/0007-task-queue-budget-and-loop-policy.md) の 6）。継ぎ目は、既存の Test をそのまま使えるように、Constructor で時計を差し替える方式ではなく Method の引数で残しています。
限界: 基準は 1 つの PostgreSQL Server の時計です。Failover などで別の Server の時計へ切り替わる場合の時計のずれは扱いません（Lease は数十秒以上なので、通常の NTP の精度では問題になりません）。`BudgetTracker` の `clock` は、Queue とは別に Process の時計を使います（Task の実行時間の測定用で、Lease の判定ではありません）。

**行ロック。** `claim_next` は 1 Transaction で、Claim できる行のうち先頭を `SELECT ... ORDER BY ... LIMIT 1 FOR UPDATE SKIP LOCKED` で選び、その行を更新します。他の Transaction がロック中の行は待たずに飛ばします。
したがって、競合する複数の Claimer が同じ Entry を得ることはなく、互いを待たず、Claim できる Entry がロック中の 1 つだけなら `None` がすぐに返ります。
`heartbeat`、`release`、`complete`、`cancel` は、それぞれ条件付きの単一の `UPDATE ... RETURNING` です。
Test は別々の DB 接続を持つ複数の Claimer で、同じ Entry を 2 人が得ないこと、ロック中の行を待たないことを実 PostgreSQL で確認します。

### Budget（Preset と 6 種類の上限）

Task ごとの実行予算で、User / Admin 単位の Quota（別の仕組み）とは独立です。種類は次の 6 つです（`BudgetKind`）。
Runtime と GPU 時間の単位は整数の秒、他は個数です。記録する量は 0 以上の `int` だけで、`float`（有限でも）、`bool`、文字列、`None` は拒否します。

| 種類 | 記録 |
| --- | --- |
| `runtime_seconds`（max runtime） | `start_runtime` / `stop_runtime` が、注入した Clock で測る（`record` は不可） |
| `steps`、`retries`、`tool_calls`、`tokens`、`gpu_seconds` | `record(task_id, kind, amount)` |

- **Preset。** Standard / Long / Unlimited は `domain.PRESET_LIMITS` のデータです。`set_preset` が Task の 6 行を作り（または上限だけを更新し、消費は保ちます）、上限をその Task の行へ写します。
  Preset を設定していない Task は `BudgetNotConfiguredError` になり、無制限とは**みなしません**。
- **超過の定義。** `消費量 + planned > 上限` のとき、その種類が `EXCEEDED` です。上限ちょうどまで使うのは超過ではなく（`max 50 steps` は 50 まで）、`check(task_id, planned={kind: 1})` で「もう 1 つ実行できるか」を調べられます。
  `EXCEEDED` の Verdict は、超過した種類をすべて、宣言順で返します。要件に警告の閾値はないため、`WARN` はありません。
- **原子性。** `record` は `UPDATE ... SET consumed = LEAST(consumed + :amount, 上限) ... RETURNING` の 1 文で、複数 Process が同時に記録しても増分は失われません。消費量は `10^15` で飽和し、Overflow しません。
- **Runtime。** `start_runtime` が `running_since` を保存し、`stop_runtime` が経過した整数秒（切り捨て、負にはならない）を加えて消します。実行中は `usage` / `check` が経過分を足して返しますが、書き込みません。同時の `stop_runtime` が時間を二重に加えることはありません。
- **Unlimited。** 6 つの数値の上限を無くすだけです。Loop 検知（下記）、Stop Now、Critical safety / resource protection による停止は Preset と無関係で、Unlimited の Task でも有効です。消費量の記録も続きます。
  要件は Unlimited に別の数値の上限を定めていないため、設けていません。
- 子 Agent が親の Budget を超えないこと（[要件](../../REQUIREMENTS.md)）は、Sub-Agent を扱う PAW-034 の責務です。

**Preset の値は仮の値です。** 要件は Preset の名前だけを定め、数値を定めていません（具体的な閾値は実装時の選択）。次の値は、人間の確認が必要な**仮置き**で、[Decision 0007](../../docs/decisions/0007-task-queue-budget-and-loop-policy.md)（Proposed、承認前）として提案しています。値は `domain.PRESET_LIMITS` のデータなので、承認された値に変えても Migration は要りません。

| 種類 | Standard | Long | Unlimited |
| --- | --- | --- | --- |
| `runtime_seconds` | 3,600（1 時間） | 14,400（4 時間） | 上限なし |
| `steps` | 50 | 200 | 上限なし |
| `retries` | 10 | 20 | 上限なし |
| `tool_calls` | 300 | 1,200 | 上限なし |
| `tokens` | 1,000,000 | 4,000,000 | 上限なし |
| `gpu_seconds` | 3,600 | 14,400 | 上限なし |

### Loop 検知

同じ失敗の繰り返しを検知し、同じ方法を止め、別の方法を試し、それでも失敗するなら上位の Agent へ渡します。Loop の検知だけで Task を `failed` にはしません。
Budget の Preset とは無関係に動きます。

- **Signature。** 失敗を `sha256(error_class + "\x1f" + step + "\x1f" + 正規化した Message)` の 16 進 64 文字にします。Message の正規化は、先頭 2000 文字、NFKC、小文字化、UUID / 16 進 / 数字の置換、空白の圧縮です（`loop.normalize_failure_message`）。
  そのため「Timeout after 30s」と「timeout after 45s」は同じ失敗です。**Message の原文、Error class、Step 名は保存せず**、`loop_failure_signatures` は Signature と試行番号（`approach`）だけを持ちます。
- **判定。** 直近 `window_size` 件（10）のうち、最後の失敗と Signature も `approach` も同じ件数（連続でなくてよい）を `repeats` とします。
  `repeats < repeat_threshold`（3）は `CONTINUE`。それ以上なら Loop で、`approach < max_alternatives`（1）なら `TRY_ALTERNATIVE`、そうでなければ `ESCALATE` です。
  Orchestrator は `TRY_ALTERNATIVE` の後に `approach` を 1 増やして次の失敗を記録します（0 が元の方法、1 が最初の代替）。新しい `approach` は、その中で改めて 3 回繰り返すまで `ESCALATE` になりません。
- 判定は Deterministic で、同じ履歴には常に同じ結果を返します（`evaluate_loop` は純粋関数）。`LoopDetector.record_failure` は履歴へ追記し、1 Task あたり `window_size` 件を超えた古い行を消します。同じ Task への同時の記録は直列化されます。
  Restart で新しい試行を始めるときは `clear(task_id)` で履歴を消してください。`clear` も同じ Task 単位の Advisory Lock（`record_failure` と同じもの）を Transaction の間ずっと取るため、書き込み中の `record_failure` があれば、その Commit を待ってから、その行も含めて削除します（未 Commit の行を見逃して先に成功を返すことはありません）。`clear` の後に始まった記録は新しい履歴に属します。
- 閾値（3 回、Window 10、代替 1 回）は仮の値です（要件は具体的な閾値を実装時の選択としています）。[Decision 0007](../../docs/decisions/0007-task-queue-budget-and-loop-policy.md)（Proposed）で承認を求めています。

### 次の行動（Escalation の判断）

`decide_next_action(budget_verdict, loop_verdict, can_escalate=...)` は、次の順で最初に当てはまる規則を使います。

| 条件 | 行動 |
| --- | --- |
| Budget が `EXCEEDED` | `retries` を含めば `FAIL`、それ以外は `WAIT_FOR_USER`（安全な区切りで停止し、人間が上限を上げるか終了する）。Loop の判定にかかわらず、超過を無視しません |
| Loop が `ESCALATE` | `can_escalate` なら `ESCALATE_AGENT`（Codex / Claude など）、なければ `WAIT_FOR_USER` |
| Loop が `TRY_ALTERNATIVE` | `TRY_ALTERNATIVE` |
| それ以外 | `CONTINUE` |

`WAIT_FOR_USER` は PAW-032 の `wait`（`WaitReason.USER`）、`FAIL` は `fail` に対応します（`domain.ACTION_TASK_COMMANDS`）。Command を発行するのは Orchestrator（PAW-034）で、この Module は発行しません。
この対応（`retries` は `FAIL`、他は `WAIT_FOR_USER`、Budget を Loop より優先）も [Decision 0007](../../docs/decisions/0007-task-queue-budget-and-loop-policy.md)（Proposed）で承認を求めています。
Budget 超過のときに Escalation しないのは、使い切った予算をさらに使うためです。`Waiting for Resource` は GPU の Scheduler（PAW-036）の担当で、ここでは使いません。

### 実装の出自

`task_queue.py`、`budget.py`、`loop.py` は、試験を書いた側（Claude）が書いた**参照実装**です。ローカルモデルによる 2 回の実装は、これらの試験を通せませんでした。
`escalation.py`（`decide_next_action`）だけは、ローカルの Qwen3-Coder-30B-A3B が書いたものです。試験を通ったあと、レビューで冗長な部分を整理しました（振る舞いは変えていません）。
試験は実 PostgreSQL に対する並行 Claim の試験を含み、参照実装に対する変異（境界、行ロック、原子性など 32 通り）のうち、実質同じ動作になる 1 つを除く全てを検出することを確認しています。

### 未確定の事項と制限

人間の確認が必要なもの（要件に定めがないため、仮に置いた値・選択です。**[Decision 0007](../../docs/decisions/0007-task-queue-budget-and-loop-policy.md) は Proposed で、承認されるまで暫定です**）。

- **Preset の数値。** 上の表は仮置きです。
- **Loop の閾値。** 3 回、Window 10、代替 1 回は仮の値です。
- **Unlimited の上限。** 要件は Unlimited に Runtime などの数値の上限を定めていないため、完全に無制限です。暴走を止めるのは Loop 検知と、Operator の Stop Now、Critical safety です。別に上限を設けるかは人間が決めてください。
- **飢餓。** Aging がないため、`HIGH` / `NORMAL` が続くと `LOW` が飢えます。要件が規則を定めたら追加します。
- **Budget 超過時の行動。** `retries` は `FAIL`、他は `WAIT_FOR_USER`、Escalation より Budget を優先する、という割り当ては私の選択です。警告の閾値が要件にないため `WARN` はありません。

制限。

- 量は 0 以上の整数だけです。GPU 時間は整数秒で、`float` は拒否します（呼び出し側が切り上げてください）。
- Queue は `tasks.state` を読まず、変更もしません。`claim_next` と PAW-032 の `start` を組み合わせるのは PAW-034 です。
- Lease の切れた Entry は、次の `claim_next` が自動で取り直します。実行中の Process を止める処理（`stop_now` など）は含みません。期限を過ぎた Worker の完了報告は拒否されます。
- 優先度の引き上げ、Preset の変更、Queue の一覧は認可付きの操作で、Endpoint と一緒に追加します。
- Migration `0033` の `down_revision` は `0021` です（鎖は `0001 → 0025 → 0032 → 0040 → 0021 → 0033`）。

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
- **Repository の ACL は呼び出す側（Repository の保存を持つ PAW-027 など）が解決して渡します。** Repository の Resource は `Resource.repository(project_id, project_state, repo_acl)` で作ります。
  - `RepoAcl.inherit(repo_id, project_id)`（既定）: Project の Role をそのまま継承します（Viewer は読み取り、Contributor は Repository 編集・Agent・PR、非 Member は不可）。
  - `RepoAcl.override(repo_id, project_id, allowed)`: `allowed`（`RepoPermission` の `read` / `write` / `agent`）だけを残します。`{read}` は read-only、空は access denied です。
    Override は Project の Role を**狭めるだけ**で、広げません（Viewer に `write` を与える Override でも書き込めません）。
  - Capability と Repository の権限の対応: `project.read`・`project.memory.use` は `read`、`project.repo.write`・`project.pr.create` は `write`、`project.task.run`・`project.agent.use` は `agent`。
    これ以外の Capability に Repository を指定した Resource は不正として拒否します。
  - **`repo_id` があるのに ACL がない Resource は拒否します**（`repo_acl_unresolved`）。ACL が不明なときに `inherit` として扱うことはありません。
  - ACL は保存された `(repo_id, project_id)` に結び付いています。Resource の Project や Repository と食い違う ACL は拒否します（`repo_acl_mismatch`）。
    別の Project の Repository を、自分が Member の Project の URL で開いても、その Repository の ACL を借りられません。呼び出す側は `project_id` に URL（と Member 資格）の Project を渡し、ACL は保存済みの行から作ってください。
  - Agent は、User の権限と Grant に加えて、Override がある Repository では `agent` が許可されている必要があります（人間が編集できても、Agent は操作できない設定ができます）。
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
`resource_kind` / `resource_id` / `project_id` / `repo_id` / `repo_acl`（Repository のときだけ `inherit` か `override`）、`decision`（`allow` / `deny`）、`reason`（固定の Reason Code）、
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
  Write は接続 Pool を使わず、その 1 文専用の接続（自動 Commit）で行い、期限（と呼び出し側の Cancel、`dispose()`）で**接続の Socket を閉じて**止めます。接続を受け付けたまま応答しない PostgreSQL に対して、Driver がサーバーへ Cancel を依頼して待つ（約 10 秒、または古い libpq では Thread の完了待ち）のを避けるためです（`Database.execute_abortable`、起動時の診断と同じ仕組み）。同時に開く接続は Pool の大きさまでで、空きがなければ同じ期限まで待って失敗します。打ち切られた Write は Commit されたかどうか分かりません（許可は拒否に変わり、Audit 行が残っていることがあります）。
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
この確認は専用の接続で実行し、時間切れ・終了・`Database.dispose()` のときは、Server に Query の取消を頼んで待つ代わりに接続の Socket を閉じます。
そのため、接続は受け付けるが Catalog の Query に答えない PostgreSQL でも、終了は `PAW_SHUTDOWN_TIMEOUT_SECONDS` の範囲で完了します。
- Table の Owner か、UPDATE / DELETE / TRUNCATE 権限を持つ（追記専用を Application が外せる）。
- INSERT 権限がない（Audit を書けず、`REQUIRED` の操作がすべて 503 になる）。`PAW_MIGRATION_DATABASE_URL` を設定して `PAW_APP_DATABASE_ROLE` を設定しなかった場合が典型で、
  Migration も、その構成であることを WARNING で Log に出します。

**`downgrade` は Table ごと監査履歴を破棄します。** 開発・Test 用で、本番では実行しないでください。

#### 残っているリスクと既知の制限

- 許可した読み取り（`DENIED_ONLY`。人間の `project.read`、`shared_memory.read`）は記録しません。誰が何を読んだかは Audit から分かりません（Agent の読み取りは記録します）。
- 認証済みの User の拒否は、1 回ごとに 1 行を書きます。回数制限は PAW-022（Rate Limit、Lockout）までありません。未認証の拒否は Log だけです。
- 保存期間・Partition・古い行の退避は未実装です（Table は削除できないため、行数は増え続けます）。
- Repository の ACL の保存と解決は呼び出す側（PAW-027 など）の責任です。この Backend は、渡された `RepoAcl` を判定するだけです。
  Override が Project の Role を広げてよいか、User 単位の許可リストを持つかは、要件が定めておらず、Decision 0004 で Human の判断を待っています（今は狭めるだけ・権限の集合）。
- `Scope.SELF` の Capability（`chat.use`、`memory.use` など）は `Project` の状態と Member 資格を見ません
  （たとえば Pending deletion の Project の Chat、Member から外された後の Memory）。Project との関係のモデル化は PAW-026 で行います。
- `tests/test_authz_routes.py` が調べるのは `/api/v1` の Route だけで、FastAPI の内部（`effective_route_contexts`）に依存します。
- `create_app` は既定の Provider と Directory を組み込みます。PAW-022 が `install_authz` を呼んで差し替えるまで、全 Endpoint が 401 です。
- 重要操作の Step-up 認証の項目は Audit にありません（PAW-023 で追加します）。
- Migration の鎖は `0001 → 0025 → 0032 → 0040 → 0021` です（`0021` の Revision ID は Issue 番号で、鎖の順序ではありません。統合時に並びを確認します）。

## Owner の初期設定と復旧

[PAW-021](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/18) で実装しました。設計は [要件](../../REQUIREMENTS.md)（Owner、Passkey Policy、Login / Session、全 Passkey を失った Owner は Ubuntu の sudo 経由で復旧）と
[SECURITY_RBAC_AUDIT](../../docs/SECURITY_RBAC_AUDIT.md) に従い、判断が要る点は [Decision 0005（Proposed）](../../docs/decisions/0005-owner-setup-and-recovery.md) にまとめています。

**最初の Owner は Server 上のコマンドでだけ作れます。** 最初に Web へ来た人が Owner になることはありません。

- Web にも HTTP API にも Owner を作る・復旧する経路はありません。`tests/test_owner_no_web_path.py` が、Route の名前、よくある URL、そして **Import Graph 全体**（Helper Module を経由しても、`paw_backend.cli` と `paw_backend.identity` 以外が Operator 側の `paw_backend.identity.operator` を Import または名指ししていないこと）を固定しています。
- Web 側が使う `TokenRedeemer` は Token を**使う**ことしかできません。Owner の作成と Token の発行は Operator 側の `OwnerOperator`（`identity/operator.py`）だけで、Web の Database Role には Token を作る権限もありません（下記「Database の Role」）。
- 管理コマンド `python -m paw_backend.cli` は Backend の Package の一部で、Server 上で Operator の DB の認証情報を使って実行します。
  [`apps/cli/`](../cli/README.md) の Client は Backend の公開 HTTP API だけを呼ぶため、**最初の Owner を作れず**、復旧もできません（HTTP API に該当する経路がないためです）。
- コマンドが作るのは Owner の行（`invited`、認証情報なし）と 1 回限りの Token だけです。Password と Passkey の登録は Token を受け取る Web 側（PAW-022 / PAW-023）が行います。

### 手順

前提: `alembic upgrade head` を適用済みで、Operator の Role を作成し（[下記](#database-の-role)）、その接続先を `PAW_OPERATOR_DATABASE_URL` として読める OS User でその Server にログインしていること。
`owner-recover` は **root（Ubuntu の `sudo`）で実行**してください。root でない Process は拒否されます（`owner-setup` は OS User を確認しません）。root かどうかは、Service（`OwnerOperator.recover_owner`）が**自分の Process の実効 uid（`os.geteuid()`）を自分で読んで**決めます。呼び出し側から uid や `OperatorIdentity` を渡す引数はなく、Library として呼ぶ非 root の Process が root を名乗ることはできません。`PAW_OPERATOR_DATABASE_URL` を書いた環境変数のファイルは、root だけが読める権限にしてください（Backend を動かす User には読ませません。読めると、Web 側の侵害で Owner の Token を作れます）。

```bash
# 初回。login name は小文字の英数字と . _ - （3〜64 文字。大文字は小文字へ、全角は半角へ正規化する）
python -m paw_backend.cli owner-setup --login-name tomoki
```

- **stdout に Token だけが 1 行**出ます（`pawst1.<Token ID>.<Secret>`）。stderr に owner_id、login name、実行した Process の uid（`operator uid=... sudo_uid=...`）、有効期限が出ます。Token は**この 1 回しか表示されません**（保存しないため再表示できません）。
  端末のスクロールバックの記録、`tee`、CI の Log、`script` に Token を残さないでください。`TOKEN=$(...)` のように取り込めますが、Shell の履歴やプロセス一覧に出さないでください（Token は引数ではなく出力です）。
- Token を Web の Setup 画面へ入力します（PAW-022 で実装。それまでは受け取る側がありません）。**有効期間は既定で 30 分**（`PAW_SETUP_TOKEN_TTL_SECONDS`。上限 4 時間）、使えるのは 1 回だけです。
- Owner は Passkey が必須です（`users.passkey_required = true`）。Passkey の登録の強制は PAW-023 です。

```bash
# Owner が全 Passkey / 端末を失った、または Token を失ったとき
python -m paw_backend.cli owner-recover --confirm-owner-recovery
```

- `owner-recover` は既存の Owner に**新しい Recovery Token を発行**し、その Owner の未使用の Token（Setup / Recovery）をすべて無効にします。確認フラグ `--confirm-owner-recovery` がないと何もしません。
- **Token を失った場合**（Setup の直後を含む）: `owner-setup` はもう実行できません（Owner が存在するため拒否され、終了コードは 1）。`owner-recover` を実行してください。古い Token は使えなくなります。
- **Token の有効期限が切れた場合**、**試行回数を使い切った場合**（下記）も同じです。
- **Token を表示できなかった場合**（終了コード 3、下記）も、`owner-recover` で新しい Token を発行します。
- **Owner のアカウントが削除待ち（`pending_deletion`）または削除済み（`deleted`）の場合**: `owner-setup` も `owner-recover` も、実際の状態を報告して拒否します（終了コード 1）。
  `owner-setup --login-name <新しい名前> --replace-non-live-owner` を明示すると、古いアカウントを一般の `user` に降格し（状態とデータはそのまま）、その Token を無効にして、新しい Owner を作ります。1 つの Transaction で、`owner.replace` を含めて Audit に残ります。**生きている Owner（`invited` / `active`）は、このフラグを付けても置き換えません。**
- Recovery Token で Owner を復旧する Web 側は、既存の全 Session、現在の Password、既存の Passkey を無効にする（または再登録を必須にする）必要があります（下記「PAW-022 / PAW-023 との接続」）。

| 終了コード | 意味 |
| --- | --- |
| `0` | 成功。Token は stdout に出た |
| `1` | 拒否・不正: 生きている Owner が既にいる（`owner-setup`）、Owner が削除待ち・削除済み、復旧する Owner がいない、login name が不正または使用済み、確認フラグなし、引数の誤り |
| `2` | 環境のエラー: 設定が不正、接続先が未設定、DB に接続できない・Migration 未適用・権限不足（Web の Role で実行した場合を含む）、Audit を書けない（この場合は何も変更しない） |
| `3` | **変更は行われ Audit にも残ったが、Token を stdout へ書けなかった**（stdout が閉じている、Pipe が切れた、Disk が満杯）。Token は失われたので `owner-recover --confirm-owner-recovery` を実行する |

- **DB の接続先は設定からだけ読み、引数では受け取りません**（`--database-url` は使えず、誤って渡された値も表示しません）。`PAW_OPERATOR_DATABASE_URL`（Owner の Token を作れる Role）を使い、**未設定のときだけ** `PAW_DATABASE_URL` に戻ります。
  戻った場合は、Web の Application が同じ Role で動いているなら Application も Token を作れることになるため、stderr に警告を出します。`PAW_MIGRATION_DATABASE_URL` は使いません。
- 出力するのは Token（stdout に 1 回）と、ID、有効期限、Operator の uid、対処の案内だけです。Token 以外の Secret は出さず、例外のメッセージは出さず（型名だけ）、traceback も出しません。
- `--json` はありません（Token を出す必要があり、Token を含まない JSON は目的に役立たないため）。
- Token の書き込みが失敗したとき、Python が終了時に stdout を再度 Flush して警告を出し、終了コードを 120 に変えることがあります。それを避けるため、書き込みに失敗した stdout は `/dev/null` へ向け直します。

### Database の Role

Token の行を **INSERT できる Role は、Owner を乗っ取れます**（自分で Salt と HMAC を計算した行を入れて `redeem` すればよいため）。そのため、Migration `0021` は Role を 2 つに分けます（Audit の Table の分離と同じ考え方で、[追記専用について](#追記専用について保証すること・しないこと)も参照）。

| Role（設定） | 誰が使うか | 与える権限 |
| --- | --- | --- |
| Web（`PAW_APP_DATABASE_ROLE`） | Web の Application（PAW-022 の `TokenRedeemer`） | `users` と `setup_tokens` の SELECT。`users.updated_at` の UPDATE（`SELECT ... FOR UPDATE` の行 Lock に UPDATE 権限が要るため）。`setup_tokens` の `attempts`、`used_at`、`locked_at` の UPDATE。**INSERT も DELETE も、Role・Salt・Hash・期限・`revoked_at` の UPDATE も与えない** |
| Operator（`PAW_OPERATOR_DATABASE_ROLE`） | 管理コマンド（`PAW_OPERATOR_DATABASE_URL`） | `users` と `setup_tokens` の SELECT と INSERT。`users.system_role`、`users.updated_at`、`setup_tokens.revoked_at` の UPDATE。`audit_events` の INSERT（Audit を書くため。Table がある場合だけ）。**DELETE も、Token の消費・Hash の更新もできない** |

- Web の Role の権限は、規則どおり `grant_app_privileges`（`users` は SELECT と `updated_at` の列 UPDATE、`setup_tokens` は SELECT と 3 列の UPDATE）で与えます。Operator の Role は別の Role なので、`0021` が自分で与え、Role 名は同じ `validate_role_name` で検証し、同じ方法で Quote します。`grant_app_privileges` は先に `REVOKE ALL ... FROM PUBLIC` を実行しますが、名前を指定した Role への `GRANT`（Operator の `audit_events` への INSERT を含む）には影響しません。
- `setup_tokens` の Trigger（`tr_setup_tokens_guard_update`）が、**どの Role に対しても**、行を Token の寿命に必要な変更だけにします。Salt・Hash・期限・`user_id`・`purpose`・`audit_ref`・`issued_by_*` は変更できず、`used_at` / `revoked_at` / `locked_at` は 1 回設定したら戻せず、`attempts` は減らせません（使用済みの Token を復活させたり、試行を巻き戻したりできない）。
- Application の起動時に、接続 User が Token を作れる、または Role を変えられる場合は **`WARNING`** を Log に出します（Role 名も接続文字列も出しません。起動は止めません）。
  1 つの Role で動かす開発の構成（`PAW_OPERATOR_DATABASE_URL` を設定しない）では、この警告が出ます。
- Web と Operator を同じ Role にすると、Migration が警告を出します。Role が存在しない場合は Migration が失敗します。`PAW_OPERATOR_DATABASE_ROLE` を設定せずに `PAW_MIGRATION_DATABASE_URL` を設定した場合も警告し、その構成では Schema の Owner の Role で管理コマンドを動かします。
- Operator の `audit_events` への INSERT の権限は、`audit_events`（Migration `0025`）がある場合だけ与えます。統合後に `0021` が `0025` より前に来る場合は、`0025` の後で `GRANT INSERT ON audit_events TO <operator role>` を実行してください。

**守れないもの**: Schema の Owner の Role と Superuser は何でもできます。Web の Role は、未使用の Token を使用済みにしたり（`used_at`）試行を使い切らせたり（`attempts`）できます。Owner が Token を使えなくなる可用性の問題で、乗っ取りではなく、Operator の `owner-recover` で回復します。
PAW-022 / PAW-023 が Password と Passkey の Table を追加すると、Application はそれを書けなければならないため、**Application が侵害されれば、Owner の認証情報をそこで変えられる可能性は残ります**。この分離が塞ぐのは Token の偽造による乗っ取りで、それ以外の経路は PAW-023 の Step-up などで守る必要があります。

### Token

- 形式は `pawst1.<Token ID>.<Secret>`。Secret は OS の CSPRNG（`secrets`）の **32 byte（256 bit）**です。**Token ID は検索用の鍵**で、Token の一部なので、知っている人は Token の試行を使い切らせられます。そのため **Token ID は Audit にも Log にも書きません**（下記）。
- DB には **Token を保存しません**。Token ごとの乱数 Salt を鍵にした HMAC-SHA256 だけを保存します（Secret が高 Entropy のため、Password 用の遅い Hash は不要です）。比較は `hmac.compare_digest`（定数時間）です。
  Token は `IssuedToken`（`repr` に出ない）以外の場所に存在せず、Log、DB の行、Audit のどこにも入りません（Test が SQL の Log と全 Table の行を検査します）。
- 各 Token は、Token ID とは無関係な乱数の **`audit_ref`**（UUID）も持ちます。Audit の Event と Log は Token をこの値だけで呼び、`audit_ref` を検索の鍵として受け付ける処理はありません。Audit を読める Admin が `audit_ref` を知っても、Token を使うことも試行を使い切らせることもできません（Test 済み）。
- **1 回限り**。消費は `UPDATE ... WHERE used_at IS NULL AND revoked_at IS NULL AND expires_at > now` の 1 文で行うため、同時に 2 回使っても片方だけが成功します。
  1 人の User について、未使用で無効になっていない Token は最大 1 つです（Partial Unique Index）。新しく発行すると、古い Token は先に無効にされます（`owner-recover`）。
- **有効期限**は `PAW_SETUP_TOKEN_TTL_SECONDS`（既定 1800、60〜**14400**）。期限ちょうどの時刻は無効です。
- **試行の上限**は Token ごとに `PAW_SETUP_TOKEN_MAX_ATTEMPTS`（既定 5、1〜20）です。試行は Secret を比較する**前に**予約して Commit するため、同時に大量の Request が来ても比較は上限回までしか行われません。
  上限を使い切る試行が **`setup_tokens.locked_at` を記録**し、その Token は正しい Token でも二度と使えません。**設定を後から大きくしても再び開くことはありません**（Test 済み）。`owner-recover` で新しい Token を発行してください。
- **失敗はすべて同じ失敗**です。`SetupTokenRejectedError`（固定の Message）は、Token が間違い・未知・形式不正・期限切れ・使用済み・無効化済み・試行上限超過・Owner でなくなった User のどれでも同じで、Message にも Cause にも違いがありません。
  Secret の比較、HMAC の計算、試行の予約のための DB 往復は、形式不正・未知の Token でも同じ回数行います（Test が回数を検査します）。理由は Audit にだけ残ります。

### Audit

すべての操作を、既存の `AuditSink`（PAW-025）へ ID と列挙値だけで記録します。Login name、Token、Token ID、Secret は入りません。Token は `audit_ref` で呼びます（`resource_kind=setup_token`）。CLI の操作の `actor_role` は `system`、`actor_id` は空です。

| `action` | `decision` / `reason` | 内容 |
| --- | --- | --- |
| `owner.create` | allow `created`（`new_role=owner`）/ deny `owner_exists`、`owner_not_live`、`login_name_taken` | Owner の作成 |
| `owner.replace` | allow `replaced`（`old_role=owner`、`new_role=user`） | 削除待ち・削除済みの Owner の降格（`--replace-non-live-owner`） |
| `owner.setup_token.issue` | allow `issued` | Setup Token の発行 |
| `owner.recovery_token.issue` | allow `issued` / deny `owner_missing`、`owner_not_live`、`not_privileged` | Recovery Token の発行 |
| `owner.token.revoke` | allow `superseded` | 復旧・置き換えで無効にした未使用の Token（1 つにつき 1 行） |
| `owner.token.redeem` | allow `redeemed`（`actor_id` は Owner）/ deny `token_mismatch`、`token_expired`、`token_used`、`token_revoked`、`token_unavailable`、`user_not_eligible`、`attempts_exhausted` | Token の使用と失敗 |

- **実行した人**: PAW-025 の `AuditEvent` には ID 以外の自由な項目がないため、Operator の uid と `SUDO_UID` は **Token の行**（`setup_tokens.issued_by_uid`、`issued_by_sudo_uid`）に数値で記録し、Audit の Event の `resource_id`（`audit_ref`）から Join できます。stderr にも出します。`SUDO_UID` は環境変数で、sudo が設定する**手掛かりであり、本人確認ではありません**。書式が不正な値は保存も表示もしません。
  **`owner-recover` は root でなければ拒否します**（要件は Ubuntu の `sudo` 経由の Recovery です）。Service 自身が、動いている Process の実効 uid が 0 であることだけを見て（呼び出し側が渡した値は信用しません。`recover_owner` にも `setup_owner` にも Identity を渡す引数はありません）、`SUDO_UID` は認可に使いません（誰でも設定できる環境変数のため）。Token の行に記録する uid と `SUDO_UID` も、この同じ読み取りの値です（`IssuedToken.operator` に入り、stderr の表示もこれです）。Test は `os.geteuid` を差し替えて Process の uid を変えます（`tests/identity_support.py` の `running_as`）。Production のコードにその手段はありません。拒否は Audit に `owner.recovery_token.issue` / deny / `not_privileged` として残り、何も変更しません。`owner-setup` は今のところ OS User を確認しません（Decision 0005）。Audit に専用の項目を足すかは PAW-025 側の判断です。
- **失敗した使用の Audit 行は Token ごとに最大 `max_attempts + 1` 行**です（予約した試行ごとに 1 行と、Token を Lock した試行の `attempts_exhausted` 1 行）。Lock 後の試行は Audit に書かず、Log に固定の 1 行を出すだけです。
- **未知の Token ID と形式不正の Token は DB に書かず**、Log に固定の 1 行（Token も ID も含まない）だけです。誰でも作れる行になり、Audit の Table は削除できないためです（PAW-025 の未認証の拒否と同じ方針）。
- **Fail-closed**: 発行・使用・置き換えの Audit は DB の Transaction が Commit される**前**に書きます。書けなければ Transaction を戻し、Token は作られず、消費されず、表示もされません（終了コード 2）。
  Audit の後で Commit が失敗した場合は、起きていない操作の Audit 行が残り得ます（Token は表示されません）。拒否の Audit は Best Effort で、書けなくても拒否のままです。

### PAW-022 / PAW-023 との接続

`paw_backend.identity.TokenRedeemer` が Web 側に使わせる API です（`paw_backend.identity` の `__init__` は Operator 側を Import しません）。HTTP の Endpoint はこの Issue では追加していません。

```python
redeemer = TokenRedeemer.from_settings(settings, database, PostgresAuditSink(database))


async def set_credentials(
    session: AsyncSession, redemption: Redemption
) -> (
    None
): ...  # 同じ Transaction で Password を設定し、users.status を active にする、など


redemption = await redeemer.redeem(token_from_request, apply=set_credentials)
# Redemption: user_id, audit_ref, purpose (setup / recovery), user_status, passkey_required
```

- `redeem` は Token を消費し、`apply` を**同じ Transaction の中で**実行してから Commit します。`apply` が例外を出すと全体を Rollback し（Token は消費されず、例外はそのまま伝わります）、消費と Password の設定は 1 つの単位になります。
  ただし試行の予約は先に Commit されるため、入力の検証は `redeem` の前に行ってください。
- `apply` に渡す Session は **Commit・Rollback・Close をしてはいけません**。Commit は Session の Event で拒否し、Rollback と Close は `apply` の後で Token の消費が残っているかを確かめて検出し、いずれも `RedeemHookError` で全体を Rollback します（Audit との整合が崩れるため）。Raw SQL の `COMMIT` や Session から取り出した Connection での Commit は止められません。`apply` は信頼する Code で、この検査は間違いを見つけるためのものです。
- `apply` の間は Owner の `users` 行と Token の行を Lock（`SELECT ... FOR UPDATE`）したままなので、時間のかかる処理（外部への通信など）は入れないでください。他の `redeem` や `owner-recover` は、その間待たされます（Test 済み）。
- `redeem` は User を作らず、`users.status` を変えず、Session も作りません。`invited` から `active` への移行、Password、Session は PAW-022、Passkey は PAW-023 の責務です。
- **Recovery の Contract**（要件: 全 Passkey / 端末を失った Owner の復旧、Owner Recovery では既存 Session を全失効）: `purpose` が `recovery` の `apply` は、同じ Transaction で **既存の全 Session を失効させ、現在の Password を無効にして新しい Password を設定させ（または再設定を必須にし）、既存の Passkey をすべて失効させて（または再登録を必須にして）**ください。
  復旧が必要な状況は、認証情報が盗まれた可能性を含むためです。Passkey が必須（`passkey_required`）の Owner に、Passkey が 1 つも登録されていないまま通常の操作を許してはいけません（PAW-023）。この Contract は Code では強制できないため、PAW-022 / PAW-023 の受け入れ条件です。
- Web の Endpoint は誰でも呼べる**公開 Route**になるため、`tests/test_authz_routes.py` の `PUBLIC_ROUTES` に理由付きで載せ、**Client（接続元）単位と全体の Rate Limit** を付けてください。
  上限は Token ごとにしか効かず、未知の Token ID への試行は数える相手がありません。
- **Passkey の必須化**: `users.passkey_required` は Owner と Admin では DB の CHECK 制約で `false` にできず、`Redemption.passkey_required` も `true` です。
- Owner は DB の Partial Unique Index で 1 人に制限されます。Ownership の移譲（`Authorizer.authorize_ownership_transfer`）は、同じ Transaction で先に旧 Owner を降格してから新しい Owner にしてください。

### `users` と `setup_tokens`

`users`: `id`（UUID）、`login_name`（正規化済み・一意）、`system_role`（`owner` / `admin` / `user`。`system` は人間の User ではなく行を持たない）、
`status`（`invited` / `active` / `pending_deletion` / `deleted`。要件の User Lifecycle）、`passkey_required`、`created_at`、`updated_at`。
**Password の Hash、Session、Passkey の列はありません**（PAW-022 / PAW-023 が追加します）。
`setup_tokens`: `id`（Token ID、検索用の鍵）、`audit_ref`（Audit と Log での呼び名、一意）、`user_id`（`users` への外部キー、`ON DELETE CASCADE`）、`purpose`（`setup` / `recovery`）、`salt`、`secret_hash`、`created_at`、`expires_at`、`used_at`、`revoked_at`、`attempts`、`locked_at`、`issued_by_uid`、`issued_by_sudo_uid`。
列挙値と制約（login name の形式を含む）は DB の CHECK 制約でも強制します。

Login name は小文字の ASCII 英数字と `.` `_` `-` だけ（3〜64 文字、先頭と末尾は英数字）です。Unicode の互換形（全角など）は NFKC で正規化し、それ以外の文字は受け付けません。
紛らわしい文字を避けるための暫定の規則で、仕様として確定したものではありません（[Decision 0005](../../docs/decisions/0005-owner-setup-and-recovery.md)、Human の判断待ち）。

**`downgrade` は `users` と `setup_tokens` を Table ごと破棄します。Owner を含む全 User と全 Token が失われます。** 開発・Test 用で、本番では実行しないでください。

### 既知の制限

- Token ID を知っている人は、試行を使い切らせて正規の使用を妨げられます（Token ID は Token の一部で、通常は Token を知る人しか持ちません。Audit と Log には書かないため、Audit を読める人は知りません）。回復は `owner-recover` です。
- 比較と DB 往復の回数は全経路で同じですが、**時間そのものは揃えていません**。既存の Token に対する失敗だけは Audit の INSERT が加わるため僅かに長く、これを観測できるのは Token ID を知る人だけです。
- **Rate Limit は Token ごとの試行の上限だけです。** 接続元ごと・全体の Limit は PAW-022 の Endpoint の責務です。
- 実行した OS User は Token の行に uid として残ります。`owner-recover` は実効 uid が 0 でなければ拒否しますが、`SUDO_UID` は手掛かりにすぎません。この確認は、DB の認証情報を持つ Process が誤って実行することを防ぐもので、境界そのものではありません（境界は認証情報のファイルの権限）。同じ Process の中の Code は `os.geteuid` の差し替えも DB への直接の書き込みもできるため、Service の確認は Library として呼ばれる場合の**誤用と Identity の偽装の防止**であり、悪意ある Code への防御ではありません。root の Process や、User Namespace の中の uid 0 は通ります。Container で root 以外として実行する構成では Recovery できません。
- 発行・使用の成功時は Transaction と Audit のために接続を 2 本同時に使います（Pool の既定は 5）。失敗の経路は同時に持ちません。
- Token の Web 側での Password・Passkey の扱い（Recovery の Contract）は PAW-022 / PAW-023 の実装で、この Issue の範囲は Token の発行・使用・失効と Audit までです。

## Tool Broker / Capability Policy

[PAW-031](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/27) で実装しました（`paw_backend/tools/`、Migration `0031`）。
Migration `0031` の `down_revision` は `0033` です（鎖は `0001 → 0025 → 0032 → 0040 → 0021 → 0033 → 0031`）。Application Role への権限は、共通の `grant_app_privileges`（PAW-025）で付与します。
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
    task_activity=PostgresTaskActivity(database),
)  # 既定は Budget 不明・Task 不明 = 拒否
tasks = TaskService(database, listeners=[approval_service.revoke_on_task_end])
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

| Capability | 範囲内（Project-local） | 範囲外の Host | 範囲外（Path / Project / Credential） | Host 全体の環境（範囲内） |
| --- | --- | --- | --- | --- |
| `read` | `AUTO` | `APPROVAL`（正確な URL を見て決める） | `DENY` | `AUTO` |
| `write` | `SCOPED_AUTO` | `APPROVAL`（Task Scope を超える外部 write） | `DENY` | `APPROVAL` |
| `execute` | `SCOPED_AUTO` | `DENY` | `DENY` | `APPROVAL` |
| `network` | `SCOPED_AUTO` | `APPROVAL` | `DENY` | `APPROVAL` |
| `credential-use` | `SCOPED_AUTO` | `DENY` | `DENY` | `STRONG_APPROVAL` |
| `destructive` | `APPROVAL` | `DENY` | `DENY` | `STRONG_APPROVAL` |

- 例: `web.fetch`（read + network）も Issue 作成（write + network）も、Task の Host の外なら `APPROVAL`。`git.merge` は `min_level=STRONG_APPROVAL` で `STRONG_APPROVAL`。Credential を使う呼び出しは、Host が Task の Host でも、その Credential の使える Host でなければ `DENY`（下の Credential）。
- **Task Scope の範囲外は Policy で許可できません。** `ToolPolicy` は、範囲外（`out_of_scope`）を `DENY` 以外にする表を作れません（`ValueError`）。Host だけは、Task の Host を超える読み取り・外部 write を `APPROVAL` にします（要件の「通常 Task scope を超える外部 write」）。
- Host 全体の環境（package、service、firewall、proxy、mount）を変える Tool は `environment=Environment.HOST` を宣言します。宣言は Tool 側で、呼び出しでは選べません。この環境では読み取り以外は `APPROVAL` 以上です。
- **Credential plaintext の取得は常に `DENY` です**（下の Credential）。

**要件との違い。** 表は次のように解釈しています。要件の表と同じにはなっていない点（**厳しい方向と、Tool 側の宣言に頼る方向の両方**）を隠さず書きます。判断は [Decision 0006](../../docs/decisions/0006-tool-broker-policy.md)（Proposed、Human の承認待ち）に提案しています。

- 厳しい方向:
  build / test / lint などの実行は `SCOPED_AUTO`（実行は Project のコードを走らせ、書き込みもできるため。人の確認が要らない点は `AUTO` と同じ）。
  Task Scope 内の一時ファイルの削除も `destructive` なので `APPROVAL`（`SCOPED_AUTO` にするには「一時ファイルだけを消す」Tool を別に宣言する必要があり、まだ決めていない）。
  Task の Host の外への Web の読み取りは、要件の「Web / Docs の read-only 取得: `AUTO`」と違い `APPROVAL`（Host の指定がなければ、どの Host へも行ける）。
- **緩い方向（Tool 側の宣言が前提）:**
  Task Scope 内の `credential-use`（handle を使う）は `SCOPED_AUTO` です。要件の `STRONG_APPROVAL` の項は Credential の**登録・更新・削除**で、使用は分類されていません（AI 専用 Branch への push と PR 作成は `SCOPED_AUTO`）。Credential を管理する Tool は `min_level=STRONG_APPROVAL` を宣言してください（Agent には委任できない操作です）。
  Host 全体の `sudo` / 特権操作は、要件では `STRONG_APPROVAL` ですが、表は `HOST` 環境の `write` / `execute` を `APPROVAL` にしています。特権の Tool は `min_level=STRONG_APPROVAL` を宣言する必要があります（表は Tool の中身を知りません）。

### 判定の順序

前の段階が通った場合だけ次へ進み、各段階は**狭める方向にしか**働きません。

1. 呼び出しの形。`ToolCall` でなければ `invalid_call`。Tool 名は登録済みの名前と**完全一致**だけ（大文字小文字、空白、Zero-width、Unicode の見た目が近い文字は別の名前）。なければ `unknown_tool`。
2. `returns_credential_plaintext` の Tool は `credential_plaintext_denied`。引数を Tool の宣言（`ArgumentSpec`）と照合します。宣言にない引数、足りない必須の引数、型の違い（`"true"` は bool でなく、`True` は int でない）、長さや範囲の超過は `invalid_arguments`。Path / Host / URL / Project / Repository / Credential handle は正規化して `invalid_target` または下の理由で拒否します。文字列に Credential の平文があれば `credential_plaintext_in_arguments`。
   **長さの検査は Credential の走査より先**です（Tool の宣言した `max_length`、Path / URL / Host / handle は種別ごとの上限）。1 回の呼び出しの文字列の合計にも上限（262,144 文字）があり、超えたものは走査も正規化もしません。
3. 対象を Task Scope と比べ（Symlink を解決）、Level を決めます。`DENY` なら `path_out_of_scope` / `host_out_of_scope` / `project_out_of_scope` / `repository_out_of_scope` / `credential_out_of_scope` / `policy_denied`。
4. 認可（PAW-025 の `authorize_agent_action`）。委任元 User の権限と `AgentGrant` の積集合で、拒否は `authz_denied`（`authz_reason` に PAW-025 の理由）。Authorizer が失敗または想定外の答えなら `authz_unavailable`。
   **Repository の呼び出しは、Repository とその ACL で判定します**（下の「Repository の ACL」）。Project の Resource だけでは Repo ACL の override（読み取り専用、Agent 禁止）が効かないためです。
5. Task Budget（`BudgetProvider`）。超過は `budget_exceeded`、不明は `budget_unknown`、Provider の失敗・Timeout・想定外の答えは `budget_unavailable`。
6. `AUTO` / `SCOPED_AUTO` は `ALLOW`（`auto` / `scoped_auto`）。`APPROVAL` / `STRONG_APPROVAL` は、**Task がまだ動けること**（`TaskActivityProvider`。完了・失敗・取り消し済み、不明、読めない、は `task_not_active` / `task_unknown` / `task_state_unavailable`）を確認してから、下の Approval に進みます（承認者に見せられない呼び出し、Open な承認が多すぎる、直前に却下された、は `approval_not_displayable` / `approval_limit_reached` / `approval_cooldown`）。

判定は `AuditSink` へ記録します（下の Audit）。**記録できない `ALLOW` は `DENY`（`audit_unavailable`）になります。**
Approval の要求（`NEEDS_APPROVAL`）を作る前に、Path・認可・Budget の判定が済んでいるため、実行できない呼び出しの承認要求は作りません。

### Tool Registry

`ToolRegistry` は起動時に一度だけ `ToolSpec` の一覧から作り、追加・置換・削除の方法がありません。`ToolSpec` は Tool 名、Capability class、対応する PAW-025 の Capability（必須）、引数の宣言、Environment、`min_level`、`requires_budget`（既定 `True`）を持ちます。
宣言の整合性は生成時に検査します。**Host / URL の引数を持つ Tool は `network`、Credential handle の引数を持つ Tool は `credential-use` でなければならず**、Project-local の `write` / `destructive` は触る対象（Path / Host / URL / Project の**必須**の引数）を宣言しなければなりません
（対象のない書き込みは、常に「範囲内」に見えるため。省略できる引数は対象を宣言したことになりません）。`network` は必須の Host / URL、`credential-use` は必須の handle が要ります。Repository への書き込み（PAW-025 の `project.repo.write` / `project.pr.create`）の Tool は、触れる Repository を表す**必須の Path か Repository の引数**が要ります（Host / URL / Project は Repository を表さないため、Repository の ACL を効かせられません）。Tool 名は `unknown` と `approval*` を使えません（Audit の action と衝突するため）。

### Task Scope と正規化の契約

`TaskScope` は、Task が触れる Path の Root（先頭が相対 Path の基準）、Host、Project（Project の状態つき）、使える Credential handle、作業対象の Repository（`repositories`、下の「Repository の ACL」）を持ちます。比較は**正規化した形どうしの完全一致**で、Prefix や Wildcard の一致はありません（`scope.py` の docstring が契約の正本です）。

- **Path**: 絶対 Path、または先頭の Root からの相対。`.` と空の Segment は取り除き、**`..`（と `...`、空白や点だけの Segment）は畳み込まず拒否**します（Symlink の先の `..` は File System と字句上で食い違うため）。
  `\`、`~` で始まる名前、`%2e` `%2f` `%5c`、制御・書式・Separator 文字、NFKC で変わる文字（全角の `．．／`、合字、分解形）、1024 文字超は拒否。
  範囲内の判定は `path == root or path.startswith(root + "/")`（`/srv/w/task-evil` は `/srv/w/task` の外）。**大文字小文字は区別します**（大文字小文字を区別しない File System では誤拒否になるだけで、逸脱にはなりません）。
- **Symlink**: `PathResolver`（既定は `os.path.realpath` を別 Thread で）で Path と Root を解決し、解決後の Path が解決後の Root の中にあることを確認します。解決が失敗・Timeout・不正な答えなら `path_resolution_unavailable`。
  **これは呼び出しの前の確認です。** 確認の後に File System が変わる（TOCTOU）ため、Executor は Root の外へ Symlink をたどらずに開く必要があります（`openat2` の `RESOLVE_BENEATH` など）。
- **Host / URL**: ASCII のみ（国際化 Domain は `xn--`）、小文字化、末尾の `.` を 1 つ除去、Label を検査。数字で終わる Host は厳密な Dotted-quad の IPv4 だけ（`127.1`、`0x7f.1`、`2130706433` は拒否）。
  URL は `http` / `https` の既定 Port だけで、ユーザー情報（`user@`）、`\`、空白は拒否します。Scope の Host と**完全に一致**したときだけ範囲内です（`github.com.evil.com` も `gist.github.com` も外）。
- **Project** は正規形の UUID、**Credential handle** は `cred_` + 32 桁の 16 進だけ。
- **Repository** は正規形の UUID（`ArgumentKind.REPOSITORY`）で、Task の作業対象（`TaskScope.repositories`）にあるものだけです（なければ `repository_out_of_scope`）。

### Repository の ACL

Project の Member でも、Repository ごとの ACL override（`read` / `write` / `agent`。要件の「Project / Repo Permission Inheritance」）で、読み取り専用や Agent の操作禁止にできます。
Broker は、呼び出しがどの Repository に触れるかを **Backend が作った `TaskScope.repositories`**（`ScopedRepository`: Repository の ID、Project、Worktree の Path、**解決済みの `RepoAcl`**）から決め、その Repository に対する `Resource.repository(...)` で `authorize_agent_action` を呼びます。ACL を読むのは Authorizer（PAW-025 の `decide` / `decide_agent`）で、Broker は判定を自分で作りません。

| 触れる Repository | 決まり方 |
| --- | --- |
| 呼び出しが名指し | `repository` 引数（`ArgumentKind.REPOSITORY`）。Task の作業対象にない ID は `repository_out_of_scope`（DENY） |
| Path | **Symlink を解決した後の** Path が、Repository の Worktree（同じく解決する）の中にあるもの。入れ子の Repository の中の Path は、外側と内側の**両方**に触れます（厳しい方の ACL が効く） |
| Host / URL | **Repository を表しません**（同じ Host に多くの Repository があるため）。リモートへ書く Tool は `repository` 引数を宣言する |

- **ACL が不明なら拒否します。** `ScopedRepository.acl=None`（Backend が解決できなかった）は `inherit` とは読まれず、`authz_denied`（`authz_reason=repo_acl_unresolved`）。ACL は Repository と Project が一致するものだけを `ScopedRepository` に入れられます（違えば構築時に `ValueError`）。
- **ACL は呼び出しごとの現在の値です。** Task の Scope は呼び出しごとに作り直すため、ACL の変更は次の呼び出しから効きます。承認を使うときも認可をもう一度行うので、ACL が狭まった後は承認済みの呼び出しも `authz_denied` になり、承認は消費されません。
- **Repository への書き込み**（PAW-025 の `project.repo.write` / `project.pr.create`）の Tool は、触れる Repository を **Path か Repository の必須引数**で宣言しなければなりません（`ToolSpec` の生成時に `ValueError`）。それでも作業対象のどの Repository にも触れない呼び出し（作業対象の外の Path など）は `repository_not_identified` で拒否します（ACL を読めない書き込みは通しません）。
- 読み取りと Agent 実行（`project.read`、`project.task.run` など）で、触れる Repository がない呼び出しは、これまでどおり Project の Resource で判定します。引数のない Tool は Repository の ACL では判定できないことを、既知の制限に書きます。
- Repository を表さない Project の Capability（`project.chat`、`project.settings.manage` など）は Project の Resource のままです（PAW-025 は、これらに Repository の Resource を渡すと拒否します）。
- 判断の理由と、Human の承認を待つ点は [Decision 0006](../../docs/decisions/0006-tool-broker-policy.md) の「8. Repository の ACL」。

### Credential

- **平文は Agent の引数にも結果にも入れません。** 使うときは不透明な handle だけで、handle を解決して Credential を付けるのは Executor（Backend 内部）です。Tool の引数が handle であり、その handle が Task の使える handle に入っていることを Broker が確認します（`credential_out_of_scope`）。
- **handle は、使える Host に束縛されています。** `TaskScope.credential_handles` は `{handle: その Credential の使える Host の集合}` です。同じ呼び出しが触れる Host のどれかがその集合に含まれなければ `credential_out_of_scope`（DENY。承認では許可できません）。Task が両方の Host に触れられても、GitHub の handle を別の Service へ送る呼び出しは通りません。Host のない呼び出し（何も送らない）には影響しません。
- 引数の文字列に Credential の平文があれば `DENY` します。形のわかる Format: GitHub（`ghp_` など、`github_pat_`）、GitLab、`sk-` の Key、Stripe、AWS の Key ID、Google の API Key と OAuth Token、Slack の Token と Webhook、npm、PyPI、Hugging Face、SendGrid、Docker、DigitalOcean、JWT、Bearer / Basic、PEM / PGP の秘密鍵、`user:password@` 付きの URL。
  Token は Key 名に連結していても（`MYTOKEN_ghp_...`、`OPENAI_API_KEY_sk-...`、`key_AKIA...`）検出します（`_` の直前は英数字でなければよい。`disk-...` のような単語の途中は一致させません）。Zero-width 文字や全角文字で隠した形も検出します。
  ソースコードで普通に出る `password = "..."` は引数では拒否しません（Agent がコードを書けなくなるため）。
- **結果は、返す前と Log へ出す前に Redact します。** 検出した Credential のほか、`.env`・JSON・YAML・ini・`--password x` の代入の形（`DB_PASSWORD=...`、`AWS_SECRET_ACCESS_KEY=...`、`{"db_password": "..."}`。Key 名の前後に語が付いてよく、引用符つきの値は空白を含めて）は**値だけ**を `[REDACTED]` にします（Key 名は残ります）。Dict の Key 自体も Redact します。
  `password` / `token` / `api_key` / `secret` / `authorization` / `credential` などを含む Key の下の、数値・bool 以外の値は中身によらず置き換えます。JSON のデータでない Object（`repr` に何が入るか分からないため）は固定の Marker（`[UNSUPPORTED]`）です。
  1 つの結果は読み取りに上限（100,000 値、4,000,000 文字）があり、超えた分は `[TRUNCATED]` 1 つになります（200 万要素の List の Redact に 8.9 秒かかった問題への歯止め）。
- 平文を返す Tool（`returns_credential_plaintext=True`）は、登録しても**常に** `credential_plaintext_denied` です。Approval を渡しても変わりません。
- **限界:** 検出は形のわかる Format と代入の形だけの Best Effort で、すべての Secret を見つけることはできません（値が別の行にある YAML、Encode された Secret など）。本来の防御は、Credential を Agent の Context に入れない構造（handle のみ）です。

### Approval

`APPROVAL` / `STRONG_APPROVAL` の呼び出しは、まず `NEEDS_APPROVAL`（`approval_required` / `strong_approval_required`）を返し、**Approval の要求を作ります。** 同じ呼び出しの要求がすでに開いていれば、それを返します（`approval_pending`）。
人が承認したあと、`request(call, approval_id=...)` で使います。

| 段階 | 内容 |
| --- | --- |
| 承認者に見せるもの | 呼び出しの**全引数**を名前つきで（`summary`）。値は Redact・制御文字と方向制御文字を `\uXXXX` に Escape し、256 文字で切ります（切ったものには全長と SHA-256 の先頭 12 桁が付き、Hash が束縛するのは全文です）。**読める `summary` がない呼び出し（引数がない Tool）の承認は開かず**、`approval_not_displayable` にします |
| Hash | `call_hash` は Tool、**正規化した**引数、Task、Requester（User と Agent）の SHA-256。同じ呼び出しの別の書き方は同じ Hash、引数を 1 つ変えれば別の Hash。別の Task・User・Agent の同じ呼び出しは別の承認になります |
| 件数 | (Task, User) ごとに Open な（pending と、承認済みで未使用の）承認は `max_pending_approvals`（既定 10、1〜100）まで。超えたら `approval_limit_reached`（Event も行も作りません）。同じ呼び出しの再要求は数えません。(Task, User) の Advisory Lock で直列化するため、並行 Request でも超えません |
| 却下の後 | 却下した呼び出しは `rejection_cooldown`（既定 5 分、1 分〜24 時間）の間 `approval_cooldown`。引数を 1 つ変えれば別の呼び出しですが、件数の上限が量を抑えます |
| 単回 | 承認は 1 回だけ使えます（実行が失敗しても消費済みです）。`UPDATE ... WHERE status = 'approved' AND expires_at > now AND`（Task、Agent、User、Tool、Level、Hash がすべて一致）の 1 文で消費するため、同時に何個の呼び出しが来ても 1 つだけが成功します（再利用は `approval_already_used`） |
| 期限 | 作成から `approval_ttl`（既定 1 時間、1 分〜24 時間）。期限ちょうども期限切れです。期限切れは `approval_expired` |
| 別の呼び出し | 引数・Tool・Task・Agent・User・Level のどれかが違えば `approval_mismatch`（承認は消費されません） |
| 承認できる人 | Agent が働いている **User 本人だけ**（`ApprovalService.approve / reject`、引数は人間の `Principal`）。Agent 自身の ID は `self_approval`。他の人は Admin / Owner でも、存在を教えず `not_found`（Audit には `not_authorised`）。DB の CHECK 制約も、承認者が委任元 User であること、Agent が User と別であることを保証します |
| `STRONG_APPROVAL` | 承認のとき `StepUpVerifier.verify(user_id, approval_id)` が**明示的な `True`** を返す必要があります（PAW-023 が実装）。Verifier がない、`False`、例外、Timeout、`True` 以外の答えは `step_up_required` で、承認は保留のままです。Store の `decide` も `step_up_verified` を受け取り、Step-up なしには強い承認を保存しません（`step_up_verified` の列と CHECK 制約。Store を直接呼ぶ側にも効きます） |
| 取り消し | `ApprovalService.revoke`。委任元 User と、Admin / Owner（権利を減らす方向だけなので代われる）。pending・承認済みで未使用の承認だけ。Task の終了での取り消しは、下の「Task の終了と承認」。使うときは `approval_revoked` |
| 使うとき | 認可・Scope・Budget を**もう一度**判定します。承認は権限を広げません。拒否された使用は承認を消費しません |

`ApprovalService` は Broker と**別の Object**です。Agent の Runtime へは Broker（または Runner）だけを渡し、`ApprovalService` は渡さないでください（渡さなくても上の規則が守られますが、それが最初の防御です）。

#### Task の終了と承認

**Task の終わりは 3 つ**です。`completed`（完了）、`failed`、`cancelled`（`paw_backend.tasks.TERMINAL_STATES`）。Task の状態に `expired` はなく、承認は自分の `expires_at` で失効します（期限後は `approval_expired`。期限切れは取り消しの対象にもなりません）。
`failed` と `cancelled` は Retry / Restart で再び動きます。

- **正常時:** `TaskService(listeners=[approval_service.revoke_on_task_end])` を配線すると、終了の遷移が Commit された**後**に、その Task の Open な承認（pending と、承認済みで未使用）を全部取り消します（`revoked`、reason `task_ended`）。3 つの終わりの全部を `tests/test_tools_postgres.py` の `TaskEndPathsTest` が、本物の `TaskService` と Table で確かめます。
- **取り消しに失敗したとき（Store の障害）:** `revoke_task` / `revoke_on_task_end` は `ApprovalRevocationError` を**上げます**（以前は Log を出して `0`、つまり「Open な承認はなかった」と同じ戻り値でした）。`TaskService` は Listener の失敗を Log（型名だけ）に残し、Commit 済みの遷移は戻りません。**再試行する仕組みはありません**（`revoke_task` は冪等なので、後から呼び直せます）。
- **だから、Broker が独立に止めます。** 承認を要する呼び出しは、承認を**開く**ときも**使う**ときも、`TaskActivityProvider.check(task_id)` が `ACTIVE` を答えたときだけ進みます。終了した Task の承認は、Store がまだ `approved` と言っていても使えず（`task_not_active`）、消費もされません。終了した Task には新しい承認も開きません。Provider が失敗・Timeout・想定外の答えなら `task_state_unavailable`、Task が見つからなければ `task_unknown`（既定の `FailClosedTaskActivity` は常に不明: 本物の Provider を入れるまで承認を要する呼び出しは通りません）。
  `PostgresTaskActivity` は `tasks.state` を、Pool を使わない中断可能な接続で読みます。
- **再び動く Task:** Retry / Restart（終了状態からの遷移）でも Listener は Open な承認を取り消します。終了時の取り消しが失敗して残った承認は、再開した Task では使えず、新しい承認を求め直します。
- **範囲と限界:** 承認を要しない呼び出し（`AUTO` / `SCOPED_AUTO`）は Task の状態を見ません（終わった Task へ呼び出しを渡さないのは Orchestrator の責務です）。確認から `consume` までの間に Task が終わる競合は残ります（その呼び出しは確認の時点では動ける Task のものです。実行中の呼び出しは Task の `stop_now` / `cancel` が止めます）。
  判断の理由は [Decision 0006](../../docs/decisions/0006-tool-broker-policy.md) の「9. Task の終了と承認」（Proposed）。

**永続化（決定）: PostgreSQL に保存します。** 理由: 承認は Task が `waiting`（承認待ち）の間、Backend の再起動をまたいで残る必要があり（要件は Client の切断後も状態を保持）、
単回・期限・二重承認の保証は複数の Process が同じ行を更新できる Database でこそ成り立つためです。Audit Sink だけに書く案は、状態の読み出しも排他もできないため採りませんでした。

| Table | 内容 |
| --- | --- |
| `tool_approvals` | 承認の現在の状態（`pending` / `approved` / `rejected` / `consumed` / `revoked` / `expired`）、Task・Project・Agent・User、Tool、Level、`call_hash`、型つきの対象（正規化した Path / Host / Project）、**`summary`**、期限、`step_up_verified`、取り消した人と時刻 |
| `tool_approval_events` | Append-only の履歴（`requested`（`summary` つき）/ `approved` / `rejected` / `consumed` / `revoked` / `expired`） |

- 開いている承認は Exact な呼び出しごとに 1 つ（`call_hash` の Partial Unique Index）。Task、Project、Agent、User の ID に Foreign Key はありません（`task_events` と同じ方針）。
- 状態の変更と履歴の行は同じ Transaction です。期限切れは、変更しようとした時に `expired` へ移し、履歴に残します。
- `ApprovalListeners`（`ToolBroker(listeners=[...])`、`ApprovalService(listeners=[...])`）は、要求・承認・却下・消費・取り消しが保存された**後**に `ApprovalEvent`（ID と Enum だけ）を受け取ります。Task を `waiting` にする Orchestrator の接続点です。Listener の失敗や遅延は承認を失敗させず、例外の型名だけを Log に残します。
- テスト用に `InMemoryApprovalStore`（`approval_memory.py`。同じ規則。件数の上限つき。本番用ではありません）があります。両方の Store に同じ Test（`tests/tools_store_contract.py`）を実行します。

**DB が守る規則（Migration 0031）。** Application の Bug や侵害でも、次は Database が拒否します（Trigger はすべて `ENABLE ALWAYS`で、`session_replication_role = replica` でも効きます）。

- 承認の行は `pending` で作る（それ以外の INSERT を拒否）。
- 状態の変更は `pending` → `approved` / `rejected` / `revoked` / `expired` と `approved` → `consumed` / `revoked` / `expired` だけ。それぞれが自分の列だけを変える。Replay（`consumed` → `approved`）、`expires_at` の延長、`call_hash` / Tool / Level / 対象 / `summary` の書き換えは拒否。
- 承認・履歴の DELETE と TRUNCATE、履歴の UPDATE は拒否。
- CHECK 制約: 承認者は委任元 User だけで Agent ではない、強い承認は Step-up つき、`summary` は 1〜16 件の配列。

**Application の Role の権限（Migration の末尾の 1 ブロック）。** `PUBLIC` には何も与えません。`PAW_APP_DATABASE_ROLE` があれば、`tool_approvals` に SELECT・INSERT と**状態の列だけ**の UPDATE、履歴に SELECT・INSERT だけを与えます（DELETE・TRUNCATE・識別する列の UPDATE はなし）。
非 Superuser の Role で、書き換え、Replay、TRUNCATE、Trigger の無効化、他人を承認者にする UPDATE を試して拒否されることを Test しています（`tests/test_tools_postgres_roles.py`）。起動時の診断（`warn_about_loose_privileges`）は、承認の 2 つの Table への過剰な権限（Owner、全体の UPDATE、DELETE、TRUNCATE）と不足（INSERT できない）を警告します。

**承認と消費の Role の分離（実装しない。理由）。** Agent 側の Process が承認できない、を Database の権限で保証するには、承認する Process と Agent 側の Process が別の Role で接続する必要があります。
今の構成は Application の Role が 1 つで、その Role は合法な遷移（`pending` → `approved`）を実行できるため、**Application の Process が侵害されれば、その User の名前で承認を書ける**（承認者は委任元 User でなければならず、強い承認は `step_up_verified` を偽るだけ）ことは、Database では防げません。
分離には、承認の Endpoint 用の別 Role（と、その接続を持つ別 Process）が要ります。認証（PAW-022）と Step-up（PAW-023）の Endpoint ができる時に、承認の Endpoint だけが `UPDATE (status = 'approved' ...)` を実行できる構成（別 Role、または `SECURITY DEFINER` 関数）へ進めてください（Decision 0006 の後続の課題）。それまでは、Agent の Runtime に Application の Role の接続を渡さず、Broker だけを渡すことが前提です。

### Audit

すべての判定を既存の `AuditSink` に記録します。**ID と Enum だけ**で、引数、対象、結果、Model が書いた文字列は入りません。

| 項目 | 値 |
| --- | --- |
| `action` | `tool.<登録済みの Tool 名>`。登録されていない名前は `tool.unknown`（名前は保存しません）。承認は `tool.approval.approve` / `tool.approval.reject` / `tool.approval.revoke` |
| `resource_kind` / `resource_id` | Tool の呼び出しは `task` / Task の ID、承認は `tool_approval` / 承認の ID |
| `actor_id` / `agent_id` | 委任元 User / Agent（承認の記録では承認・却下・取り消した人、`agent_id` なし。Task の終了による取り消しは `actor_id` なし、reason `task_ended`） |
| `decision` / `reason` | `allow` / `deny` と `BrokerReason` の値。`AuditEvent.decision` は 2 値のため、承認待ちは `deny` + `approval_required`、承認を使った実行は `allow` + `approval_consumed`。実行後に `executed` / `execution_failed` の行を追加 |
| `correlation_id` | 呼び出しごと。同じ呼び出しの PAW-025 の認可の行と共通 |

`ToolCall` でない入力（帰属する Task も User も Agent もない）は、Audit の行を作らず Log（型を含まない固定の文）だけに残します。

Tool の実行を伴う記録（許可と実行後）は Fail-closed で、許可を記録できなければ拒否します。承認の承認・却下は外部の状態を変えないため、記録の失敗は Log（型名だけ）に残し、承認は有効なままです（変更と同じ Transaction の `tool_approval_events` が消えない記録です）。

### 差し込み口

| Protocol | 実装 | 既定 |
| --- | --- | --- |
| `ToolExecutor.execute(invocation)` | 各 Tool の実装（別 Issue） | なし（`ToolRunner` に必須）。契約は下の「Executor の契約」 |
| `BudgetProvider.check / charge` | PAW-033 | `FailClosedBudgetProvider`（予算なし = 予算が必要な Tool は拒否）。`check` は何も消費せず、同時の呼び出しは上限を少し超えうる。厳密な上限には PAW-033 が原子的な予約を追加する |
| `StepUpVerifier.verify` | PAW-023 | `FailClosedStepUp`（Step-up の承認はできない） |
| `TaskActivityProvider.check` | Deployment（`PostgresTaskActivity(database)`） | `FailClosedTaskActivity`（Task は不明 = 承認を要する呼び出しは拒否） |
| `PathResolver.resolve` | Deployment | `RealpathResolver`。`LexicalPathResolver` は Symlink のない環境の Test 用 |

`ToolRunner(execution_timeout=)` の既定は 600 秒（最大 24 時間。`None` は不可）。実行後の記録（Audit と Budget の Charge）は `finally` で `asyncio.shield` して書くため、Task が Cancel されても、実行後の処理が失敗しても残ります。

Adapter は Broker / Runner / Service の生成時に検査します（Async Method か、必要な引数の数か）。間違った Adapter は生成時に `TypeError` です。

### Executor の契約

Broker は呼び出しの**前**に判定します。次は、実際に実行する Executor（別 Issue）が守ることを前提にしています。

- **File**: `TaskScope.path_roots` の外へ Symlink をたどらずに開く（`openat2` の `RESOLVE_BENEATH`、`O_NOFOLLOW` など）。Broker の Symlink の確認は呼び出しの前で、確認後に変わりうる（TOCTOU）。
- **Network**: `ToolInvocation.arguments` の URL の Host に接続する。**Redirect は自動でたどらず**（たどるなら、移動先の Host を Task の Host と handle の使える Host で再確認する）、**DNS は接続時に引いた IP を確認する**（DNS Rebinding、Loopback・Private・Link-local への接続の拒否）。Broker は名前を字句で確認するだけで、名前が指す IP は見ません。
- **Credential**: handle を解決して付けるのは Executor だけ。handle が使える Host にだけ Credential を付ける。平文を結果や Log に出さない（出ても Runner が Redact する）。
- **Memory / Project の ACL**: Project や Memory を読む Tool は、Broker が確認した Project の Scope の中だけを、PAW-040 の ACL 条件（`readable_memory_versions`）つきで読む。Broker は Project の Scope を確認するが、Memory 単位の ACL は Tool の中の責務。
- **TaskContext は呼び出しごとの新しい Snapshot**: Orchestrator は Task の Scope・Grant・Project の状態を**呼び出しごとに**現在の値から作って渡す（Project の Archive、Task の Scope の変更、Grant の縮小が次の呼び出しから効く）。Broker は渡された Context をそのまま信頼します。

### 既知の制限と判断

- **承認が広げるのは Level だけです。** 委任できない Capability（`admin.*` など）を持つ Tool は、承認があっても `authz_denied` です（PAW-025 の許可リストのまま。Decision 0004 が PAW-031 に残した点として、この実装は「Approval で委任不可の操作を Agent に許す仕組みは作らない」を選んでいます）。
- 承認できるのは委任元 User だけで、Admin / Owner が他の User の Task を承認する仕組みはありません（取り消しはできます）。
- 外部 write と外部の読み取りの許可は `TaskScope.hosts` と、承認（正確な URL を見て 1 回）で表しています。Issue 作成や PR 作成といった「目的」の単位ではありません。
- 承認者の表示は各引数の 256 文字までです。長い値は全長と Hash の先頭だけが付きます。承認 UI（PAW-022 以降）は `summary` を表示してください。
- **Repository の ACL で判定できない呼び出し。** Path も `repository` 引数もない Tool（`tests.run` のように作業ディレクトリを暗黙に使うもの）は、Project の Resource で判定します。Repository の ACL を効かせたい Tool は、触れる Path か `repository` を必須引数にしてください。作業対象の Repository を `TaskScope.repositories` に正しく並べること（Worktree の Path、ACL）は Orchestrator の責務で、Broker は渡された値を信頼します。
- 引数のない Tool（`host.reboot` など）は承認を開けません（`approval_not_displayable`）。承認が要る Tool は、何をするかを表す引数（対象、理由）を必須にしてください。
- Approval の期限は 1 つです（承認してから使うまでの猶予は別にありません）。期限は Application の時計で比較します（Database の時計ではありません。複数の Host の時計のずれ、Test の時計の注入のため）。
- 却下の Cooldown は Hash 単位で、引数を変えた別の呼び出しは止めません（件数の上限が量を抑えます）。
- `check` と `charge` の間の競合、Symlink の確認と使用の間の競合（TOCTOU）、Credential 検出が Best Effort であることは上に書いたとおりです。
- Model の出力から `ToolCall` を作る Adapter は JSON を `benchmarks/json_input.decode_json` と同じ厳密さ（重複 Key、`NaN` を拒否）で読んでください。Broker は Mapping を受け取り、Key と値の型を上の規則で検査します。
- **後続の課題:** 承認する Process と Agent 側の Process の Role の分離（上）、承認 UI と一覧の Endpoint（PAW-022）、`SECURITY DEFINER` 関数による遷移の限定、Database の時計での期限、`TaskService` への `revoke_on_task_end` の配線と `PostgresTaskActivity` の注入（PAW-034）、終了時の取り消しに失敗した Task の再取り消し（今は `revoke_task` を呼び直す）、共通 Helper（`paw_backend.db_roles.grant_app_privileges`）による GRANT の置き換え。

## Memory / Conversation Schema

[PAW-040](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/34)（Revision `0040`）で実装した Schema です。
設計は [Memory Architecture](../../docs/MEMORY_ARCHITECTURE.md) と [要件](../../REQUIREMENTS.md) の Memory の節に従います。
Repository / Service は含みません。

| 層 | Table | 内容 |
| --- | --- | --- |
| Raw Conversation | `conversations`、`messages` | 発言、Tool 結果、Agent 結果、Task の経緯。無期限に保持し、LLM Context へ全文は入れない |
| Session state | `session_states` | Conversation ごとの要約と作業状態（1 Conversation に 1 行） |
| Long-term Memory | `memories`、`memory_versions`、`memory_relations`、`memory_sources`、`embedding_models`、`memory_embeddings` | 確定した知識。Version、関係、出典、Embedding |

3 つの層は別の Table で、Foreign Key でつながるのは出典（`memory_sources`）だけです。
Conversation を消しても Memory と他の出典は残り（`ON DELETE SET NULL`）、Session state と Message は一緒に消えます。
出典の `conversation_id` と `message_id` は組で検証し（複合 Foreign Key）、Message がその Conversation のものでなければ DB が拒否します。
Message だけを消すと `message_id` だけが NULL になり、Conversation の出典は残ります。Message を指す出典は Conversation も指してください（Conversation が NULL の組は検証されず、Conversation 単位の検索から漏れます）。

**Scope と ACL。** 各 Version が `scope`（`user` / `project` / `project_group` / `repo` / `shared`）を持ち、Scope に対応する ID を 1 つだけ持ちます
（`owner_user_id` / `project_id` / `project_group_id` / `repo_id`、`shared` は無し）。CHECK 制約が組み合わせを強制します。
`project_group` は、要件の Inferred Preference の例（自由入力「開発系の Project だけ適用」を `scope: project_group` へ構造化）を保存するための Scope です。
要件は Project Group の実体、Member、権限を定義していません。そのため Schema は Group の ID（素の UUID）だけを持ち、
`Principal.project_group_ids`（呼び出し側が決めた、読める Group の ID）に含まれる場合だけ読めます。
Project が Group に属していても、それだけでは Group の Memory は読めません（既定は拒否）。
権限の判定は SQL で行います。`paw_backend.memory.acl` の `readable_memory_versions(principal)` を、
`memory_versions`（と、それを Join する `memory_embeddings`、`memory_sources`、`memory_relations`）を読む全ての Query に付けます。
Vector 検索でも、順位付けの前に付けるため、見えない行が順位に入ることはありません。
`Principal` は User の実効的な権限（読める Project、Project Group、Repo の ID）で、RBAC と Membership から Backend が決めます。
Repo は既定で Project の権限を継承し、Repo 単位の ACL override で外された Repo は `repo_ids` に入れません。
Memory ごとの権限の写しは持ちません（Member の変更で古くなり、漏れの原因になるため）。
Scope 別の Index が `status = 'active'` の絞り込みとあわせて ACL 条件を支えます。
どの Foreign Key にも先頭 Column の Index があり（Conversation や Message の削除が子の Table を全走査しない）、Test が全 Foreign Key を検査します。
Raw Conversation は所有者だけが読めます（`readable_conversations`）。Admin にも本文は見せません。

**Version と履歴。** 編集は新しい `memory_versions` の行です。旧 Version は消さず `status`（`active` / `superseded` / `deprecated` / `history`）を変えます。
1 つの `memories` に `active` は最大 1 行（Partial Unique Index）で、`(memory_id, version_number)` の Unique が楽観ロックを兼ねます。
`memory_relations` が Version の関係（`supersedes`、`extends`、`conflicts_with`、`confirmed_from`、`revalidated_from`、`merged_from`）を新しい側から古い側へ持ちます。
自分自身への関係と、同じ Version を複数の Version が supersede することは DB が拒否します。
Scope を広げる編集は新しい Version で行うため、旧 Version は元の Scope のまま非公開です。
`confirmation_state`（`observed` / `inferred` / `confirmed` / `rejected`）、`freshness_policy`（`permanent` / `revalidate` / `repo_commit` / `expiring` / `session_only`）と、
方針ごとの必須項目（`verified_at`、`revalidate_after`、`commit_sha`、`expires_at`）、`actor`、`change_reason` も Version が持ちます。

**User / Project / Repo の ID は Foreign Key なし。** Project と Repo の Table はまだありません（PAW-026 / 027）。`users`（PAW-021）はありますが、Migration の順序が統合後に決まるため、この Schema からの外部キーは付けていません。
`owner_user_id`、`project_id`、`project_group_id`、`repo_id`、`actor_user_id` は素の UUID Column で、DB は存在を確認しません。
Backend は検証した ID だけを書いてください。Table ができた後の Migration で Foreign Key を追加できます。
Task、Repo 解析、Project Decision の出典も、Table がないため `memory_sources.source_ref` の不透明な文字列です。

**Application の Role の権限。** Migration は `PAW_APP_DATABASE_ROLE` の Role に、Table ごとに必要最小限を与えます（[上の規則](#migration-は-application-の-role-に権限を与えるcontributor-向けの規則)）。
未設定のときは何も与えません。TRUNCATE、ALTER、DROP、GRANT は誰にも与えません。

| Table | 与える権限 | 理由 |
| --- | --- | --- |
| `conversations` | SELECT、INSERT、DELETE、UPDATE（`title`、`updated_at` のみ） | 会話の削除は製品の機能。所有者 `owner_user_id`（ACL の境界）と Project / Repo は変更不可 |
| `messages` | SELECT、INSERT | Raw Conversation は追記のみ。履歴を書き換えない。会話ごとの削除は下記の Cascade |
| `session_states` | SELECT、INSERT、UPDATE（`summary`、`state`、`summarized_through_sequence`、`updated_at`） | 要約と状態は会話の進行で更新する。削除は会話と一緒（Cascade） |
| `memories` | SELECT、INSERT、DELETE | 更新する列は無い。DELETE は Memory 全体の削除（会話と関連 Memory の削除、Shared Memory の Admin 削除、User 削除時の Private Memory の消去）で、Version は Cascade で消える |
| `memory_versions` | SELECT、INSERT、UPDATE（`status`、`stale_since`、`pinned`、`importance` のみ） | Version は書き換えない（編集は新しい Version）。本文、Scope と ACL の列、`confirmation_state`、鮮度の設定は変更不可。DELETE は与えず履歴を残す |
| `memory_relations` | SELECT、INSERT | 履歴 Graph の辺は追記のみ |
| `memory_sources` | SELECT、INSERT、UPDATE（`source_deleted_at` のみ） | 出典は追記のみ。会話の削除で失われた出典を記録する列だけ更新できる |
| `embedding_models` | SELECT、INSERT | Benchmark で決めた Model の登録。次元は変えず、Model の廃止は管理者が行う |
| `memory_embeddings` | SELECT、INSERT、DELETE | 再生成できる派生データ。再生成や Model の廃止で削除する。書き換えはしない |

`memories` の DELETE は、要件が書く削除の流れ（Conversation と関連 Memory の削除、Shared Memory の Admin 削除、User 削除時の消去）のために与えています。
物理削除を Application の Role に持たせたくない場合は、削除だけを行う別の保守用 Role（Owner が実行する Job など）へ移す方法もあります。

外部キーの `ON DELETE CASCADE` / `SET NULL` は Table の Owner の権限で実行されます。
そのため Application は、会話を削除すると Message と Session state が消え、出典の参照が NULL になり、
Memory を削除すると Version、関係、出典、Embedding が消えますが、それらの Table への DELETE / UPDATE 権限は持ちません。
Application の Bug や侵害でも、Version の本文や履歴の関係、Raw Conversation を書き換えたり個別に消したりできません。
Version の `status` は列の UPDATE で変わるため、遷移の正しさ（`superseded` を `active` に戻さない等）は Service が守ります。
`downgrade` は Owner の Role が実行し、Table と一緒に権限も消えます。

**pgvector。** Migration が `CREATE EXTENSION IF NOT EXISTS vector` を実行します（Migration の Role に権限が必要。管理者が先に作成済みでもよい）。
`memory_embeddings` は `(memory_version_id, embedding_model_id)` が Key で、`embedding` は **次元を固定しない** `vector` です。
Embedding Model と次元は Benchmark（PAW-019）で決めるため、まだ決めていません。Migration は Model を 1 件も登録しません。
Model の登録は `embedding_models`（Model ID と次元）への通常の INSERT です。
`memory_embeddings` は `(embedding_model_id, dimensions)` でこの Table を参照し、`dimensions` と実際の次元は CHECK で一致させます。
そのため 1 つの Model は 1 つの次元だけを持ち、別の次元の Vector は DB が拒否します。Embedding がある間は、Model の次元の変更も Model の削除もできません。
次元の異なる Vector 同士の距離は計算できないため、近傍検索は先に 1 つの `embedding_model_id` に絞ります（この絞り込みで次元の不一致は起きません）。
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

[PAW-021](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/18)（Owner Setup）、
RBAC（PAW-025）、Task Lifecycle（PAW-032）、Task Queue / Budget / Loop 検知（PAW-033）、Tool Broker（PAW-031）、Memory Schema（PAW-040）は、この Skeleton の上に実装済みです。
PAW-022（Login / Session / Password）と PAW-023（Passkey / Step-up）は Owner Setup の Token を受け取る側で、まだありません。
Memory の保存・整理・検索は PAW-041 以降で、Memory Schema の上に実装します。
受け入れ基準は [Implementation Backlog](../../docs/IMPLEMENTATION_BACKLOG.md)、
実装時に選択できる事項は [Requirements Freeze Review](../../docs/REQUIREMENTS_FREEZE_REVIEW.md) を参照してください。
