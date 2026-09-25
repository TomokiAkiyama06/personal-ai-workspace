# Research の Evidence / Claim Provenance の方針

- Status: Approved
- Date: 2026-09-24
- Scope: PAW-052（Evidence / Claim Provenance）と、Claim・Source を使う以降の Issue（Research Orchestrator、回答の引用表示、Memory Candidate への昇格など）
- Supersedes: なし
- Approval: 2026-09-25、Humanが作業Session内で、判断メモ（Artifact）の各点について「推奨どおり」と回答して承認（下記の「承認時の決定」）

## 背景

[REQUIREMENTS.md](../../REQUIREMENTS.md) の「Evidence / provenance」は、Research 結果が Claim と Source の対応を保持し、
claim、source id、source type、fetched_at、published_at、confidence、task / project の関係を持つと定める。
Source type だけでは真偽を決めない、とも書く。
一方で、次のことは文書では決まらず、PAW-052 の実装が選んだ。
Product Policy を実装が暗黙に確定させないよう、選択を一覧にして Human の承認を求め、2026-09-25 に承認された。

実装は [Backend README](../../apps/backend/README.md) の「Evidence / Claim Provenance」にある。
**この Decision は 2026-09-25 に Human が承認した（Approved）。** 下の各選択は、承認された方針である（承認時の決定は末尾を参照）。
ただし、Project の削除時の Provenance の扱い（6 節）は今は決めず、別の Decision で決める。上限の数値は暫定値として承認された（2 節・3 節・5 節と、末尾の承認時の決定を参照）。
値や規則の多くは `paw_backend/research/provenance/` の定数と関数で、変えても Schema の変更が要らないものが多い（要るものは各項に書く）。

## 承認された方針

### 1. Source の同一性と不変性

1. Source は `(project_id, 正規化した locator, content_hash)` で 1 件に決まる。同じ URL でも本文の Hash が違えば別の Source（取得した時点の内容を指すため）。
2. Source は **書き換えない**。同じ Source をもう一度記録しても、最初の `fetched_at`、`published_at`、`source_type`、`title` が残る。
3. 本文は保存しない。持つのは Hash だけ（`content_hash` は取得した Excerpt / 文書の Hash で、遠隔の文書全体とは限らない）。
4. `private_source`、Provider の種類と ID、License は記録しない（要件に定義がない。Privacy Filter は PAW-053）。

### 2. Claim の同一性と重複

1. Claim は `(project_id, 正規化した本文の Fingerprint)` で 1 件に決まる。正規化は NFKC、大文字小文字の畳み込み、空白の連続を 1 つの空白にすること。
2. 同じ Claim をもう一度記録すると、**新しい Claim を作らず既存の Claim に Source を足す**（最初の本文、作成者、Task、時刻が残る）。
3. 表現が違う近い Claim は、自動では同じにしない。`duplicate` の Relation で人間 / Agent が明示する。
4. 本文は 2000 文字まで（上限は暫定値として承認）。

### 3. Claim と Source の対応（Stance）

1. 対応は `supports`（支持）か `contradicts`（反する）。1 つの `(claim, source)` に Stance は 1 つ。
2. 同じ組に逆の Stance を記録すると `ProvenanceConflictError`（上書きしない）。同じ Stance の再記録は何もしない。
3. Claim に付く Source は 50 件まで。1 回の記録は 20 件まで（上限は暫定値として承認。「承認時の決定」を参照）。Claim には 1 件以上の Source が要る（Source のない Claim は Provenance ではない）。

### 4. duplicate / contradiction の表現

1. Claim 同士、Source 同士に、対称な Relation `duplicate`（同じことを言う）と `contradiction`（両立しない）を張れる。
2. 1 組に Relation は 1 つ。逆の種類を張ると `ProvenanceConflictError`。推移律の閉包は取らない（A と B、B と C が duplicate でも、A と C は張らない限り無関係）。
3. Source の Relation は「別 URL の同じ内容（ミラー）」や「互いに矛盾する 2 つの記事」を表すためで、どちらが正しいかは決めない。

### 5. 回答・Task からの追跡

1. 参照元は Answer か Task の ID（`answer` の ID は、まだ Answer の Table がないため Backend は存在を確認しない。呼び出し側が Project に属することを保証する）。
2. Task が Claim を記録すると、その Task がその Claim を使ったものとして自動で登録される。それ以外（回答、別の Task）は `add_reference` で明示する。
3. 追跡の結果は、Claim の作成順（`created_at`、同時刻は ID）で最大 200 件まで（既定 100。暫定値として承認）。超えれば `truncated`。

### 6. 不変性と保持

1. Application の Role は 6 つの Table に **SELECT と INSERT だけ**を持つ。UPDATE も DELETE もできない。誤った Source や Claim は、書き換えではなく新しい記録で訂正する。
2. Provenance は Research Scratch（24 時間 TTL）と違い **期限で消えない**。Project の削除時の扱い（消す、残す、匿名化する）は決めていない。Human は 2026-09-25 に、これを今は決めず、Issue [#88](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/88) で別の Decision にすると決めた（承認時の決定を参照）。
3. `confidence`（要件の例にある）は持たない（承認済み）。持つ場合は、Source の Stance と別に、誰が何を根拠に付けるかを決めてから、新しい Decision で追加する。

## 承認にあたって確認した点

Human が 2026-09-25 に確認し、承認した結果を各項に書く。

1. 同じ Claim を自動でまとめる（2 節）。まとめない場合は、Claim ごとに新しい行を作り、`duplicate` を必ず明示する形になり、Schema の変更が要る。承認: 自動でまとめる。
2. Source を書き換えない（1 節）。`fetched_at` を最新に更新したい場合は、別の Table（取得の履歴）が要る。承認: 書き換えない。
3. Project の削除と Provenance（6 節）。特に、Memory の唯一の Provenance が削除される Project の場合（[REQUIREMENTS.md](../../REQUIREMENTS.md) の「User Memory」）。承認: 今は決めず、Issue [#88](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/88) で別の Decision にする。
4. `confidence` は持たない（6 節）。承認: 持たない。
5. 認可の対応（README）。特に、Relation と Reference を `project.task.run` で足してよいか。承認: 読み取りは `project.read`、記録・Reference・Relation の追加は `project.task.run`。
6. 上限（本文 2000 文字、Source 50 件など）。承認: 暫定値として承認する。

## 承認後の扱い

2026-09-25 に承認された。PAW-052 のPRは本Decisionを参照する。
承認後に Human が承認済みの選択を変える場合は、この Decision を書き換えない（[AGENTS.md](../../AGENTS.md) の「仕様変更」）。
この Decision を `Supersedes` する新しい Decision を作り、Human の承認を得てから、実装を合わせる。
[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。

## 承認時の決定（2026-09-25）

- 本文の各点を、提案どおり承認した。
- 同じ Claim（正規化した本文が同じもの）は、自動でまとめる。
- Source は書き換えない。`fetched_at` を更新したくなった場合は、取得の履歴を持つ別の Table を新しい Decision で足す。
- `confidence`（確信度）は持たない。
- 認可の対応を承認した。読み取り（`get_claim`・`trace`・`list_relations`）は `project.read`、記録・Reference・Relation の追加（`record_claim`・`add_reference`・`mark_related`）は `project.task.run`。Endpoint は別の Issue の仕事で、この対応は Backend では強制しない。
- 上限（Claim の本文 2000 文字、Claim に付く Source 50 件、1 回の記録 Source 20 件、追跡は最大 200 件・既定 100）は、暫定値として承認した。定数で、変えても Schema の変更が要らないものが多く、変更できる。
- Project 削除時の Provenance の扱い（消す・残す・匿名化する）は、今は決めない。Issue [#88](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/88) で別の Decision にする。承認済みのこの Decision は書き換えず、新しい Decision から `Supersedes` で追記する。
