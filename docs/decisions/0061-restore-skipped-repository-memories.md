# Restore で飛ばした Repository の Repo 単位の記憶を戻さず、一覧にして手作業の手順にする

- Status: Approved
- Approval: 2026-09-30、Humanが作業Session内で、判断点ごとの説明（推奨つき）を受けたうえで直接回答して承認（判断が必要な点 1〜5 の全点。末尾の「承認時の決定」）
- Date: 2026-09-30
- Scope: Issue [#171](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/171)（PR #160 の残りの P2）。`apps/backend/paw_backend/recovery/restore.py`、`apps/backend/paw_backend/recovery/audit.py`。関連: [Decision 0054](0054-recovery-repository-projection-restore.md)（Recovery Repository の Backup と Restore。3・9・12）
- Supersedes: [Decision 0054](0054-recovery-repository-projection-restore.md) のうち次の点だけ（それ以外は変えない）: 9 の「戻す」の「Memory・全 Version・Relation・`conversation` 以外の Source」（飛ばした Repository の Repo 単位の記憶を除く）と「戻さない」の一覧（その記憶を足す）、12 の `recovery.restore.planned` / `applied` の `reason` の形式（件数を足す）

## 背景

Decision 0054 3・9 により、名前や既定の Branch に Credential が見つかって `[REDACTED]` に置き換えた Repository は、有効な名前で戻せないため Restore が飛ばし、再登録を手作業として表示する。
ところが、その Repository を指す Repo 単位の記憶（`memory_versions.scope = 'repo'`、`repo_id` がその Repository）は、古い `repo_id` のまま戻っていた。`repo_id` には外部キーがないので Restore は通るが、Repository を手で登録し直すと新しい ID になり、その記憶には誰も届かなくなる（ACL は Repository を経由する）。

Human は 2026-09-30 に作業 Session で次の方針を直接示した（Issue #171 の「Human の決定」）。

- 飛ばした Repository の Repo 単位の記憶（Version・Relation・Source を含む）は、Restore で**戻さない**。
- 代わりに、Dry run と Apply の両方で、その記憶を一覧として出す（Repository の伏せ字の名前、記憶の ID、件数）。
- 一覧は、Restore の後に行う手作業の手順として表示する。
- 方針は新しい Decision に記録し、Decision 0054 の該当点を `Supersedes` する（0054 は書き換えない）。

この Decision はその方針を提案として記録し、方針が決めていない細部（どの記憶を「Repo 単位」とするか、戻す記憶との間の Relation、Audit の形式）を推奨つきで示す。PR は推奨どおりに実装している。

## 提案

### 1. 戻さない記憶: 飛ばした Repository を指す Version を 1 つでも持つ記憶の全体

- `memory-records/<id>.json` のどれかの Version が `scope = 'repo'` で、その `repo_id` が Restore の飛ばした Repository であれば、**その記憶の全体**（すべての Version・その Version の Source・その記憶の Relation）を戻さない。
- 記憶の一部の Version だけを戻すと、`version_number` の連番と履歴（Relation）が欠けた記憶になる。記憶は 1 つの Identity なので、まとめて戻さない。
- `conversation` の Source（元から戻さないもの）も、戻さない記憶の Source として数える（`conversation` の Source の件数の表示には含めない。二重に数えない）。
- 飛ばさなかった Repository、Backup にない Repository を指す記憶の扱いは変えない（Decision 0054 のまま）。

### 2. 戻す記憶と戻さない記憶の間の Relation: 戻さず、件数に数える

- Relation の両端が戻す Version なら戻す。片端でも戻さない記憶の Version なら、その Relation は戻さず、その記憶の Repository の件数に数える。どちらでもない Relation（戻さない理由のない Version を指す）はこれまでどおり `record_invalid` で拒否する。
- 戻す記憶の側の Relation を理由に Restore 全体を拒否すると、飛ばした Repository が 1 つあるだけで Workspace を戻せなくなる。

### 3. 一覧: Dry run と Apply の手作業の手順に、Repository ごとに 1 行

- 戻さない記憶がある Repository ごとに、手作業の手順（`manual_steps`。Dry run では「After --apply:」、Apply では「Then:」の下）に次を 1 行で出す: **伏せ字の名前**（Backup のまま。元の Credential は Backup にない）、**Backup の上の Repository の ID**（同じ伏せ字の名前の Repository を区別する）、**記憶の件数と ID の一覧**、**Version・Relation・Source の件数**、Repository を登録し直した後に Backup の `memory-records/<id>.json` から必要なものを作り直すこと。
- 既存の「Repository と Remote を登録し直す」の行（件数）はそのまま残す。記憶のない飛ばした Repository には一覧の行を出さない。
- Credential は表示に出ない（名前は伏せ字の後の値、他は ID と件数だけ）。

### 4. Audit: 飛ばした Repository があるときだけ、件数を `reason` に足す

- `recovery.restore.planned` / `recovery.restore.applied` の `reason` は、Repository を飛ばしたときだけ、これまでの件数の後に `skipped_repos=N held_memories=N held_versions=N held_relations=N held_sources=N` を足す（`memories=` などはこれまでどおり**戻す**件数）。飛ばした Repository がなければ `reason` はこれまでと同じ。
- 足すと 64 文字を超えるので、Recovery の Audit の `reason` の上限（コードの切り詰め。列は `text` で制約はない）を **64 から 256 文字**にする。Migration は要らない。ID・名前・Path は `reason` に書かない（Decision 0054 12 のまま）。

### 5. 自動で付け直さない

- Repository を登録し直した後に、戻さなかった記憶を新しい ID で自動的に戻す仕組み（Mapping の入力、後からの Import）は作らない。必要な記憶は人が作り直す。Recovery Import の後の Merge を V1 で作らない Decision 0054 7 と同じ理由。

## 選定理由

- 誰も届かない記憶を古い ID で DB に残すと、ACL・検索のどれからも見えず、消すこともできないまま Data が残る。戻さずに一覧にすれば、何が戻らなかったかを運用者が確かめ、必要なものを新しい Repository の下に作り直せる。
- Dry run でも一覧を出すので、`--apply` の前に件数と記憶を確かめられる。

## 代替案

- **古い `repo_id` のまま戻す（これまで）**: 誰も届かない記憶が残る。採らない（Human の方針）。
- **`scope` を `project` などに変えて戻す**: 読める人の範囲が変わる（Repository の ACL の上書きを失う）。採らない。
- **飛ばした Repository を指す Version だけを戻さない**: 記憶の履歴が欠ける。採らない（1）。
- **戻す記憶から戻さない記憶への Relation があれば Restore 全体を拒否する**: 飛ばした Repository が 1 つで Restore できなくなる。採らない（2）。
- **Audit の `details`（JSONB）に記憶の ID を入れる**: Recovery の Audit は `reason` の件数だけとしている（Decision 0054 12）。ID は表示で足りる。採らない。

## リスク

- 戻さなかった記憶は、人が作り直すまで使えない。元の Secret を含んでいた部分は Backup でも `[REDACTED]` のまま。
- 一覧は Command の出力にだけ出て、DB と Audit には件数しか残らない。運用者は出力を保存する（README の手順）。
- 記憶が非常に多いと、1 行が長くなる。

## 決めてほしいこと

推奨の答えを添えて Human / Admin に問う（Human が 2026-09-30 に示した方針は前提とし、細部だけを問う）。

1. **戻さない記憶の範囲**: 飛ばした Repository を指す `repo` Scope の Version を 1 つでも持つ記憶は、全体（すべての Version・Source・Relation）を戻さない。
   推奨: 提案どおり。
2. **戻す記憶との間の Relation**: 片端が戻さない記憶の Relation は戻さず、その Repository の件数に数える（Restore 全体は拒否しない）。
   推奨: 提案どおり。
3. **一覧の形**: Repository ごとに 1 行、伏せ字の名前・Backup の上の Repository の ID・記憶の件数と ID・Version / Relation / Source の件数・作り直しの案内。Dry run と Apply の両方の手作業の手順に出す。
   推奨: 提案どおり。
4. **Audit**: Repository を飛ばしたときだけ `reason` に `skipped_repos=N held_memories=N held_versions=N held_relations=N held_sources=N` を足し、Recovery の `reason` の上限を 64 から 256 文字にする。
   推奨: 提案どおり。
5. **自動の付け直し**: 作らない（人が作り直す）。
   推奨: 提案どおり。

## 承認後の扱い

- 承認前も、PR は推奨どおりに実装して入り得る（Restore の既定は Dry run のまま）。承認されない点があれば別の PR で直す。
- 方針を変える場合は、この Decision を書き換えず、新しい Decision から `Supersedes` する。

## 承認時の決定（2026-09-30）

Human は、作業 Session で判断が必要な点について推奨つきの説明を受け、直接回答して承認した（判断が必要な点 1〜5 の全点）。方向（飛ばした Repository の記憶は戻さず、一覧にして手作業の手順にする）は 2026-09-30 に先に直接決定しており、実装で詳しく決めた 1〜5 もすべて推奨どおり。
