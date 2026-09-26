# Memory Version の Status・Stale 状態の変更履歴（Audit の記録との併存）

- Status: Approved
- Date: 2026-09-26
- Scope: PAW-040（Memory Schema）の `memory_versions.status` / `stale_since` の変更履歴（Migration `0071`、Issue #90）。Shared Memory の削除・復元・編集（PAW-046）と、`status` / `stale_since` を UPDATE する以降の Issue（PAW-041 Journal、PAW-042 競合処理など）
- Supersedes: [Decision 0009](0009-shared-memory-administration.md) のうち次の記述だけ（それ以外は変えない。詳細は「0009 との関係」）: 7 の 2 の「Audit の行にだけ残り」、12 の 1 の後半（「実行者と時刻を行に残さない（7）。履歴は Authorizer の Audit だけである。」）、12 の 4 の代替案「行に実行者と時刻を持たせる」が前提にしていた「Memory 側に実行者と時刻の記録を持たない」こと、13 の 1 の後半（「Memory の行にも実行者と時刻は残らない（7）」）、13 の 3 の 3 つ目の利点（「実行者と時刻を、Memory の履歴として 1 か所で読める」）、「承認時の決定」の削除・復元の項の「Audit の 2 行…にだけ残す」
- Approval: 2026-09-26、Humanが作業Session内の質問Toolで、「新Decisionで併存を承認」を選んで承認（末尾の「承認時の決定」）

## 背景

[REQUIREMENTS.md](../../REQUIREMENTS.md) の「Manual Memory Editing」は、Pin と Importance を「即反映可能だが、変更履歴は残す」低リスクの Metadata とし、PAW-040 は `memory_versions.pinned` / `importance` の変更を Trigger で `memory_metadata_changes` に記録する（Migration `0040`）。
一方で、同じく Version の中で更新できる `status`（`superseded` / `deprecated` / `history` へ変える）と `stale_since`（Stale の候補として印を付ける・外す）は、上書きされるだけで、以前の値・実行者・時刻が残らなかった。

PAW-040（PR #71）の独立 Review（Codex）は、これを P2 として指摘した（Issue #90）。履歴 / Graph が「いつ・誰が Deprecated にした / Stale の印を付けた・外したか」を説明できず、これらの遷移は新しい Version にするか、変更履歴の不変の記録に取り込むべきだ、という指摘である。
Human は 2026-09-26 に、この修正を小さな別の PR で今すぐ行うと決めた。修正は Migration `0071` で、`memory_metadata_changes` に Status と Stale 状態の列を足し、同じ Trigger が 4 列すべての変更を記録する。

その実装（PR #109）に対する Review（Codex）は、次の P1 を指摘した。[Decision 0009](0009-shared-memory-administration.md)（承認済み）の 7、12、13 は、Shared Memory の削除・復元の実行者と時刻を **Audit にだけ**残すと決め、「Memory 側に実行者と時刻を残す」案を採らない案として挙げている。
Migration `0071` は、Shared Memory の削除・復元・編集を含む、すべての `status` / `stale_since` の変更を Memory 側にも記録し、Actor を示さない変更を拒否する。これは 0009 の記述と食い違う。
承認済みの Decision は書き換えない（[AGENTS.md](../../AGENTS.md) の「仕様変更」）ので、この食い違いを新しい Decision で扱う。Human は 2026-09-26 に、2 つの記録を**併存**させる方針を、この Decision で承認した。

**この Decision は 2026-09-26 に Human が承認した（Approved）。** 実装（Migration `0071`、`SharedMemoryService` の Actor の指定）は変えない。実装は [Backend README](../../apps/backend/README.md) の「Memory / Conversation Schema」の「Status / Stale の変更履歴」にある。

## 決定

### 1. 2 つの記録は併存する。役割が違う

1. **Audit（変わらない）。** Shared Memory の管理操作（`create` / `edit` / `delete` / `restore` / `candidate.approve` / `candidate.reject`）の**試み**の行（Authorizer。変更の前に書く）と**完了**の行（Service。変更と同じ Transaction で書く）。「誰が、どの操作を、いつ試み、Commit されたか」を答える（0009 の 12、13）。
2. **`memory_metadata_changes`（追加）。** Version の行の**変更そのもの**の記録。Scope を問わず（`user` / `project` / `project_group` / `repo` / `shared`）、`memory_versions.status` または `stale_since` が変わる**すべての** UPDATE について、変更前後の `status`、変更前後の `stale_since`、Actor、DB の時計（`clock_timestamp()`）の時刻を、`memory_versions` の Trigger が追記する。
   Pin / Importance と同じ Table・同じ Trigger で、1 回の UPDATE は 1 行、値が変わらない UPDATE は記録しない。「この Version は、いつ、誰によって、何から何へ変わったか」を、Version の側から答える。

Audit は Shared Memory の管理の**操作**の記録、`memory_metadata_changes` は Version の**状態変化**の記録である。どちらか一方が他方を置き換えることはない。

### 2. Actor は必須。黙って `system` にしない

1. `status` / `stale_since` を変える書き込みは、同じ Transaction の UPDATE の前に `metadata_change_actor(actor_type, actor_user_id)`（`paw_backend.memory.metadata`）で Actor を示す。Pin / Importance と同じ規則である。
2. Actor を示さない変更は、`memory_metadata_changes.actor_type` の NOT NULL で失敗し、UPDATE も取り消される。示されなかったときに `system` などを記録する案は採らない（「代替案」）。
3. Actor の ID は Backend が主張する値で、DB は確認しない（`actor_user_id` と同じ扱い。PAW-021 で User の Table ができるまでの限界）。
4. `SharedMemoryService` は、認可した Owner / Admin を `user` の Actor として示してから `status` を更新する（編集で旧 Version を `superseded` にする変更、削除の `deprecated`、復元の `active`）。

### 3. Version は増えず、`memory_versions` に列は足さない。権限は変えない

- 変更ごとに新しい Version は作らない（0009 の 7 の 1 と 3 の却下は変わらない）。`memory_versions` に実行者や時刻の列は足さない。
- `memory_metadata_changes` は既存の Table で、新しい Table も権限もない。Application の Role は SELECT と INSERT だけを持ち（UPDATE / DELETE は持たない）、履歴は Version と一緒に Cascade で消える。Migration の Downgrade の扱いは Migration `0071` の Docstring に書く。

### 4. 2 つの記録は矛盾してはならない

同じ削除・復元・編集について、Audit の完了の行と `memory_metadata_changes` の行は、対象の Memory、Actor（操作した Owner / Admin）、遷移（削除は `active` → `deprecated`、復元は `deprecated` → `active`、編集は旧 Version の `active` → `superseded`）で一致する。
時刻は別の時計である（Audit の `occurred_at` は Service の時計、履歴の `created_at` は DB の `clock_timestamp()`）ので、完全には一致しない。2 つを結ぶ共通の Key は持たない（`correlation_id` は履歴に持たない）。結ぶときは Memory、Actor、時刻の近さで結ぶ。
この一致は Test で確かめる（`tests/test_shared_memory_status_history.py`）。

## 0009 との関係

### 変える記述（この Decision が置き換える、または狭める）

| 0009 の記述 | この Decision での扱い |
| --- | --- |
| 7 の 2「誰がいつ削除・復元したかは、Audit の行にだけ残り、`memory_versions` には残らない」 | 「Audit の行にだけ」を置き換える。Audit の行（試みと完了）に加えて、`memory_metadata_changes` にも残る。「`memory_versions` には残らない」（列を足さない）は変えない |
| 12 の 1「削除と復元は `status` を変えるだけで、実行者と時刻を行に残さない（7）。履歴は Authorizer の Audit だけである。」 | 後半を置き換える。`status` の変更は、Version の行そのものには実行者と時刻を残さないが、`memory_metadata_changes` に残る。Audit が唯一の履歴ではない。Capability を分けた理由（Audit の `action` で操作を見分ける）は変えない |
| 12 の 4 の代替案「行に実行者と時刻を持たせる（`memory_versions` に列を足す、または削除・復元ごとに新しい Version）」が前提にした「Memory 側に実行者と時刻の記録を持たない」こと | 前提を狭める。Memory 側の記録は、Trigger が書く追記専用の履歴として持つ。**列を足す案と、削除・復元ごとに Version を作る案の却下は変えない**。却下理由のうち「Audit ほど強くは守れない」は、Trigger が書き、Application が UPDATE / DELETE できない履歴 Table には当てはまらない |
| 13 の 1「削除・復元は `status` だけを変えるので、Memory の行にも実行者と時刻は残らない（7）」 | 後半を置き換える。Version の行には残らないが、`memory_metadata_changes` に残る。完了の行を足した理由（試みの行は変更が起きたかを示さない）は変えない |
| 13 の 3 の 3 つ目の利点「実行者と時刻を、Memory の履歴として 1 か所で読める（`resource_id` と `reason = 'completed'`）」 | 「1 か所」を置き換える。Audit の完了の行は Shared Memory の管理操作の記録として引き続き読める。Version ごとの状態変化は `memory_metadata_changes` からも読める |
| 「承認時の決定」の「削除・復元は新しい Version を作らず、Audit の 2 行（試みの行と完了の行）にだけ残す（7、12、13）」 | 「にだけ」を置き換える。新しい Version を作らないこと、Audit の 2 行に残すことは変えない。加えて `memory_metadata_changes` に残す |

### 変えない記述（引き続き有効）

- 7 の 1（削除は現在の Version の `deprecated`、復元は `active`。何も消さず、新しい Version を作らない）と 7 の 3 の却下（削除・復元ごとの新しい Version）。
- 8（編集は新しい Version と `supersedes` 関係、旧 Version は書き換えない。Optimistic Lock）。旧 Version を `superseded` にする変更が履歴に記録されることが加わるだけである。
- 11（Owner / Admin（人間）だけが管理する。Agent と `system` role は常に拒否）。
- 12 の Capability 6 つ（`shared_memory.create` / `edit` / `delete` / `restore` / `candidate.approve` / `candidate.reject`）、`Scope.SYSTEM`、委任不可、Audit Mode `REQUIRED`、閲覧は `shared_memory.manage` のまま。Authorizer の試みの行が変更の前に書かれること、書けなければ許可を拒否に変えること。12 の 5 の限界（削除・復元の理由は残らない。履歴も理由を持たない）。
- 13 の完了の行（変更と同じ Transaction、`decision = 'allow'`・`reason = 'completed'`、`correlation_id`、値の一覧、原子性、fail-closed、限界）。完了の行の値と書き方を変える場合は、これまでどおり新しい Decision から `Supersedes` する。
- 1〜6、9、10（Candidate、承認、System Policy との優先関係、Effective View）。

## 選定理由

- 2 つの記録は答える問いが違う。Audit は「誰が管理操作を試み、Commit されたか」で、Shared Memory の管理の操作だけを対象にする。`memory_metadata_changes` は「この Version の状態はいつ・誰によって変わったか」で、どの Scope の Version の `status` / `stale_since` の変更にも同じ規則で答える。
- 履歴を Trigger に置くので、書き込む側が省略できない（Table の Owner でない Application は Trigger を止められない）。Journal（PAW-041）や Stale の印付け、以降の競合処理は、Audit を持たない書き込みであり、Memory 側の記録がなければ Codex の指摘（履歴 / Graph が説明できない）は残る。
- 新しい Table・列・権限がなく、Pin / Importance と同じ仕組みで、PAW-040 の権限の設計（Version の列は更新できない）を変えない。
- Actor を必須にするのは、Pin / Importance の既存の規則と揃え、「誰が」の記録を誤らせないためである。

## 代替案

| 案 | 採らない理由 |
| --- | --- |
| 変更ごとに新しい Version を作る | 内容の同じ Version が並び、`active` の付け替えと Optimistic Lock（`expected_version`）が状態変化とぶつかる。Pin / Importance を新しい Version にしなかった理由（PAW-040）と、0009 の 7 の 3 の却下理由と同じ |
| `memory_versions` に `status_changed_by` / `status_changed_at` などの列を足す | 最後の 1 回の変更しか残らず、以前の値が消える。PAW-040 の列の権限（更新できる列を限る）を広げる。列の記録は Trigger で追記専用にした履歴ほど強く守れない（0009 の 12 の 4 と同じ） |
| Shared Memory の Scope は除外し、Audit だけに頼る | Scope で規則が分かれ、Shared 以外の Version と、Service を通らない Shared Memory の変更（保守作業など）が説明できない。Trigger は Scope を見ない方が単純で、迂回しにくい |
| 何も記録しない（Audit と、上書きされる現在の値だけ） | 要件（変更履歴は残す）と Review の指摘（P2）を満たさない。Shared Memory 以外の `deprecated` / Stale の印は Audit にも残らない |
| Actor が示されないときは `system` として記録する | Actor を示し忘れた書き込みほど「誰が」を誤って答える。Pin / Importance の規則（示さない変更は失敗）と揃わない |
| Status / Stale 専用の新しい Table（`memory_status_changes` など） | Table と権限が増え、Pin / Importance の履歴と 2 か所に分かれる。1 回の UPDATE で複数の列が変わっても 1 行にできる、今の Table の方が読みやすい |

## リスクと限界

- **同じ削除・復元の記録が 2 か所にある。** 決定 4 のとおり、対象・Actor・遷移が一致することが前提で、Test で確かめる。時計が別（Service と DB）なので、時刻は完全には一致しない。共通の Key はなく、結ぶのは Memory、Actor、時刻の近さである。
- **Actor を示し忘れた書き込みは、大きな音で失敗する。** UPDATE が取り消される。黙って誤った Actor を記録するよりよいが、新しい書き込み（Journal の統合など）が Actor を示さないと、その機能が止まる。Test（実 PostgreSQL）で見つかる。
- **Actor は主張である。** Backend が示した ID を DB は確認しない（決定 2 の 3）。Application の Role は履歴に INSERT できるので、Application 自体が偽の行を書けることは、Audit の行と同じである。
- **履歴は Version と一緒に消える。** Memory の物理削除（Cascade）で履歴も消える。Audit の行は残る。物理削除の経路は別 Issue で決める（0009 の 7 の 4）。
- **旧い行に Status がない。** Migration `0040` が書いた行（Pin / Importance だけ）は、当時の `status` を持たない。分からないことは NULL で表し、埋めない。
- **遷移の正しさは守らない。** この記録は起きた変更の記録で、`superseded` を `active` に戻さない等の許可ではない（Service が守る）。
- **理由は残らない。** 履歴は `change_reason` のような自由な文を持たない（0009 の 12 の 5 と同じ）。

## 以降の Issue への影響

- **`memory_versions.status` / `stale_since` を UPDATE するコードは、必ず Actor を示す。** 例: PAW-041 の Journal の統合が、新しい Version を書く前に旧 Version を `superseded` にする処理（`memory.journal.applier` の `_supersede`）と、その Test が Raw SQL で `status` を更新する箇所。Actor には、その統合を実行した主体（`system` または `agent`。どちらにするかは Decision 0018（Journal の統合の方針。PR #104 で提案中）の範囲）を、新しい Version の `actor_type` / `actor_user_id` と合わせて示すのが自然である。
- PAW-042（競合処理）、Stale の印を付ける・外す処理（鮮度の再確認）、PAW-045（Projection）が `status` / `stale_since` を書くときも同じ。
- 履歴を読む Query は、`memory_versions` を Join して `readable_memory_versions` で ACL を絞る（`memory_metadata_changes` の既存の規則）。
- Test の Seed が Raw SQL で `status` を更新するときは、Actor を示す（`tests/shared_memory_support.py`、`tests/test_retrieval_leakage.py` は対応済み）。

## 承認後の扱い

- 2026-09-26 に承認された。`Approval` に記録し、Status を Approved に改めた。[Decision 0009](0009-shared-memory-administration.md) は書き換えていない（承認済みの Decision は書き換えない）。0009 の該当箇所を読む人は、この Decision の「0009 との関係」で、変わった部分を確かめる。
- Migration `0071` と `SharedMemoryService` の実装は、この Decision を参照する（Docstring と README の「Status / Stale の変更履歴」「Audit の Action」）。
- 承認後に方針を変える場合（たとえば Actor が示されないときの扱い、Shared Memory の除外）は、この Decision を書き換えず、新しい Decision から `Supersedes` する。

## 承認時の決定（2026-09-26）

- Humanは、Auditと Memory 側の履歴（`memory_metadata_changes`）の併存を、推奨どおり承認した（新しい Decision で 0009 の該当部分を `Supersedes`。実装は変えない。Actor は必須で、黙って `system` にしない）。
