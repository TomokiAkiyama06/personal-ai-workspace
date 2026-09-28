# 本番の Task 実行の組み立て（Composition Root）と、Task 終了時・定期実行の後処理

- Status: Proposed（判断が必要な点 1〜7 は承認待ち）
- Date: 2026-09-28
- Scope: Issue [#125](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/125)。本番の `TaskAuthority`、App の起動時の `TaskService` / Tool Broker / Orchestrator の組み立て、Task の終了（Complete / Fail / Cancel）での `FreshnessMaintenance.end_task` と承認の取り消し、鮮度の Job の定期実行（`paw_backend/orchestrator/authority.py`、`task_end.py`、`freshness_loop.py`、`composition.py`、`paw_backend/app.py`）
- Supersedes: なし。[Decision 0006](0006-tool-broker-policy.md) の 9 節（Task の終了で承認を取り消す）、[Decision 0034](0034-memory-versioning-freshness.md) の 7 節（Job の定期実行と `end_task` の配線を後の Issue にする）、[Decision 0030](0030-task-working-set-model.md)（保存された Working Set から役割を作る）が後続に回した配線の実装で、どれも書き換えない
- Approval: 未承認

## 背景

main には本番用の `TaskAuthority` がなく（`parent_scope` は `NotImplementedError`）、本番の Code で `TaskService` を組み立てる場所は Project 削除の Sweep（`build_project_stop_loop`）だけだった。そのため、Decision 0034 の `end_task`（Task から来た `session_only` の Memory を退役させる）を呼ぶ場所がなく、鮮度の Job（期限切れ・要再確認）を定期的に呼ぶ仕組みもなかった。PR #124（#85）の注記どおり、本番の `TaskAuthority` は保存された Working Set から `with_working_set_roles` で役割を作らないと、Repository への呼び出しがすべて拒否される。

Issue #125 は「Task の終了で `end_task` と `revoke_on_task_end` を呼ぶ（同じ Transaction か、再実行できる後処理にする）」と、どちらにするかを決めていない。途中で Process が再起動したときに何が走るかも決まっていない。ほかにも、要件と既存の Decision が決めていない選択がいくつかある（下の判断が必要な点）。実装は下の推奨どおりの形で置いてあり、承認されない点は実装を変える。

## 提案（実装済みの形）

### 1. Task 終了の後処理は遷移の Transaction の外で行い、残りを保存された状態から見つけて再実行する

- **Commit の直後**: 本番の `TaskService` の Listener は `TaskEndCleanup.on_task_event` の 1 つだけ。終了状態（`completed` / `failed` / `cancelled`）への遷移で、承認の取り消し（`ApprovalService.revoke_task`）と `session_only` の退役（`FreshnessMaintenance.end_task` を、1 回の Batch に満たなくなるまで最大 20 回・最長 10 秒。`TaskService` は Listener を待つので、止まった DB が Cancel を止めないため。承認の取り消しは自分の Deadline を持つ）を行う。片方が失敗しても、もう片方は行う。失敗は型名だけを Log に出し、遷移は Commit されたまま（`TaskService` がすべての Listener にしていることと同じ）。終了状態**からの**遷移（Retry / Restart で開き直した）では、承認だけを取り消す（`revoke_on_task_end` と同じ規則。退役した Memory は戻さない）。これまでの `approvals.revoke_on_task_end` の Listener は、この 1 つに含めた（同じ取り消しを 2 回呼ばないため）。
- **再実行できる後処理（Sweep）**: `TaskEndResidue` が、保存された状態から「終わったのに後処理が残っている Task」を探す。
  - 開いた（`pending` / `approved`）まま期限の来ていない承認を持つ、終了状態の Task
  - `active` の `session_only` の Version の `task` Source（`source_ref = tasks.id::text`）が指す、終了状態の Task

  見つけた Task（1 回に最大 100）に同じ後処理を行う。両方とも冪等で、2 回走っても何も変わらない。**「後処理が必要」という印は記録しない。残っているもの自体が記録**である。
- Sweep は下の 6 の定期 Loop が走らせる。

### 2. 後処理の途中で Process が再起動したとき

特別な復旧処理は持たない。Commit と Listener の間で Process が落ちた、Listener の途中で落ちた、Store が失敗した、人が編集中の Version が `SKIP LOCKED` で飛ばされた、のどれでも、残ったもの（開いた承認、`active` の `session_only`）を次の Sweep が見つけて終わらせる。起動後の最初の周期は 60 秒後（間隔がそれより短ければ間隔）。

Sweep までの間も、残ったものは使えない: Tool Broker は動けない Task の承認を開かず使わず（`tools/task_state.py`、Decision 0006 の 9）、Retrieval は `session_only` を返さない（Decision 0034）。

### 3. 本番の `TaskAuthority`（`StoredTaskAuthority`）の Scope

呼び出しごとに Database から作り直し、何も保持しない。

- **Working Set は毎回 `TaskService.restore` で読み直す**（Run の開始時の Snapshot を使わない）。役割は保存された Working Set のもの（`with_working_set_roles`）。
- **各 Repository** は `RepositoryService.working_set_acl`（Project と ACL）と `RepositoryService.scope_entries`（委任した User 自身の `ready` の Checkout: Root、保存された ACL、登録された Remote）で解決する。
  - 登録がない・Project が Deleted・委任した User の `ready` の Checkout がない・Linux Account が使えない Repository は **Scope から外す**（どの呼び出しもその Repository を名指しできず、その Worktree はどの Path Root の下にもない）。
  - Root が変わった Checkout（`CheckoutChangedError`）、Checkout が多すぎる、DB の失敗などは例外のまま伝え、その呼び出しを失敗させる（Fail closed）。
- `scope_entries` が返す**他の Checkout**（Worktree を囲む、または中にある同じ User の Checkout。Decision 0006 の 8(b)）は、Scope に入った Repository でなければ `excluded_repositories` にする。上で外した Working Set の Repository が他の Repository の Worktree の中にある場合も含む（その Repository の ACL を尋ねずに、他の Root から届かないようにするため）。
- `path_roots` は Scope に入った Checkout の Root（`target` を先に。相対 Path は `target` で解決する）、`hosts` はそれらの登録された Remote の Host、`projects` は Task の Project と各 Repository の Project の**現在の**状態（Deleted は除く）。
- **`credential_handles` は空**。保存された Credential を Task に結び付ける仕組みがまだないため（Fail closed）。

### 4. 親の Grant

- `AgentGrant(agent_id, capabilities, project_ids)`。
- `agent_id` は Task と Run（試行・Retry の回数）から導く（同じ Run は同じ親、別の Run は別の親。Node の Agent の `scope.agent_id_of` と同じ考え方）。
- `capabilities` は Node の Role の上限（`ROLE_CEILING`）の和: `project.read`、`project.task.run`、`project.repo.write`。Node が受け取れる最大と同じで、委任できない Capability は含まない。`project.task.working_set.manage` は含めない（Agent に Working Set を変えさせるかは、Agent の Runtime を入れる Issue で決める）。
- `project_ids` は Scope の Project。
- Grant は委任した User を広げない: 判定のたびに Authorizer が User の現在の権限と交わりを取る（`decide_agent`）。

### 5. 組み立ての範囲

`create_app` は DB が設定されているとき `build_task_execution` で次を 1 回組み立て、`app.state.task_execution` に置く。

- `TaskService`（`ProjectStateGate`、上の Listener）、`TaskQueue`、`BudgetTracker`、`ApprovalService`、`FreshnessMaintenance`、`TaskEndCleanup`
- `StoredTaskAuthority`（`RepositoryService` の Backend 内部の読み取りを使う。Git を実行しないので、Git の Runner は既定の `SubprocessGitRunner`）
- Tool Broker（App の Authorizer、`TrackerBudgetProvider`、`PostgresTaskActivity`、Working Set の登録と Repository 利用の Gate）と `ToolRunner`。Registry は **Working Set の Tool だけ**（`WORKING_SET_TOOL_SPECS` と `WorkingSetExecutor`）。File・Git・Network の Tool は Backend に Executor がまだなく、登録されていない Tool は拒否される
- `Orchestrator` は **Agent の Runtime が渡されたときだけ**組み立てる（`create_app(agent_runtimes=..., orchestrator_config=...)`）。Backend には本番の `AgentRuntime` がまだない。Queue を Claim する Worker（`Orchestrator.serve`）は起動しない
- Project 削除の Sweep は、この `TaskService` を使う（止めた Task の後処理も同じ Listener が行う）

### 6. 定期実行

1 つの Loop（`FreshnessJobLoop`、`connection_reaper` と同じ形）が、1 周期に次の順で走らせる。

1. Task 終了の Sweep（1 の再実行できる後処理）
2. `FreshnessMaintenance.mark_revalidation_due`
3. `FreshnessMaintenance.expire_due`

各 Job は 1 回の Batch いっぱいを変えた間だけ繰り返す（最大 20 回）。1 つの段の失敗は他を止めない。間隔は `PAW_FRESHNESS_JOB_INTERVAL_SECONDS`（既定 3,600。0 で停止、それ以外は 60〜86,400）。複数の Backend Process がそれぞれ走らせてよい（行は `SKIP LOCKED`、変更は 1 回だけ、後処理は冪等）。

含めないもの:

- `end_session`: Session の終了を記録する場所がまだなく、見つける対象がない
- `mark_triggered`・`mark_repo_head`: Event（Member・Model の変更、新しい Head）に応じて呼ぶもので、周期で呼ぶものではない

## 選定理由

- **Transaction の外にした**のは、承認の Store が Abortable な接続と自分の Deadline で動き（Decision 0006 の 4 / 9）、`FreshnessMaintenance` も自分の Transaction で動くため。同じ Transaction に入れると両方の Interface を変え、遅い Store が Cancel を止めるか Rollback させることになる。Cancel はいつでも効く必要がある。
- **印を記録せず残りから探す**のは、Outbox の Table（Migration）を足さずに、どの途中の失敗でも同じ 1 つの経路で終わらせられるため。残りを探す Query の対象は小さい（開いた承認は最大 24 時間で期限切れになる。`session_only` は部分 Index `ix_memory_versions_freshness_due` の対象）。
- **Scope から外す**（例外にしない）のは、Checkout を作っていない参照用の Repository 1 つで Task 全体を止めないため。外した Repository には何も届かない（名指しもできず、Root もなく、他の Root の下なら除外される）ので、Fail closed は保たれる。
- **Grant を Role の上限の和にした**のは、User の現在の権限との交わりを Authorizer が判定のたびに取るため、ここで User の Role を読んでも同じ結果を 2 回計算するだけになるから。

## 代替案

- **同じ Transaction**: 承認と Memory の変更が遷移と原子的になるが、上の理由（Interface の変更、遅い Store が Cancel を止める）で却下。
- **Outbox の Table**（遷移の Transaction で「後処理が必要」を記録し、Worker が消化する）: 後処理の対象を正確に持てるが、Migration と Worker が増え、残ったもの自体を探せば同じことができる。
- **Listener だけ（再実行なし）**: 途中の失敗で承認と `session_only` が残り続ける。Issue の受け入れ条件（再実行できる後処理）を満たさない。
- **Sweep を別の Loop・別の間隔にする**: 後処理を早く終わらせられるが、残ったものは Sweep までの間も使えない（2 節）ので、設定を 1 つ増やす理由が弱い。
- **親の Grant を User の現在の Project Role の Capability にする**: 同じ結果を Authorizer と二重に計算する。

## リスク

- 後処理の Sweep は既定で 1 時間ごとなので、途中で失敗した Task の承認と `session_only` が、最大でその間 `revoked` / `deprecated` にならない（使えないことは 2 節のとおり）。Memory UI には `active` のまま見える。
- `credential_handles` が空なので、Credential を要する Tool は、Credential の仕組みができるまで本番では使えない。
- #126（Decision 0046、Tool 呼び出しの Lease の Fencing）が Merge されたら、`build_tool_broker` に `lease=QueueLeaseVerifier(queue)` を足す必要がある。足さないと Broker は全ての呼び出しを `lease_unavailable` で拒否する（Fail closed）。

## 判断が必要な点（未承認。推奨つき）

1. **Task 終了の後処理の Transaction の境界**: 遷移の Transaction の外で、Commit の直後の Listener と、保存された状態から残りを見つける Sweep の 2 段にする（1 節）。推奨: この形。代わりに同じ Transaction（Store の Interface の変更）か Outbox の Table（Migration）。
2. **途中で再起動したとき**: 特別な復旧を持たず、次の Sweep が残りを終わらせる（2 節。起動後の最初の周期は 60 秒後）。推奨: この形。
3. **Scope の作り方**（3 節）: Working Set を毎回読み直す、使えない Repository は Scope から外す、変わった Checkout は呼び出しを失敗させる、他の Checkout は除外、`credential_handles` は空。推奨: この形。
4. **親の Grant**（4 節）: Node の Role の上限の和。`project.task.working_set.manage` を含めない。推奨: この形（Working Set を Agent に変えさせるかは Runtime の Issue で決める）。
5. **組み立ての範囲**（5 節）: Orchestrator は Agent の Runtime が渡されたときだけ組み立て、Worker（`serve`）は起動しない。Broker の Registry は Working Set の Tool だけ。推奨: この形（Runtime と Executor の Issue で広げる）。
6. **定期実行の中身と間隔**（6 節）: Sweep・要再確認・期限切れを 1 つの Loop で、既定 1 時間（0 で停止、60〜86,400 秒）。`end_session` と Event 駆動の Job は含めない。推奨: この形。
7. **#126 との統合**: #126 が先に Merge されたら、この PR の統合時に `build_tool_broker` へ Lease の検査（`QueueLeaseVerifier`）を足す。推奨: そうする（この Decision の承認とは独立に、統合の手順として行う）。

## 承認後の扱い

判断が必要な点が承認されたら、`Approval` に記録し、Status を Approved に改める。承認されない点は実装を変え、新しい Decision を作らずにこの Decision を承認前に改める（承認後に方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する）。
[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。
