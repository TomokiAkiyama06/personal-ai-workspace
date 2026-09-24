# Shared Memory 管理の方針（Candidate、削除と復元、System Policy との優先関係）

- Status: Proposed
- Date: 2026-09-24
- Scope: PAW-046（Shared Memory Administration）と、Shared Memory を使う以降の Issue（PAW-041 保存、PAW-042 競合処理、PAW-043 Retrieval、PAW-045 Projection）
- Supersedes: なし
- Approval: 未承認（Humanの承認待ち）

## 背景

[REQUIREMENTS.md](../../REQUIREMENTS.md) の「Shared Memory permissions」と [Memory Architecture](../MEMORY_ARCHITECTURE.md) は、次を定める。

- Active User は Read できる。Owner / Admin が Create / Edit / Delete / Restore する。一般 User は直接 Write しない。
- User / Project / Repo Memory から Workspace 全体へ共有すべき内容が見つかったら、Shared Memory Candidate にし、Owner / Admin の明示承認で Shared Memory にする。
- Agent は Shared Memory へ自動昇格しない。
- Shared Memory は Global / System Security Policy を上書きできない。Policy は Memory の優先順位の外側で最優先である。
- 複数人の編集には Optimistic Lock を使う。Memory は物理削除せず、状態と Version で履歴を残す。

一方、次のことは文書だけでは決まらない。PAW-046 の実装は、動かすために下の選択を置いた。
承認前の Product Policy を実装が暗黙に確定させないよう、選択を一覧にして Human が承認または変更できるようにする。

実装は [Backend README](../../apps/backend/README.md) の「Shared Memory Administration」にある。
**この Decision は Proposed であり、Human の承認を得ていない。** 承認されるまで、次の選択は暫定である。
変更する場合は、新しい Decision から `Supersedes` する。

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
2. 提案者が自分の Candidate の状態（承認・却下）を知る方法は、この Issue では作らない。**必要かは人間が決める**（通知は別 Issue）。

### 4. 提案の上限

1. 1 人（Agent の提案は委任元の User に数える）につき `pending` の Candidate は最大 50 件。超えると提案を拒否する。決定済みの Candidate は数えない。
2. 理由。Agent の大量の提案で Owner / Admin の確認 Queue を埋められないようにする。50 は実測に基づかない仮の値。

### 5. 出典（Provenance）は提案者の申告

1. Candidate は、元の Memory の Scope（`user` / `project` / `project_group` / `repo`）と、任意で元の Version の ID を持つ。
2. Service は元の Memory を読まず、申告を確認しない。表示は Owner / Admin が内容を確認する材料であり、権限の根拠にはならない。
3. 承認で作る Shared Memory の Version には、`memory_sources` に `source_type = 'user_confirmation'`、`source_ref = 'shared_memory_candidate:<candidate id>'` を 1 行記録する。

### 6. 承認は「新しい Memory を作る」

1. 承認は、Candidate の内容で新しい `memories` と Version 1 を作り、Candidate を `approved` にする（1 つの Transaction）。元の Memory は変更しない。
2. Version は `active`、`confirmation_state = 'confirmed'`、`freshness_policy = 'permanent'`、`actor_type = 'user'`（承認した人）。
   鮮度の設定（再確認の期限など）が必要かは PAW-042 で決める。
3. 承認・却下は 1 回だけ。決定済みの Candidate への再度の決定は状態の競合で拒否する。
4. 承認時に System Policy との衝突は検査しない。衝突は下の 9 のとおり、Effective View で Policy が勝つ。

### 7. 削除は現在の Version の `deprecated`、復元は `active` に戻す

1. 削除は、現在の Version（番号が最大）の `status` を `active` から `deprecated` にする。何も消さない。復元は `deprecated` から `active` に戻す。
2. 削除・復元では新しい Version を作らない。誰がいつ削除・復元したかは、Authorizer の Audit Event にだけ残り（`action` が `shared_memory.delete` / `shared_memory.restore`。12 を参照）、`memory_versions` には残らない。
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

1. `effective_view` は Active User（と、`shared_memory.read` を Grant された Agent）が呼べる。返す `applied_policies` には、Memory を上書きした Policy の `statement` を含める。
   **User に Policy の文言を見せてよいか（Global System Prompt との関係）は人間が決める。** 見せない場合は、`applied_policies` を Backend の内部の呼び出し（Context の組み立て）だけに限る。
2. Policy を読めない（Source の失敗、遅延、契約違反の応答）ときは、Shared Memory を 1 件も返さずに失敗する（fail closed）。Policy なしの Shared Memory は返さない。
3. `list_memories` と `get_memory` は保存されている Memory をそのまま返す（管理画面の用途）。モデルに渡す Context の組み立ては `effective_view` を使う。

### 11. Owner / Admin（人間）だけが管理する

1. 作成・編集・削除・復元・承認・却下・Candidate の閲覧は、人間の Owner / Admin だけ。
2. Agent と `system` role は、Authorizer が何を答えても、常に拒否する（`AutomaticPromotionRefusedError`）。Authorizer の判定は先に記録される。
3. 通常の User は拒否する。Authorizer が誤って許可しても、Owner / Admin 以外は Service が拒否する（多層防御）。
4. 削除済みの Memory を含める読み取り（`include_deleted`）も、管理と同じ権限を要する。

### 12. 変更する操作ごとに Audit の `action` を分ける（Capability を 6 つ追加する）

1. 問題。削除と復元は `status` を変えるだけで、実行者と時刻を行に残さない（7）。履歴は Authorizer の Audit だけである。
   Authorizer は `action` に Capability の値を書く。全管理操作が 1 つの `shared_memory.manage` を使うと、削除と復元、作成、編集、承認、却下を Audit から見分けられない。
2. 提案。Shared Memory を変える 6 つの操作に、それぞれ Capability を追加する。`authz/capabilities.py` と `authz/policy.py` への加算だけで、既存の判定は変わらない。

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
   - Audit の行は変更が見える前に書かれる: 認可（と Audit）は Database の Transaction の前に終わる。
   - Audit を書けなければ許可を拒否に変える: Audit Mode `REQUIRED` の既存の規則。
   拒否された試みも、試みた操作の `action` で残る。
4. 代替案。
   - Service が `AuditSink` へ操作専用のイベントを別に書く: 1 回の呼び出しに Audit の行が 2 本（判定と操作）でき、突き合わせが要る。Service に Sink を持たせ、「書けなければ止める」規則を Authorizer と二重に実装することになる。`action` が Capability でない値になり、Audit の規則を広げる必要もある。
   - 行に実行者と時刻を持たせる（`memory_versions` に列を足す、または削除・復元ごとに新しい Version（7 の 3））: PAW-040 の権限（Version の列は更新できない）を変えるか、内容の同じ Version が並ぶ。行の記録は、Trigger で追記専用にした Audit ほど強くは守れない（列の権限だけが守り）。拒否された試みの履歴も残らない。
5. 限界と帰結。
   - Capability が 6 つ増える。Policy を差し替えて `shared_memory.manage` だけを Grant していた Role は、変更の操作ができなくなる（拒否の方向。既定の Policy では Owner / Admin が全部持つ）。
   - 削除・復元の理由は残らない（Audit は自由な文を持たない）。
   - 閲覧（`manage`）まで分けるか、6 つより粗い分け方にするかは、**人間が決める**。

## 選定理由

- Candidate の可視性（3）と Version の不変性（1）が、専用の Table を選ぶ理由。PAW-040 の Table の権限（Version の列は更新できない）を変えずに済む。
- 削除を `status` の変更にしたのは、PAW-040 が Version の `status` だけを更新可能にしていること、復元があること、履歴を消さないという要件が揃うため（7）。
- Policy との衝突を宣言にしたのは、意味の矛盾を Backend が判定できず、Policy の中身を実装が決めてはならないため（9）。宣言がなければ何も上書きしないという限界は、README にも書く。
- 数値（50 件、20 個の subject、20,000 文字など）は、実測に基づかない仮の値で、`memory.shared.limits` の定数 1 か所にある。

## 代替案

- Candidate を `memory_versions` の `scope = 'user'` の Version として置く: Owner / Admin が他人の Private な Memory を読む経路が要り、Version の状態を進められない。
- 提案を Owner / Admin だけにする: 要件の「共有すべき内容が見つかった場合」は Agent の検出を想定していると読めるため採らなかった。提案は昇格でなく、承認は人間だけなので、Agent の提案を許してもリスクは増えない。
- `policy_subjects` の代わりに、Memory の本文から Policy との矛盾を推定する: Backend の判定を LLM に委ねることになるため採らない（Security の判定は Backend が行う）。

## 影響

- `authz/capabilities.py` と `authz/policy.py` に Capability を 6 つ加える（12）。認可の実装（PAW-025）は別の PR にあるため、この変更は加算だけにしてある。
- Migration `0046`（`shared_memory_candidates`）。Application の Role には `SELECT`、`INSERT`、決定の 5 列の `UPDATE` だけを与える。
- HTTP の Endpoint はこの Issue では作らない。API の Issue が `SharedMemoryService` を呼ぶ。
- 承認されたら、この Decision の Status と Approval を更新する。値や規則を変える場合は、新しい Decision から `Supersedes` する。
