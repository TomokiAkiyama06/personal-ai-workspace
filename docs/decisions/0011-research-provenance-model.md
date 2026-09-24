# Research の Evidence / Claim Provenance の方針

- Status: Proposed
- Date: 2026-09-24
- Scope: PAW-052（Evidence / Claim Provenance）と、Claim・Source を使う以降の Issue（Research Orchestrator、回答の引用表示、Memory Candidate への昇格など）
- Supersedes: なし
- Approval: 未承認（Humanの承認待ち）

## 背景

[REQUIREMENTS.md](../../REQUIREMENTS.md) の「Evidence / provenance」は、Research 結果が Claim と Source の対応を保持し、
claim、source id、source type、fetched_at、published_at、confidence、task / project の関係を持つと定める。
Source type だけでは真偽を決めない、とも書く。
一方で、次のことは文書では決まらず、PAW-052 の実装が選んだ。
Product Policy を実装が暗黙に確定させないよう、選択を一覧にして Human が承認または変更できるようにする。

実装は [Backend README](../../apps/backend/README.md) の「Evidence / Claim Provenance」にある。
**この Decision は Proposed であり、Human の承認を得ていない。** 承認されるまで、次の選択は暫定である。
値や規則の多くは `paw_backend/research/provenance/` の定数と関数で、変えても Schema の変更が要らないものが多い（要るものは各項に書く）。

## 提案

### 1. Source の同一性と不変性

1. Source は `(project_id, 正規化した locator, content_hash)` で 1 件に決まる。同じ URL でも本文の Hash が違えば別の Source（取得した時点の内容を指すため）。
2. Source は **書き換えない**。同じ Source をもう一度記録しても、最初の `fetched_at`、`published_at`、`source_type`、`title` が残る。
3. 本文は保存しない。持つのは Hash だけ（`content_hash` は取得した Excerpt / 文書の Hash で、遠隔の文書全体とは限らない）。
4. `private_source`、Provider の種類と ID、License は記録しない（要件に定義がない。Privacy Filter は PAW-053）。

### 2. Claim の同一性と重複

1. Claim は `(project_id, 正規化した本文の Fingerprint)` で 1 件に決まる。正規化は NFKC、大文字小文字の畳み込み、空白の連続を 1 つの空白にすること。
2. 同じ Claim をもう一度記録すると、**新しい Claim を作らず既存の Claim に Source を足す**（最初の本文、作成者、Task、時刻が残る）。
3. 表現が違う近い Claim は、自動では同じにしない。`duplicate` の Relation で人間 / Agent が明示する。
4. 本文は 2000 文字まで。

### 3. Claim と Source の対応（Stance）

1. 対応は `supports`（支持）か `contradicts`（反する）。1 つの `(claim, source)` に Stance は 1 つ。
2. 同じ組に逆の Stance を記録すると `ProvenanceConflictError`（上書きしない）。同じ Stance の再記録は何もしない。
3. Claim に付く Source は 50 件まで。1 回の記録は 20 件まで。Claim には 1 件以上の Source が要る（Source のない Claim は Provenance ではない）。

### 4. duplicate / contradiction の表現

1. Claim 同士、Source 同士に、対称な Relation `duplicate`（同じことを言う）と `contradiction`（両立しない）を張れる。
2. 1 組に Relation は 1 つ。逆の種類を張ると `ProvenanceConflictError`。推移律の閉包は取らない（A と B、B と C が duplicate でも、A と C は張らない限り無関係）。
3. Source の Relation は「別 URL の同じ内容（ミラー）」や「互いに矛盾する 2 つの記事」を表すためで、どちらが正しいかは決めない。

### 5. 回答・Task からの追跡

1. 参照元は Answer か Task の ID（`answer` の ID は、まだ Answer の Table がないため Backend は存在を確認しない。呼び出し側が Project に属することを保証する）。
2. Task が Claim を記録すると、その Task がその Claim を使ったものとして自動で登録される。それ以外（回答、別の Task）は `add_reference` で明示する。
3. 追跡の結果は、Claim の作成順（`created_at`、同時刻は ID）で最大 200 件まで（既定 100）。超えれば `truncated`。

### 6. 不変性と保持

1. Application の Role は 6 つの Table に **SELECT と INSERT だけ**を持つ。UPDATE も DELETE もできない。誤った Source や Claim は、書き換えではなく新しい記録で訂正する。
2. Provenance は Research Scratch（24 時間 TTL）と違い **期限で消えない**。Project の削除時の扱い（消す、残す、匿名化する）は決めていない。
3. `confidence`（要件の例にある）は持たない。持つなら、Source の Stance と別に、誰が何を根拠に付けるかを決めてから追加する。

## 決めてほしいこと

1. 同じ Claim を自動でまとめてよいか（2 節）。まとめない場合は、Claim ごとに新しい行を作り、`duplicate` を必ず明示する形になる（Schema の変更が要る）。
2. Source を書き換えない規則（1 節）。`fetched_at` を最新に更新したい場合は、別の Table（取得の履歴）が要る。
3. Project の削除と Provenance（6 節）。特に、Memory の唯一の Provenance が削除される Project の場合（[REQUIREMENTS.md](../../REQUIREMENTS.md) の「User Memory」）。
4. `confidence` を持つか。持つなら数値か段階か。
5. 認可の対応（README の提案）。特に、Relation と Reference を `project.task.run` で足してよいか。
6. 上限（本文 2000 文字、Source 50 件など）。
