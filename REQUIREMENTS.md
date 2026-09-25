# Personal AI Workspace 要件定義（統合版）

- 更新日: 2026-09-20
- ステータス: Requirements Freeze Candidate
- 統合元: 要件策定時の設計資料・議論
- 対象: Ubuntu / RTX PRO 6000 Blackwell 96GB / Ryzen 9 9950X / RAM 128GB
- 状態表記:
  - `[FIXED]` 確定
  - `[PROVISIONAL]` 有力案
  - `[OPEN]` 未決
  - `[FUTURE]` 将来拡張

## 1. 目的

Personal AI Workspaceは、Claude CodeやCodexの単なるGUIラッパーではなく、日常会話・コーディング・Repository操作・Local LLM・Codex・Claude Code・Memory・Research・Git/GitHub・GPU管理・Artifact生成を一つのGUIに統合する自前AIワークスペースとする。

基本思想は **Local-first / Cloud-escalation / Human-final-authority**。

## 2. 既存構想から継承する原則

### [FIXED]
- GUI First
- Local First
- Evidence over Confidence
- Memory as Data
- GPU Compute First
- Model Agnostic
- Raw Conversationは保持
- Local AIの自己申告confidenceを成功判定に使わない
- Test PASSだけで正解としない
- Kaggle / 研究計算をGPU利用の最優先とする
- Local AIは停止してVRAMを解放できる
- 外部モデル名をCore設計へ直書きしない

旧メモではGPT-OSS-120BをLocal Intelligence Engineの主候補としていたが、コーディング用途は専用open-weightモデルを再選定する。

## 3. 用語

### [FIXED] サブスク
本プロジェクト内では以下をまとめて「サブスク」と呼ぶ。
- Codex
- Claude Code

高利用枠（ユーザー表現: ×5相当）で利用する方針。

実際の認証・複数ユーザー利用は各サービスの利用規約・認証仕様に準拠して確定する。

## 4. 全体構造

```text
Mac / Windows / Smartphone
          │ HTTPS / WebSocket
          ▼
┌─────────────────────────────┐
│ Ubuntu Personal AI Backend  │
│ Auth / RBAC                 │
│ Session Manager             │
│ Memory Manager              │
│ Agent Orchestrator          │
│ Evaluator                   │
│ Git / GitHub Controller     │
│ Audit / Admin               │
│ GPU Manager                 │
└──────────────┬──────────────┘
               │
     ┌─────────┼──────────┐
     ▼         ▼          ▼
   Local      Codex     Claude Code
   Agent     (サブスク)   (サブスク)
     │
RTX PRO 6000 96GB
```

### [FIXED] 通信
- App ↔ Ubuntu: HTTPS / WebSocket
- IDE ↔ Ubuntu Repo: SSH
- Ubuntu内部Agent ↔ Repo: ローカルファイルアクセス
- 外部アクセス: Tailscale等のPrivate Networkを基本
- Backend portをPublic Internetへ直接公開しない

SSHはアプリ通信の必須要件ではなく、IDE・管理・Remote Host用。


### [FIXED] Project / Repo Permission Inheritance

Repo Memoryの権限は、**原則としてProject権限を継承**する。

- Project Memberは、そのProject配下のRepo Memoryを原則すべて閲覧可能。
- Repo Memoryはアクセス権を細分化するためだけではなく、どのRepository由来の知識かを区別するための情報単位として扱う。
- 通常運用ではRepoごとの個別設定を不要とし、`inherit` をデフォルトとする。

例外として、Repo単位のACL overrideを設定可能にする。

```text
Project Permission
      ↓
   inherit
      ↓
Repo Permission
      │
      └─ overrideがある場合のみ上書き
```

Repo単位では少なくとも以下の権限を分離可能にする。

- `read`: Repo / Repo Memoryの閲覧
- `write`: Repoへの変更・commit等
- `agent`: AgentにそのRepoを操作させる権限

必要に応じて将来 `review` / `merge` 等を追加できるよう拡張可能にする。

権限制御の原則:

- Repo overrideがなければProject権限を継承する。
- Repo overrideがある場合のみRepo固有権限を優先する。
- LLMはMemoryの分類・機密度・推奨visibilityを提案してよいが、権限そのものを確定・突破してはならない。
- 最終アクセス判定はBackendのACLで強制する。
- 曖昧な場合、LLMはより狭い公開範囲を推奨する。



### [FIXED] Inferred Preference / Confirmation Flow

明示的に保存指示されていないが、会話・操作の反復から推測できる好みは、
いきなりConfirmed Memoryへ昇格させず、以下の段階で扱う。

```text
Conversation / Action
        ↓
Observation
        ↓
Inferred Preference Candidate
        ↓
evidence蓄積 / scope推測
        ↓
User Confirmation
        ↓
Confirmed Memory
```

LLMは少なくとも以下を判断材料にする。

- `frequency`: 同様の指示が繰り返された回数
- `scope_diversity`: 何Repo / 何Projectで観測されたか
- `language_strength`: 「今回」「今後」「基本的に」等の表現
- `consistency`: 反対の指示との一貫性
- `risk_level`: 権限・削除・Merge等を伴うか

Scope候補の基本ルール:

```text
同一Repo内のみで反復
→ Repo Preference候補

同一Project内の複数Repoで反復
→ Project Preference候補

複数Projectで一貫して反復
→ User Preference候補
```

確認UIでは、LLMが推奨する候補をボタンで提示する。

例:

```text
この操作を何度か指定しています。
今後の既定動作にしますか？

[このRepoだけ]
[このProject]
[すべてのProject]
[保存しない]
[その他...]
```

`その他...` では自然文入力を許可する。

例:

```text
「開発系のProjectだけ適用して。ただしmainへのMergeは毎回確認して」
```

LLMは自由入力を以下のような構造へ変換する。

```yaml
scope: project_group
rule: subscription_review_before_merge
apply_to: development_projects
merge_policy: confirm_before_main_merge
status: confirmed
```

低リスクなPreferenceでは、LLMはInferred Preferenceを確認前でも
「弱い参考情報」として利用してよい。

ただし以下の高リスク領域は、推測だけで永続Policyや実行権限へ昇格させてはならない。

- Merge権限
- Delete / destructive operation
- 公開範囲の拡大
- ACL / Role / Permission変更
- Credential / Secret関連
- 外部への送信・公開

自由入力をLLMが高リスク設定として解釈した場合は、
構造化した解釈結果をユーザーへ提示し、明示確認後に適用する。



### [FIXED] Memory Conflict / Versioning / Retrieval

Memory更新では、古い内容を物理的に上書き・削除せず、履歴として保持する。

基本状態:

- `active`: 現在有効
- `superseded`: 新しいMemoryに置き換えられた
- `deprecated`: 明示的に無効化された
- `history`: 履歴・監査目的で保持

新しいMemoryが既存Memoryを置き換える場合:

```text
Old Memory
status = superseded

New Memory
status = active
supersedes = <old_memory_id>
```

通常のLLM Contextへは、`active` なMemoryのみを候補とする。
`superseded` / `deprecated` / `history` は通常プロンプトへ投入せず、
Memory画面・監査・復元・矛盾調査等でのみ参照する。

Retrievalは以下を先に適用してからLLMへ渡す。

1. user / permission
2. scope（User / Project / Repo / Shared）
3. `status = active`
4. task relevance
5. importance / recency
6. context budget内で上位N件

Memory DBに大量の履歴が存在しても、それ自体をLLM Contextへ全投入しない。

基本優先順位:

```text
現在の明示的な指示
    >
Confirmed Memory
    >
より具体的なProject / Repo Memory
    >
User Memory
    >
Inferred Preference
```

同種の設定では、原則としてより具体的なScopeを優先する。

```text
Repo
  >
Project
  >
User
  >
Shared
```

ただし、Owner / Global Policyとして強制設定されたSecurity Policy等は、
下位Scopeから上書きできない。

LLMは新旧Memoryの関係を以下のように分類してよい。

- `same`
- `extends`
- `supersedes`
- `conflicts`
- `unrelated`

変更が明確な場合は自動で新Memoryを`active`、
旧Memoryを`superseded`にできる。
曖昧な矛盾ではユーザーへ確認する。

### [BENCHMARK] Memory Context Budget

Memory Contextへ投入する具体的なtoken上限は、現時点では固定しない。

Local modelごとの以下の条件に依存するため、モデル / Runtime設計時に別項目として決定する。

- Context Window
- KV CacheのVRAM消費
- dtype / quantization
- 同時実行数
- Agent並列数
- inference backend（vLLM / SGLang等）
- 利用可能VRAM
- Task側で必要な会話・コード・Tool Context量

Memory Retrieval自体は、関連度・importance・recency・Top-N等で絞れる設計にしておく。
具体的なtoken budget値は、Local model benchmarkとGPU scheduling設計を踏まえて決定する。



### [FIXED] Memory Freshness / Revalidate Policy

Memoryの鮮度は、すべてに一律TTLを設定するのではなく、情報の性質ごとに扱う。

基本分類:

- `permanent`: User Preferenceや恒常ルールなど、明示変更されるまで有効
- `revalidate`: 現在は正しいが、将来変わる可能性が高い事実
- `repo_commit`: Repo由来情報。commit / branch / diffを基準に鮮度判定
- `expiring`: 「今月だけ」等、明示的な期限がある一時設定
- `session_only`: Session / Task終了後にLong-term Memoryへ残さない情報

`revalidate` は対象を限定する。

例:
- 現在のメインLocal LLM
- Projectメンバー構成
- 外部サービス利用状況
- Projectの開発フェーズ
- 変更可能な運用構成・技術構成

以下は原則 `permanent` とする。
- User Preference
- 恒常的な操作方針
- 明示的に確定した長期ルール

Revalidate Memoryは少なくとも以下を保持する。

```yaml
freshness_policy: revalidate
verified_at: 2026-09-16
revalidate_after: 90d
revalidate_trigger:
  - related_setting_changed
on_stale: lower_priority
```

重要:
- `revalidate_after` 到達時に即無効化しない。
- 期限到達後は `stale_candidate` として扱う。
- 実際にそのMemoryが必要になった時点で再確認する。
- 日数だけでなく、関連設定変更・メンバー変更・モデル変更等のイベントでも再確認対象にできる。

Repo Memoryは時間TTLを主基準にしない。

```text
stored_commit / branch
        ↓
current HEAD / diff
        ↓
fresh / stale
        ↓
必要ならRepo再解析
```

Repo Memoryの鮮度判定では `commit_sha`、`branch`、変更差分等を利用する。



### [FIXED] Memory Retrieval Pipeline

Memory Retrievalは以下の順序を基本とする。

```text
Permission / ACL Filter
        ↓
Metadata Filter
        ↓
Keyword Search + Vector Search
        ↓
Rerank
        ↓
重複・矛盾整理
        ↓
Top-N Memory
        ↓
LLM
```

- Vector Searchだけで採用を決めない。
- Keyword SearchとVector Searchを併用する。
- 最終採用前にScope、status、freshness、confirmed/inferred、importance等を評価する。
- Embeddingモデル / Rerankerモデルは特定モデルへ固定せず、別途Benchmarkで決定する。

### [FIXED] Retrieval Benchmark Strategy

実Memoryが十分に蓄積するまでBenchmarkを待たない。

初期評価セットは以下を組み合わせて作成する。

- 現在の要件・確定Memoryから作る明示的なQuery / Relevant Memoryペア
- 過去会話から抽出した実際に起こり得る検索Query
- 人工的に作るHard Case
  - 類似しているが別Project
  - Repo / Project / User Scope違い
  - active / supersededの競合
  - stale / freshの違い
  - Confirmed / Inferredの競合
  - ACL境界
  - Keyword一致するが意味的には無関係
  - 意味は近いがKeywordが異なる

実運用Memoryが増えたら、実Query・検索結果・ユーザー修正等を
安全に評価セットへ追加し、人工データの比率を徐々に下げる。

主要評価指標:

- `Recall@K`: 必要なMemoryを候補に含められたか
- `MRR / nDCG`: 必要なMemoryが上位に並んだか
- Permission Leakage: **0件必須**
- superseded / deprecated Memoryの誤採用率
- stale Memoryの誤採用率
- Scope取り違え率
- Retrieval latency
- LLMへ投入するMemory token量

LLM Judgeは補助評価として利用可能だが、
LLM自身が作った問題を同じLLMだけで採点する構成には依存しない。
Ground Truthは、明示的な仕様・人手確認・既存の確定Memoryを優先する。

Benchmarkは段階化する。

```text
Stage 1: Seed Benchmark
  現在の要件 + 人工Hard Case

Stage 2: Replay Benchmark
  過去会話 / 過去Taskを再生

Stage 3: Shadow Evaluation
  実運用中に候補Retrieverを裏で比較
  （ユーザー向け回答には影響させない）

Stage 4: Production Evaluation
  実Memory・実Query・ユーザー修正を継続評価
```

Embedding / Rerankerの変更は、精度だけでなく
Latency・GPU/CPU使用量・VRAM・同時実行への影響も含めて判断する。



### [FIXED] Raw Conversation / Long-term Memory Separation

Raw ConversationとLong-term Memoryは分離する。

```text
Conversation
  ├─ Raw Conversation
  ├─ Session State / Summary
  └─ Long-term Memory
```

Raw Conversation:
- ユーザー発言
- Assistant発言
- Tool実行
- Agent結果
- Taskの詳細経緯

Long-term Memory:
- 将来も利用する価値がある確定情報
- Preference
- Project Decision
- Repo理解
- Shared Knowledge等

Raw Conversationはデフォルト無期限保存とする。
ただし、通常のLLM Contextへ会話全文を投入しない。

通常利用:

```text
Current Query
    ↓
Session Context
    +
Long-term Memory Retrieval
    ↓
LLM
```

過去会話の検索・決定経緯の確認等、必要な場合のみRaw Conversationを検索する。

Memoryはsourceを1件に限定せず、複数のConversation / Task / Repo解析等を
provenanceとして保持できるようにする。

Conversation削除時は以下を選択可能にする。

1. `会話だけ削除`
   - Raw Conversationを削除
   - Long-term Memoryは維持
   - Memory側ではsource deletedを記録可能

2. `会話 + 関連Memoryを確認`
   - そのConversationをsourceとするMemory候補を表示
   - ユーザーが削除対象を確認
   - 別sourceでも確認済みのMemoryは自動削除しない

Admin / Ownerは通常の管理操作だけで、
他ユーザーのPrivate Raw Conversation本文を自由に閲覧できない。
管理画面ではUsage Category、Project、Model、Token、GPU時間、結果等の
運用metadataを中心に扱う。



### [FIXED] Memory History Graph UI

Memoryの過去変遷は、GitLensのGraphに近いUIで可視化する。

これはProject / RepositoryのGit履歴を表示するものではなく、
PostgreSQLに保持するMemory version / relationから描画する。

表示対象となる関係例:

- `supersedes`
- `extends`
- `conflicts_with`
- `confirmed_from`
- `revalidated_from`
- `merged_from`

Graph上の各ノードはMemory変更イベントを表す。

ノードを選択すると、少なくとも以下を確認できるようにする。

- 当時のMemory本文
- 現在versionとの差分
- scope
- status
- freshness
- source / provenance
- 変更理由
- 変更者（User / LLM / Agent / System）
- 作成日時 / 更新日時
- 関連するConversation / Task / Repo

想定UI:

```text
Memory History

● 2026-09-16  Confirmed
│  「レビューはCodex + Claude」
│
├─○ 2026-09-14  Inferred Preference
│    「レビュー後はMergeまで任せる傾向」
│
● 2026-09-10  Superseded
   「レビューはCodexのみ」
```

競合するMemory Candidateが存在する場合はBranchとして表示できるようにする。

```text
● Current
│
├─● Candidate A
│
└─● Candidate B
```

ユーザーがCandidateを確認・統合した場合は、Graph上でも関係を追跡可能にする。

Memory History Graphから以下の操作を可能にする。

- versionの詳細表示
- version間Diff
- source確認
- 過去versionへの復元
- superseded / stale / inferred等の表示切替
- scope別Filter
- User / Project / Repo / Shared別Filter

過去versionへの復元は物理的な巻き戻しではなく、
選択versionを元に新しい`active` Memory versionを作成する。
履歴自体は保持する。

Graph UIはMemory管理画面の主要ビューの1つとする。
Project RepoへMemory管理用ファイルを自動生成・commitしてはならない。



### [FIXED] Memory Management UI Layout

Memory管理UIのV1基本構成を以下とする。

#### Desktop

3ペイン構成:

```text
┌──────────────┬──────────────────┬──────────────────────────────┐
│ Scope        │ Memory一覧       │ Memory詳細                   │
│              │                  │                              │
│ User         │ Pin / Status     │ 本文 / 履歴 / ソース / 詳細 │
│ Projects     │ Scope / Type     │                              │
│ Repos        │ Freshness        │                              │
│ Shared       │                  │                              │
└──────────────┴──────────────────┴──────────────────────────────┘
```

左ペイン:
- User
- Project
- Repo
- Shared
- Project配下のRepo階層

中央ペイン:
- Memory一覧
- Pin
- Confirmed / Inferred / Stale / Superseded等の状態
- Scope
- Freshness
- 検索 / Filter

右ペイン:
- `本文`
- `履歴`
- `ソース`
- `詳細`

#### History

`履歴` はGitLens風Graph UIとし、
Memory version / relation / branch / merge / superseded / inferred→confirmed等を追跡する。

#### Source

`ソース` ではMemoryのprovenanceを確認できる。

例:
- Conversation
- Task
- Repo解析
- User Confirmation
- Agent結果

#### Detail

`詳細` では以下を表示する。

- Scope
- Status
- Freshness
- Importance
- Created / Updated
- Actor
- Project / Repo
- Version
- relation metadata

#### Mobile

モバイルは3ペインではなく階層式とする。

```text
Memory一覧
   ↓
Memory詳細
   ↓
[本文] [履歴] [ソース] [詳細]
```

#### Pin

PinはMemory一覧上部での優先表示、およびRetrieval時の優先度向上に利用できる。
ただし、PinされたMemoryを常に無条件でLLM Contextへ投入してはならない。
Scope / Permission / relevance等の条件を満たす場合のみ優先する。



### [FIXED] Manual Memory Editing / Concurrency

Memory管理UIから人間が本文を編集して保存した場合、その内容は原則として
`Confirmed` Memoryとして扱う。

既存Memoryを物理的に上書きせず、新しいVersionを作成する。

```text
Old Version
status = superseded

New Version
status = active
confirmation = confirmed
actor = user
```

本文編集と、Backendの実行権限・高リスクPolicy変更は分離する。

高リスク例:
- Merge permission
- Delete / destructive operation
- ACL / Role / Permission
- Credential / Secret関連
- 外部公開
- 公開範囲の拡大

Memory本文から高リスクPolicy変更が読み取れる場合、
LLMは構造化した解釈結果を提示し、ユーザーの明示確認後にPolicyへ反映する。

Scope変更:
- 公開範囲を狭める変更: 即反映可能
- `User → Project` 等、他ユーザーから見える範囲を広げる変更: 確認必須
- `Project → Shared`: 確認必須

通常Metadata:
- Pin
- Importance
- Permanent / Revalidate
- その他低リスク属性

これらは即反映可能だが、変更履歴は残す。

#### Concurrent Editing

複数ユーザーによるProject / Shared Memoryの同時編集には
Optimistic Lockを利用する。

保存対象Versionと現在Versionが異なる場合、勝手に上書きしない。

```text
編集中Version: 12
Current Version: 13
        ↓
Conflict
        ↓
[差分を見る]
[変更を統合]
[再読み込み]
```

LLMは統合案を生成してよいが、競合解消後の内容は人間確認後に保存する。

V1の編集経路:

```text
Memory UI
   ↓
PostgreSQLへ新Version保存
   ↓
Markdown Projection再生成
   ↓
History Graph更新
```

Markdown Projectionの直接編集はV1の標準経路にしない。



### [FIXED] Immediate Journal / Background Consolidation

Memory処理は同期保存と非同期整理を分離する。

```text
User Message
   ↓
Raw Conversation保存
   ↓
Pending Observation / Immediate Journal保存
   ↓
Assistant Response
   ↓
Background Consolidation
   ├─ Memory Candidate抽出
   ├─ Scope / Preference判定
   ├─ 既存Memory検索
   ├─ Conflict / Supersede判定
   ├─ Embedding更新
   └─ Markdown Projection更新
```

#### Immediate Journal

以下は即時保存し、GPUや重いLLM処理に依存しない。

- Raw Conversation
- turn_id / conversation_id
- Pending Observation
- Project / Repo context
- timestamp / event_sequence
- processing state

Long-term Memory化が完了する前に次のターンが来た場合でも、
Current Session + Pending Observation + active Long-term Memoryを参照し、
直前の指示を失わないようにする。

#### Background Consolidation

重いMemory整理は非同期Workerで処理する。

- Candidate分類
- Inferred Preference evidence集計
- Scope判定
- Duplicate / Conflict判定
- supersedes / extends relation
- Embedding
- Markdown Projection
- 履歴更新

#### Ordering

非同期処理の完了順ではなく、発生順序とVersionを基準に適用する。

最低限以下を保持する。

- `conversation_id`
- `turn_id`
- `event_sequence`
- `created_at`
- `base_memory_version`

古いTurnの処理が後から完了して、新しいMemoryを誤って上書きしてはならない。

#### Priority Queue

Memory Workerは優先度Queueを持つ。

- `HIGH`: 明示的なPreference / Decision / 次ターンから必要な設定
- `NORMAL`: 通常のMemory整理
- `LOW`: Embedding再生成 / Repo再解析 / 履歴圧縮等

#### GPU / Kaggle Mode

GPUサービス停止中でもRaw ConversationとPending Observationは保存する。
Memory ConsolidationがGPUを必要とする場合はQueueへ保持し、
GPU復帰後に再開する。

Memory保存そのものをGPU必須にしてはならない。

#### UI

Memory同期状態をUIで確認可能にする。

例:
- `同期済み`
- `2件を整理中`
- `GPU待ち`
- `処理失敗 / 再試行`



### [FIXED] Memory Markdown Backup / Backup Authority

MemoryのGitバックアップは、DBバックアップとは分離し、後述のDedicated Recovery Repositoryへ保存する。

#### Gitへ保存するもの

Memory本文は **Memory Markdown Projection** として保存する。
Memoryのscope / status / version / relation / provenance等、再構築に必要なmachine-readable metadataも
Recovery Projectionへ含める。
Memory以外の復旧用データは、後述のDedicated Recovery Repositoryの定義に従う。

- PostgreSQL dumpはGitへ保存しない
- WALはGitへ保存しない
- Credential / SecretはGitへ保存しない
- Session token / Passkey / Password hash等はGitへ保存しない

Markdown Projectionは、Personal AI Workspace専用のPrivate Repositoryへ
定期バッチ処理でcommit / pushする。

Project / RepositoryのGitとは完全に分離する。

```text
PostgreSQL
   ↓
Markdown Projection
   ↓
Batch Job
   ↓
Dedicated Recovery Repository
```

Gitへの保存はSource of Truthではなく、
人間可読な外部バックアップ / 履歴 / 最終保険として扱う。

#### Backup Authority

Backup Repositoryに関する権限は以下とする。

Owner:
- Backup機能の有効 / 無効
- Backup先Repositoryの設定 / 変更
- Credentialの登録 / 更新 / 削除
- Backup方式・重要Security設定の変更

Admin:
- Backup状態の確認
- 最終成功日時 / 失敗状態の確認
- 手動Backup実行
- 失敗Jobの再試行
- 運用ログ / Audit Log確認

AdminはBackup先RepositoryやCredentialを変更できない。

#### Service Identity

Backup Jobは各Userの個人GitHub認証を利用しない。
専用Service IdentityまたはBackup Repositoryに限定したCredentialを使用する。

これにより、
- User削除
- UserのGitHub Logout
- 個人Token変更
- User権限変更

によってMemory Backupが停止しない構成とする。

Backup Credential本文はOwnerを含めUI上で再表示しない。
更新・置換のみ可能とする。



### [FIXED] Global Notification Policy

通知はBackupに限らず、Personal AI Workspace全体で共通Policyを使う。

基本方針:
- 通常の通知はベルアイコンのNotification Centerへ集約する。
- 作業中のユーザーを不要なPopup / Modalで遮らない。
- 未読件数・Badge・Notification Center内表示を基本とする。
- 同種通知は集約し、重複通知を抑制する。
- Owner / Admin向け通知と一般User向け通知は分離可能にする。

Severity:

- `INFO`
  - 通常のお知らせ
  - 軽微な状態変化
  - 完了通知
  - Notification Centerのみ

- `WARNING`
  - 軽微な失敗
  - Retry中
  - Backupの一時失敗
  - Resource使用率上昇
  - Bell badge + Notification Centerを基本とする

- `ERROR`
  - 複数回の失敗
  - Job停止
  - Agent処理失敗
  - Backup連続失敗
  - Notification Centerで強調し、必要に応じて非モーダルBannerを表示可能

- `CRITICAL`
  - データ損失の可能性
  - Security異常
  - 権限異常
  - 復旧が必要な重大障害
  - 長時間Backup不能など
  - 画面上部Banner / Modal等の強い表示を許可する

原則として、Modalはユーザーの即時判断が必要な場合に限定する。

通常時の例:

```text
🔔 3

Notification Center:
- Memory Backupが1回失敗しました。自動Retry中
- Repo解析が完了しました
- Memoryを2件整理しました
```

重大時の例:

```text
CRITICAL
Memory DBの書き込みに失敗しています。
データ保全のため一部の書き込み操作を停止しました。

[詳細を見る]
```

通知には可能な限りActionを持たせる。

例:
- `詳細`
- `再試行`
- `既読`
- `関連設定を開く`

Notification CenterではSeverity / System / Project / User / unread等でFilter可能にする。



### [FIXED] Memory Markdown Backup Schedule

Memory Markdown ProjectionのGit Backupは、変更がある場合のみ **30分ごと** に実行する。

```text
Memory更新
   ↓
backup_dirty = true
   ↓
30分ごとのBatch
   ↓
変更あり?
  ├─ No  → 何もしない
  └─ Yes → Markdown再生成 → commit → push
```

30分間に複数回Memoryが更新されても、原則1回のcommitへまとめる。

Owner / Adminは管理画面から手動Backupを実行可能とする。

失敗時は自動Retryを行い、段階的Backoffを利用する。
一時的な失敗はGlobal Notification Policyに従い、
Bell / Notification Centerへ静かに通知する。

連続失敗・長時間未成功の場合はSeverityを段階的に上げる。

例:
- 1回失敗: `WARNING`
- 連続失敗: `ERROR`
- 長時間Backup成功なし / データ保全上重大: `CRITICAL`

大きなBanner / ModalはCRITICAL等、即時対応が必要な場合に限定する。



### [FIXED] Multi-Repo Task / Working Set

Projectは複数Repositoryを持てるが、Agentが毎回すべてのRepoを作業対象にはしない。

Taskごとに `Working Set` を持つ。

```text
Project
  ↓
Task
  ↓
Working Set
  ├─ Repo A
  └─ Repo B
```

通常はSingle-Repo Taskとし、必要な場合のみMulti-Repo Taskへ拡張する。

RepoはTask内で少なくとも以下の役割を持てる。

- `referenced`: 調査・検索・参照のみ
- `working`: 編集・テスト可能
- `target`: PR作成まで行う対象

基本原則:

- Read範囲は原因調査のため比較的広く取ってよい。
- Write範囲はTask Working Setとして明示・制御する。
- Agentが「ついでに」別Repoを書き換えてはならない。
- LLMはProject Memory / Repo Memory / Issue / code structure等から必要Repoを推定してよい。
- Write対象Repoを増やす場合は、Read対象追加より慎重に扱う。
- 曖昧な場合はUserへ確認できるようにする。

Multi-Repo TaskではRepoごとにGit状態を分離する。

```text
Task #121
├─ backend
│   ├─ worktree / branch
│   ├─ tests
│   ├─ review
│   └─ PR
└─ ios
    ├─ worktree / branch
    ├─ tests
    ├─ review
    └─ PR
```

Task全体の完了判定は、対象Repoごとの必要条件が満たされたかで判断する。

Memory Retrieval:
- Project MemoryはTask全体で利用候補にする。
- Repo MemoryはWorking / Referenced Repoを中心に取得する。
- Project内の無関係Repo Memoryを毎回Contextへ投入しない。

Chat / Task UIでは現在のRepo Context / Working Setを表示し、
Repoの追加・除外を確認できるようにする。


## 5. クライアント

### [PROVISIONAL]
Desktop:
- Tauri
- React
- TypeScript
- macOS / Windows

Mobile:
- Web / PWA

### [FIXED] UI
Claude / ChatGPT Desktopに近いChat中心UI。
Coding時のみRepository Tree / Task / Diff / Tests / Terminal / Git / Agent / Review / PermissionをContext-aware表示。

## 6. IDE連携

### [FIXED] V1
VS Code等はRemote SSHでUbuntu上の同じRepositoryを開く。

```text
Personal AI ──┐
              ├── /home/<user>/workspace/<repo>
VS Code SSH ──┘
```

同一ファイルを参照するため、ファイル・Git・Diffの基本同期はIDE拡張なしで成立。

### [FUTURE] Thin IDE Extension
以下が必要になったら薄い拡張を作る。
- Current Project / File
- Cursor / Selection
- 未保存変更
- Ask Personal AI
- Agent Status
- AI DiffをIDEで開く

MemoryやAgent RouterはIDE側に持たせない。

## 7. ログイン・ユーザー分離

### [FIXED]
アプリログイン必須。

分離キー:
- `user_id`
- `conversation_id`
- `project_id`
- `repo_id`
- `agent_session_id`

Local LLMを共有してもConversation ContextはBackendで分離するため混ざらない。

OS側も:
```text
/home/<user-a>/
/home/<user-b>/
/home/<user>/
```
のように分ける。

ユーザー別:
- Workspace
- Git config
- GitHub CLI
- SSH key
- Agent session
- Private Memory
- Private Files


### [FIXED] Passkey Policy

- **Owner: Passkey必須**
- **Admin: Passkey必須**
- **User: Passkey任意。ただしUI上で強く推奨する**
- Owner/Adminの重要操作では、直近30分以内のPasskeyによるStep-up Authenticationを要求する。
- 一般UserはPasswordのみでも利用可能だが、初回登録・設定画面でPasskey登録を推奨する。

> **注記（[Decision 0015](docs/decisions/0015-login-session-password-policy.md) の12節。上の `[FIXED]` の規則は変更しない）**:
> 上の規則は**既定値**であり、Ownerだけが、Workspaceの設定でRoleごとのPasskey要求（required / optional）、Userへの推奨表示、Step-upの有効時間を変更できる（Adminは閲覧のみ）。
> 変更には、Owner自身の直近のPasskeyによるStep-upが必要である（PasswordによるStep-upでは変更できない。Passkey実装（PAW-023）まで、この設定変更は本番では使えない）。
> Step-upの有効時間は5〜240分の範囲で設定できる（既定30分）。変更は新しいSign-in・新しいSessionから効き、既存のSessionは失効も降格もしない。変更履歴（誰が・いつ・変更前後）はAuditに加えて専用の履歴に残る。
> Passkey未登録の間、OwnerはPasswordでLoginでき、`owner-recover` による復旧もできる（PAW-022はPasskeyを強制しない。PAW-023は「必須で未登録」をPasskeyの登録だけができる状態にし、行き止まりを作らない）。


### [FIXED] Password / Login Failure / Session Policy

- Passwordの最低長は **10文字** とする。
- 大文字・小文字・数字・記号をすべて必須にはしない。長いパスフレーズを許可する。
- PasswordはArgon2id等でハッシュ化し、平文保存しない。
- ログイン失敗は段階的バックオフを採用する。
  - 1〜4回: 通常
  - 5回目: 約30秒待機
  - 以降: 1分、5分など段階的に延長
- 永久ロックは避け、大量失敗時は一時ロックとする。
- 一時ロックはOwner/Adminが管理画面から解除可能とする。
- Owner/Adminの異常なログイン失敗はAudit Logへ記録し、可能なら既存の信頼済み端末へ警告する。
- 通常のPassword変更では、**既存の他端末Sessionを維持するか、全端末からログアウトするかをユーザーが選択可能**とする。
- 通常のPassword変更時のデフォルトは **既存Sessionを維持** とする。
- Passwordリセット、アカウント侵害対応、Owner/Adminによる強制リセット、Owner Recoveryでは、原則として既存Sessionを全失効させる。
- 本人によるPassword変更は現在のPasswordまたはPasskeyによる本人確認を要求する。
- Owner/Adminが他UserのPassword本文を閲覧することはできない。必要な場合は1回限りのPassword再設定フローを発行する。


### [FIXED] User Lifecycle

ユーザーの利用状態として **Suspended（停止）は設けない**。

基本状態:

- `Invited`: 招待済み・初回登録前
- `Active`: 利用中
- `Pending deletion`: 削除待ち
- `Deleted`: 削除済み

削除操作を行った時点で:

- 新規ログイン不可
- 既存Sessionを即失効
- 新規Agent実行不可
- 実行中Agentは安全停止
- GitHub / Codex / Claude等の外部認証利用を停止

ユーザー削除とProject/Repository削除は分離し、ユーザーが所有する共有Project等がある場合は削除前に所有権移譲を要求する。


### [FIXED] User Deletion Retention

- User削除後は `Pending deletion` として **30日間保留**する。
- `Pending deletion` 中はログイン不可、既存Session失効、新規Agent実行不可とする。
- 30日以内の復元は **Ownerのみ** 実行可能とする。
- 30日経過後は以下の個人データを完全削除する。
  - Password hash
  - Passkey
  - Private Chat
  - Private Memory
  - 個人設定
  - GitHub認証情報
  - 個人用Files
- Audit Logは削除せず、必要に応じて `Deleted User` 等へ匿名化して保持する。
- Project / RepositoryはUser削除とは分離し、必要な所有権移譲を行って残す。

完全削除は稼働DB / 個人用Filesだけでなく、削除対象個人データを含む
Current Recovery Projection、Recovery Git履歴、管理下のclone/cache、DB backup/WAL等の復旧用コピーにも適用する。
User削除の期限到達時は、通常のMemory削除とは区別し、これらの履歴・コピーの消去まで行う。

削除操作のHuman Approvalでは、対象User、対象Recovery Repository、履歴消去と復元不能になる範囲を明示する。
期限到達時は承認済み範囲の消去を実行し、history rewrite / force push等を未承認で実行しない。
通常のMemory削除でGit履歴を残せるのは、このUser完全削除要件に抵触しない範囲に限る。

消去と検証が完了するまで `Deleted` / 完全削除済みと表示しない。
消去失敗や承認不足時はアクセスを失効した `Pending deletion` を維持し、
30日期限の未達、失敗理由、再試行状態をOwnerへ通知する。保留期間を延長してよいという意味ではない。
Git history rewriteだけでprovider側の保持コピーまで消えたとは判断せず、未確認の消去を完了表示しない。

復旧時にも削除済み個人データを再生成しない。
Private Memory本文等を含まない最小限の削除記録を保持し、Recovery Import / DB restoreでは
復元元とは独立した最新の削除状態を確認・適用してから通常運用を再開する。
削除状態を確認できない場合は復旧を完了扱いしない。具体的な消去・検証・削除記録の保管方式は実装時に選ぶ。

## 8. Git / GitHub

### [FIXED] GitHub認証方式
V1ではGitHub Appを採用せず、各ユーザーが自分のLinuxユーザー環境で `gh auth login` を実行してGitHubへ個別ログインする方式を基本とする。

- `~/.config/gh/` をLinuxユーザーごとに分離
- Git identityをユーザーごとに分離
- SSH keyをユーザーごとに分離
- Personal AI WorkspaceのGUIからGitHub接続フローを開始できるようにする
- 認証自体はGitHub CLIのWeb認証フローを利用する
- Admin / Ownerは接続状態やGitHub username等のメタデータは確認できるが、tokenやSSH秘密鍵は閲覧できない
- GitHub AppはV1では使用しない
- 将来、Webhook・Repo単位の細粒度権限・App identityでの自動化が必要になった場合のみ再検討する

### [FIXED] Merge Policy
最終Merge権限は原則人間。

Agentは通常:
- 実装
- commit / push（許可範囲）
- PR作成
- Review
- Merge Ready判定

まで。

人間が現在のTask/Session内で「マージしてください」「問題なければマージまで」等を明示した場合のみAgent Merge可。

## 9. Agent運用

### [FIXED] Local
基本実装担当:
- 通常実装
- Bug fix
- Test
- Refactor
- Repo探索
- Log解析
- Docs
- サブスクReview後の修正

### [FIXED] サブスク
- 日常的なPR Review
- Merge可否の助言
- Localで解決不能
- 最新情報が必要
- 高難度実装
- Security / Architecture review
- Cross review

### [PROVISIONAL] Review役割
Codex: correctness / tests / bugs / types / exceptions / refactor
Claude: architecture / requirements / security / maintainability / boundary cases

固定せず実測で変更可能。

## 10. 並列Agent

### [FIXED]
同じWorking Treeを複数Agentで同時編集しない。Git worktreeで分離。

利用:
1. 独立Issue並列
2. 同じIssueを独立2案
3. 実装 + Review
4. Backend / Frontend / Tests分担

Review Agentは原則Read-only。

## 11. Local LLM共有

### [FIXED]
ユーザーごとにモデルを複数起動しない。原則1インスタンスを共有。

```text
User A ─┐
User B ─┼→ AI Gateway → vLLM/SGLang → Local Model ×1
User C ─┘
```

会話状態はBackend管理。

### [PROVISIONAL]
User別:
- Priority
- Max concurrent
- Max context
- Max tasks
- Rate limit
- GPU priority

## 12. Local coding model候補

詳細は `docs/MODEL_CANDIDATES.md`。

第一評価群:
1. KAT-Coder-V2.5-Dev
2. Qwen3-Coder-Next
3. Qwen3.8-27B-FP8
4. Devstral Small 2 24B
5. Devstral 2 123B Q4_K_M（Deep）

第二評価群:
- NVIDIA Nemotron 3.5 Lightning 30B-A3B NVFP4
- Qwen3-Coder 30B-A3B系
- 今後の新モデル

## 13. Evaluator

### [FIXED]
Agentの「完了」を信用しない。Agent LayerとEvaluatorを分離。

評価:
- Build
- Syntax
- Lint
- Type Check
- Unit
- Integration
- Regression
- Issue-specific Acceptance Test
- Hidden Test
- Security / forbidden operation

Bug fixは可能な限り:
`Reproduce → Root Cause → Fix → Same Reproduction → Regression`

モデル比較指標:
- Resolved@1
- Resolved@3
- FAIL_TO_PASS
- PASS_TO_PASS
- Human correction time
- Completion time
- Agent steps
- Peak VRAM
- tokens/sec
- Tool failure
- Diff size

公開Benchmarkだけで採用せず、自分の過去IssueをBenchmark化する。

## 14. Memory

### [FIXED]
MemoryはWorkspaceが持ち、Local/Codex/Claudeへ必要分だけ注入する。

Scope:
1. Session
2. User
3. Project
4. Repo
5. Shared

Scratchpadは永続Memoryと分離。

Vector検索は必ず権限・scopeで候補集合を絞った後に実行する。

### [FIXED] Storage / Markdown Projection

Memoryは **PostgreSQL + Markdownのハイブリッド方式** とする。

- PostgreSQL: 構造化Memory / metadata / permission / versionの正本
- pgvector: semantic index
- Markdown: 人間が普段すぐ確認できる常時生成ビュー
- Dedicated Recovery Repository: Memory Markdown Projectionの履歴管理とWorkspace復旧に利用
- **Project / RepositoryのGitとは完全に分離する**

重要な制約:

- Project作成時に、そのProject Repoへ `AGENTS.md`、`memory.md`、Decision文書等を自動追加しない。
- System PromptやMemory機能を理由に、Project Repoへ勝手にMarkdownをcommitしない。
- Project RepoへMemory/Decision文書を追加するのは、ユーザーが明示的に指定した場合のみ。
- Memory専用MarkdownはPersonal AI Workspace側の専用領域に保存する。
- V1ではMarkdown編集はMemory UIから行い、BackendがPostgreSQLへ保存した後にMarkdownを再生成する。
- 将来、Markdownファイルの直接編集を許可する場合はversion/conflict検知を必須にする。

概念:

```text
PostgreSQL  ← 正本
  ├─ structured memory
  ├─ permissions / scope
  ├─ metadata / version
  └─ pgvector
       │
       ▼
Memory Markdown Projection
  ├─ users/
  ├─ projects/
  ├─ repos/
  ├─ shared/
  └─ decisions/
       │
       └─ Dedicated Recovery Repository
```

Memory Markdownは常時生成・更新し、ユーザーがすぐ読める状態を維持する。

### [FIXED]
CodeのSource of TruthはGit Repository。Repo Memoryには可能なら`commit_sha`を付ける。

## 15. RBAC / Admin

### [FIXED] Role
- User
- Admin
- Owner（Adminの全権限を含むシステム最終所有者。原則1名）
- System（人間ログイン不可）

Owner専用操作とAdminの日常運用権限は、後述の「Owner / Admin の役割分離」に従う。

### [FIXED] Admin-only
- Global System Prompt
- Agent Policy
- Routing
- Security Policy
- Model設定
- GPU Resource Policy
- Shared Memory Policy
- User Role / Permission
- Tool Permission
- System Config
- 仕様変更

Backend側でRole check必須。

### [FIXED] Admin Dashboard
User別:
- Local/Codex/Claude Task数
- runtime
- GPU time/utilization（取得可能範囲）
- token/context（取得可能範囲）
- Tool calls
- Repo / Project
- Running Task
- changed files
- branch / PR
- Tests
- Review
- errors
- escalation


### [FIXED] Owner / Admin の役割分離

Ownerはシステムの最終所有者で、原則1名。Adminは日常運用の管理者。

Ownerのみ:
- Adminの追加/削除
- Owner権限の移譲
- 最後のAdmin削除防止に関わる操作
- 全体復旧/非常時設定

Admin:
- User管理
- 利用量確認
- User別Quota設定
- System Prompt / Model / Routing / Permission設定
- Audit Log閲覧
- Running Agent / Project / Repoの管理

OwnerはAdminの全権限を含む。普段の運用はAdmin機能で完結させる。

### [FIXED] User別Quota

Owner/Adminはユーザーごとに利用上限を設定できる。

対象候補:
- Local task数
- Codex task数
- Claude task数
- Local同時実行数
- サブスク同時実行数
- Agent runtime
- GPU time
- token/context usage（取得可能範囲）
- 日/週/月単位の上限

Quota超過時は新規Taskを拒否または待機キューへ送り、既存Taskを強制終了しないのを基本とする。Owner/Adminは一時的な上限緩和を可能にする。

### [FIXED] Admin Usage Analytics

管理画面では最低限以下をグラフ表示する。

- User別利用量の時系列
- Local / Codex / Claudeの利用比率
- 用途カテゴリ別利用比率
- Project別利用量
- GPU time / Agent runtime
- Task成功/失敗/エスカレーション件数
- Quota使用率

Raw Chat本文は表示せず、用途カテゴリ・非機密の短い目的要約・Project/Repo等の管理メタデータを利用する。


### [FIXED] User Quota / Unlimited

Owner/Adminはユーザー単位・サービス単位で、Local/Codex/Claudeの利用回数・時間・同時実行数・token/context・GPU時間など取得可能な指標にQuotaを設定できる。各指標は数値上限だけでなく **Unlimited（無制限）** を選択可能とする。Quota到達時は実行中Taskを原則完了させ、新規Taskのみ停止する。Owner/Adminは一時Override可能。


### [FIXED] UI言語

- V1の標準UI言語は日本語とする。
- Chat、認証、設定、Admin Dashboard、Agent状態、PR/Review、Error等の主要UIは日本語基準で設計する。
- 将来の英語/i18n対応が可能な構造にはしておくが、初期実装では日本語を優先する。

### [FIXED] Workspace Login / Session

- OwnerはUser/Admin権限を内包し、別のUser/Adminアカウントを持つ必要はない。
- 通常Sessionは **30日間無操作で失効**。
- `ログイン状態を維持` を選んだ場合は **最大90日**。
- Owner/Adminの重要操作は、直近 **30分以内** のPasskey等によるStep-up認証を要求する。
- Sessionは端末単位で管理し、各端末の最終利用日時を確認・個別Logoutできるようにする。
- 他のすべてのSessionを一括Logoutできるようにする。

### [FIXED] User Invitation / Multi-device Login

- Self registrationは行わない。Owner/Adminがユーザーを作成して招待する。
- 招待はアカウント初回作成時のみ使用する。
- 招待リンク/コードは1回限り・期限付き・Revoke可能とする。
- 初回端末でアカウント作成後、2台目以降は **既にログイン済みの信頼済み端末から「新規端末を追加」する方式を基本導線** とする。
- 「新規端末を追加」画面から **QRコード** または **共有リンク** を発行できるようにする。
- QR/共有リンクには **1回限り** のペアリングトークンを用いる。
- ペアリングトークンの有効期限は初期値 **10分** とし、期限切れ後は再利用できない。
- ペアリング成功後はトークンを即失効させ、同じQR/リンクを再利用できないようにする。
- 発行元の信頼済み端末から、未使用のペアリングトークンを手動で失効・再発行できるようにする。
- 原則として、同一ユーザーについて同時に有効な新規端末追加トークンは1つまでとし、新規発行時は旧トークンを失効させる。
- 新端末でQRまたはリンクを開いた後、本人確認、端末名登録、必要に応じたPasskey登録を行って端末を紐付ける。
- **一般Userは、有効なQR/共有リンクによるペアリングで新規端末を追加可能**とする。
- **Owner/Adminは、QR/共有リンクに加えて、既存の信頼済み端末からの明示承認を必須**とする。
- 通常のUsername/Password/Passkeyログインはフォールバックとして残すが、新規端末追加の主要UXはQR/リンク方式とする。
- ペアリングトークンの発行・使用・失効・新規端末登録はAudit Logへ記録する。
- 全Passkey/信頼済み端末を失ったOwnerはUbuntu sudo経由のRecoveryで復旧可能とする。

## 16. Audit Log

### [FIXED]
記録対象:
- Login/Logout
- Agent start/stop
- Task/Tool execution
- File change metadata
- Commit/Push/PR
- Merge（明示許可時）
- GitHub CLI operation
- System Prompt / Model / Routing / Permission change
- Shared Memory
- Admin config

最低:
`timestamp, actor, role, action, target, project, repo, agent, request_id, result`

通常UIからAudit Logを書き換え・削除しない。

## 17. Prompt / Config Versioning

### [FIXED]
Global System Prompt等はVersion管理:
- Admin
- Timestamp
- Before/After
- Diff
- Reason
- Version
- Rollback

## 18. GPU運用

### [FIXED]
Local AIは余剰GPUサービス。Kaggle/研究優先。

AI Mode:
- Local model稼働

Compute Mode:
1. 新規Local受付停止
2. Task checkpoint
3. model停止
4. vLLM/SGLang停止
5. AI CUDA process停止
6. NVML/nvidia-smi確認
7. AI由来CUDA process=0
8. Compute Ready

Core Backend/Auth/Memory/Git/MonitoringはGPU非依存で継続。

## 19. Security

### [FIXED]
- Private Network
- HTTPS/WebSocket
- SSH公開鍵
- Backend port直接公開なし
- Credential平文DB禁止
- User別GitHub/SSH credential
- filesystem scope制限
- Admin API RBAC

### [FIXED / IMPLEMENTATION CHOICE]
- Credential vault implementation: `[IMPLEMENTATION_CHOICE]`
  - plaintext DB禁止
  - Agent / General Userへsecret plaintextを見せない
  - Tool Broker / Secret Isolation要件を満たす方式から実装時に選定
- 2FA / Passkey policy: `[FIXED]`
- Session timeout / lifetime: `[FIXED]`
- Owner / Admin step-up再認証: `[FIXED]`
- Secret rotation capability: `[FIXED REQUIREMENT]`
  - 具体的vault製品・key storage方式は実装時に選定

## 20. Artifact

### [FUTURE]
旧構想を継承:
`Content → Deck Plan → Design → Render → Vision Review → Fix → PPTX/PDF/HTML`

Coding MVP後。

## 21. 開発フェーズ

1. Requirements（現在）
2. Benchmark / Evaluator Harness・Model選定
3. Core Backend
4. Local Agent PoC
5. GitHub / IDE
6. サブスクIntegration
7. Memory
8. Multi-agent
9. GPU Manager
10. Artifacts

## 22. 正本文書構成

```text
README.md
REQUIREMENTS.md
AGENTS.md
docs/
├── ARCHITECTURE.md
├── MEMORY_ARCHITECTURE.md
├── SECURITY_RBAC_AUDIT.md
├── MODEL_CANDIDATES.md
├── EVALUATION.md       # 次回
├── API_CONTRACT.md     # Architecture確定後
├── DATA_MODEL.md       # DB設計時
└── decisions/
    └── NNNN-*.md
```

## 23. 決定・延期済み項目

旧 `OPEN ITEMS` は、後続の確定要件と
`docs/REQUIREMENTS_FREEZE_REVIEW.md` に基づき、以下へ整理済み。

### [FIXED]

- MemoryのOperational Source of TruthはPostgreSQL。Recovery GitはDisaster Recovery Source。
- Password / Passkey / Session PolicyとGitHub認証方式。
- Codex / ClaudeはWorkspace全体のSystem-level Connectionとし、GitHubはUserごとに分離。
- Agent routingの既定値はLocal-first。Loop検知後は別アプローチを試し、解決しなければCodex / Claude等へEscalate。
- Owner / Adminは通常の管理操作だけで他UserのPrivate Raw Conversation本文を自由に閲覧できない。
- Audit / Security eventは長期保持。
- Shared MemoryのRead / Write / Delete / Restore / Promotion権限。

### [BENCHMARK]

- Local coding modelの実機選定。
- vLLM / SGLang / llama.cpp等のRuntime候補と比較値。

### [IMPLEMENTATION_CHOICE]

- Desktop / Backend frameworkの最終選択。（Backend は [Decision 0003](docs/decisions/0003-backend-cli-web-implementation-stack.md) で決定済み。Desktop は未決）
- Credential Vaultの具体製品・storage方式。
- Provider仕様と利用規約を満たすCodex / Claudeの具体的な認証実装。
- Benchmark結果とRuntime要件を満たす推論Backendの統合方式。
- Auto RouterやLocal retry / escalationに使う具体的な閾値。

### [FUTURE]

- Dedicated IDE Extension。
- Artifact / Slide generation（Coding MVP後）。

## 24. 既存資料の出自

要件策定時の設計資料・議論を統合。

## 25. 更新ルール

今後の壁打ちではこの文書を更新する。
1. 新案は`[PROVISIONAL]`
2. 採用明言で`[FIXED]`
3. 方針変更はDecision化
4. 時間依存情報は確認日を付ける
5. 要件変更とコード変更を分離して追跡



### [FIXED] UI Design Document

画面設計・情報設計・Desktop/Mobile差分・Memory Graph・Notification等の詳細は
`docs/UI_DESIGN.md` に分離して管理する。

`REQUIREMENTS.md` は機能要件を正とし、`UI_DESIGN.md` は実装時のUI/UX設計資料とする。



### [FIXED] Model Selection Timing

要件定義完了後、最初の実装フェーズとしてModel Benchmarkを実施し、
Memory WorkerとMain Coding Agentの採用モデルを決定する。

Architectureは特定モデルに固定しない。

Main Coding Agentの最新候補・比較項目は `docs/MODEL_CANDIDATES.md` で管理する。



### [FIXED] First implementation phase after requirements definition

要件定義完了後、最初に実装するのはPersonal AI Workspace本体ではなく、
`Benchmark / Evaluator Harness` とする。

目的:
- Main Coding Agent候補を同一条件で比較する
- Memory Worker候補を同一条件で比較する
- 公開Benchmarkだけに依存せず、自分のRepo / Issue / Test / Agent workflowで採用モデルを決める
- 将来新モデルが出た場合も同じHarnessへ投入して再評価できるようにする

Coding Agent評価の主判定:
- Build
- Unit Test
- Integration Test
- Hidden Acceptance Test
- Regression Test
- Lint / Type Check
- Security Check
- Forbidden Changes Check
- FAIL_TO_PASS
- PASS_TO_PASS

補助判定:
- Claude Review
- Codex Review

最終確認:
- Human

Claude / Codexは最終裁定者ではなく、Executable Evaluatorを主判定とする。

初期実装順:
1. Benchmark specification / task formatを確定
2. Minimal Evaluator Harnessを実装
3. Small seed benchmarkを作成
4. Candidate modelsを同一条件で比較
5. Main Coding Agent / Memory Workerを採用
6. 採用モデルを前提にWorkspace本体の実装を開始

注記: [Decision 0002](docs/decisions/0002-start-workspace-implementation-before-model-comparison.md)（Approved）により、
上記6のWorkspace本体の実装は、4〜5のModel比較・採用の完了を待たずに開始できる。
Model比較Runの実行と、その結果によるModelの採用は、Humanの判断を待つ。この節の順序は、Decision 0002より優先しない。

Humanがコード全文を理解して採点することを前提にしない。
可能な限り外部挙動・テスト・回帰・禁止変更・修正時間等で客観評価する。



### [FIXED] Project / Repository registration and per-user checkout

ProjectはWorkspace DB上の論理的な共有単位とする。

実際にUser / Agentが編集するGit checkout / worktreeはLinux Userごとに分離する。
複数UserがProject共有の単一working treeを直接編集しない。

例:

```text
/home/<user-a>/workspaces/<project>/<repo>
/home/<user-b>/workspaces/<project>/<repo>
```

V1のRepository追加経路:

1. GitHubから追加
   - 対象Linux Userの `gh auth login` 済みアカウントを利用
   - アクセス可能Repoから選択
   - Ubuntu上のUser workspaceへclone
   - Projectへ登録

2. Ubuntu上の既存Repositoryを追加
   - 指定pathがGit Repositoryであることを検証
   - remote / default branch / HEAD等を取得
   - Projectへ登録

3. 新規Repositoryを作成
   - Repository名等を指定
   - Local only または GitHubにも作成
   - 初期化後Projectへ登録

ProjectへのRepo登録は、Project Repositoryへ以下のような管理ファイルを自動追加しない。

- AGENTS.md
- MEMORY.md
- `.personal-ai/`
- その他Workspace内部管理用ファイル

Memory / Agent Policy / Project metadataはPersonal AI Workspace側で保持する。



### [FIXED] Project roles and membership

System全体の `Owner / Admin / User` とは分離して、
Project内ではV1で以下の3権限を採用する。

- `Manager`
  - Project Memberの追加・削除
  - Repository追加
  - Project設定
  - Agent Policy変更
  - Project Memory管理
- `Contributor`
  - Chat
  - Task実行
  - Repository編集
  - Agent利用
  - PR作成
  - Project Memory利用
- `Viewer`
  - Project / Repository / Task / Memoryの閲覧のみ

Project作成者は初期 `Manager` とする。

Projectは招待制とする。
System Userであるだけでは、所属していないProjectを閲覧・利用できない。

Repository権限はProject権限を継承するのを基本とし、
Repo単位のACL overrideを許可する。

例:

```text
Project role: Contributor

Repo A: inherit
Repo B: read-only
Repo C: access denied
```

Repo ACL overrideはProject roleより狭める用途を基本とする。
権限判定の最終決定はBackend ACLが行い、LLMはこれを迂回できない。



### [FIXED] Project lifecycle

Project lifecycle:

```text
Active
  ↓
Archived
  ↓
Pending deletion
  ↓ 30 days
Deleted
```

#### Active
通常状態。
Chat / Agent Task / Repo編集 / Memory更新 / PR作成等を利用可能。

#### Archived
読み取り専用状態。
過去のChat / Task / Memory / PR / Repo情報は閲覧可能だが、
新規Agent Task、Repo変更、Project Memory更新は停止する。

Managerまたは権限を持つSystem管理者はArchivedからActiveへ戻せる。

通常のProject一覧ではArchivedを分離し、
「Archivedを表示」等から参照できるようにする。

#### Pending deletion
Delete操作後、即時完全削除せず30日間保留する。

開始時点で:
- Project accessを停止
- 新規Agent Taskを停止
- 外部認証利用を停止
- 実行中Taskをsafe-stop

30日以内は復元可能。
復元時はまずArchivedへ戻す。

#### Deleted
30日後、Workspace内部の以下を削除する。

- Project Memory
- Repo Memory
- Project Chat
- Task / Agent実行データ
- Project設定
- Project Member / ACL
- Repo紐付け

Audit Logは必要最低限を `Deleted Project` として保持する。

#### External / filesystem resources

Project削除に連動して以下を自動削除しない。

- GitHub Repository
- GitHub Issue / Pull Request
- Remote branch
- Ubuntu local checkout

Local checkoutの削除は別操作として実施する。

#### User Memory

Project削除のみではUser Memoryを原則削除しない。
削除Projectが唯一のprovenanceであり、関連Memoryも消したい場合は
削除候補として提示し、別途確認する。

#### Permissions

- Manager: Archive / Unarchive / Delete開始
- System Owner / Admin: 管理上必要な操作
- Contributor / Viewer: 不可

Delete開始には確認操作を要求する。
Project名入力等の誤操作防止を入れる。

通常運用はDeleteよりArchiveを中心とする。



### [FIXED] New Project defaults

新規Project作成時は、初期入力を最小限にする。

基本入力:
- Project名
- 説明（任意）
- Repository追加（後でも可）
- Member追加（後でも可）

初期デフォルト:
- Project creator = `Manager`
- Repo ACL = Project権限を `inherit`
- Agent routing = `Local-first`
- Merge policy = `Human approval required`
- Project Memory = enabled
- Repo Memory = enabled
- Inferred Preference = confirmation required
- Web Research = on-demand only
- Archived = false

詳細設定はProject作成後に変更可能とする。
Project作成Wizardで不要な設定を大量に要求しない。



### [FIXED] Agent Task lifecycle

Task lifecycle:

```text
Queued
  ↓
Running
  ├─ Waiting for User
  ├─ Waiting for Approval
  ├─ Waiting for Resource
  └─ Paused
  ↓
Evaluating / Reviewing
  ↓
Completed

Exception:
- Failed
- Cancelled
```

状態の意味:

- `Queued`: 実行待ち
- `Running`: 実行中
- `Waiting for User`: 仕様・判断・追加情報待ち
- `Waiting for Approval`: Merge / Delete / ACL / 権限変更等の承認待ち
- `Waiting for Resource`: GPU / Cloud quota / Repo lock等のResource待ち
- `Paused`: Userの手動停止、Kaggle Mode等
- `Evaluating / Reviewing`: Test / Evaluator / Codex / Claude等による評価・Review中
- `Completed`: 必要な完了条件を満たした
- `Failed`: 実行不能または評価失敗
- `Cancelled`: UserまたはPolicyにより中止

Task StateはUbuntu Backend側で永続化する。
Clientが切断されてもTaskは必要に応じて継続し、再接続時に以下を復元できるようにする。

- Current step
- Agent / Model
- Logs
- Tool execution state
- Repo Working Set
- Branch / Worktree
- Test / Evaluator result
- Review status
- PR status

`Waiting for User` 等で判断待ちになった場合、
Task全体を必ず完全停止するのではなく、
その判断に依存する処理のみ停止する。

依存しない安全な作業:
- Read-only research
- Repo exploration
- Test execution
- Log analysis
- 他の独立したsub-task

は継続可能とする。

ただし、その判断によって無駄になる可能性が高いWrite操作や
高リスク操作は回答・承認まで待機する。



### [FIXED] Task pause / cancel / retry / restart

Task controlは以下を区別する。

#### Pause
- 現在の安全な区切りで停止する
- branch / worktree /途中成果を保持する
- Resumeで続行可能

#### Resume
- Paused Taskを保存済み状態から再開する
- branch / worktree / task contextを再利用する

#### Cancel
- Task自体を終了する
- 作成済みbranch / worktree /途中成果は原則保持する
- Cancelと成果物削除を同一操作にしない
- 不要になった成果物の削除は別操作にする

#### Retry
- 失敗地点または再試行可能なstepから再実行する
- 同じAgent / Modelまたは別Agent / Modelを選択可能にする
- Retry履歴をTaskに保持する

#### Restart
- 元のstarting commit / task inputを基準に最初からやり直す
- 新しいbranch / worktreeを作成する
- 旧試行は履歴として保持する

#### Stop Now
Agent loop、暴走、危険なTool実行等に備えた緊急停止。

- 通常Pauseより強い
- 生成・Tool実行・sub-agent実行を即時中断する
- 可能な範囲でprocess cleanup / lock releaseを行う
- Task成果物を自動削除しない
- 停止理由と実行中だったstepをAudit / Task logへ残す

Taskを止める操作と、成果物・branch・worktreeを削除する操作を分離する。



### [FIXED] Task Queue / Priority / Preemption

Task Queueは以下の3段階を基本とする。

- `HIGH`
- `NORMAL`
- `LOW`

初期方針:
- 通常User Task = `NORMAL`
- Background Memory consolidation / re-embedding = `LOW`
- Background Research refresh = `LOW`
- Owner / Adminは必要に応じてTask priorityを引き上げ可能

Priorityは原則として新規Taskの開始順にのみ影響する。
HIGH Taskが到着したからといって、実行中Taskを自動的に中断しない。

実行中Taskのpreemptionは以下の明示操作・modeに限定する。

- Explicit `Preempt`
- `Stop Now`
- `Kaggle / Full GPU Mode`
- Critical safety / resource protection

#### Kaggle / Full GPU Mode

Kaggle等でGPUを専有する場合:

- 新規Local GPU Taskの開始を停止
- Queue中Taskは `Waiting for Resource` へ
- 実行中Local GPU Taskは安全停止 / drain / unloadを試みる
- Local LLM inference serviceを停止可能
- VRAMを解放する
- Raw Conversation / Pending Observation等のCPU + PostgreSQL処理は継続可能
- GPU依存のMemory処理等はQueueしてGPU復帰後に再開

PriorityとPreemptionは分離して扱う。
高Priorityであること自体は、実行中Taskを中断する権限を意味しない。



### [FIXED] Task execution budget / loop prevention

Taskごとに実行予算を持てるようにする。

主な制限項目:
- max runtime
- max agent steps
- max retry
- max tool calls
- max token
- max GPU time

通常Userには細かい数値入力を要求せず、Presetを基本とする。

初期Preset例:
- `Standard`
- `Long`
- `Unlimited`

User / Admin単位のQuotaとは別に、Task単位の実行予算として管理する。

#### Loop detection

Local Agentが同じ失敗、同じTool Call、同種の修正を繰り返す場合は
Reactive Loop Detectorで検知する。

基本動作:

```text
Repeated failure
  ↓
Loop detected
  ↓
同じアプローチを停止
  ↓
Alternative approach
  ↓
Still failing
  ↓
Codex / Claude等へEscalate
```

Loop検知時は即Task Failedにせず、
別アプローチやAgent escalationを試せるようにする。

Budget超過時はTaskを強制的に壊すのではなく、
可能な範囲で安全な区切りで停止し、
`Waiting for User` / `Waiting for Resource` / `Failed` 等へ遷移できるようにする。



### [FIXED] Agent Orchestration / Parallel-first execution

Personal AI Workspaceは、並列化を積極的に利用する `parallel-first` 方針を採用する。
ただし、並列化そのものを目的にはせず、依存関係・競合・Resource効率をBackend Orchestratorが管理する。

#### Orchestration model

```text
User Task
   ↓
Backend Orchestrator
   ↓
Planning / Decomposition
   ↓
Task DAG
   ├─ Subtask A
   ├─ Subtask B
   ├─ Subtask C (depends on A)
   └─ Review / Integration
```

Taskは依存関係付きのDAG（Directed Acyclic Graph）として管理する。

依存しないSubtaskは同時実行し、
依存関係があるSubtaskは必要なNode完了後に開始する。

#### Logical roles

V1では以下のRoleを基本とする。

- `Planner`
  - Task分解
  - Working Set候補
  - Dependency提案
  - 実行順序提案
  - 原則Read-only
- `Worker`
  - 実装
  - 修正
  - Test
  - Write可能
- `Researcher`
  - Repo / Docs / Web調査
  - 原則Read-only
- `Reviewer`
  - Patch / Test / Spec適合確認
  - Read-only

RoleとModelを固定しない。
同じLocal Modelが複数Roleを担当してもよく、
Codex / Claude等もAdapter経由で任意Roleに割当可能とする。

Plannerは並列化・分解を提案できるが、
実際のAgent起動、Permission付与、worktree割当、並列数決定はBackend Orchestratorが行う。

#### Parallel execution

並列数は固定値にしない。
Resource Schedulerが以下を見て動的に決定する。

- GPU VRAM
- GPU compute utilization
- Local inference concurrency
- Cloud Agent quota / concurrency
- User quota
- Task execution budget
- Repo / file contention
- Tool / API rate limit
- System load
- Kaggle / Full GPU Mode

高い並列度を許容するが、
親TaskのBudget / Permission / ACLをSub-Agentが超えることはできない。

#### Repository isolation

Write可能なSub-Agentは原則それぞれ専用のworktree / branchを使用する。

例:

```text
Task #200
├─ Worker A
│  └─ worktree/task-200-a
├─ Worker B
│  └─ worktree/task-200-b
└─ Integration
   └─ worktree/task-200-integration
```

同一Repo内で複数Workerが並列実装した場合は、
default branchへ直接統合せず、
Task専用のintegration branch / worktreeへ変更を集約する。

Integrationで:
- commit取り込み
- conflict検出 / 解消
- build / test
- Evaluator
- Review

を実行する。

Project / default branchへの最終Mergeは従来どおりHuman-controlledとする。

Multi-Repo TaskではRepoごとに独立したintegration stateを持つ。

Read-only Researcher / Reviewerは、
Writeが発生しないことをBackendが保証できる場合、
同一のimmutable snapshot / checkoutを共有可能とする。

#### Dependency / failure isolation

Subtask失敗時:
- 独立している他Subtaskは継続可能
- 失敗Nodeに依存するNodeのみ待機
- Node単位でRetry可能
- Node単位で別Agent / ModelへEscalate可能
- 全Taskを即Failedにしない

Integrationに必要な必須Nodeが解決できない場合のみ、
親TaskをWaiting / Failed等へ遷移する。

#### Result passing

Sub-Agent同士が無制限に直接会話するのではなく、
Backend Orchestratorを介して構造化された結果を受け渡す。

Subtask output例:
- summary
- changed files
- commit
- test result
- discovered facts
- dependency notes
- unresolved questions
- confidence
- artifacts

必要なContextだけを次Nodeへ渡し、
全Agentの会話履歴を無条件で共有しない。

#### Review independence

実装Worker自身のself-reviewは補助として許可するが、
Merge Ready判定に利用するReviewerは可能な限り実装Agentと分離する。

基本Flow:

```text
Worker(s)
  ↓
Integration
  ↓
Executable Evaluator
  ↓
Codex / Claude / independent Reviewer
  ↓
Merge Ready
  ↓
Human Merge
```



### [FIXED] GPU / Compute Resource Scheduler

Agent数とGPU上のModel instance数は分離する。

複数Local Agentは原則として、共有されたMain LLM Runtimeへrequestを投げる。
Agent数に応じてMain Modelを複数copyしてVRAMへloadしない。

#### Resource classes

- `Interactive`: 通常Chat等
- `Coding`: Local Coding Agent
- `Support`: Memory Worker / Embedding / Reranker
- `Background`: Memory整理 / Research refresh等
- `Exclusive`: Kaggle / Model Benchmark等のGPU専有Job

Interactive / Main Coding workloadをBackground workloadより優先する。

#### Dynamic admission / concurrency

Compute Resource Schedulerは以下を見てTask開始可否・同時実行数を動的に決定する。

- GPU VRAM
- Reserved VRAM
- Safety Headroom
- GPU utilization
- KV Cache usage
- Average / maximum context length
- Local inference concurrency
- Task Priority
- Task Budget
- User quota
- Cloud Agent concurrency / quota
- Repo / file contention
- Tool / API rate limit
- System load
- Kaggle / Full GPU Mode

並列Agent数は固定値にしない。
Contextが長いTaskが多い場合はAgent数を減らし、
短いTask中心なら並列数を増やせるようにする。

#### VRAM accounting

VRAM判断はModel Weightのみでは行わない。

考慮対象:
- Model weights
- KV Cache
- CUDA Graph / runtime buffers
- Temporary workspace
- Memory Worker
- Embedding / Reranker
- Safety reserve

Safety Headroomの具体的なGB / %は要件定義段階では固定せず、
Model Benchmark / Runtime Benchmark後に決定する。

#### Model residency policy

初期方針:
- Main Coding Model: 原則常駐
- Memory Worker: VRAM余裕があれば常駐
- Embedding / Reranker: 軽量なら常駐、必要ならCPU fallback

VRAM pressure時の縮退優先順:
1. Background GPU Job停止
2. Memory Worker unload
3. Embedding / RerankerをCPUへfallback
4. 新規Local request admissionを抑制
5. KV Cache / context policyを調整
6. 必要ならMain Model構成を変更

Main Coding workloadを優先的に保護する。

#### Hybrid local / cloud scheduling

Local GPUが混雑している場合、
Task dependency / permission / quotaを満たす範囲で
Codex / Claude等のCloud Agentへ一部Subtaskを割当可能とする。

Resource SchedulerはGPUだけでなく、
Local CPU / Local GPU / Codex / Claude / external API等を含む
Compute Resource Schedulerとして扱う。

#### Kaggle / Full GPU Mode

Full GPU Mode開始時:

1. 新規Local GPU Task受付停止
2. Queue中Local Taskを `Waiting for Resource` へ
3. Running Local Taskをsafe pause / drain
4. Memory Worker unload
5. Embedding / RerankerをunloadまたはCPUへ
6. Main LLM unload
7. VRAM解放確認
8. Exclusive Job開始

終了後:
1. Main LLM reload
2. 必要なSupport model reload
3. Queued / Paused Task resume

Task state / branch / worktree / Pending Observation等は保持する。

#### MIG

RTX PRO 6000のMIGはV1では原則使用しない。
96GBを固定partitionせずSoftware Schedulerで柔軟に共有する。

将来、GPUをUser / Service間で強く隔離する要件が出た場合に再検討する。



### [FIXED] Web Research / Knowledge Layer

V1に以下を含める。

```text
Main Agent
  ↓
Research Orchestrator
  ↓
Research Worker Adapters
  ↓
Research Scratch
  ↓
Source / Evidence
  ↓
必要時のみ Memory Candidate
```

#### Research Scratch

Research ScratchはLong-term Memoryから分離する。

保持項目例:
- query
- source URL
- title
- fetched_at
- published_at
- source type
- extracted claims
- summary
- Project / Task relation
- expires_at

通常TTLは `created_at + 24 hours` とする。

以下はTTL削除を延期可能:
- 実行中Taskが参照中
- Pin済み
- Memory昇格確認中
- Userが明示保存

#### Memory promotion

Web / Research情報を直接Long-term Memoryへ自動保存しない。

```text
Research Scratch
  ↓
利用
  ↓
長期保存価値あり
  ↓
Memory Candidate
  ↓
確認 / Policy
  ↓
Long-term Memory
```

Project上の意思決定として採用された内容はProject Memory候補になり得るが、
単なる最新情報・記事要約等はResearch Scratchに留める。

#### Evidence / provenance

Research結果はClaimとSourceの対応を保持する。

例:
- claim
- source id(s)
- source type
- fetched_at
- published_at
- confidence
- task / project relation

Source typeは公式Docs、公式GitHub / Release、Primary source、Secondary source、Forum / Community等を識別する。
Source typeだけで真偽を自動決定しない。

#### Background research

Background ResearchはLOW priorityを基本とする。

Trigger例:
- Task開始
- dependency / library update
- stale Research Finding参照
- security advisory
- Userが最新情報を要求
- Projectで利用中Technologyの重要更新

常時無制限Crawlerにはしない。

#### Small research model

以下は小型Research Worker Modelで処理可能にする。

- Search query生成
- Search result粗選別
- Relevance判定
- Claim抽出
- 重複除去
- 短い要約

難しい矛盾、重要なArchitecture判断、高リスク判断はMain LLMへEscalateする。

Research Worker Modelは要件定義完了後にBenchmarkして決定する。

#### Provider abstraction

OpenCodeはResearch Provider Adapterの一候補とする。

```text
ResearchOrchestrator
├─ OpenCodeAdapter
├─ DirectWebSearchAdapter
├─ GitHubAdapter
├─ DocsAdapter
└─ FutureProvider
```

Research Architecture全体をOpenCodeへ固定しない。

#### Privacy

External Researchへ以下を原則送信しない。

- Private source code全文
- Secrets
- Token / Credentials
- Private Memory
- 個人Chat全文
- 不要な内部Project情報

Backendが必要に応じて検索Queryを抽象化・最小化して外部へ送信する。



### [FIXED] Tool Broker / Capability Policy / Secret Isolation

AgentのTool利用は、Backendの `Tool Broker` を経由させる。

Tool権限は単純なAllow / Denyだけでなく、少なくとも以下のCapabilityとして扱う。

- `read`
- `write`
- `execute`
- `network`
- `credential-use`
- `destructive`

AgentへAPI key / token / secretそのものを原則開示しない。

```text
Agent
  ↓
Tool request
  ↓
Backend Tool Broker
  ↓
Capability / ACL / Task Budget / Approval check
  ↓
Backend側で必要なcredentialを付与
  ↓
External service / local tool
```

Agentからは外部接続状態を利用できても、credentialのplaintextを読み出せない設計とする。

例:
- GitHub: connected
- Claude: available
- Codex: available

Shell / Tool操作はRisk levelを区別する。
通常のread / build / test / scoped write等はPolicyの範囲内で自動実行可能とし、
destructive / privileged / permission-changing / credential-sensitive等の危険操作はHuman Approval対象とする。

具体的なApproval境界は、後述の固定済みTool approval boundaryに従う。

RoleごとのTool CapabilityはBackend Policyとして強制し、
LLM自身の指示やpromptによって権限を拡張できない。



### [FIXED] Tool approval boundary

Tool Brokerの実行判定は、実質以下の5段階で扱う。

1. `AUTO`
   - Repo read / search
   - `git status` / `git diff`
   - Build / Test / Lint / Type check
   - Log確認
   - Web / Docsのread-only取得

2. `SCOPED_AUTO`
   - Task Working Set内のsource / test編集
   - 許可されたworktree / branch作成
   - 通常commit
   - Project-local dependency追加
   - Agent生成物・一時ファイルのTask scope内削除
   - AI専用branchへの通常push
   - Task目的に含まれる通常PR作成

3. `APPROVAL`
   - Host OSへ影響するpackage install / service変更
   - 広範囲・復元困難な削除
   - destructive DB migration
   - 通常Task scopeを超える外部write
   - Production / public exposure等、外部影響が大きい操作

4. `STRONG_APPROVAL`
   - Merge
   - protected branchへの直接push
   - force push
   - ACL / Role / Permission変更
   - credential登録・更新・削除
   - sudo / privileged operation
   - Project / User等の重要削除
   - その他Security-sensitive操作

   Strong Approvalは明示的Human Approvalを必須とし、
   System Role / Policy上Step-up認証が必要な場合はPasskey等のStep-upも要求する。

5. `DENY`
   - Agentによるcredential plaintext取得
   - SSH private key / secretそのものの読み出し
   - Backend ACL / Tool Brokerの迂回
   - 自己権限昇格
   - Policyで明示禁止された操作

#### Git boundary

AI専用branchでは、Policy / Task scope内で以下を自動化可能とする。

```text
edit
→ test
→ commit
→ push ai/task-...
→ PR create
```

一方、最終Mergeは従来どおりHuman-controlledとする。

#### Environment boundary

Project-local environmentとHost OSを分離する。

- Project-local dependency change: `SCOPED_AUTO`
- Host-wide package / system service / firewall / mount等: `APPROVAL` 以上

#### External write

外部writeは、UserがTask開始時に明示的に許可した目的範囲なら
Task限定Capabilityとして付与できる。

例:
- Issue作成
- PR作成
- 明示された外部投稿

Task scopeに含まれない外部送信・公開は自動で拡張しない。



### [FIXED] Storage placement: HDD Model Store / Memory Markdown

8TB HDDを主に以下の用途へ使用する。

- Local LLM Model Store
- Memory Markdown Projection
- PostgreSQL local backup
- WAL archive
- Long-term archive
- 低頻度Dataset / artifacts

NVMeは以下を優先する。

- PostgreSQL active data
- pgvector / indexes
- Repo / worktree
- Runtime / containers
- Build cache
- latency-sensitive temporary data

#### Local model storage

Local LLM weightsのcanonical local storeはHDDを基本とする。

理由:
- ModelをGPUへloadした後、通常のinference中はweight fileへの継続的なdisk accessはほぼ不要
- HDD容量をModel Storeへ使い、NVMeをactive workloadへ残せる
- 数十GB〜100GB級modelを複数保持しやすい

注意:
- HDDからのcold model loadはNVMeより大幅に遅い
- model switchingを頻繁に行う場合、startup latencyが問題になり得る

そのためArchitectureはOptionalな `NVMe Hot Model Cache / Staging` を許容する。

```text
HDD Model Store
   ↓
必要な場合のみstage/cache
   ↓
NVMe Hot Model Cache
   ↓
GPU Load
```

通常はHDDから直接load可能とし、
頻繁に使うMain ModelやBenchmark中のcandidateのみNVMeへ一時cacheできるようにする。

Hot CacheはSource of Truthではなく再生成可能なcacheとして扱う。

#### Memory Markdown

Memory Markdown ProjectionもHDD上で常用可能とする。

LLMの通常Memory RetrievalはPostgreSQL + pgvectorを利用するため、
HDD上のMarkdown I/OをChat latencyのcritical pathへ置かない。

Markdown Projectionは:
- Human-readable projection
- Git backup source
- Recovery fallback
- Manual inspection

として扱う。



### [FIXED] Model storage policy: Benchmark on NVMe, Production on HDD

Local LLM modelの配置方針を以下で確定する。

#### Benchmark / model selection phase
- Candidate modelはNVMe SSDへ配置する
- Model load時間による比較ノイズを減らす
- 複数modelを頻繁に切り替えるBenchmarkを高速化する
- Benchmark終了後、不要なcandidate cacheは削除可能

#### Production operation
- Canonical local model storeは8TB HDDを基本とする
- Main modelはHDDからGPUへloadし、長時間常駐運用する
- Inference開始後はdisk性能をcritical pathにしない
- 頻繁なmodel switchingが必要な一時期間のみNVMe Hot Cache / Stagingを利用可能

#### NVMe priority
NVMeは以下の常時低遅延用途を優先する。

- PostgreSQL active data
- pgvector / index
- Repository / worktree
- Runtime / container data
- Active build / temporary data
- Benchmark candidate models

#### HDD priority
HDDは以下を基本とする。

- Production model store
- Memory Markdown Projection
- PostgreSQL local backup
- WAL archive
- Long-term archive
- Low-frequency datasets / artifacts



### [FIXED] Backup simplification: Git for external fallback

V1ではOff-serverの完全なPostgreSQL backupを必須にしない。

外部Fallbackの中心は、Memory Markdown Projectionを専用Private Gitへ定期pushする方式とする。

#### Git backup

Gitへ保存する:
- Memory Markdown Projection
- 必要な非secret設定
- Human-readable recovery information

Gitへ保存しない:
- PostgreSQL binary data
- DB dump
- WAL
- Raw Conversation database
- Secret plaintext
- encryption key
- runtime cache

Memory Markdown Git backupは既存方針どおり:
- dirty flag
- 30分ごとのbatch check
- 変更がなければ何もしない
- 複数更新を1 commitへまとめる
- manual backup可能
- failure retry / notificationあり

#### Local database recovery

PostgreSQL本体はNVMeに置く。

ローカル復旧用としてHDDへDB backupを保存できる設計を維持するが、
V1の外部backup先としてDBそのものをGitへpushしない。

定期DB backupの運用とは別に、schema / data migrationを行う更新では、
後述のDeployment要件に従い、検証済みのDB復旧点を必須とする。
ローカルで取得可能な復旧点でよく、Off-server PostgreSQL backupをV1必須にはしない。

#### Accepted residual risk

サーバー全損時:
- Long-term Memoryのhuman-readable projectionはPrivate Gitから復旧可能
- Project / User Memoryの主要内容を再構築可能
- Raw Conversation
- Task / Agent state
- 一部metadata / history
- DB固有状態

は完全復旧できない可能性がある。

この残存リスクをV1では許容し、
将来NAS / Object Storage / 別Machine等が用意できた場合に
Off-server PostgreSQL backupを追加可能なArchitectureにする。



### [FIXED] Dedicated Recovery Repository

Private Gitは単なるMemory Markdown backupではなく、
Personal AI Workspace専用の `Recovery Repository` として扱う。

通常運用時:
- PostgreSQL = Operational Source of Truth
- Recovery Git = Disaster Recovery Source
- Gitから通常DBへ双方向同期しない
- 障害時のみRecovery Importを実施する

#### Recovery Repository contents

例:

```text
personal-ai-recovery/
├─ manifest.yaml
├─ schema-version
├─ memory/
│  ├─ users/
│  ├─ projects/
│  ├─ repos/
│  └─ shared/
├─ users/
├─ projects/
├─ repos/
├─ policies/
│  ├─ agents.yaml
│  ├─ permissions.yaml
│  ├─ quotas.yaml
│  └─ routing.yaml
├─ config/
│  ├─ models.yaml
│  ├─ notifications.yaml
│  └─ system.yaml
├─ tasks/
│  └─ recovery summaries
└─ recovery/
   └─ checksums / metadata
```

保存対象:
- Memory Markdown Projection
- Memory version / relationを再構築するためのmachine-readable metadata
- User metadata（secretを除く）
- Project metadata
- Repo metadata / remote / default branch等
- Project Member / ACL
- Agent Policy
- Quota
- Model / Router設定
- Notification設定
- Task recovery summary
- Recovery format version
- Workspace schema / version情報
- Checksum / generated_at等

Gitへ保存しない:
- PostgreSQL binary data
- DB dump / WAL
- Raw DB files
- Password hash
- Passkey secret material
- API Token
- SSH private key
- Secret plaintext
- encryption key
- Runtime cache

#### Recovery target

V1では以下を目標とする。

```text
Fresh Ubuntu
  ↓
Personal AI Workspace install
  ↓
Recovery Repository指定
  ↓
Recovery format / schema version確認
  ↓
PostgreSQL schema作成
  ↓
User / Project / Repo metadata復元
  ↓
Memory復元
  ↓
ACL / Policy / Quota / Settings復元
  ↓
GitHub Repoを再clone
  ↓
Credential類のみ再登録
  ↓
運用再開
```

目標:
`新品Ubuntu + Recovery Git + GitHub等の外部アカウント再認証` から、
Personal AI Workspaceの主要状態を再構築できること。

#### Raw Conversation / Task detail

Raw Conversation全文やTool log全文はGitへ常時保存しない。

代わりに:
- Long-term Memory
- Important decisions
- Conversation summary（必要な場合）
- Task goal
- Task result summary
- Related Repo / branch / PR
- Important unresolved state

等をRecovery用に保存する。

実行中Taskの完全なruntime state復旧はV1では保証しない。

#### Versioning

Recovery formatには明示的なversionを持つ。

例:

```yaml
recovery_format_version: 1
workspace_schema_version: 1
generated_at: ...
```

将来のWorkspace / DB schema変更時にはMigration Layerで旧Recovery formatを読み込めるようにする。

#### Git schedule

既存方針を維持:
- dirty flag
- 30分ごとのbatch check
- 変更がなければpushしない
- 複数変更を1 commitへまとめる
- manual backup可能
- failure retry / notificationあり

#### Deletion / history

通常削除:
- Current Recovery Projectionから削除
- 過去Git historyには残る可能性がある

完全消去:
- Recovery Git history rewrite等、通常削除とは別の処理が必要
- Userの30日保留期限到達時は「User Deletion Retention」に従い、削除対象の履歴・復旧用コピーも消去する
- 履歴・コピーの消去検証前に完全削除済みと表示しない
- 古いRecovery / DB backupからの復旧でも、最新の削除状態を適用し削除済み個人データを再生成しない

この違いをUI / Policy上で明示する。



### [FIXED] Observability / System Health baseline

通常時は静かなUIとし、異常時のみ目立たせる。

常時監視対象:
- GPU utilization
- VRAM used / reserved / available
- Main Model residency / health
- Memory Worker status
- Task Queue / Running / Waiting for Resource
- PostgreSQL health
- Recovery Repositoryの最終projection / commit / push
- Claude / Codex等External Agent connection status
- Agent failure / loop / retry / OOM等

既存のNotification Policyへ接続する。

- `INFO`: 正常完了、通常状態
- `WARNING`: 単発の軽微なfailure、retry、resource pressure
- `ERROR`: 継続failure、job停止、backup/recovery push継続失敗、OOM連発等
- `CRITICAL`: PostgreSQL異常、Secret Store異常、重大なRecovery不能、Security-sensitive incident等

通常時は詳細metricsを前面に出しすぎず、
System Health / Task Detail / Admin画面から展開できるようにする。



### [FIXED] Observability retention / downsampling

数値Metricsは時間経過に応じてdownsampleする。

初期Retention:
- 直近24時間: 10〜30秒粒度
- 直近7日: 1分粒度
- 直近30日: 5分粒度
- それ以降: 1時間集計

重要Eventは時系列Metricsと分離し、集約せず保持する。

重要Event例:
- Task start / complete / fail / cancel
- Agent escalation
- Loop detection
- OOM
- Model load / unload failure
- Recovery Repository push failure
- PostgreSQL error
- Permission denial
- Security-sensitive operation
- Critical notification

Retention目安:
- Task detail log / raw tool stdout / stderr: 90日
- Operational important event: 1年以上
- Audit / Security event: 長期保持

保存期間・粒度は、実運用時の容量と調査ニーズに応じて変更可能にする。



### [FIXED] Deployment / Update / Rollback

Personal AI Workspace本体は `versioned release` として管理する。

基本Update flow:

```text
Update requested
  ↓
Pre-update checks
  ↓
新規Task受付停止
  ↓
Running Taskをsafe drain / checkpoint
  ↓
DB writerの停止 / 書込み遮断（schema / data migration時）
  ↓
Recovery Projectionを最新化
  ↓
DB migration precheck
  ↓
整合したDB復旧点の取得 / 復元検証（schema / data migration時）
  ↓
Application / schema / data migration適用
  ↓
New version起動
  ↓
Health check
  ↓
Success: Queue再開
Failure: DB / ApplicationをRollbackし、正常性確認後のみQueue再開
```

#### Release types

以下を分離する。

- `Application update`
  - Backend / Frontend / Agent orchestration / Tool logic
- `Model update`
  - Main LLM / Memory Worker / Embedding / Reranker等
  - Application releaseとは独立して変更可能
- `Schema migration`
  - PostgreSQL schema変更
  - Application updateより慎重に扱う

#### Update policy

V1ではProductionの自動更新を行わない。

- Update availableをNotification Centerへ表示
- Release note / migration有無を確認可能
- Owner / Adminが明示的にUpdate開始
- Security updateは通常より強い通知を許可

#### Pre-update checks

最低限確認する。

- Recovery Repositoryが最新化可能
- PostgreSQL health
- migration compatibility
- disk free space
- Running / Waiting Task
- current version / target version
- required runtime / dependency availability

Schema / data migrationを含む場合は、DB変更前に以下を必須とする。

- 整合したDB backup / snapshot等の復旧点と、対応する既知の正常application version
- 復旧点の読出し可能性・schema/version整合性と、隔離環境で検証した復元手順
- 復旧点取得 / 検証 / migration / 必要なrollbackを通じたDB writerの停止または書込み遮断

Taskだけでなく、Memory Worker、Journal、projection job、API等のDB書込みも対象にする。
復旧点取得失敗、容量不足、復元検証失敗等の場合はmigrationを開始しない。
復旧点と検証結果を記録し、DB dump / WAL / snapshotはRecovery Gitへ保存しない。
Recovery Projectionの最新化はDB復旧点の代替にならない。
DBを変更しないapplication / model updateには、このDB復旧点要件を追加しない。

#### Task handling

通常Updateでは:
- 新規Task受付を停止
- Running Taskを安全なcheckpointまでdrain
- Task state / branch / worktreeを保持
- Update完了後にQueue / Taskを再開

Critical Security Updateでは、必要に応じてsafe drainを待たず `Stop Now` を利用可能とする。

#### Rollback

Application releaseは複数versionを保持し、直前のknown-good versionへ戻せるようにする。

DB migrationを伴う場合は、可能な限りBackward-compatible migrationを採用する。
Transactional migrationも利用可能だが、COMMIT後のhealth check失敗等に備えるDB復旧点の代替にはしない。

例:
1. 新schema / column追加
2. 新旧両対応
3. data migration
4. 十分な検証後に旧schema削除

Application binaryだけを戻してDB schema incompatibleになる設計を避ける。
Migration途中または更新後のhealth checkで失敗した場合は、必要に応じてDBを復旧点へ戻し、
対応する既知の正常applicationを起動する。
DBとapplicationの正常性・互換性、および最新のUser削除状態の適用を確認できた場合のみ、Queue / Task / DB writerを再開する。
復元に失敗した場合は保守状態を維持してOwner/Adminへ通知する。

#### Deployment implementation

Docker / systemd / package layout等の具体的実装方式は、
Architecture / implementation phaseで決定可能とし、
上記のversioning / drain / health check / rollback要件を満たすことを優先する。



### [FIXED] Shared Codex / Claude system connection

External Agentの認証はサービスごとに分ける。

#### GitHub
GitHubは従来どおりUserごとの個別認証とする。

- Linux Userごとに `gh auth login`
- GitHub account / SSH key / git identityを分離
- User削除時はそのUserのGitHub接続を無効化

#### Codex / Claude
CodexとClaudeはUserごとの個別Connectionではなく、
Personal AI Workspace全体で共有する `System-level Connection` とする。

```text
Personal AI Workspace
├─ Shared Codex Connection
└─ Shared Claude Connection
      ↓
Users / Tasks consume through Backend
```

Credential管理:
- Owner / Adminが接続設定
- Backend Secret Store / Tool Broker経由で利用
- General User / Agentへcredential plaintextを表示しない
- Credential更新 / 削除はSecurity-sensitive operationとして扱う

#### Usage attribution / quota

Shared Credentialであっても利用量はUser / Task単位で記録する。

計測対象例:
- request count
- runtime
- token / context usage（取得可能な範囲）
- concurrent executions
- task count
- failure / retry
- Project / Task relation

既存のPer-user quotaはそのまま適用する。

例:

```text
System Claude Subscription
├─ User A: 100 tasks/day
├─ User B: 20 tasks/day
└─ Owner: Unlimited
```

共有ConnectionであることはQuota共有・無制限を意味しない。

#### Lifecycle

- User削除時にShared Codex / Claude Connection自体は削除しない
- Project membership変更でもSystem Connectionは維持する
- UserがCodex / Claudeを利用できるかはBackend Policy / Role / Quotaで判定する

Project権限とExternal Agent利用権限は別に扱う。



### [FIXED] IDE / Remote development integration

V1では専用VS Code / IDE Extensionを基本的に作らない。

開発者の主要な作業経路は以下とする。

```text
Mac / Windows
  ├─ VS Code Remote SSH等の既存GUI
  └─ SSH / CLI
        ↓
Ubuntu
  ├─ Repo / worktree
  └─ Personal AI Workspace Backend
```

Personal AI Workspace本体はIDE非依存とする。

V1の主要Interface:
- Personal AI Workspace GUI
- CLI
- Existing Remote SSH GUI（VS Code Remote SSH等）

Dedicated IDE Extensionは `[FUTURE]` とする。

#### Consequence

専用Extensionを使わないため、Backendは以下のIDE内部状態を自動取得しない。

- active file
- cursor position
- selection
- unsaved buffer
- IDE diagnosticsのリアルタイム状態

Agentが扱うsourceは原則:
- 保存済みRepository file
- Taskで明示指定されたfile / path
- Userが明示的に渡したcontent

未保存変更を含む作業をAgentへ依頼する場合は、Userが保存するか明示的に内容を渡す。

#### CLI

CLIはGUIと同じBackend API / Auth / Permission / Task / Tool Brokerを利用する。

想定操作例:
- Project / Repo選択
- Task作成
- Task status確認
- Pause / Resume / Stop Now
- Agent / Model指定
- Worktree / Branch確認
- Diff表示
- PR / Review状態確認
- Resource / GPU status確認

CLIとGUIで権限モデルやTask stateを分岐させない。

#### Agent worktree

Agentの自律作業は引き続き専用branch / worktreeで行う。

UserがRemote SSH GUIやCLIで編集している通常checkoutと、
Agent用worktreeを分離して競合を避ける。



### [FIXED] Shared Memory permissions

Shared MemoryはWorkspace全体で共有するLLM参照用の共通知識とする。

#### Access

- Active User: Read
- Owner / Admin: Create / Edit / Delete / Restore
- General User: Shared Memoryへ直接Writeしない
- Agent: Shared Memoryへ自動昇格しない

User / Project / Repo MemoryからWorkspace全体へ共有すべき内容が見つかった場合:

```text
User / Project / Repo Memory
  ↓
Shared Memory Candidate
  ↓
Owner / Admin explicit approval
  ↓
Shared Memory
```

#### Shared Memory vs System Policy

Shared MemoryとGlobal / System Security Policyは別物とする。

- Shared Memory: LLMが参照する共通知識
- System / Security Policy: Backendが強制するmandatory policy

Shared MemoryはSystem / Security Policyを上書きできない。

Memory競合時の基本優先順位:

```text
Current explicit instruction
> Confirmed Memory
> Repo Memory
> Project Memory
> User Memory
> Shared Memory
```

Security / Owner mandatory policyはこのMemory優先順位の外側で最優先とする。



### [FIXED] GitHub merge capabilities vs Agent merge authority

GitHub Repository側のmerge method設定は、標準設定を無理に制限しない。
Merge commit / Squash / Rebase等の利用可否はGitHub側のcapabilityとして保持してよい。

ただし、Agentの実際のMerge可否はPersonal AI Workspace側のHuman Merge Policyで制御する。

- 通常: AgentはPR作成 / Review / Merge Readyまで
- Userが明示的に
  - 「問題なかったらマージして」
  - 「マージまで進めて」
  - 「問題なければマージ」
  等を指示したTask / Sessionのみ、Agentへ一時的なMERGE capabilityを付与可能
- 明示指示が無い場合、GitHub側でMerge機能が有効でもAgentはMergeしない

GitHub Repositoryのmerge method availabilityと、
Personal AI Workspaceのmerge authorizationを分離して扱う。
