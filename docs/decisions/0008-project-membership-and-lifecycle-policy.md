# Project の Membership と Lifecycle の方針

- Status: Approved
- Date: 2026-09-24
- Scope: PAW-026（Project CRUD / Membership / Lifecycle）と、Project を使う以降の Issue（PAW-027 Repository、PAW-022 Session、Project Chat など）
- Supersedes: なし
- Approval: 2026-09-25、Humanが作業Session内で、判断メモ（Artifact）の各点について「推奨どおり」と回答して承認（末尾の「承認時の決定」）

## 背景

[REQUIREMENTS.md](../../REQUIREMENTS.md) は、Project の Role（Manager / Contributor / Viewer）、招待制、Lifecycle（Active → Archived → Pending deletion → 30 日 → Deleted）、
Delete 開始時の確認操作、削除時に消すものを定めている。
[Decision 0004](0004-rbac-capability-and-audit-policy.md)（2026-09-25 承認）は、Owner / Admin が Member でなくても持つ `project.lifecycle.manage` を定めている。
一方で、次の点は要件にも Decision 0004 にも書かれていない。PAW-026 の実装は動かすために選択をした。
[AGENTS.md](../../AGENTS.md) の「仕様変更」に従い、その選択を一覧にして Human の承認を求め、2026-09-25 に承認された。

**この Decision は 2026-09-25 に Human が承認した（Approved）。** 下の各選択は、承認された方針である（承認時の決定は末尾を参照）。
数値（招待の有効期間 `INVITE_TTL`、Member と招待の合計 `MAX_MEMBERS_PER_PROJECT`、Project 名と説明の長さ）は、暫定値として承認した。いずれも `paw_backend/projects/limits.py` の定数で、後から変えられ、変えても Schema は変わらない。
承認された内容を変える場合は、この Decision を書き換えず、新しい Decision から `Supersedes` する。

実装は [Backend README](../../apps/backend/README.md) の「Project CRUD / Membership / Lifecycle」に書いている。
Delete 開始時の Task 停止（8）は、PAW-026 の独立レビューの指摘（`begin_deletion` が Project の行しか更新せず、実行中の Task を止めない）への対応で、同じ Decision に加え、あわせて承認された。

## 承認された方針

### 1. Lifecycle

1. **Delete 開始は Active からも Archived からもできる。** 要件の図は Active → Archived → Pending deletion だが、Archive を必須にする文はない（「通常運用は Delete より Archive を中心とする」）。
   Delete 開始には、**Project 名の完全一致の入力**（`confirm_name`。大文字小文字・前後の空白を区別する）を要求する（要件の「Project 名入力等の誤操作防止」）。
2. **Pending deletion の復元は `project.lifecycle.manage` を持つ人**（その Project の Manager、Owner、Admin）だけができる。復元先は Archived（要件）。
   Pending deletion では Member のアクセスは止まる（Decision 0004）ため、Manager は Member のまま復元だけができる。Contributor / Viewer はできない。
3. **30 日の数え方。** Delete 開始の時刻 `deletion_started_at` から 30 × 24 時間（720 時間。暦の「1 か月」ではない）後を `deletion_scheduled_at` とする（DB の CHECK 制約が強制する）。
   `now < deletion_scheduled_at` の間は復元でき、`now >= deletion_scheduled_at` から Purge の対象になる（同じ瞬間に両方が真になることはない）。
4. **冪等。** すでにその状態にある操作（Archived を Archive、Pending deletion を Delete 開始、Archived を復元）は成功し、何も書かない（`updated_at` も 30 日も動かない）。
   Active の復元、Pending deletion の Unarchive・Archive などは `IllegalTransitionError` とする。
5. **復元は、Manager が 1 人もいないと拒否する**（`NoManagerError`）。Manager のいない Project が Owner / Admin にも管理できなくなる（Member の管理は Manager だけの権限）のを避けるため。

### 2. Deleted が残すもの

- Purge（30 日後）は、Project の行を**消さず**、墓石（tombstone）にする: `status = 'deleted'`、`name = 'Deleted Project'`、`description` は消去。
  `id`、作成者の不透明な ID、`created_at`、`deletion_started_at`、`deletion_scheduled_at`、`deleted_at` は残す（Audit の行が指す ID を解決できるように。個人情報を含まない）。
- **Member と招待の行は全て削除する**（要件の「Project Member / ACL」）。
- Deleted の Project は、全ての操作で「存在しない」（`ProjectNotFoundError`）。
- **他の領域のデータ（Chat、Memory、Task、Repo の紐付け、調査結果）は、この Issue では消さない。** それらの `project_id` は素の UUID で、各領域の Service が `PurgeResult.purged` の ID を使って、要件のとおりに消す。
  GitHub、Local checkout などの外部資源は消さない（要件）。この分担は各 Issue へ引き継ぐ。
  調査結果（Evidence / Claim の Provenance、Research の Scratch など）を Project の消去でどう扱うか（消す・残す・匿名化）は、この分担のままでは確定せず、別 Issue [#88](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/88) で決める。

### 3. Membership

1. **招待の有効期間は 14 日**（`INVITE_TTL`）。期限（`invite_expires_at`）ちょうどからは受諾できない。期限切れの招待は、再度の招待で置き換える。
2. **辞退・取り下げ・退出は行の削除**とし、履歴は持たない（履歴は Audit に置く）。辞退した人は、再び招待できる。
3. **Member を追加できるのは Manager だけ。** Decision 0004 の Policy では `project.members.manage` は Manager だけの権限で、Owner / Admin にはない。
   Owner / Admin も、招待されなければ Project に入れない（自分で入る手段はない）。
4. **最後の Manager は退出・削除・降格できない**（`LastManagerError`）。**例外は、Pending deletion の Project からの退出**（要件の「Project が削除中でなければ」の解釈）。招待中の Manager は Manager に数えない。
5. **1 Project の Member と有効な招待の合計は 200 まで**（`MAX_MEMBERS_PER_PROJECT`。要件に上限はなく、応答のサイズを有界にするための値）。
6. 招待できるのは**存在し、`active` の User** だけ（存在しない・停止中・削除待ちの区別を返さない）。すでに Member、または有効な招待がある User の再招待はエラーで、期限を更新しない。
7. Project 名は**一意にしない**（長さ 1〜100 文字、説明は 2000 文字まで）。
8. **`project_members.user_id` は `users.id` への Foreign Key（`ON DELETE RESTRICT`）**。Member や招待のある User は物理削除できず、User 削除の流れは先に Member を外す（要件の「所有権移譲」）。

### 4. 存在を明かさない

- 認可で拒否され、かつ**操作した人が Project の Member でない**（Owner / Admin が Lifecycle 以外を試みた場合を含む）ときは、`ProjectNotFoundError`（存在しない Project と同じ）を返す。拒否の Audit は Authorizer が書く。
- Member の拒否は、状態のため（Archived の変更、Pending deletion の閲覧）は `ProjectStateError`、それ以外は理由付きの `ProjectPermissionDeniedError`。Audit を書けないときは 503 用の理由（`audit_unavailable`）を返す。

### 5. Capability を持たない操作（暫定の作りで承認）

Decision 0004 には、次の 4 つに対応する Capability がない。PAW-026 は PAW-025 の Policy を変更しないため、**Authorizer を通さず、次の本人確認だけで許可し、Audit の行を書かない**。この暫定の作りを、Human が 2026-09-25 に承認した。

| 操作 | 許可の条件 |
| --- | --- |
| Project の作成 | `system_role` が Owner / Admin / User（`SYSTEM` は不可）。作成者が最初の Manager |
| 自分宛ての招待の受諾・辞退 | 同上。Actor 自身の行だけを対象にする（他人の招待、招待のない Project は `InviteNotFoundError`） |
| Project からの退出 | 同上。Actor 自身の Member の行だけを対象にする |

**決定:** これらを Audit できるように、Capability（`project.create`（`Scope.SYSTEM`、User 以上）、`project.invitation.respond`、`project.leave`）を追加する。追加は別 Issue [#82](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/82) で行う。
承認済みの Decision 0004 は書き換えず、新しい Decision から `Supersedes` する（AGENTS.md §14）。追加されるまで、これらの操作は Audit に残らない。Project の作成は誰でもできる（要件に「作成できる人」の制限がないため）。

### 6. 一覧

- `list_projects` は、**自分の Member の行がある Project だけ**を返す（招待は含まない。Owner / Admin も同じ）。通常の一覧は Active、Archived は別の一覧（要件）。
  Pending deletion の一覧は、復元できる **Manager の Project だけ**を返す。
- **Owner / Admin が全 Project を一覧する API は、この Issue にない。** 管理上の操作（Archive、復元）は Project の ID を知っていれば Member でなくてもできるが、ID を探す手段（`admin.projects.manage`）は別 Issue [#84](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/84) で実装する。
- Principal の `project_roles` を作るための `ProjectService.roles_of(user_id)` を持つ。ただし Service 自身は、渡された Principal の `project_roles` を**信用せず**、判定のたびに DB から Actor の Role を読み直す
  （Member から外された直後の古い Principal が、古い Role で操作できないようにする）。

### 7. `Scope.SELF` の Capability

`chat.use`、`memory.use` などは、Decision 0004 のとおり Project の状態と Member 資格を見ない。この Issue は Membership の Table と `roles_of` を用意するだけで、これらの Capability の判定は変えない
（Project の Chat、Project の Memory を実装する Issue が、`roles_of` と Project の状態を使って絞る）。

### 8. Delete 開始時の Task 停止

要件の「Pending deletion」は、開始時点で「実行中Taskをsafe-stop」「新規Agent Task停止」を定める。どの Command で止めるか、どの部品が行うか、Delete 開始の後に作られる Task をどうするかは、要件にも Decision 0004 にも書かれていない。
PAW-026 のレビューで、`begin_deletion` が Project の行を更新するだけで Task を止めない点が指摘されたため、次の選択をし、Human が承認した（Outbox と、停止の Command に Cancel を使うこと）。

1. **停止の要求を、Delete 開始と同じ Transaction に残す（Outbox）。** `begin_deletion` は、Project の状態を変える Transaction で `project_task_stops`（`project_id`、`requested_at`、`processed_at`）の行を書く。両方が Commit されるか、どちらも Commit されない。
   すでにある行（復元してから再び Delete を開始した Project）は、新しい要求（`requested_at` を更新し、`processed_at` を消す）にする。何も書かない呼び出し（すでに Pending deletion）と、失敗した Delete 開始は行を書かない。
   `begin_deletion` の中では止めない: Task Service と Queue は別の Transaction を持つ別の部品で、外部の Worker は Database の Transaction の中から止められないため。
2. **Processor `ProjectTaskStopper.stop_project_tasks(project_id)` が実行する。** Orchestrator（PAW-034）が、未処理の要求（`pending_project_ids()`）の Project について、完了（`done`）まで繰り返し呼ぶ。冪等で、何度でも再実行できる。
   - Pending deletion（と Purge 済みの Deleted）の Project の active な Task（queued / running / waiting / paused / evaluating）だけを対象にする。Active / Archived の Project の Task には触れない（復元済み、または Delete していない Project を誤って止めない）。
   - Task の状態は直接書かない。Queue の取消（PAW-033 の `TaskQueue.cancel`）と、PAW-032 の Command（`TaskService.execute`）を使う。Actor は `policy`（自動の規則）、理由は固定文。
   - active な Task が 1 つも残っていないときだけ、`processed_at` を書く。
3. **停止の Command は Cancel（graceful）。** Cancel は、成果物（branch / worktree / 途中成果）を保持し、Worker が現在の Step を安全な区切りで自分で閉じられ、新しい Step は始められず、全ての active な状態で使え、復元後に Restart できる。
   Stop Now は緊急停止（実行中の Step を即時に中断）で、queued と paused には使えず、この場面の緊急性もない。Pause は、復元されない限り誰も Resume しない Task を残し、復元先の Archived は新規 Agent Task を止める状態なので、意味がない。
4. **Delete 開始の後に作られた Task。** `project.task.run` は Archived / Pending deletion で Authorizer が拒否する（PAW-025）ため、通常の経路では作られない。残る隙間は、認可の後、Delete 開始の Commit の前後に `TaskService.create_task`（または Retry / Restart）が実行される競合だけである。`TaskService` は Project の状態を見ない（PAW-032 の範囲）。
   - **PAW-026 の範囲でしたこと:** Processor は Project の状態から動くため、要求が処理済みになった後で作られた Task も、再実行で止める（`test_a_task_created_after_the_deletion_began_is_stopped_on_a_rerun`）。Orchestrator は Pending deletion の Project にも通常の周期で `stop_project_tasks` を呼ぶこと。
   - **別 Lane の変更（方針を Human が承認した。実装は Issue [#83](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/83)、PAW-034 の前後。PAW-026 では入れない）:** Task Lane（PAW-032 / PAW-034）の `create_task`、Retry、Restart と、Queue Lane（PAW-033）の `TaskQueue.enqueue`（7 の競合）が、Insert（状態の変更）と同じ Transaction で Project の行を `SELECT ... FOR SHARE` で Lock し、Active 以外なら拒否する。Delete 開始（`FOR UPDATE`）と直列になり、競合が閉じる。`enqueue` は `task_id` しか受け取らないため、Gate は Task から Project を引いて判定する。
     `tasks` の Module が `projects` を import しないよう、Project の状態を返す Gate（Protocol）を注入する形を勧める。Database の Trigger で拒否する案は、別 Lane の Table を変えること、Task Lane の Test が存在しない Project の ID を使うことから、この Decision では採らない。
5. **Restore との競合。** Processor が Project の状態を読んだ後、Task の Command の前に Restore が Commit されると、復元された Project の Task を止めることがある（Restart できる）。閉じるには Task Service が Project の行と Transaction を共有する必要があり、採らない。
   - **5 回目のレビューの指摘（窓の大きさ）:** 最初の実装は、Project を読んだ同じ Transaction で最大 `batch_size`（既定 100）件の Task の ID を一覧し、その後は Project を読まずに全件を止めていた。窓は 1 件の Command ではなく 1 回の呼び出しの全件になり、Restore の後の Archived の Project の Task を最大 100 件止められた（Sweep の Entry も同じ）。
   - **PAW-026 の範囲でしたこと:** Task 1 件ごと（Sweep は Entry 1 件ごと）の前に Project を読み直し（Lock のない読み取り 1 回）、Pending deletion / Deleted でなくなった最初の読み取りで残りを止める。Restore の前に止めた Task と Entry は止まったまま、後のものには触らない。Restore の後にもう一度 Delete が始まった Project は状態を見て止め続ける。復元された Project の要求は処理済みにする（意味を失ったため。`done` は `True`）。
     Task の Cancel と Entry の `cancel` の順序と、その間の中断は 8 で扱う（最初は Entry を先にしたが、Restore が間に入ると復元された Task が Entry のない queued のまま残るため、6 回目のレビューで Task を先にした）。
     Test: `test_a_restore_between_two_tasks_stops_the_batch`、`test_a_restore_after_the_ids_were_listed_cancels_nothing`、`test_a_restore_between_the_cancel_and_the_entry_cancel_finishes_that_task`、`test_a_restore_between_two_stray_entries_stops_the_sweep`、`test_a_restore_after_the_stray_entries_were_listed_cancels_none`、`test_a_project_that_is_deleted_again_keeps_being_stopped`。
   - **残る窓:** Project の読み取りから、その 1 件の Command（Cancel と Entry の `cancel`）の終わりまで。ここで Restore が Commit されると、その 1 件（Sweep は Entry 1 件）は止まる。Processor が別の Transaction で Project の行を `FOR SHARE` で持ったまま 2 つの部品を呼ぶ案は採らない: Stopper 1 つにつき Pool の接続を 2 本使い、この Module が時間を制限できない Task Service と Listener が動く間、Restore が待たされる（Lock timeout で `ProjectBusyError`）ため。Task Service と Queue が Command を Project の行の `FOR SHARE` の下で実行する変更（4 の Gate と同じ。Issue #83）で閉じる。
6. **`tasks(project_id, state)` の Index。** `tasks.project_id` に Index がないため、Processor の一覧は Sequential Scan になる。Task 数が増えたら Task Lane で Index を足す（この Issue は他の Table を変えない）。

7. **Cancel → Restart → enqueue の競合で残る Queue の Entry（PAW-026 の 4 回目のレビュー）。** Processor は（この指摘の時点では）Task ごとに Queue の Entry を 1 回 Cancel してから Task の Cancel を発行していた（順序は 8 で逆にした）。その間に別の呼び出しが Task を Cancel → Restart し、新しい Attempt を enqueue すると、Processor 自身の Cancel が再開された Task を終わらせ、新しい Entry（queued / claimed）だけが終了済みの Task の後ろに残る。
   Task が終了しているため、Task を経由する一覧（2）は二度とその Entry を返さず、Processor は Task の状態だけを見て `processed_at` を書いてしまう。
   - **PAW-026 の範囲でしたこと:** (a) Task の停止の後に、Project の Task が持つ active な Entry（queued / claimed）を Project で引き（`queue_entries` と `tasks` の Join。最大 `batch_size` 件。6 回目のレビュー（8）で、対象を終了している Task の Entry に絞った）、`TaskQueue.cancel` で Cancel する。再実行でも同じ Sweep が、処理済みになった後に現れた Entry を拾う。Sweep は先に Project を読み直し、Pending deletion / Deleted でなければ何もしない。
     (b) `processed_at` は、active な Task が無いことに加え、Project の Task に active な Entry が無いことを、Project の行の `FOR SHARE` Lock の下で確認してから書く。残っていれば要求を開いたままにし（`done = False`）、次の実行が Cancel する。
     Test: `test_a_queue_entry_created_by_a_raced_restart_does_not_survive`、`test_an_entry_of_a_finished_task_is_found_by_project_on_a_rerun`、`test_the_request_stays_open_while_an_entry_appears_after_the_sweep`、`test_more_stray_entries_than_a_batch_need_several_runs`、`test_a_project_restored_meanwhile_keeps_its_entries_in_the_sweep`。
   - **変えないもの:** Queue の状態機械。Processor が使うのは既存の `TaskQueue.cancel` と、`queue_entries` / `tasks` の読み取りだけである。終了済みの Task の Entry を Cancel すると、その Entry をまだ持つ Worker は Lease を失う（`complete` が `LeaseLostError`）。Project は削除中で、Task の結果は Task の側に残るため、許容する。
   - **残る窓（Issue #83 の変更で閉じる）:** Sweep と確認の後、または `processed_at` の後の `enqueue` は、再実行でしか拾えない（Orchestrator が Pending deletion の Project にも通常の周期で `stop_project_tasks` を呼ぶ前提）。Sweep が Entry 1 件ごとに Project を読み直してから、その Cancel が終わるまでの間の Restore（1 回の Queue 操作の窓。5 と同じ）も残る。どちらも、4 の Gate（`create_task`、Retry、Restart に加えて `enqueue`）が Project の行の Lock と直列にすれば閉じる。この Issue は Queue Lane を変えない。

8. **Task の Cancel と Entry の Cancel は 1 組にならない（PAW-026 の 6 回目のレビュー）。** Task の Cancel（`TaskService`）と Entry の `cancel`（`TaskQueue`）は別の Commit で、間で Process が落ちる、Error になる、Stopper が Cancel される、Restore が Commit される、ことがある。Entry を先に取り消す実装では、Task が active（queued）のまま Entry だけを失い、その隙間に Restore が入ると、次の実行は復元済みの Project に何もせず要求を処理済みにするため、復元された Task は二度と自動では実行されない（Entry のない queued の Task は、enqueue 前の普通の状態でもあるため、後から見分けられない）。
   - **PAW-026 の範囲でしたこと:** (a) **順序を逆にした。** Task の Cancel を先に発行し、Task が終了してから Entry を Cancel する（Cancel が終わらせた、または元から終了していた。`TaskConflictError` で active のままなら Entry は取り消さない）。まだ active な Task は、Cancel の Error、競合、Stopper の Cancel、間の Restore のどれでも Entry を持ち続ける。残る隙間は「終了した Task と active な Entry」で、Project が削除中なら Sweep が拾う。
     (b) **中断を状態で整合させる（`_reconcile_entry`）。** Cancel か Entry の Cancel が何かを送出したら（Listener は Commit の後に動き、Cancel されると `execute` が Commit 済みのまま例外になる）、Task を 1 回読み、終了していれば Entry を Cancel してから元の Error を再送出する。まだ active な Task と Entry には触らない。中断の `except` の中で動き、`RECONCILE_TIMEOUT_S`（5 秒）で有界。整合の失敗と時間切れは元の Error を置き換えない。
     (c) **Sweep は、終了している Task の Entry だけを取り消す。** まだ active な Task の Entry は、次の実行が Cancel、Entry の順に止める（復元されたとき実行できる Entry を残す）。
     Test: `InterruptedTaskCancelTest`（`test_a_failing_cancel_after_a_restore_leaves_the_task_its_entry`、`test_a_conflict_after_a_restore_leaves_the_task_its_entry`、`test_a_cancelled_stopper_after_a_restore_leaves_the_task_its_entry`、`test_the_task_is_cancelled_before_its_queue_entry`、`test_an_interrupted_entry_cancel_is_reconciled_by_state`、`test_a_cancellation_after_the_commit_of_the_cancel_is_reconciled`、`test_a_failing_reconciliation_does_not_hide_the_original_error`、`test_the_reconciliation_of_a_cancelled_stopper_is_bounded` ほか）と `test_the_sweep_leaves_the_entry_of_a_task_that_is_still_active`。
   - **変えないもの:** Task Lane と Queue Lane（`TaskService.execute`、`TaskQueue.cancel` はそのまま）。`project_task_stops` に Task ごとの記録は足さない（Migration が要り、Task の状態から分かる）。
   - **残る窓（Issue #83 の Lane の変更で閉じる）:** (a) Stopper の Cancel と Entry の Cancel の間に、別の呼び出しが Task を Restart すると、再開された Task は Entry を失う（`TaskQueue.cancel` に条件がなく、確認と取消が 1 つの Transaction にならない）。Project が削除中なら次の実行が active な Task として止め、Restore も競合して初めて問題になる（`test_a_restart_between_the_cancel_and_the_entry_cancel_is_stopped_later`）。
     (b) 2 つの Commit の間で Process が落ちる（または整合が失敗する）うえに、次の実行の前に Restore が Commit されると、`cancelled` の Task に active な Entry が残る。Claim した Worker が `start` を拒否される、動かない Entry である。復元された Project では Stopper は何も触らない（完了する Worker は、完了した Task の claimed の Entry を一瞬持つため。生きている Project に触らない方針）。Task 自体は 5 の「1 件は止まる窓」と同じで、Restart できる。
     閉じるには、(i) Task の Command と Queue の操作を 1 つの Transaction で実行できるようにする（外部の Session を受け取る、または両方を行う 1 つの Command）、(ii) `TaskQueue.cancel` に「Task が終了しているとき」の条件を足す、(iii) Claim が終了済みの Task の Entry を飛ばして Cancel する、のどれかが要る。いずれも Task Lane または Queue Lane の変更で、4 の Gate と同じく Issue #83 で行う（Issue #83 の受け入れ条件は、(ii) 条件つきの `TaskQueue.cancel`、または (iii) 終端 Task の Entry を飛ばす Claim で窓を閉じること）。

## 選定理由

- 数値と選択は、要件が定める Lifecycle と Role を動かすための最小の仮置きで、数値は暫定値として承認し、実運用で見直す前提。
- 墓石を残すのは、Audit と他の領域のデータが指す ID を、削除後も「削除済みの Project」として解決できるようにするため。名前と説明は個人・業務の内容を含みうるため消す。
- 存在を明かさない応答は、UUID が推測できない前提でも、Project の存在や状態を非 Member に知らせないための多層防御。
- Outbox は、「停止を頼んだ」ことを Project の状態と同時に永続にし（Process が落ちても消えない）、停止の完了と未完了を区別できるようにするため。停止そのものを外の部品に置くのは、Task と Queue の状態を書く唯一の経路（`TaskService` / `TaskQueue`）を保つためで、Project の側が Task の状態を書く近道をしない。

## 代替案

- Delete 開始を Archived からだけにする: 誤操作の防止は強くなるが、要件にない制約で、Archive を強いる。
- Purge で Project の行も消す: 墓石が要らなくなるが、他の領域の `project_id` と Audit の ID が宙に浮く。
- 招待に期限を置かない: 古い招待が残り続ける。期限は Schema に書かず、`INVITE_TTL` で変えられる。
- Capability を今すぐ追加する: Audit できるが PAW-025 の Policy と、その網羅 Test の変更が要る。別 Issue [#82](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/82) で行う。
- Delete 開始の Transaction の中で Task Service を呼んで止める: Task Service は自分の Transaction を持ち、外部の Worker は止められず、失敗しても再実行できない。Project の Transaction に Task の書き込みを混ぜると、Lock の順序も Task Lane と衝突する。
- Outbox を持たず、Orchestrator が Pending deletion の Project を走査するだけにする: Processor は Project の状態から動くため成り立つが、停止が完了したかの記録がなく、30 日間ずっと全 Project を調べる。Outbox は完了と未完了を区別し、未処理だけを引ける（部分 Index）。ただし走査の併用は、Delete 開始の後に作られた Task を拾うために採る（上の 4。Orchestrator が Pending deletion の Project にも通常の周期で `stop_project_tasks` を呼ぶことは、PAW-034（[#30](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/30)）の受け入れ条件に追記した）。
- Stop Now、または running だけ Pause する: 上の 3 のとおり採らない。

## 承認後の扱い

2026-09-25 に承認された。PAW-026 のPRは本Decisionを参照する。
暫定値として承認した数値は、`paw_backend/projects/limits.py` の定数で変えられる。
承認後に方針を変える場合は、この Decision を書き換えず、新しい Decision から `Supersedes` する。
[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。

## 承認時の決定（2026-09-25）

- 本文の各点を、提案どおり承認した。
- Delete 開始は Active からもでき、Project 名の完全一致の入力を要求する。
- 削除待ちの復元は `project.lifecycle.manage` を持つ Manager・Owner・Admin（Decision 0004 と一致）で、復元先は Archived とする。
- 30 日後の消去は、Project の行を墓石として残し、Member と招待の行を削除する。
- 暫定値（招待の有効期間 14 日、Member と有効な招待の合計 200、Project 名 1〜100 文字・説明 2,000 文字）を承認した。いずれも暫定値で、後から変えられる。
- Membership のルールを承認した。
- Project の作成・招待の受諾 / 辞退・退出の Capability と Audit は、暫定の作り（Authorizer を通さず、Audit に残らない）で承認した。Capability の追加は Issue [#82](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/82) で行う。承認済みの Decision 0004 は書き換えず、新しい Decision から `Supersedes` する。
- 管理者向けの全 Project 一覧 API（`admin.projects.manage`）は Issue [#84](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/84) で実装する。
- 削除開始時の Task 停止は、Outbox 方式（停止の Command は Cancel）を承認した。
- Task / Queue 側に Project の状態を確かめる Gate を入れる方針を承認した。実装は Issue [#83](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/83)（PAW-034 の前後）で行い、PAW-026 では入れない。残る窓は本文（8）に文書化している。
  Orchestrator が削除待ちの Project にも `stop_project_tasks` を定期的に呼ぶことは、PAW-034（[#30](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/30)）の受け入れ条件に追記済みである。
- 削除時の調査結果（Provenance・Scratch など）の扱いは、Issue [#88](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/88) で決める。
