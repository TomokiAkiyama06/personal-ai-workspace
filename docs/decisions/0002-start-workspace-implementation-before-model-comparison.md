# Workspace本体の実装をModel比較Runより先に始める

- Status: Approved
- Date: 2026-09-24
- Scope: [Implementation Backlog](../IMPLEMENTATION_BACKLOG.md) の順序（PAW-017 → PAW-020）
- Supersedes: なし
- Approval: 2026-09-24、Humanが作業Session内で承認

## 背景

[Implementation Backlog](../IMPLEMENTATION_BACKLOG.md) と [Repository構造](../REPOSITORY_STRUCTURE.md) は、
Benchmark / Evaluator、Model選定、Workspace本体の順で進めると定めている。
その結果、Backendの最小Application Skeletonである PAW-020 は、Model比較Runの PAW-017 に依存する。
PAW-017 は Seed Benchmark Dataset の PAW-016（`needs: human-decision`）に依存し、GPU / Server上での実行も必要とする。

2026-09-24 に Human が、実装環境が対象Server上にあることを理由に、残りのIssueを
Human判断が必要な内容を除いて全て進めるよう指示した。

## 提案

1. PAW-020 以降の着手を PAW-017 の完了に依存させない。
   PAW-017 への依存は「Model選定後にWorkspace本体を作る」という方針上の順序であり、Backendの技術的な依存ではない。
2. Repository開発のAgentは、Benchmark結果ではなく [AGENTS.md](../../AGENTS.md) の運用に従う。
   この決定はProduct RuntimeのModel選定（PAW-017 / 018 / 019）の結果に影響しない。
3. PAW-016 と PAW-017 は自動実行しない。
   Benchmark Harness（PAW-012〜015、018、019）の実装は進めるが、実Modelを使う比較RunはHumanの明示指示を待つ。
   PAW-016 と PAW-023 の内容は、Humanの判断なしに確定しない。
4. Model選定の結果に依存する境界（PAW-030 Agent Adapter、PAW-036 GPU Scheduler、PAW-041 Memory Worker連携）は、
   特定Modelへ固定せず、Provider Interface越しに実装する。

## リスク

- Model選定の結果でMemory Worker / Embedding / Rerankerの要件が変わると、PAW-040〜043 のSchemaやRetrieval設計に手戻りが出る。
- Repository開発に使うLocal Coding Modelの品質がBenchmarkで裏付けられていない。
  品質の担保は、Test、Evaluator、独立Reviewに依存する。

## 承認後の扱い

承認されたため、PAW-020 以降のPRは本Decisionを参照する。
Implementation Backlogの各Issueの本文（Goal / Acceptance Criteria / Depends on）は書き換えない。
一方で、Backlog・Repository構造・Decisionの目次を読むAgentが古い順序でPAW-020を止めないよう、
次の4か所へ本Decisionへの参照注記だけを追記する。

- [Implementation Backlog](../IMPLEMENTATION_BACKLOG.md) の「運用ルール」
- [Repository構造](../REPOSITORY_STRUCTURE.md) の「配置と実装順序」
- [設計判断の記録](README.md)
- [Backend README](../../apps/backend/README.md)（最初の実装Issueの依存の記述）

これらの注記と、各Issueの`Depends on`が食い違う場合は、承認済みのDecisionを優先する。
