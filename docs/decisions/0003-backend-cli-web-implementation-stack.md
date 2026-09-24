# Backend / CLI / Web の実装スタック

- Status: Approved
- Date: 2026-09-24
- Scope: PAW-020 と、それ以降の `apps/backend`・`apps/cli`・`apps/web`
- Supersedes: なし
- Approval: 2026-09-24、Humanが作業Session内で、提示された選択肢「Python + FastAPI + PostgreSQL」を承認（下記の「承認範囲」）

## 背景

[Requirements Freeze Review](../REQUIREMENTS_FREEZE_REVIEW.md) は「Backend / frontend frameworkの最終選択」を実装時選択として残している。
[Repository構造](../REPOSITORY_STRUCTURE.md) も、言語とFrameworkは該当Issueで決めた構成に合わせて文書を更新すると定めている。
PAW-020 の受け入れ基準は、HTTPS REST API、WebSocket / SSE、PostgreSQL接続、Health endpoint、
config / migration / testの基本構成である。
[REQUIREMENTS.md](../../REQUIREMENTS.md) は Client を Tauri + React + TypeScript（`[PROVISIONAL]`）、Mobile を Web / PWA としている。

## 承認範囲

Humanが承認したのは、承認時に提示した次の選択肢です。
Backend / CLI は Python 3.13 と FastAPI、SQLAlchemy 2 + psycopg 3 + Alembic、PostgreSQL、
Web は React + TypeScript + Vite、既存の Ruff / unittest / CI との一貫性。

下表のうち上記に含まれない項目（Uvicorn、Pydantic、uv と `pyproject.toml`、pnpm、Vitest、Web の Lint / Format Tool、
DesktopのTauriとMobile PWAでWeb Appを再利用する方針）は、**実装Issueが選ぶ既定値**で、
この承認では確定しません。実装Issueは、理由を示せば新しいDecisionなしで変更できます。
Tauri / PWA は [REQUIREMENTS.md](../../REQUIREMENTS.md) の `[PROVISIONAL]` のままです。

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
Web側のLint / Format Toolは、Web実装の最初のIssue（PAW-060）で、CIへ追加するコマンドとあわせて選ぶ。
PAW-060 のAcceptance Criteriaには含まれないため、選定はそのIssueの実装詳細として扱う。
