# Shared Memory 管理の方針（Candidate、削除と復元、System Policy との優先関係）

- Status: Approved
- Date: 2026-09-24
- Scope: PAW-046（Shared Memory Administration）と、Shared Memory を使う以降の Issue（PAW-041 保存、PAW-042 競合処理、PAW-043 Retrieval、PAW-045 Projection）
- Supersedes: なし
- Approval: 2026-09-25、Humanが作業Session内で、判断メモ（Artifact）の各点について「推奨どおり」と回答して承認（末尾の「承認時の決定」）

## 背景

[REQUIREMENTS.md](../../REQUIREMENTS.md) の「Shared Memory permissions」と [Memory Architecture](../MEMORY_ARCHITECTURE.md) は、次を定める。

- Active User は Read できる。Owner / Admin が Create / Edit / Delete / Restore する。一般 User は直接 Write しない。
- User / Project / Repo Memory から Workspace 全体へ共有すべき内容が見つかったら、Shared Memory Candidate にし、Owner / Admin の明示承認で Shared Memory にする。
- Agent は Shared Memory へ自動昇格しない。
- Shared Memory は Global / System Security Policy を上書きできない。Policy は Memory の優先順位の外側で最優先である。
- 複数人の編集には Optimistic Lock を使う。Memory は物理削除せず、状態と Version で履歴を残す。

一方、次のことは文書だけでは決まらない。PAW-046 の実装は、動かすために下の選択を置いた。
これらを実装が暗黙に確定させないよう、選択を一覧にして Human の承認を求めた。Human は 2026-09-25 に、この一覧を承認した（変更した点は末尾の「承認時の決定」）。

実装は [Backend README](../../apps/backend/README.md) の「Shared Memory Administration」にある。
**この Decision は 2026-09-25 に Human が承認した（Approved）。** 下の各選択は、承認された方針である（承認時の決定は末尾を参照）。
数値（50 件、20 個の subject、20,000 文字など）は暫定値として承認した（「選定理由」）。方針を変える場合は、新しい Decision から `Supersedes` する。

## 提案

### 1. Candidate は専用の Table に置く（Migration 0046）

1. Candidate は `shared_memory_candidates`（Migration `0046`）に置き、PAW-040 の `memory_versions` には置かない。
2. 理由。`scope = 'shared'` の Version は全 User が読める（`memory.acl`）。Candidate は Private な Memory から来た内容を持つので、承認前に誰にでも読める形で置けない。
   また Version は書き換えない（状態の変更は `status` だけ）ため、`pending` から `approved` / `rejected` へ進める Candidate の状態と提案者・決定者・決定日時を持てない。
3. Candidate は削除しない。決定の記録として残す。

### 2. 誰が Candidate を提案できるか

1. Active User は、自分の名前で提案できる（Capability `memory.use`）。
2. Agent は、委任元の User のために提案できる（Grant に `memory.use` があるとき）。Candidate には User と Agent の両方を記録する。
3. Backend 自身の ID（`system` role、Background Worker）は提案できない（Capability を持たない）。
4. 提案は昇格ではない。Candidate は `pending` のまま、Owner / Admin の決定を待つ。

### 3. Candidate を見られるのは Owner / Admin だけ

1. 一覧と取得は Owner / Admin だけ。提案した User 本人も、この Service では見られない。Private な内容の写しが Candidate に入るため。
2. 提案者が自分の Candidate の状態（承認・却下）を知る方法は、作らない。Human が 2026-09-25 に、今は作らないと決めた。必要になったときは、通知を含めて別 Issue で扱う。

### 4. 提案の上限

1. 1 人（Agent の提案は委任元の User に数える）につき `pending` の Candidate は最大 50 件。超えると提案を拒否する。決定済みの Candidate は数えない。
2. 理由。Agent の大量の提案で Owner / Admin の確認 Queue を埋められないようにする。50 は実測に基づかない暫定値として承認した（`memory.shared.limits` の定数で、変えられる）。

### 5. 出典（Provenance）は提案者の申告

1. Candidate は、元の Memory の Scope（`user` / `project` / `project_group` / `repo`）と、任意で元の Version の ID を持つ。
2. Service は元の Memory を読まず、申告を確認しない。表示は Owner / Admin が内容を確認する材料であり、権限の根拠にはならない。
3. 承認で作る Shared Memory の Version には、`memory_sources` に `source_type = 'user_confirmation'`、`source_ref = 'shared_memory_candidate:<candidate id>'` を 1 行記録する。

### 6. 承認は「新しい Memory を作る」

1. 承認は、Candidate の内容で新しい `memories` と Version 1 を作り、Candidate を `approved` にする（1 つの Transaction）。元の Memory は変更しない。
2. Version は `active`、`confirmation_state = 'confirmed'`、`freshness_policy = 'permanent'`、`actor_type = 'user'`（承認した人）。
   鮮度は `permanent` 固定で承認した。鮮度の設定（再確認の期限など）が必要かは PAW-042 で見直す。
3. 承認・却下は 1 回だけ。決定済みの Candidate への再度の決定は状態の競合で拒否する。
4. 承認時に System Policy との衝突は検査しない。衝突は下の 9 のとおり、Effective View で Policy が勝つ。

### 7. 削除は現在の Version の `deprecated`、復元は `active` に戻す

1. 削除は、現在の Version（番号が最大）の `status` を `active` から `deprecated` にする。何も消さない。復元は `deprecated` から `active` に戻す。
2. 削除・復元では新しい Version を作らない。誰がいつ削除・復元したかは、Audit の行にだけ残り、`memory_versions` には残らない。
   行は 2 本ある。Authorizer の試みの行（`action` が `shared_memory.delete` / `shared_memory.restore`。変更の前に書かれる。12 を参照）と、変更が Commit されたことを示す完了の行（同じ Transaction で書く。13 を参照）。
3. 代替案は、削除・復元ごとに新しい Version を作り、`actor_user_id` と `change_reason` を Memory 側に残す方法。履歴は詳しくなるが、Version が増え、内容の同じ Version が並ぶ。
4. PAW-040 は `memories` に DELETE 権限を与えているが、この Service は使わない。物理削除の経路（User 削除、法的な消去など）は別 Issue で決める。

### 8. 編集

1. 編集は新しい Version（`n + 1`、`active`）を作り、旧 Version を `superseded` にして `supersedes` 関係を残す。旧 Version は書き換えない。
2. Optimistic Lock: 呼び出し側は編集した Version の番号（`expected_version`）を渡す。現在の Version と違えば、上書きせず競合として拒否する。
3. 何も変わらない編集（すべての値が現在と同じ）は、エラーにせず、Version を作らずに現在の Memory を返す。
4. 削除済みの Memory は編集できない。先に復元する。

### 9. System Security Policy との衝突は「宣言」で決める

1. Backend は 2 つの文章が矛盾するかを判定できない。Policy の中身も、この Decision は決めない。
2. そこで衝突を宣言にする。Shared Memory は、関係する `policy_subjects`（`merge.permission` のような、`.` 区切りで最大 5 階層の Key）を持てる。設定するのは Owner / Admin（作成・編集・承認のとき）。
   Policy の項目（`SystemPolicyItem`）は `policy_id`、`subject`、`statement` を持つ。
3. 規則: Memory の `policy_subjects` のどれかが、Policy の `subject` と等しい、またはその下位（`merge` は `merge.permission` を覆う）なら、その Memory はその Policy に上書きされる。
   `merge` は `mergeable` や `merge_x` を覆わない。Policy が Memory より下位の `subject` のときも覆わない。
4. 上書きされた Memory は Effective View に含めず、ID と勝った Policy の ID だけを返す。内容は返さない。Policy は呼び出しごとに読み直すため、Policy を足せば既存の Memory にすぐ効く。
5. 限界: `policy_subjects` を宣言していない Memory は、この規則では上書きされない。Owner / Admin の承認と、PAW-042 の矛盾検出（宣言を補う）に頼る。
   なお Shared Memory 自体は権限を与えない。Tool の実行や Merge の可否は Backend の認可と Tool Broker が Memory と無関係に強制する。

### 10. Effective View の範囲と、Policy を読めないとき

1. `effective_view` は Active User（と、`shared_memory.read` を Grant された Agent）が呼べる。返すのは `memories` と `overridden`（上書きされた Memory の ID、Version の ID、勝った Policy の ID）だけで、**Policy の文言（`statement`）は返さない。**
   Memory を上書きした Policy の文言（`applied_policies`）は、**Backend 内部の呼び出し（Context の組み立て）だけ**に限り、User にも Agent にも見せない。Human が 2026-09-25 に決めた（承認時の決定）。Global System Prompt との関係は、Policy の文言を公開する場合に別途決める。
   - 実装: `effective_view` の戻り値 `EffectiveSharedMemory` は `applied_policies` の欄を持たない（`repr` にも `dataclasses.asdict` にも文言は現れない）。文言は、別のメソッド `internal_effective_view` が返す `InternalEffectiveView`（Backend 内部専用）にだけある。
     引数で切り替える方式にしないのは、公開のメソッドのどの引数でも文言が出ないようにするため。認可（`shared_memory.read`）、Policy の読み込み、fail closed は `effective_view` と同じ。
   - 限界: User は、Memory が効かない理由を Policy の文言では確認できない（勝った Policy の ID は分かる）。`internal_effective_view` を HTTP に出さないことは、Service を HTTP に出す Issue の Review で確認する（公開メソッドの一覧を固定する Test は、内部の 1 メソッドを明示している）。
2. Policy を読めない（Source の失敗、遅延、契約違反の応答）ときは、Shared Memory を 1 件も返さずに失敗する（fail closed）。Policy なしの Shared Memory は返さない。
3. `list_memories` と `get_memory` は保存されている Memory をそのまま返す（管理画面の用途）。モデルに渡す Context の組み立ては、Backend 内部では `internal_effective_view`（Policy の優先を適用した上で、勝った Policy の文言を含む）を使い、User に見せる画面と API は `effective_view` を使う。

### 11. Owner / Admin（人間）だけが管理する

1. 作成・編集・削除・復元・承認・却下・Candidate の閲覧は、人間の Owner / Admin だけ。
2. Agent と `system` role は、Authorizer が何を答えても、常に拒否する（`AutomaticPromotionRefusedError`）。Authorizer の判定は先に記録される。
3. 通常の User は拒否する。Authorizer が誤って許可しても、Owner / Admin 以外は Service が拒否する（多層防御）。
4. 削除済みの Memory を含める読み取り（`include_deleted`）も、管理と同じ権限を要する。

### 12. 変更する操作ごとに Audit の `action` を分ける（Capability を 6 つ追加する）

1. 問題。削除と復元は `status` を変えるだけで、実行者と時刻を行に残さない（7）。履歴は Authorizer の Audit だけである。
   Authorizer は `action` に Capability の値を書く。全管理操作が 1 つの `shared_memory.manage` を使うと、削除と復元、作成、編集、承認、却下を Audit から見分けられない。
2. 方針。Shared Memory を変える 6 つの操作に、それぞれ Capability を追加する。`authz/capabilities.py` と `authz/policy.py` への加算だけで、既存の判定は変わらない。

   | Capability（Audit の `action`） | 操作 |
   | --- | --- |
   | `shared_memory.create` | `create_memory` |
   | `shared_memory.edit` | `edit_memory` |
   | `shared_memory.delete` | `delete_memory` |
   | `shared_memory.restore` | `restore_memory` |
   | `shared_memory.candidate.approve` | `approve_candidate` |
   | `shared_memory.candidate.reject` | `reject_candidate` |

   すべて `Scope.SYSTEM`、委任不可（`delegable=False`。[Decision 0004](0004-rbac-capability-and-audit-policy.md) の 2 の許可リストに入れない。`shared_memory.manage` と同じ）、Audit Mode `REQUIRED`、Owner / Admin だけが持つ。
   `shared_memory.manage` は、削除済み Memory と Candidate の閲覧（`include_deleted`、`list_candidates`、`get_candidate`）に残す。何も変えない読み取りで、`resource_kind` と `resource_id` の有無で区別できるため。
3. 選んだ理由。次の 3 つの保証を、新しい仕組みなしに保てる。
   - 既定は拒否: 新しい Capability は `CAPABILITIES` の表（`delegable` の明示が必須。書かないと起動時に失敗する）と Owner / Admin の表にだけある。ほかの Role は持たない。
   - Audit の行は変更が見える前に書かれる: 認可（と Audit）は Database の Transaction の前に終わる。この行は変更の**試み**で、変更が起きたことは示さない（13）。
   - Audit を書けなければ許可を拒否に変える: Audit Mode `REQUIRED` の既存の規則。
   拒否された試みも、試みた操作の `action` で残る。
4. 代替案。
   - Service が `AuditSink` へ操作専用のイベントを別に書く: 1 回の呼び出しに Audit の行が 2 本（判定と操作）でき、突き合わせが要る。Service に Sink を持たせ、「書けなければ止める」規則を Authorizer と二重に実装することになる。`action` が Capability でない値になり、Audit の規則を広げる必要もある。
   - 行に実行者と時刻を持たせる（`memory_versions` に列を足す、または削除・復元ごとに新しい Version（7 の 3））: PAW-040 の権限（Version の列は更新できない）を変えるか、内容の同じ Version が並ぶ。行の記録は、Trigger で追記専用にした Audit ほど強くは守れない（列の権限だけが守り）。拒否された試みの履歴も残らない。
5. 限界と帰結。
   - Capability が 6 つ増える。Policy を差し替えて `shared_memory.manage` だけを Grant していた Role は、変更の操作ができなくなる（拒否の方向。既定の Policy では Owner / Admin が全部持つ）。
   - 削除・復元の理由は残らない（Audit は自由な文を持たない）。
   - 閲覧（`manage`）は分けず、6 つより粗くもしない。Human が 2026-09-25 に、6 つ（閲覧は `shared_memory.manage` のまま）で承認した。

### 13. 変更の完了を、変更と同じ Transaction で Audit に残す

1. 問題。Authorizer の `allow` の行（試み）は、変更の前に書かれる（12）。変更が対象の欠如、状態の違い、版の競合、Lock の待ち切れ、更新の失敗で終わっても、同じ行が残り、成功した変更と区別できない。
   行には遷移の時刻もない（実行者は `actor_id` にあるが、変更が起きたかが分からない）。削除・復元は `status` だけを変えるので、Memory の行にも実行者と時刻は残らない（7）。
2. 方針。変更する 6 つの操作（12 の表）は、変更と**同じ Database の Transaction の中**（最後の書き込み）で、`audit_events` へ完了の行を 1 本追加する。

   | 列 | 完了の行の値 |
   | --- | --- |
   | `action` | 試みと同じ Capability の値 |
   | `decision`、`reason` | `allow`、`completed`（`allow` / `deny` の CHECK があるため。`reason` が試みの行と分ける） |
   | `actor_id`、`actor_role` | 操作した Owner / Admin（人間。Agent と `system` は変更に進まない） |
   | `resource_kind`、`resource_id` | 変えたもの。`create_memory` は作った Memory の ID。承認・却下は Candidate の ID |
   | `occurred_at` | Service の時計の値（新しい Version の `created_at` と同じ読み。遷移の時刻） |
   | `correlation_id` | 試みの行と同じ。Service が呼び出しごとに作り、Authorizer に渡す |

   何も変えなかった呼び出し（内容が同じ編集）には完了の行を書かない。
   結果: **完了の行がある ⇔ 変更が Commit された**。Rollback、失敗した Statement、失敗した Commit は行も戻し、完了の行を書けなければ変更も戻る（fail-closed）。
   失敗した呼び出しは、`correlation_id` が同じ完了の行がない `allow` の行（完了のない試み）として見つかる。試みの規則（既定は拒否、変更の前に書く、書けなければ拒否）は変えない。
3. 選んだ理由。
   - 原子的: 変更と記録が別れる隙間がない。Commit 済みの変更の記録だけが失敗して「記録のない変更」が残ることがない。
   - 新しい仕組みがない: 新しい Table、列、権限、Migration がない。Application の Role は `audit_events` の INSERT / SELECT をすでに持ち（PAW-025）、UPDATE / DELETE / TRUNCATE は Trigger と権限が拒否したままである。
     Trigger で追記専用にした Audit は、Memory の行の列より強く守られる（12 の 4）。PAW-040 が `memory_versions` に許す UPDATE は `status` などの列だけで、実行者と時刻の列を足すには PAW-040 の Table と権限を変える必要がある。
   - 実行者と時刻を、Memory の履歴として 1 か所で読める（`resource_id` と `reason = 'completed'`）。
4. 代替案。
   - Transaction の後に `AuditSink` へ完了（または失敗）の行を書く: Commit 済みの変更に対して書き込みが失敗しうる（Log だけになり、実行者の分からない変更が残る）。Service に Sink を持たせる必要もある。失敗の行は Rollback の後に Transaction の外で書くので、行がないことが何も証明しない。
   - 失敗の行を書く: 上と同じ理由で採らない。失敗は「完了のない試み」から読む。
   - `memory_versions` に実行者と時刻を持たせる、削除・復元ごとに Version を作る: 12 の 4 と同じ理由で採らない。
   - 完了専用の Table: Table と権限が増え、Audit が 2 か所に分かれる。
5. 限界と帰結。
   - 完了の行は Authorizer の `AuditSink` を通らず、Service が `audit_events` に直接書く（Sink は別の Transaction で書くため）。Sink を差し替えた配備では、試みと完了が別の場所に分かれる。現在の Sink は `PostgresAuditSink` だけである。
   - 完了の行も `decision = 'allow'` である。`action` と `decision` だけで集計すると、1 回の操作が 2 行になる。集計は `reason` で分ける。
   - 失敗そのものの行はない。実行中の試みと、Process が落ちた試みも、完了がないので区別できない。
   - 1 回の変更で Audit の行が 1 本増える。完了の行の書き込みが失敗したときは、変更も失敗する（Database のエラーがそのまま伝わる）。
   - `occurred_at` は Service の時計の読み（Lock を待つ前に読む）で、`recorded_at`（Database が付ける。Transaction の開始時刻）ではない。
   - Application の Role は `audit_events` に INSERT できるので、Application 自体が偽の行を書けることは、他の Audit の行と同じである（PAW-025 の限界）。
   - 完了の行の値と書き方（`reason = 'completed'`、Service が直接書く）を変える場合は、新しい Decision から `Supersedes` する。

## 選定理由

- Candidate の可視性（3）と Version の不変性（1）が、専用の Table を選ぶ理由。PAW-040 の Table の権限（Version の列は更新できない）を変えずに済む。
- 削除を `status` の変更にしたのは、PAW-040 が Version の `status` だけを更新可能にしていること、復元があること、履歴を消さないという要件が揃うため（7）。
- Policy との衝突を宣言にしたのは、意味の矛盾を Backend が判定できず、Policy の中身を実装が決めてはならないため（9）。宣言がなければ何も上書きしないという限界は、README にも書く。
- 数値（`pending` の Candidate 50 件、`policy_subjects` 20 個、`content` 20,000 文字など）は、実測に基づかない暫定値として承認した。`memory.shared.limits` の定数 1 か所にあり、変えられる。

## 代替案

- Candidate を `memory_versions` の `scope = 'user'` の Version として置く: Owner / Admin が他人の Private な Memory を読む経路が要り、Version の状態を進められない。
- 提案を Owner / Admin だけにする: 要件の「共有すべき内容が見つかった場合」は Agent の検出を想定していると読めるため採らなかった。提案は昇格でなく、承認は人間だけなので、Agent の提案を許してもリスクは増えない。
- `policy_subjects` の代わりに、Memory の本文から Policy との矛盾を推定する: Backend の判定を LLM に委ねることになるため採らない（Security の判定は Backend が行う）。

## 影響

- `authz/capabilities.py` と `authz/policy.py` に Capability を 6 つ加える（12）。認可の実装（PAW-025）は別の PR にあるため、この変更は加算だけにしてある。
- 変更ごとに `audit_events` へ完了の行を 1 本、変更と同じ Transaction で書く（13）。Table と権限は増えない。
- Migration `0046`（`shared_memory_candidates`）。Application の Role には `SELECT`、`INSERT`、決定の 5 列の `UPDATE` だけを与える。
- HTTP の Endpoint はこの Issue では作らない。API の Issue が `SharedMemoryService` を呼ぶ。
- この Decision は 2026-09-25 に承認された（Status と Approval を更新した）。値や規則を変える場合は、新しい Decision から `Supersedes` する。

## 承認後の扱い

2026-09-25 に承認された。PAW-046 のPRは本Decisionを参照する。
Human が変更を指示した項目（10 の 1）に合わせて、この Decision と実装を更新した。
承認後に方針を変える場合は、この Decision を書き換えず、新しい Decision から `Supersedes` する。
[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。

## 承認時の決定（2026-09-25）

- 本文の各点を、提案どおり承認した。ただし 10 の 1 は、次の変更を加えて承認した。
- 変更を加えた点: `applied_policies`（Memory を上書きした Policy の文言）は Backend 内部の呼び出しだけに限る。User と Agent には見せない。Global System Prompt との関係は、公開する場合に別途決める（10 の 1）。
  実装は `internal_effective_view` と `InternalEffectiveView` を加え、`effective_view` の戻り値から `applied_policies` を外して合わせた。
- Candidate を見られるのは Owner / Admin だけとし、提案者に結果を返す仕組みは今は作らない（3）。
- Capability は 6 つ（`shared_memory.create` / `edit` / `delete` / `restore` / `candidate.approve` / `candidate.reject`）とし、閲覧は `shared_memory.manage` のままにする（12）。
- 提案の受付範囲（Active User と、委任元 User の `memory.use` を持つ Agent が可。`system` は不可）と、暫定値（1 人あたり `pending` の Candidate は最大 50 件など）を、暫定値として承認した（2、4、選定理由）。
- 削除・復元は新しい Version を作らず、Audit の 2 行（試みの行と完了の行）にだけ残す（7、12、13）。
- 承認で作る Memory の鮮度は `permanent` 固定とする。PAW-042 で見直す（6）。
- Policy との衝突を宣言（`policy_subjects`）で決める方式の限界（宣言のない Memory は上書きされない。Policy を読めないときは Shared Memory を 1 件も返さない）を受け入れた（9、10）。
