# Repository のディレクトリ構造

現在の Repository は要件・設計文書、GitHub 運用と CI、Application と Benchmark / Evaluator の配置先で構成します。
`apps/backend/`にはBackendの最小Application Skeleton（PAW-020）を実装しています。
`apps/web/`・`apps/cli/`・`evaluator/` は README による役割の整理までです。`benchmarks/`にはTask / Evaluator Result
schemaとそのvalidator、test fixtureを配置しています。

## 現在の構造

以下は Git に保存する実在ファイルの配置です。

```text
personal-ai-workspace/
├─ .github/
│  ├─ CI.md
│  ├─ ISSUE_TEMPLATE/
│  │  ├─ benchmark_task.yml
│  │  ├─ bug_report.yml
│  │  ├─ config.yml
│  │  └─ feature_request.yml
│  ├─ pull_request_template.md
│  ├─ requirements-ci.txt
│  ├─ scripts/
│  │  ├─ check_repository.py
│  │  ├─ install_hooks.py
│  │  ├─ run_ci.py
│  │  ├─ test_check_repository.py
│  │  ├─ test_dependency_pins.py
│  │  └─ test_install_hooks.py
│  └─ workflows/
│     ├─ ci.yml
│     └─ claude-review.yml
├─ .gitignore
├─ .pre-commit-config.yaml
├─ AGENTS.md
├─ CONTRIBUTING.md
├─ LICENSE
├─ README.md
├─ REQUIREMENTS.md
├─ SECURITY.md
├─ apps/
│  ├─ backend/
│  │  ├─ README.md
│  │  ├─ alembic.ini
│  │  ├─ migrations/
│  │  │  ├─ env.py
│  │  │  ├─ script.py.mako
│  │  │  └─ versions/
│  │  │     ├─ 0001_baseline.py
│  │  │     ├─ 0021_owner_setup.py
│  │  │     ├─ 0025_audit_events.py
│  │  │     ├─ 0031_tool_approvals.py
│  │  │     ├─ 0032_task_lifecycle.py
│  │  │     ├─ 0033_task_queue_budget_loop.py
│  │  │     ├─ 0040_memory_schema.py
│  │  │     ├─ 0046_shared_memory_candidates.py
│  │  │     └─ 0050_research_scratch_store.py
│  │  ├─ paw_backend/
│  │  │  ├─ __init__.py
│  │  │  ├─ __main__.py
│  │  │  ├─ app.py
│  │  │  ├─ config.py
│  │  │  ├─ db.py
│  │  │  ├─ errors.py
│  │  │  ├─ events.py
│  │  │  ├─ middleware.py
│  │  │  ├─ security.py
│  │  │  ├─ server.py
│  │  │  ├─ cli/
│  │  │  │  ├─ __init__.py
│  │  │  │  ├─ __main__.py
│  │  │  │  └─ owner.py
│  │  │  ├─ identity/
│  │  │  │  ├─ __init__.py
│  │  │  │  ├─ audit.py
│  │  │  │  ├─ diagnostics.py
│  │  │  │  ├─ errors.py
│  │  │  │  ├─ limits.py
│  │  │  │  ├─ login_name.py
│  │  │  │  ├─ models.py
│  │  │  │  ├─ operator.py
│  │  │  │  ├─ redeemer.py
│  │  │  │  └─ tokens.py
│  │  │  ├─ memory/
│  │  │  │  └─ shared/
│  │  │  │     ├─ __init__.py
│  │  │  │     ├─ errors.py
│  │  │  │     ├─ lifecycle.py
│  │  │  │     ├─ limits.py
│  │  │  │     ├─ models.py
│  │  │  │     ├─ policy.py
│  │  │  │     ├─ precedence.py
│  │  │  │     ├─ records.py
│  │  │  │     ├─ service.py
│  │  │  │     └─ validation.py
│  │  │  ├─ research/
│  │  │  │  ├─ __init__.py
│  │  │  │  ├─ providers/
│  │  │  │  │  ├─ __init__.py
│  │  │  │  │  ├─ broker.py
│  │  │  │  │  ├─ contract.py
│  │  │  │  │  ├─ errors.py
│  │  │  │  │  ├─ locator.py
│  │  │  │  │  ├─ normalize.py
│  │  │  │  │  ├─ registry.py
│  │  │  │  │  └─ static.py
│  │  │  │  └─ scratch/
│  │  │  │     ├─ __init__.py
│  │  │  │     ├─ errors.py
│  │  │  │     ├─ limits.py
│  │  │  │     ├─ models.py
│  │  │  │     ├─ records.py
│  │  │  │     ├─ service.py
│  │  │  │     └─ validation.py
│  │  │  ├─ tasks/
│  │  │  │  └─ queueing/
│  │  │  │     ├─ __init__.py
│  │  │  │     ├─ budget.py
│  │  │  │     ├─ domain.py
│  │  │  │     ├─ errors.py
│  │  │  │     ├─ escalation.py
│  │  │  │     ├─ loop.py
│  │  │  │     ├─ models.py
│  │  │  │     ├─ sql.py
│  │  │  │     ├─ task_queue.py
│  │  │  │     └─ validation.py
│  │  │  ├─ tools/
│  │  │  │  ├─ __init__.py
│  │  │  │  ├─ approval_memory.py
│  │  │  │  ├─ approval_store.py
│  │  │  │  ├─ approval_types.py
│  │  │  │  ├─ approvals.py
│  │  │  │  ├─ audit.py
│  │  │  │  ├─ broker.py
│  │  │  │  ├─ budget.py
│  │  │  │  ├─ calls.py
│  │  │  │  ├─ capabilities.py
│  │  │  │  ├─ credentials.py
│  │  │  │  ├─ decisions.py
│  │  │  │  ├─ interfaces.py
│  │  │  │  ├─ models.py
│  │  │  │  ├─ policy.py
│  │  │  │  ├─ registry.py
│  │  │  │  ├─ runner.py
│  │  │  │  └─ scope.py
│  │  │  └─ api/
│  │  │     ├─ __init__.py
│  │  │     ├─ deps.py
│  │  │     └─ v1/
│  │  │        ├─ __init__.py
│  │  │        ├─ events.py
│  │  │        └─ health.py
│  │  ├─ pyproject.toml
│  │  └─ tests/
│  │     ├─ __init__.py
│  │     ├─ fake_postgres.py
│  │     ├─ identity_support.py
│  │     ├─ queueing_support.py
│  │     ├─ research_support.py
│  │     ├─ scratch_support.py
│  │     ├─ shared_memory_support.py
│  │     ├─ support.py
│  │     ├─ teardown_child.py
│  │     ├─ test_config.py
│  │     ├─ test_database.py
│  │     ├─ test_errors.py
│  │     ├─ test_events.py
│  │     ├─ test_health.py
│  │     ├─ test_identity_login_name.py
│  │     ├─ test_identity_migration.py
│  │     ├─ test_identity_settings.py
│  │     ├─ test_identity_tokens.py
│  │     ├─ test_middleware.py
│  │     ├─ test_migrations.py
│  │     ├─ test_owner_no_web_path.py
│  │     ├─ test_owner_setup_cli.py
│  │     ├─ test_owner_setup_service.py
│  │     ├─ test_owner_token_roles.py
│  │     ├─ test_postgres_integration.py
│  │     ├─ test_queueing_budget.py
│  │     ├─ test_queueing_domain.py
│  │     ├─ test_queueing_escalation.py
│  │     ├─ test_queueing_flow.py
│  │     ├─ test_queueing_grants.py
│  │     ├─ test_queueing_loop.py
│  │     ├─ test_queueing_loop_db.py
│  │     ├─ test_queueing_queue.py
│  │     ├─ test_queueing_schema.py
│  │     ├─ test_research_broker.py
│  │     ├─ test_research_contract.py
│  │     ├─ test_research_locator.py
│  │     ├─ test_research_normalize.py
│  │     ├─ test_research_registry.py
│  │     ├─ test_scratch_concurrency.py
│  │     ├─ test_scratch_grants.py
│  │     ├─ test_scratch_migration.py
│  │     ├─ test_scratch_purge.py
│  │     ├─ test_scratch_records.py
│  │     ├─ test_scratch_schema.py
│  │     ├─ test_scratch_service_validation.py
│  │     ├─ test_scratch_store_items.py
│  │     ├─ test_scratch_store_use.py
│  │     ├─ test_scratch_validation.py
│  │     ├─ test_security.py
│  │     ├─ test_server.py
│  │     ├─ test_shared_memory_candidates.py
│  │     ├─ test_shared_memory_contract.py
│  │     ├─ test_shared_memory_effective_view.py
│  │     ├─ test_shared_memory_grants.py
│  │     ├─ test_shared_memory_migration.py
│  │     ├─ test_shared_memory_policy_source.py
│  │     ├─ test_shared_memory_promotion_refused.py
│  │     ├─ test_shared_memory_rules_lifecycle.py
│  │     ├─ test_shared_memory_rules_precedence.py
│  │     ├─ test_shared_memory_service_guards.py
│  │     ├─ test_shared_memory_service_manage.py
│  │     ├─ test_shared_memory_service_read.py
│  │     ├─ test_tools_approvals.py
│  │     ├─ test_tools_broker.py
│  │     ├─ test_tools_credentials.py
│  │     ├─ test_tools_migration.py
│  │     ├─ test_tools_policy.py
│  │     ├─ test_tools_postgres.py
│  │     ├─ test_tools_postgres_roles.py
│  │     ├─ test_tools_registry.py
│  │     ├─ test_tools_runner.py
│  │     ├─ test_tools_scope.py
│  │     ├─ tools_store_contract.py
│  │     └─ tools_support.py
│  ├─ cli/
│  │  └─ README.md
│  └─ web/
│     └─ README.md
├─ benchmarks/
│  ├─ README.md
│  ├─ __init__.py
│  ├─ json_input.py
│  ├─ schema_validation.py
│  ├─ schemas/
│  │  ├─ result-v1.schema.json
│  │  └─ task-v1.schema.json
│  ├─ validate_result.py
│  ├─ validate_task.py
│  └─ tests/
│     ├─ __init__.py
│     ├─ test_validate_result.py
│     ├─ test_validate_task.py
│     └─ fixtures/
│        ├─ result-schema/
│        │  ├─ invalid/
│        │  └─ valid/
│        └─ task-schema/
│           ├─ invalid/
│           └─ valid/
├─ docs/
│  ├─ ARCHITECTURE.md
│  ├─ BENCHMARK_EVALUATOR.md
│  ├─ DEPLOYMENT_UPDATE.md
│  ├─ IMPLEMENTATION_BACKLOG.md
│  ├─ ISSUE_MAP.md
│  ├─ MEMORY_ARCHITECTURE.md
│  ├─ MODEL_CANDIDATES.md
│  ├─ NOTIFICATION_POLICY.md
│  ├─ OBSERVABILITY.md
│  ├─ REPOSITORY_STRUCTURE.md
│  ├─ REQUIREMENTS_FREEZE_REVIEW.md
│  ├─ SECURITY_RBAC_AUDIT.md
│  ├─ SECURITY_TOOL_PERMISSIONS.md
│  ├─ UI_DESIGN.md
│  └─ decisions/
│     ├─ 0002-start-workspace-implementation-before-model-comparison.md
│     ├─ 0003-backend-cli-web-implementation-stack.md
│     ├─ 0004-rbac-capability-and-audit-policy.md
│     ├─ 0005-owner-setup-and-recovery.md
│     ├─ 0006-tool-broker-policy.md
│     ├─ 0007-task-queue-budget-and-loop-policy.md
│     └─ README.md
└─ evaluator/
   └─ README.md
```

## 各領域の役割

| 配置 | 役割 |
| --- | --- |
| [REQUIREMENTS.md](../REQUIREMENTS.md) / [AGENTS.md](../AGENTS.md) | 要件と Agent の作業ルールの正本 |
| [docs/](./) | Architecture、各機能の設計、Backlog、Issue 対応表 |
| [docs/decisions/](decisions/README.md) | 重要な仕様・設計判断の提案と承認経緯 |
| [apps/backend/](../apps/backend/README.md) | Core API、認証・権限、Orchestrator、Memory、Tool Broker（現在はSkeleton: REST / Event経路、Health、DB接続、Migration。Owner の初期設定・復旧は server-local の管理コマンド、PAW-021） |
| [apps/web/](../apps/web/README.md) | Backend API を利用する Web UI |
| [apps/cli/](../apps/cli/README.md) | Web と同じ Backend API を利用する CLI |
| [benchmarks/](../benchmarks/README.md) | Benchmark Task、候補 Agent の比較、公開可能な評価用データの準備領域 |
| [evaluator/](../evaluator/README.md) | Test / Acceptance Criteria による機械的検証の準備領域 |
| [.github/](../.github/CI.md) | Issue / PR Template、CI、自動レビュー、Repository 検証スクリプト |

正本文書は既存の `docs/` 内の位置を維持します。
[LICENSE](../LICENSE) は Apache-2.0 です。

## 配置と実装順序

Application の配置先は `apps/backend/`・`apps/web/`・`apps/cli/` とします。
Web / CLI は同じ Backend API を利用し、権限の最終判定は Backend が行います。
Core Backend は GPU 非依存とし、Local Model Runtime を停止できる構造にします。
具体的な Service / Package の分割は、[Architecture](ARCHITECTURE.md) の境界に従って実装フェーズで選定します。

実装は [Benchmark / Evaluator 設計](BENCHMARK_EVALUATOR.md) と
[Implementation Backlog](IMPLEMENTATION_BACKLOG.md) に従い、Benchmark / Evaluator、Model 選定、Workspace 本体の順で進めます。
PAW-010 は PAW-001 に依存します。Backend の最小 Application Skeleton である PAW-020 は Backlog 上は Model 比較 Run の PAW-017 に依存しますが、
[Decision 0002](decisions/0002-start-workspace-implementation-before-model-comparison.md)（Approved）により、PAW-017 の完了を待たずに着手できます。

Backend / CLI / Web の言語と Framework は [Decision 0003](decisions/0003-backend-cli-web-implementation-stack.md)（Approved）で決まっています（承認範囲は決定を参照）。
Deployment の具体方式など、それ以外は [Requirements Freeze Review](REQUIREMENTS_FREEZE_REVIEW.md) の実装時選択として扱い、
該当 Issue で決めた構成に合わせてこの文書を更新します。
Backend の構成と起動方法は [apps/backend/README.md](../apps/backend/README.md) を参照してください。

## Runtime データとの境界

[Memory Architecture](MEMORY_ARCHITECTURE.md) に記載する `/srv/personal-ai/` は運用時の専用領域です。
実際の Recovery Repository、User Memory、Raw Conversation、Credential、Model Weights、Private Dataset はこの Public Repository に保存しません。
データの扱いは [Security Policy](../SECURITY.md) に従います。
