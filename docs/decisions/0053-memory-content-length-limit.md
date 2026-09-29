# Memory の本文（`memory_versions.content`）の DB の長さの上限と、上限を超える既存の行の扱い

- Status: Proposed
- Date: 2026-09-29
- Scope: Issue [#147](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/147)（Memory の本文に DB の長さの上限を入れる）。Migration `0147`（`ck_memory_versions_content_length`）
- Supersedes: なし。[Decision 0038](0038-memory-markdown-projection.md) の 5 の「`memory_versions.content` には長さの上限がない」という前提を、この Decision が補う（0038 は書き換えない。5・10 の切り詰めの処理と表示は変えない）

## 背景

PR #138（PAW-045、Decision 0038）の「決めてほしいこと」の 10 について、Human は 2026-09-28 に推奨どおり承認した: 1,000,000 文字（`MAX_TEXT_CHARS`）を超える本文は先頭だけを投影し、Front Matter の `truncated: true` と Audit の `truncated=N` で示し、実行は成功のままとする。あわせて Human は、切り詰めが起こり得ること自体が本文の長さが無制限であることに由来するとして、DB の側で本文の長さに上限を設ける作業を Issue #147 とした。

現状:

- `memory_versions.content` の CHECK は `char_length(content) >= 1`（`content_not_empty`、Revision 0040）だけで、上限はない。
- Version を書く Service はどれも本文の長さを先に検査している: 手動編集（`memory/versioning/limits.py` の `MAX_CONTENT_CHARS = 20_000`、`MemoryDraft` / `MemoryChanges`）、Shared Memory の管理と候補（`memory/shared/limits.py` の `MAX_CONTENT_CHARS = 20_000`。候補の表 `shared_memory_candidates` には同じ値の CHECK がある、Revision 0046）、Immediate Journal（`memory/journal/limits.py` の `MAX_CONTENT_CHARS = 8_000`、Decision 0018）。超えた本文は `MemoryInputError`（`InputProblem.TOO_LONG`）で拒否される。Memory を書く HTTP API はまだない（API ができたときは、この入力の誤りを 422 に写す）。
- Version は書き換えない（Application の Role は `content` を UPDATE できない、Revision 0040 の Grant）。上限を超える行が既にあるとすれば、Owner の権限で直接書かれた行だけである。

Issue #147 は「既存の行に上限を超えるものがある場合の扱い（Migration を失敗させる／事前の確認 Command を用意する等）を決める」ことを求めている。これはデータを変えるかどうかの選択で、どの承認済み Decision にも書かれていない（AGENTS.md 1.8 / 14 により実装が勝手に確定しない）。この Decision で提案する。実装（このブランチ）は下の推奨どおりである。

## 提案

### 1. 上限を超える既存の行: Migration を止め、何も変えない

**推奨（案 1A）: Migration `0147` は、CHECK を加える前に `char_length(content) > 20000` の行を数える。1 行でもあれば、件数・先頭 5 件の Version の ID・全件を一覧する SQL を示すエラー（`check_violation`）で止まり、Schema もデータも変えない。行は Human が手で片付け（下の「手当」）、Migration をもう一度実行する。**

エラーの形（実際の出力）:

```text
revision 0147: 1 memory_versions row(s) have content longer than 20000 characters (first ids: <uuid>); nothing was changed
HINT:  Decision 0053: list them with: SELECT id, memory_id, status, char_length(content) FROM memory_versions WHERE char_length(content) > 20000 ORDER BY memory_id, id; deal with them by hand, then run the upgrade again.
```

- データを失わない: 本文の正本は PostgreSQL にしかない（Projection は写しで、1,000,000 文字を超えれば切られている）。Migration が本文を切ると、その部分は Backup にしか残らない。
- Version は書き換えないという約束（Decision 0034、Revision 0040 の Grant）を Migration が破らない。
- 通常の書き込みの経路はどれも 20,000 文字以下しか書かないので、止まるのは Owner の権限で直接書かれた行があるときだけで、そのときは人が見るべきである。
- CHECK は Validate した状態で加える（`NOT VALID` にしない）。Migration が通れば、全ての行が上限の内にある。

手当（この Decision は選ばない。Human が行ごとに決める）: 本文の全体を退避したうえで、Owner の権限でその Memory を削除する（Version・Relation・Source は Cascade で消える）か、Owner の権限で本文を短くする（Version を書き換えることになるので、理由を記録する）。

検討した他の案:

- 案 1B「切り詰めて印を付ける」（先頭 20,000 文字に切り、`attributes` 等に `truncated` を記録）: 本文を失い、書き換えない Version を Migration が書き換え、その変更は Audit にも `memory_metadata_changes` にも残らない。却下。
- 案 1C「`NOT VALID` で加える」（新しい行だけに効かせ、既存の行は残す）: PostgreSQL は `NOT VALID` の CHECK も UPDATE された行には検査するので、上限を超える行の `status`・`pinned`・`importance`・`stale_since` の UPDATE（置き換え・固定・鮮度の Job）が全て `check_violation` になり、その Memory は編集も置き換えもできなくなる。Schema の上でも「全ての行が上限の内」と言えない。却下。
- 案 1D「事前の確認 Command を別に用意する」: Migration 自身が同じ検査をして何も変えずに止まるので、別の Command は同じ SQL の写しになる。事前に確かめたい Operator は HINT の SQL（この Decision にも書いた）を読み取りだけで実行できる。今は作らない。

### 2. Projection の切り詰め（Decision 0038 の 5・10）は防御として残す

**推奨: `memory/projection/render.py` の切り詰め（`MAX_TEXT_CHARS` を超える文字列を先頭だけにし、`truncated: true` と Audit の `truncated=N` で示す）は変えずに残す。**

- 上限が入った後、保存された Version の本文（20,000 文字）と題名（200 文字）は `MAX_TEXT_CHARS`（1,000,000 文字）に届かないので、この処理は通常は起こらない。
- 残す理由: 将来 上限を上げる Migration があっても、検査していない部分を Recovery Repository（Git）へ出さない保証が Projection の側で保たれる。処理と Test はすでにあり、残す費用は小さい。
- 取り除く案（上限があるので不要な分岐を消す）は、上の保証を上限の値に結び付けることになるので採らない。
- `render.py` の説明にこの関係（0147 以後は起こらない・防御として残す）を書いた。

### 3. 上限の値と、値を置く場所

**推奨: 上限は 20,000 文字（Issue #147 と Human の指示のとおり、手動編集と Shared Memory の Service の上限と同じ）。Schema の値は `paw_backend/memory/models.py` の `MAX_VERSION_CONTENT_CHARS` と、Migration `0147` に書き出した数（Migration は書いたときの値を保つ）。2 つの Service の `MAX_CONTENT_CHARS` はそれぞれ残し、4 つの値が食い違えば Test（`tests/test_memory_content_length_migration.py`）が失敗する。Immediate Journal の 8,000 文字は変えない（より厳しい）。**

- 上限を変えるときは、新しい Migration と、この Decision を `Supersedes` する新しい Decision で行う。
- CHECK を加えるときの `ALTER TABLE ... ADD CONSTRAINT` は表を `ACCESS EXCLUSIVE` で Lock して全ての行を読む。この Workspace の規模（1 人〜少人数の Memory）では短い。表が大きくなってから上限を変えるときは、`NOT VALID` で加えて `VALIDATE CONSTRAINT` する 2 段の形を、その Migration で検討する。

## リスク

- 上限を超える行があると、その環境の `alembic upgrade head` は 0147 で止まり、後続の Revision も適用されない。エラーは件数と一覧の SQL を示すので、手当の後にもう一度実行すれば進む。
- 20,000 文字を超える本文を Memory に残したい用途（長い手順書など）は、Memory を分けるか、Research Scratch（`research/scratch/limits.py` の 100,000 文字）等の別の置き場所を使うことになる。これは Service の上限としてすでにそうである。

## 決めてほしいこと

1. **上限を超える既存の行**: Migration `0147` は、上限を超える行があれば件数・先頭 5 件の ID・一覧の SQL を示して止まり、何も変えない（案 1A）。切り詰め（1B）・`NOT VALID`（1C）・別の確認 Command（1D）は採らない。手当は Human が行ごとに行う。**推奨: 1A。**
2. **Projection の切り詰め**: Decision 0038 の 5・10 の切り詰めと表示は、上限の後も防御として残す。**推奨: 残す。**
3. **上限の値と置き場所**: 20,000 文字。Model の `MAX_VERSION_CONTENT_CHARS` と Migration の数を Schema の値とし、2 つの Service の上限との食い違いを Test で検出する。Journal の 8,000 文字は変えない。**推奨: このとおり。**
