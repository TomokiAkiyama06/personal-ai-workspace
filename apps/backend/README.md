# Backend

Personal AI Workspace の Core Backend です。
[PAW-020](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/17) で、後続の Issue が載る最小の Application Skeleton を実装しました。
Login と Session はまだ実装していません（PAW-022 以降）。
RBAC と Audit（PAW-025）、Task の Lifecycle と永続化（[PAW-032](#agent-task-lifecycle)、HTTP の Endpoint はまだありません）、Task Queue・Budget・Loop 検知（[PAW-033](#task-queue--budget--loop-検知)）、Memory の PostgreSQL Schema（[PAW-040](#memory--conversation-schema)）、
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
├─ migrations/             # env.py と Revision（0001 は空の Baseline、0021 は users / setup_tokens、0033 は Queue / Budget / Loop、0040 は Memory Schema）
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
│  ├─ memory/              # Memory / Conversation の Model、ACL 条件、vector 型、Pin / Importance 変更の Actor（PAW-040）
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
**Multi-Repo Task の Working Set（Repo の集合と `referenced` / `working` / `target` の役割、Repo ごとの worktree / Review / PR の状態）は PAW-032 に含みません。**
PAW-032 の受け入れ条件は Task に 1 組の worktree / review / PR 状態の復元までで（Backlog）、Working Set が指す Repository の登録（PAW-027）はまだなく、
Repo ごとの Git 状態と統合は PAW-035、Write 範囲の強制は Tool Broker（PAW-031）の責務だからです。
Working Set の単位、Single-Repo との関係、Repo 追加の承認、Task の完了条件など、要件が決めていない判断があるため、
[Decision 0014](../../docs/decisions/0014-task-working-set-persistence.md)（Proposed、Human の承認待ち）で提案しています。
したがって、`task_attempts` の worktree / Review / PR は 1 つの Repo の状態で、どの Repo かは記録せず、`TaskSnapshot`（`restore()`）も Working Set を返しません。

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
| `task_tool_invocations` | Step が呼んだ Tool の実行状態（下記）。ID、Tool 名、状態（`started` / `succeeded` / `failed` / `interrupted`）、開始・終了時刻だけを持つ。`started` の行だけの Partial Index（`step_id`）と、終了済みの行だけの Partial Index（`step_id`、開始の新しい順、`id` の新しい順）がある |
| `task_logs` | 試行ごとの Log（`debug` / `info` / `warning` / `error`）。行は書いた Run（`attempt` と `retry_count`）を持つ。Index は `(task_id, attempt, seq DESC)`（下記の `restore`） |
| `task_events` | Append-only の履歴。全遷移について、Command、遷移前後の状態、`wait_reason`、Actor（`user` / `system` / `policy` と User の UUID）、理由、その時点の Step 名、`task_version`、Event 後の Run（`attempt` と `retry_count`。Start では Worker の Run） |

- `project_id`、`created_by`、`actor_id` は UUID だけを持ち、外部キーはありません。projects の Table がまだ存在せず、`users`（PAW-021、Revision `0021`）は Migration の順序が統合後に決まるためです（両方が揃った後の Revision で外部キーを追加します）。
- **文字列入力の検証**: `TaskService` が受け取る文字列（`title`、`starting_commit`、`agent`、`model`、`reason`、Step 名、Tool 名、Log の `message`、`update_attempt` の branch / path / head commit / PR の URL）は、NUL（`\u0000`）と Surrogate 文字（不正な Unicode）を含むと `InvalidCommandArgumentError` で拒否します（エラー文に値は含めません）。PostgreSQL の text 列は NUL を保持できず、Surrogate は UTF-8 にできないため、そのままでは書き込み時に DB / 符号化のエラーが漏れます。Log の `message` は、長さの上限で切り捨てる前の全体を検査します。`update_attempt` は、branch（255 文字）、path（1024 文字）、head commit（64 文字）、PR の URL（2048 文字）を、Model の列の長さ（1 か所の定義）で検査して、超えると同じ `InvalidCommandArgumentError` で拒否します（文字数で数えます）。PR の番号は 1 から 2147483647（`INTEGER` 列の最大値）の整数だけを受け付けます（`bool`、`float`、文字列は拒否します）。下限の 1 は、PR の番号が正であることに基づく私の判断で、要件が定める値ではありません。
- **Task `input` の検証**（`TaskService.create_task`）: `input` は JSON Object で、`json.loads` が返す型（`dict`〔キーは `str`〕、`list`、`str`、`int`、`float`、`bool`、`None`）だけを受け付けます。整数キーや `tuple` などを黙って変換して保存することはしません。次のものは、DB へ書く前に `InvalidCommandArgumentError` で拒否します（エラー文に値は含めません）。
  - `NaN` / `Infinity` / `-Infinity`（PostgreSQL の JSONB は保持できず、書き込み時に DB のエラーになります）、NUL（`\u0000`）を含む文字列やキー、Surrogate 文字（不正な Unicode）を含む文字列やキー
  - 入れ子が `MAX_INPUT_DEPTH`（32 段。最上位の Object を 1 段と数え、Object と List の両方が段になります）を超えるもの、循環参照
  - JSON にした長さが `MAX_INPUT_BYTES`（256 KiB）を超えるもの。エンコードする前に、値が現れるたびにエンコードされる長さを積み上げる予算で検査するため、同じ値や List を何度も共有して展開すると巨大になる構造も、エンコードや DB への送信に至る前に拒否します。整数は 10 進の桁数（負数は符号も）、`float` は `repr` の長さ、`true` / `false` / `null` は 4 / 5 / 4 文字を、出現のたびに積み上げます（1 つの巨大な整数を何万回も参照する入力が、エンコードで数百 MB になることを防ぎます）。文字列は文字数（エンコードは最大 12 倍）、Object と List は括弧だけを積み上げ、区切りは積み上げないため、積み上げ量がエンコード後の長さを超えることはなく、エンコード後の長さの検査が最終的な判定です。
  - 桁数が `MAX_INPUT_INTEGER_DIGITS`（131072 桁）を超える整数。PostgreSQL の JSONB は数値を `numeric` で保持し、`numeric` は小数点より上に 131072 桁までしか持てないため（超えると `value overflows numeric format`）、その値です（実際の PostgreSQL で 131072 桁は保存でき、131073 桁は拒否されることを Test で確認しています）。Python が整数と文字列を相互に変換する桁数の上限（`sys.get_int_max_str_digits()`。既定は 4300）が設定されていて、これがより小さい場合は、その桁数が上限になります（`json.dumps` がそれを超える整数のエンコードを拒否するためです）。桁数の多い整数は、ビット長だけで判定して拒否し、巨大な整数を文字列に変換することはありません。
  `MAX_INPUT_DEPTH`、`MAX_INPUT_BYTES`、`MAX_INPUT_INTEGER_DIGITS` は `paw_backend/tasks/service.py` の定数で、`MAX_INPUT_DEPTH` と `MAX_INPUT_BYTES` は要件が定める値ではなく暫定の上限、`MAX_INPUT_INTEGER_DIGITS` は PostgreSQL の限界です。JSONB は数値を正規化するため、`-0.0` は `0.0`、`1e300` は整数として読み戻されます（値の意味は変わりません）。
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
- **Run（試行と Retry 回数）**: Worker の記録は、担当する **Run**（`TaskRun(attempt, retry_count)`）を明示します。`attempt` は Restart が 1 増やし、`retry_count` は Retry が 1 増やします。どちらも増えるだけで、失敗または中止した Task の再開は必ずどちらか一方を変えるため、2 つの Run が等しいのは同じ Run のときだけです。Retry は**同じ試行**を再実行する（試行番号は変わらない）ので、試行番号だけでは、失敗した Run の Worker と Retry が始めた Run の Worker を区別できません。`TaskRun` は Tool Broker（PAW-031）が承認を結びつける Run と同じ組です。
  Worker は、自分を開始した Start の `TaskEvent.run`（`task_events` に `attempt` と `retry_count` がある）から Run を受け取ります。`TaskSnapshot.run` も同じ値です。
  - Run を明示する書き込み（**現在の Run でなければ何も書かずに拒否**）: `begin_step(task_id, name, run=...)`、`add_log(task_id, message, run=...)`、`update_attempt(task_id, run=..., worktree=... / review=... / pull_request=...)`。Restart が新しい試行を始めた後の旧試行の Worker は `StaleAttemptError`（`stale_attempt`）、Retry が新しい Run を始めた後の（同じ試行の）失敗した Run の Worker は `StaleRunError`（`stale_run`）になります。`StaleAttemptError` は `StaleRunError` の派生で、「自分は置き換えられたか」だけを知りたい Worker は `StaleRunError` を捕まえれば両方を扱えます。試行番号を先に、次に Retry 回数を比べます。`run` が `TaskRun` でない値（試行番号だけの整数など）は、DB に触れる前に `InvalidCommandArgumentError` です（古い `attempt=` の引数はなくなりました）。
    - `update_attempt` と `begin_step` は Task 行の Lock を取った後に Run を比べます。Retry と競合しても、先に Commit された Retry の後の古い Worker が新しい Run の worktree / Review / Evaluator 結果 / PR の状態を上書きしたり、新しい Run の Step として始めたりすることはできません（Test は、Retry を Lock の先頭に並べて、古い Worker の書き込みがその後ろで拒否されることを確認します）。
    - `add_log` は Lock を取らないため（Log の書き込みを Task の状態変更と直列にしないため）、Retry と同時に Commit される行があり得ます。そこで行は、Task の現在の Run ではなく**書いた Worker の Run**を持ちます（`task_logs.retry_count`、`LogEntry.retry_count` / `LogEntry.run`）。Retry は同じ試行の Log を続けるので `restore` の `recent_logs` には前の Run の行も出ますが、どの Run の行かは区別できます（Restart との競合で行が試行番号を保つのと同じ考え方です）。Service が自分で書く Stop Now の行は、その時点の Task の Run を持ちます。
  - Run を明示しない書き込み: `finish_step` は Step の ID、`begin_tool_invocation` は実行中の Step の ID、`finish_tool_invocation` は Tool の ID で対象を指定します。これらは ID だけで Run を区別できます。Retry は Fail の後にしか起きず、Fail は実行中の Step を終わらせ、その Step の `started` の Tool も `interrupted` にするため、前の Run が残した Step や Tool は、Retry 後の Run では `running` / `started` ではありません。前の Run の Worker が後から `finish_step` や Tool の呼び出しをすると `TaskStepError` になり、新しい Run の Step や Tool は変わりません（Test で確認しています）。Restart の場合は、これらも `StaleAttemptError` です。
  - この Run の扱いは、要件に定めのない製品上の方針ではなく、既存の Restart の保証（`StaleAttemptError`）を Retry へ広げた実装の詳細なので、Decision には上げていません（`docs/decisions/0014-task-working-set-persistence.md` は Working Set の永続化についてで、関係しません）。
- **Tool の実行状態**: 要件の「Tool execution state」のうち、Backend が再接続後に再開または中断を判断するのに必要な最小の記録だけを持ちます。
  `begin_tool_invocation` / `finish_tool_invocation` が Tool の ID（Tool Broker が UUID を渡すこともできる）、Tool 名、状態、時刻を記録し、`restore` は現在の Step の Tool を `TaskSnapshot.tool_invocations`（開始が古い順）で返します。`started` のままの Tool は、後から何件開始・終了しても**すべて**返します（Backend が再開または中断を判断できなくなる取りこぼしを避けるため）。終了済みの Tool だけは直近 100 件に絞ります。返す件数が呼び出し側の操作で際限なく増えないよう、1 つの Step で同時に `started` にできる Tool は 1000 件（`MAX_ACTIVE_TOOL_INVOCATIONS`。**暫定の値で、人間の確認待ちです**。下の「人間の判断が必要な点」）までで、1001 件目の `begin_tool_invocation` は `TaskStepError` になります（どれかが終了すると、また開始できます）。
  `started` の Tool を尋ねる 3 つの問い合わせ（`begin_tool_invocation` の同時数の確認、`restore` が返す `started` の Tool、Step の終了時に行う `interrupted` への更新）は、Step の終了済みの Tool の履歴全体を読みません。`status = 'started'` の行だけの Partial Index `ix_task_tool_invocations_started`（`step_id`）を使うためです。履歴が長い Step でも、Tool を開始するたびの作業量が、その Step の Tool の総数ではなく同時に `started` の数だけで決まります。`started` は Bind Parameter ではなく SQL の文面へ書きます（`_tool_call_started()`）。Parameter にすると、Driver が何度も実行する文を Prepare して PostgreSQL が Plan を使い回す場合に、Partial Index の条件を満たすと判断できず、Index を使えなくなるためです（Test は Plan を使い回す設定でも Index を使うことを確認します）。
  `restore` が返す終了済みの Tool の問い合わせ（開始の新しい順、`id` の新しい順に 100 件）にも、専用の Partial Index `ix_task_tool_invocations_finished`（`WHERE status <> 'started'`、列は `step_id`、`started_at DESC`、`id DESC`）があります。PostgreSQL はこの Index を並び順のまま読み、100 件で読み取りを止めるため、再接続のたびの作業量が、その Step が積み上げた終了済みの Tool の総数に比例しません（全行を読んで並べ替えることも、`started` の行を読み飛ばすこともしません）。`status <> 'started'` も SQL の文面へ書きます（上と同じ理由です）。Test は、終了済みの Tool が 2 万件ある Step で、Plan を使い回す設定（`force_generic_plan`）でも値ごとに立てる Plan（`force_custom_plan`）でも、Seq Scan と並べ替えがなく、この Index が 100 行だけを読むことを確認します。
  **引数と出力は保存しません。** 権限判定、承認、引数と結果の扱いは Tool Broker（PAW-031）の責務です。Step が終わる（Stop Now / Fail / Restart / `finish_step`）と、`started` のままの Tool は `interrupted` になります。
- `TaskService.restore(task_id)` は DB だけから Snapshot（状態、current step、直近の Log、worktree / review / PR の状態、直近の Event）を作ります。1 つの Repeatable Read Transaction で読むため、同じ時点の値です。
  状態は Process のメモリに持たないので、Client が切断しても、Backend が再起動しても、別の Process が同じ値を返します。
  `restore` の Log の問い合わせ（現在の試行の行を `seq` の新しい順に `log_limit` 件まで）は、Index `ix_task_logs_task_id_attempt_seq`（`task_id`、`attempt`、`seq DESC`）が受け持ちます。PostgreSQL は現在の試行の位置へ直接移り、その行だけを並び順のまま読んで件数で止まるため、Restart で前の試行の Log が何万行残っていても、再接続のたびの作業量は返す行数で決まります（以前の `(task_id, seq)` の Index では、新しい方から前の試行の行を読んで捨てながら遡っていました）。`(task_id, seq)` の Index は残していません。Log を試行をまたいで読む問い合わせが今はなく、外部キー `task_id` の確認には新しい Index の先頭の列が使えるためです。書き込みの多い Table なので Index を 1 つ減らします。Log を試行をまたいで読む機能を足すときは、その問い合わせに合う Index を、そのときに足してください。`restore` の他の問い合わせは、すでに専用の Index があります（現在の Step は `UNIQUE (task_id, attempt, sequence)`、直近の Event は `(task_id, seq)`、試行の一覧は `UNIQUE (task_id, number)`）。Test は、前の試行の Log と Step が 2 万件ずつ、他の Task の Log と Event が合わせて数万件ある Task で、Plan を使い回す設定（`force_generic_plan`）でも値ごとに立てる Plan（`force_custom_plan`）でも、Seq Scan と並べ替えがなく、Log は現在の試行の行（3 行と 0 行）だけ、現在の Step と直近の Event は 1 行だけを読むことを確認します。
- `TaskService(database, listeners=[...])` の Listener は Commit 後に、書き込まれた `TaskEvent` を受け取ります。Audit（PAW-025）の接続点です。Listener の失敗は Command を失敗させず、例外の型名だけを Log に残します。
  取りこぼしを避けたい Consumer は `task_events` を `seq` で読んでください（`TaskService.history(task_id, after_seq=...)`）。
- 実行中 Task の Runtime 状態（実行中 Process など）の復旧は、要件どおり V1 では保証しません。`tool_invocations` が `started` のままの Task は、Backend が再開または中断を判断するための記録で、Process が生きている保証ではありません。

**人間の判断が必要な点（暫定の上限）。** 次の上限は、要件が定める値ではなく、この実装が置いた**暫定の値**です。人間が確認するまで、既定として承認済みとはみなしません。値は `paw_backend/tasks/service.py` の定数で、変えても Schema は変わりません。

- `MAX_ACTIVE_TOOL_INVOCATIONS` = 1000: 1 つの Step で同時に `started` にできる Tool の呼び出しの数。`restore` が返す件数を、呼び出し側の操作で際限なく増やさないための上限です（超える `begin_tool_invocation` は `TaskStepError`）。
- `MAX_INPUT_DEPTH` = 32: Task の `input` の入れ子の深さ。
- `MAX_INPUT_BYTES` = 256 KiB: Task の `input` を JSON にした長さ。

`MAX_RESTORE_TOOL_INVOCATIONS`（`restore` が返す終了済みの Tool の件数、100）、`MAX_RESTORE_LOGS`（1000）も、同じく要件が定めない実装の値です。

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
| `budget_usages` | SELECT、INSERT、UPDATE は `preset`、`limit_value`、`consumed`、`running_since`、`runtime_generation`、`settled_through` の列だけ。DELETE なし | `set_preset` は INSERT ... ON CONFLICT DO UPDATE（`preset`、`limit_value`）。`record` と `stop_runtime` は `consumed` への原子的な加算、`start_runtime` / `stop_runtime` は `running_since`、`start_runtime` は `runtime_generation` への加算、`stop_runtime` は停止の Cutoff である `settled_through`。キー（`task_id`、`kind`）と `created_at` は変更できず、Budget は削除されない |
| `loop_failure_signatures` | SELECT、INSERT、DELETE。UPDATE なし | `record_failure` が追加し（試行を確認するため `tasks` の行を `FOR SHARE` で Lock する。PAW-032 で付与済みの `tasks` の SELECT と列単位の UPDATE で足り、追加の権限は不要）、Window から外れた行を削除する。`clear_previous_attempts`（Restart の後の掃除）は、Task の現在の試行より前の試行の行だけを削除する（現在の試行との比較も `tasks` の SELECT で足りる）。この Table は Hash の Window で履歴ではない（何が起きたかの記録は `task_events`）。保存された失敗は編集できない。PAW-033 で Application が行を削除するのは、ここだけ |

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
**Claim の世代（Fencing token）。** Lease を識別するのは Entry の `id` と Worker の id だけでは足りません。Lease が切れた Worker の Entry が、**同じ Worker id**（設定で固定した id の再起動した Process など）に再び Claim されると、まだ動いている古い実行は、`claimed_by` も新しい Lease も満たしてしまい、新しい Claim の Heartbeat・返却・完了を行えてしまいます。そこで、Claim のたびに 1 増え、減ることも戻ることもない `claim_count`（Reclaim も、`release` 後の再 Claim も数える）を Lease の世代とします。`claim_next` が返す `QueueEntry.claim_count` を、Worker は `heartbeat(entry_id, worker_id, claim_count)`、`release(...)`、`complete(...)` の**必須の引数**として渡します（省略できると、渡し忘れた呼び出しが保護されないため、必須です）。Entry の現在の `claim_count` と違う世代は、Worker id が同じでも `LeaseLostError` になり、何も変更しません。`claim_count` は 1 以上の `int` で、範囲外・`bool`・`None` は `InvalidQueueingArgumentError("claim_count")` です（[Decision 0007](../../docs/decisions/0007-task-queue-budget-and-loop-policy.md) の 7）。
**時計。** Queue が信頼する時計は **Database の時計だけ**です。`enqueued_at`、`claimed_at`、`lease_expires_at`、`finished_at` と「Lease が切れたか」の判定は、全て SQL の中で PostgreSQL の `clock_timestamp()`（評価した瞬間の壁時計）を使います。`now()` は使いません（提案は [Decision 0007](../../docs/decisions/0007-task-queue-budget-and-loop-policy.md) の 6。承認待ちです）。`now()` は Transaction の開始時刻で固定されるため、行の Lock を待った後の判定に、待つ前の古い時刻が使われてしまいます。実際の動作は次のとおりです。
- **1 つの文の中では時計を 1 回だけ読みます。** 時計を使う文は、先頭に `WITH clock AS (SELECT clock_timestamp() AS ts)` を付け、文の中の「現在の時刻」と「現在 + `lease_seconds`」は全てこの CTE の 1 つの値を参照します（揮発性の関数を含む CTE は 1 回だけ評価されます）。そのため、Lease を与えるときの `claimed_at` と期限はちょうど `lease_seconds` 離れます（`clock_timestamp()` を文の中に 2 回書くと、2 回読まれて数マイクロ秒ずれます）。
- **文をまたぐと、文ごとに読み直します。** `claim_next` は 1 Transaction の 2 文で、先頭の行を選ぶ文（期限切れの判定）と、その行を更新する文（`claimed_at` と期限）が別々に時計を読みます。後者の値は前者の値以後です。`SKIP LOCKED` は待たず、選んだ行は自分が Lock しているので、その間に Lease の状態は変わりません。
- **Lease を判定する更新は、行の Lock を先に取ります。** `UPDATE` は行の Lock を待つ**前**に `WHERE` を判定し、Lock を持っていた側が Rollback した場合は判定し直さないので、`clock_timestamp()` を使うだけでは不十分です。そこで `heartbeat` / `release` / `complete` は、まず行を `SELECT ... FOR UPDATE` で Lock し（待つのはこの文）、次の文で、Lock を得た後に読んだ時刻で Lease の期限を判定して更新します。
- **Lease を判定しない文は、待つ前に読んだ時刻を保存します。** `enqueue` の `enqueued_at` と `cancel` の `finished_at` は 1 つの文で書くため、同じ Task の別の `enqueue`（未 Commit）や、`cancel` の対象行の Lock を待つと、待つ前に読んだ時刻（待った時間だけ古い値）になります。先着順と記録のための時刻で、Lease の有効・失効は決めません。

Worker が各自の時計を渡す方式では、時計が進んでいる Worker や誤って未来の時刻を渡した呼び出しが、まだ有効な Lease を「切れた」と判定して Entry を奪い、同じ Task を 2 つの Worker で始めさせられます（同様に過去の時刻で待ち行列の先頭へ割り込めます）。Database の時計なら、全ての Process が 1 つの基準を共有します。
各 Method（`enqueue`、`claim_next`、`heartbeat`、`release`、`complete`、`cancel`）の `now` は省略でき、省略（`None`）が Database の時計です。**本番のコードは `now` を渡してはいけません。** 明示の `now`（Timezone 付きの `datetime`）は Test のための継ぎ目で、`TaskQueue(database, allow_explicit_now=True)` で作った Queue だけが受け取ります。それ以外の Queue は `InvalidQueueingArgumentError("now")` で拒否するので、既定の Queue では呼び出し側が時刻を差し込めません（[Decision 0007](../../docs/decisions/0007-task-queue-budget-and-loop-policy.md) の 6）。継ぎ目は、既存の Test をそのまま使えるように、Constructor で時計を差し替える方式ではなく Method の引数で残しています。
限界: 基準は 1 つの PostgreSQL Server の時計です。Failover などで別の Server の時計へ切り替わる場合の時計のずれは扱いません（Lease は数十秒以上なので、通常の NTP の精度では問題になりません）。Runtime の Timer（`BudgetTracker`）も同じ Database の時計を使います（下の Budget の節。以前は Process の時計でしたが、Host ごとに時計が食い違うと Runtime を少なく数えられるため、Database の時計へ一本化しました）。

**行ロック。** `claim_next` は 1 Transaction で、Claim できる行のうち先頭を `SELECT ... ORDER BY ... LIMIT 1 FOR UPDATE SKIP LOCKED` で選び、その行を更新します。他の Transaction がロック中の行は待たずに飛ばします。
したがって、競合する複数の Claimer が同じ Entry を得ることはなく、互いを待たず、Claim できる Entry がロック中の 1 つだけなら `None` がすぐに返ります。
`heartbeat`、`release`、`complete` は、1 Transaction の中で、まず行を `SELECT ... FOR UPDATE` で Lock し、次に Worker id・Claim の世代・Lease の期限を条件にした `UPDATE ... RETURNING` を行います（時計の項のとおり、期限は Lock を得た後の時刻で判定します）。`cancel` は条件付きの単一の `UPDATE` で、行の Lock を先には取りません。
Test は別々の DB 接続を持つ複数の Claimer で、同じ Entry を 2 人が得ないこと、ロック中の行を待たないことを実 PostgreSQL で確認します。

**Index（履歴が増えても Claim が遅くならないこと）。** 完了・取消済みの Entry は消さずに残すため、`queue_entries` は使うほど、有効な（`queued` / `claimed` の）Entry より終わった Entry のほうがはるかに多くなります。Claim の Index `ix_queue_entries_claim_order`（`priority_rank`、`enqueued_at`、`id`）と、Task の有効な Entry を探す `uq_queue_entries_one_active_per_task`（`task_id`）は、どちらも `WHERE status IN ('queued', 'claimed')` の Partial Index です。PostgreSQL は、文の条件が Index の条件を含意すると証明できるときだけ Partial Index を使えます。Driver は何度も実行する文を Prepared Statement にし、PostgreSQL は値を使わない汎用の Plan（`plan_cache_mode = force_generic_plan` と同じもの）を Cache できます。その Plan では、`status` が Bind Parameter だと証明できず、Index が使われません。`claim_next` は終わった Entry も含めて全行を読んで並べ替え、`cancel` は全行を読むため、Claim の遅さが過去の全 Entry の数に比例して増えます。そこで、`claim_next` の `status = 'queued'` / `status = 'claimed'` と `cancel` の `status IN ('queued', 'claimed')` は、`bindparam(..., literal_execute=True)` で **SQL の文面に書き込みます**（`task_queue.py` の `_inlined()` / `_active()`。PAW-032 の `_tool_call_started()` と同じ手法です）。値は 2 つの定数なので、Plan を使い回せなくなる代償はありません。Test（`IndexPlanTest`）は、終わった Entry が 2 万件ある Queue に有効な Entry が 4 つある状態で、値ごとに立てる Plan（`force_custom_plan`）でも汎用の Plan（`force_generic_plan`）でも、Claim の選択は Seq Scan と並べ替えがなく `ix_queue_entries_claim_order` だけを使うこと、`cancel` は Seq Scan がなく `uq_queue_entries_one_active_per_task` だけを使うこと、Entry の `id` で行う Lock と更新は主キーを使うことを確認します。

### Budget（Preset と 6 種類の上限）

Task ごとの実行予算で、User / Admin 単位の Quota（別の仕組み）とは独立です。種類は次の 6 つです（`BudgetKind`）。
Runtime と GPU 時間の単位は整数の秒、他は個数です。記録する量は 0 以上の `int` だけで、`float`（有限でも）、`bool`、文字列、`None` は拒否します。

| 種類 | 記録 |
| --- | --- |
| `runtime_seconds`（max runtime） | `start_runtime` / `stop_runtime(task_id, generation)` が、Database の時計で測る（`record` は不可） |
| `steps`、`retries`、`tool_calls`、`tokens`、`gpu_seconds` | `record(task_id, kind, amount)` |

- **Preset。** Standard / Long / Unlimited は `domain.PRESET_LIMITS` のデータです。`set_preset` が Task の 6 行を作り（または上限だけを更新し、消費は保ちます）、上限をその Task の行へ写します。
  Preset を設定していない Task は `BudgetNotConfiguredError` になり、無制限とは**みなしません**。
- **超過の定義。** `消費量 + planned > 上限` のとき、その種類が `EXCEEDED` です。上限ちょうどまで使うのは超過ではなく（`max 50 steps` は 50 まで）、`check(task_id, planned={kind: 1})` で「もう 1 つ実行できるか」を調べられます。
  `EXCEEDED` の Verdict は、超過した種類をすべて、宣言順で返します。要件に警告の閾値はないため、`WARN` はありません。
- **原子性。** `record` は `UPDATE ... SET consumed = LEAST(consumed + :amount, 上限) ... RETURNING` の 1 文で、複数 Process が同時に記録しても増分は失われません。消費量は `10^15` で飽和し、Overflow しません。
- **Runtime。** `start_runtime` が `running_since` を保存し、`stop_runtime` が経過した整数秒（切り捨て、負にはならない）を加えて消します。実行中は `usage` / `check` が経過分を足して返しますが、書き込みません。同時の `stop_runtime` が時間を二重に加えることはありません。
- **Runtime の時計は Database の時計だけです。** Timer の端点（`running_since`、`settled_through`）と経過時間は全て、SQL の文の中で読む PostgreSQL の `clock_timestamp()` です。文は `WITH clock AS (SELECT clock_timestamp() AS ts)` で始まり、1 つの文の中では時計を 1 回だけ読みます（Queue と同じ方式。`now()` は使いません）。実行中の `usage` / `check` / `set_preset` も、行を読んだ文が読んだ Database の時刻で経過分を数えます。Process の時計は一切読みません。Host ごとに時計が食い違っていても（Process の時計だと、時計が進んだ Host が書いた `settled_through` が、遅れた Host の置き換えの Session の `running_since` を未来へ押し出し、その Session の Runtime が 0 と数えられて上限を回避できました）、全ての Worker が 1 つの基準を共有するので、数える秒は変わりません（[Decision 0007](../../docs/decisions/0007-task-queue-budget-and-loop-policy.md) の 10）。
  本番のコードは `BudgetTracker(database)` だけで作ります。`clock`（Timezone 付きの `datetime` を返す引数なしの callable）は Test のための継ぎ目で、`BudgetTracker(database, clock=..., allow_explicit_clock=True)` でだけ受け取ります（`TaskQueue` の `allow_explicit_now=True` と同じ考え方）。それ以外は `InvalidQueueingArgumentError("clock")` で拒否するので、既定の Tracker には Process の時刻を差し込めません。継ぎ目の時計は Database の時計の代わりに、その値を文へ束縛します（Test が時間を動かすため）。1 つの Database に、Test の時計の Tracker と Database の時計の Tracker を混在させてはいけません。
- **Timer の時刻の順序。** Database の時計は、行の Lock を待つ**前**に読まれ（Queue の時計の項と同じ）、壁時計は後ろへ戻り得ます（NTP の Step、別の Server への Failover）。そこで `stop_runtime` は、精算した Cutoff を `settled_through`（`greatest(now, running_since)`）に記録し、`start_runtime` は Timer を `greatest(now, settled_through)` から始めます（どちらも行を Lock する文の中で計算します。`greatest` は `NULL` を無視するので、まだ停止していないときは `now` です）。競合する `stop_runtime` が、より後の時刻（たとえば t=150）まで精算して `running_since` を消した**後**に、それより前の時刻（t=100）を読んで待っていた `start_runtime` の文が実行されても、新しい Timer は t=150 から始まり、100〜150 が二重に数えられることはありません。Cutoff は前にしか進まないので（`running_since` は常に `settled_through` 以上で、CHECK でも守ります）、数える区間は重なりません。`stop_runtime` は Cutoff より前の時刻を読んでも 0 秒を加えるだけです。Cutoff は、Database の時計への一本化で Host 間の食い違いには不要になりましたが、上の 2 つの競合（読んでから待つ間、時計の後戻り）を防ぐので残しています（Column・CHECK・Grant はすでにあり、費用はありません）。限界: 文は時計を Lock の待ちの前に読むので、`stop_runtime` が別の Transaction の Lock を待った時間（通常はミリ秒）は数えません（`stop_runtime` は切り捨てで整数秒に丸めます）。
- **Runtime の Session（Fencing）。** `start_runtime` は呼ぶたびに新しい Runtime の Session を始め、その世代（`runtime_generation`、1 以上の `int`。増える一方で、Timer が止まっても戻りません）を返します。Worker は、その世代を `stop_runtime(task_id, generation)` の**必須の引数**として渡します。現在の世代と違う `stop_runtime` は、何も変更せず `StaleRuntimeSessionError`（`code` は `runtime_session_stale`）にします。Lease が切れて Entry が Reclaim された古い Worker や、Restart 前の実行が遅れて `stop_runtime` を呼んでも、新しい Session の `running_since` と累積の Runtime には触れず、`check` は新しい Worker の Runtime を数え続けます（上限を回避できません）。Session の世代は Queue の `claim_count` と同じ考え方ですが、`claim_count` は Entry ごとに 1 から数え直す（Restart の新しい Entry と衝突する）ため、専用の Counter にしています。
  すでに Timer が動いているときの `start_runtime` は、Session を**引き継ぎ**ます（`running_since` と `settled_through` はそのままで、それまでの時間は失われず二重にも数えられません。前の世代は古くなります）。同じ世代の `stop_runtime` を 2 回呼ぶと、2 回目は何も変えず現在の Runtime を返します。渡す世代の型・範囲の誤り（`bool`、0 以下、文字列など）は `InvalidQueueingArgumentError("generation")` です。Preset の変更（`set_preset`）は世代を変えません（[Decision 0007](../../docs/decisions/0007-task-queue-budget-and-loop-policy.md) の 10）。
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
  そのため「Timeout after 30s」と「timeout after 45s」は同じ失敗です。**Message の原文、Error class、Step 名は保存せず**、`loop_failure_signatures` は Signature、方法の番号（`approach`）、Task の試行（`attempt`）だけを持ちます。
- **入力の検証。** `error_class`、`step`、`message` は、Signature を計算する前に（Database に触れる前に）検証します。`error_class` と `step` は空白だけ・制御文字（NUL を含む）・長さの超過を、`message` は `str` でないものを、`InvalidQueueingArgumentError` で拒否します。さらに、3 つとも**Surrogate 文字（U+D800〜U+DFFF。JSON の `"\ud800"` などから生じ、UTF-8 にできない不正な Unicode）を含むと**、Hash の計算で `UnicodeEncodeError` が漏れる代わりに、同じ `InvalidQueueingArgumentError`（`parameter` は `error_class` / `step` / `message`）で拒否します。値はエラーに含めません。`message` は、先頭 2000 文字への切り詰めの前の全体を検査します（切り捨てられる部分の Surrogate も拒否するので、結果は切り捨ての位置に依存しません）。`message` の NUL は拒否しません（Hash にするだけで保存しないため、書き込みのエラーにならず、拒否すると繰り返される失敗を記録できなくなるため）。呼び出し側は、Surrogate を含み得る出力（`errors="surrogateescape"` で読んだ Process の出力など）を、渡す前に整形してください。
- **判定。** 直近 `window_size` 件（10）のうち、最後の失敗と Signature も `approach` も同じ件数（連続でなくてよい）を `repeats` とします。
  `repeats < repeat_threshold`（3）は `CONTINUE`。それ以上なら Loop で、`approach < max_alternatives`（1）なら `TRY_ALTERNATIVE`、そうでなければ `ESCALATE` です。
  Orchestrator は `TRY_ALTERNATIVE` の後に `approach` を 1 増やして次の失敗を記録します（0 が元の方法、1 が最初の代替）。新しい `approach` は、その中で改めて 3 回繰り返すまで `ESCALATE` になりません。
- 判定は Deterministic で、同じ履歴には常に同じ結果を返します（`evaluate_loop` は純粋関数）。`LoopDetector.record_failure` は履歴へ追記し、1 Task あたり `window_size` 件を超えた古い行を消します。同じ Task への同時の記録は直列化されます。
  Restart で新しい試行を始めたら、**Restart の Command が Commit された後に** `clear_previous_attempts(task_id)` で古い試行の行を掃除してください。これは Task の**現在の試行より前**の試行の行だけを削除し、削除した件数を返します（未知の Task と、前の試行がない Task は 0）。Restart が Commit された後、この掃除が走る前に新しい試行が記録した失敗は、現在の試行のものなので**削除されません**（Task 全体を消す `clear` は、その失敗も消してしまうため、なくしました）。現在の試行は同じ文で `tasks.attempt` から読み、`tasks.attempt` は増えるだけなので、途中で Restart が Commit されても、削除が減るだけです。同じ Task 単位の Advisory Lock（`record_failure` と同じもの）を Transaction の間ずっと取るため、書き込み中の `record_failure` とは直列になります。掃除は正しさに必要ではなく（下の読み取りの規則のため、古い行は読まれません）、Table を小さく保つためのものです。
- **試行（Attempt）による Fencing。** `record_failure(task_id, *, attempt, error_class, step, message, approach=0)` の `attempt` は**必須**で、報告する Worker が開始された試行の番号（PAW-032 の `TaskEvent.attempt`。Step・Log・Tool の書き込みが持つものと同じ）です。`tasks.attempt`（Restart が 1 増やす既存の Counter）と違う試行の報告は `StaleAttemptError` で拒否し、何も書きません（未知の Task は先に `TaskNotFoundError`）。そのため、Restart の後に古い Worker が遅れて失敗を報告しても、新しい試行の履歴には入りません（`clear_previous_attempts` の前でも後でも）。
  確認と書き込みの間に Restart が割り込まないよう、`record_failure` は Task の行を `SELECT ... FOR SHARE` で Lock し、Transaction の終わりまで持ちます。Restart（PAW-032 の Command は `FOR NO KEY UPDATE` を取る）は、進行中の記録の Commit を待ってから実行されます。したがって、Commit された失敗は、Commit の時点で現在だった試行のものです。`FOR SHARE` は他の `record_failure`（Advisory Lock が直列化する）や外部キーの確認（`FOR KEY SHARE`）とは競合しません。
  失敗の行は、報告された試行（`attempt`）を持ち、**Task の現在の試行の行だけ**が判定に使われます（`history`、`assess`、`record_failure` が返す判定）。そのため、Restart が Commit された瞬間から、新しい試行は空の履歴で始まります（`clear_previous_attempts` の前でも同じで、古い行と一緒に数えて、新しい試行の最初の失敗が Loop と判定されることはありません）。Window の上限（`window_size`）は Task 全体の行数にかかり、現在の試行の行は古い試行の行より常に新しいため、古い行から先に消えます。
  試行の番号は既存の PAW-032 の Counter をそのまま使います（[Decision 0007](../../docs/decisions/0007-task-queue-budget-and-loop-policy.md) の 8）。Retry（同じ試行のやり直し）は試行を変えず、履歴も消しません。Migration（`0033`）の列は `attempt`（1 以上の `INTEGER`、必須）です。
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
- **Loop 検知の範囲。** 検知するのは**失敗の繰り返しだけ**です（`record_failure` が受け取る `error_class` / `step` / `message`）。要件は「同じ Tool Call」「同種の修正」の繰り返しも対象としますが、成功した同じ Tool Call、何も変えない Tool Call、同種の修正の繰り返しは Window に入らず、`TRY_ALTERNATIVE` / `ESCALATE` になりません。今はそれらを `tool_calls` / `steps` / `runtime_seconds` の Budget の上限が止めるだけで、`Unlimited` の Task では止まりません。PAW-033 の受け入れ条件は「repeated failure loop detection」だけで、担当の Issue は要件にも Backlog にもなく**未定**です（入力を持つのは Tool Broker（PAW-031）、Worktree（PAW-035）、判定する Orchestrator（PAW-034）。[Decision 0007](../../docs/decisions/0007-task-queue-budget-and-loop-policy.md) の 3。人間が決めてください）。
- **Unlimited の上限。** 要件は Unlimited に Runtime などの数値の上限を定めていないため、完全に無制限です。暴走を止めるのは Loop 検知と、Operator の Stop Now、Critical safety です。別に上限を設けるかは人間が決めてください。
- **飢餓。** Aging がないため、`HIGH` / `NORMAL` が続くと `LOW` が飢えます。要件が規則を定めたら追加します。
- **Budget 超過時の行動。** `retries` は `FAIL`、他は `WAIT_FOR_USER`、Escalation より Budget を優先する、という割り当ては私の選択です。警告の閾値が要件にないため `WARN` はありません。

制限。

- 量は 0 以上の整数だけです。GPU 時間は整数秒で、`float` は拒否します（呼び出し側が切り上げてください）。
- Queue は `tasks.state` を読まず、変更もしません。`claim_next` と PAW-032 の `start` を組み合わせるのは PAW-034 です。
- Lease の切れた Entry は、次の `claim_next` が自動で取り直します。実行中の Process を止める処理（`stop_now` など）は含みません。期限を過ぎた Worker の完了報告は拒否されます。
- 優先度の引き上げ、Preset の変更、Queue の一覧は認可付きの操作で、Endpoint と一緒に追加します。
- `start_runtime` は、`BudgetTracker` が Queue を読まないため、呼んだ Worker が Lease を持つかを確認しません。Lease を失った古い Worker が `start_runtime` を呼ぶと、Session を引き継げてしまいます（時間は数え続けるので Budget は回避されませんが、新しい Worker の `stop_runtime` は `StaleRuntimeSessionError` になります）。Lease を持つ Worker だけが呼ぶ規則は、Orchestrator（PAW-034）の責務です。Tracker が Queue の Entry を確かめる案は、[Decision 0007](../../docs/decisions/0007-task-queue-budget-and-loop-policy.md) の 10 で承認を求めています。
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
Resource を作る関数は**認証済みの User の Request にだけ**呼びます。認証されていない Request は、Resource を作る前（Project や Repository の状態と ACL を Storage から読む前）に 401 で拒否し、Log の Resource の種類は `unknown` になります（匿名の Client が Database の処理を起こしたり、その停止に巻き込んだりできないようにするためです）。

| 状況 | HTTP | WebSocket |
| --- | --- | --- |
| 認証されていない | 401 `unauthorized` | Close Code 1008 |
| 認証済みだが権限がない（Role 不足、Project の非 Member、他人のデータ、不正な ID、Project の状態） | 403 `forbidden`。Body は固定で、どの規則で拒否したかは含めない | 1008 |
| Audit を書けない Capability で Audit が書けない | 503 `service_unavailable` | 1013 |

`/api/v1` のすべての Route は `require_capability` で守るか、`tests/test_authz_routes.py` の公開一覧に理由付きで載せる必要があります（載せ忘れると Test が失敗します）。守りの有無は Route の**操作（HTTP Method と Path の組、WebSocket は `WEBSOCKET` と Path の組）ごと**に調べます。同じ Path の `GET` を守っても `POST` を守ったことにはならず、公開一覧の `GET` は同じ Path の他の Method を公開しません。

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

Tool Broker の Capability（read / write / execute / network / credential-use / destructive）と Approval は PAW-031 以降で、ここで決めた権限をさらに狭める方向にだけ働きます。

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
  Write は接続 Pool を使わず、その 1 文専用の接続（自動 Commit）で行い、期限（と呼び出し側の Cancel、`dispose()`）で**接続の Socket を閉じて**止めます。接続を受け付けたまま応答しない PostgreSQL に対して、Driver がサーバーへ Cancel を依頼して待つ（約 10 秒、または古い libpq では Thread の完了待ち）のを避けるためです（`Database.execute_abortable`、起動時の診断と同じ仕組み）。同時に開く接続は Pool の大きさまでで、空きがなければ待ちますが、空き待ちと実行は**1 つの期限を共有**します（空き待ちに使った分だけ実行に使える時間が減り、1 回の呼び出しが期限を超えることはありません）。打ち切られた Write は Commit されたかどうか分かりません（許可は拒否に変わり、Audit 行が残っていることがあります）。
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
- `tests/test_authz_routes.py` が調べるのは `/api/v1` の Route だけで、FastAPI の内部（`effective_route_contexts`）に依存します。Method の一覧を持たない Route（`Mount` など）は Method `*` の 1 操作として報告し、見逃しません。
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
- **1 回限り**。消費は `UPDATE ... WHERE used_at IS NULL AND revoked_at IS NULL AND expires_at > <その文が行を判定する瞬間>` の 1 文で行うため、同時に 2 回使っても片方だけが成功します。
  1 人の User について、未使用で無効になっていない Token は最大 1 つです（Partial Unique Index）。新しく発行すると、古い Token は先に無効にされます（`owner-recover`）。
- **有効期限**は `PAW_SETUP_TOKEN_TTL_SECONDS`（既定 1800、60〜**14400**）。期限ちょうどの時刻は無効です。
  期限は**消費の瞬間**に判定します（Decision 0005 の 10。Proposed）。`redeem` は開始時に 1 度判定し（期限切れの Token は Owner の行 Lock を待ちません）、Owner の `users` 行を `FOR UPDATE` で Lock した**後**、消費する 1 文の中で判定し直します。別の Transaction が Owner の行を Lock している間に待たされても、待っている間に期限が切れた Token は使用済みになりません（拒否は同じ `SetupTokenRejectedError`、Audit は deny `token_expired`）。
  その文は、この Process の Clock（Lock を得た後に読み直した値。Test が動かす時計）と DB の `clock_timestamp()`（`now()` は Transaction の開始時刻なので使いません）の**新しいほう**を「現在」とします。どちらか一方が期限を過ぎたと言えば期限切れで、Clock がずれていても Token の寿命が**短くなる**側にしか働きません（Test は、止まった Process Clock でも DB の Clock だけで拒否されることを確かめます）。`used_at` は Process の Clock で、Lock を得た後の時刻を記録します。
  **発行する側も、寿命は保存の瞬間から数えます**（Decision 0005 の 10）。`owner-recover` と `owner-setup --replace-non-live-owner` は Owner の行を Lock する間、別の Transaction に待たされることがあります。Process の Clock は Lock を得た**後**に読み（旧 Token の `revoked_at`、旧 Owner の降格、新 User の時刻）、Token の `created_at` / `expires_at` と表示する `IssuedToken.expires_at` は、待ちうる文（旧 Token の無効化、新 Owner の INSERT）がすべて終わった後にもう一度読んだ値から決めます。待ちが TTL を超えても、表示された Token には TTL の全体が残ります（Test は、Owner の行・旧 Token の行・競合する Owner の INSERT を別の Transaction が持つ間に発行し、Clock を TTL 以上進めてから解放します。`pg_stat_activity` で待ちを確認します）。最初の `owner-setup` の新 User の `created_at` だけは、競合する INSERT を待つ前の時刻のままです（記録用で、Token の寿命には関わりません）。
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

## Memory / Conversation Schema

[PAW-040](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/34)（Revision `0040`）で実装した Schema です。
設計は [Memory Architecture](../../docs/MEMORY_ARCHITECTURE.md) と [要件](../../REQUIREMENTS.md) の Memory の節に従います。
Repository / Service は含みません。

| 層 | Table | 内容 |
| --- | --- | --- |
| Raw Conversation | `conversations`、`messages` | 発言、Tool 結果、Agent 結果、Task の経緯。無期限に保持し、LLM Context へ全文は入れない |
| Session state | `session_states` | Conversation ごとの要約と作業状態（1 Conversation に 1 行） |
| Long-term Memory | `memories`、`memory_versions`、`memory_metadata_changes`、`memory_relations`、`memory_sources`、`embedding_models`、`memory_embeddings` | 確定した知識。Version、Pin / Importance の変更履歴、関係、出典、Embedding |

3 つの層は別の Table で、Foreign Key でつながるのは出典（`memory_sources`）だけです。
Conversation を消しても Memory と他の出典は残り（`ON DELETE SET NULL`）、Session state と Message は一緒に消えます。
出典の `conversation_id` と `message_id` は組で検証し（複合 Foreign Key）、Message がその Conversation のものでなければ DB が拒否します。
Message だけを消すと `message_id` だけが NULL になり、Conversation の出典は残ります。Message を指す出典は Conversation も指す必要があります。Conversation が NULL の組は複合 Foreign Key の検証（`MATCH SIMPLE`）を通り抜け、Conversation 単位の検索（削除の流れ）から漏れるため、
遅延（`DEFERRABLE INITIALLY DEFERRED`）Constraint Trigger `tr_memory_sources_message_requires_conversation` が拒否します。違反は INSERT ではなく COMMIT（または `SET CONSTRAINTS ... IMMEDIATE`）で分かります。
CHECK 制約にしないのは、Conversation の削除で Foreign Key の `SET NULL` が `conversation_id` と `message_id` を 1 列ずつ NULL にするため、途中で（Conversation が NULL、Message あり）の状態を通り、削除が失敗するからです（順序は Object ID 由来で保証されません）。Trigger は行の最終の状態を読み直して判定します。
読み直しは、Trigger を持つ Table の Schema と名前（`TG_TABLE_SCHEMA`、`TG_TABLE_NAME`）で修飾して行い、関数は `search_path` を `pg_catalog, pg_temp` に固定します。
Application の Role も既定の `TEMP` 権限で一時 Table を作れるため、修飾がないと、空の一時 Table `memory_sources` が読み直しの答えを空にし、不正な行が COMMIT を通ってしまいます（`ShadowedRelationTest`）。
種別 `conversation` の出典が Conversation も Message も指さない行（`conversation_id`、`message_id`、`source_ref` がすべて NULL）は、何の出典かを示さないのに、CHECK 制約を通ります。
Conversation を削除した後に `ON DELETE SET NULL` が残す状態（正当）と区別できないため、CHECK では拒否できません。そこで INSERT だけを見る `BEFORE INSERT` の Trigger `tr_memory_sources_conversation_source_identified` が、新しい行を INSERT の時点で拒否します。
UPDATE には働かないので、Conversation の削除（Foreign Key の `SET NULL`）と、削除の流れが `source_deleted_at` を記録する UPDATE は通ります。既に保存された行は検査しません。

**Scope と ACL。** 各 Version が `scope`（`user` / `project` / `project_group` / `repo` / `shared`）を持ち、Scope に対応する ID を 1 つだけ持ちます
（`owner_user_id` / `project_id` / `project_group_id` / `repo_id`、`shared` は無し）。CHECK 制約が組み合わせを強制します。
`project_group` は、要件の Inferred Preference の例（自由入力「開発系の Project だけ適用」を `scope: project_group` へ構造化）を保存するための Scope です。
要件は Project Group の実体、Member、権限を定義していません。そのため Schema は Group の ID（素の UUID）だけを持ち、
`Principal.project_group_ids`（呼び出し側が決めた、読める Group の ID）に含まれる場合だけ読めます。
Project が Group に属していても、それだけでは Group の Memory は読めません（既定は拒否）。
権限の判定は SQL で行います。`paw_backend.memory.acl` の `readable_memory_versions(principal)` を、
`memory_versions`（と、それを Join する `memory_embeddings`、`memory_sources`、`memory_relations`、`memory_metadata_changes`）を読む全ての Query に付けます。
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

**Pin / Importance の変更履歴。** 要件（Manual Memory Editing）は、Pin と Importance を「即反映可能だが、変更履歴は残す」低リスクの Metadata としています。
そのため `memory_versions.pinned` / `importance` は Version の中で更新でき（新しい Version は作りません。本文の編集の楽観ロックと衝突させないためです）、
変更は `memory_metadata_changes` に追記されます（変更前後の `pinned` と `importance`、`actor_type` / `actor_user_id`、時刻）。
記録は `memory_versions` の Trigger（`tr_memory_versions_record_metadata_change`）が行うので、書き込む側が省略することはできません（Table の Owner でない Application は Trigger を止められません）。
Trigger の関数は書き込む側の Session で動き、Application の Role も PostgreSQL 既定の `TEMP` 権限を持つため、同名の一時 Table（や一時 Type `uuid`）で履歴の書き込み先をすり替えられないようにしてあります。
関数は `search_path` を `pg_catalog, pg_temp`（`pg_temp` を明示して最後に置く）へ固定し、履歴 Table は Trigger を持つ Table と同じ Schema（`TG_TABLE_SCHEMA`）で修飾して書き込みます（動的 SQL）。
Test は Application の Role で、一時 Table を先に作ってから Pin を変更し、実際の履歴に行が残ることを確認します（`tests/test_memory_grants.py` の `ShadowedRelationTest`）。
値が変わらない UPDATE は記録しません。`status` と `stale_since` の更新も対象外です。
Trigger は誰の操作か知らないため、書き込む側が同じ Transaction の UPDATE の前に `metadata_change_actor(actor_type, actor_user_id)`（`paw_backend.memory.metadata`）で Actor を示します。
Actor を示さない変更は `memory_metadata_changes.actor_type` の NOT NULL で失敗し、UPDATE も取り消されます。設定は Transaction 内だけ有効（`set_config(..., true)`）で、接続 Pool を通じて次の Request へ残りません。
`memory_metadata_changes` は追記のみ（Application に UPDATE / DELETE は与えません）で、Version と一緒に Cascade で消えます。
限界: Actor の ID は Backend が主張する値で、DB は確認しません（User の Table が無いため。`actor_user_id` と同じ扱い）。Application は INSERT を持つので、変更を伴わない行を追加することはできます（既存の行は書き換えられません）。
`pinned` / `importance` は Trigger が使う列なので、型を変える Migration は Trigger を作り直す必要があります。
Permanent / Revalidate など鮮度の設定は Version の不変の列なので、変更は新しい Version になり、その履歴が変更履歴です。

**User / Project / Repo の ID は Foreign Key なし。** Project と Repo の Table はまだありません（PAW-026 / 027）。`users`（PAW-021）はありますが、Migration の順序が統合後に決まるため、この Schema からの外部キーは付けていません。
`owner_user_id`、`project_id`、`project_group_id`、`repo_id`、`actor_user_id` は素の UUID Column で、DB は存在を確認しません。
Backend は検証した ID だけを書いてください。Table ができた後の Migration で Foreign Key を追加できます。
Task、Repo 解析、Project Decision の出典も、Table がないため `memory_sources.source_ref` の不透明な文字列です。
種別 `conversation` 以外の出典は、`source_ref` が NULL でなく、1 文字以上であることを CHECK 制約（`ck_memory_sources_other_sources_have_reference`）が求めます。
空文字は NULL ではありませんが、Task、Repo 解析、確認、Decision のどれも指さず、出典として辿れないため拒否します（`char_length(NULL)` は NULL で CHECK を通り抜けるため、NULL も明示して拒否します）。
検査するのは長さだけです。空白だけの文字列は DB が受け入れます。`title`、`content`、`memory_type`、Embedding Model の `id` など、この Schema の他の文字列の Column と同じく、DB は文字列の意味を知らず、
空白の除去や正規化は Backend が行うためです。種別 `conversation` は従来どおり `source_ref` を持てません（`conversation_has_no_opaque_reference`）。Conversation の削除が残す、参照がすべて NULL の状態（正当）はこの CHECK の対象外です。

**Application の Role の権限。** Migration は `PAW_APP_DATABASE_ROLE` の Role に、Table ごとに必要最小限を与えます（[上の規則](#migration-は-application-の-role-に権限を与えるcontributor-向けの規則)）。
未設定のときは何も与えません。TRUNCATE、ALTER、DROP、GRANT は誰にも与えません。

| Table | 与える権限 | 理由 |
| --- | --- | --- |
| `conversations` | SELECT、INSERT、DELETE、UPDATE（`title`、`updated_at` のみ） | 会話の削除は製品の機能。所有者 `owner_user_id`（ACL の境界）と Project / Repo は変更不可 |
| `messages` | SELECT、INSERT | Raw Conversation は追記のみ。履歴を書き換えない。会話ごとの削除は下記の Cascade |
| `session_states` | SELECT、INSERT、UPDATE（`summary`、`state`、`summarized_through_sequence`、`updated_at`） | 要約と状態は会話の進行で更新する。削除は会話と一緒（Cascade） |
| `memories` | SELECT、INSERT、DELETE | 更新する列は無い。DELETE は Memory 全体の削除（会話と関連 Memory の削除、Shared Memory の Admin 削除、User 削除時の Private Memory の消去）で、Version は Cascade で消える |
| `memory_versions` | SELECT、INSERT、UPDATE（`status`、`stale_since`、`pinned`、`importance` のみ） | Version は書き換えない（編集は新しい Version）。本文、Scope と ACL の列、`confirmation_state`、鮮度の設定は変更不可。DELETE は与えず履歴を残す。`pinned` / `importance` の UPDATE は Trigger が `memory_metadata_changes` へ記録する |
| `memory_metadata_changes` | SELECT、INSERT | Pin / Importance の変更履歴は追記のみ。Trigger が Application の権限で INSERT するため INSERT が必要。UPDATE / DELETE は与えない（Version の削除の Cascade でだけ消える） |
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

Model と Migration の一致は Test が検証します（Alembic の autogenerate の差分が空であること、Model から作った Schema と Migration の Catalog（Trigger を含む）が同じであること）。Trigger は Alembic の比較の対象外なので、Model は DDL Event、Migration は同じ DDL の複製で作り、Trigger 関数の定義も Test が比較します。
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
RBAC（PAW-025）、Task Lifecycle（PAW-032）、Task Queue / Budget / Loop 検知（PAW-033）、Memory Schema（PAW-040）は、この Skeleton の上に実装済みです。
PAW-022（Login / Session / Password）と PAW-023（Passkey / Step-up）は Owner Setup の Token を受け取る側で、まだありません。
Memory の保存・整理・検索は PAW-041 以降で、Memory Schema の上に実装します。
受け入れ基準は [Implementation Backlog](../../docs/IMPLEMENTATION_BACKLOG.md)、
実装時に選択できる事項は [Requirements Freeze Review](../../docs/REQUIREMENTS_FREEZE_REVIEW.md) を参照してください。
