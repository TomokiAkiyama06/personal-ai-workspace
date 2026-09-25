# Backend

Personal AI Workspace の Core Backend です。
[PAW-020](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/17) で、後続の Issue が載る最小の Application Skeleton を実装しました。
Login と Session はまだ実装していません（PAW-022 以降）。
RBAC と Audit（PAW-025）、Task の Lifecycle と永続化（[PAW-032](#agent-task-lifecycle)、HTTP の Endpoint はまだありません）、Task Queue・Budget・Loop 検知（[PAW-033](#task-queue--budget--loop-検知)）、Tool Broker と Capability Policy（[PAW-031](#tool-broker--capability-policy)、HTTP の Endpoint はまだありません）、Memory の PostgreSQL Schema（[PAW-040](#memory--conversation-schema)）、
最小の `users` Table と Owner の初期設定・復旧のコマンド（[PAW-021](#owner-の初期設定と復旧)）を実装済みです。Memory の保存・整理・検索の処理は PAW-041 以降です。
Shared Memory の管理（Owner / Admin の作成・編集・削除・復元、Candidate の承認、Agent の自動昇格の拒否、System Policy の優先。[PAW-046](#shared-memory-administration)、HTTP の Endpoint はまだありません）も実装済みです。
Research の一時保存（[PAW-050](#research-scratch-store)、24 時間 TTL、期限切れを消す Janitor つき、HTTP の Endpoint はまだありません）と、Research Provider の Adapter Interface（[PAW-051](#research-provider-adapter)、実際の Provider（Direct Web、Docs、GitHub、OpenCode）はまだありません）と、外部の検索へ送る Query の最小化と送信の Audit（[PAW-053](#research-privacy-filter)、Audit の永続化はまだありません）も実装済みです。
Claim と Source の対応・回答や Task からの追跡（[PAW-052](#evidence--claim-provenance)、HTTP の Endpoint はまだありません）も実装済みです。
Project の作成・招待制の Membership・Lifecycle（Active / Archived / Pending deletion / Deleted）は [PAW-026](#project-crud--membership--lifecycle) で実装済みです（Service のみ。HTTP の Endpoint と Session はまだありません）。
DAG Agent Orchestrator（Task を Dependency DAG へ分解し、独立した Node を並列に実行し、Node ごとに Retry / Escalate し、Sub-Agent が親の権限と予算を超えない。[PAW-034](#dag-agent-orchestrator)、Agent の Runtime は Protocol で実際の Runtime は別の Issue、HTTP の Endpoint はまだありません）と、削除待ちの Project の Task を周期的に止める Loop も実装済みです。

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
├─ migrations/             # env.py と Revision（0001 は空の Baseline、0021 は users / setup_tokens、0026 は Project、0031 は Tool Approval、0033 は Queue / Budget / Loop、0040 は Memory Schema、0046 は Shared Memory Candidate、0050 は Research Scratch、0052 は Evidence / Claim Provenance、0034 は DAG Orchestrator）
├─ paw_backend/
│  ├─ app.py               # create_app(settings)
│  ├─ config.py            # PAW_ 環境変数から読む Settings
│  ├─ server.py            # Uvicorn 起動（TLS 設定）
│  ├─ db.py                # 非同期 Engine / Session、Readiness 確認、中断できる接続（`fetch_abortable`、`run_abortable`）
│  ├─ events.py            # プロセス内 Event Bus と Heartbeat
│  ├─ errors.py            # 共通の Error Response
│  ├─ middleware.py        # Request ID、Host 検証、Security Header
│  ├─ security.py          # Host / Origin の判定
│  ├─ authz/               # Role・Capability・認可の判定と Audit Event（PAW-025）、子 Agent の Grant の導出（`delegation.py`、PAW-034）
│  ├─ identity/            # 最小の users、One-time Token。`redeemer.py` は Web 側、`operator.py`（Owner の作成・Token の発行）は cli だけが使う（PAW-021）
│  ├─ cli/                 # server-local の管理コマンド `python -m paw_backend.cli`（PAW-021）
│  ├─ orchestrator/        # DAG Agent Orchestrator: Plan、Scheduler、DAG の永続化と Fencing、Runtime の Protocol、Tool・Budget の Gateway、Project 削除の Sweep（PAW-034）
│  ├─ tasks/               # Agent Task の状態遷移と永続化（PAW-032）
│  │  └─ queueing/         # Task Queue、Budget、Loop 検知、Escalation の判断（PAW-033）
│  ├─ memory/              # Memory / Conversation の Model、ACL 条件、vector 型、Pin / Importance 変更の Actor（PAW-040）
│  │  └─ shared/           # Shared Memory の管理: Service、Candidate、Rule 関数、Policy の優先（PAW-046）
│  ├─ projects/            # Project、Membership（招待制）、Lifecycle（PAW-026）
│  ├─ research/providers/  # Research Provider の Adapter Interface と Broker（PAW-051）
│  ├─ research/privacy/    # Research の Privacy Filter: Query の最小化と外部送信の Audit（PAW-053）
│  ├─ research/scratch/    # Research Scratch Store: 24 時間 TTL の一時保存と、期限切れを消す Janitor（PAW-050）
│  ├─ research/provenance/ # Evidence / Claim Provenance: Claim と Source の対応、回答・Task からの追跡（PAW-052）
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
Database には pgvector が必要です（CI は `pgvector/pgvector:pg18` を使います）。承認の要求と使用（`Database.transact_abortable`。Tool Broker の経路）は `transaction_timeout` を使い、**PostgreSQL 18 以上（human 決定済み 2026-09-25）** が必要です。Migration の実行には `CREATE EXTENSION` の権限が要ります。

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
| `PAW_SCRATCH_PURGE_INTERVAL_SECONDS` | `3600` | 期限切れの Research Scratch Item を消す Janitor の間隔（秒）。`0` で Janitor を止める（期限切れの行が DB に残り続ける）。それ以外は 60〜86400。DB が未設定のときも起動しない。[Janitor](#janitor期限切れの削除) |
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
同じ仕組みの中断できる接続が、ほかに 2 つあります。1 文を実行する `fetch_abortable` / `execute_abortable`（起動時の診断、Audit の書き込み、承認の取り消し）と、複数の文を 1 Transaction で実行する `run_abortable`（Research Scratch の Purge、[PAW-050](#janitor期限切れの削除)）です。どちらも Pool を使わず、呼び出し元の Cancel と `dispose()` で接続の Socket を閉じます。`run_abortable` は Session を渡す Callback を受け取り、正常に戻れば Commit、例外なら Rollback します。
どちらも同時に開く接続は `PAW_DATABASE_POOL_SIZE` までで、空きがなければ待ちますが、**この空き待ちも中断できる処理の一部**です。待っている呼び出しを Cancel すると待ちがその場で終わり（空きは取りも返しもしません）、`dispose()` は待っている呼び出しを全て `DatabaseDisposedError` で失敗させます（何も実行していません）。`dispose()` の実行中に始まった呼び出しも同じ例外で拒否します。実行中の呼び出しが空きを返した直後に待っていた呼び出しが動き出して、`dispose()` が止める対象を数えた後で Transaction を始めたり、破棄した後の Engine を作り直したりすることはありません（独立 Review の指摘）。`dispose()` が戻った後は、Engine を最初の使用時に作り直すので、再び使えます。空きの管理は `paw_backend/db.py` の小さな `_Slots`（`asyncio.BoundedSemaphore` と同じく先着順で、Cancel された待ちが空きを失わず、返しすぎを拒否する。待っている呼び出しを一度に失敗させられる）です。
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
Repo ごとの worktree / branch の作成と統合の処理は PAW-035、Write 範囲の強制は Tool Broker（PAW-031）の責務だからです。
Working Set の単位、Single-Repo との関係、Repo 追加の承認、Task の完了条件など、要件が決めていない判断があるため、
[Decision 0014](../../docs/decisions/0014-task-working-set-persistence.md)（Approved、2026-09-25 に Human が承認）で、PAW-032 に含めないことを決めました。
実装の担当は、PAW-027 の後・PAW-034 の前に立てる新しい Issue [#85](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/85) です。#85 が Working Set と Repo ごとの Git 状態を**保存する表**を持ち、PAW-035 は worktree・統合の**振る舞い**を持って、その結果を #85 の表へ書きます（PAW-035 には保存の表を含めません）。
上の未決の判断は、#85 の実装の前に別の Decision で決めます。特に Repo の役割と Write 範囲の対応は、保存より先に決めます。
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
- **Stop Now（immediate）**: 緊急停止です。Cancel と同じ `cancelled` になりますが、実行中の Step を即座に `interrupted` にし、停止理由と中断した Step を Task Log と履歴 Event へ残します。**`reason` は必須**です（[要件](../../REQUIREMENTS.md)の「停止理由と実行中だったstepをAudit / Task logへ残す」を満たすためで、他の Command では任意です）。`reason` がない（`None`）、空または空白だけ、文字列でない、長さの上限（500 文字）を超える、NUL / Surrogate を含む場合は、Task の状態を見る前に、何も書き込まずに `InvalidCommandArgumentError` で拒否します（エラー文に値は含めません）。受け付けた Stop Now は必ず、履歴 Event の `reason` と Task Log の行の両方に理由を残します。Log の行は `Stop Now: interrupted step '<Step 名>' (reason: <理由>)` です。Step が既に終わっていた場合（Worker が先に閉じた場合を含む）は何も中断していないため、Step 名を記録せず、Log は `Stop Now: no step was running (reason: <理由>)` とします（Fail も、自分が終わらせた Step だけを Event に記録します）。実行中に何かが動きうる状態（running / waiting / evaluating）だけが対象で、queued と paused には Cancel を使います。成果物は削除しません。
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

- `project_id`、`created_by`、`actor_id` は UUID だけを持ち、外部キーはありません。`projects`（PAW-026、Revision `0026`）と `users`（PAW-021、Revision `0021`）は、この Schema の Revision より後にできた Table です（外部キーを付ける後の Revision の手順は「[Project CRUD / Membership / Lifecycle](#project-crud--membership--lifecycle)」の「他の領域との関係」にあります）。
- **引数の検証（`TaskService` のすべての Public メソッド）**: すべての引数を、DB を使う前（Session を開く前）に検査し、型や値が誤っていれば固定文言の `InvalidCommandArgumentError` で拒否します（エラー文に値は含めません）。`AttributeError`、`TypeError`、SQLAlchemy の `StatementError`、`DBAPIError` として漏れることも、黙って成功することもありません（以前は、偽と評価される `invocation_id`（`""` や `0`）が黙って新しい ID に置き換わっていました）。
  - **ID**（`task_id`、`project_id`、`created_by`、`invocation_id`）は `uuid.UUID` です。**文字列の UUID は解釈せずに拒否します**（簡単さを優先した判断です。ID は Event や Snapshot が返す `uuid.UUID` をそのまま使います）。`invocation_id` は `None`（自動採番）か UUID です。`step_id` は 1 から 9223372036854775807（`BIGINT` の識別列で、1 から始まる）の `int` です。
  - **Enum**（`command`、`wait_reason`、Step / Tool 呼び出しの `status`、Log の `level`、Review の状態、Evaluator の結果、PR の状態）は、Member か、その直列化した値（ちょうど `str` 型。`"stop_now"` など）を受け付け、以降は Member に揃えて使います。`str` の派生、別の Enum の Member（同じ文字列でも）、bytes、数値、`None`（必須のもの）、未知の値は拒否します。`Actor(kind, id)` も同じ規則で、`kind` は Member かその値、`id` は `uuid.UUID`（User だけが持つ）です。`finish_step` / `finish_tool_invocation` の `status` は、終了を表す値だけを受け付けます。`plan_transition` も、`wait_reason` が `WaitReason` でも値でもなければ、状態に関係なく拒否します。
  - **整数**（`expected_version`、`log_limit`、`after_seq`、`limit`、PR の番号）は `int` で、`bool` ではありません。`expected_version` は `None` か 1 から 2147483647 です（Task の `version` は 1 から始まるため、0 は「どの Task の version でもない値」として拒否します）。`log_limit` は 0 から 1000、`after_seq` は 0 から 9223372036854775807、`limit` は 1 から 5000、PR の番号は 1 から 2147483647 です。
  - **オブジェクト**: `actor` は `Actor`、`run` は `TaskRun`、`worktree` / `review` / `pull_request` は `WorktreeState` / `ReviewState` / `PullRequestInfo`（省略時は `None`）で、そのフィールドも上の規則で検査します。PR は URL が必須です。branch / path / head commit / PR の URL は、`str` でない値、空・空白だけの値も拒否します（worktree のフィールドの `None` は「未設定」）。名前や `title`、`reason` も空・空白だけは拒否します。Log の `message` は、Worker が出力の空行をそのまま転送することがあるため空文字列を許します（`str` であることだけを要求します）。
  - **DB を読んでから判断する規則が 1 つだけあります**: `execute` の `wait_reason` が Command に合わない場合（Wait に無い、Wait 以外にある）は、不正な遷移（`IllegalTransitionError`）を先に報告する Domain の規則（`test_illegal_transition_is_reported_before_a_bad_argument`）のため、Task を読んだ後に同じ `InvalidCommandArgumentError` で拒否します。何も書き込みません。`wait_reason` の型と値そのものは、Transaction を開く前に検査します。
  - `tests/test_task_argument_validation.py` が、Method × 引数 × 誤った値（`None`、型違い、`int` の代わりの `bool`、bytes、空文字列、未知の Enum 値、`object()`、範囲外の数など）の表で確認します。各値で、型付きエラーであること、エラー文に値が含まれないこと、SQL が 1 文も送られず Session も開かれないこと、Task 関連の全 Table の行が変わらないことを検査します。すべての Public メソッドとすべての引数が表に載っていることも Test しています。
- **文字列入力の検証**: `TaskService` が受け取る文字列（`title`、`starting_commit`、`agent`、`model`、`reason`（Stop Now では必須）、Step 名、Tool 名、Log の `message`、`update_attempt` の branch / path / head commit / PR の URL）は、NUL（`\u0000`）と Surrogate 文字（不正な Unicode）を含むと `InvalidCommandArgumentError` で拒否します（エラー文に値は含めません）。PostgreSQL の text 列は NUL を保持できず、Surrogate は UTF-8 にできないため、そのままでは書き込み時に DB / 符号化のエラーが漏れます。Log の `message` は、長さの上限で切り捨てる前の全体を検査します。`update_attempt` は、branch（255 文字）、path（1024 文字）、head commit（64 文字）、PR の URL（2048 文字）を、Model の列の長さ（1 か所の定義）で検査して、超えると同じ `InvalidCommandArgumentError` で拒否します（文字数で数えます。空・空白だけの値と `str` でない値も拒否します）。PR の番号は 1 から 2147483647（`INTEGER` 列の最大値）の整数だけを受け付けます（`bool`、`float`、文字列は拒否します）。下限の 1 は、PR の番号が正であることに基づく私の判断で、要件が定める値ではありません。
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
- **Run（試行と Retry 回数）**: Worker の記録は、担当する **Run**（`TaskRun(attempt, retry_count)`）を明示します。`attempt` は Restart が 1 増やし、`retry_count` は Retry が 1 増やします。どちらも増えるだけで、失敗または中止した Task の再開は必ずどちらか一方を変えるため、2 つの Run が等しいのは同じ Run のときだけです。Retry は**同じ試行**を再実行する（試行番号は変わらない）ので、試行番号だけでは、失敗した Run の Worker と Retry が始めた Run の Worker を区別できません。`TaskRun`（`paw_backend.tasks.TaskRun`）は Tool Broker（PAW-031）が承認を結びつける Run と**同じクラス**です（Broker に別の `TaskRun` はありません。`paw_backend.tools.TaskRun` はこれの再 Export です）。
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
**HTTP の Endpoint も Orchestration もありません**（Orchestration は [PAW-034](#dag-agent-orchestrator) が実装しました）。Orchestrator が呼ぶ部品だけです。認可も行いません（Endpoint を作る側が権限を確認してください）。
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
**時計。** Queue が信頼する時計は **Database の時計だけ**です。`enqueued_at`、`claimed_at`、`lease_expires_at`、`finished_at` と「Lease が切れたか」の判定は、全て SQL の中で PostgreSQL の `clock_timestamp()`（評価した瞬間の壁時計）を使います。`now()` は使いません（方針は [Decision 0007](../../docs/decisions/0007-task-queue-budget-and-loop-policy.md) の 6 で、2026-09-25 に Human が承認しました）。`now()` は Transaction の開始時刻で固定されるため、行の Lock を待った後の判定に、待つ前の古い時刻が使われてしまいます。実際の動作は次のとおりです。
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

**Preset の値は暫定値です。** 要件は Preset の名前だけを定め、数値を定めていません（具体的な閾値は実装時の選択）。次の値は、実測に基づかない**暫定値**で、[Decision 0007](../../docs/decisions/0007-task-queue-budget-and-loop-policy.md)（Approved）で暫定値として承認されています（2026-09-25）。値は `domain.PRESET_LIMITS` のデータなので、後で変えても Migration は要りません（変えるときは、新しい Decision から `Supersedes` します）。

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
- **入力の検証。** `error_class`、`step`、`message` は、Signature を計算する前に（Database に触れる前に）検証します。`error_class` と `step` は空白だけ・制御文字（NUL を含む）・長さの超過を、`message` は `str` でないものを、`InvalidQueueingArgumentError` で拒否します。さらに、3 つとも**Surrogate 文字（U+D800〜U+DFFF。JSON の `"\ud800"` などから生じ、UTF-8 にできない不正な Unicode）を含むと**、Hash の計算で `UnicodeEncodeError` が漏れる代わりに、同じ `InvalidQueueingArgumentError`（`parameter` は `error_class` / `step` / `message`）で拒否します。値はエラーに含めません。`message` は、先頭 2000 文字への切り詰めの前の全体を検査します（切り捨てられる部分の Surrogate も拒否するので、結果は切り捨ての位置に依存しません）。`message` の NUL は拒否しません（Hash にするだけで保存しないため、書き込みのエラーにならず、拒否すると繰り返される失敗を記録できなくなるため）。呼び出し側は、Surrogate を含み得る出力（`errors="surrogateescape"` で読んだ Process の出力など）を、渡す前に整形してください。拒否のままとし、整形は Worker 側で行うことは [Decision 0007](../../docs/decisions/0007-task-queue-budget-and-loop-policy.md) の 9 で承認されています（2026-09-25。この規約は PAW-034 の受け入れ条件に追記しました）。
- **判定。** 直近 `window_size` 件（10）のうち、最後の失敗と Signature も `approach` も同じ件数（連続でなくてよい）を `repeats` とします。
  `repeats < repeat_threshold`（3）は `CONTINUE`。それ以上なら Loop で、`approach < max_alternatives`（1）なら `TRY_ALTERNATIVE`、そうでなければ `ESCALATE` です。
  Orchestrator は `TRY_ALTERNATIVE` の後に `approach` を 1 増やして次の失敗を記録します（0 が元の方法、1 が最初の代替）。新しい `approach` は、その中で改めて 3 回繰り返すまで `ESCALATE` になりません。
- 判定は Deterministic で、同じ履歴には常に同じ結果を返します（`evaluate_loop` は純粋関数）。`LoopDetector.record_failure` は履歴へ追記し、1 Task あたり `window_size` 件を超えた古い行を消します。同じ Task への同時の記録は直列化されます。
  Restart で新しい試行を始めたら、**Restart の Command が Commit された後に** `clear_previous_attempts(task_id)` で古い試行の行を掃除してください。これは Task の**現在の試行より前**の試行の行だけを削除し、削除した件数を返します（未知の Task と、前の試行がない Task は 0）。Restart が Commit された後、この掃除が走る前に新しい試行が記録した失敗は、現在の試行のものなので**削除されません**（Task 全体を消す `clear` は、その失敗も消してしまうため、なくしました）。現在の試行は同じ文で `tasks.attempt` から読み、`tasks.attempt` は増えるだけなので、途中で Restart が Commit されても、削除が減るだけです。同じ Task 単位の Advisory Lock（`record_failure` と同じもの）を Transaction の間ずっと取るため、書き込み中の `record_failure` とは直列になります。掃除は正しさに必要ではなく（下の読み取りの規則のため、古い行は読まれません）、Table を小さく保つためのものです。
- **試行（Attempt）による Fencing。** `record_failure(task_id, *, attempt, error_class, step, message, approach=0)` の `attempt` は**必須**で、報告する Worker が開始された試行の番号（PAW-032 の `TaskEvent.attempt`。Step・Log・Tool の書き込みが持つものと同じ）です。`tasks.attempt`（Restart が 1 増やす既存の Counter）と違う試行の報告は `StaleAttemptError` で拒否し、何も書きません（未知の Task は先に `TaskNotFoundError`）。そのため、Restart の後に古い Worker が遅れて失敗を報告しても、新しい試行の履歴には入りません（`clear_previous_attempts` の前でも後でも）。
  確認と書き込みの間に Restart が割り込まないよう、`record_failure` は Task の行を `SELECT ... FOR SHARE` で Lock し、Transaction の終わりまで持ちます。Restart（PAW-032 の Command は `FOR NO KEY UPDATE` を取る）は、進行中の記録の Commit を待ってから実行されます。したがって、Commit された失敗は、Commit の時点で現在だった試行のものです。`FOR SHARE` は他の `record_failure`（Advisory Lock が直列化する）や外部キーの確認（`FOR KEY SHARE`）とは競合しません。
  失敗の行は、報告された試行（`attempt`）を持ち、**Task の現在の試行の行だけ**が判定に使われます（`history`、`assess`、`record_failure` が返す判定）。そのため、Restart が Commit された瞬間から、新しい試行は空の履歴で始まります（`clear_previous_attempts` の前でも同じで、古い行と一緒に数えて、新しい試行の最初の失敗が Loop と判定されることはありません）。Window の上限（`window_size`）は Task 全体の行数にかかり、現在の試行の行は古い試行の行より常に新しいため、古い行から先に消えます。
  試行の番号は既存の PAW-032 の Counter をそのまま使います（[Decision 0007](../../docs/decisions/0007-task-queue-budget-and-loop-policy.md) の 8）。Retry（同じ試行のやり直し）は試行を変えず、履歴も消しません。Migration（`0033`）の列は `attempt`（1 以上の `INTEGER`、必須）です。
- 閾値（3 回、Window 10、代替 1 回）は暫定値です（要件は具体的な閾値を実装時の選択としています）。[Decision 0007](../../docs/decisions/0007-task-queue-budget-and-loop-policy.md) で暫定値として承認されています（2026-09-25）。`LoopPolicy` のデータなので、後で変えられます。

### 次の行動（Escalation の判断）

`decide_next_action(budget_verdict, loop_verdict, can_escalate=...)` は、次の順で最初に当てはまる規則を使います。

| 条件 | 行動 |
| --- | --- |
| Budget が `EXCEEDED` | `retries` を含めば `FAIL`、それ以外は `WAIT_FOR_USER`（安全な区切りで停止し、人間が上限を上げるか終了する）。Loop の判定にかかわらず、超過を無視しません |
| Loop が `ESCALATE` | `can_escalate` なら `ESCALATE_AGENT`（Codex / Claude など）、なければ `WAIT_FOR_USER` |
| Loop が `TRY_ALTERNATIVE` | `TRY_ALTERNATIVE` |
| それ以外 | `CONTINUE` |

`WAIT_FOR_USER` は PAW-032 の `wait`（`WaitReason.USER`）、`FAIL` は `fail` に対応します（`domain.ACTION_TASK_COMMANDS`）。Command を発行するのは Orchestrator（PAW-034）で、この Module は発行しません。
この対応（`retries` は `FAIL`、他は `WAIT_FOR_USER`、Budget を Loop より優先）も [Decision 0007](../../docs/decisions/0007-task-queue-budget-and-loop-policy.md) で承認されています（2026-09-25）。
Budget 超過のときに Escalation しないのは、使い切った予算をさらに使うためです。`Waiting for Resource` は GPU の Scheduler（PAW-036）の担当で、ここでは使いません。

### 実装の出自

`task_queue.py`、`budget.py`、`loop.py` は、試験を書いた側（Claude）が書いた**参照実装**です。ローカルモデルによる 2 回の実装は、これらの試験を通せませんでした。
`escalation.py`（`decide_next_action`）だけは、ローカルの Qwen3-Coder-30B-A3B が書いたものです。試験を通ったあと、レビューで冗長な部分を整理しました（振る舞いは変えていません）。
試験は実 PostgreSQL に対する並行 Claim の試験を含み、参照実装に対する変異（境界、行ロック、原子性など 32 通り）のうち、実質同じ動作になる 1 つを除く全てを検出することを確認しています。

### 未確定の事項と制限

要件に定めがなく、実装が置いた値・選択です。**[Decision 0007](../../docs/decisions/0007-task-queue-budget-and-loop-policy.md) は 2026-09-25 に Human が承認しました（Approved）。** 数値は暫定値として承認されたもので、後で変えられます。次の項目のうち、未決なのは「Loop 検知の範囲」の担当だけです。

- **Preset の数値。** 上の表は暫定値として承認されています。
- **Loop の閾値。** 3 回、Window 10、代替 1 回は暫定値として承認されています。
- **Loop 検知の範囲。** 検知するのは**失敗の繰り返しだけ**です（`record_failure` が受け取る `error_class` / `step` / `message`）。要件は「同じ Tool Call」「同種の修正」の繰り返しも対象としますが、成功した同じ Tool Call、何も変えない Tool Call、同種の修正の繰り返しは Window に入らず、`TRY_ALTERNATIVE` / `ESCALATE` になりません。今はそれらを `tool_calls` / `steps` / `runtime_seconds` の Budget の上限が止めるだけで、`Unlimited` の Task では止まりません。PAW-033 の受け入れ条件は「repeated failure loop detection」だけで、担当の Issue は要件にも Backlog にもなく**未決**です（入力を持つのは Tool Broker（PAW-031）、Worktree（PAW-035）、判定する Orchestrator（PAW-034）。[Decision 0007](../../docs/decisions/0007-task-queue-budget-and-loop-policy.md) の 3。担当の決定は別途で、2026-09-25 の承認には含まれません）。
- **Unlimited の上限。** 要件は Unlimited に Runtime などの数値の上限を定めていないため、完全に無制限です。暴走を止めるのは Loop 検知と、Operator の Stop Now、Critical safety です。Human は、Unlimited に絶対の上限を設けないことを承認しました（2026-09-25）。上限を設けるなら、値を決める新しい Decision が要ります。
- **飢餓。** Aging がないため、`HIGH` / `NORMAL` が続くと `LOW` が飢えます。Human は、Aging を入れないことを承認しました（2026-09-25）。要件が規則を定めたら追加します。
- **Budget 超過時の行動。** `retries` は `FAIL`、他は `WAIT_FOR_USER`、Escalation より Budget を優先する、という割り当ては、[Decision 0007](../../docs/decisions/0007-task-queue-budget-and-loop-policy.md) で承認されています（2026-09-25）。警告の閾値が要件にないため `WARN` はありません。

制限。

- 量は 0 以上の整数だけです。GPU 時間は整数秒で、`float` は拒否します（呼び出し側が切り上げてください）。
- Queue は `tasks.state` を読まず、変更もしません。`claim_next` と PAW-032 の `start` を組み合わせるのは PAW-034 です（[実装済み](#dag-agent-orchestrator)）。
- Lease の切れた Entry は、次の `claim_next` が自動で取り直します。実行中の Process を止める処理（`stop_now` など）は含みません。期限を過ぎた Worker の完了報告は拒否されます。
- 優先度の引き上げ、Preset の変更、Queue の一覧は認可付きの操作で、Endpoint と一緒に追加します。
- `start_runtime` は、`BudgetTracker` が Queue を読まないため、呼んだ Worker が Lease を持つかを確認しません。Lease を失った古い Worker が `start_runtime` を呼ぶと、Session を引き継げてしまいます（時間は数え続けるので Budget は回避されませんが、新しい Worker の `stop_runtime` は `StaleRuntimeSessionError` になります）。Lease を持つ Worker だけが呼ぶ規則は、Orchestrator（PAW-034）の責務です（`Orchestrator` は `start_runtime` の直前に Heartbeat で Lease を確かめます）。Tracker が Queue の Entry を確かめる案は採らないことを、[Decision 0007](../../docs/decisions/0007-task-queue-budget-and-loop-policy.md) の 10 で承認しています（2026-09-25。「Lease を持つ Worker だけが呼ぶ」規則は PAW-034 の受け入れ条件に置きます）。
- Migration `0033` の `down_revision` は `0021` です（鎖は `0001 → 0025 → 0032 → 0040 → 0021 → 0033`）。

## 認可（RBAC / Capability）と Audit

権限の判定は Backend だけが行います。Frontend の表示、Client が送る Header・Query・Body、Prompt、Model の出力は判定の入力になりません。
判定は型付きの入力（`Principal`、`Capability`、`Resource`）だけから決まる純粋関数で、**既定は拒否**です
（[設計](../../docs/SECURITY_RBAC_AUDIT.md)、[Tool 権限](../../docs/SECURITY_TOOL_PERMISSIONS.md)、[Decision 0004](../../docs/decisions/0004-rbac-capability-and-audit-policy.md)）。
呼び出す側は `Authorizer` を使います。判定だけを行う `policy.decide` などは Audit を書かないため、`paw_backend.authz` から公開していません。

**[Decision 0004](../../docs/decisions/0004-rbac-capability-and-audit-policy.md) は、2026-09-25 に Human が承認しました（Approved）。**
この節の Owner / Admin の権限、Agent への委任、Audit Mode、Fail-closed の選択は、承認された方針です。

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
  Project 設定・Repository 追加・Project Memory 管理を含む管理系、`admin.*`、`owner.*`、`shared_memory.manage` と Shared Memory を変える操作の Capability（`shared_memory.create` など）、Member / Agent Policy / Lifecycle は Grant に書いてあっても拒否します（自己権限昇格の禁止）。
  **`agent.use` と `project.agent.use`（Agent を起動する操作）も委任できません。** 子 Agent の Grant を親の部分集合として導く仕組み（PAW-032）ができるまで、Agent が自分より強い Agent を作れないようにするためです。
- `AgentGrant.project_ids` は**必須**です。Agent が触れる Project の集合か、明示的な `ALL_PROJECTS` を渡します（既定の「User の全 Project」はありません）。
  Project を限定した Grant は、その外の Resource（個人のデータを含む）に及びません。文字列 1 つを渡すと `TypeError` です。
- User の Principal は**判定のたびに** `PrincipalDirectory` から引き直します。Role を外す、User を削除する、といった変更は Agent の次の操作から効きます。
  Directory が User を返さない、例外を出す、`PAW_DATABASE_TIMEOUT_SECONDS` を超える、別の User を返す、または委任元 ID が正規の UUID でないときは、
  Audit を書いたうえで `delegator_not_active` で拒否します（エラーにはしません）。既定の `NoPrincipalDirectory` は誰も返さないので、User Store ができるまで Agent の操作は許可されません。
  引き直しの期限は、Directory が Cancel にどう反応するかに**依存しません**。引き直しは独立した Task で走らせ、期限まで待ち（`asyncio.wait`）、期限が来たら Cancel を依頼して**その終了を待たずに**拒否します。
  Directory が PostgreSQL の応答しない Query の Cancel 待ち（約 10 秒、または永久）に入っても、Agent の判定は期限内に Audit 付きの拒否になります。期限後に Task が返した値や例外は捨てます（使わず、Log にも出しません）。
  Directory の実装は自分でも Cancel で仕事を止めてください。PostgreSQL を読む実装は、Pool 経由の SQLAlchemy / psycopg 呼び出しではなく `Database.fetch_abortable` を使います（期限で接続の Socket を閉じるので、放棄された引き直しが接続を握り続けません）。
  期限後も終わっていない引き直しは最大 32 件まで許容し、それ以上は新しい引き直しをせずに同じ拒否にします（応答しない Directory に Task を積み上げないため）。
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
  Write は接続 Pool を使わず、その 1 文専用の接続（自動 Commit）で行い、期限（と呼び出し側の Cancel、`dispose()`）で**接続の Socket を閉じて**止めます。接続を受け付けたまま応答しない PostgreSQL に対して、Driver がサーバーへ Cancel を依頼して待つ（約 10 秒、または古い libpq では Thread の完了待ち）のを避けるためです（`Database.execute_abortable`、起動時の診断と同じ仕組み）。同時に開く接続は Pool の大きさまでで、空きがなければ待ちますが、空き待ちと実行は**1 つの期限を共有**します（空き待ちに使った分だけ実行に使える時間が減り、1 回の呼び出しが期限を超えることはありません）。接続の枠は Query の Task が**実際に終わるまで**保持します（呼び出しが期限で戻っても、Socket の Shutdown に失敗した、Driver の後始末が終わらない、といった理由で Query がまだ動いていれば、枠は空きません）。そのため、DB の障害中でも専用接続が `PAW_DATABASE_POOL_SIZE` を超えて増えることはなく、次の Audit の Write は枠が空くまで待つか、期限で失敗します（許可は拒否になります）。打ち切られた Write は Commit されたかどうか分かりません（許可は拒否に変わり、Audit 行が残っていることがあります）。
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
  Override が Project の Role を広げてよいか、User 単位の許可リストを持つかは、要件が定めておらず、Decision 0004 で Human が「狭めるだけ・権限の集合」で承認しました（2026-09-25）。
- `Scope.SELF` の Capability（`chat.use`、`memory.use` など）は `Project` の状態と Member 資格を見ません
  （たとえば Pending deletion の Project の Chat、Member から外された後の Memory）。Project との Member 関係は PAW-026 の `project_members` にありますが、これらの Capability の判定はまだ Member 資格を見ません（[Project CRUD / Membership / Lifecycle](#project-crud--membership--lifecycle)）。
- `tests/test_authz_routes.py` が調べるのは `/api/v1` の Route だけで、FastAPI の内部（`effective_route_contexts`）に依存します。Method の一覧を持たない Route（`Mount` など）は Method `*` の 1 操作として報告し、見逃しません。
- `create_app` は既定の Provider と Directory を組み込みます。PAW-022 が `install_authz` を呼んで差し替えるまで、全 Endpoint が 401 です。
- 重要操作の Step-up 認証の項目は Audit にありません（PAW-023 で追加します）。
- Migration の鎖は `0001 → 0025 → 0032 → 0040 → 0021` です（`0021` の Revision ID は Issue 番号で、鎖の順序ではありません。統合時に並びを確認します）。

## Owner の初期設定と復旧

[PAW-021](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/18) で実装しました。設計は [要件](../../REQUIREMENTS.md)（Owner、Passkey Policy、Login / Session、全 Passkey を失った Owner は Ubuntu の sudo 経由で復旧）と
[SECURITY_RBAC_AUDIT](../../docs/SECURITY_RBAC_AUDIT.md) に従い、判断が要った点は [Decision 0005（Approved。2026-09-25）](../../docs/decisions/0005-owner-setup-and-recovery.md) にまとめています。

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
- **1 回限り**。消費は `UPDATE ... WHERE used_at IS NULL AND revoked_at IS NULL AND expires_at > <その文が行を判定する瞬間>` の 1 文で行うため、同時に 2 回使っても片方だけが成功します（その前に Token の行を `SELECT ... FOR UPDATE` で Lock するので、この文が行を待つことはありません）。
  1 人の User について、未使用で無効になっていない Token は最大 1 つです（Partial Unique Index）。新しく発行すると、古い Token は先に無効にされます（`owner-recover`）。
- **有効期限**は `PAW_SETUP_TOKEN_TTL_SECONDS`（既定 1800、60〜**14400**）。期限ちょうどの時刻は無効です。既定値は暫定値として承認されたもの（Decision 0005）で、設定で変えられます。
  期限は**消費の瞬間**に判定します（Decision 0005 の 10。承認済み）。`redeem` は開始時に 1 度判定し（期限切れの Token は Owner の行 Lock を待ちません）、Owner の `users` 行、続けて Token の行を `FOR UPDATE` で Lock した**後**に Clock を読み、消費する 1 文の中で判定し直します。別の Transaction が Owner の行や Token の行を Lock している間に待たされても、待っている間に期限が切れた Token は使用済みになりません（拒否は同じ `SetupTokenRejectedError`、Audit は deny `token_expired`。Lock を待つ間に使用済み・無効化済みになった Token は `token_unavailable`）。Token の行を先に Lock するのは、待つ `UPDATE` が、持ち主が行を変えずに手放したとき、待つ前に評価した期限の条件を評価し直さずに続行しうるためです（Test は、Token の行を別の Transaction が持つ間に期限を過ぎさせ、その Token が消費されないこと、期限内なら待った後の時刻が `used_at` に入ることを確かめます。待ちは `pg_stat_activity` と `pg_blocking_pids` で確認します）。
  その文は、この Process の Clock（Lock を得た後に読み直した値。Test が動かす時計）と DB の `clock_timestamp()`（`now()` は Transaction の開始時刻なので使いません）の**新しいほう**を「現在」とします。どちらか一方が期限を過ぎたと言えば期限切れで、Clock がずれていても Token の寿命が**短くなる**側にしか働きません（Test は、止まった Process Clock でも DB の Clock だけで拒否されることを確かめます）。`used_at` は Process の Clock で、Lock を得た後の時刻を記録します。
  **発行する側も、寿命は保存の瞬間から数えます**（Decision 0005 の 10）。`owner-recover` と `owner-setup --replace-non-live-owner` は Owner の行を Lock する間、別の Transaction に待たされることがあります。Process の Clock は Lock を得た**後**に読み（旧 Token の行も先に Lock してから読み直し、その値を旧 Token の `revoked_at`、旧 Owner の降格、新 User の時刻に使います）、Token の `created_at` / `expires_at` と表示する `IssuedToken.expires_at` は、待ちうる文（旧 Token の無効化、新 Owner の INSERT）がすべて終わった後にもう一度読んだ値から決めます。待ちが TTL を超えても、表示された Token には TTL の全体が残ります（Test は、Owner の行・旧 Token の行・競合する Owner の INSERT を別の Transaction が持つ間に発行し、Clock を TTL 以上進めてから解放します。`pg_stat_activity` で待ちを確認します）。最初の `owner-setup` の新 User の `created_at` だけは、競合する INSERT を待つ前の時刻のままです（記録用で、Token の寿命には関わりません）。
- **試行の上限**は Token ごとに `PAW_SETUP_TOKEN_MAX_ATTEMPTS`（既定 5、1〜20。暫定値として承認済みで、設定で変えられます）です。試行は Secret を比較する**前に**予約して Commit するため、同時に大量の Request が来ても比較は上限回までしか行われません。
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
  **`owner-recover` は root でなければ拒否します**（要件は Ubuntu の `sudo` 経由の Recovery です）。Service 自身が、動いている Process の実効 uid が 0 であることだけを見て（呼び出し側が渡した値は信用しません。`recover_owner` にも `setup_owner` にも Identity を渡す引数はありません）、`SUDO_UID` は認可に使いません（誰でも設定できる環境変数のため）。Token の行に記録する uid と `SUDO_UID` も、この同じ読み取りの値です（`IssuedToken.operator` に入り、stderr の表示もこれです）。Test は `os.geteuid` を差し替えて Process の uid を変えます（`tests/identity_support.py` の `running_as`）。Production のコードにその手段はありません。拒否は Audit に `owner.recovery_token.issue` / deny / `not_privileged` として残り、何も変更しません。`owner-setup` は OS User を確認しません（Decision 0005 で、確認を付けないことを承認済みです）。Audit には専用の項目を足さず、Token の行との Join で調べる方式のままです（同じく承認済みで、必要が分かれば新しい Decision で扱います）。
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
紛らわしい文字を避けるための規則で、ASCII のみとすることを Human が承認しました（[Decision 0005](../../docs/decisions/0005-owner-setup-and-recovery.md)、2026-09-25）。変えるには Migration が要ります。

**`downgrade` は `users` と `setup_tokens` を Table ごと破棄します。Owner を含む全 User と全 Token が失われます。** 開発・Test 用で、本番では実行しないでください。

### 既知の制限

- Token ID を知っている人は、試行を使い切らせて正規の使用を妨げられます（Token ID は Token の一部で、通常は Token を知る人しか持ちません。Audit と Log には書かないため、Audit を読める人は知りません）。回復は `owner-recover` です。
- 比較と DB 往復の回数は全経路で同じですが、**時間そのものは揃えていません**。既存の Token に対する失敗だけは Audit の INSERT が加わるため僅かに長く、これを観測できるのは Token ID を知る人だけです。
- **Rate Limit は Token ごとの試行の上限だけです。** 接続元ごと・全体の Limit は PAW-022 の Endpoint の責務です（受け入れ条件として Issue [#19](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/19) に追記済み）。
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

`ToolCall` の `tool` と `arguments` だけが Model の出力です。Task、委任元 User、`AgentGrant`、Task Scope、Task の **Run**（`TaskRun(attempt, retry_count)`。Worker が開始された Task の `attempt` と `retry_count`）は Backend が `TaskContext` に解決します。`run` は必須で、Task Lifecycle の `paw_backend.tasks.TaskRun`（`TaskEvent.run` / `TaskSnapshot.run`）をそのまま渡します（Broker に別の `TaskRun` はなく、似た別のクラスは `TypeError` です。別クラスだと Run が等しくならず、現在の Run が `task_superseded` になってしまうため）。

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

この表は 36 マスのうち 24 マスです（Project-local の 3 列と、Host 全体の環境の「範囲内」）。残りの 12 マス（Host 全体の環境の「範囲外の Host」と「範囲外」）は、Project-local の同じ列と同じ値です（「範囲外の Host」は `read` / `write` / `network` が `APPROVAL`、`execute` / `credential-use` / `destructive` が `DENY`。「範囲外」は全て `DENY`）。
「範囲外の Host」は、対象のうち Host だけが `TaskScope.hosts` の外にある場合です。Path・Project・Repository・Credential のどれかも外にあれば「範囲外」になり、こちらが優先します。

- 例: `web.fetch`（read + network）も Issue 作成（write + network）も、Task の Host の外なら `APPROVAL`。`git.merge` は `min_level=STRONG_APPROVAL` で `STRONG_APPROVAL`。Credential を使う呼び出しは、Host が Task の Host でも、その Credential の使える Host でなければ `DENY`（下の Credential）。
- **Task Scope の範囲外は Policy で許可できません。** `ToolPolicy` は、範囲外（`out_of_scope`）を `DENY` 以外にする表を作れません（`ValueError`）。Host だけは、Task の Host を超える読み取り・外部 write を `APPROVAL` にします（要件の「通常 Task scope を超える外部 write」）。
- Host 全体の環境（package、service、firewall、proxy、mount）を変える Tool は `environment=Environment.HOST` を宣言します。宣言は Tool 側で、呼び出しでは選べません。この環境では読み取り以外は `APPROVAL` 以上です。
- **Credential plaintext の取得は常に `DENY` です**（下の Credential）。

**要件との違い。** 表は次のように解釈しています。要件の表と同じにはなっていない点（**厳しい方向と、Tool 側の宣言に頼る方向の両方**）を隠さず書きます。この表と解釈は [Decision 0006](../../docs/decisions/0006-tool-broker-policy.md)（Approved。2026-09-25 に Human が承認）に記録しています。

- 厳しい方向:
  build / test / lint などの実行は `SCOPED_AUTO`（実行は Project のコードを走らせ、書き込みもできるため。人の確認が要らない点は `AUTO` と同じ）。
  Task Scope 内の一時ファイルの削除も `destructive` なので `APPROVAL`（`SCOPED_AUTO` にするには「一時ファイルだけを消す」Tool を別に宣言する必要があり、まだ決めていない）。
  Task の Host の外への Web の読み取りは、要件の「Web / Docs の read-only 取得: `AUTO`」と違い `APPROVAL`（Host の指定がなければ、どの Host へも行ける）。
- **緩い方向（Tool 側の宣言が前提）:**
  Task Scope 内の `credential-use`（handle を使う）は `SCOPED_AUTO` です。要件の `STRONG_APPROVAL` の項は Credential の**登録・更新・削除**で、使用は分類されていません（AI 専用 Branch への push と PR 作成は `SCOPED_AUTO`）。Credential を管理する Tool は `min_level=STRONG_APPROVAL` を宣言してください（Agent には委任できない操作です）。
  Host 全体の `sudo` / 特権操作は、要件では `STRONG_APPROVAL` ですが、表は `HOST` 環境の `write` / `execute` を `APPROVAL` にしています。特権の Tool は `min_level=STRONG_APPROVAL` を宣言する必要があります（表は Tool の中身を知りません）。
  Human はこの 2 点を、条件つきで承認しました（Decision 0006）。**Tool の登録時のレビューで、Credential を管理する Tool と特権 Tool が `STRONG_APPROVAL` を宣言していることを確認してください。**

### 判定の順序

前の段階が通った場合だけ次へ進み、各段階は**狭める方向にしか**働きません。

1. 呼び出しの形。`ToolCall` でなければ `invalid_call`。Tool 名は登録済みの名前と**完全一致**だけ（大文字小文字、空白、Zero-width、Unicode の見た目が近い文字は別の名前）。なければ `unknown_tool`。
2. `returns_credential_plaintext` の Tool は `credential_plaintext_denied`。引数を Tool の宣言（`ArgumentSpec`）と照合します。宣言にない引数、足りない必須の引数、型の違い（`"true"` は bool でなく、`True` は int でない）、長さや範囲の超過は `invalid_arguments`。Path / Host / URL / Project / Repository / Credential handle は正規化して `invalid_target` または下の理由で拒否します。文字列に Credential の平文があれば `credential_plaintext_in_arguments`。
   **長さの検査は Credential の走査より先**です（Tool の宣言した `max_length`、Path / URL / Host / handle は種別ごとの上限）。1 回の呼び出しの文字列の合計にも上限（262,144 文字）があり、超えたものは走査も正規化もしません。
3. 対象を Task Scope と比べ（Symlink を解決）、Level を決めます。`DENY` なら `path_out_of_scope` / `host_out_of_scope` / `project_out_of_scope` / `repository_out_of_scope` / `credential_out_of_scope` / `remote_not_in_repository`（下の「Repository の ACL」）/ `policy_denied`。
4. 認可（PAW-025 の `authorize_agent_action`）。委任元 User の権限と `AgentGrant` の積集合で、拒否は `authz_denied`（`authz_reason` に PAW-025 の理由）。Authorizer が失敗または想定外の答えなら `authz_unavailable`。
   **Repository の呼び出しは、Repository とその ACL で判定します**（下の「Repository の ACL」）。Project の Resource だけでは Repo ACL の override（読み取り専用、Agent 禁止）が効かないためです。
5. Task Budget（`BudgetProvider`）。超過は `budget_exceeded`、不明は `budget_unknown`、Provider の失敗・Timeout・想定外の答えは `budget_unavailable`。
6. `AUTO` / `SCOPED_AUTO` は `ALLOW`（`auto` / `scoped_auto`）。`APPROVAL` / `STRONG_APPROVAL` は、**Task が Worker の Run でまだ動けること**（`TaskActivityProvider`。完了・失敗・取り消し済み、不明、読めない、Retry / Restart で別の Run に替わった、は `task_not_active` / `task_unknown` / `task_state_unavailable` / `task_superseded`）を確認してから、下の Approval に進みます（承認者に見せられない呼び出し、Open な承認が多すぎる、直前に却下された、は `approval_not_displayable` / `approval_limit_reached` / `approval_cooldown`）。

判定は `AuditSink` へ記録します（下の Audit）。**記録できない `ALLOW` は `DENY`（`audit_unavailable`）になります。**
Approval の要求（`NEEDS_APPROVAL`）を作る前に、Path・認可・Budget の判定が済んでいるため、実行できない呼び出しの承認要求は作りません。

### Tool Registry

`ToolRegistry` は起動時に一度だけ `ToolSpec` の一覧から作り、追加・置換・削除の方法がありません。`ToolSpec` は Tool 名、Capability class、対応する PAW-025 の Capability（必須）、引数の宣言、Environment、`min_level`、`requires_budget`（既定 `True`）を持ちます。
宣言の整合性は生成時に検査します。**Host / URL の引数を持つ Tool は `network`、Credential handle の引数を持つ Tool は `credential-use` でなければならず**、Project-local の `write` / `destructive` は触る対象（Path / Host / URL / Project の**必須**の引数）を宣言しなければなりません
（対象のない書き込みは、常に「範囲内」に見えるため。省略できる引数は対象を宣言したことになりません）。`network` は必須の Host / URL、`credential-use` は必須の handle が要ります。Repository への書き込み（PAW-025 の `project.repo.write` / `project.pr.create`）の Tool は、触れる Repository を表す**必須の Path か Repository の引数**が要ります（Host / Project は Repository を表さず、URL は Backend が登録した Remote の下にあるときだけ表すため、これらだけでは Repository の ACL を効かせられません）。Tool 名は `unknown` と `approval*` を使えません（Audit の action と衝突するため）。

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
Broker は、呼び出しがどの Repository に触れるかを **Backend が作った `TaskScope.repositories`**（`ScopedRepository`: Repository の ID、Project、Worktree の Path、**解決済みの `RepoAcl`**、**Repository を表す URL の `remotes`**）から決め、その Repository に対する `Resource.repository(...)` で `authorize_agent_action` を呼びます。ACL を読むのは Authorizer（PAW-025 の `decide` / `decide_agent`）で、Broker は判定を自分で作りません。

| 触れる Repository | 決まり方 |
| --- | --- |
| 呼び出しが名指し | `repository` 引数（`ArgumentKind.REPOSITORY`）。Task の作業対象にない ID は `repository_out_of_scope`（DENY） |
| Path | **Symlink を解決した後の** Path が、Repository の Worktree（同じく解決する）の中にあるもの。入れ子の Repository の中の Path は、外側と内側の**両方**に触れます（厳しい方の ACL が効く） |
| URL | Backend が Repository に登録した **Remote（`ScopedRepository.remotes`）の下にある** URL は、その Repository に触れます（`.../repo` の下は `.../repo/info/refs` まで。`.../repo-evil`、`.../repo.git`、大文字小文字違いは下ではありません。`..`・`\`・`;`・`%2e` `%2f` `%5c` を含む Path は Remote の外へ出られるため下と見なしません。Query は Path ではないので無視）。`repository` 引数を持つ Tool の URL が**別の** Repository の Remote の下なら、その Repository の ACL も効きます（片方が読み取り専用なら `authz_denied`） |
| Host | **Repository を表しません**（同じ Host に多くの Repository があるため） |

- **Repository に触れる呼び出しの URL は、作業対象のどの Repository の Remote の下にもなければ拒否します**（`remote_not_in_repository`、Level は `DENY`）。同じ Host の Repository は数多くあり、`repository` 引数だけを認可すると、Executor が受け取る URL（Model が書いたもの）が ACL を解決していない別の Repository を指せてしまうためです（例: 書き込める B を名指しし、`remote` に読み取り専用の A の URL）。
  Remote を登録しない Repository は URL を持たず、URL を伴う呼び出しは通りません（既定は拒否）。Remote は、Executor に渡す綴り（`.../repo` と `.../repo.git`、API の Base URL）ごとに登録します（1 つの Repository に 8 つまで。Query・末尾の `/`・Host だけの URL・`..` などは登録時に `ValueError`）。
  Host の規則が先です（Scope の外の Host は従来どおり上の表で決まります。Credential を使わない書き込み（`write` + `network`）も読み取りも `APPROVAL`、Credential を使う・`execute` / `destructive` を持つ Tool は `DENY`）。Repository に触れない呼び出し（`web.fetch` など）の URL は、これまでどおり Host の検査だけです。
- **ACL が不明なら拒否します。** `ScopedRepository.acl=None`（Backend が解決できなかった）は `inherit` とは読まれず、`authz_denied`（`authz_reason=repo_acl_unresolved`）。ACL は Repository と Project が一致するものだけを `ScopedRepository` に入れられます（違えば構築時に `ValueError`）。
- **ACL は呼び出しごとの現在の値です。** Task の Scope は呼び出しごとに作り直すため、ACL の変更は次の呼び出しから効きます。承認を使うときも認可をもう一度行うので、ACL が狭まった後は承認済みの呼び出しも `authz_denied` になり、承認は消費されません。
- **Repository への書き込み**（PAW-025 の `project.repo.write` / `project.pr.create`）の Tool は、触れる Repository を **Path か Repository の必須引数**で宣言しなければなりません（`ToolSpec` の生成時に `ValueError`）。それでも作業対象のどの Repository にも触れない呼び出し（作業対象の外の Path など）は `repository_not_identified` で拒否します（ACL を読めない書き込みは通しません）。
- 読み取りと Agent 実行（`project.read`、`project.task.run` など）で、触れる Repository がない呼び出しは、これまでどおり Project の Resource で判定します。引数のない Tool は Repository の ACL では判定できないことを、既知の制限に書きます。
- Repository を表さない Project の Capability（`project.chat`、`project.settings.manage` など）は Project の Resource のままです（PAW-025 は、これらに Repository の Resource を渡すと拒否します）。
- 判断の理由と、Human が承認した点（2026-09-25）は [Decision 0006](../../docs/decisions/0006-tool-broker-policy.md) の「8. Repository の ACL」。

### Credential

- **平文は Agent の引数にも結果にも入れません。** 使うときは不透明な handle だけで、handle を解決して Credential を付けるのは Executor（Backend 内部）です。Tool の引数が handle であり、その handle が Task の使える handle に入っていることを Broker が確認します（`credential_out_of_scope`）。
- **handle は、使える Host に束縛されています。** `TaskScope.credential_handles` は `{handle: その Credential の使える Host の集合}` です。同じ呼び出しが触れる Host のどれかがその集合に含まれなければ `credential_out_of_scope`（DENY。承認では許可できません）。Task が両方の Host に触れられても、GitHub の handle を別の Service へ送る呼び出しは通りません。Host のない呼び出し（何も送らない）には影響しません。
- 引数の文字列に Credential の平文があれば `DENY` します。形のわかる Format: GitHub（`ghp_` など、`github_pat_`）、GitLab、`sk-` の Key、Stripe、AWS の Key ID、Google の API Key と OAuth Token、Slack の Token と Webhook、npm、PyPI、Hugging Face、SendGrid、Docker、DigitalOcean、JWT、Bearer / Basic、PEM / PGP の秘密鍵、`user:password@` 付きの URL。
  Token は Key 名に連結していても（`MYTOKEN_ghp_...`、`OPENAI_API_KEY_sk-...`、`key_AKIA...`）検出します（`_` の直前は英数字でなければよい。`disk-...` のような単語の途中は一致させません）。Zero-width 文字や全角文字で隠した形も検出します。
  ソースコードで普通に出る `password = "..."` は引数では拒否しません（Agent がコードを書けなくなるため）。
- **結果は、返す前と Log へ出す前に Redact します。** 検出した Credential のほか、`.env`・JSON・YAML・ini・`--password x` の代入の形（`DB_PASSWORD=...`、`AWS_SECRET_ACCESS_KEY=...`、`{"db_password": "..."}`。Key 名の前後に語が付いてよく、引用符つきの値は空白を含めて）は**値だけ**を `[REDACTED]` にします（Key 名は残ります）。Dict の Key 自体も Redact します。
  `password` / `token` / `api_key` / `secret` / `authorization` / `credential` などを含む Key の下の、数値・bool 以外の値は中身によらず置き換えます。JSON のデータでない Object（`repr` に何が入るか分からないため）は固定の Marker（`[UNSUPPORTED]`）です。
  1 つの結果は読み取りに上限（100,000 値、4,000,000 文字）があり、超えた分は `[TRUNCATED]` 1 つになります（200 万要素の List の Redact に 8.9 秒かかった問題への歯止め）。
  Dict の Key の文字も同じ文字数の上限に数えます。上限に収まらない最初の Key は、折り畳みも走査もせず（巨大な Key 1 つで上限を超える仕事をさせないため）、その Value とそれより後の要素も読まずに、`[TRUNCATED]` 1 つにして読み取りを終えます（Key の文字数がちょうど上限に収まるものは、これまでどおり読みます）。
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
| 単回 | 承認は 1 回だけ使えます（実行が失敗しても消費済みです）。`UPDATE ... WHERE status = 'approved' AND expires_at > now AND`（Task、**Task の Run**、Agent、User、Tool、Level、Hash がすべて一致）の 1 文で消費するため、同時に何個の呼び出しが来ても 1 つだけが成功します（再利用は `approval_already_used`） |
| 期限 | 作成から `approval_ttl`（既定 1 時間、1 分〜24 時間）。期限ちょうども期限切れです。期限切れは `approval_expired` |
| 別の呼び出し | 引数・Tool・Task・Agent・User・Level のどれかが違えば `approval_mismatch`（承認は消費されません） |
| 別の Run | 同じ呼び出しでも、承認を求めた Run（`tool_approvals.task_attempt` / `task_retry_count`）と違う Run の Worker は使えません（`approval_superseded`。承認は消費されません）。下の「Task の終了と承認」の「Run への結びつけ」 |
| 承認できる人 | Agent が働いている **User 本人だけ**（`ApprovalService.approve / reject`、引数は人間の `Principal`）。Agent 自身の ID は `self_approval`。他の人は Admin / Owner でも、存在を教えず `not_found`（Audit には `not_authorised`）。DB の CHECK 制約も、承認者が委任元 User であること、Agent が User と別であることを保証します |
| `STRONG_APPROVAL` | 承認のとき `StepUpVerifier.verify(user_id, approval_id)` が**明示的な `True`** を返す必要があります（PAW-023 が実装）。Verifier がない、`False`、例外、Timeout、`True` 以外の答えは `step_up_required` で、承認は保留のままです。Store の `decide` も `step_up_verified` を受け取り、Step-up なしには強い承認を保存しません（`step_up_verified` の列と CHECK 制約。Store を直接呼ぶ側にも効きます） |
| 取り消し | `ApprovalService.revoke`。委任元 User と、Admin / Owner（権利を減らす方向だけなので代われる）。pending・承認済みで未使用の承認だけ。Task の終了での取り消しは、下の「Task の終了と承認」。使うときは `approval_revoked` |
| 使うとき | 認可・Scope・Budget を**もう一度**判定します。承認は権限を広げません。拒否された使用は承認を消費しません |

`ApprovalService` は Broker と**別の Object**です。Agent の Runtime へは Broker（または Runner）だけを渡し、`ApprovalService` は渡さないでください（渡さなくても上の規則が守られますが、それが最初の防御です）。

**人の判断（承認・却下・取り消し）の Store 呼び出し、そして Broker が呼ぶ要求と使用も、時間で区切ります。** 独立 Review が、接続は受け付けるが Query に答えない PostgreSQL に、`approve` / `reject` / `revoke` の照会（`get`）と更新（`decide` / `revoke`）が無期限に待たされ、承認の HTTP 要求と Pool の枠が塞がると指摘しました（`revoke_task`・Step-up・Listener・Audit は区切られていました）。Pool の Session の Query を `asyncio.timeout` で取り消しても、サーバが取り消しを確認しないため約 10 秒かかり、期限になりません（実測）。そこで 2 段にしました。

1. `ApprovalService` は、1 回の操作の Store 呼び出し（照会と更新）を**1 つの期限 `timeout_seconds`** で区切ります。期限は操作の開始時に 1 回だけ数え、各呼び出しには残りを渡します（呼び出しごとに数え直すと、1 回の操作が 2 倍かかります）。使い切った後は次の呼び出しを始めません。期限になれば型付きの結果 `ApprovalOutcome.UNAVAILABLE` を返し、型名だけを Log に残します（Audit 行は、照会に成功して更新まで進んだ場合に `unavailable` で残ります）。Step-up は自分の期限を持つので、この期限には数えません。
2. `PostgresApprovalStore` の `get` / `decide` / `revoke` は、Pool を使わない**中断可能な接続**（`Database.fetch_abortable`）で、変更と履歴の行を 1 つにした CTE の **1 つの Statement**（原子的）として実行し、`decision_timeout_seconds`（既定 3 秒）で Socket を閉じます。拒否の理由を説明する読み取りや、期限切れの印付けが要る呼び出しは、それらと**1 つの期限**を分け合います。

期限を過ぎた Statement は、Server 側では続きが実行されることがあります（`revoke_task` と同じ）。ただし 1 つの Statement なので、承認の行と履歴の行は**両方が反映されるか、どちらも反映されない**かで、部分的な状態にはなりません。呼び直すと真の状態が返ります（反映済みなら `not_pending` / `not_open`）。
Test（人の判断）: `tests/test_tools_approvals.py` の `DecisionDeadlineTest`（応答しない Store、1 つの期限、Step-up は数えないこと、型名だけの Log）、`tests/test_tools_postgres.py` の `StalledServerTest`（応答しない Server）、`DecisionStatementsShareOneDeadlineTest`（Statement ごとの残り時間）、`DecisionDeadlineTest`（行の Lock で Statement を止めて期限で返ること、承認と履歴が食い違わないこと）。

**Broker が呼ぶ `open_request`（要求を開く）と `consume`（使う）も、同じ方法で区切ります。** 独立 Review が、この 2 つが Pool の Transaction で動くため、接続は受け付けるが応答しない PostgreSQL や Lock 待ちでは、Broker の `asyncio.timeout` を超えて待たされ（約 10 秒）、Pool の枠も塞ぐと指摘しました。2 つとも複数の Statement が要ります（`open_request` は (Task, User) ごとの advisory lock、Task の行の `FOR SHARE`、期限切れの印付け、前の Run の取り消し、重複・Cooldown・件数の確認、挿入、`consume` は Task の行の `FOR SHARE` と更新）。1 つの CTE にはできません。READ COMMITTED では Statement が Lock を待つ**前**に Snapshot を取るので、advisory lock の下の件数の確認が古い値で決まり、上限を超えるからです。そこで:

- `Database.transact_abortable`（新規）が、`fetch_abortable` と同じ**中断可能な接続**（Pool を使わない。空き待ちと実行が**1 つの期限**を共有。期限、呼び出し側の Cancel、`dispose()` で Socket を閉じる）の上で、**1 つの `BEGIN` / `COMMIT`** の Transaction を実行します。`PostgresApprovalStore.open_request` / `consume` は、上の規則を生の SQL にして（Statement は 1 つずつ別のまま）これで実行し、`transaction_timeout_seconds`（既定 3 秒）で呼び出し全体を区切ります。`open_request` が競合で再試行する分も、この期限を共有します。時間切れは `TimeoutError` で、Broker は型付きの `approval_unavailable`（Log は型名だけ）にします。Broker の `asyncio.timeout` が先に来ても同じです（Cancel も Socket を閉じるので、約 10 秒待ちません）。
- **打ち切られた Transaction は、全部か何もか**です。Server は閉じた接続しか見ず、次の Statement も `COMMIT` も受け取らないので、Transaction を巻き戻します（承認の行、履歴、取り消しの全て）。`COMMIT` の最中に打ち切られたときだけ、反映されたかどうかが分かりません。呼び直すと分かります（要求は `EXISTING`、使ったものは `already_used`）。使う側は失敗（Fail-closed）に倒れます（承認が使われたのに Tool が動かないことはあっても、その逆はありません）。
- **Server にも上限を伝えます。上限は Transaction 全体にかけます。** Lock を待っている Backend は、閉じた Socket に気づきません（送るものができるまで気づかない）。そのままだと、打ち切られた Transaction が Lock の持ち主が終わるまで待ち続け、取った advisory lock と Server の接続を持ち続けます。そこで Transaction の最初の Statement で `SET LOCAL transaction_timeout` を、その時点の残り時間に `_SERVER_GRACE_SECONDS`（1 秒）を足した値にします（呼び出し側の期限が必ず先に来て `TimeoutError` になり、Server の側は少し後に自分で手を引きます）。`transaction_timeout` は Transaction の開始から数える 1 つの時計で、その間に走っている Statement が Lock を待っていても、Statement の合間でも、`COMMIT` の最中でも、時間が来れば Server がその Session を終わらせ、Lock を手放します。
  独立 Review（第 6 回）が、最初は `lock_timeout` / `statement_timeout` を Transaction の**開始時**に「残り時間 + 1 秒」で 1 回だけ設定していたため、前の Statement が期限の大半（例: 3 秒のうち 2.9 秒）を使った後に Lock を待つ Statement は、そこからまた約 4 秒、Server に残れると指摘しました（`lock_timeout` / `statement_timeout` は Statement ごとに数え直される）。事実として確かめ、再現しました（旧実装は期限の 2.5 秒に対し 5.8 秒後に Backend が残っていた。新しい実装は約 3.5 秒 = 期限 + 1 秒）。`lock_timeout` / `statement_timeout` は同じ値の予備として残します（`transaction_timeout` 以上なら Server は長い方を無視するので、短くしません）。
  採らなかった案: Statement の直前ごとに残り時間から上限を設定し直す。`work` が使う接続を包み、Statement ごとに余分な往復が要り、Statement の合間と `COMMIT` は覆えず、包みを通らない実行があると漏れます。`transaction_timeout` は 1 つの設定で全部を覆います。**この経路（`transact_abortable`。承認の要求と使用）に必要な Server は PostgreSQL 18 以上です（human 決定済み 2026-09-25。Decision 0006（Approved）に記録）**（README と CI は `pgvector/pgvector:pg18`、Test は 18 の実 DB で動かしています。承認済みの Decision 0003 は major version を決めていませんが、この経路の下限は Human が 18 と決めました）。17 は `transaction_timeout` を知っていますが Test も保証もしません。16 以下は `transaction_timeout` を知らず、Transaction は最初の Statement で失敗します（弱い上限で動き続けず、Fail-closed になります）。起動時の Version 確認はありません。
  Test: `tests/test_db_transact_abortable.py` の `test_a_later_statement_cannot_outlive_the_deadline_by_the_whole_limit`（1 つ目の Statement が期限の大半を使い、2 つ目が握られた advisory lock を待つ。Backend が期限 + 猶予の少し先までに Server から消えること）と、`test_the_server_is_told_to_stop_waiting_shortly_after_the_caller`（3 つの設定の値）。

Test（要求と使用）: `tests/test_tools_postgres.py` の `StalledServerTest`（応答しない Server に、Store と Broker が期限で返ること、Pool を使わないこと、Log に接続先を出さないこと。旧実装は 10.3 秒かかり失敗）、`RequestAttemptsShareOneDeadlineTest`（再試行が 1 つの期限を共有）、`TransactionDeadlineTest`（advisory lock と Task の行と承認の行を別の Transaction で Lock して止め、期限で返ること、打ち切られた Transaction が何も残さず、Lock の持ち主が残っていても Server の Backend が自分で去ること）、`tests/test_db_transact_abortable.py`（`Database.transact_abortable` の Commit・Rollback・打ち切り・Server 側の上限・Slot の共有・`dispose()`）。
限界: `history`（Test と診断が読む。どの Request の経路からも呼ばれない）は Pool の Session で動き、期限で区切っていません。中断可能な接続は呼び出しごとに接続を張るので、Pool の Session より重いです（承認の要求と使用は Tool 呼び出しごとに 1 回で、承認の Endpoint は低頻度です。接続数は Pool の大きさで抑えています）。

#### Task の終了と承認

**Task の終わりは 3 つ**です。`completed`（完了）、`failed`、`cancelled`（`paw_backend.tasks.TERMINAL_STATES`）。Task の状態に `expired` はなく、承認は自分の `expires_at` で失効します（期限後は `approval_expired`。期限切れは取り消しの対象にもなりません）。
`failed` と `cancelled` は Retry / Restart で再び動きます。

- **正常時:** `TaskService(listeners=[approval_service.revoke_on_task_end])` を配線すると、終了の遷移が Commit された**後**に、その Task の Open な承認（pending と、承認済みで未使用）を全部取り消します（`revoked`、reason `task_ended`）。3 つの終わりの全部を `tests/test_tools_postgres.py` の `TaskEndPathsTest` が、本物の `TaskService` と Table で確かめます。
- **取り消しに失敗したとき（Store の障害・応答しない Store）:** `revoke_task` / `revoke_on_task_end` は `ApprovalRevocationError` を**上げます**（以前は Log を出して `0`、つまり「Open な承認はなかった」と同じ戻り値でした）。`TaskService` は Listener の失敗を Log（型名だけ）に残し、Commit 済みの遷移は戻りません。**再試行する仕組みはありません**（`revoke_task` は冪等なので、後から呼び直せます）。
  **取り消しは時間で区切ります。** `TaskService` は Listener を `await` するので、接続は受け付けるが Statement に答えない PostgreSQL に取り消しが待たされると、遷移の Commit の後で、Cancel / Complete / Retry の要求が返らなくなります。
  そこで `PostgresApprovalStore.revoke_task` は、取り消しと履歴を **1 つの Statement**（CTE）にして、Pool を使わない**中断可能な接続**（`Database.fetch_abortable`）で `revoke_timeout_seconds`（既定 3 秒）以内に実行し、超えたら接続の Socket を閉じて `TimeoutError` を上げます。`ApprovalService.revoke_task` はさらに、Store の呼び出しも、取り消した承認 1 件ごとの照会（`get`）・Event・Audit 行も含めた**取り消し全体を、1 つの期限 `timeout_seconds` で**区切ります（開始時に 1 回だけ数え、承認ごとには数え直しません。承認が上限の 100 件あっても全体で `timeout_seconds` です）。時間切れは `ApprovalRevocationError` になります。取り消しが Store に保存された後で期限が来た場合（報告が間に合わなかった場合）も同じ例外ですが、承認は取り消し済みで（呼び直すと `0`）、まだ報告していない承認の Event と Audit 行は残りません（Log に「何件中何件」と出ます）。
  時間切れの Statement は、Commit されたかどうか分かりません（放棄された Statement が後で完了することがあります）。冪等なので、`revoke_task` の呼び直しで終わり、その間も Broker は終わった Task の承認を使わせません。`tests/test_tools_postgres.py` の `RevocationDeadlineTest` が、承認の行を別の Transaction で Lock して Statement を止め、Task の終了が返ることを確かめます。`tests/test_tools_approvals.py` の `TaskEndTest`（`test_the_whole_revocation_shares_one_deadline` ほか）が、呼び出し 1 回ごとには期限の内でも、合計が期限を超える Store・Listener・Audit で、期限に打ち切られることを確かめます。
- **だから、Broker が独立に止めます。** 承認を要する呼び出しは、承認を**開く**ときも**使う**ときも、`TaskActivityProvider.check(task_id, run)` が `ACTIVE` を答えたときだけ進みます（`run` は `TaskContext.run`）。終了した Task の承認は、Store がまだ `approved` と言っていても使えず（`task_not_active`）、消費もされません。終了した Task には新しい承認も開きません。Provider が失敗・Timeout・想定外の答えなら `task_state_unavailable`、Task が見つからなければ `task_unknown`（既定の `FailClosedTaskActivity` は常に不明: 本物の Provider を入れるまで承認を要する呼び出しは通りません）。
  `PostgresTaskActivity` は `tasks.state`、`attempt`、`retry_count` を、Pool を使わない中断可能な接続で読みます。答えは `ACTIVE` / `ENDED`（完了・失敗・取り消し済み。Run が違っても先にこれを答えます）/ `SUPERSEDED`（Task は生きているが、別の Run に替わっている）/ `UNKNOWN`（Task がない、状態が未知、カウンタが整数でない）です。
- **使うときの確認は、消費と同じ Transaction です。** 上の確認は、使う前の早い答え（理由がはっきりする）でしかなく、確認の後で終了の遷移が Commit されることがあります。Commit された後の取り消しと消費が競うと、消費が勝った承認は `consumed` になって取り消しに拾われず、終わった Task の破壊的な呼び出しが走ってしまいます。
  そこで Broker は `ApprovalStore.consume(..., require_active_task=True)` で使い、`PostgresApprovalStore` は**同じ Transaction の中で Task の行を `FOR SHARE` で読み直してから**消費します（`ACTIVE` でなければ何も消費せず `task_not_active` / `task_unknown`）。
  進行中の終了の遷移があれば、その Commit を待って新しい状態を読み、後から来た遷移は消費の Transaction の終わりを待ちます。使うことと終わりの順序は決まり、またぐことがありません（順序は `tests/test_tools_postgres.py` の `ConsumeRacesWithTaskEndTest` が、本物の Transaction を決まった順に動かして確かめます）。
  行の Lock には `tasks` への UPDATE 権限が要り、Application の Role は Task の状態を更新するために持っています（`tests/test_tools_postgres_roles.py`）。approval の理由（取り消し済み、使用済み、別の呼び出し）が言える場合は、Task の理由より先にそれを返します。
  `InMemoryApprovalStore` は Task を持たないので、`task_activity=` を渡したときだけ同じ確認を Store の Lock の中で行います（Test の代役。本番は `PostgresApprovalStore`）。
- **開くときの確認も、挿入と同じ Transaction です。** 独立 Review が、Broker が Task を `ACTIVE` と読んだ後、要求を挿入する前に終了の遷移が Commit されると、Commit の後で動く取り消しは何も見つけられず、その後に挿入された要求が終わった Task の承認として残ると指摘しました（`ApprovalService` はその要求を承認でき、Retry / Restart で Task が再び動いた後、Listener の取り消しより先に Worker が消費する窓ができます）。
  そこで Broker は `ApprovalStore.open_request(..., require_active_task=True)` で開き、`PostgresApprovalStore` は (Task, User) ごとの advisory lock の直後、**同じ Transaction の中で Task の行を `FOR SHARE` で読み直してから**挿入します。`ACTIVE` でなければ何も作らず、同じ呼び出しの既存の要求も返さず、`OpenOutcome.TASK_NOT_ACTIVE` / `TASK_UNKNOWN`（Broker では `task_not_active` / `task_unknown`）です。
  進行中の終了の遷移はその Commit を待って終了を読み、後から来た遷移は挿入の Commit を待つので、その遷移の後の取り消しは必ず挿入された要求を見つけます（順序は `tests/test_tools_postgres.py` の `OpenRacesWithTaskEndTest` が、本物の Transaction を決まった順に動かして確かめます。Lock の権限は消費と同じです: `tests/test_tools_postgres_roles.py`）。`InMemoryApprovalStore` は `task_activity=` を渡したときだけ、同じ確認を Store の Lock の中で行います。
- **Run への結びつけ（Retry / Restart の窓）。** 独立 Review（第 4 回）が、終了時の取り消しが失敗して承認が残った Task を Restart すると、Restart の Commit の後、Listener の取り消しが動く前の窓で、**新しい試行の Worker が Task の状態の確認（`ACTIVE`）を通り、前の試行の承認を消費できる**と指摘しました（再現した: 前の試行の承認で破壊的な呼び出しが `approval_consumed` になった）。`RESTART`（`failed` / `cancelled` → `queued`）の遷移と Listener の取り消しは別の操作で、Listener の失敗は誰も再試行しません。Retry も同じ窓を持ちます。
  そこで承認は**Run**に結びつきます。Run は Task Lifecycle の `paw_backend.tasks.TaskRun(attempt, retry_count)`（`TaskEvent.run` / `TaskSnapshot.run` の型。Broker に別のクラスはない）で、`tasks.attempt`（Restart が 1 増やす）と `tasks.retry_count`（Retry が 1 増やす）です。どちらも増えるだけで、Task の再開は必ずどちらか一方を変えるので、2 つの Run が等しいのは同じ Run のときだけです。
  - `tool_approvals` に `task_attempt`（1 以上）と `task_retry_count`（0 以上）の列があり（Migration `0031`。`NOT NULL`、CHECK 制約）、要求を作った Run が入ります。Trigger は、他の「何を承認したか」の列と同じく、この 2 列の書き換えも拒否します。`ApprovalBinding` と `NewApproval` と `ApprovalRecord` も `task_run` を持ちます。
  - **使う**とき、消費の `UPDATE` の `WHERE` に Run が入ります。`require_active_task=True`（Broker は必ずそうします）では、同じ Transaction で `tasks` の行を `FOR SHARE` で読み、`state` が終了でなく、かつ**`attempt` と `retry_count` が束縛した Run に一致する**ことを確認します。**Listener が動いたかどうかに依存しません**（Listener が取り消せていなくても、前の Run の承認は使えません）。Retry / Restart の遷移が進行中なら、その Commit を待って新しい Run を読みます。
  - 結果: 承認を求めた Run より後の Run の Worker が使うと `approval_superseded`（Store の `ConsumeOutcome.SUPERSEDED`。承認は消費されず `approved` のまま）。Task の現在の Run でない Run の Worker（Restart で置き換えられた古い Worker）は、要求も使用も `task_superseded`（`TaskActivity.SUPERSEDED`）。承認そのものの状態（取り消し済み・使用済み・却下・期限切れ・別の呼び出し）が言える場合は、Run の理由より先にそれを返します（承認が使えるはずだったのに Run が違うときだけ、Task の側の理由 `task_superseded` / `task_not_active` か、`approval_superseded` を返します）。
  - **開く**とき（`require_active_task=True`）も、Task の行を読んだ Transaction の中で、要求の Run が Task の現在の Run であることを確認してから挿入します（`open_request` は `OpenOutcome.TASK_SUPERSEDED`）。そして**同じ Transaction の中で、その Task の、別の Run の Open な承認を取り消します**（システムによる取り消し。履歴に `revoked` が残ります）。それらは使えず、残していると、開いている承認は呼び出しごとに 1 つという制約のために、新しい Run が同じ呼び出しを求められなくなるためです。この取り消しは Listener の失敗を補います（Audit 行は書きません。Listener が書く `task_ended` の行は、Listener が動かなかったので存在しません。履歴 `tool_approval_events` には残ります）。他の Task の承認と、現在の Run の承認には触れません。件数の上限（`max_pending_approvals`）にも、前の Run の承認は数えません（取り消した後に数えるため）。
  - `TaskActivityProvider.check` は `(task_id, run)` を受け取ります（以前は `task_id` だけ）。`InMemoryApprovalStore`（Test の代役）は Provider にこの Run を渡し、同じ規則を守ります。
  - **限界:** `TaskContext.run` は Orchestrator が Worker の開始時の Task（`TaskSnapshot.attempt.number`、`TaskSnapshot.retry_count`）から作ります。Broker は渡された Run を信頼します（他の Context の値と同じ）が、その Run が Task の現在のものでなければ、上のとおり拒否します。`require_active_task` なしで Store を直接呼ぶ側は、Run の照合を受けません（承認の Run と束縛の Run の一致だけ）。DB の Trigger は `tasks` を読みません（Task の生涯と結びつけないため）。
  判断の理由は [Decision 0006](../../docs/decisions/0006-tool-broker-policy.md) の「9. Task の終了と承認」（Approved）。
- **期限は、Lock を取った後の時刻で判定します。** 独立 Review（第 6 回）が、`consume` が呼び出し側の `now` を、Task の行や承認の行の Lock を待つ**前**に受け取ったまま `expires_at` と比べると指摘しました。期限の直前に使い始めて Lock を待ち、その間に期限が過ぎても、待つ前の `now` では条件が通り、期限切れの承認が `consumed` になって破壊的な呼び出しが走ります（`open_request` も、待った間に期限が切れた承認を「既にある」として返し、件数に数えていました）。事実として確かめ、再現しました（Lock を握った別の Transaction と、進めた時計で。旧実装は `consumed` / `existing`）。
  `PostgresApprovalStore` は、**Lock を全て取った後に**時刻を読み直します。`consume` は Task の行（`FOR SHARE`）の次に、承認の行を `SELECT ... FOR NO KEY UPDATE` で自分で Lock し、待ちが終わってから時刻を読み、その時刻で消費の `UPDATE`・期限切れの印付け・説明（`diagnose_consume`）を判定します。`open_request` は advisory lock と Task の行の後に読み、期限切れの印付け、前の Run の承認の取り消し、Open な件数の数え方に使います。
  「読み直した時刻」は、**呼び出し側の `now` に、その呼び出しが始まってから経った時間（単調時計）を足したもの**です（`PostgresApprovalStore(monotonic=...)`。既定は `time.monotonic`、Test は手で動かす時計を渡します）。理由は 2 つです。(1) 期限を比べる時計は、`expires_at` を付けた Application の時計 1 つだけにします（上の「期限は Application の時計で比較する」。Database の絶対時刻と比べると、時計のずれと、固定の時刻を使う Test が混ざります）。(2) 単調時計は戻らないので、待った分だけ**期限を短くする向きにしか働きません**。`created_at` と却下の Cooldown は、要求した時刻（呼び出し側の `now`）のままです。使用の時刻（`consumed_at`、履歴）は、判定した時刻です。Database の時計での期限は「後続の課題」のままです。
  Test: `tests/test_tools_postgres.py` の `ExpiryAfterLockWaitTest`（Task の行・承認の行・advisory lock を別の Transaction で握って呼び出しを止め、止まっている間に時計を動かし、離す。期限の 1 秒前は使えて、期限ちょうどは使えないこと、時計を注入しない実時間でも同じこと、開く側の印付け・件数・前の Run の取り消し）。
- **再び動く Task:** Retry / Restart（終了状態からの遷移）でも Listener は Open な承認を取り消します。終了時の取り消しが失敗して残った承認は、再開した Task では使えず（上の Run で）、新しい承認を求め直します。
- **範囲と限界:** 承認を要しない呼び出し（`AUTO` / `SCOPED_AUTO`）は Task の状態を見ません（終わった Task へ呼び出しを渡さないのは Orchestrator の責務です）。消費より前に決まった使用は有効です（消費の後に Task が終わっても、実行中の呼び出しは Task の `stop_now` / `cancel` が止めます。Executor の中の確認は Executor の責務です）。
  判断の理由は [Decision 0006](../../docs/decisions/0006-tool-broker-policy.md) の「9. Task の終了と承認」（Approved）。

**永続化（決定）: PostgreSQL に保存します。** 理由: 承認は Task が `waiting`（承認待ち）の間、Backend の再起動をまたいで残る必要があり（要件は Client の切断後も状態を保持）、
単回・期限・二重承認の保証は複数の Process が同じ行を更新できる Database でこそ成り立つためです。Audit Sink だけに書く案は、状態の読み出しも排他もできないため採りませんでした。

| Table | 内容 |
| --- | --- |
| `tool_approvals` | 承認の現在の状態（`pending` / `approved` / `rejected` / `consumed` / `revoked` / `expired`）、Task・**Task の Run（`task_attempt`、`task_retry_count`）**・Project・Agent・User、Tool、Level、`call_hash`、型つきの対象（正規化した Path / Host / Project）、**`summary`**、期限、`step_up_verified`、取り消した人と時刻 |
| `tool_approval_events` | Append-only の履歴（`requested`（`summary` つき）/ `approved` / `rejected` / `consumed` / `revoked` / `expired`） |

- 開いている承認は Exact な呼び出しごとに 1 つ（`call_hash` の Partial Unique Index）。Task、Project、Agent、User の ID に Foreign Key はありません（`task_events` と同じ方針）。
- 状態の変更と履歴の行は同じ Transaction です。期限切れは、変更しようとした時に `expired` へ移し、履歴に残します。
- `ApprovalListeners`（`ToolBroker(listeners=[...])`、`ApprovalService(listeners=[...])`）は、要求・承認・却下・消費・取り消しが保存された**後**に `ApprovalEvent`（ID と Enum だけ）を受け取ります。Task を `waiting` にする Orchestrator の接続点です。Listener の失敗や遅延は承認を失敗させず、例外の型名だけを Log に残します。
- テスト用に `InMemoryApprovalStore`（`approval_memory.py`。同じ規則。件数の上限つき。本番用ではありません）があります。両方の Store に同じ Test（`tests/tools_store_contract.py`）を実行します。

**DB が守る規則（Migration 0031）。** Application の Bug や侵害でも、次は Database が拒否します（Trigger はすべて `ENABLE ALWAYS`で、`session_replication_role = replica` でも効きます）。

- 承認の行は `pending` で作る（それ以外の INSERT を拒否）。
- 状態の変更は `pending` → `approved` / `rejected` / `revoked` / `expired` と `approved` → `consumed` / `revoked` / `expired` だけ。それぞれが自分の列だけを変える。Replay（`consumed` → `approved`）、`expires_at` の延長、`call_hash` / Tool / Level / 対象 / `summary` / Task の Run の書き換えは拒否。
- 承認・履歴の DELETE と TRUNCATE、履歴の UPDATE は拒否。
- CHECK 制約: 承認者は委任元 User だけで Agent ではない、強い承認は Step-up つき、`summary` は 1〜16 件の配列、`task_attempt` は 1 以上・`task_retry_count` は 0 以上（`tasks` の列と同じ）。

**Application の Role の権限（Migration の末尾の 1 ブロック）。** `PUBLIC` には何も与えません。`PAW_APP_DATABASE_ROLE` があれば、`tool_approvals` に SELECT・INSERT と**状態の列だけ**の UPDATE、履歴に SELECT・INSERT だけを与えます（DELETE・TRUNCATE・識別する列の UPDATE はなし）。
非 Superuser の Role で、書き換え、Replay、TRUNCATE、Trigger の無効化、他人を承認者にする UPDATE を試して拒否されることを Test しています（`tests/test_tools_postgres_roles.py`）。起動時の診断（`warn_about_loose_privileges`）は、承認の 2 つの Table への過剰な権限（Owner、全体の UPDATE、DELETE、TRUNCATE）と不足（INSERT できない）を警告します。

**承認と消費の Role の分離（実装しない。理由）。** Agent 側の Process が承認できない、を Database の権限で保証するには、承認する Process と Agent 側の Process が別の Role で接続する必要があります。
今の構成は Application の Role が 1 つで、その Role は合法な遷移（`pending` → `approved`）を実行できるため、**Application の Process が侵害されれば、その User の名前で承認を書ける**（承認者は委任元 User でなければならず、強い承認は `step_up_verified` を偽るだけ）ことは、Database では防げません。
分離には、承認の Endpoint 用の別 Role（と、その接続を持つ別 Process）が要ります。認証（PAW-022）と Step-up（PAW-023）の Endpoint ができる時に、承認の Endpoint だけが `UPDATE (status = 'approved' ...)` を実行できる構成（別 Role、または `SECURITY DEFINER` 関数）へ進めてください（Decision 0006 の後続の課題）。それまでは、**Agent の Runtime に Application の Role の DB 接続を渡さず、Broker だけを渡すことが前提条件です**（Decision 0006 で承認）。分離は PAW-022 / PAW-023 の受け入れ条件に入っています。

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
| `TaskActivityProvider.check(task_id, run)` | Deployment（`PostgresTaskActivity(database)`） | `FailClosedTaskActivity`（Task は不明 = 承認を要する呼び出しは拒否） |
| `PathResolver.resolve` | Deployment | `RealpathResolver`。`LexicalPathResolver` は Symlink のない環境の Test 用 |

`ToolRunner(execution_timeout=)` の既定は 600 秒（最大 24 時間。`None` は不可）。実行後の記録（Audit と Budget の Charge）は `finally` で `asyncio.shield` して書くため、Task が Cancel されても、実行後の処理が失敗しても残ります。

Adapter は Broker / Runner / Service の生成時に検査します（Async Method か、必要な引数の数か）。間違った Adapter は生成時に `TypeError` です。

### Executor の契約

Broker は呼び出しの**前**に判定します。次は、実際に実行する Executor（別 Issue）が守ることを前提にしています。

- **File**: `TaskScope.path_roots` の外へ Symlink をたどらずに開く（`openat2` の `RESOLVE_BENEATH`、`O_NOFOLLOW` など）。Broker の Symlink の確認は呼び出しの前で、確認後に変わりうる（TOCTOU）。
- **Network**: `ToolInvocation.arguments` の URL の Host に接続する。**Redirect は自動でたどらず**（たどるなら、移動先の Host を Task の Host と handle の使える Host で再確認する）、**DNS は接続時に引いた IP を確認する**（DNS Rebinding、Loopback・Private・Link-local への接続の拒否）。Broker は名前を字句で確認するだけで、名前が指す IP は見ません。
- **Credential**: handle を解決して付けるのは Executor だけ。handle が使える Host にだけ Credential を付ける。平文を結果や Log に出さない（出ても Runner が Redact する）。
- **Memory / Project の ACL**: Project や Memory を読む Tool は、Broker が確認した Project の Scope の中だけを、PAW-040 の ACL 条件（`readable_memory_versions`）つきで読む。Broker は Project の Scope を確認するが、Memory 単位の ACL は Tool の中の責務。
- **TaskContext は呼び出しごとの新しい Snapshot**: Orchestrator は Task の Scope・Grant・Project の状態を**呼び出しごとに**現在の値から作って渡す（Project の Archive、Task の Scope の変更、Grant の縮小が次の呼び出しから効く）。Broker は渡された Context をそのまま信頼します。`TaskContext.run` は Worker が開始された Task の `attempt` と `retry_count` です（Worker が Retry / Restart で置き換えられた後は、Task の現在の Run と違うので `task_superseded` になります。Worker の新しい Run では、新しい Context を作ってください）。

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
- **後続の課題:** 承認する Process と Agent 側の Process の Role の分離（上）、承認 UI と一覧の Endpoint（PAW-022）、`SECURITY DEFINER` 関数による遷移の限定、Database の時計での期限（Human は Application の時計だけを使う方針で承認した。変える場合は新しい Decision から `Supersedes` する）、`TaskService` への `revoke_on_task_end` の配線と `PostgresTaskActivity` の注入（PAW-034）（[DAG Agent Orchestrator](#dag-agent-orchestrator) の「組み立て」と Project 削除の Sweep が、この配線の形です）、終了時の取り消しに失敗した Task の再取り消し（今は `revoke_task` を呼び直す）、共通 Helper（`paw_backend.db_roles.grant_app_privileges`）による GRANT の置き換え。

## Memory / Conversation Schema

[PAW-040](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/34)（Revision `0040`）で実装した Schema です。
設計は [Memory Architecture](../../docs/MEMORY_ARCHITECTURE.md) と [要件](../../REQUIREMENTS.md) の Memory の節に従います。
Repository / Service は含みません（Shared Memory の管理だけは [PAW-046](#shared-memory-administration) の `memory/shared/` にあります）。

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

**User / Project / Repo の ID は Foreign Key なし。** `projects`（PAW-026、Revision `0026`）と `users`（PAW-021）の Table は、この Schema の Revision より後にできます（Repo の Table はまだありません: PAW-027）。この Schema からの外部キーは付けていません。
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

## Shared Memory Administration

[PAW-046](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/40)（Revision `0046`、`paw_backend/memory/shared/`）で実装しました。
Workspace 全体で共有する Memory（Shared Memory）の閲覧・作成・編集・削除・復元と、Shared Memory Candidate の提案・承認・却下、
System Security Policy を優先する Effective View を扱う `SharedMemoryService` です。**HTTP の Endpoint はまだありません**（API の Issue が呼びます）。
要件は [REQUIREMENTS.md](../../REQUIREMENTS.md) の「Shared Memory permissions」、
要件が決めていない選択は [Decision 0009](../../docs/decisions/0009-shared-memory-administration.md)（**Approved、2026-09-25 に Human が承認**）です。

Shared Memory は PAW-040 の Table（`memories` と、`scope = 'shared'` の `memory_versions`）に置きます。新しい Table は Candidate 用の `shared_memory_candidates` 1 つです。
Candidate を Version にしないのは、`shared` の Version は全 User が読める（`memory.acl`）のに対し、Candidate は承認前で、Private な Memory から来た内容を持つためです。

### 権限

すべてのメソッドは、最初の引数に操作する `actor`（`paw_backend.authz.Principal`、または委任元 User と Grant を持つ `AgentActor`）を取り、
`Authorizer` に 1 回問い合わせます。Audit は Authorizer が記録します（`shared_memory.read` は拒否だけ、その他は全件で、記録に失敗すると許可は拒否になります）。
変更が実際に行われたことは、Service が変更と同じ Transaction で完了の行として記録します（下の「変更の完了の記録」）。
Audit の `action` は Capability の値です。Shared Memory を変える操作は、それぞれ**専用の Capability** を使うので、Audit の履歴だけで操作を見分けられます（下の「Audit の Action」）。

| メソッド | Capability | できる人 |
| --- | --- | --- |
| `list_memories`、`get_memory`、`effective_view` | `shared_memory.read` | すべての Active User。`shared_memory.read` を Grant された Agent（全 Project 対象の Grant のみ。Project を限った Grant は拒否） |
| `internal_effective_view` | `shared_memory.read`（`effective_view` と同じ検査） | **Backend 内部（Context の組み立て）だけが呼びます。** User と Agent には出さず、HTTP の Endpoint にも出しません（下の「System Security Policy の優先」）。返す値に Policy の文言を含む唯一の経路です |
| `create_memory` | `shared_memory.create` | Owner、Admin（人間） |
| `edit_memory` | `shared_memory.edit` | Owner、Admin（人間） |
| `delete_memory` | `shared_memory.delete` | Owner、Admin（人間） |
| `restore_memory` | `shared_memory.restore` | Owner、Admin（人間） |
| `approve_candidate` | `shared_memory.candidate.approve` | Owner、Admin（人間） |
| `reject_candidate` | `shared_memory.candidate.reject` | Owner、Admin（人間） |
| `list_candidates`、`get_candidate`、`list_memories` / `get_memory` の `include_deleted=True` | `shared_memory.manage`（管理者だけが見られる情報の閲覧。何も変えない） | Owner、Admin（人間） |
| `propose_candidate` | `memory.use` | User が自分のために。Agent が委任元 User のために（Grant に `memory.use`） |

**自動昇格はしません。** 上の表の管理の操作（`shared_memory.manage` と、操作ごとの `shared_memory.create` などの Capability）は、人間の Owner / Admin の決定だけで行います。

1. Agent（`AgentActor`）と `system` role の Principal（Background Worker）は、Authorizer が何を答えても、常に `AutomaticPromotionRefusedError` で拒否します（Authorizer の判定は先に記録されます）。
   管理の Capability はすべて委任不可（`delegable=False`）でもあります。
2. Owner / Admin 以外の `Principal` は `SharedMemoryPermissionError` です。Authorizer が（Policy の変更などで）許可しても、Service が Owner / Admin でなければ拒否します。
3. Authorizer が `Decision` でない値を返したら拒否します（`invalid_decision`）。
4. Agent は Candidate を提案できますが、Candidate は `pending` のままです。承認は人間だけで、承認した人が Version の `actor_user_id` になります。
5. Shared Memory を作る・変える経路は、`SharedMemoryService` の上の表のメソッドだけです（`tests/test_shared_memory_contract.py` が公開メソッドの一覧を固定します）。

#### Audit の Action

削除・復元は `memory_versions` の `status` を変えるだけで、誰がいつ行ったかを行に残しません（[Decision 0009](../../docs/decisions/0009-shared-memory-administration.md) の 7）。履歴は Audit だけです。
Authorizer は `action` に Capability の値を書くので、全操作が 1 つの Capability（`shared_memory.manage`）だと、削除と復元、作成、編集、承認、却下を見分けられません。
そこで、変更する 6 つの操作にそれぞれ Capability を追加しました（`authz/capabilities.py`。すべて `Scope.SYSTEM`、委任不可、Audit Mode `REQUIRED`、Owner / Admin だけ）。

| `action` | 操作 | `resource_kind`、`resource_id` |
| --- | --- | --- |
| `shared_memory.create` | `create_memory` | `shared_memory`、なし |
| `shared_memory.edit` | `edit_memory` | `shared_memory`、Memory の ID |
| `shared_memory.delete` | `delete_memory` | `shared_memory`、Memory の ID |
| `shared_memory.restore` | `restore_memory` | `shared_memory`、Memory の ID |
| `shared_memory.candidate.approve` | `approve_candidate` | `shared_memory_candidate`、Candidate の ID |
| `shared_memory.candidate.reject` | `reject_candidate` | `shared_memory_candidate`、Candidate の ID |

- Audit の行は `actor_id`（操作した User）、`actor_role`、`decision`、時刻を持つので、「誰がいつ何を試みたか」は行から分かります。拒否された試みも、試みた操作の `action` で残ります。実際に変更が起きたかは、下の完了の行が示します。
- この行は変更の**試み**（判定）です。判定の Audit そのものなので、既存の性質（既定は拒否、Audit の行は変更が見える前に書かれる、Audit を書けなければ許可を拒否に変える）はそのままです。
  変更の前に書かれるので、この行だけでは変更が**起きたか**は分かりません（対象がない、状態が違う、Lock を待ち切れない、更新が失敗する場合も同じ行が残ります）。起きたことは次の完了の行が示します。
- `shared_memory.manage` は、管理者だけが見られる情報の閲覧（削除済みの Memory、Candidate）に残しました。何も変えないので、履歴で見分ける必要が小さく、`resource_kind` と `resource_id` の有無（一覧か 1 件か）で区別できます。
- 限界: 復元・削除の理由（`reason`）は残りません（Audit は Content や自由な文を持たない）。Candidate の承認・却下の理由は Candidate の行にあります。

#### 変更の完了の記録

変更する 6 つの操作は、変更と**同じ Database の Transaction の中**（最後の書き込み）で、`audit_events` へ完了の行を 1 本追加します（`memory/shared/audit.py`、[Decision 0009](../../docs/decisions/0009-shared-memory-administration.md) の 13）。
試みの行と 2 本で 1 回の変更を表し、次の値を持ちます（ID、Enum、固定の `reason` だけです。Memory の内容は含みません）。

| 列 | 試みの行（Authorizer） | 完了の行（Service） |
| --- | --- | --- |
| `action` | 操作の Capability | 同じ |
| `decision`、`reason` | `allow`、`granted_by_system_role` など（拒否は `deny`） | `allow`、`completed` |
| `actor_id`、`actor_role` | 操作した User、Role | 同じ |
| `resource_kind`、`resource_id` | 対象（`create_memory` は ID なし） | 変えたもの。`create_memory` は作った Memory の ID。承認・却下は Candidate の ID |
| `occurred_at` | Authorizer の時計（判定の時刻） | Service の時計（遷移の時刻。新しい Version の `created_at` と同じ読み） |
| `correlation_id` | 呼び出しごとに Service が作った ID | 同じ ID（2 本の行を結ぶ） |

- **完了の行がある ⇔ 変更が Commit された**。Rollback、失敗した Statement、失敗した Commit は行も戻します。完了の行を書けなければ変更も戻ります（fail-closed。Test は行の INSERT と Commit を失敗させて確認）。
- 試みの後で失敗した呼び出し（対象がない、状態が違う、版が古い、Lock を待ち切れない、更新が失敗する）は、**完了のない試み**として残ります（`correlation_id` が同じ完了の行がない `allow` の行）。何も変えなかった呼び出し（内容が同じ編集）にも完了の行はありません。
  拒否された試み、Audit を書けずに拒否された試みは、これまでどおりで、完了しません。
- 「誰がいつ削除・復元したか」は、`resource_id` と `reason = 'completed'` で絞った行の `action`、`actor_id`、`occurred_at` です。
- 権限は増えません。Application の Role が持つ `audit_events` の INSERT / SELECT（Revision `0025`）で書きます。UPDATE、DELETE、TRUNCATE は Trigger と権限が拒否したままです（`tests/test_shared_memory_grants.py` が確認）。
- 限界:
  - 完了の行は Authorizer の `AuditSink` を通らず、Service が `audit_events` に直接書きます（Sink は別の Transaction で書くため、変更と記録が別れうるからです）。Sink を差し替えた配備では、試みと完了が別の場所に分かれます（現在の Sink は `PostgresAuditSink` だけです）。
  - `decision` の CHECK（`allow` / `deny`）があるため、完了の行も `allow` です。`action` と `decision` だけで数えると 1 回の操作が 2 行になるので、集計は `reason` で分けてください。
  - **失敗そのものの行はありません**。失敗は「完了のない試み」から読みます。失敗の行は Rollback の後に Transaction の外で書くことになり、行がないことが何も証明しないためです。実行中の試みと、Process が落ちた試みも、完了がないので区別できません。
  - `occurred_at` は Service の時計の読みで、Lock を待つ前に読みます。Database が付ける `recorded_at`（Transaction の開始時刻）が、行が確定した時刻に近い値です。
  - Application の Role は `audit_events` に INSERT できるので、Application 自体が偽の行を書けることは、他の Audit の行と同じです（`0025` の限界）。

呼び出しの順序は、(1) 引数の検証（`InvalidSharedMemoryInputError`、`actor` の型を含む）、(2) 認可（拒否は Database に触れる前）、(3) Database の使用、です。
削除済みの Memory を含める読み取りも、認可の前に存在を知らせないため、Owner / Admin 以外には「見つからない」ではなく「権限がない」を返します。

### Shared Memory の状態と Version

Memory は、**現在の Version（`version_number` が最大の Version）** で見ます。現在の Version が `scope = 'shared'` で、`status` が `active`（状態 `ACTIVE`）または
`deprecated`（状態 `DELETED`）のものだけが Shared Memory です。それ以外（他の Scope、`history` / `superseded` の Version）は、この Service では「見つからない」です。

- **作成**: Version 1（`active`、`confirmation_state = 'confirmed'`、`freshness_policy = 'permanent'`、`actor_type = 'user'`）。
- **編集**: 上書きしません。現在の Version を `superseded` にし、新しい Version `n + 1`（`active`）を書き、新しい側から古い側への `supersedes` 関係（`reason` は変わった項目の名前をアルファベット順に `", "` でつないだもの）を追加します。
  `expected_version` が現在の番号と違えば `SharedMemoryVersionConflictError`（Optimistic Lock）。何も変わらない編集は、何も書かずに現在の Memory を返します。削除済みの Memory は編集できません（先に復元）。
- **削除**: 現在の Version の `status` を `deprecated` にします（何も消しません）。**復元**は `active` に戻します。Version は増えません。誰がいつ削除・復元したかは、Audit の試みの行と完了の行にだけ残ります（上の「変更の完了の記録」）。
  削除済みの Memory は、一般 User の一覧・取得には出ません（「見つからない」）。
- 編集できる項目は `title`（200 文字まで）、`content`（20,000 文字まで）、`memory_type`（`[a-z][a-z0-9_]{0,63}`）、`importance`（0〜100）、`policy_subjects`（20 個まで）です。`reason`（500 文字まで）は Version の `change_reason` になります。
- 一覧は古い順（`memories.created_at`、同時刻は `id`）で、`limit`（1〜200、既定 50）と `offset`（0〜100000）で区切ります。

### Candidate

Candidate は「Shared Memory にしたい内容」で、提案者（User、Agent の場合は委任元 User と Agent）、元の Memory の Scope と任意の Version ID（提案者の申告で、Service は確認しません）、状態を持ちます。
**Owner / Admin だけが見られます**（`list_candidates`、`get_candidate`）。

- 状態機械は `pending` → `approved` / `rejected` だけです（`lifecycle.next_candidate_state`）。決定済みの Candidate への操作は `SharedMemoryStateError` です。
- **承認**は、1 つの Transaction で、Candidate を `FOR UPDATE` でロックし、Candidate の内容で新しい Memory の Version 1 を書き、`memory_sources` に
  `source_type = 'user_confirmation'`、`source_ref = 'shared_memory_candidate:<candidate id>'` の 1 行を書き、Candidate を `approved`（決定者、時刻、任意の理由、新しい Memory の ID）にします。
- **却下**は Candidate を `rejected` にするだけです。
- 1 人が持てる `pending` の Candidate は 50 件までです（`CandidateLimitError`）。Agent の提案は委任元 User に数えます。数える処理は User ごとの Advisory Lock で直列化するので、同時に提案しても超えません。

### System Security Policy の優先（Effective View）

Shared Memory は System Security Policy を上書きできません。Backend は文章の矛盾を判定できないので、衝突は**宣言**で決めます。

- Shared Memory は `policy_subjects`（`merge.permission` のような、`.` 区切りで最大 5 階層の Key）を持てます。Owner / Admin が作成・編集・承認のときに設定します。
- Policy の項目 `SystemPolicyItem` は `policy_id`、`subject`、`statement`（不透明な本文）です。項目は `SystemPolicySource.items()` から、呼び出しごとに読みます（`StaticPolicySource` は固定のリスト用）。Policy の内容はこの Issue では決めません。
- Memory の `policy_subjects` のどれかが Policy の `subject` と等しい、またはその下位なら、その Memory は上書きされます（`merge` は `merge` と `merge.permission` を覆い、`mergeable` や `merge_x` は覆いません。Policy が下位のときも覆いません）。
- `effective_view` は、上書きされた Memory を `memories` に含めず、`overridden` に ID と勝った Policy の ID だけを返します（内容は返しません）。
  **Policy の文言（`statement`）は、User にも Agent にも返しません。** 返す `EffectiveSharedMemory` には `applied_policies` の欄がなく、`repr` や `dataclasses.asdict` にも文言は現れません（[Decision 0009](../../docs/decisions/0009-shared-memory-administration.md) の 10）。
- 上書きした Policy の項目（文言を含む。`policy_id` 順）は、**Backend 内部の Context の組み立て**だけが `internal_effective_view` で受け取ります（`InternalEffectiveView.applied_policies`）。
  引数で切り替える方式ではなく別のメソッドにしたのは、`effective_view` のどの引数でも文言を出せないようにするためです。`repr` には `applied_policies` を含めません（Log に出さないため）。
  API の Issue が Service を HTTP に出すときは、`effective_view` だけを出します（`internal_effective_view` を出さないことを、その Issue の Review で確認します）。
- Policy を読めないとき（Source の失敗、遅延、契約違反）は、Memory を 1 件も返さずに `PolicySourceError` で失敗します（fail closed）。エラーと Log に Source の例外の文言は出ません。
- `list_memories` と `get_memory` は保存されている Memory をそのまま返します（管理用）。モデルに渡す内容は、Backend 内部では `internal_effective_view`、User に見せる画面や API では `effective_view` で作ります。
- **限界**: `policy_subjects` を宣言していない Memory は、この規則では上書きされません（意味の矛盾は Owner / Admin の承認と、PAW-042 の矛盾検出で補います）。Shared Memory は権限を与えないので、Tool や Merge の可否は Memory と無関係に Backend が強制します。

### Database と権限

Migration `0046` は `shared_memory_candidates` を作ります（`down_revision` は `0050`）。
Application の Role には、[上の規則](#migration-は-application-の-role-に権限を与えるcontributor-向けの規則)のとおり、Service が実行する最小の権限だけを与えます。

| Table | 与える権限 | 理由 |
| --- | --- | --- |
| `shared_memory_candidates` | SELECT、INSERT、UPDATE（`state`、`decided_by`、`decided_at`、`decision_reason`、`memory_id` のみ） | 提案（INSERT）と 1 回の決定（UPDATE）。`SELECT ... FOR UPDATE` は UPDATE 権限が要り、この 5 列で足りる。提案の内容、提案者、出典、作成時刻は書き換えられず、DELETE も与えない |
| `memories`、`memory_versions`、`memory_relations`、`memory_sources` | PAW-040 のまま | Service は INSERT と、`memory_versions.status` の UPDATE だけを使う。`memories` の DELETE は使わない（削除は `deprecated`。物理削除の経路は別 Issue） |
| `audit_events` | Revision `0025` のまま（SELECT、INSERT） | Authorizer の試みの行と、変更ごとの完了の行（変更と同じ Transaction の INSERT 1 本）。UPDATE、DELETE、TRUNCATE は与えず、Trigger も拒否する |

`shared_memory_candidates` の CHECK 制約は、状態の値、文字数の上限（Service の上限と同じ数）、`pending` は決定を持たないこと、決定済みは決定者と時刻を持つこと、`memory_id` は `approved` だけが持つことを強制します。
`memory_id`、人・Agent・出典の ID は、外部キーのない素の UUID です（PAW-040 の Test が、Memory の層の Table と他の Table を外部キーでつなぐことを禁じています）。DB は存在を確認せず、Service は承認で自分が書いた Memory の ID だけを入れます。

`tests/test_shared_memory_grants.py` は、Service の Test を非 Superuser の Role で実行し、権限が過不足ないことを検査します。

### 同時実行

- 1 つの Memory に書く操作（編集・削除・復元）は、まず `memory_lock_key(memory_id)` の Advisory Lock（Transaction 単位）を取り、新しい Statement で現在の Version を読みます。
  そのため同時に編集する 5 人のうち、勝つのは 1 人で、残りは `SharedMemoryVersionConflictError` です。削除と編集が競合しても、`active` の Version が残ることはありません。
- Candidate の決定は Candidate の行を `FOR UPDATE` でロックし、`WHERE state = 'pending'` の条件付き UPDATE で決めるので、同時に 2 回承認しても Memory は 1 つだけです。
- 書き込みの Transaction は `SET LOCAL lock_timeout`（`lock_timeout_ms`、既定 3000）で始まり、超えると `SharedMemoryBusyError`（何も変わらない）です。読み取りはロックを待ちません。
- `version_number` の Unique 制約と `active` の Partial Unique Index が、最後の防波堤です。

### Rule 関数と Service の分担

判断の規則（Candidate の状態機械、Version の扱い、優先の解決）は、純粋な関数として `lifecycle.py` と `precedence.py` に分けています。
Service（`service.py`）は認可、検証、Transaction、Lock、SQL を持ち、判断の箇所でこれらの関数を呼びます。
Service は、Rule 関数の戻り値を、契約に照らして確認してから書き込みます（`RulesContractError`）。
たとえば、承認では Candidate と違う内容の Draft や期待と違う状態を、編集では並び順の違う変更項目や、版の違う Plan を拒否し、状態の更新は「更新前の状態が想定どおり」の場合だけ行います。

| Module | 関数 |
| --- | --- |
| `lifecycle.py` | `next_candidate_state`、`check_deletable`、`check_restorable`、`apply_changes`、`changed_fields`、`plan_edit`、`draft_from_candidate` |
| `precedence.py` | `subject_covers`、`overriding_policy_ids`、`resolve_effective_view` |

**実装の由来:** この 2 つの Module の関数本体（10 個）は、ローカルの Qwen3-Coder-30B-A3B が、契約（Docstring と手計算した例）と Test だけを仕様として実装しました（1 回の実行、約 190 回の Tool 呼び出し）。Claude が書いた契約と Test（507 件）を、実 PostgreSQL の全体の CI（2,885 件）で通ることを確認しました。
Review で、`resolve_effective_view` の仕様の Docstring が Model によって書き換えられていたため、元の Docstring に戻しています（本体の振る舞いは変えていません）。空の作業 File（`APPROVED`、`REJECTED`）も残していたので削除しました。
Model の実装は、Test を通すことに必要な範囲で素直な書き方です。Model が書いた部分と、Claude が書いた部分（Model、Service、Validation、Migration、Test、Decision 0009）の境界は、上の表のとおりです。

### 制限と未確認の点

- HTTP の Endpoint、通知、Web の画面はありません。提案者が自分の Candidate の状態を見る方法もありません（Decision 0009 の 3）。
- Policy の実体（保存、Admin による変更、強制）はこの Issue の範囲外です。`SystemPolicySource` の実装は、Policy を持つ Issue が用意します。
- `policy_subjects` の宣言が前提です（上の限界）。PAW-042 の矛盾検出が宣言を補う設計は未実装です。
- Shared Memory の鮮度（再確認の期限など）は `permanent` 固定です（PAW-042 で決めます）。
- Embedding と Markdown Projection（PAW-043 / PAW-045）は、この Service を通りません。Shared Memory を読む Retrieval は、`readable_memory_versions` を使い、モデルに渡す前に Policy の優先を適用する必要があります（この Service の `internal_effective_view`、または `precedence.resolve_effective_view`。どちらも Policy の文言を含む `InternalEffectiveView` を返すので、User や Agent へ返すときは `.public()` か `effective_view` を使います）。
- 上限の数値（50 件、20 個、20,000 文字など）は実測に基づかない仮の値で、`memory.shared.limits` にあります。
- Migration `0046` の `down_revision` は `0050` です（鎖は `0001 → 0025 → 0032 → 0040 → 0021 → 0033 → 0031 → 0050 → 0046`）。Revision ID は Issue 番号で、鎖の順序ではありません。統合時に Orchestrator が並びを確認します。
- 一覧の同時刻の並び（`id` の副次キー）は決定的にするためのもので、Test は「同時刻の 12 件が `id` 順」だけを確認します。Query Plan によっては副次キーがなくても同じ順になるため、その Test だけでは副次キーの削除を検出できません（変異 Test で確認済み）。

### 承認された判断

[Decision 0009](../../docs/decisions/0009-shared-memory-administration.md)（Approved、2026-09-25 に Human が承認）の次の点は、承認された方針です。

1. Candidate を別 Table にすること、Agent の提案を許すこと、提案者に Candidate を見せない（結果を返す仕組みは今は作らない）こと、`pending` 50 件の上限（暫定値として承認）。
2. 削除・復元を `status` の切り替えにし、Version を増やさないこと（誰が削除したかは Audit Event だけ）。その Audit の `action` を操作ごとに分けるために Capability を 6 つ追加したこと（閲覧は `shared_memory.manage` のまま。Decision 0009 の 12）と、変更の完了を Audit の行として同じ Transaction で書くこと（同 13。Service が `audit_events` に直接書く）。
3. `policy_subjects` の宣言で Policy との衝突を決めること（宣言がなければ上書きされない。この限界を受け入れた）。
4. `effective_view` は、上書きした Policy の `statement`（文言）を User にも Agent にも返さないこと。文言は Backend 内部の `internal_effective_view` だけが受け取ります（Global System Prompt との関係は、公開する場合に別途決める。Decision 0009 の 10）。
5. 承認した Shared Memory の鮮度を `permanent` にすること（PAW-042 で見直す）。

### Test

`tests/test_shared_memory_*.py`。Rule 関数は Database なしの Test（`..._rules_*.py`）、Service は実 PostgreSQL の Test（`PAW_TEST_DATABASE_URL` がないと Skip）、
Migration（上げ下げ、Model との差分、制約）、権限（非 Superuser の Role で Service の Test を実行）、自動昇格の拒否（`..._promotion_refused.py`）、
操作ごとの Audit の `action`（`..._audit_actions.py`。実 `audit_events` の行を読み、非 Superuser の Role でも実行）、
変更の完了の記録（`..._completion.py`。成功は完了の行が続くこと、失敗・Lock の待ち切れ・更新の失敗・Commit の失敗は完了の行がなく変更もないこと、完了の行を書けなければ変更も戻ること。非 Superuser の Role でも実行）があります。

## Research Scratch Store

[PAW-050](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/42)（Revision `0050`）で実装しました。
`paw_backend/research/scratch/` は、Web / Research の調査結果を **`created_at + 24 時間`** だけ置く一時保存です。
**Long-term Memory とは別の Table** で、どちらの向きにも Foreign Key はありません。Long-term Memory へ自動では保存しません（[要件](../../REQUIREMENTS.md)の「Memory promotion」）。
**HTTP の Endpoint はありません。** `ScratchStore` は `TaskService` と同じく認可を行わず、権限の確認は呼び出す側（API 層）の仕事です。

| ファイル | 内容 |
| --- | --- |
| `models.py` | `research_scratch_items`、`research_scratch_leases` の Model |
| `limits.py` | TTL と各種の上限（Test が DB の CHECK 制約との一致を検証） |
| `records.py`、`errors.py` | 返す値（`ScratchItem`、`Lease`、`PurgeResult`）と型付きの Error |
| `validation.py` | 引数の検証（DB を使わない純粋関数） |
| `service.py` | `ScratchStore`（Clock は注入） |
| `janitor.py` | `ScratchJanitor`: `purge_expired` を定期的に呼ぶ Loop（`sleep` は注入）。`create_app` の Lifespan が起動する |

### Table

| Table | 内容 |
| --- | --- |
| `research_scratch_items` | 調査結果 1 件。`query`、`title`、`summary`、`content`（`summary` か `content` のどちらかは必須）、`source_metadata`（JSON Object。URL、種別、`fetched_at`、`published_at`、抽出した Claim など。16 KiB まで）、`created_at`、`expires_at`、`pinned`（Pin）、`saved`（User の明示保存）、`promotion_state`（`none` / `pending` / `promoted` / `rejected`）、`promotion_requested_at` |
| `research_scratch_leases` | 「今使っている」印。`(item_id, holder_id)` が Key。`holder_id` は Task や Worker の実行の UUID |

- **TTL**: `expires_at = created_at + interval '24 hours'` を CHECK 制約で強制します（Generated Column は `timestamptz + interval` が immutable ではないため使えません）。`expires_at` は変更しません。延期は TTL の延長ではなく削除の保留です。
- **Project / Task の関係**: `project_id` も `task_id` も**素の UUID**で、Foreign Key はありません（`projects` は Revision `0026` で、外部キーは後の Revision で付けられます。`task_id` の理由は [Decision 0013](../../docs/decisions/0013-research-scratch-task-relation.md)（2026-09-25 に承認）にあります）。Task を削除しても、削除が調査結果に止められることも、Pin 済みなどの調査結果が Task と一緒に消えることもなく、Item は `task_id` を保ちます（`list_items(project_id, task_id=...)` で読めます）。期限切れの削除は Task と無関係に働きます。`add` は、Task の行を `FOR KEY SHARE` で Lock して、Task が存在し、その `project_id` が同じであることを確認します（存在しない Task と他の Project の Task は区別しません）。**DB は `task_id` の存在を検査しません**（この確認が唯一で、削除された Task を指す `task_id` は残ります）。
- 全ての Method は `project_id` を受け取り、その Project の中だけで Item を探します。他の Project の ID は「存在しない」と同じ扱いです。

### 削除の延期

次のどれかに当てはまる Item は削除を延期します（**exempt**）。

- **Pin 済み**（`pinned`）。一時的に残す印です。
- **User が明示保存**（`saved`）。要件は「Pin 済み」と「User が明示保存」を別の延期理由として挙げているため、Pin とは**別の独立した印**にしています（[Decision 0013](../../docs/decisions/0013-research-scratch-task-relation.md)（2026-09-25 に承認）の「Pin と明示保存」）。
- **Memory 昇格の確認中**（`promotion_state = 'pending'`）。
- **使用中**（`expires_at > now` の Lease が 1 つ以上ある）。

Item は `now < expires_at` または exempt のとき **見える**（visible）ことにします。見えない Item は、`purge_expired` がまだ行を消していなくても、全ての Method で「存在しない」（`ScratchItemNotFoundError`）です。
挙動が Janitor の実行時刻に左右されないようにするためです。期限切れで exempt でない Item は、Pin も保存も Lease も昇格要求もできません（復活させない）。
最後の exempt 理由が終わる（Unpin、Unsave、Lease の終了・Release、昇格の解決）と、期限切れの Item は見えなくなり、次の `purge_expired` が削除します。延期用の別の状態や Queue はありません。

- **Pin と保存は独立。** `pin` / `unpin` は `pinned` の 1 列だけ、`save` / `unsave` は `saved` の 1 列だけを、Item の行を Lock した後の 1 つの `UPDATE` で変えます（もう一方の印は読み直した値のまま残り、同時の変更を失いません）。`unpin` は保存を消さず、`unsave` は Pin を消しません。どちらか一方でも立っていれば Item は残り、**両方**が下りて TTL が過ぎたときに限り、次の `purge_expired` が削除します。TTL は延びません。どちらも冪等で、探し方は `pin` と同じです（存在しない・他の Project・見えない Item は `ScratchItemNotFoundError`）。Test は `test_scratch_saved.py`（Pin + 保存の後の `unpin` で Purge を越えて残る、保存だけ、Pin だけ、両方を下ろすと TTL の後に削除される、`unpin` と `unsave` の同時実行、Lock を待つ間に他方の印が変わっても消えない）と `test_scratch_purge.py` です。
- **Lease**: `acquire_use` が `lease_seconds`（既定 300、1〜3600）の Lease を作ります。同じ Holder の再取得は更新（短くもできる）、Holder ごとに別の行、同時に有効な Lease は 1 Item に 16 まで（`ScratchLeaseLimitError`）。
  Lease は `[leased_at, expires_at)` の間だけ有効で、更新も Release もされなければ最長 1 時間（CHECK 制約）で終わります。落ちた Worker が Item を固定し続けることはありません。期限切れの Lease は次の `acquire_use` が消します。`release_use` は何度呼んでも、Item が既に消えていても Error になりません。
- **昇格**: `request_promotion` で `pending`（`none` と `rejected` から。`pending` は何もしない。`promoted` は `ScratchStateError`）。`resolve_promotion(outcome)` で `promoted` / `rejected`（`pending` のときだけ。同じ結果の再実行は何もしない。それ以外は `ScratchStateError`）。Store は Memory Candidate を作りません。それは昇格の Flow の仕事です。

### Purge と同時実行

`purge_expired(now=None, batch_size=500)` は全 Project を対象に、`expires_at <= now` で exempt でない行を最大 `batch_size`（1〜5000）件、1 Transaction で削除します。`PurgeResult(purged, deferred, has_more)` の意味は次のとおりです。

- `purged`: 削除した行数。`deferred`: 期限切れで exempt のために残っている行数（複数の理由があっても 1）。`has_more`: バッチに入りきらなかった削除対象が残っている（呼び出し側は繰り返す）。
- 古い exempt の行がバッチを埋めて削除対象を飢えさせないよう、exempt の行は選択の段階で除外します。

同時実行の規則（`service.py` の冒頭にも書いています）。

1. Item / Lease を変える操作は、先に Item の行を `SELECT ... FOR UPDATE` で Lock します（待ちます）。Lock 待ちは `lock_timeout_ms`（既定 5000）で `ScratchBusyError` になります。読み取り（`get`、`list_items`）は Lock も待ちもしません。
2. `purge_expired` は待たずに `FOR UPDATE ... SKIP LOCKED` で候補を Lock し、**別の Statement** で exempt を再確認して削除します。Purge の Snapshot の後に Commit された Lease / Pin も見えるため、Purge より前に取得された Item は消えません。Lock 中の行は飛ばされ（数にも入らない）、次の呼び出しが扱います。Purge が先に Lock した場合、待っていた操作は「存在しない」になります。
3. 2 つの Purge が同時に動いても、各行は 1 回だけ削除されます。

### 呼び出し側の認可（提案）

Endpoint は次の Issue の仕事です。次の対応を提案します（未強制）。読み取り（`get`、`list_items`）は `project.read`。`add`、`acquire_use`、`release_use`、`pin`、`unpin` は `project.task.run`。`pin`、`unpin` は Agent へ委任できます。
**`save`、`unsave` は User 本人だけができる操作で、Agent へ委任できません**（[Decision 0013](../../docs/decisions/0013-research-scratch-task-relation.md)で 2026-09-25 に承認。要件の「User が明示保存」は人の意思表示であり、Agent が調査結果を TTL から免れさせられないようにするためです）。`ScratchStore` 自体は認可をしないので、この制限は呼び出し側（API 層）が強制します。`save`、`unsave` を、Agent の権限（委任元 User と `AgentGrant` の積集合）では呼べない経路にしてください。専用の Capability を新設するかと、その id はここでは決めていません。新設するときは、委任不可（`CapabilityInfo.delegable=False`）にしてください（[Decision 0004](../../docs/decisions/0004-rbac-capability-and-audit-policy.md) は、Capability を追加するときに委任の可否を明示することを求めます）。
`request_promotion` は `project.memory.use`、`resolve_promotion` は `project.memory.manage`（Agent へ委任できない: 調査結果を Agent の判断だけで Long-term Memory へ送らないため）。`purge_expired` は Backend 自身の Janitor だけ（User も Agent も呼べない）。

### 上限と入力の検証

`query` 1000 文字、`title` 500、`summary` 8000、`content` 100000（Unicode の Code Point）、`source_metadata` は Compact な UTF-8 JSON で 16384 Byte・深さ 6・Key 128 文字。
整数は ±(2^53 - 1)、浮動小数点は有限で 0 か 1e-6 以上 1e15 未満（PostgreSQL は数値を 10 進で持つため、極端な指数で Size の上限が意味を失うのを防ぐ）。
型は変換しません（`"false"` は真偽値でなく、`bool` は `int` でなく、UUID の文字列は UUID でない）。NUL と UTF-8 にできない文字は拒否します。
Error の Message は固定文字列（Field 名と理由の語彙）で、入力の内容・ID・DB の Message を含みません。DB の Error（接続断など）は加工せず伝わりますが、SQL の引数を含みうるため、呼び出し側は `str(error)` を User へ見せないでください。

### 実装の経緯

`validation.py` と `service.py` は、当初 Local Model（Qwen3-Coder-30B-A3B）に実装させる計画でした。仕様（Contract、Model、Migration、Test）は Claude が先に書き、Local Model の合格基準は Test だけでした。
Local Model の試行は収束せず、構文エラーを含む部分的なコードしか作れなかったため、**この 2 つの Module は Claude の参照実装**です（人間の判断が必要な点は下記）。
Test は参照実装で成り立つことを確認しながら書いたものです。Local Model の試行の前に確定しており、その後の変更は、負荷の高い環境で 20 件の同時 `acquire_use` が Lock Timeout に達しないよう、その 1 件の Test の `lock_timeout_ms` を伸ばしただけです。

### Janitor（期限切れの削除）

`ScratchStore` は期限切れの Item を全ての Method で見えなくしますが、行と調査内容を消すのは `purge_expired` だけです。呼ぶ処理がなければ 24 時間の TTL は DB で実施されません。
`janitor.py` の `ScratchJanitor` がその Loop です。`create_app` の Lifespan が、**DB が設定されていて `PAW_SCRATCH_PURGE_INTERVAL_SECONDS` が 0 より大きいとき**だけ、Heartbeat や権限の Diagnostic と同じ場所で起動します。

- **間隔。** `PAW_SCRATCH_PURGE_INTERVAL_SECONDS`（既定 3600、`0` で止める。それ以外は 60〜86400 で、1〜59 と範囲外は起動時の設定 Error）。起動の 30 秒後（間隔が短ければその間隔）に最初の Tick、その後は間隔ごとです。すぐには実行しないので、起動処理や Diagnostic と競合せず、すぐ止められた Backend は Purge の接続を開きません。
- **1 回の Tick。** `purge_expired` を 500 件の Batch で、これ以上消せる行がない（`has_more` が偽）まで呼びます。1 Tick は最大 100 Batch（5 万行）で、上限に達して残りがあれば、次の Tick は 5 秒後です。Batch は 1 Transaction なので、途中で失敗しても、済んだ Batch の削除は残ります。
- **削除の条件は Store のまま。** 期限は Store の Clock が決めます。Pin 済み・保存済み・使用中（Lease）・昇格確認中の Item は消えず、その事情が終わった後の最初の Tick で消えます。TTL は延びません。Long-term Memory には触れません。
- **失敗。** Tick が失敗しても Loop は止まりません。WARNING に**例外の型名だけ**を出し（Message、SQL、調査内容は出さず、Traceback も付けません）、30 秒から倍にして間隔まで待ち、成功で元に戻ります。削除できた件数は INFO に出します。
- **停止。** Lifespan の終了で Cancel し、`PAW_SHUTDOWN_TIMEOUT_SECONDS` の範囲で待ってから `Database.dispose()` を呼びます（Diagnostic と同じ）。Purge が Query の途中でも、PostgreSQL が応答しなくても、Janitor はその場で終わります（下記「止まらない PostgreSQL」）。待ちを打ち切って Task を見捨てるのは、Cancel を無視する Task だけで、その数を WARNING に出します。
- **複数 Process。** それぞれが Janitor を持ってかまいません。`purge_expired` は `SKIP LOCKED` なので、互いに待たず、同じ行を二重に消しません（`test_two_janitors_at_once_delete_every_row_exactly_once`）。
- **権限。** `PAW_APP_DATABASE_ROLE` の Role のままで動きます。Migration `0050` の SELECT / DELETE だけを使います（`test_scratch_grants.py` が Janitor の Test もその Role で実行します）。
- **Test。** `test_scratch_janitor.py`（Fake の Store と `sleep`）、`test_scratch_janitor_settings.py`、`test_scratch_janitor_lifespan.py`（起動する・しない、Cancel が `dispose` より先、待ちが有界）、`test_scratch_janitor_postgres.py`（実際の PostgreSQL で期限切れの行が消え、Pin・使用中・昇格確認中は残る。Application 全体でも確認。`StalledPurgeTest` は PostgreSQL の前に置いた Proxy を Purge の途中で止めて、Cancel と `dispose()` がその場で Purge を止めることを確かめます）、`test_scratch_janitor_stall.py`（応答しない PostgreSQL の Fake で、Purge の途中の Janitor が Lifespan の終了で本当に終わること、Process も遅れずに終わること）、`test_database_run_abortable.py`（`Database.run_abortable` の Commit・Rollback・同時数・Cancel）、`test_database_slot_wait.py`（空き待ちの Cancel と `dispose()`。応答しない PostgreSQL の Fake を、`database_pool_size=1` の唯一の空きを持ったまま止まる呼び出しにして確かめます）。

**止まらない PostgreSQL。** PostgreSQL が接続を受け付けたまま応答しなくなると、通常の Pool の Query は Cancel でも止まりません（psycopg がサーバに Cancel を頼んで、答えを待ちます。約 10 秒、libpq が 17 未満なら Interpreter が終了時に待つ Thread から）。Lifespan の待ちは `PAW_SHUTDOWN_TIMEOUT_SECONDS` で打ち切れても、Task と接続は残り、Process の終了が遅れます（独立 Review の指摘）。そこで `purge_expired` は、Transaction 全体を `Database.run_abortable` で実行します。

- Pool を使わない**専用の接続**で、Purge 専用の Task の中で動きます。Janitor はその Task を待つだけです。
- Janitor の Cancel と `Database.dispose()` は、その接続の **Socket を閉じます**（起動時の Diagnostic の `fetch_abortable` と同じ仕組み）。実行中の文はすぐ失敗し、サーバー側の Transaction は接続が切れたときに Rollback されます。接続を開く途中や、SQLAlchemy が新しい接続で最初に実行する Query の途中でも同じです（接続は作った時点で登録します）。
- `SKIP LOCKED`、行の Lock、2 つ目の文での再確認、`lock_timeout_ms` と `ScratchBusyError`、`purge_probe` は変わりません。Transaction が途中で止められても、消す文が Commit されるのは全体が成功したときだけで、次の Tick が同じ仕事をやり直します（冪等）。`COMMIT` が既にサーバーへ届いていた場合は、適用されていることがあります。
- Test は、応答しない PostgreSQL の Fake（`HangingPostgres`）で、Lifespan の終了が待ちの上限を使い切らず Janitor が終わること、Process が遅れず終わること（libpq 17 と、17 未満の Thread による Cancel の両方）を、PostgreSQL の前に置いた Proxy で、Purge の Transaction の途中（行を選んで Lock した後）で止めた場合を確かめます。実装を Pool 経由の Transaction に戻すと、どちらも失敗します。

限界: 接続は 1 Batch ごとに開いて閉じます（1 Tick に最大 100 回）。Purge には**独自の時間切れがありません**。アプリケーションが動き続けたまま PostgreSQL だけが止まると、Tick は接続が失敗するか Janitor が止められるまで待ちます（以前の Pool 経由でも同じで、この Issue では時間切れを足していません）。

### 制限と未確認の点

- 削除は Janitor の間隔だけ遅れます（既定で最大約 1 時間）。ただし、期限切れの Item は削除の前から全ての Method で「存在しない」ので、読み取りは Janitor に依存しません。Janitor が動かない間（`PAW_SCRATCH_PURGE_INTERVAL_SECONDS=0`、`PAW_DATABASE_URL` 未設定、Process の停止、30 秒より短い間隔での再起動の繰り返し）は、期限切れの行と内容が DB に残ります。Janitor の停止や失敗を知らせる仕組み（Metrics、Alert）はなく、あるのは Log の WARNING だけです。詳しくは[Janitor](#janitor期限切れの削除)。
- `task_id` は素の UUID なので、Task を削除した後は存在しない Task を指し、DB はそれを検査しません（存在の確認は `add` の時点だけ）。[Decision 0013](../../docs/decisions/0013-research-scratch-task-relation.md)で、この選択が承認されています。
- Claim と Source の対応（Evidence / Provenance）は [PAW-052](#evidence--claim-provenance) です。ここでは `source_metadata` に置くだけで、構造化しません。
- 1 Project あたりの Item 数の上限（Quota）は持ちません。
- Purge の「Snapshot の後に Commit された Lease」の Race は、実際の同時実行では起こしにくい時間窓です。Test は `purge_probe`（Test 用の接続点）で、その瞬間に exempt が現れる状況を決定的に再現して確認しています。同時実行の Test は複数回繰り返して安定を確認していますが、時間窓そのものを外部から狙って再現しているわけではありません。
- Migration `0050` の `down_revision` は `0031` です（鎖は `0001 → 0025 → 0032 → 0040 → 0021 → 0033 → 0031 → 0050`）。
- **Application Role の権限。** 共通の `grant_app_privileges`（PAW-025）で、`ScratchStore` が実行する文に必要な最小の権限だけを付けます。`research_scratch_items` は SELECT / INSERT / DELETE と、UPDATE は `pinned`・`saved`・`promotion_state`・`promotion_requested_at` の 4 列だけ、`research_scratch_leases` は SELECT / INSERT / DELETE と、UPDATE は `leased_at`・`expires_at` だけです。したがって Application は `expires_at`（Item の期限）、内容、`project_id` などを SQL で書き換えられず、TRUNCATE と Schema の変更もできません。`test_scratch_grants.py` は、この Role で `ScratchStore` の Test を全て実行し、上の権限の一致と、禁止した文の拒否を確かめます。

### 人間の判断が必要な点

1. **昇格の確認中（`pending`）の期限。** 期限がないため、確認の Flow が止まると、その Item は残り続けます（`promotion_requested_at` で見つけられます）。期限を付けるか。
2. **「User が明示保存」（[Decision 0013](../../docs/decisions/0013-research-scratch-task-relation.md)で承認済み）。** [要件](../../REQUIREMENTS.md)の「Pin 済み」と「User が明示保存」は別の延期理由なので、Pin を 1 つの真偽値で兼ねると `unpin` が保存まで消して期限切れの Item を削除させてしまいます（Review の指摘）。そこで Pin（`pinned`）と保存（`saved`）を**独立した 2 つの印**にしました（情報を失わない最小の案）。承認された内容は次のとおりです。保存と Pin は UI・一覧で今は区別しない。保存済み・Pin 済みの件数の Quota は今は持たない。`save` / `unsave` は User 本人だけができる操作で Agent へ委任できない（`pin` / `unpin` は従来どおり）。誰が・いつ保存したかは記録せず、1 人の `unsave` で保存の印が消える。保存に最大の期間は付けない。
3. **Claim と Source の置き場所。** PAW-052 まで `source_metadata`（16 KiB まで）に置きます。
4. **Task を削除したときの `task_id`。** Review は、最初の実装（`tasks.id` への Foreign Key、`ON DELETE SET NULL`）は Task の削除で `task_id` を失い、Pin 済みの Item が Project / Task の関係を保てないと指摘しました。要件は Task 削除時の扱いを定めていないため、[Decision 0013](../../docs/decisions/0013-research-scratch-task-relation.md)（2026-09-25 に承認）として、Foreign Key を持たない素の UUID にしました。Task の削除は止められず（`RESTRICT` は止める）、Pin 済みは消えず（`CASCADE` は消す）、期限切れは消え、関係は残ります。代わりに DB は存在を保証せず、削除された Task を指す `task_id` が残ります。`RESTRICT` など別の選択にするか、Task を論理削除にして Foreign Key に戻すかは、Task の削除の方針（物理か論理か）や Project の Table が入ったときに、新しい Decision で見直します。
5. **認可の対応。** 上の「呼び出し側の認可（提案）」で、特に `resolve_promotion` を委任不可の `project.memory.manage` にする点。
6. **Quota。** Item 数の上限。保存済み・Pin 済みの件数の上限は、[Decision 0013](../../docs/decisions/0013-research-scratch-task-relation.md)で今は持たないと承認されています。
7. **Janitor の既定値。** 間隔（1 時間）、起動の 30 秒後に最初の Tick、1 Tick の上限（500 件 × 100 Batch）。仕様に数値がないため、最も単純な値を選びました。
8. **PostgreSQL が止まったときの終了。** Review の指摘に従い、Purge は中断できる専用の接続で行い、Cancel と `dispose()` で接続の Socket を閉じます（上の「止まらない PostgreSQL」）。残る判断は、Purge の 1 Batch に**時間切れ**を付けるか（付ければ、応答しない PostgreSQL の間も Janitor が自分で諦めて次の Tick に進みます。値は仕様にないため付けていません）と、1 Batch ごとに接続を開くコストを許すかです。

## Research Provider Adapter

[PAW-051](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/43) で実装した、Research の Provider を抽象化する層です（`paw_backend/research/providers/`）。
設計は [要件](../../REQUIREMENTS.md)の「Web Research / Knowledge Layer」（Provider abstraction、Privacy）に従います。
**実際の Provider は含みません。** Direct Web、Docs、GitHub、OpenCode の Adapter は Credential と Network Policy が必要なため、別の Issue で実装します。
**HTTP の Endpoint も、Research Scratch（PAW-050）への保存もありません。** この層は検索結果を返すだけで、何も保存しません。

Main Agent が使うのは `ResearchBroker` だけです。
Provider の違い（API の形、Response の形、失敗の仕方）は Broker が吸収し、Main Agent には常に同じ形の `ResearchResult` が返ります。

| Module | 内容 |
| --- | --- |
| `contract.py` | `ProviderKind`、`SourceType`、`ResearchRequest`、`ProviderHit` / `ProviderDocument`（Adapter が返す）、`SourceMetadata` / `ResearchItem` / `ResearchError` / `ResearchResult`（Main Agent が受け取る）、`ResearchProvider`（Protocol） |
| `errors.py` | 閉じた `ResearchErrorCode`、`ProviderFailure`（Provider が分類済みの失敗を報告する）、Registry 等の例外 |
| `guard.py` | `CancelGuard`: 同期で動く Adapter の Code が要求した Task の Cancel を取り消す（Registry と Broker が使う） |
| `locator.py` | `canonicalize_locator`: URL の正規化と無害化 |
| `normalize.py` | `normalize_hits`（Provider の Response を検証して統一形式へ）、`merge_items`（交互配置・重複除去・件数制限） |
| `registry.py` | `ProviderRegistry`: 登録時の Interface 検証、一意な名前、決定的な順序 |
| `preflight.py` | `SearchPreflight`（Protocol）: Provider を呼ぶ前に Request を書き換える・拒否する差し込み口（PAW-053 の Privacy Filter が実装する。`gather` に必須）、`PreflightNotConfiguredError` / `PreflightRequiredError` |
| `broker.py` | `ResearchBroker.gather` / `fetch`: 並行実行、Timeout、失敗の隔離 |
| `static.py` | `StaticProvider`: Test 用の Fake（Network を使わない） |

### Provider Interface

Adapter は `ResearchProvider`（Protocol）を実装します。

```python
class ResearchProvider(Protocol):
    name: str  # Registry 内で一意な ID（[a-z0-9][a-z0-9_-]{0,63}）
    kind: ProviderKind  # web / docs / github / opencode

    async def search(self, query: str, *, limit: int) -> Sequence[ProviderHit]: ...
    async def fetch(self, locator: str) -> ProviderDocument: ...
```

- `search` は最大 `limit` 件を **`list` か `tuple` そのもの**で返します（Subclass は不正な Response です。理由は下の「Broker の動作」4）。`fetch` は正規化済みの URL の文書を返します。
- Credential は受け取りません。Adapter は Backend Tool Broker から取得します（Secret Isolation）。Main Agent にも Request にも Credential は載りません。
- Adapter は自分で Retry せず、`asyncio.CancelledError` を握りつぶしません（Timeout は Cancel で実現するため）。
- 分類できる失敗は `raise ProviderFailure(ResearchErrorCode.RATE_LIMITED)` のように報告します。元の例外の文言は捨てます。
- `ProviderHit` / `ProviderDocument` に `private_source`（Private Repository 由来など）の既定値はありません。Adapter が必ず明示します。Provider 名や生の Payload を入れる欄もありません。

Registry は登録時に `name` と `kind` の形、`search` と `fetch` が `async def` であること、Signature が `search("q", limit=1)` と `fetch("https://x/")` を受け付けることを検証します。
不適合な Adapter は `ProviderInterfaceError`（失敗した Member 名だけを持つ）で拒否され、あとから「全 Provider が失敗した成功 Response」になることはありません。
`name` と `kind` は登録時に 1 度だけ読み、以後 Provider 側が変えても結果には影響しません。

**名前と種類の検査（Adapter の Object の Method を動かさない）。**

- `name` は、受け取った文字列そのものを `fullmatch` で `[a-z0-9][a-z0-9_-]{0,63}` に照合します。正規化した写しは照合しません。Pattern は ASCII だけで、`$` も `IGNORECASE` も使いません。そのため、全角の英数字、NFKC で ASCII と同じになる文字（合字、丸数字、Kelvin 記号、長い s など）、大文字、Zero-width 文字、前後や途中の空白、末尾の改行（`$` なら通る）は、全て `ProviderInterfaceError("name")` で拒否されます。NFKC・小文字化・`strip` で別の名前と一致させられることも、登録済みの名前と「見た目が同じ」名前が別に登録されることもありません（`test_research_registry.py` が、全 Unicode コードポイントを 1 文字目と 2 文字目に置いて、通るのが ASCII の `[a-z0-9]` / `[a-z0-9_-]` だけであることを確かめます）。
- `str` の Subclass（`StrEnum` の要素など）は、中身が合っていれば受け付けますが、Registry が保持するのは**厳密な `str` の写し**で、Adapter が返した Object そのものではありません。Subclass が `__hash__`・`__eq__`・`__lt__`・`__str__` などを上書きしていても、登録・`select()`・`gather()` が例外で失敗すること、一意性の検査をすり抜けて同じ名前を 2 つ登録すること、Log の文字列に Credential のような文字が入ることは起きません（[PAW-051 の独立 Review の指摘](../../docs/decisions/0012-research-provider-adapter-policy.md)）。写しは C の `str.encode` で作るので、上書きされた Method は呼びません。
- 型は `type()` で読みます（`isinstance` は Object の `__class__` を信じます）。`__class__` で `str` や `ProviderKind` を名乗るだけの Object は、`re` の `TypeError` などの別の例外ではなく、`ProviderInterfaceError("name")` / `("kind")` になります。`kind` は `ProviderKind` の要素そのものだけを受け付けます（Member を持つ Enum は継承できません）。
- 範囲: これは Adapter が返す `name` と `kind` の話です。`ProviderRegistry.get(name)` の引数と、呼び出し元が作る `SourceMetadata.provider_id` は、呼び出し元（Tool Broker）の Code で、この層は写しません（Broker が返す値は Registry の写しです）。

**Adapter の属性の読み取り（登録の窓）。** `register` と `validate_provider` が `name`、`kind`、`search`、`fetch` を読む部分と、`search` / `fetch` が返した Object を `inspect` で調べる部分は、Adapter の Code（Property、`__getattribute__`、`__getattr__`、Descriptor、`__class__`、`__signature__`、`Signature.bind`）が動く窓です。

- Member ごとの窓の中で Adapter が出した例外は、`BaseException`（`CancelledError`、`KeyboardInterrupt`、`SystemExit`、`GeneratorExit` を含む）を全て、その Member の固定の `ProviderInterfaceError("name" / "kind" / "search" / "fetch")` になります。Adapter の文言や例外 Object は Error に入らず、`__cause__` / `__context__` にも付きません（`try` の外で送出するため。Log に `exc_info` を付けても Adapter の文言は出ません）。`register` が失敗しても Registry は変わりません。
- 例外を出さずに `asyncio.current_task().cancel()` を呼んで値を返す Hook は、Broker と同じ `Task.cancelling()` の Guard（`guard.CancelGuard`）で取り消し、その Member の `ProviderInterfaceError` にします。
- 登録は同期（`await` しない）です。本物の Cancel は窓の中には届かず、登録の前からあった Cancel の要求は残って、呼び出し元の次の `await` で届きます。
- 代償は Broker の同期の窓と同じで、窓の間（起動時の配線の数マイクロ秒）に Signal で届いた本物の `KeyboardInterrupt` も、不適合な Adapter として拒否されます（Decision 0012 の 10）。
- Test: `tests/test_research_registry.py` の `HostileMemberReadTest`（Property、`__getattribute__`、`__getattr__`、Descriptor が、4 つの Member それぞれで `RuntimeError`、`RecursionError`、Hook が敵対的な例外、`CancelledError`、`KeyboardInterrupt`、`SystemExit`、`GeneratorExit`、独自の `BaseException` を出しても、固定の `ProviderInterfaceError` になり、`str` / `repr` / `args` / 整形した Traceback と Log に Adapter の文言がなく、`__cause__` / `__context__` が空であること）、`HostileMemberProbeTest`（`__class__` を読むと出す Object、Metaclass の `__getattribute__` が出す Class、`bind` が出す `Signature` を `search` / `fetch` に持つ Adapter）、`RegistrationCancellationTest`（Cancel を要求して値を返す Hook の取り消し、前からある要求は残って次の `await` で届くこと、`CancelledError` を出しても呼び出し元は Cancel されないこと）。

### Request と Result

`ResearchRequest` は不変で、構築時に全項目を検証します。

| 項目 | 内容 |
| --- | --- |
| `query` | 1 〜 512 文字、空白のみ不可、改行・Tab を含む制御文字は不可。Privacy Filter（PAW-053）で最小化済みの Query を渡す。Broker は中身を見ず、書き換えない（pre-flight を設定した場合だけ、pre-flight が Query を差し替える。pre-flight の無い Broker は `unfiltered=True` を明示しない限り検索しない。[Research Privacy Filter](#research-privacy-filter)） |
| `max_results` | 1 〜 50（既定 10）。各 Provider に頼む件数と、結果の最大件数の両方 |
| `kinds` | `ProviderKind` の空でない `frozenset`（既定は全種類）。Provider 名では選べない |
| `time_budget_seconds` | 1 回の `gather` 全体の上限。0 より大きく 120 以下（既定 30） |

型は変換しません（`bool` は `int` ではなく、`set` や `list` は `kinds` になりません）。

`ResearchResult` は `items`（`ResearchItem` の tuple）、`errors`（失敗した Provider ごとに 1 つ）、`providers_queried`、`truncated`（`max_results` を超えて捨てた）を持ちます。
`providers_queried` により「見つからなかった」と「誰にも聞かなかった」を区別でき、`all_failed` で全 Provider の失敗を判定できます。

### 統一された Source Metadata

Provider の種類によらず、全ての `ResearchItem` は同じ `SourceMetadata` と本文 `text` だけを持ちます。

| 項目 | 内容 |
| --- | --- |
| `provider_kind` / `provider_id` | Registry が持つ値。Provider 自身の申告ではない |
| `locator` | 正規化済みの http(s) URL（次の節） |
| `title` | 空白を 1 つにまとめた Title（空でもよい、300 文字まで） |
| `retrieved_at` | 取得時刻（UTC）。1 回の `gather` で全 Item に同じ値 |
| `content_hash` | `sha256:` + `text` の UTF-8 の SHA-256。取得した Excerpt / 文書の Hash で、遠隔の文書全体とは限らない |
| `source_type` | `official_docs` / `official_github` / `primary` / `secondary` / `community` / `unknown`。Provider が宣言し、真偽の判定には使わない |
| `published_at` | 公開日時（UTC）。不明なら `None` |
| `private_source` | Private な Source かどうか。PAW-053 の Privacy Filter が使う |

`SourceMetadata.to_dict()` は JSON にできる形（Enum は値、日時は ISO 8601）を返します。
`SourceMetadata` 自身も、http(s) 以外、User 情報、Fragment、空白・制御文字を含む URL を拒否します。

License や `robots.txt` に関する項目はありません。要件と設計文書に定義がないため、決まってから追加します。

### URL の正規化

`canonicalize_locator` は、同じページを指す URL を 1 つの文字列にして、重複を除けるようにします（DNS 解決も Network もない純粋な関数です）。

- `http` / `https` 以外、User 情報（`user:pass@`）、空白・制御文字・バックスラッシュ、不正な Port、Host が `a-z0-9-` と `.` だけで作れない場合（IPv6、`_`、非 ASCII の Host）は `InvalidLocatorError`。
- Scheme と Host は小文字にし、Host 末尾の `.` を 1 つ取り、既定の Port（http 80、https 443）と Fragment を外し、空の Path を `/` にします。
- Path と Query の `%xx` は大文字にし、非 ASCII の文字は UTF-8 の `%XX` にします。
- Query は `&` で分け、追跡用（`utm_*`、`fbclid`、`gclid` など）と Credential 用（`access_token`、`token`、`api_key`、`sig` など）の Parameter を除き、`(名前, 値)` の順に並べます。除く名前の一覧は `locator.py` の定数です。名前は Percent-decode（最大 4 回）してから比べます（`%61ccess_token` も除きます）。decode した名前に `&`、`;`、`=`、`#` が入るもの（`%26access_token` など、先に decode する Parser では別の Parameter になる）は、名前として成り立たないので丸ごと除きます。Credential の一覧は Best effort で、Path に入った Credential は判別できません。
- Path の Dot Segment、末尾の `/`、`www.`、`http` と `https` の違いは正規化しません。そのため、これらだけが違う URL は別の Source として扱います。
- 正規化の結果は 2048 文字以下で、もう一度かけても同じ結果になります。
- 例外の文言に URL は入りません。

### Broker の動作

`ResearchBroker(registry, preflight=gate).gather(request, preflight_input=...)` は次のとおり動きます（`preflight` が無く `unfiltered=True` でもない Broker の `gather` は、次の手順に入る前に `PreflightRequiredError` で拒否し、Provider を 1 つも呼びません。[Research Privacy Filter](#research-privacy-filter)）。

1. `request.kinds` に合う Provider を Registry の順序（`ProviderKind` の宣言順、次に名前順。登録順には依存しない）で選びます。
   `ResearchBroker(registry, preflight=...)` で pre-flight を設定した場合は、ここで、どの Provider も呼ぶ前に `preflight.preflight(request, kinds, preflight_input)` が走り、返された Request が以降の全ての段階で使われます（拒否は例外として `gather` から出て、Provider は呼ばれません。詳しくは [Research Privacy Filter](#research-privacy-filter)）。`unfiltered=True` の Broker（pre-flight が無い）では、この段階は何もせず、Query はそのまま Provider へ渡ります。
2. **全 Provider を並行**で実行します。各 Provider の制限時間は、登録時の `timeout_seconds`（既定 10 秒、最大 120 秒）と、全体の Budget の残りの小さい方です。時間切れの Provider は Cancel し、完全に終わるまで待ってから `timeout` として報告します。`gather` が返るとき、起動した Task は残りません。
3. Provider の例外は Provider ごとに隔離します。他の Provider の結果は失われません。`gather` を Cancel した場合は全 Provider を Cancel して `CancelledError` を伝えます。
   **`await` の最中の Cancel と、同期で動く Adapter の Code の例外は区別します。** `Task.cancel()` は `await` の地点でしか届きません。`provider.search` を読んで呼ぶ部分（Property、`__getattribute__` など）と、`published_at` の `tzinfo.utcoffset` は `await` を挟まない同期の Code なので、そこが `CancelledError`、`KeyboardInterrupt`、`SystemExit`、`GeneratorExit` などの `BaseException` を出しても、それは Task の Cancel ではなく Adapter 自身の失敗です。前者は `internal_error`、後者は `invalid_response` として報告し、他の Provider の結果を残します（`gather` / `fetch` は Cancel されません）。`await` の最中に届いた Cancel と Timeout は、これまでどおり握りつぶさず伝えます。
   **例外を出さずに Cancel だけを要求する Code も同じです。** 同期の Code が `asyncio.current_task().cancel()` を呼んでから普通に値を返す（`__getattribute__` が Method を返す、`tzinfo.utcoffset` が Offset を返す）と、何も出ませんが、要求は Task に残り、次の `await`（か Task の終わり）で `gather` 全体を Cancel して、成功した他の Provider の結果を失わせます。Broker は同期の窓（`provider.search` / `fetch` を読んで呼ぶ部分と、1 つの Provider の Response の検証）の前後で `Task.cancelling()` を比べ、増えた分だけ `Task.uncancel()` で取り消して、その Provider を Adapter の失敗（前者は `internal_error`、後者は `invalid_response`。Log の `exception_type` は `adapter_error`）にします。呼び出した Coroutine は `await` せずに閉じるので、Provider の Code は動きません。窓の前からあった Cancel（呼び出し元自身の `cancel()`）は残り、`await` の地点で届きます。限界は Decision 0012 の 10 に書いたとおりです（数が 1 以上で Cancel が未着の Task では印を消せない）。`ProviderRegistry.register` / `validate_provider` の窓も同じ Guard で保護します（「Adapter の属性の読み取り」）。
4. Response は Provider ごとに全体を検証します（`list` / `tuple` そのもの、件数が `limit` 以下、全要素が `ProviderHit` で **Field の値も正しい**、全 URL が正規化できる）。1 つでも違反があれば、その Provider の結果は全て捨てて `invalid_response` にします。Field の検証は下の「Constructor を通らない Hit と Document」のとおりです。
   **Container 自体も Adapter の Code を動かしません。** `list` / `tuple` の Subclass（と、`__class__` で `list` を名乗る Object）は、`__len__`、`__iter__`、`__getitem__` を Adapter が上書きでき、Broker の中で例外を出したり、長さを偽って `limit` を超えさせたりできます。そのため Class は `type(x) is list`（または `tuple`）で確かめ（`isinstance` は `__class__` を Object に尋ねます）、Subclass は Hook を一切呼ばずに `invalid_response` にします。
5. Provider の結果を交互に並べ（各 Provider の 1 位、2 位、…）、正規化した URL で重複を除いて（最初の 1 件を残し、どれか 1 つでも Private なら `private_source` を True にする）、`max_results` 件までにします。
6. `errors` は Registry の順序です（完了順ではありません）。

**Constructor を通らない Hit と Document。** `isinstance(x, ProviderHit)` は Class しか証明しません。`object.__setattr__` で作った Object、Constructor が Slot を設定しない Subclass、`text` が `str` でない Object、上限を超える文字列、UTC で表せない `published_at` も `ProviderHit` の Instance です。
そのため `normalize_hits`（`gather` の経路）と `fetch` は、受け取った Object の Field を全て 1 度だけ読み直し、`ProviderHit` / `ProviderDocument` の Constructor と同じ規則で検証し直します（`revalidate_hit` / `revalidate_document`）。以後の処理は、その検証済みの複製だけを使います。
- Field は `ProviderHit` 自身の Slot から直接読みます。Subclass の Property や `__getattribute__` は呼びません（呼ぶと、任意の例外や、読むたびに変わる値を許すため）。Subclass 自体は使えますが、Property だけで Field を返し Slot を設定しない Subclass は不正な Response です。
- `str` の Subclass は、`__len__` や `encode` を呼ばずに通常の `str` へ複製してから検証します（長さを偽れません）。`source_type` は `SourceType` そのもの、`private_source` は `bool` そのものだけを受け付けます（`__class__` を偽る Object は不正）。
- `published_at` は `None` か、UTC に変換できる Timezone つきの `datetime` だけです。まず標準の `datetime` の Method で Field を通常の `datetime` へ複製し（`datetime.astimezone` は途中の値を Subclass 自身の Constructor で作るため、複製せずに呼ぶと Adapter の Code が動きます）、その複製を UTC へ変換して、通常の UTC の `datetime` にします。動くのは Adapter の `tzinfo.utcoffset` だけで、それが出した例外は、`asyncio.CancelledError`、`KeyboardInterrupt`、`SystemExit`、`GeneratorExit` を含む `BaseException` の全てが不正な Response です（同期の Code に Task の Cancel は届かないため。[Decision 0012](../../docs/decisions/0012-research-provider-adapter-policy.md) の 10）。`utcoffset` が `asyncio.current_task().cancel()` を呼んで Offset を返す場合も同じで、Broker が Response の検証の前後で `Task.cancelling()` を比べ、増えた分を `Task.uncancel()` で取り消して、`invalid_response` にします。
- 違反は全て `InvalidProviderResponseError`（固定の文言。値も例外の文言も含みません）になり、`gather` はその Provider を `invalid_response` にして他の Provider の結果を残します。`fetch` は `errors` に `invalid_response` を 1 件返します。例外は呼び出し元へ出ません。
- Test: `tests/test_research_normalize.py` の `test_a_timezone_that_raises_a_base_exception_is_an_invalid_response`（5 種類の `BaseException` を `utcoffset` の 1 回目と `astimezone` の 2 回目で出す）と `test_a_datetime_subclass_constructor_never_runs`、`tests/test_research_broker.py` の `SynchronousHookBaseExceptionTest`（`gather` / `fetch` が `invalid_response` / `internal_error` を返し他の Provider の結果を残すことと、`await` の最中の Cancel が `gather` / `fetch` へ伝わること）、`SynchronousHookCancelRequestTest`（`__getattribute__` と `utcoffset` が現在の Task を Cancel して普通に値を返す場合に、`gather` で他の Provider の結果が残り、`fetch` が `internal_error` / `invalid_response` を返し、Task に要求が残らないこと。窓の前からあった要求は残ること、本物の Cancel と Timeout はこれまでどおり効くこと）と `CancelGuardTest`（取り消す数、前からある要求、Task の外）。

失敗は閉じた `ResearchErrorCode` の値としてだけ報告します。

| Code | 意味 |
| --- | --- |
| `timeout` | Provider または全体の Budget の時間切れ。Provider 自身が `TimeoutError` を出した場合も含む |
| `rate_limited` / `unavailable` / `not_found` / `internal_error` | Provider が `ProviderFailure` で報告した値。`internal_error` は、`ProviderFailure` 以外の全ての例外にも使う |
| `invalid_response` | Interface の違反（型、件数、URL、Field の値） |

**例外の文言は Code にも Result にも Log にも入りません。** Log は Provider 1 つの失敗ごとに WARNING を 1 行（Provider の ID、種類、Code、`exception_type`）出し、Query と URL は出しません。
**`exception_type` は固定の分類で、Adapter が決められる文字列は入りません。** Adapter が上げた例外の Class 名は Adapter のデータです（`type("access_token=SECRET\nforged", (Exception,), {})()` のように、Credential や、次の Log 行を偽造する改行を入れられます。`__name__` の読み方を変えても、実際の名前は Adapter が決めています）。そのため Log に出すのは、`type(error)` が**次の Class そのもの**（`is` で照合。名前が同じ Class、Subclass は含まない）のときだけ、その Class 名です。1 つは Python 組み込みの例外（`RuntimeError`、`ValueError`、`OSError` とその Subclass の `ConnectionRefusedError`、`TimeoutError`、`ExceptionGroup` など）、もう 1 つはこの Package の例外（`ProviderFailure`、`InvalidProviderResponseError` など）で、一覧は `broker.py` の `LOGGED_EXCEPTION_TYPES` です。それ以外の全て（Adapter が定義した Class、組み込みの Subclass、名前だけ似せた Class を含む）は固定の `adapter_error` です。名前を切り詰めたり無害化したりして出すことはしません。Adapter が独自の例外で `RATE_LIMITED` などを伝えたいときは `ProviderFailure` を上げます（`code` は別に Log に出ます）。**限界:** 組み込み以外の Library の例外（`httpx.ConnectError` など）は `adapter_error` になり、型では区別できません。区別が要るときは Adapter が `ProviderFailure` へ変えます。
`ProviderFailure.code` を書き換えて文字列にしても、`internal_error` になります。
**失敗の分類も Adapter の Code を動かしません。** Adapter が上げた例外は、`ProviderFailure` の Subclass が `code` の Property、`__getattribute__`、`__class__`、Metaclass の `__name__` を上書きして、例外を出したり読むたびに違う値を返したりできます。それが `_call_provider` の Handler から漏れると `TaskGroup` が中断し、他の Provider の成功した結果も失われます。そのため `code` は `ProviderFailure` 自身の Slot（`ProviderFailure.code`）から 1 度だけ読み（Constructor も同じ Slot へ書きます）、Class は `type()` と `issubclass` で確かめ、`ResearchErrorCode` そのものでない値、Slot が未設定（Subclass の Constructor が `super().__init__` を呼ばない）の場合は `internal_error` にします。ログの `exception_type` も Adapter の Code を動かしません（`type()` で Class を得て `id` で一覧を引くので、Metaclass の `__getattribute__`、`__name__`、`__hash__`、`__eq__` などは呼ばれません）。`classify_failure` と `log_type_name` は例外を出しません。

Test: `tests/test_research_broker.py` の `LoggedExceptionTypeTest`（Credential・改行・制御文字・10 万文字・非 ASCII・書式指定子・組み込みや `ProviderFailure` を名乗る Class・`__name__` / `__qualname__` / `__module__` の上書き・作成後の改名・Metaclass の Hook を持つ例外が、`gather` と `fetch` で固定の `adapter_error` の 1 行になること、一覧の Class だけが名前で出ること、Subclass は出ないこと、Hook が動かないこと）。

`fetch(source, time_budget_seconds=30)` は、以前の結果の `SourceMetadata` から、同じ Provider の `fetch` を、`gather` と同じ隔離・Timeout・Log の規則で呼びます。制限時間は `min(登録時の timeout_seconds, time_budget_seconds)` です。Provider が登録から外れている（または種類が違う）場合は `unavailable` です。`ProviderDocument` でない Response と、Field が不正な `ProviderDocument`（上の「Constructor を通らない Hit と Document」）は `invalid_response` です。結果は 1 件の `ResearchItem`（`retrieved_at` は取得時、`private_source` は Source と文書のどちらかが True なら True）か、`errors` の 1 件です。

### Security と Privacy

- `network` Capability の確認は、この層の呼び出し元（Tool Broker、PAW-031）の責任です。この層は認可の判断も Network の Access もしません。
- どの Host へ接続してよいか（SSRF、Private Address、`robots.txt`）は、具体的な Adapter と Network Policy の責任です。`canonicalize_locator` は名前を解決しません。
- Query の最小化と Secret の除去は、Privacy Filter（PAW-053、[Research Privacy Filter](#research-privacy-filter)）が、pre-flight として行います。`private_source` は、以前の結果を Context の Piece にする `context_pieces_from_items` が使います。pre-flight を設定しない Broker の `gather` は `PreflightRequiredError` で拒否します（Fail closed）。Query をそのまま Provider へ渡すのは、`unfiltered=True` を明示した Broker だけです。
- 全ての入力（Query、件数、文字数、Provider 数）に上限があります。Registry は 32 Provider までです。

#### 各 Adapter の受け入れ条件

[Decision 0012](../../docs/decisions/0012-research-provider-adapter-policy.md) の承認時の決定により、次の責務は Broker ではなく個々の Adapter と呼び出し元（Tool Broker）にあります。
Direct Web、Docs、GitHub、OpenCode などの Adapter の Issue は、受け入れ条件に次を明記します（現在の [Implementation Backlog](../../docs/IMPLEMENTATION_BACKLOG.md) には、個別の Adapter の Issue はまだありません）。

- [ ] `network` Capability を、呼び出し元（Tool Broker、PAW-031）が確認してから Adapter を呼ぶ（Adapter を直接呼ぶ経路を作らない）。
- [ ] SSRF 対策（Private Address、Loopback、Link-local、Cloud の Metadata Address などへの接続の拒否。Redirect の先も同じ）。
- [ ] 名前解決の後の接続先の検査（DNS Rebinding への対策。検査した Address へ接続する）。`canonicalize_locator` は名前を解決しません。
- [ ] `robots.txt` の遵守（該当する Adapter だけ）。
- [ ] Timeout と Cancel に応じる非同期の実装（Broker の Timeout は協調的です）。
- [ ] Response は `ProviderHit` / `ProviderDocument` に変換し、外部の Library の例外は `ProviderFailure` に分類してから返す。
- [ ] License と `robots.txt` は `SourceMetadata` に含めない（必要になったときに、Decision を経て追加する）。

### 実装の由来と制約

- `locator.py` と `normalize.py` は、Local の Qwen3-Coder（30B-A3B）が最初の実装を書き、テストにも合格しました。
  しかし、レビューで、テストが見逃す不具合が見つかったため、本体は Claude が書き直しています（書き直し前の本体は残っていません）。
  見つかった不具合は、`%` の直後の非 ASCII を変換しない、`?é=` の `=` を落とす、KELVIN SIGN が ASCII の Host になる、入力の長さを最後に検査するため 20 MB の入力の拒否に数秒かかる、例外の Context に入力が残る、`hasattr` による偽の Hit の受理、広すぎる `except` です。
- `registry.py` と `broker.py` は、Local Model が仕様どおりに実装できなかったため、仕様を書いた側の参照実装を整えたものです。
- Timeout は協調的です。Adapter が Cancel を無視する、または Event Loop を止める同期処理をする場合、Broker は止められません。
- 実装が選んだ方針（**[Decision 0012](../../docs/decisions/0012-research-provider-adapter-policy.md) は 2026-09-25 に Human が承認しました**。変える場合は新しい Decision から `Supersedes` します）:
  1. License と `robots.txt` の項目は、要件に定義がないため `SourceMetadata` にありません。
  2. 不正な Hit が 1 つでもあると、その Provider の Response 全体を `invalid_response` にします（Adapter の不具合を隠さないため）。Constructor を通らずに作られた Hit / Document は、Field を読み直して検証し、Subclass の Property は使いません。Response の Container は `list` / `tuple` そのものだけで、Subclass は Hook を呼ばずに不正とします。`ProviderFailure` の分類も Subclass の Hook を呼びません（Adapter の Code を Broker の中で動かさないため）。
  3. 複数 Provider の結果は交互に並べ、正規化した URL の最初の 1 件を残します（要件に統合の規則がありません）。
  4. Credential 用の Query Parameter の一覧は Best effort です。
  5. IPv6 と非 ASCII の Host は拒否し、名前解決はしません。`network` Capability、SSRF、`robots.txt` は呼び出し元（Tool Broker、PAW-031）と個々の Adapter の責任です。
  6. Provider の名前は正規化せず（look-alike は拒否）、`str` の Subclass は厳密な `str` の写しにして保持します。Subclass を拒否する案は採っていません（`StrEnum` の要素を名前にできるため）。
  7. 同期で動く Adapter の Code（`published_at` の `tzinfo`、`provider.search` を読んで呼ぶ部分）が出した `BaseException` は、`CancelledError`、`KeyboardInterrupt`、`SystemExit` を含めて Adapter の失敗として報告します。同期の Code に Task の Cancel は届かないためです。代償として、その数マイクロ秒の間に Signal で本物の `KeyboardInterrupt` が届くと、それも `invalid_response` になり、握りつぶされます。例外を出さずに `asyncio.current_task().cancel()` を呼んで値を返す同期の Code は、`Task.cancelling()` の増加を `Task.uncancel()` で取り消して、同じく Adapter の失敗にします（数が 1 以上で Cancel が未着の Task では `uncancel()` が印を消せない、という限界があります。登録の窓も同じ扱いです）。**限界:** Adapter の非同期の Code（`await` の最中）が自分で出した `CancelledError` や自分で呼んだ `Task.cancel()` は、Task の Cancel と区別せず、これまでどおり `gather` へ伝わります（`await` の最中は本物の Cancel と数の増加で区別できないため、区別する案は Decision 0012 では決めず、必要になったときに別の Decision で決めます）。

### Test

`apps/backend/tests/test_research_*.py` です。標準 `unittest` だけで、DB も Network も使いません。
Timeout の Test は、永遠に待つ Provider を 0.3 秒で打ち切り、成功する Provider は即座に答える構成です（所要時間を厳密には検査せず、30 秒の Guard で CI の停止を防ぎます）。

## Research Privacy Filter

[PAW-053](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/45) で実装した、外部の検索へ送る Query を最小化し、送信を Audit する層です（`paw_backend/research/privacy/`）。
設計は [要件](../../REQUIREMENTS.md)の「Web Research / Knowledge Layer」の Privacy に従い、数値と規則は [Decision 0010](../../docs/decisions/0010-research-privacy-filter-policy.md)（Approved。2026-09-25 に Human が承認）で決めています。数値は暫定値として承認されたもので、変更できます。
**永続の Audit Sink（Issue [#87](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/87)）が接続されるまでは、Private 由来の Context を Research へ自動で入れる設計を有効にしません**（承認時の条件）。
**Provider には依存しません。** HTTP の Endpoint も、永続化する Store もありません（Audit の保存先は `ExternalSendAudit`（Protocol）で、後続の Issue が接続します）。Migration もありません。

Main Agent（または Research Worker）は、書いた Query の**下書き**と、その下書きの元になった Context（各 Piece に `ContextLabel` を付けたもの）を Gate に渡します。
Gate は、外へ出してよい `MinimizedQuery` を返すか、`PrivacyRefusal` で拒否します。

| Module | 内容 |
| --- | --- |
| `contract.py` | `ContextLabel`、`ContextPiece`、`PrivacyInput`、`MinimizedQuery`、`ExternalSendRecord`、`WithheldCounts`、`RefusalReason` / `PrivacyRefusal`、`ExternalSendAudit`（Protocol）、`InMemoryExternalSendAudit`、上限の定数 |
| `rules.py` | 最小化の規則（小さな純粋関数）。正規化、写しの検出、Credential の除去、抽象化の規則、切り詰め、Fingerprint |
| `gate.py` | `PrivacyGate`: 入力の検証、Default deny、規則を呼ぶ順序、Rule とは別の最終検査、Audit してから返す |

### Context のラベル

| `ContextLabel` | 意味 | Query に入ってよいか |
| --- | --- | --- |
| `PUBLIC` | Public な文書、Web の結果 | 入ってよい（Gate は書き換えない） |
| `PRIVATE_SOURCE` | Private な Repository・File の本文 | 写しは入らない |
| `PRIVATE_MEMORY` | User の Private Memory | 写しは入らない |
| `RAW_CONVERSATION` | 会話の Raw Text | 写しは入らない |
| `SECRET` | Credential、Token、Password、Key | 写しは（4 文字以上でも）入らない |

ラベルは、出所を知っている Backend のコードが付けます。LLM や Client の申告、Text の中身から推測することはしません。
`context_pieces_from_items` は、以前の Research の結果（`ResearchItem`）を、`private_source` が True なら `PRIVATE_SOURCE`（本文と、あれば Title）、False なら `PUBLIC` の Piece にします。

### Query が外へ出るまで

`PrivacyGate.minimize(draft, context)` は、次の順に処理します（正確な定義は `rules.py` の Docstring）。

1. **拒否の判定**（Text を読む前）: Context に `ContextPiece` でないもの（ラベルの無い文字列など）があれば `unclassified_context`。Draft が 2,000 文字を超えれば `draft_too_long`。Context が 32 個、または合計 400,000 文字（1 個は 200,000 文字）を超えれば `context_too_large`。**文字数の上限は 2 通りに数えます**: 書かれたままの文字数（早い段階の安価な拒否）と、NFKC と完全な Case folding をかけたあとの文字数（`gate.folded_length` = `len(unicodedata.normalize("NFKC", text).casefold())`）です。どちらかが上限を超えれば拒否します。NFKC は 1 文字を最大 18 文字（U+FDFA）に増やせるので、書かれたままの上限（400,000 文字）の範囲でも、展開後は 700 万文字を超え、正規化と窓の集合（`find_copied_spans`）が Event Loop の上で 1 秒を超える時間と数百 MB のメモリを使えたためです（開発機で測った例: U+FDFA 200,000 文字の Piece 1 個で、正規化に約 0.35 秒、窓の集合に約 0.2 秒、最大 RSS 約 360 MB。Piece は 2 個まで作れます）。Folding 後の形は、写しの検出と最終検査が比べる形です。上限の数値は変えていません（Draft 2,000、1 個 200,000、合計 400,000 を、展開後の文字数にも当てます）。Piece は 1 個ずつ測り、超えた時点で止めるので、展開する Context は、`rules.py` が 1 文字も正規化・比較しないうちに拒否されます（最悪でも、200,000 文字までの 1 Piece に NFKC を 1 回かける分の時間とメモリです）。数える対象は全ての Label の Piece です（`PUBLIC` の Text は読みませんが、規則を 1 つにするため数えます）。`ß`（`ss`）のように Case folding で増える文字も数えるので、書かれたままでは上限内の Draft や Piece が、展開後に上限を超えて拒否されることがあります（Decision 0010 の承認後の訂正）。
2. **正規化**: NFKC、制御文字・ゼロ幅文字の除去、空白の圧縮（`normalize_text`）。
3. **Credential の除去**（`strip_credentials`）: PAW-031 の `redact_text` が認識するものを、Draft に書かれたままの形で、**最初に**消します。Text を切る処理（手順 4）を先に走らせると、Credential が断片（`ghp_ABCDEF` と `abcdefghij` など）に切られて、この規則も最終検査も認識できなくなり、断片が外へ出るためです（`ghp_` + 36 文字の GitHub Token が、Private な Context と 16 文字を共有しただけで送られていました）。Credential と同じ文字列の `SECRET` の Piece があっても、Credential として先に消えるので、その Piece は「写しがあった」とは数えません（`credentials_removed` に数えます）。
4. **写しの除去**: `PUBLIC` 以外の Piece ごとに、Draft の中で Piece にもある 16 文字以上（`SECRET` は 4 文字以上。Piece がそれより短ければ Piece の長さ）の連続を、大文字小文字を無視して検出し、全部消します（`find_copied_spans`）。大文字小文字の無視は、Unicode の**完全な Case folding**（`str.casefold`）です。`ß` と `SS`（`ẞ` も）、`İ` と `i` + 結合文字の点（U+0307）、語末の `ς` と `σ` は同じとみなします（`lower()` では一致しません）。窓の長さは Folding 後の文字数で数えます（`ß` は 2 文字）。消す範囲は Draft の**元の文字**の位置で、`ß` のように 1 文字が複数の文字に展開される文字は、一部だけ一致しても、その文字全体を消します。消した場所は空白にし、前後の語が連結することはありません。
   **ただし、消す範囲が、手順 6 の抽象化の規則が書き換える語（Path、Private Host、IP Address、ID、URL、E-mail、長い Token、Version）に触れるときは、その語の全体を消します。** 語の一部だけを切ると、その語を丸ごと消すはずだった規則が語を認識できなくなり、断片が残るためです（Private な Path `/srv/billing/secret-plan-2026.md` の途中の 17 文字だけが Context と共通だと、`an-2026.md` が残っていました。UUID の `123e4567-` と、Private Host の `printer-` も同じでした）。どの規則も書き換えない語（普通の単語）は、これまでどおり写しの部分だけを消します。語は、空白で区切られた 1 続きの文字です。
5. **Credential の再確認**: 手順 4 で Text が変わったときは、`strip_credentials` をもう一度かけます。写しを消すと、隣の文字に隠れていた Credential（`abcdefghijklmnopsk-…` の `abcdefghijklmnop` を消すと `sk-…` が現れる）が見えるようになるためです。
6. **抽象化**（URL → E-mail → File Path → Private Host → ID → 長い Token → Version の順）: 内容は次の表のとおりです。手順 5 と、この手順の各規則のあと、`redact_text` が見つける Credential の数が、その手順の前より**増えて**いれば、直ちに `credential_remains` で拒否します。規則が Credential を作ってしまうと、後の規則がそれを断片に切って、最終検査から隠せるためです。
7. **切り詰め**: 256 文字を超えたら、語の区切りで切ります（`truncated`）。
8. **最終検査**（`rules.py` を使わない）: 単語の文字が 1 つもなければ `empty_query`。`redact_text` が Credential を見つければ `credential_remains`。`PUBLIC` 以外の Piece の全文、または `SECRET` の 4 文字以上の単語が残っていれば `private_text_remains`（NFKC、制御文字・ゼロ幅文字の除去のあと、`rules.py` とは別に `str.casefold` で比べます。`rules.py` と同じ完全な Case folding です）。Rule に不具合があっても、Private な Text は外へ出ません。

| 抽象化の規則 | 内容 |
| --- | --- |
| `abstract_urls` | URL を Host だけにする。Host が Private（IP、`localhost`、Dot の無い名前、末尾が `local` `internal` `corp` など）なら消す |
| `abstract_emails` | E-mail Address を消す |
| `abstract_paths` | `/`、`~/`、`./`、`../`、ドライブ、UNC で始まる語と、区切りを 2 つ以上含む語を消す |
| `abstract_hosts` | Private な Host・IP の語を消す。Port、Path、User 情報（`admin@10.0.0.5`）、IPv6 の `[...]` と Zone ID（`[fe80::1%eth0]:8080`）、`[]` の無い IPv6（`fe80::1%eth0`。`fd00::`、`2001:db8::` のように `::` で終わる圧縮形も、後ろに句読点（`.` `,` `)` など）が付いていても消す）、IPv6 の Network（CIDR 形式。`fd12:3456:789a::/48`、`fe80::1%eth0/64`、`[fd12::/48]`）、末尾の Dot の付いた絶対名（`db.internal.:5432`）を含む。判定の前に、User 情報、Port、Path、`[]`、Zone ID、末尾の Dot を取り除く。`%` を含む名前（Zone ID の付いた名前 `db.internal%eth0`、`1.2.3.4%eth0`、`%` 符号化を含む名前 `db.%69nternal`、`db%2einternal`）も、`is_private_host` が Private とみなすので消す（下の「制限と未確認の点」）。Dot の無い名前は単語と区別できないので残す |
| `abstract_ids` | UUID、Credential Handle、12 桁以上の 16 進数、5 桁以上の数字を消す（小数は残す） |
| `drop_opaque_tokens` | 40 文字以上の Base64 風の連続を消す |
| `generalize_versions` | `3.13.15` を `3.13` にする |

例: `explain <Private な Note の 1 文> in stripe at /srv/billing/app.py` は `explain in stripe at` になります。

### Audit

`PrivacyGate.authorize(...)`（`ResearchBroker` は pre-flight として呼ぶ）は、`minimize` のあと、`ExternalSendRecord` を `ExternalSendAudit.record` へ渡し、**受理されてから** Query を返します。

| Record の項目 | 内容 |
| --- | --- |
| `recorded_at` | 送ってよいと判断した時刻（UTC） |
| `project_id` | Project の UUID |
| `query_fingerprint` / `query_chars` | Query の SHA-256（`sha256:` + 64 桁の 16 進数）と文字数 |
| `provider_kinds` | 実際に Query を送る Provider の種類（`ProviderKind` の順） |
| `withheld` | Label ごとの、Query から外した Piece の数 |
| `credentials_removed` / `pieces_matched` / `abstractions` | 除いた Credential の数、写しがあった Piece の数、抽象化した数 |
| `truncated` | 長さの上限で切ったか |

Record は Query の本文、消した文字列、Context の本文を**持ちません**（Field 自体がありません）。`ContextPiece` の本文は `repr` にも出ません。
Sink が例外を出す、5 秒（既定）以内に終わらない、満杯である場合は、`audit_failed` で拒否し、送りません。ログには例外の型を 1 行（WARNING）だけ出しますが、それは**固定の分類**で、Sink が決められる文字列は入りません。Sink が上げた例外の Class 名は Sink のデータです（`type("access_token=SECRET\nforged", (Exception,), {})()` のように、Credential や、次の Log 行を偽造する改行を入れられ、Metaclass の `__name__` や `__getattribute__` を上書きして、名前を読む処理そのものを例外にもできます）。そのため Gate は `type(error)` が組み込みの例外、またはこの Package の例外の Class **そのもの**（`is` で照合。Subclass や名前だけ似せた Class は含まない）のときだけその Class 名（`RuntimeError`、時間切れの `TimeoutError` など）を出し、それ以外は固定の `adapter_error` にします。これは Provider Broker の `log_type_name`（「例外の文言は Code にも Result にも Log にも入りません」の節。一覧は `broker.py` の `LOGGED_EXCEPTION_TYPES`）をそのまま使っており、Class の属性は 1 つも読まず、`id` で一覧を引くので、Metaclass の Hook（`__getattribute__`、`__name__`、`__hash__`、`__eq__`）は呼ばれず、どんな Class の例外でも拒否は `audit_failed` のままです。組み込み以外の Library の例外（DB Driver の例外など）は `adapter_error` になり、型では区別できません。
Record は「送ってよいと判断した」記録で、Provider が答えたかは含みません。拒否した要求は Record を作りません。

### Broker への差し込み

```python
gate = PrivacyGate(audit)  # audit は ExternalSendAudit
broker = ResearchBroker(registry, preflight=gate)
result = await broker.gather(
    ResearchRequest(draft_query),
    preflight_input=PrivacyInput(context_pieces, project_id),
)
```

- **`gather` は Fail closed です。** `preflight` を渡さず、`unfiltered=True` も渡さない Broker（`ResearchBroker(registry)` だけの呼び出しを含む）の `gather` は、Registry の中身にかかわらず、どの Provider も呼ぶ前に `PreflightRequiredError` を出します。Query が Privacy Filter を通らず、Audit もされずに外へ出るのを、渡し忘れで起こさないためです。例外の文言は固定で、Query も Provider 名も含みません。
- **唯一の明示的な Opt-out は `ResearchBroker(registry, unfiltered=True)` です。** Test と、Private な情報を何も持たない呼び出し側のためのもので、Query を**そのまま**（最小化も Audit も検査もせず）全ての選ばれた Provider へ渡します。Private Source、Memory、会話、Secret から Agent が書いた Query には使わないでください。`preflight` と同時には指定できず（`ValueError`）、`bool` 以外は `TypeError` です。PAW-051 の Test は、この Opt-out で今までどおり通ります。
- `preflight` が無い Broker（`unfiltered=True` を含む）に `preflight_input` を渡すと `PreflightNotConfiguredError` です（確認されると思った Query が、確認されずに出るのを防ぐため）。`unfiltered=True` でない Broker では、先に `PreflightRequiredError`（`PreflightNotConfiguredError` の下位の型）になります。
- `preflight` がある Broker に `preflight_input` を渡さない（`None`）、または `PrivacyInput` でないものを渡すと、Gate は `unclassified_context` で拒否します（Default deny）。
- Provider を選んだ後、どの Provider も呼ぶ前に、Gate が Query を最小化して Audit します。Provider が受け取る Query は、最小化した Query だけです。拒否されると、Provider は 1 つも呼ばれず、`PrivacyRefusal` が `gather` から出ます。
- 選ばれた Provider が 1 つもなければ、何も送られないので、Gate も Audit も動きません。
- 1 回の `gather` につき Record は 1 つです。Time Budget は pre-flight の後から数えます。
- **`ResearchBroker.fetch` は Gate を通りません**（Decision 0010）。`fetch` は Query を持たず、Provider が以前返した Source の Locator（正規化済み）を、その Provider へ戻すだけです。そのため、`preflight` の有無にも `unfiltered=True` にも関係なく、どの Broker でも使えます（`preflight` も `unfiltered=True` も無い Broker でも動きます）。Private な Source を取得してよいかは、Tool Broker（PAW-031）と個々の Adapter の責任です。

### 実装の由来

`rules.py` の各関数は、仕様（Docstring）と Test（270 件）を先に固定してから実装しています。
**最終的な実装は Claude の参照実装です。** ローカルの Qwen3-Coder-30B-A3B に、14 関数の実装を 2 回（各約 265 回の Tool 呼び出し）任せましたが、収束しませんでした。
1 回目は `query_fingerprint`、`truncate_query`、`fold_for_match`、`normalize_text` の 4 関数が Test を通り、2 回目は `abstract_emails` も通りましたが、正規表現を使う残りの関数（`is_private_host`、`strip_credentials`、`abstract_ids`、`abstract_paths`、`abstract_hosts`、`abstract_urls` など）は Test を通せず、途中で構文エラーや、仕様に反する挙動（Credential を除かずに `[REDACTED]` を残すなど）が残りました。
AGENTS.md のとおり、同じ失敗を繰り返したのでエスカレーションし、仕様の Docstring を保ったまま、Claude の参照実装（変異 176 個のうち 173 個を Test が検出。残る 3 個は同値）に置き換えています。ローカルモデルの成果物は、最終物に含まれていません。`abstract_hosts` と `is_private_host` の Host の書式（IPv6 の Zone ID、末尾の Dot、User 情報、`[]` の無い IPv6）の追加分は、その後に Claude が拡張し、その部分の正規表現と判定に手で入れた変異 40 個のうち 38 個を Test が検出しました（残る 2 個は同値: `[]` の無い IPv6 の `:` の後が空でもよいか、と、空の Host の判定が `%` の判定の前にあるか。どちらも結果が変わりません）。
上の「Test を通り」は、当時の仕様のことです。`fold_for_match` と `find_copied_spans` の当時の仕様（1 文字ずつ `lower()`、長さは変わらない）は、`ß` と `SS` などを別の文字として扱う誤りがあったため、独立レビューを受けて、完全な Case folding（`str.casefold`、長さが増えることがある）に改めました。Docstring と Test は新しい仕様に合わせて更新し、現在の実装はその仕様に対する Claude の実装です。

### 制限と未確認の点

- **検出できるのは Text の写しだけです。** 言い換え、翻訳、Base64 以外の符号化、文字を分けて送る方法、Context に載っていない情報は検出できません。Prompt Injection を受けた Agent に対する完全な防御ではありません。
- ラベルは呼び出し側が付けます。付け忘れた Private な Text は、`PUBLIC` として扱われます（ラベルを付けない場合は拒否されます）。
- Private な Host の判定は構文だけです（IP、`localhost`、Dot の無い名前、末尾が `local` `internal` `lan` `home` `corp` `intranet` `localdomain` `private` `arpa`、`%` を含む名前。`%` は IPv6 の Zone ID（`fe80::1%eth0`、URL では `%25eth0`）で、DNS の名前には無いので、公開名とはみなしません。IPv4 に Zone は無いので `1.2.3.4%eth0` のような組み合わせも同じ扱いで、`%` を含む名前は、`%` の前が何であっても Private として消します）。`git.example.com` のような、外から見ると普通の名前の Private な Host は判定できず、URL の Host としては残ります。Dot の無い名前は、単語との区別がつかないため、URL の外では残ります。
- URL の外の Host は、空白で区切られた 1 語の全体が Host（User 情報、Port、Path を除く）である場合だけ消えます。`host=db.internal` や `db.internal,port=5432` のように他の文字と続いている語、`_` や ASCII 以外の文字を含む名前（`my_db.internal`、`データ.corp`）、Zone ID に `/` や `[` `]` を含むもの（`[fe80::1%a/b]:80`）は 1 語の Host として認識できず残ります（URL の中の Host は `urlsplit` が解析するので、名前の文字の制限はありません）。
- `%` を含む名前は、URL の外でも 1 語の全体が次のどちらかの形であれば Host として認識し、`is_private_host`（`%` を含む Host は Private）に渡して消します。(1) Dot で区切った 2 つ以上の Label（`A-Z a-z 0-9 _ -`、`%` と 16 進数 2 桁の符号化を含んでもよい）。Dot を表す `%2e`（と、もう一度符号化した `%252e`）は Dot として扱います（`db.%69nternal`、`db%2einternal`、`10.0.0.%31`）。(2) (1) の後に `%` と Zone ID（`A-Z a-z 0-9 . _ ~ % -`）が付いたもの（`db.internal%eth0`、`1.2.3.4%25eth0`、`db.internal%`）。Zone ID の前の名前には ASCII の英字が 1 つ以上要ります（`%` 符号化の 16 進数の字も数えます。数字だけの点線 4 つ組の IPv4 は除く）。`3.5%`、`12.5%off`、`3.5%increase` のような割合を Host にしないためです。ここでは Port（`:5432`）、Path、User 情報も付けられます。**残るもの**: Dot の無い語（`100%`、`50%off`、`%eth0`、`db%eth0`、`C%2B%2B`、`hello%20world`）、`%` の後が 16 進数 2 桁でも Zone ID でもない Label（`db.%zzinternal`、`%.2f`、`%s.%d`）、空の Label（`db..%69nternal`、`db.%2einternal`）、3 回以上符号化した Dot（`%25252e`）、`_` を含む名前の Path にだけ `%` があるもの（`my_db.internal/a%20b`。`%` を含む Host ではないので、上の `_` の制限のまま残ります）。**過剰に消すもの**: `%` 符号化を含む Dot 付きの名前は、Host でなくても消えます（`my%20file.txt`、`report%202024.pdf`）。`%` が Private な Label のどの文字でも隠せるためで、Decision 0010（Approved）が「過剰に消す方向」として承認しています。Regex は 2 段で、名前を貪欲に 1 回だけ読み（`%2e` は Label の文字にならないので曖昧さがなく、線形時間）、Zone ID は文字クラス 1 つです（`test_the_host_rules_take_linear_time_on_one_very_long_token` が 40 万文字の 1 語で確かめます）。`[]` の無い IPv6 は `ipaddress` が受理する書式だけを消すので、`12:30`、`aa:bb:cc:dd:ee:ff`（MAC）、`std::vector` は残りますが、`a::b` のように IPv6 として有効な語は消えます。語末の `:` は先に句読点として取り除くので、`::` で終わる圧縮形（`fd00::`、`2001:db8::`）は、取り除いた部分が `::` で始まり、取り除いた後の語が空でないときに限り、`::` を付け直した形を 1 回だけ IPv6 として調べます（`fd00::.`、`(fd00::)` も消えます。`::` を付け直すのは 2 個までで、線形時間です）。`note:`、`fd00:`、`10:30:`、`std::` と、前の語が無い `::` だけの語は残ります。この形の語も、有効な Address であれば消えるので、16 進数の字だけで作れる語（`Bad::`、`Face::`）も消えます。この過剰な除去は、Decision 0010（Approved）で Human が承認しています。
- **Credential を先に消すので、写しの検出は Credential を除いた Draft に対して行います。** Draft が Private な Context の 1 続きの写しで、その途中に Credential があると、Credential の前後に分かれた 2 つの写しが、それぞれ窓（16 文字、`SECRET` は 4 文字）より短いと、その断片は写しとして検出できず残ります。断片は窓の長さ未満で、Credential 自体は残りません。
- **写しが触れた語は全体を消します**（上の手順 4）。書き換えられる語は「1 続きの文字」単位で判断するので、URL の Path の一部だけが Context と共通でも、URL の Host まで消えます（Private な Context から来た URL とみなします）。この過剰な除去は、Decision 0010（Approved）で Human が承認しています。
- **IPv6 の Network（CIDR 形式）は、`[]` の有無にかかわらず消します**（Decision 0010）。Address（`ipaddress` が受理するもの。Zone ID があってもよい）、`/`、1 桁以上の ASCII 数字が、語の全体であるときです（`fd12:3456:789a::/48`、`fd00::/8`、`fe80::1%eth0/64`、`::/0`、`::1/128`、`[fd12::/48]`、`[fd12::]/48`、`[fd12::/48, fd00::/8]` の各項目）。以前は、`/` が 1 つで Path の規則に当たらず、`[]` が無いので Host の規則にも当たらず、`fd12:3456:789a::/48` が外へ出ていました。**Prefix 長の範囲（0〜128）は検査しません**: `fd12::/129` のように不正でも、Address が分かるので消します（IPv4 の `10.0.0.0/8` を消すのと同じ方向です）。`/` の後が数字だけでない語（`a::b/c`、`fd12::/`、`fd12::/48x`、`fd12::/4/8`）は Network ではないので残し、Address でない語（`10:30/12:00`、`aa:bb:cc:dd:ee:ff/48`、`std::a/2`、`3:2/5`）、日付（`2020/01/02` は Path の規則が消すことがあります）、分数も残ります。`--subnet=fd12::/48` のように他の文字と続く語と、`["fd12::/48"]` のように `[` の内側に引用符がある語は、1 語の Host として認識できず残ります（`[` は IPv6 の Literal のために取り除かないので、IPv4 の `[10.0.0.0/8]` のように `[` で始まる語も、以前から残ります）。Regex は文字クラスが 1 つの `:` で分かれる形で、線形時間です（`test_the_host_rules_take_linear_time_on_one_very_long_token` が 40 万文字の 1 語で確かめます）。
- 4 文字の窓は、`SECRET` に近い普通の語（`internal` の `nter` など）も消します。Query が読めなくなることがあります。
- 写しの検出は文字の並びの比較です。Case folding は Unicode の 1 文字ずつの対応（`str.casefold`）だけで、言語ごとの規則（トルコ語の `I` と `ı` など）、発音が同じ別の綴り、アクセント記号の有無の違い（`e` と `é`）は同じとはみなしません。
- 日付（`2026/09/24`）など、規則に当たる正当な語も消えます（過剰に消す方向に倒しています）。
- 処理は同期で、CPU を使います。上限（Draft 2,000 文字、Context 合計 400,000 文字。書かれたままの文字数と、NFKC・Case folding 後の文字数の両方）で処理量を抑えていますが、Event Loop の上で動きます。展開する Text（U+FDFA など）は、展開後の文字数で拒否するので、窓の集合は作られません。上限ぎりぎりの Context は、通常の Text でも、Event Loop を数十ミリ秒から 0.1 秒程度止めます（この Repository の開発機で測った目安で、保証する値ではありません。`test_a_context_that_expands_past_the_limit_is_refused_before_any_text_work` は 1 秒未満だけを確かめます）。
- Audit の Sink は、メモリ上の Test 用（`InMemoryExternalSendAudit`、最大 1,000 件で満杯になると拒否する）だけです。永続化と Audit Log への接続は、後続の Issue [#87](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/87) です。それまで、Private 由来の Context を Research へ自動で入れる設計は有効にしません（Decision 0010 の承認時の条件）。
- `Fingerprint` は塩なしの SHA-256 です。Query が Public な内容になっていることが前提です。
- 数値と規則は [Decision 0010](../../docs/decisions/0010-research-privacy-filter-policy.md)（Approved、2026-09-25）にまとめています。数値は暫定値として承認されたもので、変更する場合は新しい Decision から `Supersedes` します。

### Test

`apps/backend/tests/test_privacy_*.py` です。標準 `unittest` だけで、DB も Network も使いません。
`test_privacy_rules_text.py`、`test_privacy_rules_abstract.py`、`test_privacy_rules_properties.py` は `rules.py` の各関数を、`test_privacy_gate.py` は Gate（拒否、最終検査、Audit の失敗。Class 名に Credential や改行を入れた例外、Metaclass の `__name__`・`__getattribute__`・`__hash__`・`__eq__` が例外になる Class を Sink が上げても、Log の型が固定の `adapter_error` で、拒否が `audit_failed` のままであること）を、`test_privacy_broker.py` は `ResearchBroker` への差し込みを、`test_privacy_contract.py` は値の検証を確かめます。
Timeout の Test は、永遠に待つ Sink を 0.2 秒で打ち切り、成功する経路は即座に終わる構成です（30 秒の Guard で CI の停止を防ぎます）。

## Evidence / Claim Provenance

[PAW-052](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/44)（Revision `0052`）で実装しました。
`paw_backend/research/provenance/` は、Research の結果について「どの主張（Claim）を、どの出典（Source）が支持または否定するか」と、
「どの回答・Task がその Claim を使ったか」を **Project ごとに** 残します（[要件](../../REQUIREMENTS.md)の「Evidence / provenance」）。
Research Scratch（24 時間 TTL）とは別の Table で、Long-term Memory とも Foreign Key でつながりません。Source の本文は保存せず、Hash だけを持ちます。
**HTTP の Endpoint はありません。** `ProvenanceStore` は `TaskService`、`ScratchStore` と同じく認可を行わず、権限の確認は呼び出す側（API 層）の仕事です。
選んだ規則のうち要件にないものは [Decision 0011](../../docs/decisions/0011-research-provenance-model.md)（Approved、2026-09-25 に Human が承認）にまとめています。

| ファイル | 内容 |
| --- | --- |
| `models.py` | 6 つの Table の Model（`SourceRow`、`ClaimRow`、`ClaimSourceRow`、`ClaimUseRow`、`ClaimRelationRow`、`SourceRelationRow`） |
| `records.py` | 入力（`SourceInput`、`SourceLinkInput`、`Reference`。作った時点で検証する）と返す値（`Source`、`Claim`、`SourceLink`、`Relation`、`TracedClaim`、`RecordedClaim`、`Trace`） |
| `validation.py`、`errors.py`、`limits.py` | 引数の検証（DB を使わない純粋関数）、型付きの Error、上限 |
| `rules.py` | 純粋な規則（正規化と Fingerprint、組の順序、Source の統合、追跡結果の組み立て） |
| `queries.py` | DB の文（1 つの関数が 1 つの仕事） |
| `mapping.py` | 行から値への変換 |
| `store.py` | `ProvenanceStore`（Clock は注入）: 検証、Transaction、Lock、Error の変換 |

### Table

| Table | 内容 |
| --- | --- |
| `research_sources` | 取得した Source。`(project_id, locator, content_hash)` で 1 件。`locator` は `canonicalize_locator`（PAW-051）の結果、`source_type`（`SourceType`）、`title`（300 文字まで）、`fetched_at`、`published_at`（不明なら NULL）、`created_at` |
| `research_claims` | Claim。`(project_id, text_fingerprint)` で 1 件。`claim_text`（2000 文字まで）、`task_id`（`tasks.id` への Foreign Key、`ON DELETE SET NULL`）、`created_by`、`created_at` |
| `research_claim_sources` | Claim と Source の対応。`(claim_id, source_id)` が Key、`stance`（`supports` / `contradicts`） |
| `research_claim_uses` | 回答・Task が Claim を使った記録。`(claim_id, ref_kind, ref_id)` が Key（`ref_kind` は `answer` / `task`） |
| `research_claim_relations`、`research_source_relations` | Claim 同士・Source 同士の対称な Relation `duplicate` / `contradiction`。`low_id < high_id` の組で 1 回だけ持ち、1 組に 1 つ |

- **Project をまたがない。** 対応・使用・Relation の Table は `project_id` を持ち、Claim と Source を複合 Foreign Key `(id, project_id)` で参照します。Application が間違えても、2 つの Project の行を結ぶ行は DB が拒否します。
  全ての Method は `project_id` を受け取り、その Project の中だけで探します。他の Project の ID は「存在しない」と同じ扱いです。
- `project_id`、`created_by` は素の UUID です（projects と users の Table がまだありません）。回答の ID（`ref_id`）も、Answer の Table がないため素の UUID で、存在は確認しません。
- **不変。** Application の Role は 6 つの Table に SELECT と INSERT だけを持ちます（UPDATE も DELETE もできません）。誤りは書き換えではなく、新しい記録で訂正します。

### 記録と重複

`record_claim(project_id, *, created_by, text, sources, task_id=None)` は 1 つの Transaction で次を行います（どれか 1 つでも失敗すれば全て取り消します）。

1. `task_id` があれば、その Project の Task であることを確認します（なければ `InvalidProvenanceInputError("task_id", UNKNOWN_REFERENCE)`）。
2. Claim: 正規化した本文（NFKC、大文字小文字の畳み込み、空白の連続を 1 つに）の Fingerprint が同じ Claim がその Project にあれば **それを使い**（`created=False`。最初の本文・作成者・Task・時刻が残る）、なければ作ります。
3. Source: `(locator, content_hash)` が同じ Source があればそれを使い（最初の記録が残る）、なければ作ります。同じ URL でも Hash が違えば別の Source です。
4. 対応: Claim と Source を Stance 付きで結びます。同じ Stance の再記録は何もせず、逆の Stance は `ProvenanceConflictError` です。
5. Claim の Source は合計 50 件まで（`ProvenanceLimitError`）。1 回の呼び出しは 1〜20 件です。
6. `task_id` があれば、その Task を Claim の利用者として登録します。

`SourceInput.from_metadata(metadata)` は PAW-051 の `SourceMetadata` から Source を作ります（`retrieved_at` が `fetched_at` になり、Provider の種類・ID と `private_source` は記録しません）。
同じ呼び出しを 2 回しても、2 回目は何も変えません（`created=False`、`new_links=0`）。1 回の呼び出しの中で同じ Source が 2 回出てきたら 1 件に統合し（最初が残る）、Stance が食い違えば `sources` / `CONFLICT` です。

### duplicate / contradiction

`mark_related(project_id, *, entity, kind, first_id, second_id, created_by)` は、2 つの Claim（または 2 つの Source）に `duplicate`（同じことを言う）か `contradiction`（両立しない）を張ります。

- 対称です。`(a, b)` と `(b, a)` は同じ Relation で、`low_id < high_id`（`UUID.int` の順）で 1 回だけ保存します。
- 1 組に Relation は 1 つ。同じ種類の再記録は最初のものを返し、逆の種類は `ProvenanceConflictError` です。自分自身との Relation は `SELF_REFERENCE` です。
- 推移律は扱いません（A と B、B と C が `duplicate` でも、A と C は張らない限り無関係です）。
- 表現の違う Claim を自動で `duplicate` にはしません。Fingerprint が同じ場合だけ、記録の時点で 1 つの Claim にまとまります。
- `list_relations(project_id, entity, entity_id)` は、その Claim（または Source）の Relation を返します（`contradiction` が先、次に相手の ID の順）。

### 回答・Task からの追跡

- `add_reference(project_id, *, reference, claim_ids, created_by)` は、回答（`Reference.answer(id)`）または Task（`Reference.task(id)`）が Claim を使ったことを記録し、新しく記録した数を返します（1〜50 件、重複は 1 件）。Task の参照は、その Project の Task でなければなりません。
- `trace(project_id, reference, *, limit=100)` は、その参照が使った Claim と、それぞれの Source（`source_type`、`fetched_at`、`published_at`、Stance）と Relation を返します。
  Claim は `(created_at, id)` の順、1 つの Claim の Source は「支持が先、取得の新しい順、ID の順」です。
  `limit` は 1〜200 で、超える Claim があれば `truncated` が True です。`Trace.sources` は、全 Claim の Source を重複なく、現れた順に並べます。
  誰も使っていない参照、存在しない参照、他の Project の参照は、区別なく空の Trace です。1 つの Snapshot（`REPEATABLE READ`、読み取り専用）で、Lock を取りません。
- `get_claim(project_id, claim_id)` は 1 つの Claim を、同じ形（`TracedClaim`）で返します。

Claim を記録した Task は自動で利用者になるので、`trace(project_id, Reference.task(task_id))` でその Task の Claim と Source を辿れます。回答から辿るには、回答の Claim を `add_reference` で登録します。

### 同時実行と権限

`store.py` の冒頭にも書いています。

1. 同じ本文の Claim を同時に記録すると、一意制約で 2 番目以降は待ち、既存の Claim に Source を足します（`created=True` は 1 つだけ）。
2. Claim ができた後、その Claim の Advisory Lock（Transaction の間）を取ってから Source を結びます。同じ Claim への同時の追加は順に実行され、50 件の上限を超えません。
   Row の Lock（`FOR UPDATE`）は使いません。Application の Role に UPDATE がなく、`FOR UPDATE` と `FOR KEY SHARE` は UPDATE 権限を要するためです。
3. Source は `(locator, content_hash)` の順に作ります。重なる Source を逆順に持つ 2 つの呼び出しが、互いを Deadlock させません。
4. 書き込みの Transaction は `SET LOCAL lock_timeout`（`lock_timeout_ms`、既定 5000）で始まります。待ちが超えた場合と、DB が Deadlock を解消した場合は `ProvenanceBusyError` です（取り消し済み、再試行できます）。
5. 読み取り（`get_claim`、`trace`、`list_relations`）は Lock を取らず、待ちません。

**Application の Role の権限。** 共通の `grant_app_privileges`（PAW-025）で、6 つの Table に SELECT と INSERT だけを付けます（UPDATE の列も付けません）。
`test_provenance_grants.py` は、この Role で Store と Query の Test を全て実行し、権限の一致と、書き換え・削除・`TRUNCATE`・`ON CONFLICT DO UPDATE`・Schema の変更・Project をまたぐ対応の拒否を確かめます。

### 上限と入力の検証

上限は Decision 0011 で暫定値として承認された値で、定数として変更できます。Claim の本文は 2000 文字（Unicode の Code Point）、Source の `title` は 300 文字、Locator は正規化後 2048 文字です。1 回の `record_claim` は Source 20 件、`add_reference` は Claim 50 件、Claim あたりの Source は合計 50 件、`trace` は Claim 200 件までです。
型は変換しません（`"supports"` は `Stance` でなく、`bool` は `int` でなく、UUID の文字列は UUID でなく、Naive な日時は UTC でありません）。NUL と UTF-8 にできない文字は拒否します。
全ての要素を検証してから統合します。時刻は全て aware で、UTC の同じ瞬間に変換します。
Error の Message は固定文字列（Field 名と理由の語彙）で、入力の内容（本文、Locator、ID）・DB の Message を含みません。DB の Error（接続断など）は、Lock の待ち超過と Deadlock 以外は加工せず伝わりますが、SQL の引数を含みうるため、呼び出し側は `str(error)` を User へ見せないでください。

### 呼び出し側の認可

Endpoint は次の Issue の仕事です。次の対応を [Decision 0011](../../docs/decisions/0011-research-provenance-model.md) で承認しました（Backend では未強制）。読み取り（`get_claim`、`trace`、`list_relations`）は `project.read`。`record_claim`、`add_reference`、`mark_related` は `project.task.run`。

### 実装の由来

`rules.py`（純粋な規則）と `queries.py`（DB の文）は、仕様（Contract と Test）を先に固定し、その Test だけを合格基準にして書く前提の Module です。
それ以外（Model、Migration、`validation.py`、`records.py`、`store.py`、Test）は仕様の側が書いています。
**最終的な実装は Claude の参照実装です。** ローカルの Qwen3-Coder-30B-A3B に、16 関数の実装を 2 回（各約 265 回の Tool 呼び出し）任せましたが、収束しませんでした（1 回目は SQL の文の構文を壊し、2 回目も構文エラーが残り、全 Test の Module が Import できない状態でした）。
AGENTS.md のとおり、同じ失敗を繰り返したのでエスカレーションし、仕様の Docstring を保ったまま、Claude の参照実装（変異 76 個のうち 75 個を Test が検出。残る 1 個は同値）に置き換えています。ローカルモデルの成果物は、最終物に含まれていません。

### 制限と未確認の点

- Migration `0052` の `down_revision` は `0046` です（鎖は `0001 → 0025 → 0032 → 0040 → 0021 → 0033 → 0031 → 0050 → 0046 → 0052`）。
- 回答・Task から Claim への向きだけを引けます。「この Source を使った回答」への逆引き（Source が古くなったときの影響調査）はありません。
- Source の `private_source`、Provider、License、Claim の `confidence` は記録しません（[Decision 0011](../../docs/decisions/0011-research-provenance-model.md)）。
- 削除・保持・Project 削除時の扱いはありません（Application は削除できません）。Project 削除時の Provenance の扱い（消す・残す・匿名化する）は、Decision 0011 で今は決めないとし、Issue [#88](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/88) で別の Decision にします。1 Project あたりの件数の上限（Quota）もありません。
- `published_at` が `fetched_at` より後でも拒否しません（ページの日付は不正確なことがあるため）。
- Claim の Fingerprint は、正規化した本文が同じかだけを見ます。同じ意味の別の表現は、`mark_related` で明示します。
- Advisory Lock の Key は Claim の ID の Hash（64 bit）です。衝突すると無関係な 2 つの Claim が互いを待ちますが、結果は正しいままです。
- Deadlock が実際に起きた場合の `ProvenanceBusyError` への変換は、Driver の Error を作る Test で確認しています。実際の Deadlock を起こす Test はありません（順序を固定して起きないようにしています）。

### 承認済みの点と、残る判断

[Decision 0011](../../docs/decisions/0011-research-provenance-model.md) は 2026-09-25 に承認されました（「承認時の決定」を参照）。
上限（本文 2000 文字、Source 50 件など）は暫定値として承認されており、定数なので変更できます。
Project 削除時の Provenance の扱いは、Issue [#88](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/88) で別の Decision にします。

### Test

`apps/backend/tests/test_provenance_*.py` と `provenance_support.py` です。標準 `unittest` だけです。
DB を使わない Test（`records`、`validation`、`rules`、`store_validation`、`store_wiring`、`migration` の一部）と、実 PostgreSQL の Test（`PAW_TEST_DATABASE_URL` が未設定なら Skip）があります。
期待値は SQL で用意して SQL で確かめ、Store の別の Method には頼りません。並行の Test は、別の接続で Lock を持たせて「待っている」状態を確かめ、機械の速さに頼りません。

## Project CRUD / Membership / Lifecycle

[PAW-026](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/23)（Revision `0026`）で実装しました。設計は [要件](../../REQUIREMENTS.md)の「Project roles and membership」「Project lifecycle」「New Project defaults」と
[Decision 0004](../../docs/decisions/0004-rbac-capability-and-audit-policy.md)（承認済み）に従い、要件が決めていない選択は [Decision 0008（承認済み）](../../docs/decisions/0008-project-membership-and-lifecycle-policy.md)にまとめています。
**Decision 0008 は 2026-09-25 に Human が承認しました。** 招待の期限（14 日）、Member と招待の合計（200）、Project 名と説明の長さ（1〜100 文字、2,000 文字）は暫定値として承認されました。Project 名と説明の長さは DB の CHECK 制約にも書かれているため、変えるには新しい Migration と `models.py` の変更が要ります（`limits.py` の定数だけでは足りません）。Member と招待の合計は `limits.MAX_MEMBERS_PER_PROJECT` で、招待の期限は `domain.invite_expiry` で決まり、どちらも Migration は要りません（詳しくは Decision 0008 の「背景」）。
**HTTP の Endpoint はありません**（Session は PAW-022）。`ProjectService` は、認証済みの `Principal` を受け取り、`Authorizer` で判定します。

| ファイル | 内容 |
| --- | --- |
| `models.py` | `projects`、`project_members`、`project_task_stops` の Model |
| `limits.py` | 上限と時間の定数（名と説明の長さ、30 日は DB の CHECK 制約と一致することを Test が検証。招待の期限 `INVITE_TTL` は記録だけで、実際の値は `domain.invite_expiry`） |
| `records.py`、`errors.py` | 返す値（`Project`、`Member`、`PendingInvite`、`PurgeResult`）、状態の Enum、型付きの Error |
| `validation.py` | 引数の検証（DB を使わない純粋関数） |
| `domain.py` | Lifecycle と Membership の規則（純粋関数。状態遷移の表、30 日、招待の期限、最後の Manager） |
| `store.py` | SQL（1 文 1 関数。Transaction・Lock・Timeout・認可は持たない） |
| `service.py` | `ProjectService`（Transaction、Lock、認可、規則の組み立て。Clock は注入） |
| `task_stop.py` | `ProjectTaskStopper`: Delete 開始で記録された Task 停止の要求を実行する Processor（[Delete 開始時の Task 停止](#delete-開始時の-task-停止outbox-と-processor)） |
| `transaction.py` | Lock Timeout 付きの Transaction（`ProjectService` と `ProjectTaskStopper` が共有する） |

### Table

| Table | 内容 |
| --- | --- |
| `projects` | Project 1 件。`name`（1〜100 文字、前後に空白なし）、`description`（なし、または 1〜2000 文字）、`status`（`active` / `archived` / `pending_deletion` / `deleted`）、`created_by`、`created_at`、`updated_at`、`deletion_started_at`、`deletion_scheduled_at`、`deleted_at` |
| `project_members` | 受諾済みの Member（`status = 'active'`）か招待（`status = 'invited'`）。`(project_id, user_id)` が Key で、1 人が 1 Project に持てる行は 1 つ。`role`（`manager` / `contributor` / `viewer`）、`invited_at`、`invite_expires_at`、`joined_at` |
| `project_task_stops` | 「この Project の Task を止める」要求（Outbox）。`project_id` が Key で 1 Project に 1 行。`requested_at`、`processed_at`（未処理の間は `NULL`）。`begin_deletion` が Project の状態を変える**同じ Transaction で**書く。部分 Index `ix_project_task_stops_open`（`processed_at IS NULL`）が未処理の要求を古い順に引く |

- **Repo なしで作れます。** `projects` に Repository の列はありません（Repo の紐付けは PAW-027）。作成には Project 名だけが要り、作成者が最初の Manager になります。
- **Pending deletion は 30 日。** `deletion_scheduled_at = deletion_started_at + interval '720 hours'` を CHECK 制約で強制します（`interval '30 days'` はセッションの Time Zone の暦日で、夏時間の切り替えで 1 時間ずれるため時間で書きます）。
- **削除しても行は残る。** `projects` は DELETE しません（Application の Role にも DELETE を与えていません）。Deleted は墓石で、名前は `Deleted Project`、説明は消去、`id`・作成者の不透明な ID・時刻は残します。
- CHECK 制約: 状態と時刻の対応（`deletion_*` は Pending deletion と Deleted のときだけ、`deleted_at` は Deleted のときだけ、招待の期限は招待のときだけ、`joined_at` は受諾済みのときだけ）を DB が強制します。
- **Foreign Key**: `projects.created_by → users.id`（`ON DELETE SET NULL`）、`project_members.project_id → projects.id`（`ON DELETE CASCADE`）、`project_task_stops.project_id → projects.id`（`ON DELETE CASCADE`）、`project_members.user_id → users.id`（`ON DELETE RESTRICT`: Member や招待のある User は物理削除できません。User の削除の流れは先に Member を外します）。
  **`users`（Revision `0021`）が鎖の前にあることが必要です。**
- Migration `0026` の `down_revision` は `0052` です（鎖は `0001 → 0025 → 0032 → 0040 → 0021 → 0033 → 0031 → 0050 → 0046 → 0052 → 0026`）。`0021`（`users`）が `0026` より前にあることが必要です（`project_members` が `users` を参照します）。
- **Application Role の権限**（`grant_app_privileges`、`test_projects_grants.py` が実 DB で確認）: `projects` は SELECT / INSERT と、UPDATE は `name`・`description`・`status`・`updated_at`・`deletion_started_at`・`deletion_scheduled_at`・`deleted_at` の 7 列だけ（DELETE なし。`id`・`created_by`・`created_at` は書き換えられません）。
  `project_members` は SELECT / INSERT / DELETE と、UPDATE は `role`・`status`・`joined_at`・`invite_expires_at` だけです。
  `project_task_stops` は SELECT / INSERT と、UPDATE は `requested_at`・`processed_at` だけです（DELETE なし。要求は「いつ Task に停止を求めたか」の履歴で、`project_id` も書き換えられません。再度の Delete 開始は同じ行への `INSERT ... ON CONFLICT DO UPDATE`、Processor は `processed_at` の更新）。TRUNCATE と Schema の変更は誰にも与えません。`users` は読むだけです（`0021` の権限）。
  停止の Processor が読む `tasks`、Task を止める `TaskService` と `TaskQueue` の権限は、PAW-032 / PAW-033 の Migration のものです（`test_projects_grants.py` が Processor の Test も同じ Role で実行し、足りることを確認します）。
  Migration に加えて、`tests/test_projects_grants.py` は Service の Test を全てこの Role で実行します。
- `0026` の FK のうち 3 つは `ALTER TABLE ... ADD CONSTRAINT` の手書きの文で付けています。既存の Test（`test_task_persistence.OfflineMigrationTest`）が、鎖全体の SQL に `FOREIGN KEY(project_id)` の文字列がないことで「`tasks` に外部キーがない」ことを確かめているためです（上の 3 つは `projects` の Key で、`tasks` とは無関係）。統合時にその Test を `tasks` の DDL に絞ることを勧めます。

### Lifecycle

```text
Active ⇄ Archived
   ╲        ╲
    ╲        ╲ Delete 開始（確認: Project 名の入力）
     └────────→ Pending deletion（30 日）──→ Deleted（Purge。墓石）
                     │
                     └── 復元（30 日以内）→ Archived
```

| 操作 | 許可する人（`project.lifecycle.manage`） | 遷移 |
| --- | --- | --- |
| `archive` | Manager、Owner、Admin | Active → Archived |
| `unarchive` | 同上 | Archived → Active（**Archived からの復帰**） |
| `begin_deletion(confirm_name)` | 同上 | Active / Archived → Pending deletion。`deletion_started_at = now`、`deletion_scheduled_at = now + 30 日`。**同じ Transaction で** Task 停止の要求（`project_task_stops`）を書く（下記） |
| `restore` | 同上 | Pending deletion → **Archived**（`now < deletion_scheduled_at` の間だけ。Manager が 1 人もいなければ拒否） |
| `purge_expired(now)` | Backend の Janitor だけ（Actor なし） | `now >= deletion_scheduled_at` の Pending deletion → Deleted。Member と招待の行を全て削除 |

- 遷移の表は `domain.plan_transition` にあり、`tests/test_projects_domain.py` が 5 操作 × 4 状態の全 20 通りを固定しています。**すでにその状態にある操作は成功して何も書きません**（`updated_at` も 30 日も動かない）。それ以外の組み合わせは `IllegalTransitionError` です。
- Archived は読み取り専用（Policy: `project.read` と Lifecycle だけ）、Pending deletion は Lifecycle だけです（Member のアクセスは止まります）。Deleted は全ての操作で「存在しない」です。
- Delete 開始は `confirm_name` が Project 名と**完全に一致**しなければ `ConfirmationMismatchError`（認可の後に検査するので、権限のない人には名前の一致を教えません）。
- Purge は **`now >= deletion_scheduled_at`** から（復元は `now < deletion_scheduled_at`。同じ瞬間に両方が真にはなりません）。1 回の Transaction で、期限の古い順に最大 `batch_size`（1〜500、既定 50）件を `FOR UPDATE SKIP LOCKED` で選び、各 Project の Member と招待を全て削除して墓石にします。
  Lock 中の Project は待たずに飛ばし、`has_more` は「まだ期限の来た Project が残っている」（Lock 中を含む）ことを示します。二重に呼んでも安全です。Scheduler は含みません（別の Issue）。
- **他の領域のデータは、この Issue では消しません。** Chat、Memory、Task、Repo の紐付け、調査結果の `project_id` は素の UUID で、各領域の Service が `PurgeResult.purged` の ID を使って消します（各 Issue へ引き継ぎます。調査結果の扱い（消す・残す・匿名化）は Issue [#88](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/88) で決めます）。
  GitHub、Local checkout などの外部資源は消しません（要件）。

### Delete 開始時の Task 停止（Outbox と Processor）

要件は Delete 開始の時点で「実行中Taskをsafe-stop」「新規Agent Task停止」と定めます（[Decision 0008](../../docs/decisions/0008-project-membership-and-lifecycle-policy.md) の 8。2026-09-25 に承認）。
`ProjectService.begin_deletion` は Project の行を更新するだけでは Task を止められない（Task Service と Queue は別の部品で、外部の Worker は Transaction の中から止められない）ため、2 段で実現します。

1. **要求（同じ Transaction）。** `begin_deletion` が、Project の状態を変える Transaction で `project_task_stops` の行を書きます（`requested_at = now`、`processed_at = NULL`）。両方が Commit されるか、どちらも Commit されません（Test は要求の書き込みを失敗させ、Project が Active のまま残ることを確認します）。
   何も書かない再度の呼び出し（すでに Pending deletion）と、失敗した Delete 開始（確認の不一致、権限なし、状態の誤り）は行を書きません。復元してからの再度の Delete 開始は、同じ行を新しい要求にします（`requested_at` を更新し、`processed_at` を消す）。
2. **実行（Processor）。** `ProjectTaskStopper(database, task_service, task_queue)`。Orchestrator（PAW-034 の `ProjectTaskStopLoop`。[別の節](#project-削除の-sweepprojecttaskstoploop)）が、`pending_project_ids()` が返す Project のそれぞれについて `stop_project_tasks(project_id)` を、`TaskStopResult.done` になるまで呼びます。

`stop_project_tasks` の動き。

- Project が **Pending deletion**（または Purge 済みの Deleted）のときだけ、その Project の active な Task（queued / running / waiting / paused / evaluating）を、最大 `batch_size`（既定 100、1〜500）件、古い順に止めます。Active / Archived の Project（復元された、または Delete していない）には何もしません。誤った呼び出しで、生きている Project の Task を止めることはありません。
- 各 Task について、まず PAW-032 の **Cancel** を `TaskService.execute` で発行し（Actor は `policy`、理由は固定文 `Project deletion started`）、Task が終了してから Queue の Entry を `TaskQueue.cancel` します（Claim できなくなり、Lease を持つ Worker は失う）。**Task の状態は直接書きません**（状態遷移表、`task_events` の履歴、Listener が全て適用されます）。
  **Task が先、Entry が後です**（6 回目のレビュー）。2 つは別々の Commit で、間で Process が落ちる、Error になる、Stopper が Cancel される、Restore が Commit される、ことがあります。Entry を先に取り消すと、その隙間に Restore が入った Project の queued の Task は Entry を失い、二度と実行されません（次の実行は復元済みの Project に何もせず、要求を処理済みにします。Entry のない queued の Task は enqueue 前の普通の状態でもあるため、後から見分けられません）。Task が先なら、まだ active な Task は **どの失敗でも Entry を持ち続けます**（Cancel の Error、`TaskConflictError`、Stopper の Cancel、間の Restore）。Entry を Cancel するのは、Task が終了しているとき（この呼び出しの Cancel が終わらせた、または元から終了していた）だけです。残る隙間は「終了した Task と active な Entry」で、Project が削除中なら次の実行の Sweep が拾います。
  隙間の後の中断は、同じ呼び出しの中でも **状態で** 整合させます（`_reconcile_entry`）。Cancel か Entry の Cancel が何かを送出したとき（Cancel、Listener が Commit の後に Cancel されて `execute` が例外になる場合を含む）、Task を 1 回読み、終了していれば Entry を Cancel してから元の Error を再送出します（まだ active な Task と Entry には触りません）。中断の `except` の中で動くので Cancel 1 回では止まらず、`RECONCILE_TIMEOUT_S`（5 秒）で有界です。整合の失敗や時間切れは元の Error を置き換えません。
- **Cancel を使う理由。** Cancel は成果物（branch / worktree / 途中成果）を保持し、Worker は現在の Step を安全な区切りで自分で閉じられます（`finish_step`。新しい Step は始められません）。全ての active な状態で使えます。Stop Now（実行中の Step を即時に中断する緊急停止）は queued と paused には使えず、Pause は復元されない限り Resume されない Task を残します。
- **Queue の Entry も Project で掃除します（Sweep）。** Task の一覧は active な Task だけです。別の呼び出しが Task を Cancel → Restart し、新しい Attempt を enqueue すると、Processor 自身の Cancel が再開された Task を終わらせ、新しい Entry だけが終了済みの Task の後ろに残ります。中断された停止（上）も、Cancel の済んだ Task の Entry を残しえます。Task が終了しているので、Task の一覧では二度と見つかりません。
  そのため、Task の停止の後に、Project の Task のうち **終了している Task** が持つ active な Entry（queued / claimed）を Project で引き（`queue_entries` と `tasks` の Join。最大 `batch_size` 件）、`TaskQueue.cancel` で Cancel します。まだ active な Task の Entry は Sweep では取り消しません（その Task は次の実行で Cancel と Entry の順に止めます。復元されたときに実行できる Entry を残すため）。処理済みになった後に現れた Entry も、再実行の Sweep が拾います。Sweep は先に Project を読み直し、Pending deletion / Deleted でなければ何もしません（復元済みの Project の Entry は残ります）。Queue の状態機械は変えません（既存の `cancel` と読み取りだけ）。終了済みの Task の Entry を Cancel すると、その Entry を持つ Worker は Lease を失います（Project は削除中で、Task の結果は Task の側に残るため許容）。`cancelled_entries` はこの Entry も数えます。
- active な Task が 1 つも残っておらず、Project の Task に active な Queue の Entry も無いことを、Project の行の `FOR SHARE` Lock の下で確認してから `processed_at` を書きます（Project は、その間に復元も再削除もされません）。残っているとき（件数が `batch_size` を超えた、他の書き込みと競合した、Task がまた active になった、Sweep の後に Entry が enqueue された）は要求を開いたままにし、`done` は `False` で、次の実行が残りを止めます。
- **冪等で再実行できます。** 2 回目は何も変えず（`TaskStopResult(project_id, (), 0, done=True)`）、最初の `processed_at` も変わりません。一覧と Command の間に Task が終わった場合（`IllegalTransitionError`）は数えません（Entry が残っていれば Cancel します。Task は終了済みです）。競合（`TaskConflictError`）は Task も Entry もそのまま残し、次の実行に任せます。それ以外の Error は、要求を開いたまま伝わります。
- 認可も Audit も持ちません（`purge_expired` と同じ Backend 内部の部品）。止められた Task には `task_events` の行が残ります。

制限（[Decision 0008](../../docs/decisions/0008-project-membership-and-lifecycle-policy.md) の 8。方針は承認済みで、残る窓は Issue [#83](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/83) で閉じます）。

- **Delete 開始の後に作られた Task と Entry。** `project.task.run` は Archived / Pending deletion で Authorizer が拒否します（PAW-025）。残るのは、認可の後、Delete 開始の Commit の前後に `TaskService.create_task`（または Retry / Restart）や `TaskQueue.enqueue` が実行される競合だけです（`TaskService` も `TaskQueue` も Project の状態を見ません）。
  この Issue は、その Task と Entry（終了済みの Task の Entry も）を **Processor の再実行で止めます**（Processor は Project の状態から動きます。Test: `test_a_task_created_after_the_deletion_began_is_stopped_on_a_rerun`、`test_an_entry_of_a_finished_task_is_found_by_project_on_a_rerun`、`test_a_queue_entry_created_by_a_raced_restart_does_not_survive`）。Orchestrator は Pending deletion の Project にも通常の周期で `stop_project_tasks` を呼んでください。競合そのものを閉じるには、Task Lane（PAW-032 / PAW-034）の `create_task` / Retry / Restart と Queue Lane（PAW-033）の `enqueue` が、Insert と同じ Transaction で Project の行を `FOR SHARE` で Lock し、Active 以外を拒否する必要があります。Decision 0008 の 4 で Human が方針を承認しており、実装は Issue #83（PAW-034 の前後）で行います（この Issue は Task Lane と Queue Lane を変えません）。Orchestrator が削除待ちの Project にも呼ぶことは、PAW-034（[#30](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/30)）の受け入れ条件に追記済みです。
- **Restore との競合。** 1 回の呼び出しは、最大 `batch_size`（既定 100）件の Task（Sweep は Entry）を 1 回の読み取りで一覧してから 1 件ずつ止めるため、その途中で Restore が Commit されることがあります。そこで Processor は **Task 1 件ごと**（Sweep は Entry 1 件ごと）の前に Project を読み直し（Lock のない読み取りが 1 件につき 1 回増えるだけ）、Pending deletion / Deleted でなくなった最初の読み取りで残りを止めます。Restore の後にもう一度 Delete が始まった Project は、状態を見るので止め続けます。
  Restore の前に止めた Task と Entry は止まったままで（Restart できます）、Restore の後の Task と Entry には触りません。復元された Project の要求は意味を失ったものとして処理済みになり、`done` は `True` です。
  **残る窓は、その 1 件の Command が終わるまでです。** Project の読み取りと Command は 1 つの原子的な操作ではないため、読み取りの後、その Task の Cancel と Entry の Cancel が終わるまでの間に Restore が Commit されると、その 1 件（Sweep なら Entry 1 件）は止まります。Cancel が先なので、Cancel が終わらせなかった Task（Error、競合、Stopper の Cancel）は復元された Project で Entry を持ち続けます（Entry を先に取り消すと、復元された Task が Entry のない queued のまま残り、二度と実行されませんでした）。
  閉じるには、Task Service と Queue が Command を Project の行の `FOR SHARE` の下で実行する必要があります（Decision 0008 の 5。Issue #83 で行い、この Issue は両方の Lane を変えません）。Processor が別の Transaction で `FOR SHARE` を持ったまま 2 つの部品を呼ぶ案は採りません。Stopper 1 つにつき Pool の接続を 2 本使い、この Module が時間を制限できない Task Service と Listener が動く間、Restore が待たされる（Lock timeout で `ProjectBusyError`）ためです。Test: `test_a_restore_between_two_tasks_stops_the_batch`、`test_a_restore_after_the_ids_were_listed_cancels_nothing`、`test_a_restore_between_the_cancel_and_the_entry_cancel_finishes_that_task`、`test_a_restore_between_two_stray_entries_stops_the_sweep`、`test_a_restore_after_the_stray_entries_were_listed_cancels_none`、`test_a_project_that_is_deleted_again_keeps_being_stopped`。
- **Cancel と Entry の Cancel の間の隙間（6 回目のレビュー。Decision 0008 の 8）。** 上の順序と整合で、1 つの故障（Error、Stopper の Cancel、Listener の Cancel）はどれも閉じます。閉じないものが 2 つあり、いずれも別 Lane の変更（2 つの Command を 1 つの Transaction にする、条件付きの Queue の Cancel、または Claim が終了済みの Task の Entry を飛ばす規則）が要り、Issue #83 で行います。
  (a) 別の呼び出しが、Stopper の Cancel と Entry の Cancel の間に Task を Restart すると、再開された Task は Entry を失います。Project が削除中なら次の実行が active な Task として止めます（Restore も競合して初めて問題になります。`test_a_restart_between_the_cancel_and_the_entry_cancel_is_stopped_later`）。
  (b) 2 つの Commit の間で Process が落ちる（または整合が失敗する）**うえに** 次の実行の前に Restore が Commit されると、`cancelled` の Task に active な Entry が残ります。Claim した Worker が `start` を拒否される、動かない Entry です。復元された Project では、Stopper は何も触りません（完了する Worker が、完了した Task の claimed の Entry を一瞬持つため）。Task 自体は「1 件は止まる窓」（上）と同じで、Restart できます（整合が失敗して Entry が残る場合: `test_a_failing_reconciliation_does_not_hide_the_original_error`）。
- `tasks.project_id` に Index がないため（PAW-032）、Task の一覧は `tasks` の Sequential Scan です。Task 数が増えたら、Task Lane で `(project_id, state)` の Index を足してください。
- 実行中の Process への停止の伝達は Orchestrator / Worker の責務です。Cancel は状態を `cancelled` にして Lease を失わせますが、Process を殺しません（Worker は Step の区切りで状態を見て止まります）。

### Membership（招待制）

- **誰も自分では入れません。** Manager が `invite_member(project, user, role)` で招待し（存在して `active` の User だけ）、招待された人が `accept_invite` で受諾して Member になります。`decline_invite` は行を削除します。System の Owner / Admin も、招待されなければ入れません。
- 招待は **14 日**（`domain.invite_expiry`）。`invite_expires_at` ちょうどからは受諾できません（`InviteExpiredError`）。期限切れの招待は、再度の `invite_member` で置き換わります。有効な招待がある人、Member の人への再招待はエラーです（期限を更新しません）。
- 招待された人は `list_my_invites` で自分宛ての有効な招待を見られます（Project 名、Role、期限）。受諾するまで、Project も Member 一覧も見えません（`roles_of` にも入りません）。
- Manager は `remove_member`（招待の取り下げも）、`change_role` で Member を管理します。**最後の受諾済み Manager は、退出・削除・降格できません**（`LastManagerError`。招待中の Manager は数えません）。
  例外は、Pending deletion の Project からの `leave_project`（削除中なので許可）です。その場合、Manager のいない Project の復元は `NoManagerError` で拒否されます。
- 1 Project の Member と有効な招待の合計は 200 までです（`MemberLimitError`）。
- Member の一覧は Member 全員が見られます（`project.read`）。有効な招待の一覧は Manager だけです（`project.members.manage`）。

### 認可と Audit

| 方法 | 操作 |
| --- | --- |
| `Authorizer`（Capability を Audit に残す） | `get_project`、`list_members`（`project.read`）、`list_invites`、`invite_member`、`remove_member`、`change_role`（`project.members.manage`）、`rename_project`、`set_description`（`project.settings.manage`）、`archive`、`unarchive`、`begin_deletion`、`restore`（`project.lifecycle.manage`） |
| **本人確認だけ（Audit を書かない）** | `create_project`、`accept_invite`、`decline_invite`、`leave_project`、`list_projects`、`list_my_invites`。`system_role` が Owner / Admin / User の `Principal` だけ（`SYSTEM` は拒否）。受諾・辞退・退出は Actor 自身の行だけを対象にします |
| Backend 内部（Actor なし） | `purge_expired`（Janitor）、`roles_of`（`Principal.project_roles` を作る PAW-022 用） |

- **`Principal.project_roles` を信用しません。** Service は、Actor の Role を同じ Transaction で `project_members` から読み直し（受諾済みの行だけ）、それを使って `Authorizer` に渡す `Principal` を作り直します。
  Member から外された後の古い `Principal`、自分で Manager と申告した `Principal`、招待中なのに Manager と申告した `Principal` は、効きません。`system_role` と `user_id` だけを呼び出し側から受け取ります。
- **存在を明かしません。** 認可で拒否され、かつ Actor が受諾済みの Member でないときは、存在しない Project と同じ `ProjectNotFoundError` です（Audit は Authorizer が書きます）。Owner / Admin が Lifecycle 以外を試みた場合も同じです。
  Member への拒否は、状態のため（Archived の変更、Pending deletion の閲覧）は `ProjectStateError`、それ以外は `ProjectPermissionDeniedError`（`reason` は固定の Reason Code。`audit_unavailable` は API 層が 503 にします）です。
- **一覧は自分の Member の行だけ**です（Owner / Admin も同じ）。Pending deletion の一覧は、復元できる Manager の Project だけです。Owner / Admin が全 Project を探す手段はこの Issue にありません（Decision 0008。管理者向けの全 Project 一覧 API は Issue [#84](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/84) で実装します）。
- Audit の行は「判定」を記録します。許可された操作が後から失敗しても（規則の違反、DB の Error）、Audit の行は残ります。
- `Scope.SELF` の Capability（`chat.use`、`memory.use` など）は、今も Member 資格と Project の状態を見ません。Membership の Table と `roles_of` を用意しただけで、絞り込みは Project の Chat や Memory を実装する Issue が行います。

### 同時実行

- Project を変える操作（設定、Member、Lifecycle、Purge）は、Transaction の最初に **Project の行を `SELECT ... FOR UPDATE`** で Lock し（待ちます）、それから存在・認可・規則を評価します。1 Project の変更は直列になり、
  2 人の Manager が同時に退出しても、最後の 1 人は残ります（`tests/test_projects_concurrency.py`）。Lock 待ちは `lock_timeout_ms`（既定 3000、1〜60000）で `ProjectBusyError` になります。読み取りは Lock も待ちもしません。
- `Authorizer` の呼び出しは、この Lock を持ったまま行います（Audit の書き込みは Authorizer の Timeout で有界）。
- Task 停止の Processor は、Task の Command を Project の行の Lock なしで（Task Service 自身の Transaction で）実行し、要求を完了にする最後の短い Transaction だけ Project の行を `FOR SHARE` で Lock します（Lifecycle の操作は `FOR UPDATE` なので互いに待ちます。待ちは同じ `lock_timeout` で `ProjectBusyError`）。
- Clock は 1 回の操作で 1 度だけ読みます（`validate_instant`）。

### 上限と入力の検証

Project 名は 1〜100 文字（前後の空白は除き、内側は保持）、説明は 2000 文字まで（空・空白だけは「なし」）。長さは Unicode の Code Point で数えます。制御文字（名前は改行・Tab も）、Surrogate、行・段落の区切り、双方向の書式制御文字は拒否します。
ID は `UUID` か正規の文字列だけです。Role と Status は Enum の Member だけで、`"manager"` の文字列は受け付けません（`bool` は `int` でなく、naive な `datetime` は時刻でもありません）。
`list_projects` の `limit` は 1〜200（既定 50）、`offset` は 0〜100000。Error の Message は固定文字列で、入力の内容・ID・DB の Message を含みません。DB の Error（接続断など）は加工せず伝わります。

### 他の領域との関係

`tasks`（PAW-032）、Memory（PAW-040）、Research Scratch（PAW-050）などの `project_id` は素の UUID のままで、この Issue はそれらの Table を変えません。**後の Migration で外部キーを付けるには:**
Project を持たない孤児の行がないことを確認し、`ALTER TABLE <table> ADD CONSTRAINT fk_<table>_project_id_projects FOREIGN KEY (project_id) REFERENCES projects (id) NOT VALID` の後に `VALIDATE CONSTRAINT`（長い Lock を避けるため）。
`ON DELETE` は `RESTRICT` を勧めます（Project の行は削除しないので、実際には働きません）。Purge では行が消えないため、他の領域の削除は各領域の Service が `PurgeResult.purged` を使って行います。

### 実装の由来

`domain.py`（7 関数）と `store.py`（20 関数）は、仕様（Docstring と `tests/test_projects_*.py`）を先に書き、関数の本体を別の実装者に埋めさせる設計です。Model、Migration、権限、`validation.py`、`service.py` は仕様の作者が実装しています。
**最終的な実装は Claude の参照実装です。** ローカルの Qwen3-Coder-30B-A3B に、27 関数の実装を 2 回（各約 265 回の Tool 呼び出し）任せましたが、収束しませんでした（1 回目は `domain.py` の書式を壊し、`store.py` は未着手、2 回目は `domain.py` の Test の約半数が通らないまま、`store.py` に届きませんでした）。
AGENTS.md のとおり、同じ失敗を繰り返したのでエスカレーションし、仕様の Docstring を保ったまま、Claude の参照実装（変異 38 個をすべて Test が検出）に置き換えています。ローカルモデルの成果物は、最終物に含まれていません。
レビューの指摘（Delete 開始時の Task 停止）への対応で足した `store.py` の 9 関数（4 回目のレビューで Project から Queue の Entry を引く 2 関数、6 回目のレビューで Task が終了しているかを読む 1 関数を追加）、`task_stop.py`、`transaction.py` とその Test は、最初から Claude が書いています（ローカルモデルは関与していません）。Test は変異 15 個（要求を書かない、別 Transaction に移す、完了を確認せずに記録する、完了の確認を Project の Lock の外で行う、生きている Project の Task も止める、Queue を取り消さない、他の Project の Task を含める、終了した Task を active と数える、全 Error を握りつぶす、再度の要求を上書きしない、`processed_at` を上書きする、Stop Now を使う、理由を落とす、並びと件数の上限を外す）をすべて検出しました。4 回目のレビューの Sweep には変異 6 個（Sweep を外す、`processed_at` の前の Entry の確認を外す、Sweep が Project の状態を見ない、Sweep の件数の上限を外す、Entry を Project で絞らない、claimed の Entry を数えない）を足し、すべて検出しました。5 回目のレビューの Restore 対応（`_is_stopping`）には変異 5 個（Task の読み直しを外す、Sweep の読み直しを外す、最初の 1 件だけ読み直す、Deleted を停止の対象から外す、Entry と Cancel の間でも止める）を足し、すべて検出しました。6 回目のレビューの順序と整合には変異 5 個（整合を外す、整合が状態を見ずに Entry を Cancel する、整合の Error が元の Error を隠す、整合の時間制限を外す、Sweep が active な Task の Entry も Cancel する）を足し、すべて検出しました（順序を戻すと新しい Test の 13 個が失敗します）。

### 制限と未確認の点

- HTTP の Endpoint、Session は含みません（PAW-022）。作成・受諾・退出は Audit に残りません（Decision 0008 の 5。暫定の作りとして承認され、Capability と Audit の追加は Issue [#82](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/82) です）。
- 招待を通知する仕組み（Notification、Email）はありません。招待された人は `list_my_invites` で見つけます。User の削除の流れ（Member を外す、所有権の移譲）は PAW-021 以降の Issue です。
- Purge を定期的に呼ぶ Janitor、Purge 後の他の領域のデータ削除、Task 停止の Processor を周期的に呼ぶ Loop（PAW-034 の `ProjectTaskStopLoop`）は含みません。Processor 自体は含みます（上の「Delete 開始時の Task 停止」）。Repository の紐付け（PAW-027）と Repo ACL の保存もありません。
- Project 名の一意性、Project ごとの設定（Agent Policy、Merge Policy など。要件の「New Project defaults」）、Owner / Admin の全 Project 一覧（Issue #84）は含みません。
- `Authorizer` の呼び出しと Project の Lock は同じ Transaction の中です。Audit の Store が遅いと、その間 Project の行の Lock が続きます（Authorizer の Timeout で有界）。
- PostgreSQL 18 の実 DB で Test しました。`READ COMMITTED` を前提に、Lock の順序（Project の行が最初）で直列化しています。他の Isolation Level では未確認です。

### Human の承認（2026-09-25）と、後続の Issue

Human は [Decision 0008](../../docs/decisions/0008-project-membership-and-lifecycle-policy.md) の各点を、推奨どおり承認しました。

1. **承認した点。** Delete 開始を Active から許し、Project 名の完全一致の入力を要求すること。復元できる人（`project.lifecycle.manage` を持つ Manager、Owner、Admin。復元先は Archived）。墓石を残す Purge。Membership のルール（辞退・退出は行の削除で履歴を持たない、`users` への Foreign Key `ON DELETE RESTRICT` を含む）。
2. **暫定値として承認した数値。** 招待の期限（14 日）、Member と有効な招待の合計（200）、Project 名（1〜100 文字）と説明（2,000 文字）。後から変えられますが、名と説明の長さは DB の CHECK 制約にも書かれているため、新しい Migration と `models.py` の変更が要ります。他の数値は Migration が要りません（Decision 0008 の「背景」）。
3. **Capability を持たない 4 つの操作**（作成、招待の受諾・辞退、退出）は、暫定の作りで承認されました。Capability と Audit の追加は Issue [#82](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/82) で行います（承認済みの Decision 0004 は書き換えず、新しい Decision から `Supersedes` します）。
4. **Owner / Admin が全 Project を一覧する API**（管理上の Lifecycle 操作の入口）は、Issue [#84](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/84) です。
5. **Purge 後の他の領域のデータ削除**は、各 Service が `PurgeResult.purged` を使う分担で承認されました。調査結果（Provenance・Scratch など）の扱いは、Issue [#88](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/88) で決めます。
6. **Delete 開始時の Task 停止**（Decision 0008 の 8）: 停止に Cancel（graceful）を使うこと、Outbox と Processor に分けることを承認しました。Delete 開始の後に作られた Task の競合を閉じる Gate（`create_task` / Retry / Restart / `enqueue` が Project の行を Lock して Active 以外を拒否する）の方針も承認され、実装は Issue [#83](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/83)（PAW-034 の前後。`tasks(project_id, state)` の Index を含む）です。この Issue では入れません。
7. **`0021` を `0026` より前に置く並び**は、Migration の実装上の順序で、統合時に確認してください。

### Test

`apps/backend/tests/test_projects_*.py`、`projects_support.py` です。標準 `unittest` だけで、`test_projects_domain.py`、`test_projects_validation.py`、`test_projects_service_validation.py` と Model の Test は DB を使いません。
それ以外は実 PostgreSQL（`PAW_TEST_DATABASE_URL`）を使い、未設定なら Skip します。時刻は注入した Clock で、速度に依存する Test はありません。
`test_projects_task_stop.py` は Task と Queue を本物の `TaskService` / `TaskQueue` で作り（SQL で読み戻す）、Delete 開始が要求を同じ Transaction で記録すること（失敗させると Project も Active のまま）、Processor が running / queued などの Task を止めて Queue の Entry を取り消すこと、冪等なこと、他の Project の Task と Active / Archived の Project の Task に触れないこと、Delete 開始の後に作られた Task を再実行で止めること、要求が「Task が残っている間は完了にならない」ことを確認します。Cancel → Restart → enqueue の競合（Queue の `cancel` に差し込んだ処理で再現します）で残る Entry が、終了済みの Task の後ろでも Project から見つかって Cancel され（claimed の Entry の Worker は Lease を失う）、Sweep の後に現れた Entry があると要求が開いたままになること、件数が `batch_size` を超えると複数回に分かれること、Sweep の途中で復元された Project の Entry は残ることを確認します。6 回目のレビューの Test（`InterruptedTaskCancelTest`）は、Restore が Cancel の前に Commit され Cancel が失敗する、競合する、Stopper が Cancel されるとき、復元された queued の Task が active な Entry を持ち続けること、Task の Cancel が Entry の Cancel より先であること、Cancel の後の中断（Entry の Cancel の失敗と Cancel、Listener の Cancel）が状態で整合され元の Error が伝わること、整合の失敗が元の Error を隠さないこと、整合が有界なこと、Sweep が active な Task の Entry を残すことを確認します。5 回目のレビューの Restore の Test は、6 件の Task（または Entry）の 1 件目の後に Restore を Commit させ（Task Service と Queue の `cancel` に差し込んだ処理と、一覧の直後に差し込んだ処理で再現します）、止まったのが 1 件だけで残りの Task と Entry が queued のままであること、Delete が再び始まった Project は止め続けることを確認します。`test_projects_grants.py` はこの Test も Application の Role で実行します。

## DAG Agent Orchestrator

[PAW-034](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/30)（Revision `0034`、`paw_backend/orchestrator/`）で実装しました。設計は [要件](../../REQUIREMENTS.md)の「Agent Orchestration / Parallel-first execution」に従い、要件が決めていない選択（Plan の形、Role の上限、Escalation の段、並列数、結果の大きさ、Sub-Agent の予算、Sweep の周期）は **[Decision 0021（Proposed、未承認）](../../docs/decisions/0021-dag-orchestrator-policy.md)** にまとめています。数値は暫定値で、人間の承認を待っています。
**HTTP の Endpoint はありません**（Session は PAW-022）。Orchestrator は Worker Process が `Orchestrator.run_once` / `serve` で駆動し、Agent の Runtime は Protocol で、実際の Codex / Claude / Local の Runtime は別の Issue です（Test は Fake の Runtime で動かします）。認可は行いません（Task の権利は呼び出し側が渡す `TaskAuthority`）。
Migration `0034` の `down_revision` は `0026` です（この Issue を Merge する順序で、統合時に鎖をつなぎ直します）。

```text
Queue の Entry を Claim ─→ Task を Start（または、死んだ Worker の Run を引き継ぐ）
   │                         │
   │   Lease を Heartbeat で確認 ─→ Runtime の Timer を開始（Lease を持つ Worker だけ）
   ▼
DAG がなければ Planner に分解させ、Plan を検査して保存 ─→ DAG を引き継ぐ（epoch + 1）
   ▼
Loop:  Task の状態を読む ─→ 準備のできた Node を並列に起動（Budget の確認つき）
        ─→ 終わった Node を順に処理（成功: 結果を保存 / 失敗: Retry・別の方法・Escalation・見放す）
   ▼
DAG の判定 ─→ 成功: Task を evaluating へ / 失敗: Task を failed へ / 予算・Loop: Task を waiting へ
```

| Module | 内容 |
| --- | --- |
| `plan.py` | `Plan` / `PlanNode`: Planner の出力の検査（Cycle、上限、Role の上限）と、決定的な位相順 |
| `result.py`、`jsonvalue.py` | `NodeResult`（構造化された結果）と、大きさを制限した JSON |
| `scheduling.py` | 純粋関数の Scheduler の規則: 準備のできた Node、失敗の伝播、DAG の判定 |
| `domain.py`、`limits.py` | Role・状態の Enum、Role ごとの Capability の上限（データ）、上限の定数 |
| `models.py`、`store.py`、`records.py`、Migration `0034` | DAG の永続化と Fencing（SQL だけ。方針は持たない） |
| `runtime.py` | `AgentRuntime` の Protocol、`NodeAssignment`、`NodeOutcome` |
| `scope.py`、`authz/delegation.py` | 子 Agent の Grant と Scope を親のものから導く（広げられない） |
| `gateway.py` | Node へ渡す Tool（`NodeToolGateway`）と Budget（`NodeBudgetHandle`）、Broker の `BudgetProvider`（`TrackerBudgetProvider`） |
| `orchestrator.py` | `Orchestrator`: 上の全てを組み立てる |
| `project_sweep.py` | 削除待ちの Project の Task を周期的に止める `ProjectTaskStopLoop` |

### Plan（Task を Dependency DAG へ分解する）

Planner が返す JSON を `Plan.from_mapping` が検査します。**Plan を作った時点で全て検査される**ため、`Plan` の Object は常に受け入れ可能です。Cycle（`cycle`）、未知・自己・重複した依存、Node・依存・Edge・鎖の長さの超過、大きさの超過、未知の Field（綴りの誤りが「依存なし」にならないため）、Role の上限を超える Capability の要求は、固定の理由 Code（`PlanReason`）の `InvalidPlanError` になり、何も保存しません（エラー文に Plan の内容は含めません）。Node の順序は決定的です（Kahn 法。同時に進める Node は Plan に書かれた順）。

| 項目 | 上限（暫定。Decision 0021 の 1） |
| --- | --- |
| Node | 1〜32。1 Node の依存 8、全体の Edge 96、鎖の長さ 16 |
| `key` | `[a-z][a-z0-9_-]{0,31}`。`title` 100 文字（1 行）、`goal` 4,000 文字 |
| `input` | JSON Object で 16 KiB、入れ子 8 段。Plan 全体は 128 KiB |
| `required`（既定 true） | 全て false の Plan は拒否 |

Plan の提出は `Orchestrator.submit_plan(task_id, plan)`（Task の現在の試行の DAG として保存。1 試行に 1 回）か、DAG がなければ Planner Role の Runtime を Orchestrator が呼びます（2 回まで。2 回目は Planner の Ladder の次の Agent）。**新しい Node を提案できるのは Planner の最初の呼び出しだけ**で、他の Node が Plan を返すと失敗として扱います。

### Role・Grant・Scope（Sub-Agent は親を超えない）

| Role | Capability の上限（`domain.ROLE_CEILING`） | Credential Handle |
| --- | --- | --- |
| Planner / Researcher / Reviewer | `project.read`、`project.memory.use`、`shared_memory.read`（**読み取り専用**） | なし |
| Worker | 上記 + `project.task.run`、`project.repo.write` | Task の Handle |

- **Grant**: `authz.derive_child_grant(parent, child_agent_id, capabilities, project_ids)` が子 Agent の Grant を作ります。Capability も Project も**親の部分集合**だけで、親が持たないものを求めると `GrantEscalationError`（削らずに拒否）、委任不可の Capability（`agent.use` など）と親自身の ID も拒否します。`is_subgrant(child, parent)` で判定できます。Node の Grant は「Role の上限 ∩ Plan が求めたもの ∩ 親の Grant」で、Plan が親の持たないものを求めた Node は失敗します（再試行しません。`GrantEscalation`）。Sub-Agent の ID は Task・Node・試行から導きます（`uuid5`）。`agent.use` / `project.agent.use` は**委任不可のまま**です（Decision 0004。委任可能にするのは Agent が起動を要求する Tool を作るときの新しい Decision。0021 の 9）。
- **Scope**: `derive_child_scope(parent_scope, role, repositories)` が親の `TaskScope` を狭めます。Repository は Plan が求めた Working Set の一部（外の ID は `ScopeEscalationError`）で、ACL と Remote は親の値のまま、読み取り専用の Role には Credential Handle を渡しません。`scope_within(child, parent)` は Orchestrator が導いた Scope ごとに確認し、Test が手で作った広い Scope も検出します。
- **予算**: Sub-Agent の予算は**親 Task の予算の共有 Pool**で、Node ごとの予算は持ちません。Node の消費は全て親 Task の `budget_usages` へ記録されます（下の「予算」）。

### 実行（Parallel-first、決定的な Scheduler）

- **準備のできた Node（依存が全て成功した Node）を、`ordinal` の小さい順に、`max_parallel_nodes`（既定 4）まで並列に起動します。** 並列数の上限は静的で、要件の動的な並列数は PAW-036（GPU / Resource Scheduler）の担当です。
- 1 つの Run の書き込みは 1 つの Loop（`_drive`）が順に行い、終わった Node は Node の順に処理します。同じ DAG は同じ順で起動されます。時計は注入する `Clock`（Heartbeat、Poll、Back-off、Node の Timeout）で、Test は手で動かします。
- **Task の状態を、Node の終了ごとと Poll（既定 2 秒）に読みます。** `cancelled`（Cancel と Stop Now）は走っている Node を即座に止めて DAG を `cancelled` にし、`failed`（外部の Fail）は Node を `ready` に戻し（Retry が続きから実行）、`paused` は新しい Node を起動せず走っている Node は終わらせてから止め（Resume されたら再開）、Retry / Restart で Run が替わったら（`superseded`）何も書かずに止まります。Resume と `unblock` の後の再 Enqueue は呼び出し側（API 層）が行います。
- Node の 1 回の試行は `node_timeout_seconds`（既定 30 分）で打ち切ります。

### Node ごとの Retry / Escalate

Node が失敗するたび（Runtime が失敗を返す、例外を送出する、Timeout、`NodeOutcome` でない値を返す）、Orchestrator は次の順で判断します。

1. 失敗の文字列を**整形**（`format_failure_text`: UTF-8 にできない文字を置換）してから `LoopDetector.record_failure` へ渡します（Decision 0007 の 9。Surrogate を含む文字列は拒否されるので、整形を忘れると Loop 検知に入りません）。`step` は Node の `key`、`attempt` は Task の試行、`approach` は Node の方法の番号です。Error の Class 名は固定の名前に減らします（Adapter の独自の Class は `AdapterError`）。**失敗の文字列は保存も Log もしません**。Node の試行の記録は Class 名と Signature（Hash）だけです。
2. Budget の判定（`retries` を 1 つ足した予定で）と Loop の判定を `decide_next_action` に渡します（Budget が Loop に優先）。

| 判断 | Node の行き先 |
| --- | --- |
| `CONTINUE` | 同じ Agent・同じ方法で再試行（1 つの段で 6 回まで。超えたら失敗） |
| `TRY_ALTERNATIVE` | `approach` を 1 増やして再試行 |
| `ESCALATE_AGENT` | 次の段（Ladder の次の Agent）へ。`approach` も増やし、その段の試行を数え直す |
| `WAIT_FOR_USER`（Budget、Loop で上位がない） | Node を残して Run を止め、Task を `waiting`（理由 `user`） |
| `FAIL`（`retries` の超過） | Node を失敗にして Run を止め、Task を `failed` |
| `retryable=False` の失敗 | 再試行せず Node を失敗にする |

再試行の前に Back-off（`retry_backoff_seconds`、倍で最大 60 秒）で待ちます。Escalation の段は `OrchestratorConfig.ladders`（Role ごとの Agent の並び）です。

**失敗の伝播（要件の Dependency / failure isolation）**: 失敗した Node に依存する Node（推移的）だけが `blocked` になり、独立した Node は最後まで実行します。実行できる Node がなくなったとき、`required` な Node が全て成功なら DAG は `succeeded`（Task は `evaluating`。**完了は Evaluator の責務**）、そうでなければ `failed`（Task は `failed`。Retry が失敗・Blocked の Node を開き直す）。

### 構造化された結果の受け渡し

`NodeResult`（`summary` 必須、`changed_files`、`commit`、`test_result`、`discovered_facts`、`dependency_notes`、`unresolved_questions`、`confidence`、`artifacts`。要件の「Subtask output例」）。未知の Field、NUL / Surrogate を含む文字列、長すぎる List は拒否し、1 つの結果は JSON で 32 KiB まで（DB の CHECK は 64 KiB の予備）。Node は**直接の依存の結果だけ**を `NodeAssignment.upstream` として受け取ります（会話履歴は渡しません）。

### Runtime の契約

```python
class AgentRuntime(Protocol):
    async def run_node(self, assignment: NodeAssignment) -> NodeOutcome: ...
```

`Orchestrator(...)` を作るとき、全ての Runtime を `validate_runtime`（`async run_node` が 1 引数か）で検査し、Ladder が名前を挙げる Agent の Runtime がなければ失敗します。`NodeAssignment` は Goal・Input・上流の結果・Agent の Label・試行と方法の番号と、`tools`（Tool の呼び出し）、`budget`（消費の報告）を持ちます。**Runtime は Broker、Runner、`TaskContext`、Grant、Scope、DB 接続を受け取りません。** `NodeStopped`（Task が終わった、Lease を失った、Budget が尽きた）は Runtime が通さなければなりません。`NodeOutcome.succeeded(result, plan=...)` / `NodeOutcome.failed(error_class, message, retryable=...)`。

### Tool Broker との接続（Decision 0006 の条件）

| 条件 | 実装 | Test |
| --- | --- | --- |
| 終了した Task へ Tool 呼び出しを渡さない（承認を要しない呼び出しも） | `NodeToolGateway.call` が、**全ての**呼び出しの直前に `TaskActivityProvider.check`（本番は `PostgresTaskActivity`）を確認し、`ACTIVE` 以外・不明・エラー・Timeout は `NodeStopped`（Broker へ渡さない） | `test_orchestrator_control.test_a_cancelled_task_is_never_handed_a_tool_call`、`test_orchestrator_tools.test_an_approval_of_a_task_that_ends_is_revoked_and_no_call_follows` |
| `TaskContext.run` は `TaskSnapshot.run` / `TaskEvent.run` から組み立てる | Start の `TaskEvent.run`（または引き継ぐ Run の `TaskSnapshot.run`）を `TaskContext.run` に使う。Retry の後は新しい Run | `test_orchestrator_tools.test_a_node_reaches_the_tools_through_a_context_the_backend_built`、`test_a_retried_task_hands_its_new_run_to_the_broker` |
| 書き込む Repository は `TaskScope.repositories`、Remote を登録する | 呼び出し側の `TaskAuthority.parent_scope(task)`（**Working Set の Seam**。#85 が保存するまで呼び出し側が渡す）。`ScopedRepository`（Worktree、解決済みの ACL、Remote）は Node の Scope へ**そのまま**渡る。Remote のない Repository では URL を伴う呼び出しが通らない（Broker の既定拒否） | `test_a_url_passes_only_under_a_registered_remote_of_the_working_set`、`test_a_repository_without_a_registered_remote_lets_no_url_through` |

`TaskContext` は**呼び出しごとに**作り直します（`TaskAuthority` を毎回呼ぶ）。ACL を狭める、Project を Archive する、Grant を縮めるは、次の呼び出しから効きます（`test_an_acl_narrowed_meanwhile_takes_effect_on_the_next_call`）。承認を要する呼び出しの承認は、Task の終了で取り消されます（`TaskService(listeners=[approval_service.revoke_on_task_end])` を配線した構成を Test しています）。

### Queue・Budget・Loop との接続（Decision 0007 の条件）

- **Lease を持つ Worker だけが `BudgetTracker.start_runtime` を呼ぶ**: Run は、Task を読んだ後、`start_runtime` の直前に `TaskQueue.heartbeat` で Lease を確かめます。Lease を失った Worker は `start_runtime` に到達しません（`test_a_worker_whose_lease_has_expired_does_nothing`）。Run 中は周期的に Heartbeat し、**Lease を失う（3 回続けて Heartbeat に失敗する場合も）と Node を止めて、何も書かずに終わります**。`stop_runtime` は `start_runtime` が返した世代で呼びます（新しい Session に奪われたら `StaleRuntimeSessionError` を無視）。
- **Budget**: Node の起動ごとに `steps` を確認・記録し、再試行ごとに `retries` を記録します。Node は `NodeBudgetHandle.charge(kind, amount)`（`tokens`、`gpu_seconds` など）で報告し、Budget が尽きた Node と、同じ Run の他の Node は `NodeStopped` で止まります。Tool 呼び出しは Broker の `BudgetProvider`（`TrackerBudgetProvider`）が `tool_calls` として原子的に記録します。Budget を使い切った時の遷移は Decision 0007 のとおりです（`retries` は `failed`、それ以外は `waiting`）。`BudgetPreset` の設定は `Orchestrator.enqueue_task(task_id, preset=...)`（Preset のない Task は `budget_not_configured` として `failed` にし、Timer を開始しません）。

### DAG の永続化と Fencing

| Table | 内容 |
| --- | --- |
| `agent_dags` | Task の**試行ごと**に 1 つ（`UNIQUE (task_id, attempt)`）。`state`（`active` / `succeeded` / `failed` / `cancelled`）、`epoch`（Fencing Token）、`owner`、`task_retry_count`（Retry の検出） |
| `agent_dag_nodes` | Node（`ordinal`、Role、`goal`、`input`、`required`、Plan が求めた Capability / Repository、`state`、Ladder の段 `agent_index`、`approach`、`attempt_count`、`rung_attempts`、`result`、`error_class`）。結果は JSONB（64 KiB の CHECK） |
| `agent_dag_edges` | `node_key` が `depends_on_key` に依存（追加のみ） |
| `agent_dag_node_attempts` | Node の起動ごとの記録（Agent の段、`approach`、起動した `epoch`、`running` / `succeeded` / `failed` / `interrupted`、失敗の Class と Signature） |

- **Fencing**: Worker が DAG を引き継ぐ（`acquire`）たびに `epoch` を 1 増やし、書き込みは全て自分の `epoch` を示します。書き込みは DAG の行を `SELECT ... FOR NO KEY UPDATE` で Lock してから `epoch` を比べ、引き継ぎと同じ Lock を取るため、**引き継ぎの後の書き込みも、引き継ぎを Lock 待ちしていた書き込みも、古い `epoch` なら何も変えずに `StaleDagEpochError`** になります。同じ Lock が 1 つの DAG の書き込みを直列にするので、同時に終わった 2 つの Node の合流点は必ず `ready` になります。Node の試行も Fencing します（`attempt_count` を示さない報告は `StaleNodeAttemptError`）。
- **Worker が死んだ場合（Crash）**: 次の Lease 保持者が Task を引き継ぎ（Task が `running` のまま）、`acquire` が走っていた Node を `ready` に戻し試行を `interrupted` にします。中断された試行も、その段の試行数に数えます。死んだ Worker が戻って報告しても、Queue（`LeaseLostError`）、Runtime の Timer（`StaleRuntimeSessionError`）、DAG（`StaleDagEpochError`）のどれでも拒否されます（`test_a_crash_mid_node_is_recovered_and_the_zombie_is_refused`）。
- **Retry / Restart**: Retry（同じ試行の新しい Run）は `acquire` が失敗・Blocked の Node を開き直して続きから実行します。Restart は新しい試行なので新しい DAG です（古い DAG は履歴）。
- **Application の Role の権限**（Role を分ける構成、`PAW_APP_DATABASE_ROLE`）: Migration `0034` は 4 つの Table のそれぞれに [`grant_app_privileges`](#migration-は-application-の-role-に権限を与えるcontributor-向けの規則) で必要な最小の権限だけを与えます。DELETE と TRUNCATE はどこにも与えません。

  | Table | Application の Role の権限 |
  | --- | --- |
  | `agent_dags` | SELECT、INSERT、UPDATE は `state`、`epoch`、`owner`、`task_retry_count`、`updated_at` だけ（`task_id`、`attempt` は変えられない。`FOR NO KEY UPDATE` の Lock に UPDATE 権限が要る） |
  | `agent_dag_nodes` | SELECT、INSERT、UPDATE は `state`、`agent_index`、`approach`、`attempt_count`、`rung_attempts`、`result`、`error_class`、`finished_at`、`updated_at` だけ（Plan が言ったこと `key`、`ordinal`、`role`、`goal`、`input`、`required`、要求は変えられない） |
  | `agent_dag_edges` | SELECT、INSERT だけ |
  | `agent_dag_node_attempts` | SELECT、INSERT、UPDATE は `state`、`error_class`、`failure_signature`、`finished_at` だけ |

  `tests/test_orchestrator_grants.py` が、Migration を実際にこの構成で実行し、Superuser でない Role で DAG Store・Orchestrator・Planner・Budget・制御・Lease・Tool の Test を全て実行します。あわせて、この表と Role の権限が一致すること、Plan や履歴の書き換え、削除、Schema の変更が拒否されることを確認します。

### Project 削除の Sweep（`ProjectTaskStopLoop`）

Decision 0008 の 8 が Orchestrator に課した「削除待ちの Project にも通常の周期で `stop_project_tasks` を呼ぶ」の実装です。Application の Lifespan が、DB があり `PAW_PROJECT_TASK_STOP_INTERVAL_SECONDS`（既定 60、0 で停止、10〜3,600）が 0 でないとき起動し、終了時に `stop()` と Cancel で止めます（DB を破棄する前）。

- **1 周期**: 未処理の要求（`pending_project_ids`）の Project を先に、次に**削除待ちの全 Project**（要求が処理済みでも。処理の後で作られた Task と Entry を止めるため）を id の順に、前の周期の続きから（Cursor）最大 50 件（`projects_per_cycle`）。1 Project に 1 周期で最大 5 回（`rounds_per_project`）呼び、`done` にならなければ次の周期が続けます。
- **周期**: 最初の周期は起動の 5 秒後、以降は周期ごと（未完了があれば 5 秒後）。周期全体が失敗したら 10 秒から倍で、周期を上限に待ちます。1 つの Project の失敗は他を止めません（Log は Exception の型名だけ）。
- 削除待ちの一覧は、状態を Statement に書き込んだ（`literal_execute`）部分 Index `ix_projects_pending_deletion` の Query です（Plan の Test つき）。停止は `TaskService(listeners=[revoke_on_task_end])` で行うので、止めた Task の承認も取り消されます。

### 組み立て

```python
approvals = ApprovalService(PostgresApprovalStore(database), audit_sink)
tasks = TaskService(database, listeners=[approvals.revoke_on_task_end])
budget = BudgetTracker(database)
broker = ToolBroker(
    registry,
    authorizer,
    PostgresApprovalStore(database),
    audit_sink,
    budget=TrackerBudgetProvider(budget),
    task_activity=PostgresTaskActivity(database),
)
orchestrator = Orchestrator(
    tasks=tasks,
    queue=TaskQueue(database),
    budget=budget,
    loops=LoopDetector(database),
    store=DagStore(database),
    activity=PostgresTaskActivity(database),
    tools=ToolRunner(broker, executor),
    authority=authority,  # TaskAuthority: 親の Grant と、呼び出しごとに現在の Scope（Working Set）
    runtimes={"local": local_runtime, "codex": codex_runtime},
    config=OrchestratorConfig.uniform(["local", "codex"]),
)
await orchestrator.enqueue_task(task_id, preset=BudgetPreset.STANDARD)
await orchestrator.serve("worker-1", stop_event)  # または run_once("worker-1")
```

これは Decision 0006 の「後続の課題」（`TaskService` への `revoke_on_task_end` の配線と `PostgresTaskActivity` の注入は PAW-034）の実装の形です。Application の Process が Agent の Runtime へ DB 接続を渡さないこと（Decision 0006 の前提）は、この組み立てを行う側の責務です。

### 制限と未確認の点

- Agent の Runtime、実 Model、実際の Tool の Executor はありません（Fake で Test）。Runtime が `NodeStopped` を通す契約と、Executor の契約（Tool Broker の節）は実装側の責務です。
- 承認待ち（`NEEDS_APPROVAL`）で Task を `waiting`（`approval`）にする配線はありません（承認の Endpoint が PAW-022 以降）。Runtime は `NEEDS_APPROVAL` の結果を受け取り、承認後に `approval_id` を付けて呼び直します。
- Node の停止は、Cancel と Stop Now を区別しません（どちらも Node を即座に止めます。成果物は Worktree に残ります）。
- Worktree の作成・統合（PAW-035）、Evaluator による完了、Resource Scheduler による並列数（PAW-036）、Working Set の永続化（#85）、Node ごとの予算は含みません。
- **実 PostgreSQL 18 で Test しました。** 複数の Process が同じ DAG を触る競合（Lock の順序）は、別の接続 Pool（別の Worker Process の代わり）を使った Test で確かめています。
- 削除待ちの Project の Sweep は、Task Lane / Queue Lane の Gate（Issue #83）の実装ではありません。競合そのものは閉じず、周期の再実行で止めます。
- `TaskQueue` の Lease は Database の時計で判定され、Worker の Heartbeat の間隔（Lease の 1/3）の間は、Lease を失った Worker が気づかず Node の Runtime を動かし続けることがあります（書き込みは `epoch` が拒否します）。Runtime の副作用（File への書き込み）は At-least-once で、Node の冪等性は Runtime の責務です。

### Test

`apps/backend/tests/test_orchestrator_*.py`、`orchestrator_support.py`、`test_authz_delegation.py`。標準 `unittest` だけで、`test_orchestrator_plan.py`（Plan の検査の表と、ランダムな DAG の位相順・Cycle 検出）、`test_orchestrator_result.py`、`test_orchestrator_scheduling.py`（純粋な規則と、ランダムな DAG の Property Test）、`test_orchestrator_scope.py`、`test_orchestrator_argument_validation.py`（全 Public Method × 全引数 × 誤った値の表。DB を設定しない Database を渡し、DB に届く前に型付きのエラーになること）、`test_orchestrator_migration.py` の前半と `test_orchestrator_project_sweep.py` の前半は DB を使いません。
それ以外は実 PostgreSQL（`PAW_TEST_DATABASE_URL`）を使い、未設定なら Skip します: 永続化と Fencing の競合（`test_orchestrator_store.py`: 引き継ぎ・書き込み・Lock 待ちの順序、同時に終わる 2 Node、同じ Node の 2 重の起動）、実行・並列・結果の受け渡し（`test_orchestrator_run.py`）、失敗・Retry・Escalation・Isolation（`test_orchestrator_failures.py`）、Plan の受け入れ（`test_orchestrator_planning.py`）、Budget（`test_orchestrator_budget.py`）、Pause / Cancel / Retry / Restart（`test_orchestrator_control.py`）、Lease・Crash・引き継ぎ（`test_orchestrator_lease.py`）、Worker の停止と `serve`（`test_orchestrator_shutdown.py`）、実際の Tool Broker と（`test_orchestrator_tools.py`）、ランダムな DAG を Orchestrator 全体で動かす Property Test（`test_orchestrator_property.py`）、Migration の上げ下げと Model との一致（`test_orchestrator_migration.py`）、Sweep（`test_orchestrator_project_sweep.py`）、非 Superuser の Role（`test_orchestrator_grants.py`）。時間は注入した `ManualClock` で、速度に依存する Test はありません（Lock 待ちや非同期の進行は上限を長く取った待機で確かめます）。

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
RBAC（PAW-025）、Task Lifecycle（PAW-032）、Task Queue / Budget / Loop 検知（PAW-033）、Tool Broker（PAW-031）、Memory Schema（PAW-040）、Research Scratch Store（PAW-050）、Research Provider Adapter（PAW-051）、Research Privacy Filter（PAW-053）、Evidence / Claim Provenance（PAW-052）は、この Skeleton の上に実装済みです。
PAW-022（Login / Session / Password）と PAW-023（Passkey / Step-up）は Owner Setup の Token を受け取る側で、まだありません。
Memory の保存・整理・検索は PAW-041 以降で、Memory Schema の上に実装します。
Research Privacy Filter（PAW-053）と Evidence / Claim Provenance（PAW-052）は、Research Provider Adapter の上に実装済みです。Research の Provider（Direct Web、Docs、GitHub、OpenCode）の Adapter は、Research Provider Adapter の上に実装します。外部送信の Audit を Audit Log へ保存する実装は、後続の Issue です。
受け入れ基準は [Implementation Backlog](../../docs/IMPLEMENTATION_BACKLOG.md)、
実装時に選択できる事項は [Requirements Freeze Review](../../docs/REQUIREMENTS_FREEZE_REVIEW.md) を参照してください。
