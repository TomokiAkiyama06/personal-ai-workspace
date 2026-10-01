# Memory 画面の HTTP API の方針（読み取りモデル、件数と一覧の意味、検索と上限、履歴に出す範囲、書き込める判定、変更者の名前）

- Status: Proposed
- Date: 2026-10-01
- Scope: Issue [#186](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/186)（Memory の HTTP API。UI #49・PR #178 の `MemorySource` の接続先）の `apps/backend/paw_backend/memory/board/`（読み取りモデル `MemoryBoard`）、`apps/backend/paw_backend/api/v1/memory.py`（`/api/v1/memory/*`）、`apps/web/src/memory/apiSource.ts`
- Supersedes: なし。[Decision 0024](0024-memory-read-capability.md)・[0034](0034-memory-versioning-freshness.md)・[0045](0045-memory-edit-sources.md)・[0065](0065-manual-supersedes-undo.md)（いずれも Approved）を変えない

## 背景

PR #178（#49）の Memory 画面は、`MemorySource`（`scopes` / `list` / `history` / `sources` / `edit` / `restore`）の形でデータを受ける。
Issue #186 は次を求める（2026-10-01 に Human が承認）。件数つきの Scope の木、Scope ごとの今の版の一覧（検索つき）、履歴（版、関係、関係の先で読める版）、版ごとの出典、`expected_version` つきの編集と復元（衝突は 409 `memory_version_conflict`）、User Scope の読み取りを `memory.read` で認可すること（Decision 0024）、履歴の任意の `can_write`、変更者の表示名 `actor_name`。

Backend の Domain には、書き込み（`MemoryVersioningService` の `edit_memory` / `restore_version`）と 1 つの Memory の版の読み取り（`history`）がある。
しかし `history` は User Scope を `memory.use`（`REQUIRED`。読むたびに Audit の行を書く）で認可し、Repo Memory を扱わず、関係・関係の先の版・出典を返さない。Scope の木と一覧を読む Service はない。

**次のことは要件も既存の Decision も決めていない。** 読み取りをどこに置くか、何を「件数」「一覧」とするか、一覧の上限と検索の対象、履歴にどこまで出すか、`can_write` の決め方、表示名に何を使うか、Route の入口の認可。
[AGENTS.md](../../AGENTS.md) の「仕様変更」に従い、実装はこれらを下の推奨で置き、この Decision で承認を求める。

## 提案

### 1. Endpoint の形

`/api/v1/memory` 以下（応答の形は `apps/web/src/memory/types.ts` と同じ。ID は文字列、時刻は ISO 8601、`revalidate_after` は秒）。

| Method / Path | 内容 |
| --- | --- |
| `GET /scopes` | Scope の木と件数（`{user, projects: [{project_id, name, count, project_count, repos: [{repo_id, name, count}]}], shared}`） |
| `GET /memories?scope=user\|project\|repo\|shared[&project_id][&repo_id][&q]` | その Scope の各 Memory の今の版（`{memories, truncated}`） |
| `GET /memories/{id}/history` | `{versions, relations, related, can_write}` |
| `GET /memories/{id}/versions/{n}/sources` | `{sources}` |
| `POST /memories/{id}/edit` | `{expected_version, title?, content?, reason?}` → 新しい版 |
| `POST /memories/{id}/restore` | `{expected_version, source_version, reason?}` → 新しい版 |

誤りは共通の形（`error.code`）。読めない Memory・Scope は存在しないものと同じ 404 `memory_not_found`。409 は `memory_version_conflict`（何も書かない）・`memory_state_conflict`・`memory_scope_not_supported`、403 `forbidden`（Viewer の編集など）、422 `validation_error`、503 `memory_busy` / `service_unavailable`（Audit を書けないときを含む）。
Web の Client の `Method` は `GET` / `POST` / `PUT` / `DELETE` だけなので、編集と復元は `POST` の動詞の Path にする。

**推奨: この形。** 代替案は `PATCH /memories/{id}`（Client に `PATCH` を足す必要がある）と、版の作成として `POST /memories/{id}/versions` に `kind` を持たせる形（編集と復元の Body が混ざる）。

### 2. Route の入口の認可

全 Route に `require_capability(memory.read, 本人の Memory)` を付ける（Passkey Policy で制限された Session は 403 `passkey_required`。`memory.read` は `DENIED_ONLY` なので、許可した読み取りは Audit を書かない）。何を読め、何を変えられるかは、Route ではなく Service が Scope ごとに、Database から読んだ Role と Project の状態で決める。
`require_capability` は Principal を Resource の解決に渡さないため、入口の Resource を作るときに Session の Principal をもう一度読む（1 Request で 2 回）。

**推奨: これ。** 代替案の `account.read` を入口にする案は、Memory の読み取りと関係のない Capability が拒否の Audit に出る。

### 3. 読み取りは新しい `MemoryBoard` に置き、書き込みは `MemoryVersioningService` のまま

`paw_backend/memory/board/` の `MemoryBoard` が、Scope の木・一覧・履歴・出典を読む（何も書かない）。読める範囲は Hybrid Retrieval（`retrieval/resolver.py`、Decision 0019・0024）と同じ決め方: `user` は本人の Memory への `memory.read`、`project` は Database から読んだ承諾済みの Membership と Project の状態（Active / Archived）で `project.read`、`repo` はその Project の登録済みの Repository を保存された ACL つきで `project.read`（`read` を外した Override は見えない）、`shared` は `shared_memory.read`、`project_group` は出さない（Group が定義されていないため、`acl.py`）。
SQL は `readable_memory_versions` をどの読み取りにも入れ、読めない行は Backend に届かない。
編集と復元は `MemoryVersioningService`（`memory.use` / `project.memory.use`、`REQUIRED`）をそのまま呼ぶ。

**推奨: これ。** `MemoryVersioningService.history` を使う案は、User Scope の読み取りが `memory.use`（読むたびに Audit の行。Decision 0024 が避けた形）になり、Repo Memory・関係・出典が読めない。`history` を書き換える案は、書き込みの Service の承認済みの振る舞い（Decision 0034）を変える。

### 4. 一覧と件数の意味

- Memory の Scope は**今の版（番号が最大の版）の Scope**。広げた・狭めた Memory は、今の版の Scope にだけ出る。
- 一覧と件数は、今の版の状態を問わない（`active` / `superseded` / `deprecated` / `history`）。画面の状態の Filter（すべて / 有効 / 置き換え済み・廃止）が分ける。
- 例外として、`shared` は今の版が `active` のものだけ。削除した（`deprecated`）Shared Memory は Owner / Admin が `SharedMemoryService`（`include_deleted`）で見るもので、Decision 0009 のとおり一般の User には見せない。
- Project の `count` は Project 共通（`project_count`）と、その Project の読める Repository の件数の合計。`project` の一覧は Project 共通（Scope `project`）だけ、`repo` の一覧はその Repository だけ。
- 読めない Project・Repository・Scope を指定した一覧は 404（存在を教えない）。拒否された Scope は木に出ない。

**推奨: これ。** 代替案の `active` だけを数える案は、画面の「置き換え済み・廃止」の Filter が空になる。

### 5. 一覧の上限（Pagination）

1 回の一覧は最大 **500 件**（ピン留めを先に、作成日時の新しい順）で、超えたら `truncated: true`。Cursor のページングはまだ作らない（画面は `truncated` をまだ表示しない）。

**推奨: これ。今は Cursor を作らない。** 1 人・小さなチームの Workspace では 1 Scope の Memory は数百件の見込みで、Cursor の契約（並び順の固定、版が増えたときの重複・欠け）を決める必要がまだない。件数が上限に近づいたら、別の Issue で Cursor と画面の「さらに読む」を足す。

### 6. 検索の対象と方法

`q` は、題・本文・今の版の出典の `source_ref` の**部分一致**（大文字小文字を区別しない `ILIKE`。`%` と `_` は文字として扱う）。1〜200 文字、空白だけは「検索なし」。Hybrid Retrieval の全文検索の Index（`active` の版だけ）と Vector は使わない。

**推奨: これ。** 画面は置き換え済み・廃止の Memory も探せる必要があり、全文検索の Index は `active` だけを持つ。部分一致は Scope と ACL で絞った行だけを走査するため、上の件数の見込みでは足りる。件数が増えたら Trigram の Index（Migration）を別の Issue で検討する。

### 7. 履歴に出す範囲

- **版**: その Memory の版のうち、読者が読める版（版の Scope が読める）。その Memory の今の版が読めなければ、Memory ごと 404。Decision 0034 の `history` と同じく、広げる前の Private な版はその公開範囲を読める人だけに出し、本人の Private な Memory に狭めた Memory は、Project のメンバーには全版が見えなくなる。
- **関係**: 両端の版がどちらも上の意味で読めるものだけ（片方が読めない辺は出さない。辺そのものも Backend に届かない）。
- **関係の先の版（`related`）**: その関係の、他の Memory の読める版。
- 関係に「誰が付けたか」は返さない（列がない。Decision 0065 の承認時に、今は足さないと決まっている）。

**推奨: これ。**

### 8. Repo Memory と Shared Memory は読むだけ

`repo` の Memory の書き込みの権限は Decision 0034 の 3 が未決のまま（`MemoryVersioningService` は Not Found）、`shared` は `SharedMemoryService`（Owner / Admin）が扱う。この API ではどちらも `can_write: false` で、編集・復元を送っても 404 / 409 になる。

**推奨: これ。** Repo Memory の手動編集は、権限（Repository の ACL の `write` か `project.memory.use` か）を決める別の Issue で扱う。

### 9. `can_write` の決め方

今の版が `user` なら本人への `memory.use`、`project` なら Database から読んだ Role と Project の状態での `project.memory.use` を、**Audit を書かない純粋な Policy の判定**（`authz.policy.decide`）で決める。最終の判定は書き込みのとき `MemoryVersioningService` が Authorizer で行い、Audit に残す。

**推奨: これ。** 代替案の Authorizer で判定する案は、`memory.use` / `project.memory.use` が `REQUIRED` のため、履歴を開くたびに Audit の行が増える（Decision 0024 が避けた量の問題）。表示のための答えなので、記録は要らない。

### 10. 変更者の名前（`actor_name`）

`actor_type = user` の版は、その User の **Login 名**（`users.login_name`）を返す。User の状態が `deleted` なら `null`（画面は「別のユーザー」）。`agent` / `system` の版は `null`（版に Agent 名の列はない。画面は「エージェント」「システム」）。表示名の列は User にまだないため、Login 名で代える。

**推奨: これ。** Login 名は Workspace の Member どうしで既に見える値（Project のメンバー一覧、通知）である。表示名を別に持つなら、User の Profile の Issue で列を足し、この API はそれに切り替える。Agent 名は Journal / Worker が書くときに残す列が要るため、別の Issue とする。

### 11. 書き込みの Step-up と CSRF

編集と復元に Passkey の Step-up は求めない（REQUIREMENTS.md の Step-up は Owner / Admin の重要操作が対象で、自分・Project の Memory の編集は通常の操作）。`reason` は任意（最大 500 文字）。CSRF は既存の Origin の検査（Decision 0044）が全 `POST` に掛かる。

**推奨: これ。**

### 12. 手動の `supersedes` の取り消し（Decision 0065 の操作）は、この Issue では作らない

Decision 0065（案 B）の取り消しは、置き換えられた古い Memory に、その最後の内容で新しい版を書く。ところが古い Memory の今の版は `supersedes` の終点で、`memory_relations` の一意 Index `ix_memory_relations_one_successor`（1 つの版の後継は 1 つ）のため、その版から新しい版への `supersedes` を張れない。また `restore_version` は今の版が `superseded` の Memory を `NOT_ACTIVE` で拒否する。取り消しには Domain の変更（新しい版と古い版を結ぶ関係の種類、または Index の条件）が要り、承認済みの Decision 0034 の振る舞いに触れる。

**推奨: 別の Issue で、Domain の変更と一緒に作る。** そのときの選択の推奨（その Issue の Decision の叩き台）:

1. 権限: 古い Memory（取り消して有効に戻す側）への書き込みの権限（`memory.use` / `project.memory.use`。`relate_memories` と同じく、両方の Memory を変えられる人に限るなら新しい側も）。
2. Step-up: 求めない（通常の Memory の操作。11 と同じ）。
3. 理由: 任意（復元と同じ）。
4. 新しい版と古い版は、`supersedes` ではなく新しい種類（例: `restored_from`）の関係で結ぶ。取り消し前の `supersedes` は履歴に残す（Decision 0065 の 2）。

## 選定理由

- 書き込みの Service（承認済みの Decision 0034）に手を入れず、画面の読み取りだけを足す。読める範囲の決め方を Hybrid Retrieval と同じにし、Permission Leakage の安全網（ACL を SQL で掛ける）を共有する。
- 量の多い読み取り（一覧・履歴を開くたび）が Audit の行を増やさない（Decision 0024 の考え方）。
- 画面の型（`types.ts`）と応答の形を合わせ、Web 側は薄い Client（`apiSource.ts`）だけで済む。

## リスク

- 一覧の上限（500 件）を超える Scope は、新しい順の 500 件しか出ない（`truncated` は画面にまだ出ない）。
- 部分一致の検索は Index を使わない。Memory が数万件になると遅くなる。
- `can_write` は表示のための答えで、判定と書き込みの間に Role が変われば、書き込みは 403 になる（最終の判定は書き込みのとき）。
- 出典の `conversation_id` / `message_id` は、Project のメンバーに他の人の会話の ID を見せる（ID だけで、会話は `readable_conversations` で本人しか読めない）。
- Route の入口で Session の Principal を 2 回読む。

## 判断が必要な点（推奨つき）

1. Endpoint の形（1）。推奨: 承認。
2. Route の入口を本人の Memory への `memory.read` にすること（2）。推奨: 承認。
3. 読み取りを新しい `MemoryBoard` に置き、書き込みは `MemoryVersioningService` のままにすること（3）。推奨: 承認。
4. 一覧と件数の意味（今の版の Scope、状態を問わない、Shared は `active` だけ、Project の件数は Repository を含む）（4）。推奨: 承認。
5. 一覧を 500 件で打ち切り `truncated` を返し、Cursor は後にすること（5）。推奨: 承認。
6. 検索を題・本文・出典の `source_ref` の部分一致にすること（6）。推奨: 承認。
7. 履歴に出す範囲（両端が読める辺だけ、関係の先は読める版だけ）（7）。推奨: 承認。
8. Repo / Shared Memory は読むだけにすること（8）。推奨: 承認。
9. `can_write` を Audit なしの Policy の判定で返すこと（9）。推奨: 承認。
10. `actor_name` に Login 名を使い、削除済みの User と Agent は `null` にすること（10）。推奨: 承認。
11. 編集・復元に Step-up を求めず、理由を任意にすること（11）。推奨: 承認。
12. 手動の `supersedes` の取り消しを別の Issue（Domain の変更つき）で作ること、とその Issue の選択の推奨（12）。推奨: 承認。
