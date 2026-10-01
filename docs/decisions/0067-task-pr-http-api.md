# Task / DAG / 操作 / PR の記録の HTTP API の方針（一覧の Capability、見える範囲、操作の権限と Queue、Merge Ready、未実装の Board）

- Status: Approved
- Approval: 2026-10-01、Humanが作業Session内で、判断点ごとの説明（推奨つき）を受けたうえで直接回答して承認（判断点1〜8、すべて推奨どおり。末尾の「承認時の決定」）
- Date: 2026-10-01
- Scope: Issue [#185](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/185)（UI #48・PR #174 の接続先）の `apps/backend/paw_backend/api/v1/tasks.py`、`apps/backend/paw_backend/api/v1/task_views.py`、Capability `tasks.list`、`apps/web/src/tasks/apiSource.ts`
- Supersedes: なし。[Decision 0004](0004-rbac-capability-and-audit-policy.md)（Approved）の Capability 表と読み取り専用の許可リストに 1 つを加える（書き換えない）

## 背景

PAW-062 の画面（`/agents`、`/pulls`）は `TaskSource` でデータを受ける形でマージされ、本番では API がないため「未提供」を表示している（PR #174）。Python の Service（`TaskService`、`DagStore`、`BudgetTracker`、`TaskQueue`）はあるが、HTTP の経路がない。

承認済みの Decision が決めていること: 権限は Backend の Capability で判定し、Project 内は Membership（招待制）で、System Role だけでは所属しない Project を見られない（0004・0008・0022）。Repository の ACL override は Project の権限を狭めるだけ（REQUIREMENTS「Project roles」）。Task の操作は遷移表（`tasks/domain.py`）が唯一の判定で、Stop Now は理由が必須（REQUIREMENTS「Task pause / cancel / retry / restart」）。Merge は人間が行い、API の機能と Merge の許可を混同しない（AGENTS.md）。Project が Active でないと Retry / Restart / Start と Queue への投入は拒否される（0020）。

**次のことは要件も既存の Decision も決めていない。** 自分の Task / PR の一覧を、どの Capability で受けるか。一覧で何を隠すか。誰がどの操作を送れるか、操作の後に Queue をどうするか。Merge Ready をどう判定するか。画面が求める並列の上限 / VRAM と、PR 画面・モバイルの Board（変更ファイル、Review の要約、Audit の行、Tool の承認、Diff）をどうするか。AGENTS.md の「仕様変更」に従い、実装は下の推奨で置き、この Decision で承認を求める。

## 提案

### 1. 一覧の Capability `tasks.list`

`/api/v1` の経路はすべて `require_capability` で守る（`tests/test_authz_routes.py`）。Task の詳細と操作は Task の Project について `project.read` / `project.task.run` で判定できるが、「自分が見られるすべての Project の Task の一覧」と「PR の一覧」には判定の対象の Project が一つではない。そこで次の Capability を足す。

- `tasks.list`（`Scope.SYSTEM`、委任不可、読み取り専用＝Audit は拒否だけ）: User・Admin・Owner。一覧を求めてよいことだけを表す。**中身は 2 のとおり Project ごと・Repository ごとに `project.read` で絞る**ので、これ自体は何のデータも開かない。Agent は自分の Task を Scope で読むので要らない（委任不可）。

### 2. 見える範囲

- Task は、その Project に `project.read` があるとき（受諾した Membership、Active か Archived）だけ一覧に出し、詳細を返す。Owner / Admin も所属しない Project の Task は見えない（System Role は Project を開かない、0004）。
- Task の Repository（名前、Branch、Worktree の Path、Review、Evaluation、PR）は、**その Repository に** `project.read` があるときだけ出す。ACL override で拒否された Repository は詳細からも一覧からも外し、その PR も PR の一覧から外す（存在も名前も出さない）。一覧の Card の Repository は、読める最初の `target`、なければ読める最初の Repository。
- 存在しない Task と、見られない Project の Task はどちらも 403 `forbidden`（区別しない。拒否は Audit される）。
- Task の入力、Log、Node の Goal / 結果、Event の詳細は返さない（画面が使わない）。Worktree の Path は返す（同じ Project の Member が見る Task の事実。PR #174 の「事実の帯」）。
- 一覧は更新の新しい順に最大 `limit`（既定 100、上限 200）。Page 送り（Cursor）は作らない。

### 3. 操作の権限と Queue

- 操作（Pause / Resume / Cancel / Retry / Restart / Stop Now）は `project.task.run`（Contributor 以上、Audit 必須）と、画面が見た `expected_version`（必須。違えば 409 `task_conflict`）で受け、可否は `TaskService.execute` の遷移表が決める（409 `illegal_transition`）。Stop Now は理由が必須（422）。Archived の Project では Policy が `project.task.run` を拒否する（403）。
- **止める操作（Pause / Cancel / Stop Now）は `project.task.run` を持つ Member なら誰でも、動かし直す操作（Resume / Retry / Restart）は Task の作成者だけ**（他は 403 `task_creator_only`）。理由: 動き出した Task は作成者の権限・GitHub の Identity・Linux Account で動く（Decision 0029・0052）ので、他の Member が作成者の名前で仕事を再開できない方がよい。止めることはいつでもできる（REQUIREMENTS の Stop Now）。
- 動かし直す操作は、**同じ Transaction の中で Task を Queue へ戻す**（`TaskService.execute(..., in_transaction=...)`）。Priority は Task の最後の Queue Entry のもの（なければ `normal`）。Task がまだ有効な Entry を持っている（Worker が止まり切っていない）ときは新しく作らず、その Worker が続ける（`compute.holds` の Resume と同じ）。止める操作では Queue に触れない（Orchestrator の Worker が Task の状態を見て止まり、Entry を終える）。
- 既知の限界: Pause の直後、Worker が Entry を終える直前に Resume が来ると、Entry が残っているので新しい Entry は作られず、その後 Worker が Entry を終えると Task は `running` のまま Queue にない。`compute.holds` の Resume と同じ限界で、Orchestrator 側の Sweep で拾うのが筋（後の Issue）。

### 4. Merge Ready の判定

PR の記録（`task_attempt_repositories` の PR）に、Backend が次の条件で `merge_ready` を付ける。**Task が `completed`**（Integration Gate はテスト・Evaluator・Review が通り、PR を届けた後にだけ Complete する）、**PR が Task の今の Attempt のもの**、**PR が `open`**、**その Repository の Review が `approved` かつ Evaluation が `passed`**。Merge の API は作らない（画面の Merge ボタンは GitHub の PR へのリンクのまま）。CI / Review の承認は Merge の許可ではない（AGENTS.md）。

### 5. 画面とのずれ（Backend に合わせて UI を直す）

- ツールの呼び出しは、Backend では Task の **今の Step** に属し、DAG の Node には結び付いていない。API は `current_step.tool_calls` として返し、画面は「いま実行しているステップ」のツール呼び出しとして出す（Node の詳細には出さない）。
- 予算は Preset と 6 項目の消費 / 上限を返し、画面の「残り %」は上限のある項目の残りの割合の最小値を Web の Adapter が出す（表示の計算だけ）。
- PR の Title は Task の Title（`integration/publish.py` が PR の Title にするもの）。

### 6. 並列の上限 / VRAM（未実装）

Scheduler の「Task の並列の上限」に当たる値が Backend にない（`max_parallel_nodes` は一つの Task の Node の上限、Compute Scheduler の値は Deployment ごとの Sequence と KV）。VRAM は System Health（Owner / Admin の詳細、0059）で見られる。**この PR では一覧に `capacity` を返さない**（画面は値がないとき表示しない）。何を「並列の上限」とするかを決めてから足す。

### 7. 未実装の Board（Issue #185 の 6）

PR の画面とモバイルの Board 用の、変更したファイル・Review の要約・Audit の行・Tool の承認（MobileApproval）・Diff（MobileDiff）は、**この PR では作らない**。どれも新しい読み取りの Model（変更ファイルと Diff は Worktree / GitHub を読む、Review の要約は Check の Verdict を保存しない方針（`integration/gate.py`）との調整、Audit の行は `audit_events` を Project の Member に見せる範囲、Tool の承認は `ApprovalService` の承認・拒否を HTTP で受けるための Step-up）を要する。Issue #185 は閉じず（Refs）、後の PR で扱う。

### 8. Migration

不要（既存の Index で足りる）。予約された Revision `0185` は使わない。

## 決めてほしいこと

1. **一覧を新しい Capability `tasks.list`（全 User、委任不可、読み取り専用）で受け、中身を `project.read` で Project ごと・Repository ごとに絞る**（1・2）でよいか。推奨: はい。代案: 既存の `account.read` で受ける（意味が違う）。
2. **ACL で拒否された Repository は、Task の詳細と一覧と PR の一覧から外す（名前も出さない）**（2）でよいか。推奨: はい。代案: 名前だけ出して中身を隠す。
3. **止める操作は `project.task.run` の Member なら誰でも、Resume / Retry / Restart は Task の作成者だけ**（3）でよいか。推奨: はい。代案 A: すべて `project.task.run` の Member なら誰でも。代案 B: 動かし直す操作は作成者と Project Manager。
4. **Resume / Retry / Restart は同じ Transaction で Queue へ戻す（最後の Priority、有効な Entry があれば作らない）。止める操作では Queue に触れない**（3）でよいか。推奨: はい。既知の限界（Pause 直後の Resume）は後の Issue の Sweep で扱う。
5. **Merge Ready = Task が `completed`・PR が今の Attempt のもので `open`・Review `approved`・Evaluation `passed`。Merge の API は作らない**（4）でよいか。推奨: はい。
6. **ツールの呼び出しは今の Step のものとして出し、Node の詳細には出さない**（5）でよいか。推奨: はい（Backend に Node と Step の結び付きがない）。
7. **並列の上限 / VRAM は今は返さない**（6）でよいか。推奨: はい。何を並列の上限とするか（Task の Worker 数を設定に足す、Compute Scheduler の Sequence の合計、など）を決めてから足す。
8. **PR 画面・モバイルの Board（変更ファイル、Review の要約、Audit の行、Tool の承認、Diff）は後の PR**（7）でよいか。推奨: はい。Tool の承認は Step-up の扱いを含めて別の Decision にする。

## 承認後の扱い

承認されたら Status を Approved に改め、承認の内容を記録する。
方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。
[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。

## 承認時の決定（2026-10-01）

Human は、作業 Session で判断が必要な点について推奨つきの説明を受け、直接回答して承認した（判断点1〜8、すべて推奨どおり）。
