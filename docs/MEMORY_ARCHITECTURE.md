# Memory Architecture

更新日: 2026-09-15

## 1. 基本方針

Personal AI WorkspaceのMemoryは **PostgreSQL + Markdown Projection** のハイブリッド方式とする。

PostgreSQLをシステム上の正本とし、同じMemoryを人間がすぐ確認できるよう、
専用領域へMarkdownとして常時投影する。

これは各Project RepoへMarkdownを自動生成する設計ではない。

## 2. 保存イメージ

```text
/srv/personal-ai/
├── database/
│   └── PostgreSQL
│
└── memory/
    ├── users/
    │   └── <user-id>/
    ├── projects/
    │   └── <project-id>/
    ├── repos/
    │   └── <repo-id>/
    ├── shared/
    └── decisions/
```

上記 `memory/` はPersonal AI Workspace専用領域であり、
ユーザーのProject Repositoryとは別物。

## 3. PostgreSQL側

Memory 1件の概念例:

```text
memory_id
user_id
project_id
repo_id
scope
type
title
content
importance
status
source
created_at
updated_at
version
commit_sha
metadata(JSONB)
embedding(vector)
```

本文 `content` はMarkdown記法を含むTEXTとして保存可能。

## 4. Markdown側

PostgreSQLのMemoryを、人間向けにまとめたMarkdownとして常時生成する。

用途:
- すぐ読む
- Memory UIで閲覧
- Backup
- Export
- 必要に応じた履歴比較

V1では編集はMemory UIから行う。

```text
Memory UIで編集
      ↓
PostgreSQL更新
      ↓
version更新
      ↓
Markdown Projection再生成
```

これにより、DBとMarkdownを別々に手動修正する二重管理を避ける。

## 5. Memory専用Git

Markdown Projectionの履歴が必要な場合、
Personal AI Workspace専用のGit Repositoryを利用してよい。

```text
/srv/personal-ai/memory/
        ↓
Internal Memory Git
```

このGitは以下とは無関係:

```text
/home/<user>/workspace/ExampleProject/.git
/home/<user>/workspace/ExampleRepoB/.git
```

Memory更新を理由にProject Repoへcommitしてはならない。

## 6. Project Repoへのファイル追加

[FIXED]

Projectを新規作成・登録しただけでは、Personal AI Workspaceは以下を勝手に追加しない。

- `AGENTS.md`
- `MEMORY.md`
- `.personal-ai/`
- `docs/decisions/*.md`
- System Prompt由来のMarkdown
- その他Memory管理用ファイル

Repoへ文書を追加するのは、ユーザーが明示的に要求した場合のみ。

## 7. 将来の直接Markdown編集

必要になれば、Memory専用Markdownをエディタから直接変更する機能を追加可能。

その場合は:
- front matterにmemory_id/version
- optimistic concurrency control
- conflict表示
- import validation
- Audit Log

を必須とする。

V1では複雑化を避け、UI編集 → DB → Markdown再生成を基本とする。



## 8. Repo Memoryと権限

[FIXED]

Repo Memoryは主にRepository由来のコード理解を保持するための単位とする。

権限は原則としてProjectから継承する。

```text
Project Member
  └─ Project配下のRepo Memoryを原則閲覧可能
```

ただし、一部Repoだけ制限したいProjectに対応するため、Repo単位でACL overrideを設定可能にする。

デフォルト:
- `inherit`

override可能な基本権限:
- `read`
- `write`
- `agent`

例:
```text
Project: ExampleProject

User A
  project = member
  backend = inherit
  ios = inherit

User B
  project = member
  backend = inherit
  ios.read = false
  ios.write = false
  ios.agent = false
```

LLMはRepo Memoryの分類や推奨visibilityを出してよいが、ACLの最終決定はBackendが行う。
LLMの判断だけでアクセス権を広げてはならない。



## 9. Inferred Preference

[FIXED]

明示的なMemoryと、行動から推測されたPreferenceを区別する。

### States

- `observed`: 単発の観測
- `inferred`: 反復から推測された候補
- `confirmed`: ユーザー確認済み
- `rejected`: 保存しないと判断済み
- `deprecated`: 後の指示で無効化

Inferred Preferenceは、低リスク領域では弱い参考情報として利用可能。
ただし、高リスクな操作権限・Merge・Delete・公開範囲・ACL等には
Confirmed状態になるまで適用しない。

### Confirmation UX

反復が十分に蓄積した場合、チャットを大きく遮らずに確認UIを表示する。

```text
この指定を何度か使っています。
今後も既定にしますか？

[このRepoだけ]
[このProject]
[すべてのProject]
[保存しない]
[その他...]
```

`その他...` は自由入力とし、LLMが自然文から以下を抽出する。

- 適用scope
- Preference内容
- 例外条件
- 強さ / defaultか必須Policyか
- Risk level
- 有効期限があればexpiry

高リスクと判断した場合は、LLMの解釈をプレビューして
ユーザー確認後にConfirmed Memory / Policyへ反映する。



## 10. Conflict / Versioning / Retrieval

[FIXED]

Memoryは更新時に古い内容を物理削除せず、状態を変更して履歴を保持する。

```text
active       = 現在有効
superseded   = 後続Memoryに置換済み
deprecated   = 明示的に無効化
history      = 監査・履歴目的
```

新しいMemoryが古いMemoryを置き換える場合は、
`supersedes` 関係を保存する。

通常のLLM Contextへは `active` のみを候補として投入し、
過去履歴は通常の推論コンテキストへ入れない。

Retrieval Pipeline:

```text
ACL / user filter
      ↓
Scope filter
      ↓
status = active
      ↓
relevance
      ↓
importance / recency
      ↓
Context Budget
      ↓
Top-N Memory
      ↓
LLM
```

そのため、PostgreSQL内に大量の履歴が存在しても、
履歴件数自体がLLMのトークン消費や生成速度へ直接影響しない設計とする。

優先順位の基本:

```text
Current explicit instruction
> Confirmed Memory
> specific Project / Repo Memory
> User Memory
> Inferred Preference
```

同種の設定では、より具体的なScopeを優先する。
Global / Owner Security Policy等の強制Policyは下位Scopeでoverride不可。

新旧Memoryの関係が明確なら自動更新可能。
曖昧なconflictではユーザー確認を行う。

### Memory Context Budget

[OPEN]

具体的なtoken上限は現時点では決めない。

Context Windowだけでなく、KV CacheのVRAM消費、dtype / quantization、
同時実行数、Agent並列数、推論バックエンド、利用可能VRAM等に強く依存するため、
Local model / Runtime / GPU Scheduler設計時に別途決定する。

Retrieval PipelineはTop-N等で制御可能な構造だけ先に用意する。




## 11. Freshness / Revalidate

[FIXED]

Memoryには一律TTLを設定せず、情報の性質に応じて鮮度Policyを持たせる。

### Policies

- `permanent`
- `revalidate`
- `repo_commit`
- `expiring`
- `session_only`

### Revalidate

Revalidateは「現在は正しいが、将来変化しやすい事実」に限定する。

代表例:
- Primary Local LLM
- Project Member
- External Service
- Development Phase
- Runtime / operational configuration

User Preferenceや恒常的なPolicyは原則Permanentとする。

Revalidate Memory metadata:

```yaml
verified_at: 2026-09-16
revalidate_after: 90d
revalidate_trigger:
  - related_setting_changed
  - member_changed
on_stale: lower_priority
```

期限到達でMemoryを即時無効化しない。
`stale_candidate` とし、そのMemoryを利用する必要が生じたタイミングで再確認する。

イベントトリガーによるRevalidateも可能にする。

### Repo Memory

Repo Memoryの鮮度は時間よりGit状態を優先する。

```text
memory.commit_sha
memory.branch
       ↓
current repository state
       ↓
diff / change significance
       ↓
fresh or stale
```

必要であれば再解析してRepo Memoryを更新する。



## 12. Retrieval Pipeline

[FIXED]

```text
ACL / Permission
      ↓
Metadata
      ↓
Keyword + Vector
      ↓
Rerank
      ↓
Deduplicate / Conflict handling
      ↓
Top-N
      ↓
LLM
```

Vector similarityだけでMemory採用を確定しない。
Scope / status / freshness / confirmation state等は構造化metadataとして評価する。

Embedding / Rerankerのモデル選定はBenchmarkで行う。

## 13. Retrieval Benchmark

[FIXED]

本番Memoryの蓄積を待たず、Seed Benchmarkから開始する。

### Stage 1: Seed Benchmark
- 確定済み要件・MemoryからQueryと正解Memoryを作る
- Scope違い、矛盾、古いMemory、ACL等のHard Caseを人工作成

### Stage 2: Replay Benchmark
- 過去会話・過去Taskを再生し、必要Memoryを正解ラベル化

### Stage 3: Shadow Evaluation
- 実運用回答には影響を与えず、複数Retriever / Rerankerの候補を裏で比較

### Stage 4: Production Evaluation
- 実Query、実Memory、ユーザー修正を評価セットへ段階的に追加

主要指標:
- Recall@K
- MRR / nDCG
- Permission Leakage = 0
- stale / superseded誤採用率
- Scope取り違え率
- Retrieval latency
- Memory token量

Ground Truthは人手確認・明示仕様・Confirmed Memoryを優先する。
LLM Judgeは補助として利用可能だが、唯一の評価基準にはしない。

Embedding / Reranker選定では精度だけでなく、
CPU/GPU負荷、VRAM、Latency、並列実行への影響も比較する。



## 14. Raw Conversation / Long-term Memory

[FIXED]

Raw ConversationはLong-term Memoryとは別ストレージ/論理層として扱う。

### Raw Conversation

保持対象:
- User / Assistant message
- Tool result
- Agent result
- Task execution history

デフォルト保存期間:
- 無期限

通常のLLM Context:
- Raw Conversation全文は投入しない
- Session ContextとLong-term Memory Retrievalを利用する

Raw Conversationを参照するのは、
過去会話検索、決定経緯確認、Memory provenance確認等の必要時に限定する。

### Provenance

Memoryは複数sourceを持てるようにする。

例:

```text
Memory X
├─ Conversation #105
├─ Conversation #144
└─ Project Decision #12
```

1つのConversationが削除されても、他sourceで確認済みなら
Memory自体を自動削除しない。

### Deletion

Conversation削除UIでは以下を選べる。

- 会話だけ削除
- 会話と関連Memoryを確認して削除

関連Memoryを削除する場合も、複数source・Confirmed状態・他Projectへの影響等を
確認してから処理する。

### Admin Privacy

Admin / OwnerはUsage Metadataを確認できるが、
通常の管理UIから他ユーザーのPrivate Raw Conversation本文を自由閲覧できない。



## 15. Memory History Graph UI

[FIXED]

Memory管理画面にはGitLens Graphに近い履歴ビューを用意する。

GraphはProject Gitを読むのではなく、Memory DB上のversion relationから構築する。

主なrelation:
- supersedes
- extends
- conflicts_with
- confirmed_from
- revalidated_from
- merged_from

各Memory versionはノードとして表示する。

クリック時に表示:
- 本文
- Diff
- Scope
- Status
- Freshness
- Source / Provenance
- Change reason
- Actor
- Timestamp
- Related Conversation / Task / Repo

Inferred PreferenceがConfirmed Memoryへ昇格した履歴や、
競合Candidateが人間確認によって1つに統合された履歴もGraph上で追跡可能にする。

過去versionへ戻す場合、古いversionを直接`active`へ戻すのではなく、
その内容をベースに新しいversionを作成して`active`にする。
これにより履歴を壊さずUndo / Restoreを実現する。

GraphからProject Repoへファイルを自動追加することはない。



## 16. Memory Management UI

[FIXED]

### Desktop

V1は3ペイン構成とする。

- 左: Scope Tree
- 中央: Memory List
- 右: Memory Detail

右ペインの主要タブ:
- 本文
- 履歴
- ソース
- 詳細

履歴はGitLens風Graph UIを利用し、
各Memory versionの分岐・統合・superseded・inferred→confirmed等を表示する。

ソースではConversation / Task / Repo解析 / User Confirmation等の
provenanceを確認できる。

### Mobile

モバイルは以下の階層式とする。

```text
Memory一覧
  → Memory詳細
      → 本文 / 履歴 / ソース / 詳細
```

### Pin

PinはUI上の優先表示とRetrieval優先度の補助情報として扱う。
Pin = 常時LLM Context投入ではない。
Permission / Scope / relevanceを通過した場合のみ優先度を上げる。



## 17. Manual Editing / Concurrency

[FIXED]

人間がMemory UIで編集して保存した内容は原則Confirmedとする。

編集は既存Versionの物理上書きではなく、新しいVersionを作成する。

高リスクPolicyやPermissionはMemory本文と分離し、
本文変更だけで実行権限を自動変更しない。

公開範囲を広げるScope変更やMerge / Delete / ACL等の高リスク変更は
解釈結果を提示し、明示確認後に適用する。

複数人で同じMemoryを編集する場合はOptimistic Lockを使う。
編集中VersionとCurrent Versionが異なる場合はConflictとして扱い、
Diff / Merge / Reloadを提示する。

LLMは統合案を作成可能だが、最終保存には人間確認を要求する。

V1では以下を標準編集経路とする。

```text
Memory UI
→ PostgreSQL
→ Markdown Projection
→ History Graph
```



## 18. Immediate Journal / Background Consolidation

[FIXED]

Memory処理は、即時保存が必要なJournal層と、
非同期でLong-term Memoryを整理するConsolidation層に分離する。

### Immediate Journal

User messageを受けたら、Raw ConversationとPending Observationを
PostgreSQLへ即時保存する。

Long-term Memory整理が未完了でも、次Turnでは
Session + Pending Observation + active Memoryを利用できる。

### Background Consolidation

非同期Workerで以下を処理する。

- Memory candidate extraction
- Scope classification
- Preference evidence
- Duplicate / Conflict detection
- Version relation
- Embedding
- Markdown Projection

### Ordering

非同期Workerの完了時刻ではなく、
conversation_id / turn_id / event_sequence / base_memory_version等を使って
論理順序を保証する。

### Priority

- HIGH: explicit preference / decision
- NORMAL: standard consolidation
- LOW: re-embedding / repo refresh / history compression

### GPU Independence

GPU停止中もRaw Conversation / Pending Observationの保存は継続する。
GPU依存処理はQueueへ保持し、GPU復帰後に処理する。

### Status UI

Memory管理UIで同期済み / 整理中 / GPU待ち / 失敗等を表示可能にする。



## 19. Markdown Backup / Authority

[FIXED]

MemoryのGitバックアップでは、PostgreSQL dump / WALは扱わない。
Gitへ保存するのはMemory Markdown Projectionのみとする。

```text
PostgreSQL
  → Markdown Projection
      → Scheduled Batch
          → Memory専用 Private Repository
```

このRepositoryはProject Repoとは完全に独立する。

### Authority

Ownerのみ:
- Backup destination変更
- Credential登録 / 更新 / 削除
- Backup機能の有効 / 無効
- Security-sensitive backup configuration変更

Admin:
- Backup status確認
- 最終成功 / 失敗確認
- 手動実行
- Retry
- Audit確認

AdminはBackup先やCredentialを変更できない。

### Service Identity

Backup JobはUser個人のGitHub session / `gh auth login`に依存させず、
専用Service Identity / Repo限定Credentialで実行する。

Credential本文はUIで再表示せず、置換のみ可能とする。



## External fallback / Git backup

[FIXED]

Memory Markdown Projectionは専用Private Gitへ30分単位でbatch pushする。

GitはMemoryのhuman-readable external fallbackとして扱う。
PostgreSQL DB dump / WAL / binary DB dataはGitへ保存しない。

サーバー全損時はMarkdown ProjectionからLong-term Memory内容を再構築できるが、
Raw Conversation / Task state等のDB固有データは完全復旧できない可能性をV1では許容する。



## Dedicated Recovery Repository

[FIXED]

Memory Markdown Projectionは専用Private Gitへ30分単位でbatch pushする。
このRepositoryはMemoryだけでなく、主要Workspace状態を再構築するためのRecovery Repositoryとして扱う。

通常時のSource of TruthはPostgreSQL。
Recovery GitはDisaster Recovery専用で、通常運用時にDBへ逆同期しない。

Memory本文だけでなく、復元に必要なscope / status / version / relation / provenance等の
machine-readable metadataもRecovery Projectionへ含める。

Raw Conversation全文、DB dump、WAL、credential等はGitへ保存しない。

## Shared Memory permissions

[FIXED]

Shared MemoryはWorkspace-wide common knowledgeとして扱う。

- Active User: Read
- Owner / Admin: Create / Edit / Delete / Restore
- General User / Agent: direct write / auto-promotion不可
- 他ScopeからSharedへの昇格は `Shared Memory Candidate` としOwner/Admin承認必須

Shared MemoryはBackend-enforced System / Security Policyを上書きできない。
