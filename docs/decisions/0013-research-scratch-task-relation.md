# Research Scratch の Task との関係（`task_id`）の方針

- Status: Proposed
- Date: 2026-09-25
- Scope: PAW-050（Research Scratch Store）の `research_scratch_items.task_id`（Migration `0050`）と、Item を Task に結びつける以降の Issue（PAW-051 / 052 など）
- Supersedes: なし
- Approval: 未承認（Humanの承認待ち）

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
それでも、その経路が起きたときの規則を Schema が決めてしまうため、提案として一覧にする。

**この Decision は Proposed であり、Human の承認を得ていない。** 承認されるまで、次の選択は暫定である。
Migration `0050` はまだ Merge されていないため、承認された内容が違う場合は `0050` を直接直せる（Merge 後は新しい Migration になる）。

## 提案

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

- 承認前でも守りたい保証は、Task の削除を止めないこと、Pin 済みを失わないこと、期限切れは消えること、関係を保つことの 4 つ。これを同時に満たす Foreign Key の `ON DELETE` は無い（下の表）。
- 関係の保持は PAW-050 の受け入れ条件で、削除の延期は要件の本文である。Task の削除は稀で、Task 側の履歴（`task_events` など）は削除できない作りなので、調査結果を Task の削除に従属させない方を選んだ。
- `project_id` は同じ理由（参照先がまだ無い、参照先の削除の方針が未定）で素の UUID にしており、`task_id` も同じ扱いにすると Table の中で一貫する。

## 代替案

| 案 | Task の削除を止めない | Pin 済みを失わない | 関係を保つ | 採らない理由 |
| --- | --- | --- | --- | --- |
| Foreign Key + `SET NULL`（最初の実装） | はい | はい | いいえ | Task の削除で `task_id` が消え、Review が指摘したとおり受け入れ条件の「Project/Task relation保持」を満たさない |
| Foreign Key + `RESTRICT` | いいえ | はい | はい | 行が残る間は Task を削除できない。期限切れで未 Purge の行は Janitor の間隔だけ、Pin 済みは無期限に、削除を止める |
| Foreign Key + `CASCADE` | はい | いいえ | いいえ | Pin 済み・使用中・昇格確認中の Item まで消え、要件の削除の延期を破る |
| Foreign Key を残し、Task の削除の前に別処理で Item を切り離す | はい | はい | いいえ | 切り離すと関係が消える。Backend 以外の削除経路では失敗する |
| 素の UUID（提案） | はい | はい | はい | DB が存在を保証しない（提案 3、4） |

- **Task を論理削除にして Foreign Key を保つ**: DB が存在を保証したまま 4 つの性質も満たせる。ただし Task 側の Schema と方針の変更で、PAW-050 の範囲を超える。採られたら、素の UUID を Foreign Key に戻す新しい Decision で足りる。

## 承認後の扱い

- 承認されたら、`Approval` に記録して Status を Approved に改める。
- `RESTRICT` など別の選択が承認された場合は、Migration `0050`（未 Merge のうち）と Model、`test_scratch_schema.py`、`test_scratch_migration.py`、`test_scratch_purge.py` の Task 削除の Test を、承認された選択に合わせる。
- 承認されるまで、Item の `task_id` が存在する Task を指すことを前提にした処理（`tasks` との JOIN を必須にするなど）を作らない。
