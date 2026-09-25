# Backend

Personal AI Workspace の Core Backend です。
[PAW-020](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/17) で、後続の Issue が載る最小の Application Skeleton を実装しました。
認証と User はまだ実装していません（PAW-021 以降）。
RBAC と Audit（PAW-025）、Task の Lifecycle と永続化（[PAW-032](#agent-task-lifecycle)、HTTP の Endpoint はまだありません）、Memory の PostgreSQL Schema（[PAW-040](#memory--conversation-schema)）を実装済みです。Memory の保存・整理・検索の処理は PAW-041 以降です。

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
├─ migrations/             # env.py と Revision（0001 は空の Baseline、0040 は Memory Schema）
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
Queue、Budget、Loop 検知（PAW-033）と DAG Orchestration（PAW-034）は含みません。
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
| `task_tool_invocations` | Step が呼んだ Tool の実行状態（下記）。ID、Tool 名、状態（`started` / `succeeded` / `failed` / `interrupted`）、開始・終了時刻だけを持つ。`started` の行だけの Partial Index（`step_id`）がある |
| `task_logs` | 試行ごとの Log（`debug` / `info` / `warning` / `error`） |
| `task_events` | Append-only の履歴。全遷移について、Command、遷移前後の状態、`wait_reason`、Actor（`user` / `system` / `policy` と User の UUID）、理由、その時点の Step 名、`task_version` |

- `project_id`、`created_by`、`actor_id` は UUID だけを持ち、外部キーはありません。users と projects の Table がまだ存在しないためです（Table が入るときに外部キーを追加します）。
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
- Worker の記録（Step、Tool、Log、試行状態）は、担当する試行を明示します。`begin_step(task_id, name, attempt=...)` は `StepInfo`（`id` と `attempt` を持つ）を返し、`finish_step` は `step_id` で Step を指定します。
  Restart で新しい試行が始まった後に旧試行の Worker が書き込むと `StaleAttemptError` になり、何も書き込まれません（新しい試行の Step、Log、worktree の状態は変わりません）。同じ試行の中でも、Step の ID を指定するため、Retry より前の Worker が後から新しい Step を閉じることはできません。
- **Tool の実行状態**: 要件の「Tool execution state」のうち、Backend が再接続後に再開または中断を判断するのに必要な最小の記録だけを持ちます。
  `begin_tool_invocation` / `finish_tool_invocation` が Tool の ID（Tool Broker が UUID を渡すこともできる）、Tool 名、状態、時刻を記録し、`restore` は現在の Step の Tool を `TaskSnapshot.tool_invocations`（開始が古い順）で返します。`started` のままの Tool は、後から何件開始・終了しても**すべて**返します（Backend が再開または中断を判断できなくなる取りこぼしを避けるため）。終了済みの Tool だけは直近 100 件に絞ります。返す件数が呼び出し側の操作で際限なく増えないよう、1 つの Step で同時に `started` にできる Tool は 1000 件（`MAX_ACTIVE_TOOL_INVOCATIONS`。**暫定の値で、人間の確認待ちです**。下の「人間の判断が必要な点」）までで、1001 件目の `begin_tool_invocation` は `TaskStepError` になります（どれかが終了すると、また開始できます）。
  `started` の Tool を尋ねる 3 つの問い合わせ（`begin_tool_invocation` の同時数の確認、`restore` が返す `started` の Tool、Step の終了時に行う `interrupted` への更新）は、Step の終了済みの Tool の履歴全体を読みません。`status = 'started'` の行だけの Partial Index `ix_task_tool_invocations_started`（`step_id`）を使うためです。履歴が長い Step でも、Tool を開始するたびの作業量が、その Step の Tool の総数ではなく同時に `started` の数だけで決まります。`started` は Bind Parameter ではなく SQL の文面へ書きます（`_tool_call_started()`）。Parameter にすると、Driver が何度も実行する文を Prepare して PostgreSQL が Plan を使い回す場合に、Partial Index の条件を満たすと判断できず、Index を使えなくなるためです（Test は Plan を使い回す設定でも Index を使うことを確認します）。
  **引数と出力は保存しません。** 権限判定、承認、引数と結果の扱いは Tool Broker（PAW-031）の責務です。Step が終わる（Stop Now / Fail / Restart / `finish_step`）と、`started` のままの Tool は `interrupted` になります。
- `TaskService.restore(task_id)` は DB だけから Snapshot（状態、current step、直近の Log、worktree / review / PR の状態、直近の Event）を作ります。1 つの Repeatable Read Transaction で読むため、同じ時点の値です。
  状態は Process のメモリに持たないので、Client が切断しても、Backend が再起動しても、別の Process が同じ値を返します。
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
- Migration `0025`、`0032`、`0040` は今は同じ `down_revision="0001"` を持ちます。統合時に 1 本の鎖へつなぎ直します。

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

**User / Project / Repo の ID は Foreign Key なし。** User、Project、Repo の Table はまだありません（PAW-021 / 026 / 027）。
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

[PAW-021](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/18)（Owner Setup）、PAW-022（Login / Session）、
RBAC（PAW-025）、Task Lifecycle（PAW-032）、Memory Schema（PAW-040）は、この Skeleton の上に実装済みです。
Memory の保存・整理・検索は PAW-041 以降で、Memory Schema の上に実装します。
受け入れ基準は [Implementation Backlog](../../docs/IMPLEMENTATION_BACKLOG.md)、
実装時に選択できる事項は [Requirements Freeze Review](../../docs/REQUIREMENTS_FREEZE_REVIEW.md) を参照してください。
