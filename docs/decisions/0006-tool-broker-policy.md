# Tool Broker の Policy・承認・Credential の方針

- Status: Proposed
- Date: 2026-09-24
- Scope: PAW-031（Tool Broker / Capability Policy）と、Tool を使う以降の Issue（PAW-023 / 030 / 033 / 034 など）
- Supersedes: なし
- Approval: 未承認（Humanの承認待ち）

## 背景

[Security / Tool Permissions](../SECURITY_TOOL_PERMISSIONS.md) と [REQUIREMENTS.md](../../REQUIREMENTS.md) の
「Tool Broker / Capability Policy / Secret Isolation」「Tool approval boundary」は、Capability の 6 つの class、5 つの Approval level、
Credential の Plaintext を Agent に渡さないこと、Backend が最終判定することを定める。
一方、次のことは文書だけでは決まらず、PAW-031 の実装が選んだ。独立 Review もこれらを問題として指摘した。
承認前の Product Policy を実装が暗黙に確定させないよう、選択を一覧にして Human が承認または変更できるようにする。

実装は [Backend README](../../apps/backend/README.md) の「Tool Broker / Capability Policy」にある。
**この Decision は Proposed であり、Human の承認を得ていない。** 承認されるまで、実装の選択は暫定である。
[Decision 0004](0004-rbac-capability-and-audit-policy.md)（Proposed）が PAW-031 に残した「Tool Broker の Approval で委任不可の操作を許す仕組み」もここで決める。

## 提案

### 1. Approval は Level を上げるだけで、権限を広げない

1. Tool の呼び出しは、まず PAW-025 の認可（委任元 User の権限と Agent の Grant の積集合）を通る。Broker の Level はその上に足すだけで、置き換えも拡張もしない。
2. Approval で、委任できない Capability（`admin.*`、`owner.*`、Project の設定・Member・Agent Policy・Lifecycle など）の操作を Agent に許す仕組みは作らない。承認があっても `authz_denied` のまま。
3. Approval は認可・Scope・Budget を使うときにもう一度確認する。承認の後に権限が減れば、使えない（承認は消費されない）。

### 2. Policy の表（Capability class × Environment × Scope）

`DEFAULT_TOOL_POLICY` は 36 マス。表にない組み合わせは `DENY`（既定拒否）。Tool の Level は class ごとの Level のうち最も厳しいもの。
`ToolSpec.min_level` は Level を上げるだけ。Task Scope の範囲外（Path / Project / Credential）は Policy で許可できない。

| Capability | 範囲内 | Task の Host の外 | 範囲外（Path 等） | Host 全体の環境（範囲内） |
| --- | --- | --- | --- | --- |
| `read` | `AUTO` | `APPROVAL` | `DENY` | `AUTO` |
| `write` | `SCOPED_AUTO` | `APPROVAL` | `DENY` | `APPROVAL` |
| `execute` | `SCOPED_AUTO` | `DENY` | `DENY` | `APPROVAL` |
| `network` | `SCOPED_AUTO` | `APPROVAL` | `DENY` | `APPROVAL` |
| `credential-use` | `SCOPED_AUTO` | `DENY` | `DENY` | `STRONG_APPROVAL` |
| `destructive` | `APPROVAL` | `DENY` | `DENY` | `STRONG_APPROVAL` |

要件の表との違い（Human に判断を求める）:

- **厳しい方向**: build / test / lint（`execute`）は `AUTO` でなく `SCOPED_AUTO`（挙動は同じ: 人の確認なし）。Task Scope 内の一時ファイルの削除は `destructive` なので `APPROVAL`
  （要件は `SCOPED_AUTO`。「一時ファイルだけを消す」Tool を別に宣言する方法が要る）。Task の Host の外への Web の読み取りは `APPROVAL`（要件は Web / Docs の read-only 取得を `AUTO`）。
- **緩い方向（Tool の宣言が前提）**: Task Scope 内の `credential-use` は `SCOPED_AUTO`。要件の `STRONG_APPROVAL` は Credential の登録・更新・削除で、使用は分類されていない
  （AI 専用 Branch への push と PR 作成は `SCOPED_AUTO`）。Credential を管理する Tool は `min_level=STRONG_APPROVAL` を宣言する。
  Host 全体の `sudo` / 特権操作は要件では `STRONG_APPROVAL` だが、表は `HOST` 環境の `write` / `execute` を `APPROVAL` にしている。特権の Tool は `min_level=STRONG_APPROVAL` を宣言する必要がある。

### 3. 外部の読み取り・書き込み

1. 外部 write の許可は `TaskScope.hosts` で表す。Issue 作成や PR 作成といった「目的」の単位ではない。
2. Task の Host の外への読み取りは、行き止まりの `DENY` でなく `APPROVAL`（承認者は正確な URL を見て 1 回だけ許す）。ワイルドカードや「任意の公開 Host」の許可はない。
3. Credential は、その Credential の使える Host にだけ使える（`TaskScope.credential_handles` は Host の集合を持つ）。Task が触れられる Host でも、その Credential の使える Host でなければ `DENY`（承認でも許可しない）。

### 4. 承認者と取り消し

1. 承認・却下できるのは、Agent が働いている **User 本人だけ**（人間の `Principal`）。Agent 自身の ID は拒否する。Admin / Owner が他の User の Task を承認する仕組みは作らない。
2. 第三者には、他の User の承認の存在を教えない（`not_found`。Audit には `not_authorised`）。
3. 取り消しは、委任元 User と Admin / Owner ができる（権利を減らす方向だけなので、Admin / Owner が代われる）。Task の終了（cancelled / failed / completed）で、その Task の Open な承認を取り消す。この取り消しが失敗したときの扱いは「9. Task の終了と承認」。
4. `STRONG_APPROVAL` は Step-up（PAW-023）の確認が要る。確認できない（Verifier がない、失敗、Timeout）ときは承認できない。Store も `step_up_verified` を受け取り、DB は Step-up なしの強い承認を保存しない。
5. **人の判断の Store 呼び出しは時間で区切る。** 独立 Review が、応答しない DB に `approve` / `reject` / `revoke` が無期限に待たされると指摘した（`revoke_task`・Step-up・Listener・Audit は区切られていた）。Pool の Session の Query の取り消しは、サーバの確認を待って約 10 秒かかるので、`asyncio.timeout` だけでは区切れない（実測）。`ApprovalService` は 1 回の操作の Store 呼び出し（照会と更新）を **1 つの期限 `timeout_seconds`**（開始時に 1 回数え、残りを渡す。Step-up は数えない）で区切り、期限になれば型付きの `unavailable` を返す。`PostgresApprovalStore` の `get` / `decide` / `revoke` は、変更と履歴の行を 1 つの CTE にした Statement を、中断可能な接続（`Database.fetch_abortable`）で実行する。期限を過ぎた Statement はサーバで続きが実行されうるが、原子的なので、承認と履歴は両方反映されるかどちらも反映されない（呼び直すと真の状態が返る）。取り消しの `revoke_task`（9）と同じ方針。
   **Broker が呼ぶ `open_request` / `consume` も同じ方法で区切る**（独立 Review が、Pool の Transaction のままでは応答しない DB や Lock 待ちで `asyncio.timeout` を超え（約 10 秒）、Pool の枠も塞ぐと指摘した）。この 2 つは複数の Statement が要る（(Task, User) ごとの advisory lock、Task の行の `FOR SHARE`、確認、更新）。1 つの CTE にはしない: READ COMMITTED では Statement が Lock を待つ前に Snapshot を取るので、advisory lock の下の件数の確認が古い値で決まり、上限を超える。そこで `Database.transact_abortable` が、中断可能な接続（Pool を使わない）の上で **1 つの `BEGIN` / `COMMIT` の Transaction** を、呼び出し全体で 1 つの期限（`transaction_timeout_seconds`、既定 3 秒）で実行し、期限で Socket を閉じる。打ち切られた Transaction は、Server が閉じた接続しか見ず次の Statement も `COMMIT` も受け取らないので、全部が巻き戻される（`COMMIT` の最中に打ち切られたときだけ、反映されたか分からない。呼び直すと分かる。使う側は失敗に倒れる）。Lock を待つ Backend は閉じた Socket に気づかないので、Server にも `lock_timeout` / `statement_timeout`（残り時間 + 1 秒）を伝え、打ち切られた Transaction が advisory lock と Server の接続を持ち続けないようにする。時間切れは `TimeoutError` で、Broker は型付きの `approval_unavailable`（Log は型名だけ）にする。Task の終了・Run との順序（9）は変えない（Lock の順序、Task の行の `FOR SHARE` の位置、同じ Transaction での前の Run の取り消しは同じ）。
   Human に判断を求める点: 中断可能な接続は呼び出しごとに接続を張るので Pool の Session より重い（承認の要求と使用は承認を要する Tool 呼び出しごとに 1 回）。`AUTO` / `SCOPED_AUTO` の呼び出しは承認の Store を呼ばない。

### 5. 承認の表示・件数・却下

1. 承認者に、呼び出しの全引数を名前つきで見せる（Redact・Escape・256 文字で切り、全長と Hash 先頭つき）。見せるものがない承認は開かない。
2. (Task, User) ごとの Open な承認は 10 まで（1〜100 で設定可）。却下した呼び出しは 5 分（1 分〜24 時間で設定可）は再要求できない。

### 6. Credential

1. Agent の引数・結果に Credential の平文を入れない。使用は不透明な handle だけ。Tool の引数に平文があれば `DENY`、結果は Redact する（形式・代入の形・Key 名・Dict の Key）。
2. 検出は Best Effort であることを認める（本来の防御は handle のみの構造）。

### 7. 永続化と DB の保証

1. 承認は PostgreSQL に保存する（Migration `0031`）。状態は DB の Trigger と CHECK 制約でも守る（作成は pending のみ、合法な遷移のみ、識別する列は不変、DELETE / TRUNCATE は拒否、すべて `ENABLE ALWAYS`）。
2. Application の Role には最小権限（`tool_approvals` は SELECT・INSERT と状態の列の UPDATE、履歴は SELECT・INSERT）だけを与える。
3. **承認と消費の Role の分離は、この Decision の範囲では実装しない。** Application の Role が 1 つの間は、Application の Process が侵害されれば、その User の名前で承認を書ける（Database では防げない）。
   Agent の Runtime へ Application の Role の接続を渡さないことが前提。承認の Endpoint（PAW-022 / 023）ができる時に、別 Role または `SECURITY DEFINER` 関数で分離する。

### 8. Repository の ACL

要件の「Project / Repo Permission Inheritance」は、Repository ごとの ACL override（`read` / `write` / `agent`）で、Project の Member でも Repository を読み取り専用や Agent 禁止にできると定める。
独立 Review が、Broker は Project の Resource で認可するため、この override が Tool の呼び出しに効かないと指摘した。実装は次を選んだ。要件は「どの Repository に触れる呼び出しか」の決め方を定めていない。

1. **触れる Repository は Backend が決める。** Orchestrator が Task の作業対象（Working Set）を `TaskScope.repositories`（Repository の ID、Project、Worktree の Path、解決済みの ACL）として呼び出しごとに作る。Model の出力は Repository の ID を名指しできるだけで、ACL を指定できない。
2. **触れる Repository の決め方:** 呼び出しが `repository` 引数（作業対象の ID のみ）で名指しするか、Symlink を解決した後の Path が Repository の Worktree の中にあるとき。入れ子の Repository は両方に触れる（厳しい ACL が効く）。**Host は Repository を表さない**（同じ Host に多くの Repository があるため）。**URL は、Backend が Repository に登録した Remote（`ScopedRepository.remotes`）の下にあるときだけ、その Repository を表す**（Symlink を解決した Path と同じく、入れ子と同様に触れる Repository が増える）。`..`・`\`・`;`・`%2e` などで Remote の外へ出られる Path は「下」と見なさない。
3. 触れる Repository があれば、その Repository の `Resource.repository(...)` で認可する（override は Project の Role を狭めるだけで広げない、Agent は `agent` を持たない Repository を操作できない、は PAW-025 の規則のまま）。**ACL が不明（`None`）なら拒否**し、`inherit` とは読まない。ACL は承認を使うときにも再判定する。
4. **Repository への書き込み**（`project.repo.write` / `project.pr.create`）の Tool は、触れる Repository を Path か `repository` の必須引数で宣言しなければならず（Registry が検査）、作業対象のどの Repository にも触れない呼び出しは拒否する（`repository_not_identified`）。
   根拠は要件の Multi-Repo Task の「Write 範囲は Task Working Set として明示・制御する」「Agent が「ついでに」別 Repo を書き換えてはならない」。読み取りと Agent 実行は触れる Repository がなければ Project の Resource で判定する（Read 範囲は比較的広く取ってよい、と要件にある）。
5. **Repository に触れる呼び出しの URL を、その Repository に結びつける。** 独立 Review が、`repository` 引数だけを認可すると、書き込める B を名指しして `remote` に読み取り専用の A の URL（同じ Host）を渡せば、Executor は A の URL を受け取り、A の ACL を迂回できると指摘した（再現した: `git.push` が `allow`）。実装は次を選んだ。
   - 呼び出しの URL は Model が書くので、Backend が登録した Remote の下にあるかで Repository を決める（上の 2）。A の Remote の下なら A にも触れ、A の ACL も効く（読み取り専用なら拒否）。
   - Repository に触れる呼び出しの URL が作業対象のどの Repository の Remote の下にもなければ拒否する（`remote_not_in_repository`）。ACL を解決していない Repository を指せるため。Remote を登録しない Repository は URL を持てない（既定は拒否）。
   - Host の規則が先: Scope の外の Host は従来どおり（書き込みは拒否、読み取りは承認）。Repository に触れない呼び出し（URL の読み取りだけ）は Host の検査のままだが、その URL が作業対象の Repository の Remote の下なら、その Repository の ACL が効く。
   - 採らなかった案: Model に URL を渡させず Backend が Remote を Executor へ渡す（Tool の引数を変え、`network` の Tool が必須の Host / URL を持つ規則と合わない。将来の選択肢）。
6. **Human に判断を求める点:** (a) 書き込みだけを「Repository を特定できなければ拒否」にしたこと（読み取りと `project.task.run` は Project の Resource のまま）。(b) 入れ子の Repository を「両方の ACL に従う」にしたこと。(c) Host から Repository を推定せず、リモートに書く Tool に `repository` 引数を求めること。(d) URL を Backend が登録した Remote で Repository に結びつけ、`repository` 引数と URL が別の Repository を指す呼び出しは両方の ACL に従わせる（不一致を拒否にはしない）こと。Remote の登録は Orchestrator（Task の Scope を作る側）の責務で、登録がない Repository では URL を伴う呼び出しが通らないこと。

### 9. Task の終了と承認

独立 Review が、Task の終了時の承認の取り消しが失敗すると、それが握りつぶされ、承認が期限まで使えてしまうと指摘した。事実として、取り消しは終了の遷移が Commit された**後**の Listener で行われ、`TaskService` は Listener の失敗を Log に残すだけで再試行しない。実装は次を選んだ。要件は Task の終了と承認の関係を定めていない。

1. **Task の終わりは `completed` / `failed` / `cancelled`**（`TERMINAL_STATES`）。Task の状態に `expired` はなく、承認は自分の `expires_at` で失効する。終了時に、その Task の Open な承認（pending、承認済みで未使用）を取り消す。
2. **取り消しの失敗は握りつぶさない。** `revoke_task` は Store の失敗で `ApprovalRevocationError` を上げる（以前は `0` を返し、「Open な承認はなかった」と区別できなかった）。終了の遷移はもう Commit されているので戻せず、`TaskService` は型名だけを Log に残す。再試行は今は呼び出し側（`revoke_task` は冪等）。
   **取り消しは時間で区切る。** `TaskService` は Listener を待つので、Statement に答えない DB が取り消しを止めると、Commit 済みの遷移の後で Cancel / Complete / Retry の要求が返らなくなる（独立 Review の指摘）。取り消しと履歴を 1 つの Statement にして、中断可能な接続（`Database.fetch_abortable`、期限で Socket を閉じる）で実行する。時間切れは失敗と同じく `ApprovalRevocationError` で、`revoke_task` を呼び直せる。`ApprovalService` は取り消し全体（Store の呼び出しと、取り消した承認ごとの照会・Event・Audit 行）を、承認ごとではなく**1 つの期限 `timeout_seconds`** で区切る（独立 Review の指摘: 期限を承認ごとに数え直すと、承認が最大 100 件ある Task で期限の何倍もかかった）。Store に保存された後で期限が来ても `ApprovalRevocationError` で、承認は取り消し済みだが、まだ報告していない承認の Event と Audit 行は残らない（呼び直すと `0` になる）。
3. **Broker は独立に Fail-closed で止める。** 承認を要する呼び出しは、承認を開くときも使うときも、Task の**現在の状態**（`TaskActivityProvider`）が動ける（`ACTIVE`）ときだけ進む。終了・不明・読めないなら拒否する（`task_not_active` / `task_unknown` / `task_state_unavailable`）。取り消しの成否によらず、終わった Task の承認は使えない。既定の Provider は不明を答える（本物を入れるまで承認は使えない）。
   遷移と取り消しを 1 つの Transaction にする案（`TaskService` が Tool の Table を触る）は、Task と Tool の境界を越えるため採らなかった。
   **使うときの確認と消費は 1 つの Transaction にする。** 独立 Review が、確認（読み取り）と消費が別の操作で、その間に終了の遷移が Commit されると、Commit 後の取り消しと消費が競い、消費が勝った承認が終わった Task で使われると指摘した（再現した）。`consume` は `require_active_task` を受け取り、`PostgresApprovalStore` は同じ Transaction で Task の行を `FOR SHARE` で読み直してから消費する（`ACTIVE` でなければ消費しない）。進行中の遷移は待ち、後の遷移は消費の Commit を待つので、使うことと終了は順序づけられる。Broker の確認は、理由をはっきり返す早い答えとして残す。Task の表を読む（Lock する）のは読み取りだけで、Task の状態は変えない。
   **開くときの確認も挿入と同じ Transaction にする。** 独立 Review が、同じ競合が承認を「開く」側にもあると指摘した。Broker が `ACTIVE` と読んだ後、要求の挿入の前に終了の遷移が Commit されると、Commit 後の取り消しは何も見つけられず、その後に挿入された要求が終わった Task の承認として残る（`ApprovalService` はそれを承認でき、Retry / Restart の後、Listener の取り消しより先に Worker が消費する窓ができる）。`open_request` も `require_active_task` を受け取り、`PostgresApprovalStore` は (Task, User) の advisory lock の直後に、同じ Transaction で Task の行を `FOR SHARE` で読み直してから挿入する（`ACTIVE` でなければ何も作らず、既存の要求も返さず、`TASK_NOT_ACTIVE` / `TASK_UNKNOWN`）。
   **承認は Task の Run に結びつける（独立 Review 第 4 回の指摘で追加）。** 以前の版は「試行番号への結びつけは、Table の列と Migration が要るため採らない。Retry / Restart での取り消し（4）と Task の現在の状態の確認で足りる」としていた。それは誤りだった。終了時の取り消しが失敗して承認が残った Task を Restart すると、Restart の Commit の後、Listener の取り消しが動く前の窓で、新しい試行の Worker は Task の状態の確認（`ACTIVE`）を通り、前の試行の承認を消費できる（再現した。Listener の後、あるいは Listener が動かなければ、消費が取り消しに勝つ）。Retry も同じ窓を持つ。状態の確認は「Task が生きているか」しか言えず、それが**どの試行の承認か**を言えないためである。
   実装は Task の **Run**（`TaskRun(attempt, retry_count)`。`tasks.attempt` は Restart が、`tasks.retry_count` は Retry が 1 増やす。どちらも増えるだけで、再開は必ずどちらか一方を変える）を承認に持たせる。
   - `tool_approvals` に `task_attempt`（1 以上）と `task_retry_count`（0 以上）を加える（`NOT NULL`、CHECK、Trigger が書き換えを拒否。Migration `0031` を直接更新した。未 Merge）。`NewApproval` / `ApprovalBinding` / `ApprovalRecord` が `task_run` を持ち、Broker は `TaskContext.run`（Orchestrator が Worker の開始時の Task から作る）を入れる。
   - 消費の `UPDATE` の `WHERE` に Run を加え、`require_active_task` では、Task の行を `FOR SHARE` で読む同じ Transaction で、Task が生きており、**かつ `attempt` / `retry_count` が束縛の Run に一致する**ことを確認する（`TaskActivity.SUPERSEDED`）。Listener の成否に依存しない。進行中の Retry / Restart はその Commit を待つ。Provider の Protocol は `check(task_id, run)` になった。
   - 承認を求めた Run と違う Run が使えば `approval_superseded`。Task の現在の Run でない Worker は、開くのも使うのも `task_superseded`。
   - 開くとき（`require_active_task`）も、同じ Transaction で要求の Run が現在の Run であることを確認し、**その Task の別の Run の Open な承認を、システムとして取り消す**（履歴に残る）。取り消しは Listener の失敗を補い、「開いている承認は呼び出しごとに 1 つ」の制約で新しい Run が同じ呼び出しを求められなくなるのを防ぐ。Audit 行は書かない（Listener が書く `task_ended` の行は、動かなかった Listener のものとして存在しない。履歴 `tool_approval_events` には残る）。
   - 採らなかった案: (a) `call_hash` に Run を入れる: 古い承認と新しい承認が別の Hash になり一意性の衝突は避けられるが、Hash は不可逆で、消費の Transaction が Task の行の Run と照合する値（承認の行の列）にならない。列を持つほうが単純で、DB の CHECK も効く。(b) DB の Trigger が `tasks` を読む: Task の生涯と承認の証拠を結びつける（承認は Task が Archive されても読めるべき）。(c) Store が Task の行から Run を読んで承認に付ける（Context に Run を持たない）: 古い Worker が新しい Run の承認を開き、使えてしまう。
4. **再び動く Task。** Retry / Restart（終了状態からの遷移）でも Open な承認を取り消す。終了時の取り消しが失敗して残った承認は、再開した Task では使えず（3 の Run による）、新しい承認を求め直す。
5. **Human に判断を求める点:** (a) 承認を要する呼び出しだけが Task の状態を見ること（`AUTO` / `SCOPED_AUTO` は見ない。終わった Task へ呼び出しを渡さないのは Orchestrator の責務）。(b) 既定の Provider を「不明 = 拒否」にしたこと（配線を忘れると承認を要する呼び出しが全て通らない）。(c) 再試行の仕組み（再取り消しの Job）を持たず、Broker の Fail-closed に任せること。(d) 開くとき・使うときの Task の確認を `PostgresApprovalStore` が `tasks` の行で行うこと（Tool の Store が Task の Table を読み Lock する）。(e) 承認を Task の Run（試行の番号と Retry の回数）に結びつけ、Retry / Restart の後の Run は前の Run の承認を使えないこと、新しい Run が求めたときに前の Run の Open な承認をシステムとして取り消すこと（Audit 行なし）、`TaskContext.run` を Orchestrator が作ること。要件は Retry / Restart と承認の関係を定めていない。Retry（同じ試行のやり直し）でも承認を引き継がない選択は、Retry が「同じ Context のやり直し」であることと緊張する（4 の取り消しに合わせて、引き継がない側を選んだ）。

## 既知の制限と後続の課題

- Symlink の確認と使用の間の競合（TOCTOU）、DNS Rebinding、Redirect は Executor の責務（[README](../../apps/backend/README.md) の「Executor の契約」）。
- Approval の期限は Application の時計で比較する（Database の時計ではない）。
- `PostgresApprovalStore.history`（Test と診断が読む。どの Request の経路からも呼ばれない）は Pool の Session で動き、期限で区切っていない。
- 却下の Cooldown は Hash 単位で、引数を変えた別の呼び出しは止めない（件数の上限が量を抑える）。
- 引数のない Tool は承認を開けない。承認が要る Tool は、何をするかを表す引数を必須にする。
- Credential の検出は形のわかる Format と代入の形だけ。
- Task の Scope（作業対象の Repository とその ACL を含む）・Grant・Project の状態は、Orchestrator が呼び出しごとに現在の値から作る（Broker は渡された Context を信頼する）。Path も `repository` 引数もない Tool（Remote の下にない URL だけの Tool を含む）は、Repository の ACL では判定できない。

## リスク

- 承認の Role を分離するまで、Application の Process の侵害は承認の偽造につながる（Database の保証は、書き換え・Replay・削除・自己承認以外の Application の誤りに効く）。
- 要件より緩い点（`credential-use`、特権の Tool）は Tool の宣言に頼る。宣言を誤った Tool は、要件より少ない確認で走る。Tool の登録時のレビューが要る。
- 厳しい点（Host の外の読み取りが `APPROVAL`）は、調査型の Task で承認が増える。運用で `TaskScope.hosts` の作り方を見直す。

## 承認後の扱い

承認された場合、PAW-031 の PR は本 Decision を参照する。
Human が変更を指示した項目は、この Decision を更新してから実装を合わせる。
承認後に方針を変える場合は、この Decision を書き換えず、新しい Decision から `Supersedes` する。
[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。
