# 落ちた Process が残した Repository の書き込み予約を人が解除する操作

- Status: Approved
- Date: 2026-09-28
- Scope: Issue [#129](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/129)（落ちた Process が残した Repository 書き込み予約の手動解除）
- Supersedes: [Decision 0035](0035-working-set-capability-grants-and-legacy-tasks.md) の 5 節の「落ちた Executor の予約は期限まで残す（Human が解放する操作は今は作らない）」の部分だけ。0035 の 5 節のほかの点（予約がある間の Begin evaluation / Complete の拒否、停止系の Command と Retry / Restart の扱い、終わった Task への書き込みの拒否）と 1〜4 節は変えない
- Approval: 2026-09-29、Humanが作業Session内で、判断点ごとの説明（推奨つき）を受けたうえで「推奨どおり」と回答して承認（1〜6 の全点。4 は案 4A。末尾の「承認時の決定」）

## 背景

PR #124（Issue #85）は、Tool Broker が許可した Repository への書き込み（または `execute`）を、Executor が終わるまで `task_repository_writes` の予約として残す。予約が解放も期限切れもしていない間は、その Task の Begin evaluation / Complete と、その Repo の降格・削除が `RepositoryWriteInFlightError` で拒否される（Decision 0035 の 5 節、Approved）。

Executor の Process が落ちると予約は解放されず、期限（`WRITE_RESERVATION_SECONDS`、約 24 時間 15 分）まで Task は評価にも完了にも進めない。Decision 0035 は「Executor が確実に止まったと Human が確かめたときに解放する操作（Audit つき）を加えるかどうかは、この Decision では決めず、必要になったら別の Issue にする」とし、承認時にも「Human が解放する操作は今は作らない」とした。Issue #129 はその別の Issue で、Acceptance Criteria は「Owner / Admin（または Project Manager）が予約を解除する操作。Capability、Step-up の要否、Audit を決める」「解除するときは、書き込みを `modified` かもしれない扱いにする（評価と Review をやり直す）」「必要なら Decision で提案する」である。

誰が解除できるか、Step-up を求めるか、何を記録するか、どういうときは解除を拒否するかは、どの承認済み Decision にも書かれていない（Security Policy の選択で、AGENTS.md 1.8 / 14 により実装が勝手に確定しない）。この Decision で提案する。実装（このブランチ）は下の推奨どおりの値を置いており、承認で変わっても変わるのは `authz` の付与の集合と `projects/task_write_release.py`・`TaskService.release_stale_repository_write` の検査だけで、Schema は変わらない（Migration `0129` は `task_events.command` に値を 1 つ加えるだけ）。

## 提案

### 1. 誰が解除できるか: Project の Manager と Owner / Admin

**推奨: 新しい Capability `project.task.write_reservation.release`（Scope.PROJECT）を、Project の Manager と、System の Owner / Admin に与える。Contributor・Viewer には与えない。**

- 解除は「Executor がまだ書き込むかもしれない」という安全装置を外す操作で、外すと評価・Review・降格の判定が、まだ変わり得る Repo に対して行われ得る。Task を実行できる者（Contributor）全員ではなく、Project を管理する者に限る。
- Owner / Admin は `project.lifecycle.manage` と同じく「System Owner / Admin: administrative operations」として、Member でなくても解除できる（Server の Process を再起動した Operator が、落ちた Process の予約を片付ける経路）。
- Project の状態: 他の Project Capability と同じく、Active の Project だけで許す（Archived は読み取りのみ、Pending deletion は Lifecycle の操作のみ。Policy の既存の規則）。
- 判定は 2 回行う。最初の判定（Audit つき）で拒否されれば何も書かない。許可されたら、解除を書く Transaction の中でもう一度、Project の行を `FOR SHARE` で Lock してから Project の状態と操作する人の Membership を読み直して同じ Policy で判定する（Archive・Delete・Member の削除・Role の変更は Project の行を `FOR UPDATE` で Lock するので、その間に割り込めない）。最初の判定の後に Archive・Delete・削除・降格が Commit されていれば、解除はすべて Rollback し、その拒否を Audit に残す（許可の Event は 2 回は残さない）。

検討した他の案: Contributor にも与える（Task を実行する本人が解除できる。上の理由で却下）。Owner / Admin だけ（Project の日常の運用に Admin が要る。Issue #129 が Project Manager を挙げているので却下）。

### 2. 委任: 不可（`delegable=False`）

**推奨: Agent には委任できない。** 予約は Agent の Executor の書き込みを守るためのもので、Agent が自分（や他の Agent）の予約を外せる理由がない。解除は、Executor が止まったことを人が確かめてから行う。

### 3. Step-up: Passkey Step-up を求める

**推奨: 操作する人の Session に、Policy の Window 内の Passkey Step-up を求める。** 他の Owner / Admin の Sensitive operation（Decision 0015・0032・0033）と同じ `StepUpGuard`（`require_passkey_step_up_in`）を使う。Password の Step-up では通さない。

- 確認は、解除を書く Transaction の中、Commit の前に行う（`StepUpGuard` が Session の行を `FOR SHARE` で Lock するので、確認と Commit の間に Session の失効が入らない）。Step-up がなければ解除はすべて Rollback する（`StepUpRequiredError` / `StepUpMethodInsufficientError`）。
- Manager は Owner / Admin ではないが、同じ Step-up を求める（Decision 0035 の 2 節の `STRONG_APPROVAL` が、Working Set を狭める変更に Human の Step-up を求めるのと同じ重さの操作と考える）。

検討した他の案: Step-up を求めない（Session を盗まれただけで安全装置を外せるので却下）。

### 4. 解除を拒否する場合: 生きている Worker がいる間

**推奨: Task の Queue Entry に有効な Lease を持つ Worker がいる間は、解除を拒否する（`RepositoryWriteHolderAliveError`。何も書かない）。**

- Lease は、Process が生きていることを Backend が確かめられる唯一の証拠である（Worker は Heartbeat で Lease を延ばし、落ちると Lease が切れる）。有効な Lease がある間は、その Worker の Executor がまだ書き込み得る。
- 判定は Queue と同じく DB の時計（`clock_timestamp()`）で行い、Entry を `FOR SHARE` で Lock してから次の文で判定する（Lock を待つ間に切れた Lease を有効と見ない。Heartbeat の `FOR UPDATE` とは互いに待つ）。
- Lease が切れている（または Entry が `queued`・終わっている・ない）ときは、Backend には Executor が止まったことは分からない。そこから先は、**人が確かめたこと（`reason`、必須）を記録して、人の責任で解除する**。Lease だけで自動的に解放はしない（Executor が別の Host で Worker より長く生きる場合がある）。
- ほかに拒否する場合: 予約がその Task のものでない（`RepositoryWriteNotFoundError`）、解放済み・期限切れ（`RepositoryWriteNotHeldError`）、`expected_version` が古い（`TaskConflictError`）。

確認してほしい影響（重要）: 拒否は「どの Worker の Lease か」を区別しない。そのため、**落ちた Worker の Entry を別の Worker が取り直した後は、落ちた Executor の予約も解除できない**。これは Retry の後だけでなく、**通常の落ち方でも起きる**。Queue は Lease が切れた `claimed` の Entry を自動で取り直させる（Decision 0007 の 7 節。`claim_count` が 1 増える。同じ id で再起動した Worker でも同じ）。たとえば:

1. Worker A が Tool の呼び出しの途中で落ちる（予約が残る）。
2. A の Lease が切れ、Worker B（または再起動した A）が Entry を取り直して続きを実行する。B は Heartbeat で有効な Lease を持つ。
3. B が Begin evaluation に進むと、A の予約のために `RepositoryWriteInFlightError` で拒否される。
4. Manager が解除しようとしても、B の Lease が有効なので `RepositoryWriteHolderAliveError` で拒否される。

したがって Issue #129 の主な場面では、多くの場合、解除の前に次の手順が要る:

1. Task を **Pause**（または **Stop Now**）する。Worker は実行中の Node を終えてから Entry を完了させ（Orchestrator の既存の動き）、Lease が終わる。Worker も落ちていれば Lease の期限が切れるのを待つ。
2. Lease が終わったことを確かめ（解除が `RepositoryWriteHolderAliveError` を返さなくなる）、落ちた Executor の Process / Host がないことを確かめて、`reason` を書いて**解除**する。
3. **Resume** する（Stop Now の場合は Retry）。解除した Repo は書き込まれたものとして扱われ、Evaluation / Review をやり直す。

何もしなければ、予約の期限（約 24 時間 15 分）まで Task は評価にも完了にも進めない。`test_a_reclaimed_entry_holds_the_dead_executors_reservation` がこの場面（予約 → Lease 切れ → `claim_next` による取り直し → 解除の拒否 → Lease が終わった後の解除）を確かめる。

代わりの案（4B、未実装）: 許可の時点で、その Task の `claimed` の Entry の `(id, claim_count)`（Lease の世代。Decision 0007 の 7 節で main にある）を `task_repository_writes` に記録し、**同じ世代の Lease が有効な間だけ**拒否する。落ちた Holder とその後を継いだ Worker を区別でき、Pause の手順は要らなくなる（後を継いだ Worker の Lease は、落ちた Executor が生きていることの証拠ではないので、Lease が切れた場合と同じ扱いになる）。Issue #126 の Fencing を待たずに実装できるが、`task_repository_writes` に列を 2 つ加える Migration と、許可の Transaction で Entry を読む変更が要る（記録した世代が誤っても、拒否が増える向きにしか誤らない: 許可を出した Worker とは別の Worker の世代を記録した場合、その Worker の Lease の間は拒否される）。承認で 4B を選ぶ場合は、この PR か続きの PR で実装する。

検討した他の案: Lease を見ずに人の判断だけで解除する（生きている Executor の予約を誤って外せるので却下）。予約から一定時間（例: Runner の Timeout）経つまで解除を拒否する（落ちた Process を待たせる理由がない。Lease の方が直接の証拠なので却下）。

### 5. 解除の効果: 書き込まれたものとして扱う

**推奨: 解除した予約の各 Repo を、予約した試行（`attempt`）で書き込まれたものとして扱う。** `task_attempt_repositories` の `modified = true`、Evaluation `not_run`、Review `not_started` にする（Issue #129 の「`modified` かもしれない扱い、評価と Review をやり直す」）。許可の時点で同じ値にしてあるが、解除でもう一度そろえる。Repo の降格・削除には、これまでどおり変更の破棄の確認（Decision 0030 の 3 節）が要る。

- 予約の `released_at` を解除の時刻にする（行は消さない）。後から戻ってきた Executor の `release_repository_use` は何も変えない（冪等）。
- Task の状態は変えない。どの状態でも解除できる（Failed の Task の予約を Retry の前に外す経路。Completed の Task には予約が残らない: 予約がある間は Complete が拒否され、Complete の後は書き込みが許可されない）。
- Restart の後に前の試行の予約を解除した場合は、前の試行の Repo の状態に記録する（新しい試行は 0030 の 1 節どおり新しい状態から始まる）。

### 6. Audit と記録

**推奨: 次の 2 つを残す。**

- **Authorization の判定**: `Authorizer` が Capability の判定を Audit Event に残す（Audit Mode `REQUIRED`。許可も拒否も記録し、Audit に書けなければ拒否になる）。Actor、Action（`project.task.write_reservation.release`）、Project、結果と理由が残る。
- **解除そのもの**: Task の履歴（`task_events`、追記のみ）に `release_repository_write` の Event を 1 行残す。Actor（人の User id）、`reason`（何を確かめたか。必須）、`detail` に予約の id・Repo・実行（試行と Retry 回数）・許可と期限の時刻。Task の版を 1 つ上げる。

Step-up の拒否は、この操作では別の Audit Event にしない（Authorization の許可の Event は残り、Task の Event は残らない。Step-up の拒否は呼び出し側に `StepUpRequiredError` として返る）。Auth の Audit（`AuthAudit`）にも残すかは、HTTP の経路を作るときに決めてよい。

### 7. この Decision に含めないこと

- 期限（`WRITE_RESERVATION_SECONDS`）を Runner ごとの実行時間に合わせて短くすること（Decision 0035 の 5 節で同じく送られた案。今回も送る）。
- Lease が切れたときの自動解放（4 節のとおり、人の確認を挟む）。
- 予約を Lease の世代に結び付けること（4 節の 4B。承認で選ばれた場合に実装する）。
- HTTP の経路と UI（Task の API はまだない。`projects.TaskWriteReleaser` を API 層から呼ぶ）。

## 影響

- `authz`: Capability を 1 つ加える（`capabilities.py`、`policy.py` の `_ADMIN_ONLY` と `_MANAGER_ONLY`）。`Authorizer.policy`（読み取りのみ）を加える（解除の Transaction の中の 2 回目の判定が、同じ Policy で Audit なしに判定するため）。`tests/test_authz_policy.py` の表も同じく変わる。
- `tasks`: `TaskCommand.RELEASE_REPOSITORY_WRITE`（どの状態も `execute` の Command としては受け付けない）、`TaskService.release_stale_repository_write`、Error 3 つ。Migration `0129`（`task_events.command` の CHECK に値を加える。権限は変えない）。
- `projects`: `TaskWriteReleaser`（Authorization → 解除 → 同じ Transaction の中で Project の行を `FOR SHARE` で Lock して Authorization をもう一度 → Step-up）。
- 1〜3 が変われば、付与の集合・`delegable`・`TaskWriteReleaser` の Step-up を変える。4・5 が変われば `TaskService.release_stale_repository_write` の検査と効果を変える。Schema は変わらない。

## 承認後の扱い

承認されたら Status を Approved に改め、`Approval` に日付と承認の様子を記録する。承認後に方針を変える場合は、この Decision を書き換えず、新しい Decision から `Supersedes` する。

## 決めてほしいこと（2026-09-29 に推奨どおり承認）

1. **解除できるのは Project の Manager と Owner / Admin（新しい Capability `project.task.write_reservation.release`）で、Contributor・Viewer はできない。Active の Project だけ**（1 節）でよいか。推奨: はい。
2. **Agent には委任できない（`delegable=False`）**（2 節）でよいか。推奨: はい。
3. **操作する人の Session の Passkey Step-up（Policy の Window 内）を、解除の Transaction の中で確かめる。Manager にも求める**（3 節）でよいか。推奨: はい。
4. **Task の Queue Entry に有効な Lease を持つ Worker がいる間は解除を拒否する。Lease がなければ、人が確かめたこと（`reason`、必須）を記録して解除を許す**（4 節、4A）でよいか。どの Worker の Lease かは区別しないので、**落ちた Worker の Entry を Queue が自動で別の Worker（または再起動した同じ Worker）に取り直させた後（Retry がなくても起きる、通常の落ち方）は解除が拒否され、Pause / Stop Now → Lease が終わる → 解除 → Resume（Stop Now なら Retry）の手順が要る**点を含む。それとも、許可の時点の Lease の世代（Entry の id と `claim_count`）を予約に記録し、同じ世代の Lease が有効な間だけ拒否する 4B（Migration が要る。Pause の手順は要らない）にするか。推奨: 4A（実装済み）。Pause の手順が運用で重すぎると判断する場合は 4B。
5. **解除した Repo は予約した試行で書き込まれたものとして扱い（`modified`、Evaluation / Review をやり直す）、Task の状態は変えず、どの状態でも解除できる**（5 節）でよいか。推奨: はい。
6. **Audit は Authorization の判定（`REQUIRED`）と Task の履歴の `release_repository_write` の Event の 2 つ。Step-up の拒否は別の Audit にしない**（6 節）でよいか。推奨: はい。

## 承認時の決定（2026-09-29）

Human は、作業 Session で判断が必要な点について推奨つきの説明を受け、「推奨どおり」と回答して承認した（1〜6 の全点。4 は案 4A）。**すべて推奨どおり**で、個別の変更はない。4 は案 4A（有効な Lease の Worker がいる間は解除を拒否する。自動で再 Claim された後は、Pause → Lease の終了 → 解除 → Resume の順に操作する）とする。
