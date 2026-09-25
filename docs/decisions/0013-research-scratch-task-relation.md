# Research Scratch の Task との関係（`task_id`）の方針

- Status: Approved
- Date: 2026-09-25
- Scope: PAW-050（Research Scratch Store）の `research_scratch_items.task_id` と、削除の延期の印 `pinned` / `saved`（Migration `0050`）、Item を Task に結びつける以降の Issue（PAW-051 / 052 など）。「決定」は `task_id`、後半の「追加の決定: Pin と明示保存を別の印にする」は `pinned` / `saved` の扱い
- Supersedes: なし
- Approval: 2026-09-25、Humanが作業Session内で、判断メモ（Artifact）の各点について「推奨どおり」と回答して承認（末尾の「承認時の決定」）

## 背景

[REQUIREMENTS.md](../../REQUIREMENTS.md) の「Research Scratch」は、保持項目に「Project / Task relation」を挙げ、TTL 削除を延期できる場合として
「実行中Taskが参照中」「Pin済み」「Memory昇格確認中」「Userが明示保存」を挙げる。
[Implementation Backlog](../IMPLEMENTATION_BACKLOG.md) の PAW-050 の受け入れ条件にも「Project/Task relation保持」がある。
一方で、**Task を削除したときにこの関係をどうするかは、要件にも Backlog にも書かれていない。**

PAW-050 の最初の実装は、`task_id` を `tasks.id` への Foreign Key（`ON DELETE SET NULL`）にした。
独立 Review（Codex）は、Task を削除すると `task_id` が消え、Pin 済みなどの削除を延期した調査結果が Task との関係を失う（受け入れ条件に反する）と指摘した。
Review は、関係を保つ規則（たとえば `RESTRICT`）にするか、Decision の承認を得てから `SET NULL` にするよう求めた。
[AGENTS.md](../../AGENTS.md) の「仕様変更」は、重要判断を `docs/decisions/` に提案して人間 / Admin の承認を得ると定める。

現状の Task は、実際にはほとんど削除されない。Migration `0032` は DELETE をどの Role にも与えず、`task_events` は DB の Trigger が削除を拒否する。
したがって Task の削除は、Admin の保守作業や将来の Project 削除などの経路に限られる。
それでも、その経路が起きたときの規則を Schema が決めてしまうため、選択として一覧にして Human が承認した。

**この Decision は 2026-09-25 に Human が承認した（Approved）。** 下の各選択は、承認された方針である（承認時の決定は末尾を参照）。
承認の時点で Migration `0050` はまだ Merge されていない。Merge 後に方針を変える場合は、新しい Decision から `Supersedes` し、新しい Migration で直す。

## 決定

### 1. `task_id` は Foreign Key を持たない素の UUID にする

- `research_scratch_items.task_id` は NULL 可の UUID で、`tasks.id` への Foreign Key を持たない（`project_id`、`created_by` と同じ）。`ix_research_scratch_items_task_id` の Index は残す。
- Item に Foreign Key が残るのは、Lease から Item への `ON DELETE CASCADE` だけになる。

### 2. これで保たれる 4 つの性質

1. **Task の削除は、調査結果に止められない。** `research_scratch_items` の行は、Task の削除の妨げにならない（`RESTRICT` にしないこと）。
2. **Pin 済み・使用中（Lease）・昇格確認中の Item は、Task の削除で失われない。** 削除の延期は Task と独立に働く（`CASCADE` にしないこと）。
3. **期限切れの削除は従来どおり。** Task が消えていても、期限切れで延期の理由がない Item は Janitor が消す。TTL は延びない。
4. **Item は Task との関係を保つ。** Task が削除された後も `task_id` は書かれたままで、`ScratchStore.list_items(project_id, task_id=...)` と `get` で読める（`SET NULL` にしないこと）。

### 3. Task の存在確認は `ScratchStore.add` が行う

- `add` は、同じ Transaction で Task の行を `FOR KEY SHARE` で Lock し、Task が存在して同じ Project に属することを確認してから Item を書く。存在しない Task と他の Project の Task は区別しない。
- Lock は、確認と Insert の間に Task が削除されないためのもの。Insert の後の Task の削除は待たされず、Item も変わらない。
- **DB は `task_id` の存在を検査しない。** `add` を通らずに書かれた行（保守用の SQL など）は、存在しない Task を指せる。Application のコードが Item を書く経路は `add` だけで、`task_id` は書いた後に変更できない（列単位の UPDATE 権限に含まれない）。ただし Role の INSERT 権限は値を制限しない。

### 4. 削除された Task を指す `task_id` は、読む側が許容する

- Task が削除された後、`task_id` は存在しない Task を指す。表示や集計をする側は、Task が見つからないことを許容する（Item はそのまま読める）。
- Project の Table が入ったとき、または Task の削除の方針（物理削除か論理削除か）が決まったときに、`project_id` と合わせてこの選択を見直す。その場合は新しい Decision から `Supersedes` する。

## 選定理由

- 守りたい保証は、Task の削除を止めないこと、Pin 済みを失わないこと、期限切れは消えること、関係を保つことの 4 つ。これを同時に満たす Foreign Key の `ON DELETE` は無い（下の表）。
- 関係の保持は PAW-050 の受け入れ条件で、削除の延期は要件の本文である。Task の削除は稀で、Task 側の履歴（`task_events` など）は削除できない作りなので、調査結果を Task の削除に従属させない方を選んだ。
- `project_id` は同じ理由（参照先がまだ無い、参照先の削除の方針が未定）で素の UUID にしており、`task_id` も同じ扱いにすると Table の中で一貫する。

## 代替案

| 案 | Task の削除を止めない | Pin 済みを失わない | 関係を保つ | 採らない理由 |
| --- | --- | --- | --- | --- |
| Foreign Key + `SET NULL`（最初の実装） | はい | はい | いいえ | Task の削除で `task_id` が消え、Review が指摘したとおり受け入れ条件の「Project/Task relation保持」を満たさない |
| Foreign Key + `RESTRICT` | いいえ | はい | はい | 行が残る間は Task を削除できない。期限切れで未 Purge の行は Janitor の間隔だけ、Pin 済みは無期限に、削除を止める |
| Foreign Key + `CASCADE` | はい | いいえ | いいえ | Pin 済み・使用中・昇格確認中の Item まで消え、要件の削除の延期を破る |
| Foreign Key を残し、Task の削除の前に別処理で Item を切り離す | はい | はい | いいえ | 切り離すと関係が消える。Backend 以外の削除経路では失敗する |
| 素の UUID（採用） | はい | はい | はい | DB が存在を保証しない（決定 3、4） |

- **Task を論理削除にして Foreign Key を保つ**: DB が存在を保証したまま 4 つの性質も満たせる。ただし Task 側の Schema と方針の変更で、PAW-050 の範囲を超える。採られたら、素の UUID を Foreign Key に戻す新しい Decision で足りる。

## 承認後の扱い

- 2026-09-25 に承認された。`Approval` に記録し、Status を Approved に改めた。
- 承認後に別の選択（`RESTRICT` など）へ変える場合は、この Decision を書き換えず、新しい Decision から `Supersedes` する。Migration `0050` が未 Merge のうちは、`0050` と Model、`test_scratch_schema.py`、`test_scratch_migration.py`、`test_scratch_purge.py` の Task 削除の Test を、その選択に直接合わせられる（Merge 後は新しい Migration になる）。
- Item の `task_id` が存在する Task を指すことを前提にした処理（`tasks` との JOIN を必須にするなど）は作らない（決定 4）。

## 追加の決定: Pin と明示保存を別の印にする

同じ Decision（同じ Migration `0050`）に、Review（PAW-050、6 回目）の指摘への対応として追記した。`task_id` の決定とは独立した決定で、どちらも 2026-09-25 に Human が承認した。

### 背景

[REQUIREMENTS.md](../../REQUIREMENTS.md) の「Research Scratch」は、TTL 削除を延期できる場合として「Pin済み」と「Userが明示保存」を**別の項目**として挙げる。
最初の実装は、この 2 つを 1 つの真偽値 `pinned` で表していた（README は、別の状態にするかを「人間の判断が必要な点」に残していた）。
Review は、Item が「一時的な Pin」と「User の明示保存」の両方の理由で残っているとき、後の `unpin` が保存まで消し、期限切れの Item が削除されると指摘した。
2 つの延期理由は独立でなければならないため、実装が暗黙に固定せず、この Decision に記録して Human が承認した。

### 決定

1. **2 つの独立した印を持つ。** `research_scratch_items` に `pinned`（一時的に残す）と `saved`（User の明示保存）を、どちらも `boolean NOT NULL DEFAULT false` で持つ。片方から片方を導かない。
2. **各操作は自分の印だけを変える。** `pin` / `unpin` は `pinned` だけ、`save` / `unsave` は `saved` だけ。`unpin` は保存を消さず、`unsave` は Pin を消さない。どちらも冪等で、Item の行を Lock してから、自分の列だけの `UPDATE` を 1 つ実行する（同時の変更で他方の印を失わない）。
3. **Purge と TTL の失効は、どちらかが立っている間 Item を残す。** 削除の延期は、Pin 済み、保存済み、Memory 昇格の確認中、使用中（Lease）のどれか 1 つでもあれば有効になる。全てが終わり TTL が過ぎた Item を、次の `purge_expired` が削除する。TTL（`expires_at`）は延びない。`ix_research_scratch_items_purgeable`（削除候補の部分 Index）は `NOT pinned AND NOT saved AND promotion_state <> 'pending'` を条件にする。
4. **Application Role の権限は最小のまま。** `research_scratch_items` の UPDATE の列に `saved` を 1 つ足すだけ（`pinned`、`saved`、`promotion_state`、`promotion_requested_at`）。
5. **API の互換性。** `pin` / `unpin`、`ScratchItem.pinned`、`DeferralReason.PINNED` は変えない。追加は `save` / `unsave`、`ScratchItem.saved`、`DeferralReason.SAVED` で、`deferral_reasons` の順は `pinned`、`saved`、`in_use`、`promotion_pending`。

### 選定理由と代替案

| 案 | 情報を失わない | 採らない理由 |
| --- | --- | --- |
| 1 つの `pinned`（最初の実装） | いいえ | `unpin` が保存を消す（Review の指摘） |
| `pinned` と `saved` の 2 つの真偽値（採用） | 誰が・いつは失う | 最小。要件の 2 項目に 1 対 1 で対応し、既存の `pin` / `unpin` の意味を変えない |
| 参照の Table（Item ごとに `kind`、`holder` など） | はい | 誰が・いつ・複数の保存者を持てるが、Table・権限・Lease との整合が増える。要件は誰が保存したかを求めていない |
| `saved` に応じて TTL を延長する | — | TTL は `created_at + 24 hours` の CHECK 制約で、削除の延期は TTL の延長ではない（既存の方針） |

### 判断が必要だった点と、承認された内容

- **見え方。** 保存と Pin を UI や一覧で今は区別しない。API では `DeferralReason.PINNED` / `SAVED` で区別できる。UI・一覧の設計は Task 表示など以降の Issue で決める（要件は「Userが明示保存」した Item を、一時的な Pin とは別の扱いにするとは述べていない。現在の Store は、どちらも「TTL を過ぎても見える」だけで区別しない）。
- **Quota。** 保存済み・Pin 済みの Item の数の上限は、今は持たない（要件に数値が無く、実運用の数も分かっていない）。
- **誰が付け外しできるか。** `save` / `unsave` は **User 本人だけができる操作**で、Agent へ委任できない。要件の「Userが明示保存」は人の意思表示であり、Agent が調査結果を TTL から免れさせられないようにするため。`pin` / `unpin` は従来どおり、README の「呼び出し側の認可（提案）」の暫定 Capability `project.task.run`（Agent へ委任できる）とする。
  現在の Store は認可をしない。呼び出し側（API 層、以降の Issue）が、`save` / `unsave` を Agent の権限（委任元 User と `AgentGrant` の積集合）では呼べない経路にする。専用の Capability を新設するかと、その id はこの Decision では決めない。新設するときは [Decision 0004](0004-rbac-capability-and-audit-policy.md) に従い、委任不可を明示する。
- **記録。** 誰が・いつ保存したか（`saved_by`、`saved_at`）は、今は記録しない（要件は誰が保存したかを求めていない）。1 人の `unsave` で保存の印が消える。記録するなら、上の「参照の Table」案に移る。
- **保存の期限。** 保存に最大の期間は付けない。`unsave` まで無期限（Pin も同じ）。

### 承認後の扱い

- 2026-09-25 に承認され、`Approval` に記録して Status を Approved に改めた（`task_id` の決定と同時）。
- 承認後に別の選択（参照の Table など）へ変える場合は、この Decision を書き換えず、新しい Decision から `Supersedes` する。Migration `0050` が未 Merge のうちは、`0050` と Model、`test_scratch_saved.py`、`test_scratch_migration.py`、`test_scratch_grants.py` を、その選択に直接合わせられる。

## 承認時の決定（2026-09-25）

- 本文の各点を、提案どおり承認した。
- `task_id` は Foreign Key のない素の UUID とする（「決定」1〜4）。
- Pin と明示保存は、独立した 2 つの印（`pinned` / `saved`）とする（決定済みの事項。「追加の決定」）。
- 保存と Pin は、UI・一覧で今は区別しない。
- Quota（保存済み・Pin 済みの件数の上限）は、今は持たない。
- `save` / `unsave` は User 本人だけができる操作とし、Agent へ委任できない。`pin` / `unpin` は従来どおり、README の暫定 Capability `project.task.run`。Store 自体は認可をしないので、API 層が強制する。README の「呼び出し側の認可（提案）」と `ScratchStore` の Docstring に反映した。
- 誰がいつ保存したかは、今は記録しない。1 人の `unsave` で保存の印が消える。
- 保存に最大の期間は付けない。
