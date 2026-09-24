# Backend / CLI / Web の実装スタック

- Status: Approved
- Date: 2026-09-24
- Scope: PAW-020 と、それ以降の `apps/backend`・`apps/cli`・`apps/web`
- Supersedes: なし
- Approval: 2026-09-24、Humanが作業Session内で、提案した構成（Python + FastAPI + PostgreSQL）を承認

## 背景

[Requirements Freeze Review](../REQUIREMENTS_FREEZE_REVIEW.md) は「Backend / frontend frameworkの最終選択」を実装時選択として残している。
[Repository構造](../REPOSITORY_STRUCTURE.md) も、言語とFrameworkは該当Issueで決めた構成に合わせて文書を更新すると定めている。
PAW-020 の受け入れ基準は、HTTPS REST API、WebSocket / SSE、PostgreSQL接続、Health endpoint、
config / migration / testの基本構成である。
[REQUIREMENTS.md](../../REQUIREMENTS.md) は Client を Tauri + React + TypeScript（`[PROVISIONAL]`）、Mobile を Web / PWA としている。

## 提案

| 領域 | 選択 |
| --- | --- |
| Backend（`apps/backend`） | Python 3.13、FastAPI + Uvicorn（REST / SSE / WebSocket）、Pydantic v2 |
| DB | PostgreSQL + pgvector、SQLAlchemy 2.x + psycopg 3、Alembic（migration） |
| CLI（`apps/cli`） | Python。Backendの公開HTTP APIだけを呼ぶClientとし、権限の最終判定はBackendに置く |
| Web（`apps/web`） | React + TypeScript + Vite。DesktopのTauriとMobile PWAは同じWeb Appを再利用する |
| Test / Lint | Python: 標準`unittest`とRuff（既存CIと同じ）。Web: Vitest、Lint / FormatはBiomeまたはESLint（PAW-060で確定） |
| Package管理 | Python: uv + `pyproject.toml`。Web: pnpm |

Core BackendはGPU非依存とし、Local Model Runtimeを停止できる構造を維持する。
Application codeのFormat / Lint / TestはCIへ追加する（PAW-004で予定済み）。

## 選定理由

- 既存のBenchmark / EvaluatorとCI（Python 3.13、Ruff、unittest、jsonschema）と一貫し、実装とToolingを共有できる。
- Local Model RuntimeのOpenAI互換API、Embedding / Reranker、Agent subprocess管理のEcosystemが厚い。
- 要件が固定するPostgreSQL + pgvectorをPythonから扱いやすい。
- FastAPIがOpenAPIを自動生成するため、Web / CLIが同じAPI契約を使える。

## 代替案

- Backendも TypeScript（Node）で統一: WebとTypeを共有できるが、既存資産とML / Agent周辺のEcosystemと一貫しない。
- Go: 単一binary配布とconcurrencyに優れるが、既存資産とLocal Model連携Libraryが薄く、初期速度が落ちる。

## 承認後の扱い

承認されたため、PAW-020 でこの構成のSkeletonとCIを追加し、[Repository構造](../REPOSITORY_STRUCTURE.md) を更新する。
Web側のLint / Format Toolは、PAW-060 で確定する。
