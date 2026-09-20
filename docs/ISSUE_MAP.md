# GitHub Issue Map

Repository: `TomokiAkiyama06/personal-ai-workspace`
状態確認日時: 2026-09-20T11:41:56.434357+00:00

Backlogの安定IDとGitHub Issueの対応表。状態は上記日時のスナップショットで、最新状態は各Issueを参照する。

## Source

正本: [IMPLEMENTATION_BACKLOG.md](IMPLEMENTATION_BACKLOG.md)（更新日: 2026-09-20）。
登録時に参照した提供ファイルをRepository内へ保存し、各Issueの本文・依存関係を原文から再検証できるようにした。

- 登録時入力のSHA-256: `5d4c3ab95da26376d97fcb7aa27799f3fe9fa523630f4577f1075168c398447c`
- 格納版のSHA-256: `b5adbd6fd0e4735342733dd81569e7d06036f77ec5ffcc7aecada6ab910f9d3a`

格納時の差分は、3行目の更新日のMarkdown改行記法を「行末space 2文字」からbackslashへ置き換えた点のみ。
表示とGoal / Acceptance Criteria / Depends on / Needsは変えていない。
各IssueのSource欄には、登録時入力の情報とSHA-256を履歴として保持している。

Goal本文がない項目は「原文にGoal本文の記載なし」と明記し、Acceptance Criteriaは原文を維持した。
Needs未記載の27件は、登録時のUser確認に従い `not specified` として扱い、needsラベルを追加していない。

## Milestones

| Backlog Phase | Milestone | Issue数 |
| --- | --- | ---: |
| Phase 0 / Phase 1 | M0 — Benchmark & Evaluator | 14 |
| Phase 2 | M1 — Core Backend & Auth | 9 |
| Phase 3 | M2 — Agent Runtime & Scheduler | 8 |
| Phase 4 | M3 — Memory System | 8 |
| Phase 5 | M4 — Research Layer | 4 |
| Phase 6 | M5 — UI & Operations | 9 |

## PAW ID → GitHub Issue

| PAW ID | GitHub Issue | Title | Milestone | Current state |
| --- | --- | --- | --- | --- |
| PAW-001 | [#3](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/3) | PAW-001: 要件文書のSTALE記述を整理する | M0 — Benchmark & Evaluator | OPEN |
| PAW-002 | [#4](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/4) | PAW-002: GitHub Labelsを初期作成する | M0 — Benchmark & Evaluator | OPEN |
| PAW-003 | [#5](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/5) | PAW-003: Claude Code自動PR Review workflowを追加する | M0 — Benchmark & Evaluator | OPEN |
| PAW-004 | [#6](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/6) | PAW-004: 最小CI workflowを追加する | M0 — Benchmark & Evaluator | OPEN |
| PAW-010 | [#7](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/7) | PAW-010: Benchmark Task Schemaを定義する | M0 — Benchmark & Evaluator | OPEN |
| PAW-011 | [#8](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/8) | PAW-011: Evaluator Result Schemaを定義する | M0 — Benchmark & Evaluator | OPEN |
| PAW-012 | [#9](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/9) | PAW-012: 隔離Worktree Runnerを実装する | M0 — Benchmark & Evaluator | OPEN |
| PAW-013 | [#10](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/10) | PAW-013: Test / Hidden Acceptance Runnerを実装する | M0 — Benchmark & Evaluator | OPEN |
| PAW-014 | [#11](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/11) | PAW-014: Metrics Collectorを実装する | M0 — Benchmark & Evaluator | OPEN |
| PAW-015 | [#12](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/12) | PAW-015: Coding Agent Candidate Adapter Interfaceを実装する | M0 — Benchmark & Evaluator | OPEN |
| PAW-016 | [#13](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/13) | PAW-016: Seed Benchmark Datasetを作成する | M0 — Benchmark & Evaluator | OPEN |
| PAW-017 | [#14](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/14) | PAW-017: Main Coding Model比較Runを実施する | M0 — Benchmark & Evaluator | OPEN |
| PAW-018 | [#15](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/15) | PAW-018: Memory Worker専用Benchmarkを実装・実行する | M0 — Benchmark & Evaluator | OPEN |
| PAW-019 | [#16](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/16) | PAW-019: Embedding / Reranker Benchmarkを実装する | M0 — Benchmark & Evaluator | OPEN |
| PAW-020 | [#17](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/17) | PAW-020: Backendの最小Application Skeletonを作る | M1 — Core Backend & Auth | OPEN |
| PAW-021 | [#18](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/18) | PAW-021: Initial Owner Setup / Recovery CLIを実装する | M1 — Core Backend & Auth | OPEN |
| PAW-022 | [#19](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/19) | PAW-022: Workspace Login / Session / Password Policyを実装する | M1 — Core Backend & Auth | OPEN |
| PAW-023 | [#20](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/20) | PAW-023: Passkey / Step-up Authenticationを実装する | M1 — Core Backend & Auth | OPEN |
| PAW-024 | [#21](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/21) | PAW-024: User Invite / Device Pairingを実装する | M1 — Core Backend & Auth | OPEN |
| PAW-025 | [#22](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/22) | PAW-025: RBAC / Capability Backend Enforcementを実装する | M1 — Core Backend & Auth | OPEN |
| PAW-026 | [#23](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/23) | PAW-026: Project CRUD / Membership / Lifecycleを実装する | M1 — Core Backend & Auth | OPEN |
| PAW-027 | [#24](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/24) | PAW-027: Repository Registration / Per-user Checkoutを実装する | M1 — Core Backend & Auth | OPEN |
| PAW-028 | [#25](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/25) | PAW-028: GitHub User Connection (`gh auth`) Integrationを実装する | M1 — Core Backend & Auth | OPEN |
| PAW-030 | [#26](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/26) | PAW-030: Shared Codex / Claude Connection Adapterを実装する | M2 — Agent Runtime & Scheduler | OPEN |
| PAW-031 | [#27](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/27) | PAW-031: Tool Broker / Capability Policyを実装する | M2 — Agent Runtime & Scheduler | OPEN |
| PAW-032 | [#28](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/28) | PAW-032: Agent Task Lifecycle / Persistenceを実装する | M2 — Agent Runtime & Scheduler | OPEN |
| PAW-033 | [#29](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/29) | PAW-033: Task Queue / Budget / Loop Detectionを実装する | M2 — Agent Runtime & Scheduler | OPEN |
| PAW-034 | [#30](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/30) | PAW-034: DAG Agent Orchestratorを実装する | M2 — Agent Runtime & Scheduler | OPEN |
| PAW-035 | [#31](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/31) | PAW-035: Parallel Worktree / Integration Nodeを実装する | M2 — Agent Runtime & Scheduler | OPEN |
| PAW-036 | [#32](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/32) | PAW-036: GPU / Compute Resource Schedulerを実装する | M2 — Agent Runtime & Scheduler | OPEN |
| PAW-037 | [#33](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/33) | PAW-037: Kaggle / Full GPU Modeを実装する | M2 — Agent Runtime & Scheduler | OPEN |
| PAW-040 | [#34](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/34) | PAW-040: Memory / Conversation PostgreSQL Schemaを実装する | M3 — Memory System | OPEN |
| PAW-041 | [#35](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/35) | PAW-041: Immediate Journal / Background Consolidationを実装する | M3 — Memory System | OPEN |
| PAW-042 | [#36](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/36) | PAW-042: Memory Conflict / Versioning / Freshnessを実装する | M3 — Memory System | OPEN |
| PAW-043 | [#37](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/37) | PAW-043: Hybrid Retrieval Pipelineを実装する | M3 — Memory System | OPEN |
| PAW-044 | [#38](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/38) | PAW-044: Inferred Preference Confirmation Flowを実装する | M3 — Memory System | OPEN |
| PAW-045 | [#39](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/39) | PAW-045: Memory Markdown Projectionを実装する | M3 — Memory System | OPEN |
| PAW-046 | [#40](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/40) | PAW-046: Shared Memory Administrationを実装する | M3 — Memory System | OPEN |
| PAW-047 | [#41](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/41) | PAW-047: Recovery Repository Projection / Restoreを実装する | M3 — Memory System | OPEN |
| PAW-050 | [#42](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/42) | PAW-050: Research Scratch Store / 24h TTLを実装する | M4 — Research Layer | OPEN |
| PAW-051 | [#43](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/43) | PAW-051: Research Provider Adapter Interfaceを実装する | M4 — Research Layer | OPEN |
| PAW-052 | [#44](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/44) | PAW-052: Evidence / Claim Provenanceを実装する | M4 — Research Layer | OPEN |
| PAW-053 | [#45](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/45) | PAW-053: Research Privacy Filter / Query Minimizationを実装する | M4 — Research Layer | OPEN |
| PAW-060 | [#46](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/46) | PAW-060: Web UI Application Shell / Authentication UIを実装する | M5 — UI & Operations | OPEN |
| PAW-061 | [#47](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/47) | PAW-061: Project / Repo / Member UIを実装する | M5 — UI & Operations | OPEN |
| PAW-062 | [#48](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/48) | PAW-062: Task / Agent DAG UIを実装する | M5 — UI & Operations | OPEN |
| PAW-063 | [#49](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/49) | PAW-063: Memory 3-pane / History Graph UIを実装する | M5 — UI & Operations | OPEN |
| PAW-064 | [#50](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/50) | PAW-064: Admin Usage / Quota UIを実装する | M5 — UI & Operations | OPEN |
| PAW-065 | [#51](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/51) | PAW-065: Notification Centerを実装する | M5 — UI & Operations | OPEN |
| PAW-066 | [#52](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/52) | PAW-066: System Health / Observability Backendを実装する | M5 — UI & Operations | OPEN |
| PAW-067 | [#53](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/53) | PAW-067: System Health UIを実装する | M5 — UI & Operations | OPEN |
| PAW-068 | [#54](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/54) | PAW-068: Deployment / Update / Rollback機構を実装する | M5 — UI & Operations | OPEN |

## Registration validation

- 登録予定52件と登録済み52件が一致し、PAW ID重複なし。
- Acceptance Criteria 243項目と依存リンク81件を原文・GitHub API取得結果で全件照合。
- 推奨Label 23件とMilestone 6件の存在・割当を検証。
- Needsラベル: GPU 8件 / Server 17件 / Human decision 2件。
- 全52件をOPENで登録。実装済みかどうかの判定やIssueのCloseは行っていない。
