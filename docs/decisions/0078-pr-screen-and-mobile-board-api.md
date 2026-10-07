# PR 画面とモバイルの Board の HTTP API（変更ファイルと差分の出所、Review の要約、Audit の行の見える範囲、Tool の承認の経路）

- Status: Proposed
- Date: 2026-10-07
- Scope: Issue [#185](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/185) の 6（PR の画面とモバイルの Board 用：変更したファイル、Review の要約、Audit の行、Tool の承認（MobileApproval）、Diff（MobileDiff））。`apps/backend/paw_backend/api/v1/boards.py`、`api/v1/board_views.py`、`integration/changes.py`、`integration/gate.py`、Migration `0190`、`apps/web/src/tasks/`
- Supersedes: なし。[Decision 0067](0067-task-pr-http-api.md)（Approved）の 7・8 で後に回した Board を扱う（0067 は書き換えない）

## 背景

[Decision 0067](0067-task-pr-http-api.md) の 7 は、PR 画面とモバイルの Board の 5 つを後の PR に回した。どれも新しい読み取りの Model が要るためである。

承認済みの Decision が決めていること: PR の記録を見られるのは、その Project とその Repository に `project.read` がある人だけ（0067 の 2）。Merge の API は作らない（0067 の 4、AGENTS.md）。Integration Gate は Check が言ったことを保存しない（`integration/gate.py`、Decision 0036）。git の Sub-command は Wrapper の許可リストのものだけ（Decision 0029・0036）で、`diff` はない。Tool の承認は 1 回の呼び出しごとで、決められるのはその Agent が代わりに働く本人だけ、`STRONG_APPROVAL` は Step-up が要る（Decision 0006、`tools/approvals.py`）。Passkey の Step-up は User に結び付き、承認には結び付かない（Decision 0025 の限界、`auth/passkeys/approvals.py`）。全体の Audit Log の閲覧は Admin 専用（`docs/SECURITY_RBAC_AUDIT.md` の `/admin/audit`、`admin.audit.view`）。

**次のことは要件も既存の Decision も決めていない。** 変更ファイルと差分をどこから読むか。Review の要約に何を出すか。Task の Audit の行を誰にどこまで見せるか。Tool の承認を HTTP でどう受けるか、Strong Approval と Design の「このタスクの間は許可」をどうするか。AGENTS.md の「仕様変更」に従い、実装は下の推奨で置き、この Decision で承認を求める。

## 提案

### 1. 変更ファイルと差分の出所（PR を届けた時点で GitHub から 1 回読んで保存）

Integration Gate が PR を作って Attempt に記録し、Task の完了（または停止）を決めた後で、**その PR の変更ファイルを GitHub の `pulls/{n}/files` から、Task の作成者の `gh api`（Publisher と同じ Identity）で読み、PR の記録（`task_attempt_repositories` の行）に保存する**（新しい Table `pull_request_changes`、Migration `0190`）。画面はこれだけを読む（閲覧のたびに GitHub や Worktree は読まない）。保存されるのは、確認した Commit（Merge Ready の Commit）を Default Branch に対して届けた差分そのものである。

- 読むのは Best effort で、Task の完了の経路の外で行う（失敗・時間切れでは何も保存せず、画面は「変更ファイルは記録されていません」）。読む前と後に PR の Head が確認した Commit であることを確かめ、違えば保存しない（別の Commit の差分を混ぜない）。同じ記録を読み直したら置き換える。
- 代案 A: 閲覧時に Worktree で `git diff`。Wrapper の許可リストに `diff` を足す（Decision 0029・0036 の変更）必要があり、他の User の Linux Account の Worktree を閲覧のたびに読む。
- 代案 B: 閲覧時に GitHub を読む。他の Member の閲覧で作成者の GitHub の Identity を使うことになり、5 秒ごとの再読み込みで Rate limit にかかる。

### 2. 保存する範囲と安全

- ファイルは最大 300（GitHub は最大 3,000 を返す。超えた分は `truncated`）。Path は 300 文字で切る。
- Patch は先頭の 50 ファイルだけ、1 ファイル 9,000 文字まで（Redact はその少し先まで読んだうえで行い、切るときは行の切れ目で切る。超えたら `patch_truncated`）。GitHub が Patch を返さない Binary や巨大なファイルは Patch なし。
- `gh` の出力の上限（64 KiB）に、最悪の名前でも収まるように Page の大きさと `--jq` の切り詰めを決める（読み切れずに失敗しない）。全体で 120 秒の期限。
- Patch は Credential を Redact し（`tools.credentials.redact_text`）、Tab と改行以外の制御文字を見える Escape にし、その後でも 9,000 文字を超えないように切る。Path も制御文字を Escape する。読んだ内容は Log に出さない。
- 保存した行は記録と一緒に消える（`ON DELETE CASCADE`）。Application の Role は SELECT・INSERT・UPDATE だけ。

### 3. 変更ファイル・差分・Review・Audit を見られる人

PR の記録を見られる人と同じ（Decision 0067 の 2: Project と Repository に `project.read`）。それ以外の記録と存在しない記録は、どれも 404 `pull_request_not_found`。経路は `GET /api/v1/pull-requests/{id}/files`、`/files/{index}`、`/review`、`/audit`（Capability `tasks.list`）。

### 4. Review の要約（Check の本文は保存しない方針のまま）

Gate が Check の Verdict の本文を保存しない方針（0036）を保ち、**Review の要約は、記録された Review / Evaluation の結果と、その記録の Attempt の DAG の Reviewer Node（Agent・Model・状態・終了時刻）**とする。Node の Goal や結果の本文は返さない（0067 の 2）。Design の各 Reviewer の一行の要約と「レビュー全文」は出さない。

- 代案: Check の Verdict の要約を Redact して保存し、画面に出す（Gate の方針の変更。Reviewer の Check を実装する Issue で別の Decision にする）。

### 5. Audit の行の見える範囲

PR を見られる Member に、**その Task を Resource とする `audit_events` の行（Tool Broker の判断と実行）と、その Task の Tool の承認の行（承認・拒否・取り消し）を、閉じた形で**返す: 時刻、Action、allow / deny、理由の Code、行ったのが Agent か人か System か。Actor の ID・Request ID・Details は返さない。新しい順に既定 50 行、最大 100 行。全体の Audit Log（`/admin/audit`）は Admin 専用のまま。Resource で引くための Index `ix_audit_events_resource (resource_id, occurred_at)` を足す（Migration `0190`。Partition の親に作り、すべての Partition に付く）。

- 代案 A: Task の作成者と Owner / Admin だけに見せる。
- 代案 B: 出さない（`admin.audit.view` の画面だけ）。

### 6. Tool の承認の一覧

`GET /api/v1/approvals`（`tasks.list`、`task_id` で 1 つの Task に絞れる）と `GET /api/v1/approvals/{id}`（一覧の上限の外の 1 件）は、**その人が決める承認だけ**（`requester_user_id` が本人。`ApprovalService` が決められるのも本人だけ）を、読める Project の、`pending` で期限内のものに限って返す。中身は承認を作ったときの `summary`（上限つき・Redact 済み）、Level、Task の題名と Agent、承認が指す Repository のうち本人が読めるものの名前、期限。他の Member には見せない。Partial Index `ix_tool_approvals_pending_requester` を足す（Migration `0190`）。

### 7. 承認・拒否の経路と Strong Approval

`POST /api/v1/approvals/{id}/decision`（`{"decision": "approve" | "reject"}`）は、承認の Project について `project.task.run`（Contributor 以上、Audit 必須）で受け、`ApprovalService` の `approve` / `reject` を呼ぶ（本人以外は 404 `approval_not_found`、決定済みは 409 `approval_not_pending`、期限切れは 409 `approval_expired`。Service も自分の Audit の行を書く）。

**`STRONG_APPROVAL` はこの経路では承認できない**（Service の Step-up は既定の Fail closed のまま。403 `strong_approval_unavailable`。拒否はできる）。Design の MobileApproval の注記「main へのマージと Credential の変更は、この画面では許可できません。Passkey での再認証が必要です。」と同じ。

- 代案: `PasskeyApprovalStepUp` を入れて、直近の Passkey の Step-up があれば承認できるようにする。ただし Step-up が承認ではなく User に結び付く（別の Session の Step-up でも通る。Decision 0025 の限界）ので、承認に結び付く Challenge を作ってからにするのがよい（別の Issue / Decision）。

### 8. Design の「このタスクの間は許可」

Backend の承認は 1 回の呼び出しごと（Decision 0006）なので、**このボタンは出さない**（「今回だけ許可」と「拒否する」だけ）。

- 代案: Task の間だけ同じ種類の呼び出しを許す承認を新設する（Decision 0006 の変更。別の Decision）。

### 9. Design の Board との差分（画面）

- PR 画面: 完了条件に「実装 N ファイル」の行を足す（変更ファイルが記録されているとき）。テスト / Evaluator・レビュー・人のマージ承認の行は 0067 のまま（Backend の記録が 1 つずつのため）。「変更されたファイル」「レビューの要点」（Reviewer Node ごと）「監査」の欄を Design の位置に出す。「マージする」は無効のまま GitHub への Link（Merge の API はない）、「差分を見る」は差分の画面へ。マージ方法の選択とブランチ削除の Checkbox は出さない（GitHub で行う）。「レビュー全文」は出さない（4）。
- 差分（MobileDiff）: `/pulls/<id>/files/<n>`。ファイルの前後の移動とファイル一覧。Design の「編集中」の表示は出さない（届けた PR の差分なので）。
- 承認（MobileApproval）: `/approvals` の「承認待ち」の一覧と、選んだ承認の Sheet。理由・影響の行は出さない（Backend にない。`summary` の項目をそのまま出す）。タスクの画面で承認待ちのとき「確認」から開く（MobileTask）。
- MobileTask の「いま実装している箇所」（いま編集中のファイルと +/-）は出さない（Backend にない）。

### 10. 本番での組み立て

`IntegrationGate` と `GitHubPullRequestPublisher` は、まだ App の中で組み立てられていない（Gate を動かす Issue の範囲）。この PR は `IntegrationGate(changes=ChangeRecorder(...))` を足すところまでで、組み立てるときに `ChangeRecorder(GitHubChangeReader(...), PullRequestChangeStore(database))` を渡す。それまで本番の PR 画面の変更ファイルは「記録されていません」になる。

## 決めてほしいこと

1. **変更ファイルと差分は、Gate が PR を届けた直後に GitHub から作成者の `gh api` で 1 回読み、PR の記録に保存し、画面はそれだけを読む**（1）でよいか。推奨: はい。代案 A: 閲覧時に Worktree の `git diff`（Wrapper の許可リストの変更）。代案 B: 閲覧時に GitHub。
2. **保存の上限（300 ファイル、Patch は先頭 50 ファイル・各 9,000 文字、Credential の Redact）と Best effort（失敗しても Task は止めない）**（2）でよいか。推奨: はい。
3. **Review の要約は、記録された結果と DAG の Reviewer Node（Agent・Model・状態・終了時刻）だけで、Check の本文は出さない**（4）でよいか。推奨: はい（本文は Reviewer の Check を実装するときに別の Decision）。
4. **Audit の行は、PR を見られる Member に、その Task とその Tool の承認の行を閉じた形（時刻・Action・allow / deny・理由・Agent / 人 / System）で最大 100 行**（5）でよいか。推奨: はい。代案 A: 作成者と Owner / Admin だけ。代案 B: 出さない。
5. **Tool の承認の一覧は本人（その Agent が代わりに働く人）だけに、読める Project の待っているものだけ**（6）でよいか。推奨: はい。
6. **承認・拒否は `project.task.run` と `ApprovalService` で受け、`STRONG_APPROVAL` はこの経路では承認しない（拒否はできる）**（7）でよいか。推奨: はい。Passkey で承認する経路は、承認に結び付く Challenge と一緒に別の Issue にする。
7. **Design の「このタスクの間は許可」は出さない**（8）でよいか。推奨: はい。
8. **Design の Board との差分**（9。マージ方法・ブランチ削除・レビュー全文・いま実装している箇所・理由 / 影響を出さない、完了条件に「実装 N ファイル」を足す）でよいか。推奨: はい。

## 承認後の扱い

承認されたら Status を Approved に改め、承認の内容を記録する。
方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。
[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。
