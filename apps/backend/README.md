# Backend

Personal AI Workspace の Core Backend です。
[PAW-020](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/17) で、後続の Issue が載る最小の Application Skeleton を実装しました。
認証、User、RBAC、Task、Memory はまだ実装していません（PAW-021 以降）。

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

pgvector は Memory Schema の Issue（PAW-040）で導入します。

## 構成

```text
apps/backend/
├─ pyproject.toml          # 依存（完全一致で固定）と Ruff 設定
├─ alembic.ini             # Alembic 設定（DB URL は持たない）
├─ migrations/             # env.py と Revision（0001 は空の Baseline）
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
Readiness は `PAW_DATABASE_TIMEOUT_SECONDS` で必ず応答します（Driver がその後も Query の取消を待つ場合は、Background で待ちます）。
`Database.session()` と `paw_backend.api.deps.get_session` が Session を提供します。

Alembic は `PAW_DATABASE_URL` から接続先を読み、`alembic.ini` には DB URL を書きません。
どのディレクトリからでも実行できます。

```bash
alembic -c apps/backend/alembic.ini upgrade head          # 適用
alembic -c apps/backend/alembic.ini upgrade head --sql    # SQL の出力のみ（DB 接続は不要）
alembic -c apps/backend/alembic.ini revision -m "説明"    # 新しい Revision
```

## 認可（RBAC / Capability）と Audit

権限の判定は Backend だけが行います。Frontend の表示、Client が送る Header・Query・Body、Prompt、Model の出力は判定の入力になりません。
判定は型付きの入力（`Principal`、`Capability`、`Resource`）だけから決まる純粋関数で、**既定は拒否**です
（[設計](../../docs/SECURITY_RBAC_AUDIT.md)、[Tool 権限](../../docs/SECURITY_TOOL_PERMISSIONS.md)）。

| 層 | Role | 内容 |
| --- | --- | --- |
| System | Owner / Admin / User | Owner は Admin の全権限を含み、Admin は User の権限を含む。Owner 専用は Admin の追加・削除、Owner 権限の移譲、全体復旧、削除待ち User の復元、Backup 設定 |
| System | System | Backend 内部の Identity。人間はログインできず、Capability を持たない |
| Project | Manager / Contributor / Viewer | Viewer は閲覧、Contributor は Chat・Task・Repository 編集・Agent・PR、Manager は Member・Repository 追加・設定・Agent Policy・Project Memory・Archive / Delete 開始 |

- Project の Role は Project ごとです。Manager である Project A の権限は Project B に及びません。
  System Role だけでは所属していない Project を閲覧・利用できません（Owner / Admin が持つ Project の権限は、Archive / Delete 開始などの管理操作だけです）。
- 自分のデータ（Chat、Workspace、GitHub、Memory）の Capability は、`Resource.owner_id` が本人のときだけ許可します。Owner でも他の User の Private Data は使えません。
- 判定の全体表は `paw_backend/authz/policy.py` にあり、`tests/test_authz_policy.py` が全 Role × 全 Capability を文字列で列挙して固定しています。

### Endpoint への適用

```python
from paw_backend.authz import Capability, Resource, require_capability


# Workspace 全体の操作
@router.get(
    "/admin/audit",
    dependencies=[Depends(require_capability(Capability.ADMIN_AUDIT_VIEW))],
)
async def audit_log(): ...


# Project の操作は、Path Parameter から Backend が Resource を作る
def project_of(request: Request) -> Resource:
    return Resource.project(request.path_params["project_id"])


@router.post(
    "/projects/{project_id}/tasks",
    dependencies=[Depends(require_capability(Capability.PROJECT_TASK_RUN, project_of))],
)
async def run_task(project_id: str): ...
```

`require_capability` は `Principal` を返すので、Endpoint の引数で受け取れます。

| 状況 | Response |
| --- | --- |
| 認証されていない | 401 `unauthorized` |
| 認証済みだが権限がない（Role 不足、Project の非 Member、他人のデータ、不正な ID） | 403 `forbidden`。Body は固定で、どの規則で拒否したかは含めない |
| 特権 Capability で Audit を書けない | 503 `service_unavailable`（下記） |

**現在、認証は未実装（PAW-022）なので、`require_capability` を付けた Endpoint はすべて 401 を返します。**
既定の `UnauthenticatedProvider` が誰も認証しないためです。PAW-022 は `PrincipalProvider`（Request から有効な User の `Principal` を返す）を実装し、
`install_authz(app, ..., principal_provider=...)` で差し替えます。Provider は保存済みのデータから `Principal` を作ることが必須で、Client が申告した Role を使ってはいけません。
`/api/v1/events` の 2 つの Endpoint は現在も認証なしです（`TODO(PAW-022)`）。

### Agent と LLM

Agent の操作は、委任した人間の User の操作として判定します（`Authorizer.authorize_agent_action` / `policy.decide_agent`）。

- 許可されるのは、**User 本人が許可される** かつ **`AgentGrant` に含まれる** 操作だけです（積集合）。Grant は権限を狭めるだけで、User の権限を超えることはありません。
  User の Role を外せば、次の判定から Agent にも効きます。
- `AgentGrant.project_ids` を指定すると、その Project の中だけに限定できます。Grant は Backend が Task の範囲から作り、Model が書いた文字列から作りません。
- 特権 Capability（`admin.*`、`owner.*`、`shared_memory.manage`、`project.members.manage`、`project.agent_policy.manage`、`project.lifecycle.manage`）は、Grant に書いてあっても Agent には常に拒否します（自己権限昇格の禁止）。
- Capability 名は完全一致だけです。Model が出力した名前を判定に渡しても、未知の名前は `unknown_capability` で拒否し、Audit にも入力の文字列は残しません。

Tool Broker の Capability（read / write / execute / network / credential-use / destructive）と Approval は PAW-031 以降で、ここで決めた権限をさらに狭める方向にだけ働きます。

### Audit Event

判定のたびに 1 件の `AuditEvent` を `AuditSink` へ渡します（許可も拒否も、未認証も）。
項目は `event_id`、`occurred_at`、`actor_id`（人間の User。Agent の操作では委任元）、`actor_role`、`agent_id`、`action`（Capability 名）、
`resource_kind` / `resource_id` / `project_id` / `repo_id`、`decision`（`allow` / `deny`）、`reason`（固定の Reason Code）、`request_id` です。
Secret、Prompt、本文は持ちません。ID は英数字と `._:-` の 128 文字以内に制限しています。

- 保存先は `audit_events` Table（Migration `0025`）で、`PostgresAuditSink` が Request の Transaction とは別の短い Transaction で INSERT します。
  Test 用に `InMemoryAuditSink` があります。
- **追記専用**: Trigger が UPDATE、DELETE、TRUNCATE を拒否します（`restrict_violation`。`session_replication_role = replica` でも有効）。
  Table の Owner や Superuser は Trigger を外せるため、運用では Migration 用の Role と、INSERT / SELECT だけを持つ実行時の Role を分けてください。
  Downgrade は Table ごと履歴を消します。
- **Fail-closed**: Audit を書けない（失敗または `PAW_DATABASE_TIMEOUT_SECONDS` の超過）とき、
  特権 Capability（管理者・Owner の操作、権限を変える操作）の許可は拒否に変わり（`audit_unavailable`）、HTTP は 503 です。
  それ以外の Capability は許可のまま続行し、失敗は Log（例外の型名だけ）に残します。Audit の障害で Workspace 全体が止まらないようにするためです。拒否は常に拒否のままです。
- 未認証の Request も 1 件ずつ記録するため、認証前の連続アクセスはそのまま行数になります。回数制限は PAW-022（Lockout、Rate Limit）の課題です。
- 保存するのは ID だけです。User の削除後の匿名化（`Deleted User`）は、Audit の行を書き換えず、User 側の個人情報を消して行います。

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
PAW-025（RBAC）、PAW-032（Task Lifecycle）、PAW-040（Memory Schema）はこの Skeleton の上に実装します。
受け入れ基準は [Implementation Backlog](../../docs/IMPLEMENTATION_BACKLOG.md)、
実装時に選択できる事項は [Requirements Freeze Review](../../docs/REQUIREMENTS_FREEZE_REVIEW.md) を参照してください。
