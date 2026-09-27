# Working Set の Capability の付与・委任、作成時の Working Set、Revision 0085 より前の Task の扱い

- Status: Proposed
- Date: 2026-09-28
- Scope: Issue [#85](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/85)（Task Working Set の永続化、PR #124）の実装で、[Decision 0030](0030-task-working-set-model.md) と #85 の必須の制約が決めていない点。#85 の制約 3（「新しい判断が要る場合は Decision で提案する」）と、PR #124 に対する独立 Review（Claude）の指摘による
- Supersedes: なし（[Decision 0030](0030-task-working-set-model.md)・[Decision 0014](0014-task-working-set-persistence.md)・[Decision 0004](0004-rbac-capability-and-audit-policy.md) は書き換えない。0030 が #85 に送った点を埋める）
- Approval: 未承認（Human の承認を待つ。承認されるまで、この Decision の推奨は方針として使わない）

## 背景

Decision 0030 の 3 節は、Working Set の変更を新しい Capability `project.task.working_set.manage`（Scope.PROJECT）で判定すると決めた。その版に対する Codex Review の P1 の 1 つ（#85 の制約 3）は、**どの Project Role にこの Capability を与えるか**と、**`CAPABILITIES` の `delegable` をどうするか**を、#85 が勝手に決めてよい細部ではない（重要な Security Policy の選択）と指摘し、#85 に「決めること。新しい判断が要る場合は Decision で提案する」ことを求めた。

PR #124 の最初の版は、この 2 点を README で「0030 の設計から導いた実装上の選択」として決め、Decision を立てなかった。独立 Review（Claude）は、これが AGENTS.md 1.8 / 14 の「仕様にない重要判断を勝手に確定しない」に当たると指摘した。あわせて、次の 3 点も Human の確認が要ると指摘した。

- `create_task(repositories=...)` が作成時の `working` / `target` を、3 節の Approval Level と Project・Repository の AND 判定を通さずに書くこと（`TaskService` のほかの Command と同じく、認可は呼び出し側の API 層に任せている）。
- Working Set が空（`target` がない）Task の作成を認めること。
- Migration 0085 が、既存の試行の worktree / Review / PR の状態と `tasks.starting_commit` を Repo に割り当てられないこと（最初の版はそれを捨てていた）。

PR #124 は、実装を動かすために下の推奨どおりの値をすでに置いている。値は `authz/policy.py` の付与の集合、`authz/capabilities.py` の 1 行、`TaskService` の検査だけで、承認で変わっても Schema は変わらない。

## 提案

### 1. `project.task.working_set.manage` の付与先: Project の Contributor と Manager

**推奨: Contributor と Manager に与え、Viewer には与えない。** 付与先を、Task を実行して Repo に書き込める Role（`project.task.run` / `project.repo.write` を持つ Role）にそろえる。

根拠:

- Working Set は「この Task が何に触れてよいか」の宣言で（0030 の 1 節）、Task を実行できない Viewer が変える理由がない。
- この Capability だけでは Repo に対して何もできない。0030 の 3.4 節のとおり、対象 Repo の Repository 資源に対する代理 Capability（`referenced` の追加は `project.read`、`working` / `target` は `project.repo.write`）も必要で（AND）、Repo の ACL で READ / WRITE を拒否された者はその Repo の行を変えられない。
- Manager だけに絞る案は、日常の Task で Repo を 1 つ参照に加えるたびに Manager が要ることになり、要件の「Read 範囲は比較的広く取ってよい」と合わない。

検討した他の案: Manager だけ（上記の理由で却下）。全 Member（Viewer を含む）（Task を実行できない Role に Task の範囲を変えさせる理由がないので却下）。

### 2. 委任: `delegable=True`

**推奨: 委任可能にする。** この Capability を宣言した初めての委任可能な `project.*.manage` になる。

根拠:

- 0030 の 3 節の変更の経路は、Agent が呼ぶ Working Set の Tool（`task.working_set.*`）である。既存の `*.manage` と同じく委任不可にすると、`referenced` の追加（`SCOPED_AUTO`）を含むすべての呼び出しが Approval の前に拒否され、0030 の設計が動かない。
- 委任しても、Agent が Repo に対してできることは広がらない。変更ごとに対象 Repo の代理 Capability（それ自体も Agent Grant と委任者の権限の積で判定される）が要り、`referenced` の追加以外（`working` / `target` への追加・昇格、降格、削除）はすべて Step-up つきの `STRONG_APPROVAL`（Human）である。
- 委任で Agent が Human なしにできるのは、**委任者が READ できる、Task の Scope 内の Project に登録済みの Repo を `referenced` として加えること**だけである（読み取り範囲が広がる）。要件の「Read 範囲は比較的広く取ってよい」と 0030 の `SCOPED_AUTO` の判断の範囲内と考える。

確認してほしい影響: Contributor が Agent に与える Grant にこの Capability が入ると、その Agent は Human の操作なしに Task の**読み取り**範囲を広げられる（Step-up は要らない）。これを許さない場合は、`delegable=False` にして Working Set の変更を Human の UI / API だけにするか（0030 の Agent の Tool 経路は使えなくなる）、`add_referenced` も `APPROVAL` 以上にする（0030 の 3 節の変更になるので、別の Decision で 0030 を Supersede する）。

### 3. 作成時の Working Set

**推奨: 作成時の Working Set は `change_working_set` の「追加」として扱わない。** `create_task(repositories=...)` は、Human が API から Task を作るときの一部として、API 層が作成者の `project.task.run` と各 Repo の ACL（`referenced` は READ、`working` / `target` は WRITE）を判定してから呼ぶ（`TaskService` は、ほかの Command と同じく認可しない）。作成そのものが Human の明示的な操作なので、`working` / `target` を作成時に置くことに追加の `STRONG_APPROVAL` は求めない。

- **Orchestrator（PAW-034）が作る Sub-task**: 親 Task の Working Set の部分集合（同じ Repo、同じか弱い役割）だけを持てる。親にない Repo や、親より強い役割を求める Node は Escalation せず失敗させる（Decision 0021 §2 の `ScopeEscalation` と同じ）。親にない Repo が要るなら、親の Working Set を 0030 の 3 節の手続きで変える。作成時の `added_by` の Actor の種類（今は常に作成者の `user`）を Orchestrator の場合に `system` にするかは、PAW-034 の実装で決める。
- **`target` のない作成**: 認める。`target` のない Task は `running` にならない（Start / Resume / Unblock が `NoTargetRepositoryError`）ので、0030 の 2 節の不変条件（`target` が 0 件のまま `running` にしない）は保たれる。作成後に `change_working_set` で `target` を加える経路（UI で Repo を後から選ぶ）を残すためである。

検討した他の案: 作成時の `working` / `target` も `STRONG_APPROVAL` にする（Single-Repo の通常の Task を作るたびに Step-up が要り、要件の「通常は Single-Repo Task」と合わないので却下）。`target` のない作成を拒否する（上の経路がなくなる。不変条件は `running` への遷移で守れるので却下）。

### 4. Revision 0085 より前の Task

**推奨: 既存の状態は捨てずに退避し、Repo には割り当てない。既存の Task は Working Set が空として扱い、Human が `target` を与えるまで動かさない。**

- 0085 以前の試行は、worktree / Review / PR の状態を 1 組だけ持ち、どの Repo の状態かを記録していない。どの Repo に割り当てても推測になるので、Upgrade は割り当てず、**列を削除する前に `task_attempt_state_archive` へすべて写す**（Task の `starting_commit` も）。Application の Role にはこの表の権限を与えない（Operator が参照するための表）。Downgrade は、Repo のない試行の退避した値を列へ書き戻す。
- 既存の Task は Working Set が空なので、`running` にならず、Complete できず、Repository に触れる呼び出しはすべて `repository_role_unresolved` で拒否される（0030 の 4.5 節の fail-closed）。実行中・待機中・一時停止中の Task は、Operator / Human が `change_working_set` で `target` を与える（退避した状態を見て、その Task が扱っていた Repo を選ぶ）まで止まる。
- 退避した PR の番号・URL・状態は、新しい表（`task_attempt_repositories`）へは自動では写さない。写すと、Human が選んだ Repo と PR の対応を Backend が推測することになるためである。

検討した他の案: Project に登録された Repo が 1 つだけなら、それに割り当てる（推測を含み、Project に後から Repo が加わった場合に誤るので却下）。状態を捨てる（最初の版。復旧の手がかりがなくなるので却下）。

### 5. 許可済みの書き込みが実行中の間の Task の Command

PR #124 に対する Codex Review は、Broker が書き込み（または `execute`）を許可してから Executor が書き込むまでの間に、Repo の降格・削除がまだ clean な Repo を見て義務を外せる、と指摘した。PR #124 は、許可した書き込みを `task_repository_writes` の予約として、Executor が終わるまで（落ちた Executor の場合は Runner の最長実行時間より長い期限まで）残す。その間の降格・削除は拒否する（0030 の 3 節の「破棄を確かめる」を、確かめられる状態になるまで待つだけで、0030 の意味は変えない）。同じ間に Task の各 Command をどう扱うかは 0030 / 0014 が決めていないので、次を提案する。

**推奨（PR #124 に実装済み）:**

- **Begin evaluation と Complete は拒否する**（`RepositoryWriteInFlightError`。何も書かない。呼び出し側は、呼び出しが終わってからもう一度頼む）。どちらも Repo を今の状態で判定する Command で、まだ変わり得る Repo を判定しない。どの試行・どの実行の予約でも拒否する（Retry は同じ worktree で続けるので、前の実行の Executor の書き込みも同じ試行に届く）。
- **Stop Now・Fail・Cancel・Pause などは止めない。** 停止はいつでもできなければならない。代わりに、Event の `detail["writes_in_flight"]`（Repo、試行、Retry 回数）に残し、Stop Now の Task log にも「許可済みの書き込みがまだ実行中かもしれない」と書く。
- **Retry・Restart は新しい実行を始める。** 前の実行は、予約を取ることも、試行の状態を書くことも（`StaleRunError` / `StaleAttemptError`）できない。前の実行の予約の解放は、新しい実行の状態を変えない。Restart の新しい試行は 0030 の 1 節どおり新しい状態から始める。その試行の Begin evaluation / Complete は、前の実行の予約が解放されるか期限が切れるまで上と同じく待つ。
- **終わった Task（Completed・Failed・Cancelled。Stop Now を含む）には、書き込み・実行をもう許可しない**（`TaskNotActiveError`、Broker では `task_not_active`）。読み取りは許す。Complete の後や停止の後に書き込みが入らないようにするためである。

検討した他の案:

- Begin evaluation / Complete を**待たせる**（Lock を持ったまま解放を待つ）。Executor が長く動くと Task 行の Lock を持ち続けて、停止も妨げるので却下。
- 現在の試行の予約だけで判定する。Retry は同じ worktree を使うので却下。
- **落ちた Executor の予約を Human / Operator が解放できるようにする。** 今は、期限（約 24 時間）まで Begin evaluation / Complete と、その Repo の降格・削除ができない。Executor が確実に止まったと Human が確かめたときに解放する操作（Audit つき）を加えるかどうかは、この Decision では決めず、必要になったら別の Issue にする。期限を Runner ごとの実行時間に合わせて短くする案も同じ扱いにする。
- Graceful な Cancel の後も、Worker が今の Step を終えるための書き込みを許す。Cancel は Task を終わらせる Command なので、fail-closed の側（許さない）を選ぶ。

## 影響

- 1・2 が承認されれば、PR #124 の `authz/policy.py`（`_CONTRIBUTOR_ONLY`）と `authz/capabilities.py`（`delegable=True`）はそのまま方針になる。変更されれば、その 2 か所と `tests/test_authz_policy.py`・`tests/test_tools_working_set.py` の表を変える。
- 3 は PAW-034（PR #106、Decision 0021）の Sub-task の作成に制約を加える（親の Working Set の部分集合だけ）。PR #106 は `TaskScope.repositories`（と `excluded_repositories`）を親から絞る形なので、役割も親から絞ればよい。
- 4 は Migration 0085 と Backend README（「Working Set（Issue #85）」の Migration）に書いてある。0085 を適用する前に実行中の Task を終えておく運用も取れる。

## 承認後の扱い

承認されたら Status を Approved に改め、`Approval` に日付と承認の様子を記録する。承認後に方針を変える場合は、この Decision を書き換えず、新しい Decision から `Supersedes` する。

## 決めてほしいこと

1. **`project.task.working_set.manage` を Project の Contributor と Manager に与え、Viewer には与えない**（1 節）でよいか。推奨: はい。
2. **`project.task.working_set.manage` を委任可能（`delegable=True`）にする。これにより Agent は、委任者が READ できる Scope 内の登録済み Repo を、Human なしに `referenced` として加えられる**（2 節）でよいか。推奨: はい。
3. **作成時の Working Set は API 層の作成者の認可（`project.task.run` と各 Repo の ACL）で足り、追加の `STRONG_APPROVAL` は求めない。Orchestrator の Sub-task は親の Working Set の部分集合だけを持てる。`target` のない作成は認め、`target` がない間は `running` にしない**（3 節）でよいか。推奨: はい。
4. **Revision 0085 より前の試行の状態と `starting_commit` は `task_attempt_state_archive` に退避して Repo に割り当てず、既存の Task は `target` を与えられるまで動かさない**（4 節）でよいか。推奨: はい。
5. **許可済みの書き込みが実行中の間は、Begin evaluation と Complete を拒否し、停止系の Command と Retry / Restart は止めずに記録する。終わった Task には書き込み・実行を許可しない。落ちた Executor の予約は期限まで残す（Human が解放する操作は今は作らない）**（5 節）でよいか。推奨: はい。
