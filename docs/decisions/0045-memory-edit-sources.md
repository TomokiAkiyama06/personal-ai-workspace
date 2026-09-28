# Memory の編集でできた新しい Version の出典（`memory_sources`）と、会話・Task から由来する Version の検索

- Status: Proposed（1 は Human の決定として伝達済み。2〜6 は推奨つきの提案）
- Date: 2026-09-28
- Scope: Issue [#128](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/128)（[Decision 0034](0034-memory-versioning-freshness.md) の 9。会話の削除の前提）。関連: PAW-042（#36、PR #122 / #135）、[Decision 0009](0009-shared-memory-administration.md)（Shared Memory の `user_confirmation` の出典）、[Decision 0018](0018-memory-journal-consolidation-policy.md)（Journal）
- Supersedes: [Decision 0034](0034-memory-versioning-freshness.md) の 2 のうち「編集・復元・Revalidate の新しい Version に `memory_sources` を写さない」という箇条のみ（承認後）。0034 の 9（「会話削除の Flow の Issue で決める」とした未決の点）はこの Decision が埋める。0034 の File 自体は書き換えない（AGENTS.md 14）
- Approval: 1 は、2026-09-28 に Orchestrator の作業 Session で Human が AskUserQuestion に直接回答して決めたと、実装 Agent に伝えられた（下の「1 の記録について」）。2〜6 は未承認

## 背景

PR #122（PAW-042）では、編集・復元・Revalidate でできた新しい Version に `memory_sources` を写さない（Decision 0034 の 2 と 9）。
そのため会話を削除するとき、出典（`memory_sources.conversation_id`）から Version を探すと、その会話から来た内容を人が編集した Version が見つからず、残ってしまう（Privacy に効く）。
Issue #128 は、会話の削除を実装する前にこれを決め、「会話・Task の削除で、由来する Version をすべて見つけられる」ことを求める。

## 決定と提案

### 1. 編集の新しい Version は出典を写し、編集者の `user_confirmation` を足す（Human の決定）

- 編集（`edit_memory`）で新しい Version `n + 1` を書くとき、Version `n` の `memory_sources` を**すべて** `n + 1` に写し、さらに編集した人の `user_confirmation` の出典を 1 行足す。
- 会話・Task を削除したとき、編集でできた Version を**どう処理するか（消す・残す・`user_confirmation` があれば残す、など）は、ここでは決めない**。会話の削除の Issue で決める。

実装での具体化（1 の範囲内の実装上の選択）:

- 写す列は `source_type`、`conversation_id`、`message_id`、`source_ref`、`source_deleted_at`、`created_at`（出典を記録した時刻をそのまま持つ）。写しは Database の中の `INSERT ... SELECT` で行い、出典の値は Backend に届かない。
- `user_confirmation` の `source_ref` は `memory_confirmed_by:<user id>`（Decision 0009 の `shared_memory_candidate:<candidate id>` と同じく、種類を表す接頭辞と ID）。同じ人が続けて編集した場合、写した出典に同じ行が既にあれば足さない（1 人につき 1 行）。別の人が編集すれば、その人の行が増える。
- Scope の縮小（`project → user`、Decision 0034 の 2）も編集なので、出典を写す。
- 何も変えない編集は何も書かない（出典も）。
- `system` の Identity は、#135 のとおり、どの Method でも Database に触る前に拒否される（出典も書かない）。
- `FreshnessMaintenance.end_session` / `end_task` は出典で `session_only` の Version を探すが、人は `session_only` を書かない（Decision 0034 の 4。編集で `session_only` を保つと拒否され、新しい鮮度を要する）。そのため、写した出典によって人の Version が Session / Task の終わりに `deprecated` になることはなく、0034 の振る舞いは変わらない（Test: `test_a_person_s_version_is_never_ended_with_the_session`）。

#### 1 の記録について

AGENTS.md の Merge Policy と同じく、Agent 間の伝達は Human の承認そのものではない。実装 Agent は Human の回答を直接見ていないため、この File では 1 を「Human の決定として伝達済み」と書き、Status を Approved にしていない。
Human がこの File（または PR）で 1 を確認したら、「承認時の決定」に日付と回答を追記し、Status を更新する。

### 2. 復元・Revalidate の新しい Version も出典を写すか（提案）

Decision 0034 の 9 は編集・復元・Revalidate の 3 つを扱うが、1 の回答は「編集」についてである。

- **推奨: 同じ規則にする。**
  - Revalidate（`revalidate_memory`）: 内容は `n` と同じなので、`n` の出典を写し、確かめた人の `user_confirmation` を足す。
  - 復元（`restore_version`）: 内容は選んだ過去の Version `k` のものなので、**`k` の出典**を写し（現在の `n` の出典ではない）、復元した人の `user_confirmation` を足す。
- 理由: どちらも会話から来た内容をそのまま新しい Version に持ち込む。写さなければ、会話の削除で見つからない Version が残る（1 と同じ問題）。
- 代替案: 写さず、下の 4 の由来の検索（`attributes` の辿り）だけで見つける。見つけることはできるが、出典を読む他の処理（`FreshnessMaintenance.end_session` など）からは見えない。
- **PR は推奨どおりに実装している**（承認されなければ別の PR で直す）。

### 3. 削除済みの会話を指す出典は写さない（提案）

会話を削除すると `ON DELETE SET NULL` で `conversation_id` / `message_id` が NULL になり、出典の行は `source_type = 'conversation'` のまま何も指さなくなる（`source_deleted_at` で失われたことを記録する）。
Database は、何も指さない `conversation` の出典を**新しく INSERT することを拒否する**（Trigger `tr_memory_sources_conversation_source_identified`、PAW-040）。そのため 1 の「すべて写す」をそのまま行うと、そのような出典を持つ Version は編集できなくなる。

- **推奨: その行だけ写さない**（古い Version には残る）。何も指さないので、会話からの検索で一致することもない。
- 代替案: Trigger を変えて写しを許す（Migration が要る）。「失われた出典があった」ことが新しい Version にも残るが、会話の削除の Flow が決まる前に Schema を変えることになる。
- 現状では会話の削除の Flow がまだないため、この状態の行は通常は存在しない。
- **PR は推奨どおりに実装している**。

### 4. 由来する Version の検索と、既存の Version の扱い（提案）

会話・Task から由来する Version を探す Backend 内部の検索（`MemoryDerivation.versions_from_conversation` / `versions_from_task`）を置く。Version が見つかる条件は次のどちらか（推移的に）:

1. その会話（`conversation_id`。Message の出典も会話を持つ）または Task（`source_type = 'task'` で `source_ref = str(task_id)`。`FreshnessMaintenance.end_task` と同じ照合）を指す出典を持つ。
2. 見つかった Version から人が書いた同じ Memory の Version（`actor_type = 'user'`）で、`attributes` の `edited_from_version` / `revalidated_from_version` / `restored_from_version` がその番号を指す。

状態（`active` / `superseded` / `deprecated` / `history`）と Scope を問わず返す。返すのは ID と番号と「出典を直接持つか」だけで、内容は返さない（Freshness の Job と同じく呼び出しの認可はない。会話の削除の Flow が、削除そのものの認可の後で `system` として呼ぶ）。

- **推奨: 既存の編集済み Version に出典を Backfill する Migration は作らない。** 2 の条件で、写しがない既存の Version（PR #122 の書き方）も見つかるため。Migration は不要で、Revision `0128` は使っていない。
- 代替案: Revision `0128` で既存の編集済み Version に出典を写す（編集の順に辿る必要がある）。出典を読む他の処理からも見えるようになるが、検索の完全さには要らない。

### 5. Manual Versioning の外の由来をこの Issue の範囲外にする（提案）

内容が別の Memory へ移る経路が、Manual Versioning の外にもある。

- Shared Memory への昇格（Decision 0009）: Candidate（`shared_memory_candidates.origin_version_id`）から Shared Memory の Version 1 を作る。その出典は `user_confirmation`（`shared_memory_candidate:<id>`）だけで、元の会話は写らない。Candidate の行自体も内容を持つ。
- Journal の Consolidator（Decision 0018）: 同じ Key の新しい Version は、新しい Turn の出典を持つ（古い Version の出典は写さない）。

- **推奨: この Issue では扱わず、会話の削除の Issue で、4 の検索を Candidate の `origin_version_id` と Shared Memory の Version にも広げるか、Journal の Version をどう扱うかと一緒に決める。** どちらも Decision 0009 / 0018 の規則に関わり、削除時の処理（1 で未決）と切り離せないため。

### 6. 他の User の Private Memory に縮小された Version（提案。削除時の処理は未決）

Project Memory を編集者の User Memory へ縮小すると（1 の Scope の縮小）、Project の Version の出典（別の Member A の会話を指しうる）が編集者 B の Private Memory の Version に写る。A がその会話を削除すると、4 の検索は B の Private の Version も返す。

- **推奨: 設計どおり返す**（検索は Scope と所有者を問わず、見つけることに徹する）。
- B の Private の Version を削除時に消すか、残すか、B の `user_confirmation` があれば残すかは、他の User の Private Memory に関わる Privacy の問題として、1 の未決の処理と一緒に会話の削除の Issue で決める。

## 選定理由

- 出典を写すと、会話の削除は `memory_sources` を 1 回引くだけで、編集を重ねた Version まで見つかる（1）。
- `user_confirmation` を足すと、その内容を人が確かめたことが出典として残り、削除の Issue で「人が確かめた内容を残すか」を判断する材料になる（1。処理は未決）。
- 由来の辿り（4 の 2）を併せ持つと、写しがない過去の Version や写せなかった Version も見逃さない。

## リスク

- 編集ごとに出典の行が増える（出典の数 + 最大 1 行）。手動の操作なので量は小さいとみなす。
- 4 の検索は件数の上限を持たない（削除の Flow はすべてを要する）。巨大な会話で多くの Version が見つかる場合は、削除の Issue で分割を考える。
- 検索と削除の順序・並行性: 会話を削除すると `ON DELETE SET NULL` で `conversation_id` が消え、その後に検索しても何も見つからない。また、検索と削除の間に手動の編集が Commit されると、検索が返さなかった新しい Version に出典が写る。そのため会話の削除の Flow は、**会話を削除する前に**検索を行い、手動の編集と直列化する（例: 検索と削除を 1 つの Transaction で行い、見つかった Memory の Advisory Lock を取る）必要がある。具体的な設計は会話の削除の Issue で決める。
- `attributes` の 3 つの Key は `MemoryVersioningService` だけが書く前提（`actor_type = 'user'` の Version に限る）。他の書き手が同じ Key を人の Version に書くようになれば、辿りが広がる（見つける側に倒れる）。

## 決めてほしいこと

1. （伝達済みの決定の確認）編集の新しい Version に前の Version の出典をすべて写し、編集者の `user_confirmation`（`memory_confirmed_by:<user id>`）を足すこと。削除時の処理は会話の削除の Issue で決めること。
2. 復元・Revalidate も同じ規則にすること（復元は復元した Version `k` の出典を写す）。推奨: 承認。
3. 削除済みの会話を指す（何も指さない）出典は写さないこと。推奨: 承認。
4. 由来の検索を出典と `attributes` の辿りで行い、既存の Version の Backfill（Revision `0128`）を作らないこと。推奨: 承認。
5. Shared Memory への昇格と Journal の由来をこの Issue の範囲外にし、会話の削除の Issue で決めること。推奨: 承認。
6. 他の User の Private Memory に縮小された Version も検索で返し、その削除時の処理（他の User の Private Memory の Privacy）は会話の削除の Issue で決めること。推奨: 承認。

承認後に方針を変える場合は、この Decision を書き換えず、新しい Decision から `Supersedes` する。
