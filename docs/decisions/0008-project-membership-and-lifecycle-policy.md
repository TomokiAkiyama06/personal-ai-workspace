# Project の Membership と Lifecycle の方針

- Status: Proposed
- Date: 2026-09-24
- Scope: PAW-026（Project CRUD / Membership / Lifecycle）と、Project を使う以降の Issue（PAW-027 Repository、PAW-022 Session、Project Chat など）
- Supersedes: なし
- Approval: 未承認（Humanの承認待ち）

## 背景

[REQUIREMENTS.md](../../REQUIREMENTS.md) は、Project の Role（Manager / Contributor / Viewer）、招待制、Lifecycle（Active → Archived → Pending deletion → 30 日 → Deleted）、
Delete 開始時の確認操作、削除時に消すものを定めている。
[Decision 0004](0004-rbac-capability-and-audit-policy.md)（Proposed）は、Owner / Admin が Member でなくても持つ `project.lifecycle.manage` を定めている。
一方で、次の点は要件にも Decision 0004 にも書かれていない。PAW-026 の実装は動かすために仮の選択をした。
[AGENTS.md](../../AGENTS.md) の「仕様変更」に従い、その選択を一覧にして承認または変更を求める。

**この Decision は Proposed であり、Human の承認を得ていない。** 承認されるまで、次の選択は暫定である。
値のうち `INVITE_TTL` と `MAX_MEMBERS_PER_PROJECT` は `paw_backend/projects/limits.py` の定数で、変更しても Schema は変わらない。
承認された内容が変わる場合は、新しい Decision から `Supersedes` する。

実装は [Backend README](../../apps/backend/README.md) の「Project CRUD / Membership / Lifecycle」に書いている。
Delete 開始時の Task 停止（8）は、PAW-026 の独立レビューの指摘（`begin_deletion` が Project の行しか更新せず、実行中の Task を止めない）への対応で、同じ Decision に加えた。

## 提案

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
  GitHub、Local checkout などの外部資源は消さない（要件）。この分担は承認後に各 Issue へ引き継ぐ。

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

### 5. Capability を持たない操作（要判断）

Decision 0004 には、次の 4 つに対応する Capability がない。PAW-026 は PAW-025 の Policy を変更しないため、**Authorizer を通さず、次の本人確認だけで許可し、Audit の行を書かない**。

| 操作 | 許可の条件 |
| --- | --- |
| Project の作成 | `system_role` が Owner / Admin / User（`SYSTEM` は不可）。作成者が最初の Manager |
| 自分宛ての招待の受諾・辞退 | 同上。Actor 自身の行だけを対象にする（他人の招待、招待のない Project は `InviteNotFoundError`） |
| Project からの退出 | 同上。Actor 自身の Member の行だけを対象にする |

**提案:** これらを Audit できるように、Capability（たとえば `project.create`（`Scope.SYSTEM`、User 以上）、`project.invitation.respond`、`project.leave`）を Decision 0004 の後継 Decision で追加する。
承認されるまで、これらの操作は Audit に残らない。Project の作成は誰でもできる（要件に「作成できる人」の制限がないため）。

### 6. 一覧

- `list_projects` は、**自分の Member の行がある Project だけ**を返す（招待は含まない。Owner / Admin も同じ）。通常の一覧は Active、Archived は別の一覧（要件）。
  Pending deletion の一覧は、復元できる **Manager の Project だけ**を返す。
- **Owner / Admin が全 Project を一覧する API は、この Issue にない。** 管理上の操作（Archive、復元）は Project の ID を知っていれば Member でなくてもできるが、ID を探す手段は別の Issue（`admin.projects.manage`）で要る。
- Principal の `project_roles` を作るための `ProjectService.roles_of(user_id)` を持つ。ただし Service 自身は、渡された Principal の `project_roles` を**信用せず**、判定のたびに DB から Actor の Role を読み直す
  （Member から外された直後の古い Principal が、古い Role で操作できないようにする）。

### 7. `Scope.SELF` の Capability

`chat.use`、`memory.use` などは、Decision 0004 のとおり Project の状態と Member 資格を見ない。この Issue は Membership の Table と `roles_of` を用意するだけで、これらの Capability の判定は変えない
（Project の Chat、Project の Memory を実装する Issue が、`roles_of` と Project の状態を使って絞る）。

### 8. Delete 開始時の Task 停止（要判断）

要件の「Pending deletion」は、開始時点で「実行中Taskをsafe-stop」「新規Agent Task停止」を定める。どの Command で止めるか、どの部品が行うか、Delete 開始の後に作られる Task をどうするかは、要件にも Decision 0004 にも書かれていない。
PAW-026 のレビューで、`begin_deletion` が Project の行を更新するだけで Task を止めない点が指摘されたため、次の選択をした。

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
   - **別 Lane の変更の提案（承認が要る）:** Task Lane（PAW-032 / PAW-034）の `create_task`、Retry、Restart が、Insert（状態の変更）と同じ Transaction で Project の行を `SELECT ... FOR SHARE` で Lock し、Active 以外なら拒否する。Delete 開始（`FOR UPDATE`）と直列になり、競合が閉じる。
     `tasks` の Module が `projects` を import しないよう、Project の状態を返す Gate（Protocol）を注入する形を勧める。Database の Trigger で拒否する案は、別 Lane の Table を変えること、Task Lane の Test が存在しない Project の ID を使うことから、この Decision では採らない。
5. **Restore との競合。** Processor が Project の状態を読んだ後、Task の Command の前に Restore が Commit されると、復元された Project の Task を 1 件止めることがある（Restart できる）。窓は 1 つの Command の間だけで、閉じるには Task Service が Project の行と Transaction を共有する必要があり、採らない。
6. **`tasks(project_id, state)` の Index。** `tasks.project_id` に Index がないため、Processor の一覧は Sequential Scan になる。Task 数が増えたら Task Lane で Index を足す（この Issue は他の Table を変えない）。

## 選定理由

- 数値と選択は、要件が定める Lifecycle と Role を動かすための最小の仮置きで、実運用で見直す前提。
- 墓石を残すのは、Audit と他の領域のデータが指す ID を、削除後も「削除済みの Project」として解決できるようにするため。名前と説明は個人・業務の内容を含みうるため消す。
- 存在を明かさない応答は、UUID が推測できない前提でも、Project の存在や状態を非 Member に知らせないための多層防御。
- Outbox は、「停止を頼んだ」ことを Project の状態と同時に永続にし（Process が落ちても消えない）、停止の完了と未完了を区別できるようにするため。停止そのものを外の部品に置くのは、Task と Queue の状態を書く唯一の経路（`TaskService` / `TaskQueue`）を保つためで、Project の側が Task の状態を書く近道をしない。

## 代替案

- Delete 開始を Archived からだけにする: 誤操作の防止は強くなるが、要件にない制約で、Archive を強いる。
- Purge で Project の行も消す: 墓石が要らなくなるが、他の領域の `project_id` と Audit の ID が宙に浮く。
- 招待に期限を置かない: 古い招待が残り続ける。期限は Schema に書かず、`INVITE_TTL` で変えられる。
- Capability を今すぐ追加する: Audit できるが PAW-025 の Policy と、その網羅 Test の変更が要る。承認後の別の変更にした。
- Delete 開始の Transaction の中で Task Service を呼んで止める: Task Service は自分の Transaction を持ち、外部の Worker は止められず、失敗しても再実行できない。Project の Transaction に Task の書き込みを混ぜると、Lock の順序も Task Lane と衝突する。
- Outbox を持たず、Orchestrator が Pending deletion の Project を走査するだけにする: Processor は Project の状態から動くため成り立つが、停止が完了したかの記録がなく、30 日間ずっと全 Project を調べる。Outbox は完了と未完了を区別し、未処理だけを引ける（部分 Index）。ただし走査の併用は、Delete 開始の後に作られた Task を拾うために勧める（上の 4）。
- Stop Now、または running だけ Pause する: 上の 3 のとおり採らない。
