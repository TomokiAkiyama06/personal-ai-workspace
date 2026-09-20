# Personal AI Workspace UI Design

更新日: 2026-09-16\
Status: [PROVISIONAL DESIGN / FIXED PRINCIPLES]

この文書はPersonal AI Workspaceの画面設計・情報設計・操作原則をまとめる。
機能要件そのものは `REQUIREMENTS.md` を正とし、この文書は実装時のUI/UX指針として利用する。

---

## 1. UI基本原則

[FIXED]

- V1の基本UI言語は **日本語**
- 将来の英語対応を考慮し、実装はi18n-readyとする
- Desktop / Web / Mobileで同じ情報構造を維持する
- 通常操作では画面を不要なModalで遮らない
- 状態・進行・Agent動作・Memory同期状態をユーザーが確認できる
- 高度な情報は隠さず、必要時に深掘りできる
- Project RepoへUI都合の管理ファイルを勝手に追加しない
- Owner / Admin機能は別アカウントではなく、権限に応じて同一UI内で表示する

---

## 2. Global Layout

Desktopの基本構成イメージ:

```text
┌─────────────────────────────────────────────────────────────────────┐
│ Personal AI Workspace                 Search      🔔       User Menu │
├───────────────┬─────────────────────────────────────────────────────┤
│ Global Nav    │ Main Content                                        │
│               │                                                     │
│ Chat          │                                                     │
│ Projects      │                                                     │
│ Agents        │                                                     │
│ Memory        │                                                     │
│ Pull Requests │                                                     │
│ Usage         │                                                     │
│               │                                                     │
│ ───────────   │                                                     │
│ Admin*        │                                                     │
│ Settings      │                                                     │
└───────────────┴─────────────────────────────────────────────────────┘
```

`Admin` はAdmin / Owner権限を持つユーザーにのみ表示する。

Global Header:
- Global Search
- Notification Bell
- User / Device menu
- 必要に応じてSystem / GPU status

---

## 3. Global Navigation

V1候補:

- Chat
- Projects
- Agents / Tasks
- Memory
- Pull Requests
- Usage
- Admin（Admin / Ownerのみ）
- Settings

機能数が増えた場合でも、主要操作をSidebarに詰め込みすぎない。
低頻度機能はSettings / Admin配下へ移動する。

---

## 4. Chat UI

ChatはPersonal AI Workspaceの主要入口とする。

想定構成:

```text
┌────────────────────────────────────────────────────────────────────┐
│ Project: ExampleProject     Agent: Auto     Model Router: Auto     │
├────────────────────────────────────────────────────────────────────┤
│                                                                    │
│ User                                                               │
│ 認証周りを直して                                                   │
│                                                                    │
│ Assistant                                                          │
│ 調査しています…                                                    │
│                                                                    │
│ ┌ Agent Activity ────────────────────────────────────────────────┐ │
│ │ Local Agent     Repo調査 ✓                                    │ │
│ │ Codex           Review中…                                     │ │
│ │ Claude          待機                                          │ │
│ └───────────────────────────────────────────────────────────────┘ │
│                                                                    │
├────────────────────────────────────────────────────────────────────┤
│ 添付 / Project / Repo / Tool                       [ Send ]        │
└────────────────────────────────────────────────────────────────────┘
```

原則:
- Agentが何をしているか確認できる
- 自動処理を完全なブラックボックスにしない
- Tool call / Test / Review等は折りたたみ可能
- 高リスクActionは必要時のみ確認UIを表示
- Pending Memoryがある場合でも会話自体は継続可能

---

## 5. Project UI

Projectは複数Repoを束ねられる上位概念。

Project Overview候補:

```text
Project: ExampleProject

[概要] [Repos] [Tasks] [Memory] [PR] [Members] [Settings]

Status
- Active Agents: 2
- Open PR: 3
- Memory: synced
- Repos: 2

Repositories
- backend
- ios

Recent Activity
- PR #42 reviewed
- Memory decision updated
- Agent task completed
```

Projectに参加しているユーザーは、原則としてProject配下のRepo Memoryを閲覧できる。
Repo単位のACL overrideがある場合はUI上で明示する。

---

## 6. Memory Management UI

[FIXED]

Desktopは3ペイン構成。

```text
┌──────────────┬──────────────────┬──────────────────────────────────┐
│ Scope        │ Memory一覧       │ Memory詳細                       │
│              │                  │                                  │
│ 👤 User      │ 📌 UIは日本語    │ [本文][履歴][ソース][詳細]       │
│ 📁 Projects  │ ● Merge Policy   │                                  │
│  ├ Project A │ ◐ Preference     │ Markdown View / Edit             │
│  │ ├ Repo A  │ ⚠ Revalidate    │                                  │
│  │ └ Repo B  │                  │                                  │
│ 🌐 Shared    │                  │                                  │
└──────────────┴──────────────────┴──────────────────────────────────┘
```

### 6.1 Scope Pane

- User Memory
- Project Memory
- Repo Memory
- Shared Memory

Project配下のRepoをTree表示する。

### 6.2 Memory List

最低限表示:
- Title
- Scope
- Confirmed / Inferred
- Active / Stale / Superseded
- Freshness
- Pin
- Updated

Filter:
- Scope
- Status
- Freshness
- Type
- Project / Repo

### 6.3 Memory Detail

Tabs:
- 本文
- 履歴
- ソース
- 詳細

本文はMarkdownとして表示し、編集可能。

---

## 7. Memory History Graph

[FIXED]

GitLensのGraphに近い履歴UIを採用する。

```text
● Current / Confirmed
│
├─● Inferred Candidate
│ │
│ ○ Observation
│
● Superseded
```

表現対象:
- supersedes
- extends
- conflicts_with
- confirmed_from
- revalidated_from
- merged_from

Node click:
- 当時の本文
- Diff
- Actor
- Timestamp
- Scope
- Status
- Source
- Change reason

過去versionへのRestoreは、古いVersionを直接書き換えず、
その内容を基に新しいActive Versionを作る。

---

## 8. Memory Source View

Memoryが「なぜ存在するか」を確認できる。

例:

```text
Sources

Conversation #1842
「今後レビューはCodexとClaudeで」

Conversation #1904
「今回も両方でレビューして」

Task #531
ExampleProject PR #42

User Confirmation
「このProjectに適用」
```

Sourceが削除済みの場合はその旨を表示する。

---

## 9. Inferred Preference Confirmation UI

[FIXED]

繰り返し観測されたPreferenceは、チャットを大きく遮らず確認する。

```text
この指定を何度か使っています。
今後も既定にしますか？

[このRepoだけ]
[このProject]
[すべてのProject]
[保存しない]
[その他...]
```

`その他...` は自然文入力可能。

例:

```text
開発系Projectだけ適用して。
ただしmainへのMergeは毎回確認して。
```

LLMが構造化した解釈結果を表示する。

高リスク変更の場合:

```text
解釈結果

対象: 開発系Project
レビュー: Codex + Claude
main Merge: 毎回確認

[この内容で設定]
[修正]
[キャンセル]
```

---

## 10. Notification Center

[FIXED]

通常通知はBellへ集約する。

```text
🔔 3

- Memory Backupが1回失敗しました。Retry中
- Repo解析が完了しました
- Memoryを2件整理しました
```

Severity:
- INFO
- WARNING
- ERROR
- CRITICAL

通常:
- Bell badge
- Notification Center

重大:
- Non-modal Banner
- CRITICALのみ必要に応じModal

同種通知はまとめる。

---

## 11. Agent / Task UI

Agent実行は状態が追えるようにする。

表示候補:
- Task
- User
- Project / Repo
- Assigned Agent
- Model
- Status
- Started / Runtime
- Current Step
- Tool calls
- Token / GPU usage
- Branch / Worktree
- PR
- Review status

例:

```text
Task #203
ExampleProject Auth Fix

Local Agent   Implementation ✓
Codex         Review ✓
Claude        Review…
Evaluator     Pending

Branch: ai/auth-fix-203
PR: #42
```

---

## 12. PR / Merge UI

Human Merge AuthorityをUI上でも明確にする。

```text
PR #42

Implementation   ✓
Tests            ✓
Codex Review     ✓
Claude Review    ✓
Evaluator        ✓

Status: Merge Ready

[Diffを見る]
[レビューを見る]
[マージ]
```

原則、自動でMergeしない。

人間からそのTask / Sessionで明示的なMerge許可がある場合のみ、
AgentがMerge可能。

---

## 13. Usage UI

User / Adminで表示範囲を変える。

User:
- 自分のLocal / Codex / Claude利用
- Task count
- Token
- Runtime
- GPU time
- Quota

Admin:
- User比較
- Model別
- Category別
- Project別
- Time series
- Failure / Escalation
- Quota consumption

Graph中心で確認可能にする。

---

## 14. Admin UI

Admin / Ownerのみ表示。

候補:
- Users
- Usage
- Quotas
- Models / Router
- Global Prompts
- Permissions
- Audit
- Backup
- System Health
- Notification Rules

Owner限定:
- Admin追加 / 削除
- Backup destination
- Credential設定
- Critical authentication settings
- Owner recovery関連

---

## 15. Authentication UI

基本フロー:

```text
Login
- Username
- Password
- Passkey
```

新規端末:

```text
既存端末
[新規端末を追加]
     ↓
QR / Share Link
     ↓
新端末
```

Owner / Adminの新端末追加では、既存Trusted Deviceの承認も要求する。

Session管理:
- Device
- Last active
- Current device
- Revoke
- Log out all other devices

---

## 16. Backup UI

Owner / Admin:

```text
Memory Backup

Status: ✓ 正常
Last success: 13:30
Next check: 14:00

Destination: configured
Dirty: No

[今すぐバックアップ]
```

Ownerのみ:
- Destination変更
- Credential更新
- Enable / Disable

Admin:
- Status
- Retry
- Manual run
- Audit

---

## 17. Mobile UI

MobileはDesktopの情報構造を維持しつつ、階層型にする。

Memory:

```text
Memory List
   ↓
Memory Detail
   ↓
本文 / 履歴 / ソース / 詳細
```

Project:

```text
Project List
   ↓
Project
   ↓
Overview / Repos / Tasks / Memory / PR
```

Mobileでは主に:
- Chat
- 状態確認
- Notification
- Approval
- Merge confirmation
- Agent stop / retry
- Memory確認

を重視する。

複雑なDiffや大量編集はDesktop優先でもよい。

---

## 18. Status / System Health

必要に応じてGlobal HeaderまたはAdmin Dashboardに以下を表示可能にする。

- Local LLM status
- GPU Mode
- GPU / VRAM usage
- Memory Worker
- Pending Memory
- Backup
- Agent Queue
- Codex / Claude connection

常時すべてを出して画面を騒がしくしない。
正常時はCompact、異常時だけ目立たせる。

---

## 19. Visual Direction

現時点では詳細なDesign Systemは未確定。

方向性:
- 情報密度は比較的高め
- VS Code / GitLensのような開発ツール的視認性
- ChatGPT / Claude系の会話しやすさ
- 過剰な装飾を避ける
- Status / Diff / Graphを読みやすくする
- Dark / Light両対応を前提にする
- DesktopではResizable Paneを積極的に利用

具体的なColor / Typography / Component LibraryはFrontend設計時に決定する。

---

## 20. Open UI Decisions

今後決める項目:

- Chat / Project / Agentの最終Navigation構造
- Global Searchの対象
- Command Paletteの有無
- Agent Activityの表示粒度
- Diff Viewer
- Graph描画Library
- Mobileの承認UI
- Desktop App / Webでの差分
- Keyboard Shortcut
- Design System / Component Library
- Dark / Light theme詳細



## 21. Multi-Repo Working Set UI

[FIXED]

Projectに複数Repoがある場合でも、Chat / Taskでは現在のWorking Setを明示する。

例:

```text
Project: ExampleProject

Repo Context:
[ backend × ] [ ios × ] [ + Repo ]
```

Repoごとの役割も確認できるようにする。

```text
backend   Target
ios       Referenced
docs      -
```

Agentが別Repoの参照を提案することは可能。
Write対象へ追加する場合は、Read-only追加より慎重なUXにする。

Multi-Repo Task詳細ではRepoごとに以下を表示する。

- Role: Referenced / Working / Target
- Branch / Worktree
- Test status
- Review status
- PR
- Current step

Task全体の状態とRepo別状態を分けて表示する。



## Later fixed UI additions (v29-v40 consolidation)

### Project members / roles
- Manager / Contributor / Viewer
- Project is invite-only
- Repo ACL overrides are visibly distinguished from inherited Project permissions

### Project lifecycle
- Active / Archived / Pending deletion / Deleted
- Archive is the normal cleanup path
- Delete is a low-frequency dangerous action with 30-day pending period
- GitHub Repo and local checkout are not deleted automatically

### New Project
Minimal creation flow:
- Project name
- description (optional)
- Repo (optional / later)
- members (optional / later)

Safe defaults are applied for Agent routing, Merge, Memory and Web Research.

### Agent Task states
- Queued
- Running
- Waiting for User
- Waiting for Approval
- Waiting for Resource
- Paused
- Evaluating / Reviewing
- Completed / Failed / Cancelled

### Task controls
- Pause / Resume / Cancel / Retry / Restart / Stop Now
- Cancel does not automatically delete worktree / branch / partial output

### Queue / budget
- HIGH / NORMAL / LOW
- Standard / Long / Unlimited task-budget presets
- Loop detection and escalation are shown in Task Activity

### Parallel Agent DAG
Task detail can expand into dependency nodes showing:
Role, Agent, Model, status, Repo, worktree, runtime, resource usage, tests, retry and escalation.

### GPU / Resource Scheduler
System Health can expose:
VRAM used/reserved/available, GPU utilization, model residency, Local queue,
parallelism limit reason, Waiting for Resource tasks, and Full GPU Mode.

### Research / Knowledge
Research Scratch is separate from Long-term Memory.
UI shows sources, timestamps, TTL, Project/Task relation, pin and Memory promotion.

### Tool approval
Tool actions can be surfaced as:
AUTO / SCOPED_AUTO / APPROVAL / STRONG_APPROVAL / DENY.
Normal coding activity stays low-friction; dangerous operations receive explicit approval UI.



## Recovery Repository UI

[FIXED]

Owner/Admin向けBackup / Recovery画面で以下を確認できるようにする。

- Recovery Repository connection status
- Last successful projection generation
- Last Git commit / push
- Pending changes
- Recovery format version
- Workspace schema version
- Last recovery validation
- Backup failure / retry state

Owner向けに:
- Manual backup
- Recovery validation
- Restore wizard
- Recovery repository設定

を用意する。

通常削除とGit historyを含む完全消去は別操作として明示する。



## System Health / Observability

[FIXED BASELINE]

通常時はHeader等にcompactなhealth stateのみ表示する。

詳細画面では:
- GPU / VRAM
- Model residency
- Task Queue
- Waiting for Resource
- PostgreSQL
- Recovery Repository
- External Agent / Provider
- recent failure / retry / OOM

を確認できる。

異常通知は既存のINFO / WARNING / ERROR / CRITICALポリシーに従う。



## Codex / Claude connection UI

[FIXED]

Codex / ClaudeはUser SettingsではなくSystem / Admin Settingsに表示する。

Owner / Admin:
- Connection status
- Account / plan label（取得可能な範囲）
- Last health check
- Replace credential / reconnect
- Disable connection

General User:
- `Claude: Available / Unavailable`
- `Codex: Available / Unavailable`
- 自分のusage / quota

Credential plaintextは表示しない。



## IDE integration scope

[FIXED]

V1では専用IDE Extensionを作らない。

Personal AI Workspace GUIをControl Centerとし、
実コード編集はVS Code Remote SSH等の既存GUIまたはCLIを利用する。

Workspace GUIからは以下を確認可能にする。
- Project / Repo
- Task
- Branch / Worktree
- Diff
- PR / Review
- Agent status
- Resource status

IDE内部のactive file / cursor / selection / unsaved buffer連携はV1外とする。
必要性が出た場合のみ将来Optional Extensionとして追加する。
