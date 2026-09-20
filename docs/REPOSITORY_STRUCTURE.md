# Repository のディレクトリ構造

現在の Repository は要件・設計文書、GitHub 運用と CI、Application と Benchmark / Evaluator の配置先で構成します。
`apps/`・`evaluator/` は README による役割の整理までです。`benchmarks/`にはTask / Evaluator Result
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
│  │  └─ README.md
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
| [apps/backend/](../apps/backend/README.md) | Core API、認証・権限、Orchestrator、Memory、Tool Broker |
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
PAW-010 は PAW-001 に依存し、Backend の最小 Application Skeleton である PAW-020 は Model 比較 Run の PAW-017 に依存します。

言語、Framework や Deployment の具体方式は [Requirements Freeze Review](REQUIREMENTS_FREEZE_REVIEW.md) の実装時選択として扱い、
該当 Issue で決めた構成に合わせてこの文書を更新します。

## Runtime データとの境界

[Memory Architecture](MEMORY_ARCHITECTURE.md) に記載する `/srv/personal-ai/` は運用時の専用領域です。
実際の Recovery Repository、User Memory、Raw Conversation、Credential、Model Weights、Private Dataset はこの Public Repository に保存しません。
データの扱いは [Security Policy](../SECURITY.md) に従います。
