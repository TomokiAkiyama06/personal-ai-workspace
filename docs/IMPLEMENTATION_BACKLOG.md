# Personal AI Workspace — Implementation Backlog

更新日: 2026-09-20\
Status: Requirements Freeze Candidate / Issue registration source

この文書は、要件定義から実装Issueへ落とし込むための一次バックログです。
GitHub Issue作成後も `PAW-xxx` を安定IDとして残し、Issue番号との対応は別途追記できます。

## 運用ルール

- 実装開始順は **Benchmark / Evaluator Harness → Model選定 → Workspace本体** を基本とする。
  ただし [Decision 0002](decisions/0002-start-workspace-implementation-before-model-comparison.md)（Approved）により、
  PAW-020 以降はPAW-017の完了を待たずに着手できる。各Issueの`Depends on`にあるPAW-017への依存はこのDecisionで解除する。
- `BLOCKING` なUser判断が発生した場合は勝手に仕様を補完せず確認する。
- Main Coding Model / Memory Worker / Research Worker / Embedding / RerankerはBenchmark後に決定する。
- Codexを初期開発のPrimary Implementation Agentとする。
- Claude Codeは独立Reviewを基本とする。
- MergeはHuman-controlled。Task内で明示的なMerge許可がある場合のみAgentがMerge可能。
- Public RepositoryにSecret、Private Memory、Raw Conversation、Recovery実データを入れない。
- GPU/Server実機が必要なIssueは `needs: gpu` / `needs: server` を付ける。

## 推奨Labels

### Type
- `type: feature`
- `type: bug`
- `type: refactor`
- `type: docs`
- `type: chore`
- `type: research`

### Area
- `area: benchmark`
- `area: backend`
- `area: frontend`
- `area: agent`
- `area: memory`
- `area: research`
- `area: security`
- `area: infra`
- `area: recovery`
- `area: observability`
- `area: github`

### Priority
- `priority: high`
- `priority: normal`
- `priority: low`

### Environment / decision
- `needs: gpu`
- `needs: server`
- `needs: human-decision`

---

# Phase 0 — Repository / Requirements Hygiene

## PAW-001: 要件文書のSTALE記述を整理する
- Type: `type: docs`
- Area: `area: github`
- Priority: `priority: high`
- Needs: none
- Depends on: none

### Goal
`REQUIREMENTS.md` と専用設計文書に残っている旧OPEN・旧Phase・旧Backup記述を、Freeze済みの最新方針へ統一する。

### Acceptance Criteria
- [ ] `REQUIREMENTS.md` の旧OPEN一覧で確定済み項目が残っていない
- [ ] 「Core Backend先行」と「Benchmark先行」の矛盾をBenchmark先行へ統一
- [ ] RecoveryはDedicated Recovery Repository方針へ統一
- [ ] Benchmark後に決める項目は `[BENCHMARK]` 等として明示
- [ ] `git diff --check` が通る

## PAW-002: GitHub Labelsを初期作成する
- Type: `type: chore`
- Area: `area: github`
- Priority: `priority: high`
- Needs: none
- Depends on: none

### Acceptance Criteria
- [ ] 本文書の推奨Labelを作成
- [ ] Issue Templateが参照する `type: bug` / `type: feature` / `area: benchmark` が存在
- [ ] 色と説明文を設定
- [ ] 重複Labelを作らない

## PAW-003: Claude Code自動PR Review workflowを追加する
- Type: `type: feature`
- Area: `area: github`, `area: agent`
- Priority: `priority: high`
- Needs: none
- Depends on: none

### Goal
PR作成・更新時にClaude Codeで独立Reviewを実行する。

### Acceptance Criteria
- [ ] `.github/workflows/claude-review.yml` を追加
- [ ] Subscription OAuth token利用時は `CLAUDE_CODE_OAUTH_TOKEN` をSecretから参照
- [ ] `pull_request` の opened / synchronize / ready_for_review / reopened を対象
- [ ] PRへReviewコメントを投稿可能
- [ ] Public Repo向けにSecretをlogへ出さない
- [ ] Workflow自体のPRで動作確認

## PAW-004: 最小CI workflowを追加する
- Type: `type: chore`
- Area: `area: github`, `area: infra`
- Priority: `priority: normal`
- Needs: none
- Depends on: `PAW-001`

### Acceptance Criteria
- [ ] 初期コード構成確定後にformat/lint/testを実行
- [ ] `main` RulesetへRequired Checkとして追加可能なjob名を固定
- [ ] Secretsへの不要なwrite permissionを与えない

---

# Phase 1 — Benchmark / Evaluator Harness

## PAW-010: Benchmark Task Schemaを定義する
- Type: `type: feature`
- Area: `area: benchmark`
- Priority: `priority: high`
- Needs: none
- Depends on: `PAW-001`

### Acceptance Criteria
- [ ] Task ID / Repo / starting commit / issue text / visible checks / hidden checks metadataを表現可能
- [ ] Historical / Spec / Injected Bugを区別可能
- [ ] Machine-readable schemaを定義
- [ ] Schema versionを持つ

## PAW-011: Evaluator Result Schemaを定義する
- Type: `type: feature`
- Area: `area: benchmark`
- Priority: `priority: high`
- Depends on: `PAW-010`

### Acceptance Criteria
- [ ] FAIL_TO_PASS / PASS_TO_PASSを保存可能
- [ ] Build / unit / integration / lint / type / security / forbidden change結果を保存可能
- [ ] wall clock / token / tool failure / Peak VRAM / human correction timeを保存可能
- [ ] Candidate model/runtime/quantizationを記録可能

## PAW-012: 隔離Worktree Runnerを実装する
- Type: `type: feature`
- Area: `area: benchmark`, `area: infra`
- Priority: `priority: high`
- Needs: `needs: server`
- Depends on: `PAW-010`

### Acceptance Criteria
- [ ] 指定commitからisolated worktreeを作成
- [ ] Candidateごとに同一starting stateを再現
- [ ] timeout/cancel時にcleanup可能
- [ ] main/default branchを汚さない
- [ ] 実行ログを保存

## PAW-013: Test / Hidden Acceptance Runnerを実装する
- Type: `type: feature`
- Area: `area: benchmark`
- Priority: `priority: high`
- Needs: `needs: server`
- Depends on: `PAW-010`, `PAW-012`

### Acceptance Criteria
- [ ] Visible TestとHidden Testを分離
- [ ] CandidateからHidden Test本文を読めない
- [ ] timeout / exit code / stdout / stderrを記録
- [ ] Known-good commitでPASS、buggy commitでFAILを検証できる

## PAW-014: Metrics Collectorを実装する
- Type: `type: feature`
- Area: `area: benchmark`, `area: observability`
- Priority: `priority: high`
- Needs: `needs: gpu`, `needs: server`
- Depends on: `PAW-011`, `PAW-012`

### Acceptance Criteria
- [ ] wall clock / steps / retries / tool callsを収集
- [ ] GPU利用時にPeak VRAM / utilizationを取得
- [ ] token/context usageを取得可能なRuntimeでは記録
- [ ] Candidate間で同一formatへ正規化

## PAW-015: Coding Agent Candidate Adapter Interfaceを実装する
- Type: `type: feature`
- Area: `area: benchmark`, `area: agent`
- Priority: `priority: high`
- Depends on: `PAW-010`

### Acceptance Criteria
- [ ] model/runtime差を共通interfaceで隠蔽
- [ ] Local model / Codex / Claudeを将来同じTask Runnerから呼べる
- [ ] prompt/tool schema/context limitを明示設定可能
- [ ] retry/timeout/cancelをBackendから制御可能

## PAW-016: Seed Benchmark Datasetを作成する
- Type: `type: research`
- Area: `area: benchmark`
- Priority: `priority: high`
- Needs: `needs: human-decision`
- Depends on: `PAW-010`, `PAW-013`

### Acceptance Criteria
- [ ] Historical / Spec / Injected Bugを含む
- [ ] 単純編集だけでなくmulti-file / repo exploration / test修正を含む
- [ ] 難易度とカテゴリを記録
- [ ] Golden behaviorをExecutable Testで確認

## PAW-017: Main Coding Model比較Runを実施する
- Type: `type: research`
- Area: `area: benchmark`, `area: agent`
- Priority: `priority: high`
- Needs: `needs: gpu`, `needs: server`
- Depends on: `PAW-014`, `PAW-015`, `PAW-016`

### Acceptance Criteria
- [ ] CandidateをNVMeへstageして比較
- [ ] 同一Harness / prompt / tool / timeout条件
- [ ] Resolved@1 / human correction time / Peak VRAM等を比較
- [ ] 採用理由と不採用理由を文書化

## PAW-018: Memory Worker専用Benchmarkを実装・実行する
- Type: `type: research`
- Area: `area: benchmark`, `area: memory`
- Priority: `priority: high`
- Needs: `needs: gpu`, `needs: server`
- Depends on: `PAW-014`

### Acceptance Criteria
- [ ] Memory抽出Recall
- [ ] 不要Memory生成率
- [ ] Scope分類精度
- [ ] Confirmed / Inferred分類精度
- [ ] Conflict / Supersedes判定
- [ ] JSON Schema遵守率
- [ ] latency / VRAM比較

## PAW-019: Embedding / Reranker Benchmarkを実装する
- Type: `type: research`
- Area: `area: benchmark`, `area: memory`
- Priority: `priority: normal`
- Needs: `needs: gpu`, `needs: server`
- Depends on: `PAW-014`

### Acceptance Criteria
- [ ] Seed Retrieval BenchmarkでRecall@K / MRR / nDCGを測定
- [ ] permission leakage = 0を検証
- [ ] stale/superseded/scope誤選択率を測定
- [ ] latency / CPU / GPU / VRAMを比較

---

# Phase 2 — Core Backend / Auth / Project / Repo

## PAW-020: Backendの最小Application Skeletonを作る
- Type: `type: feature`
- Area: `area: backend`
- Priority: `priority: high`
- Depends on: `PAW-017`

### Acceptance Criteria
- [ ] HTTPS REST APIの基本構造
- [ ] WebSocket/SSEのイベント経路
- [ ] PostgreSQL接続
- [ ] Health endpoint
- [ ] config / migration / testの基本構成

## PAW-021: Initial Owner Setup / Recovery CLIを実装する
- Type: `type: feature`
- Area: `area: security`, `area: backend`
- Priority: `priority: high`
- Needs: `needs: server`
- Depends on: `PAW-020`

### Acceptance Criteria
- [ ] first web visitorをOwnerにしない
- [ ] CLIからOwner初期作成
- [ ] one-time setup/recovery token
- [ ] Auditへ記録
- [ ] Owner Passkey必須化へ接続可能

## PAW-022: Workspace Login / Session / Password Policyを実装する
- Type: `type: feature`
- Area: `area: security`, `area: backend`
- Priority: `priority: high`
- Depends on: `PAW-020`, `PAW-021`

### Acceptance Criteria
- [ ] Argon2id
- [ ] Secure + HttpOnly server-side session
- [ ] normal 30日 inactivity / Remember Me最大90日
- [ ] progressive login backoff
- [ ] password reset時のsession invalidation

## PAW-023: Passkey / Step-up Authenticationを実装する
- Type: `type: feature`
- Area: `area: security`, `area: backend`
- Priority: `priority: high`
- Needs: `needs: human-decision`
- Depends on: `PAW-022`

### Acceptance Criteria
- [ ] Owner/Admin Passkey必須
- [ ] Userは任意
- [ ] sensitive operationで30分step-up window
- [ ] 複数Passkey登録
- [ ] device revoke可能

## PAW-024: User Invite / Device Pairingを実装する
- Type: `type: feature`
- Area: `area: security`, `area: backend`
- Priority: `priority: normal`
- Depends on: `PAW-022`, `PAW-023`

### Acceptance Criteria
- [ ] invite-only registration
- [ ] one-time invite / pair token
- [ ] QR/link pairing
- [ ] Owner/Admin pairingは既存trusted device承認
- [ ] Invited / Active / Pending deletion / Deleted状態

## PAW-025: RBAC / Capability Backend Enforcementを実装する
- Type: `type: feature`
- Area: `area: security`, `area: backend`
- Priority: `priority: high`
- Depends on: `PAW-020`

### Acceptance Criteria
- [ ] System Owner/Admin/User
- [ ] Project Manager/Contributor/Viewer
- [ ] Backendでpermission判定
- [ ] LLMが権限を迂回できない
- [ ] Audit eventを出力

## PAW-026: Project CRUD / Membership / Lifecycleを実装する
- Type: `type: feature`
- Area: `area: backend`
- Priority: `priority: high`
- Depends on: `PAW-025`

### Acceptance Criteria
- [ ] ProjectはRepoなしで作成可能
- [ ] invite-only membership
- [ ] Active / Archived / Pending deletion / Deleted
- [ ] 30日Pending deletion
- [ ] Archiveから復帰可能

## PAW-027: Repository Registration / Per-user Checkoutを実装する
- Type: `type: feature`
- Area: `area: backend`, `area: github`
- Priority: `priority: high`
- Needs: `needs: server`
- Depends on: `PAW-026`

### Acceptance Criteria
- [ ] GitHubからclone
- [ ] Ubuntu既存Repo登録
- [ ] 新規Local/GitHub Repo作成経路
- [ ] Userごとのcheckout分離
- [ ] Project Repoへ管理MDを自動注入しない

## PAW-028: GitHub User Connection (`gh auth`) Integrationを実装する
- Type: `type: feature`
- Area: `area: github`, `area: security`
- Priority: `priority: normal`
- Needs: `needs: server`
- Depends on: `PAW-027`

### Acceptance Criteria
- [ ] Linux UserごとのGitHub認証状態を認識
- [ ] token/private keyをAdmin UIへ表示しない
- [ ] issue / PR / API操作を対象User identityで実行

---

# Phase 3 — Agent Runtime / Tool Broker / Scheduler

## PAW-030: Shared Codex / Claude Connection Adapterを実装する
- Type: `type: feature`
- Area: `area: agent`, `area: security`
- Priority: `priority: high`
- Depends on: `PAW-020`, `PAW-025`

### Acceptance Criteria
- [ ] Workspace-level Codex Connection
- [ ] Workspace-level Claude Connection
- [ ] Secret plaintextをAgent/Userへ出さない
- [ ] usageをUser/Taskへattribution
- [ ] Per-user quota適用

## PAW-031: Tool Broker / Capability Policyを実装する
- Type: `type: feature`
- Area: `area: security`, `area: agent`
- Priority: `priority: high`
- Depends on: `PAW-025`

### Acceptance Criteria
- [ ] read/write/execute/network/credential-use/destructive capability
- [ ] AUTO / SCOPED_AUTO / APPROVAL / STRONG_APPROVAL / DENY
- [ ] credential plaintext取得をDENY
- [ ] task scope / ACL / budgetを強制
- [ ] high-risk actionでapproval event発生

## PAW-032: Agent Task Lifecycle / Persistenceを実装する
- Type: `type: feature`
- Area: `area: agent`, `area: backend`
- Priority: `priority: high`
- Depends on: `PAW-020`

### Acceptance Criteria
- [ ] Queued / Running / Waiting / Paused / Evaluating / Completed / Failed / Cancelled
- [ ] Client切断後もstate保持
- [ ] current step / logs / worktree / review / PR stateを復元
- [ ] Pause / Resume / Cancel / Retry / Restart / Stop Now

## PAW-033: Task Queue / Budget / Loop Detectionを実装する
- Type: `type: feature`
- Area: `area: agent`, `area: backend`
- Priority: `priority: high`
- Depends on: `PAW-032`

### Acceptance Criteria
- [ ] HIGH / NORMAL / LOW
- [ ] Standard / Long / Unlimited preset
- [ ] runtime / step / retry / tool / token / GPU time budget
- [ ] repeated failure loop detection
- [ ] alternative approach / escalation

## PAW-034: DAG Agent Orchestratorを実装する
- Type: `type: feature`
- Area: `area: agent`
- Priority: `priority: high`
- Needs: `needs: server`
- Depends on: `PAW-032`, `PAW-033`

### Acceptance Criteria
- [ ] Taskをdependency DAGへ分解
- [ ] independent nodeをparallel-first実行
- [ ] Planner/Worker/Researcher/Reviewer role
- [ ] node単位Retry/Escalate
- [ ] structured result passing
- [ ] parent ACL/budgetをSub-Agentが超えない

## PAW-035: Parallel Worktree / Integration Nodeを実装する
- Type: `type: feature`
- Area: `area: agent`, `area: github`
- Priority: `priority: high`
- Needs: `needs: server`
- Depends on: `PAW-034`, `PAW-027`

### Acceptance Criteria
- [ ] Write Workerごとに専用worktree/branch
- [ ] 同一Repo並列変更をintegration worktreeへ集約
- [ ] conflict検知
- [ ] integration後にtest/evaluator/review
- [ ] default branchへ直接統合しない

## PAW-036: GPU / Compute Resource Schedulerを実装する
- Type: `type: feature`
- Area: `area: agent`, `area: infra`
- Priority: `priority: high`
- Needs: `needs: gpu`, `needs: server`
- Depends on: `PAW-034`

### Acceptance Criteria
- [ ] actual/reserved VRAM + safety headroom管理
- [ ] context/KVに応じたdynamic concurrency
- [ ] Interactive/Coding/Support/Background/Exclusive class
- [ ] Memory Worker unload / CPU fallback
- [ ] Local/Cloud hybrid scheduling

## PAW-037: Kaggle / Full GPU Modeを実装する
- Type: `type: feature`
- Area: `area: infra`, `area: agent`
- Priority: `priority: normal`
- Needs: `needs: gpu`, `needs: server`
- Depends on: `PAW-036`

### Acceptance Criteria
- [ ] 新規Local GPU Task停止
- [ ] running task safe pause/drain
- [ ] Main/Support model unload
- [ ] VRAM解放確認
- [ ] Exclusive Job終了後reload/resume

---

# Phase 4 — Memory System

## PAW-040: Memory / Conversation PostgreSQL Schemaを実装する
- Type: `type: feature`
- Area: `area: memory`, `area: backend`
- Priority: `priority: high`
- Depends on: `PAW-020`

### Acceptance Criteria
- [ ] Raw Conversation / Session state / Long-term Memoryを分離
- [ ] User / Project / Repo / Shared scope
- [ ] version/status/freshness/provenance/relationを保持
- [ ] pgvector利用可能
- [ ] ACL filter可能なschema

## PAW-041: Immediate Journal / Background Consolidationを実装する
- Type: `type: feature`
- Area: `area: memory`
- Priority: `priority: high`
- Depends on: `PAW-040`, `PAW-018`

### Acceptance Criteria
- [ ] User message時にRaw + Pending Observation即保存
- [ ] background consolidation queue
- [ ] HIGH/NORMAL/LOW優先度
- [ ] GPU unavailable時もPendingを失わない
- [ ] event sequenceで順序保証

## PAW-042: Memory Conflict / Versioning / Freshnessを実装する
- Type: `type: feature`
- Area: `area: memory`
- Priority: `priority: high`
- Depends on: `PAW-040`, `PAW-041`

### Acceptance Criteria
- [ ] supersedes/extends/conflicts等のrelation
- [ ] active versionのみ通常retrieval対象
- [ ] permanent/revalidate/repo_commit/expiring/session_only
- [ ] stale candidate処理
- [ ] manual editで旧version保持

## PAW-043: Hybrid Retrieval Pipelineを実装する
- Type: `type: feature`
- Area: `area: memory`
- Priority: `priority: high`
- Needs: `needs: gpu`
- Depends on: `PAW-019`, `PAW-040`

### Acceptance Criteria
- [ ] ACL/metadata filterをvector検索より先に適用
- [ ] Keyword + Vector hybrid
- [ ] Rerank
- [ ] dedup/conflict handling
- [ ] scope/freshness/confirmed importanceを考慮
- [ ] permission leakage = 0のtest

## PAW-044: Inferred Preference Confirmation Flowを実装する
- Type: `type: feature`
- Area: `area: memory`, `area: frontend`
- Priority: `priority: normal`
- Depends on: `PAW-041`, `PAW-042`

### Acceptance Criteria
- [ ] Observation → Candidate → Confirmation → Confirmed
- [ ] Repo/Project/User scope候補
- [ ] free-text `その他` をstructured previewへ変換
- [ ] Merge/Delete/ACL等high-risk inferenceは自動実行不可

## PAW-045: Memory Markdown Projectionを実装する
- Type: `type: feature`
- Area: `area: memory`, `area: recovery`
- Priority: `priority: high`
- Needs: `needs: server`
- Depends on: `PAW-040`, `PAW-042`

### Acceptance Criteria
- [ ] PostgreSQLをSource of TruthとしてHDDへProjection生成
- [ ] User/Project/Repo/Sharedを分離
- [ ] direct repo injectionなし
- [ ] diff-friendly Markdown
- [ ] projection failure通知

## PAW-046: Shared Memory Administrationを実装する
- Type: `type: feature`
- Area: `area: memory`, `area: security`
- Priority: `priority: normal`
- Depends on: `PAW-025`, `PAW-040`

### Acceptance Criteria
- [ ] Active User read
- [ ] Owner/Admin create/edit/delete/restore
- [ ] Shared Memory Candidate approval
- [ ] Agent auto-promotion拒否
- [ ] System Security PolicyがShared Memoryより優先

## PAW-047: Recovery Repository Projection / Restoreを実装する
- Type: `type: feature`
- Area: `area: recovery`, `area: memory`
- Priority: `priority: high`
- Needs: `needs: server`
- Depends on: `PAW-045`, `PAW-026`, `PAW-025`

### Acceptance Criteria
- [ ] 30分dirty-check batch commit/push
- [ ] machine-readable User/Project/Repo/ACL/Policy/Quota metadata
- [ ] schema/recovery format version
- [ ] Secret/DB dump/WALをGitへ保存しない
- [ ] Fresh Ubuntuから主要状態を再構築するrestore flow
- [ ] credentialは再登録方式

---

# Phase 5 — Research / Knowledge Layer

## PAW-050: Research Scratch Store / 24h TTLを実装する
- Type: `type: feature`
- Area: `area: research`, `area: memory`
- Priority: `priority: normal`
- Depends on: `PAW-020`

### Acceptance Criteria
- [ ] Long-term Memoryから分離
- [ ] created_at + 24h TTL
- [ ] in-use / pinned / promotion-pendingは削除延期
- [ ] Project/Task relation保持

## PAW-051: Research Provider Adapter Interfaceを実装する
- Type: `type: feature`
- Area: `area: research`, `area: agent`
- Priority: `priority: normal`
- Depends on: `PAW-050`

### Acceptance Criteria
- [ ] Direct Web / Docs / GitHub / future OpenCodeを抽象化
- [ ] Provider差をMain Agentから隠蔽
- [ ] source metadataを統一

## PAW-052: Evidence / Claim Provenanceを実装する
- Type: `type: feature`
- Area: `area: research`
- Priority: `priority: normal`
- Depends on: `PAW-050`, `PAW-051`

### Acceptance Criteria
- [ ] claim ↔ source対応
- [ ] fetched_at / published_at / source_type
- [ ] duplicate / contradictionを表現可能
- [ ] 回答・Taskから参照sourceを追跡可能

## PAW-053: Research Privacy Filter / Query Minimizationを実装する
- Type: `type: feature`
- Area: `area: research`, `area: security`
- Priority: `priority: high`
- Depends on: `PAW-031`, `PAW-051`

### Acceptance Criteria
- [ ] Private source全文を外部検索へ送らない
- [ ] Secret/Private Memory/Raw Conversationを除去
- [ ] query抽象化
- [ ] external sendをAudit可能

---

# Phase 6 — UI / Admin / Operations

## PAW-060: Web UI Application Shell / Authentication UIを実装する
- Type: `type: feature`
- Area: `area: frontend`
- Priority: `priority: high`
- Depends on: `PAW-022`, `PAW-023`

### Acceptance Criteria
- [ ] 日本語Default / i18n-ready
- [ ] login/passkey/device management
- [ ] responsive layout
- [ ] Notification Center shell

## PAW-061: Project / Repo / Member UIを実装する
- Type: `type: feature`
- Area: `area: frontend`
- Priority: `priority: high`
- Depends on: `PAW-026`, `PAW-027`

### Acceptance Criteria
- [ ] Project create/archive/delete flow
- [ ] Repo registration
- [ ] Manager/Contributor/Viewer
- [ ] Repo ACL override表示

## PAW-062: Task / Agent DAG UIを実装する
- Type: `type: feature`
- Area: `area: frontend`, `area: agent`
- Priority: `priority: high`
- Depends on: `PAW-032`, `PAW-034`, `PAW-035`

### Acceptance Criteria
- [ ] Task state / Waiting reason
- [ ] DAG node / dependency
- [ ] Agent/model/worktree/current step
- [ ] Pause/Resume/Retry/Restart/Stop Now
- [ ] Review/PR/Evaluator状態

## PAW-063: Memory 3-pane / History Graph UIを実装する
- Type: `type: feature`
- Area: `area: frontend`, `area: memory`
- Priority: `priority: normal`
- Depends on: `PAW-042`, `PAW-043`, `PAW-044`

### Acceptance Criteria
- [ ] Scope Tree / Memory List / Detail
- [ ] 本文/履歴/ソース/詳細tab
- [ ] GitLens風relation graph
- [ ] optimistic lock conflict UI
- [ ] old version restore = new active version

## PAW-064: Admin Usage / Quota UIを実装する
- Type: `type: feature`
- Area: `area: frontend`, `area: security`
- Priority: `priority: normal`
- Depends on: `PAW-025`, `PAW-030`, `PAW-033`

### Acceptance Criteria
- [ ] User別Local/Codex/Claude usage
- [ ] numeric/Unlimited quota
- [ ] privacy-safe purpose categories
- [ ] Private chat/raw memory本文は表示しない

## PAW-065: Notification Centerを実装する
- Type: `type: feature`
- Area: `area: frontend`, `area: observability`
- Priority: `priority: normal`
- Depends on: `PAW-060`

### Acceptance Criteria
- [ ] INFO/WARNING/ERROR/CRITICAL
- [ ] bell + unread badge
- [ ] dedup/aggregation
- [ ] high-risk actionはstep-up policyを維持

## PAW-066: System Health / Observability Backendを実装する
- Type: `type: feature`
- Area: `area: observability`, `area: backend`
- Priority: `priority: normal`
- Needs: `needs: gpu`, `needs: server`
- Depends on: `PAW-020`, `PAW-036`, `PAW-047`

### Acceptance Criteria
- [ ] GPU/VRAM/model residency
- [ ] queue/task/error metrics
- [ ] PostgreSQL health
- [ ] Recovery push status
- [ ] Claude/Codex connection status
- [ ] retention/downsampling policy

## PAW-067: System Health UIを実装する
- Type: `type: feature`
- Area: `area: frontend`, `area: observability`
- Priority: `priority: low`
- Depends on: `PAW-066`

### Acceptance Criteria
- [ ] normal時はcompact
- [ ] abnormal時に詳細展開
- [ ] VRAM/queue/recovery/external service状態
- [ ] Warning/Error/Critical通知へ連携

## PAW-068: Deployment / Update / Rollback機構を実装する
- Type: `type: feature`
- Area: `area: infra`, `area: recovery`
- Priority: `priority: normal`
- Needs: `needs: server`
- Depends on: `PAW-020`, `PAW-047`, `PAW-066`

### Acceptance Criteria
- [ ] versioned release
- [ ] manual update
- [ ] safe drain/checkpoint
- [ ] pre-update health/recovery check
- [ ] backward-compatible migration方針
- [ ] known-good version rollback

---

# Future / Not V1 Blocking

以下はV1の初期実装Issueへ原則登録しないか、`priority: low` / Future milestoneとして扱う。

- Dedicated IDE Extension
- Artifact / Slide generation
- MIGによるGPU partition
- Full off-server PostgreSQL replica / object storage backup
- 複数IDE固有Context連携
- Production-grade external multi-node deployment

---

# 推奨最初のMilestone

`M0 — Benchmark & Evaluator`

対象:
- PAW-001〜004
- PAW-010〜019

最初の実装ゴールは **Main Coding Model / Memory Workerを自分の実Repoで評価して選定できること**。
