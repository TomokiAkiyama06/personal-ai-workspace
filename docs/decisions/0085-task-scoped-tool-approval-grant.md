# Tool の承認の「このタスクの間は許可」（Task の間だけ有効な許可、範囲・対象外・作る人・記録・取り消し）

- Status: Approved
- Approval: 2026-10-10、Humanが作業Session内で、判断点ごとの説明（推奨つき）を受けたうえで直接回答して承認（approved as recommended, all 7 points: 決めてほしいことの 1〜7 をすべて推奨どおり承認（画面の目視確認も OK）。末尾の「承認時の決定」）
- Date: 2026-10-08
- Scope: Tool の承認（`apps/backend/paw_backend/tools/`）に Task の間だけ有効な許可（Grant）を足す。`tools/task_grants.py`、`tools/task_grant_store.py`、`tools/task_grant_memory.py`、`tools/broker.py`、`tools/approvals.py`、`api/v1/boards.py`、Migration `0193`、`apps/web/src/tasks/`（承認の Sheet と Task の画面）
- Supersedes: [Decision 0006](0006-tool-broker-policy.md)（Approved）の「承認は 1 回の呼び出しごと」を**一部**（下の 1〜3 の範囲だけ）。[Decision 0078](0078-pr-screen-and-mobile-board-api.md)（Approved）の 8（決めてほしいことの 7）「Design の『このタスクの間は許可』は出さない」。どちらも本文は書き換えない

## 背景

Human は 2026-10-08、承認の画面へのコメントで「毎回許可するのではなく、許可し続けるボタン」を求め、範囲を「**このタスクの残りの間だけ**」と選んだ。

承認済みの Decision が決めていること:

- 承認は 1 つの呼び出しごと（`call_hash` に Tool・正規化した引数・Task・依頼者が入る）。決められるのは、その Agent が代わりに働く本人だけ。Agent 自身は拒否。`STRONG_APPROVAL` は Step-up が要る（Decision 0006 の 4）。承認は Level を上げるだけで権限を広げない（0006 の 1）。
- 承認は Task の Run（試行と Retry の回数）に結び付き、Retry / Restart の後の Run は使えない。Task の終了で取り消す。Broker は使うときに Task の行を Lock して確かめる（0006 の 9）。
- 承認・拒否の HTTP の経路は `project.task.run` と `ApprovalService`。`STRONG_APPROVAL` はこの経路では承認しない（0078 の 6・7）。Design の「このタスクの間は許可」は出さない（0078 の 8）。
- Passkey の Step-up は承認でなく User に結び付く（Decision 0025 の限界）。

**次のことは要件も既存の Decision も決めていない。** Task の間の許可がどの呼び出しを覆うか（「同じ種類」の定義）、何を対象外にするか、誰がどう作り、どう記録し、どう取り消すか。[AGENTS.md](../../AGENTS.md) の「仕様変更」に従い、実装は下の推奨で置き、この Decision で承認を求める。

## 提案

### 1. Grant が覆う呼び出し（同じ Task・同じ Run・同じ Tool・同じか狭い対象）

「このタスクの間は許可」で作る Grant は、元の承認と次のすべてが同じ呼び出しだけを、人に聞かずに通す。

- 同じ Task の**同じ Run**（Retry / Restart で Run が変われば使えない。0006 の 9 と同じ）、同じ Agent、同じ依頼者（本人）、同じ Tool。
- Broker が出した Level が `APPROVAL`（`STRONG_APPROVAL` は通さない。2）。
- Scope の状態が同じか狭い（範囲内 ⊂ Task の Host の外）。
- 引数ごとに、元の承認と**同じか狭い**:
  - Path: 同じ Path か、その下（正規化した Path の `/` の区切りで比べる。`/w/a` は `/w/a-b` を覆わない）。
  - URL: Query のない URL は、同じ URL かその下（Remote と同じ `url_within` の規則。`..`・`;`・`%2e` などで外へ出られる Path は「下」と見なさない）で、Query がないもの。Query のある URL は完全一致だけ。
  - Host・Project・Repository: 完全一致。
  - 文字列・整数・真偽値（コマンド・内容など）: 完全一致（値そのものでなく SHA-256 を保存して比べる）。
  - 省略できる引数は、あるかないかも同じ。

**推奨: これ。** 文字列（コマンド）は「狭い」を判定できないので完全一致にした。代案 A: Tool が同じなら引数を問わない（`host.run(command)` が任意のコマンドになり、広すぎる）。代案 B: Tool の宣言で「変わってよい引数」を決める（Tool ごとの Review が要る。必要になれば別の Decision）。

### 2. Grant にできないもの

次の呼び出しの承認には「このタスクの間は許可」を出さず、Grant も使わない（毎回の承認のまま）。

1. `STRONG_APPROVAL`（Passkey の再認証が要るもの。main への Merge と Credential の変更を含む）。
2. `destructive` の Capability を持つ Tool（削除）。
3. `credential-use` の Capability を持つ Tool と、Credential の handle を引数に取る Tool（Credential の操作）。
4. `network` と `write` を両方持つ Tool（外部への送信・外部 write。Issue の作成なども含む）。
5. Working Set の変更の Tool（Repository の役割の変更）と、認可の Capability が `admin.*`・`owner.*`・Project の設定・Member・Agent Policy・Lifecycle・Repository の追加（ACL / Role / Permission の変更）の Tool。

判定は Broker が Tool の宣言（Capability の class と認可の Capability）と Level から行い、承認を開くときに「Grant にできるか」と Grant の型（1 の比べ方）を承認に保存する。**推奨: これ。** 代案: Tool の宣言に `task_grant: bool` を足して Tool ごとに許す（宣言漏れで広がる向きがあるので、既定拒否の一覧を推奨）。

### 3. 期間（Task が終わるまで。時間の期限は持たない）

Grant は作った Run の間だけ有効で、Task の終了（completed / failed / cancelled）、Retry / Restart、本人の取り消しで終わる。Broker は使うときに Task の行を `FOR SHARE` で Lock し、Task が動いていて Run が Grant の Run と同じことを、使用の記録と同じ Transaction で確かめる（0006 の 9 の承認の消費と同じ順序づけ）。Task の終了の Listener は、Open な承認と一緒に Grant も取り消す（表示のため。失敗しても上の確認で使えない）。

- (Task の Run, User) ごとの有効な Grant は 20 まで（暫定値。超えると 409 `task_grant_limit_reached`。前の Run の Grant は数えない）。
- **推奨: Task の終わりまで。** 代案: 承認と同じ 1 時間などの期限を足す（Human が選んだ「このタスクの残りの間」と違う）。

### 4. 作る人と経路

- 「このタスクの間は許可」は、承認の Sheet（Desktop と MobileApproval）のボタン。`POST /api/v1/approvals/{id}/decision` に `{"decision": "approve_for_task"}` を送る。Capability は承認・拒否と同じ `project.task.run`（承認の Project について）、Service は `ApprovalService.approve_for_task`。
- 作れるのは承認が尋ねている本人（`requester_user_id`）だけ。Agent 自身・他の Member・Admin / Owner は作れない（他の人には 404 `approval_not_found`）。
- 1 つの Transaction で、その承認を承認し（いま待っている呼び出しは、これまでどおりその承認を 1 回使う）、Grant を作る。Task が動いていて承認の Run が現在の Run のときだけ（そうでなければ 409 `task_not_active`）。Grant にできない承認は 409 `task_grant_not_allowed`（承認は pending のまま）。
- `GET /api/v1/approvals` の各承認に `task_grant_allowed` を足し、Web はそれが真のときだけボタンを出す。

### 5. 自動の許可の記録

Grant で通した呼び出しごとに、

- Broker の判断の Audit の行（`tool.<name>`、`allow`、理由 `task_grant_applied`）。
- Grant の Audit の行（`tool.approval.grant.use`、Resource は `tool_task_grant` とその Grant の ID、同じ Correlation ID）。この行が書けなければ呼び出しは通さない（`audit_unavailable`）。
- 使用の行 `tool_task_grant_uses`（Grant の ID、`call_hash`、Correlation ID、Run、時刻。追記のみ）を、使用の確認と同じ Transaction で書く。

Grant を作る・取り消すときも Audit の行（`tool.approval.grant`、`tool.approval.grant.revoke`）を書く。Agent の Runtime は Grant を作れない（`ApprovalService` は Agent に渡さない。0006 の 7 の前提）。

### 6. 取り消し（Task の画面の「このタスクで許可中」）

- Task の画面に、そのタスクの有効な Grant（Tool・元の承認の `summary`・作った時刻・使った回数）を出し、「取り消す」で止める。見えるのは本人だけで、Task の現在の Run の Grant だけ（`GET /api/v1/tasks/{id}/approval-grants`、`tasks.list`）。
- 取り消しは `POST /api/v1/approval-grants/{id}/revoke`。本人と Admin / Owner（権利を減らすだけなので。0006 の 4 の 3）。他の人には 404。取り消しの後の呼び出しは、毎回の承認に戻る。使用と取り消しは Grant の行の Lock で順序づけ、取り消しの Commit の後に Grant で通る呼び出しはない。

### 7. 保存（Migration 0193）

- `tool_approvals.grant_pattern`（JSONB、NULL 可）: Broker が承認を開くときに入れる 1 の型。NULL は Grant にできない。Trigger で変更を拒否し、Application の Role に UPDATE を与えない。
- `tool_task_grants`: Grant（Task・Run・Project・Agent・依頼者・Tool・型・表示用の `summary`・元の承認・状態 `active` / `revoked`・取り消した人と時刻）。作成は `active` だけ、変更は `active` → `revoked` だけ、DELETE / TRUNCATE は拒否（Trigger は `ENABLE ALWAYS`）。元の承認ごとに 1 つ。
- `tool_task_grant_uses`: 使用の行（追記のみ）。
- Application の Role は `tool_task_grants` に SELECT・INSERT と状態の列の UPDATE、`tool_task_grant_uses` に SELECT・INSERT だけ。

### 8. いま本番で効く範囲

本番の Tool Registry にあるのは Working Set の変更の Tool だけで、どれも 2 の 5 で対象外である。この Decision の仕組みは、`APPROVAL` の Tool（Task の Host の外の Web の読み取り、Host 全体の環境のコマンドなど）が登録されたときに効く。それまで画面にボタンは出ない（`task_grant_allowed` が偽）。

## 決めてほしいこと

1. **Grant が覆うのは、同じ Task・同じ Run・同じ Agent・同じ本人・同じ Tool・Level が `APPROVAL` で、Scope の状態と引数が同じか狭い呼び出し（Path と Query のない URL は下、他は完全一致）**（1）でよいか。推奨: はい。代案 A: Tool だけで判定。代案 B: Tool ごとに変わってよい引数を宣言。
2. **対象外は `STRONG_APPROVAL`・削除（`destructive`）・Credential（`credential-use` と handle）・外部送信（`network` + `write`）・Working Set と ACL / Role / Permission の変更**（2）でよいか。推奨: はい（既定拒否の一覧）。
3. **期間は Task の終わり（終了・Retry / Restart）まで、時間の期限なし。(Task の Run, User) ごとに有効な Grant は 20 まで（暫定値）**（3）でよいか。推奨: はい。
4. **作るのは承認が尋ねている本人だけで、`project.task.run` と `ApprovalService` の経路で、その承認の承認と Grant の作成を 1 つの Transaction で行う**（4）でよいか。推奨: はい。
5. **自動の許可は、Broker の判断の行・Grant の ID つきの Audit の行・使用の行の 3 つで記録し、Grant の Audit が書けなければ通さない**（5）でよいか。推奨: はい。
6. **「このタスクで許可中」は本人だけに見せ、取り消しは本人と Admin / Owner**（6）でよいか。推奨: はい。
7. **Decision 0006 の「1 回の呼び出しごと」をこの範囲で一部置き換え、Decision 0078 の 8（ボタンを出さない）を置き換える**でよいか。推奨: はい。

## 既知の制限

- Path の「下」は、Symlink を解決する前の正規化した Path で比べる。後の呼び出しも Scope・Policy・認可・Repository の ACL の検査は毎回すべて通るので、Task の Scope の外には出ない。Scope の中の Symlink で別の場所を指す呼び出しは、Grant の Path の下として通りうる。
- 引数の型は承認を開いたときの Broker の判定で、承認の行に保存する。この Decision の前に開いた承認（`grant_pattern` が NULL）には、ボタンは出ない。
- Grant を探す読み取りが失敗・時間切れのときは、Grant を使わず毎回の承認に戻る（人に聞く向きに倒れる）。

## 承認後の扱い

承認されたら Status を Approved に改め、承認の内容を記録する。
方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。
[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。

## 承認時の決定（2026-10-10）

Human は、作業 Session で判断が必要な点について推奨つきの説明を受け、直接回答して承認した（2026-10-10、Human (approved as recommended, all 7 points)。決めてほしいことの 1〜7 をすべて推奨どおり承認（画面の目視確認も OK））。
