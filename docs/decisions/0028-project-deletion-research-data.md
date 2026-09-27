# Project 削除時の調査結果（Evidence / Claim Provenance、Research Scratch）の扱い

- Status: Approved
- Date: 2026-09-27
- Scope: Issue [#88](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/88)。[Decision 0008](0008-project-membership-and-lifecycle-policy.md) の 2 節が保留した「調査結果の扱い」と、[Decision 0011](0011-research-provenance-model.md) の 6 節が保留した「Project 削除時の Provenance の扱い」に答える。対象は `paw_backend/research/provenance/`（`ProvenanceStore.purge_projects`）と `paw_backend/research/scratch/`（`ScratchStore.purge_projects`）、Migration `0088`（Application Role への DELETE 権限の追加）。Long-term Memory（`memories` / `memory_sources`）のスキーマ変更は対象外（背景・4 節を参照）
- Supersedes: なし（0008 と 0011 を書き換えず、両者が「別の Decision で決める」と予告した空欄を埋める）
- Approval: 2026-09-27、Humanが作業Session内で、判断点ごとの説明（推奨つき）を受けたうえで「全部推奨どおり」と回答して承認（末尾の「承認時の決定」）

## 背景

[Decision 0008](0008-project-membership-and-lifecycle-policy.md)（PAW-026、Approved）の 2 節「Deleted が残すもの」は、Project の Purge（30 日後）で他の領域のデータ（Chat、Memory、Task、Repo の紐付け、調査結果）は消さず、各領域の Service が `PurgeResult.purged` の ID を使って要件のとおりに消す、という分担だけを決めた。
調査結果（Evidence / Claim の Provenance、Research の Scratch）を Project の消去でどう扱うか（消す・残す・匿名化する）は、この分担のままでは確定せず、この Issue #88 で決めると明記している。

[Decision 0011](0011-research-provenance-model.md)（PAW-052、Approved）の 6 節「不変性と保持」は、Application の PostgreSQL Role が 6 つの Provenance Table に **SELECT と INSERT だけ**を持ち、UPDATE も DELETE もできないと定めた上で、「Provenance は Research Scratch（24 時間 TTL）と違い期限で消えない。Project の削除時の扱い（消す、残す、匿名化する）は決めていない」とし、Issue #88 で別の Decision にすると記録している。
承認にあたって確認した点（3）は、特に「Memory の唯一の Provenance が削除される Project の場合」（[REQUIREMENTS.md](../../REQUIREMENTS.md) の「User Memory」）を名指しし、今は決めないとしている。

[REQUIREMENTS.md](../../REQUIREMENTS.md) の「User Memory」は次のように定める。

> Project削除のみではUser Memoryを原則削除しない。
> 削除Projectが唯一のprovenanceであり、関連Memoryも消したい場合は削除候補として提示し、別途確認する。

[Decision 0013](0013-research-scratch-task-relation.md)（PAW-050、Approved）は Research Scratch の `task_id` を素の UUID にする決定（Task の削除に Item を従属させない）であり、この Decision が変えるものではない。ただし、Scratch Item の `project_id` も同じ理由（参照先の削除の方針が未定）で素の UUID になっており、この Decision がその「未定」を埋める。

**この Decision が実際に読んだ実装**（PR 差分そのものではなく、現在の `main` の実装）は次のとおり。

- `paw_backend/research/provenance/`（`models.py`、`store.py`、Migration `0052`）: 6 Table（`research_sources`、`research_claims`、`research_claim_sources`、`research_claim_uses`、`research_claim_relations`、`research_source_relations`）。全て `project_id` を素の UUID で持ち、`projects` への外部キーはない。
- `paw_backend/research/scratch/`（`models.py`、`service.py`、Migration `0050`）: `research_scratch_items`、`research_scratch_leases`。Application Role は既に `research_scratch_items` / `research_scratch_leases` の両方に **INSERT・DELETE** を持つ（`purge_expired` が使う）。
- `paw_backend/projects/`（`service.py`、`task_stop.py`、Migration `0026`）: `ProjectService.purge_expired` は Pending deletion → Deleted の Purge で、Project の行を墓石にし、`PurgeResult(purged, has_more)` を返す。他の領域のデータには触れない。
- `paw_backend/repositories/service.py` の `RepositoryService.purge_projects(project_ids)`（PAW-027、`#98`。Decision 0008 の分担を初めて実装した箇所）: `project_ids` を受け取り、その場で `projects.status = 'deleted'` かどうかを DELETE 文の中の副問い合わせで確認してから、その Project の Repository 登録を削除する。Outbox は持たず、Orchestrator（未実装。PAW-034 相当）が `PurgeResult.purged` をそのまま渡す前提のコードである。
- `paw_backend/memory/models.py`: `memory_sources.source_type` は `conversation` / `task` / `repo_analysis` / `user_confirmation` / `project_decision` の 5 種類だけで、Research の Claim・Source・Scratch Item を指す種類は**存在しない**。Research Scratch の `promotion_state`（`none` / `pending` / `promoted` / `rejected`）は昇格の**意図**だけを記録し、実際に Memory へ書き込む昇格 Flow はまだ実装されていない（`research/scratch/service.py` の Docstring: 「Store は Memory Candidate を作りません」）。

## 提案

### 1. Research Scratch: Project が Deleted になったら、その Item を無条件に削除する

`ScratchStore.purge_projects(project_ids)` を追加する。`ProjectService.purge_expired` が返す `PurgeResult.purged` を Orchestrator がそのまま渡す（`RepositoryService.purge_projects` と同じ呼び出し方）。

- 対象は `project_ids` のうち **その時点で `projects.status = 'deleted'`** の Project だけ（`DELETE ... WHERE project_id IN (SELECT id FROM projects WHERE id = ANY(...) AND status = 'deleted')`）。まだ Deleted でない Project は触れない。
- `pinned`、`saved`、`promotion_state = 'pending'`、有効な Lease のどれであっても関係なく削除する。これらは `purge_expired`（TTL の猶予）のための印であり、「Project がまだ生きている間、24 時間で消えるはずの調査結果を消さずに残す」ためのものである。Project 自体が消えたら、その猶予が守るべき対象（Project の中の作業）がそもそも無い。
- 新しい Role 権限は不要（Migration `0050` が既に `research_scratch_items` / `research_scratch_leases` の両方に DELETE を与えている）。Lease は外部キー `ON DELETE CASCADE` で一緒に消える。
- Backend 内部の Method（Actor なし、Authorizer を呼ばない）。`purge_expired` と同じ扱い。

### 2. Evidence / Claim Provenance: Project が Deleted になったら、6 Table の行を全て削除する

`ProvenanceStore.purge_projects(project_ids)` を追加する。1 の Scratch と同じ呼び出し方（`projects.status = 'deleted'` を DELETE 文自身が確認する）。

- 6 Table を外部キーの向きに沿って子から先に削除する: `research_claim_sources`、`research_claim_uses`、`research_claim_relations`、`research_source_relations` → `research_claims` → `research_sources`。
- **Application Role に DELETE を新たに与える**（Migration `0088`）。UPDATE は与えない（[Decision 0011](0011-research-provenance-model.md) の「誤った記録は書き換えでなく新しい記録で訂正する」という不変性はそのまま残る。書き換えの手段は増えない）。
  - PostgreSQL の `GRANT` は Table 単位で、「`purge_projects` からの呼び出しだけ」という粒度には絞れない。**最小権限は「不要な操作を許可しない」という意味では保っている**（UPDATE は今回も与えない。DELETE を使う Application のコード経路は `purge_projects` の 1 つだけで、他の全ての public な Method（`record_claim`、`add_reference`、`mark_related`、読み取り）は今までどおり SELECT と INSERT だけで書かれている）が、「DB がその 1 経路だけに強制する」わけではないことは明示しておく（Migration `0050`（Research Scratch）が既に同じトレードオフを取っている: `purge_expired` 用の DELETE は Table 全体に与えられている）。Row Level Security でこれを DB 側にも強制するかは、この Decision では決めない（5 節「決めてほしいこと」4）。
- Backend 内部の Method（Actor なし）。

### 3. Orchestrator への引き継ぎ方: Outbox は持たず、`PurgeResult.purged` をそのまま渡す

`ProjectService.purge_expired` 自体は変えない。`ProjectTaskStopper`（Decision 0008 の 8 節）のような Outbox（`project_task_stops` Table + 冪等な Processor）は、この 2 つの `purge_projects` のためには**新設しない**。
理由は、この分担の最初の実装である `RepositoryService.purge_projects`（PAW-027、`#98`）が、同じ「他領域のデータを `PurgeResult.purged` で消す」という仕事を、Outbox なしで実装し、レビュー済み・Merge 済みだからである。この Decision はその先例に合わせる。

- Orchestrator（PAW-034 相当。未実装）が、`purge_expired` が返した ID を、`RepositoryService.purge_projects`、`ScratchStore.purge_projects`、`ProvenanceStore.purge_projects` の 3 つへ同じように渡す。
- 各 `purge_projects` は、渡された ID のうち Deleted なものだけを対象にする（1 節・2 節）ため、順序や再実行に依存しない。2 回目の呼び出しは何も見つけない（冪等）。
- **残るリスク**: `purge_expired` が Commit された直後、Orchestrator がまだ 3 つの `purge_projects` を呼ぶ前に落ちると、その回の呼び出しは失われる。`purge_expired` は既に Deleted になった Project を二度と返さないため、次回の定期実行では拾えない。これは `RepositoryService.purge_projects` に既にある制約で、この Decision も同じ制約を引き継ぐ（4 節「代替案」、5 節「決めてほしいこと」4 を参照）。

### 4. Memory の唯一の Provenance が消える問題は、今回のコード変更の対象にしない

現在の実装には、Long-term Memory（`memories` / `memory_sources`）から Research の Claim・Source・Scratch Item を指す経路が**存在しない**（背景を参照: `SourceType` に該当する種類がなく、Scratch → Memory の昇格 Flow 自体が未実装）。
したがって、「削除される Project が唯一の Provenance である Memory」は、現時点では**存在し得ない**。1 節・2 節の削除は、今のところどの Memory の正当性も損なわない。

この Decision は、将来 Research の調査結果を Memory へ昇格する Flow（`resolve_promotion(PROMOTED)` の先、`research/scratch/service.py` の Docstring が「それは昇格の Flow の仕事」と書いている部分）を実装する Issue に対して、次の制約を残す。

- REQUIREMENTS.md の「Project削除のみではUser Memoryを原則削除しない」を守るため、Memory ↔ Research の対応は、**Provenance / Scratch の行が消えても Memory 自体は消えない形**にすること。推奨は、`memory_sources` が既に持つパターン（`source_deleted_at`: 参照先の Conversation / Message が消えても Memory Source の行は残り、「参照先が消えた」とだけ記録する）を Research にも当てはめ、`SourceType` に `research_claim` 等の種類を足し、`source_ref` に Claim の ID をオペークに持たせること。この Decision の `purge_projects` が Provenance の行を消した後、その Memory Source の `source_deleted_at` を（この Issue ではなく、その将来の Issue が）埋める。
- 「唯一の Provenance を失う Memory」を検出し、REQUIREMENTS の「削除候補として提示し、別途確認する」を満たす一覧・確認の UI / API は、その将来の Issue の受け入れ条件とする（この PR には含めない）。
- **採らない案**: Memory ↔ Research の対応を実際の外部キー + `ON DELETE RESTRICT` にすること。[Decision 0013](0013-research-scratch-task-relation.md) が Research Scratch の `task_id` の `RESTRICT` 案を退けた理由（Task の削除を止めてしまう）と同じ理由で、Project の削除（Human が確認し 30 日待った操作）を、参照する側が忘れている可能性のある Memory の存在で止めるべきではない。

### 5. Audit

`purge_projects` は `purge_expired`、`ProjectTaskStopper`、`RepositoryService.purge_projects` と同じ「Backend 内部・Actor なし」の Method であり、`audit_events` への行は書かない（Decision 0004 の Audit は Capability の判定を記録するもので、判定する Actor がいない）。
削除した Project 数を `logging`（Python 標準の `logger.info`）に 1 行残す（`paw_backend.research.provenance.store` / `paw_backend.research.scratch.service`。件数だけで、Project の ID や内容は含まない）。これは `ScratchJanitor` が `purge_expired` の実行結果を INFO に残す既存の方法と同じである。

## 選定理由

- **削除（1 節・2 節）を選ぶ理由。** Decision 0008 の全体設計は「Project の Purge で他領域の Project 固有データは各領域が消す」であり（墓石が Project の行だけを残し、名前・説明を消すのも「個人・業務の内容を含みうるため」）、Provenance・Scratch の中身（Claim の本文、Source の URL、Scratch の Summary / Content）はまさにその「内容」にあたる。REQUIREMENTS が明示的に例外にしているのは「User Memory」だけで、調査結果を例外にする記述はない。
- **Provenance の「不変性」は削除の対象外にならない。** Decision 0011 の 6.1 の「書き換えず、新しい記録で訂正する」は、Project が生きている間に誤りをどう扱うかの規則であり、Project 自体の（Human が確認し 30 日待った）削除の後もデータを残す理由にはならない。
- **Table 単位の DELETE 権限が先例と一致する。** Research Scratch はまったく同じトレードオフ（TTL の Purge のために Table 全体へ DELETE を与える）を Migration `0050` で既に採用しており、Provenance だけを異なる基準にする理由はない。
- **Outbox を新設しない理由。** `RepositoryService.purge_projects`（既に Merge 済み）が同じ問題をより単純な形（呼び出しごとに `projects.status` を確認するだけ）で解決しており、実装・レビューの負担を増やす新しい仕組みを、既にある分担の 2 つ目・3 つ目の実装のためだけに導入しない。

## 代替案

| 案 | 内容 | 採らない理由 |
| --- | --- | --- |
| 匿名化して残す | Claim の本文や Source の URL を固定文字列に置き換え、行は残す | Provenance の値のほとんどが内容そのもの（Claim の本文、Source の URL、Scratch の `title` / `summary` / `content`）で、匿名化すると読む側に意味のある情報は残らない。実質的に「消す」と同じ手間で、消えないことを示す ID だけが残る利点に見合わない。行数だけ残るとリークの表面が増える（誰が読んでも中身のない行が Project 数だけ存在する）。 |
| 保持して Project だけ論理削除 | Provenance / Scratch の行を無期限に残す | 読む経路が誰にもない（`get_claim`、`trace` などは全て `project_id` で絞り、Deleted な Project の Provenance を返す Endpoint はない）まま無期限に増え続ける。Decision 0008 が墓石に名前・説明を残さない理由（内容を含みうる）は、内容そのものである Provenance・Scratch によりあてはまる。 |
| Outbox + 冪等な Processor（Decision 0008 の 8 節と同じ形） | `project_task_stops` と同様の Table を作り、Orchestrator が未処理の Project を再試行できるようにする | 3 節のクラッシュのリスクをより強く閉じられるが、`RepositoryService.purge_projects` に同じ機構がなく、この PR だけ厚くする一貫性の問題がある。5 節「決めてほしいこと」4 で Human に確認する。 |
| Provenance に `deleted_at` 列を足す論理削除 | 行は残し、削除済みの印だけ付ける | データ量は減らず、読む経路がないままの行が残る点は「保持」案と同じ。 |

## リスク

- **Table 単位の DELETE は、`purge_projects` 以外の誤ったコードからも実行できてしまう。** DB は「Deleted な Project だけ」を強制しない（強制するのは `purge_projects` 自身の SQL 文だけ）。Application のコードレビューと `tests/test_provenance_grants.py` / `tests/test_scratch_grants.py` の Store Test だけが実質的な守りになる。
- **3 節のクラッシュの窓は閉じていない。** `purge_expired` の Commit と Orchestrator の 3 つの呼び出しの間に Process が落ちると、その回は失われ、次の `purge_expired` はその Project をもう返さない。`RepositoryService.purge_projects` に既にある制約で、この Decision もそれを引き継ぐ。
- **Memory との対応（4 節）は宙に浮いたまま。** 実装しないため、将来の昇格 Flow の Issue がこの Decision を読み落とすと、REQUIREMENTS の「原則削除しない」に反する形（Memory ごと消す、または Provenance を Project の削除より優先して残す）で作られる可能性がある。README とこの Decision の相互参照で軽減する。

## 承認後の扱い

承認されたら Status を Approved に改め、`Approval` に日付と承認の様子を記録する。
承認後に方針（削除にするかどうか、DELETE 権限の与え方、Outbox の要否）を変える場合は、この Decision を書き換えず、新しい Decision から `Supersedes` する。
[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文、[Decision 0008](0008-project-membership-and-lifecycle-policy.md)、[Decision 0011](0011-research-provenance-model.md)、[Decision 0013](0013-research-scratch-task-relation.md) は書き換えない。

## 決めてほしいこと

1. **Research Scratch と Evidence / Claim Provenance は、どちらも Project が Deleted になったら削除する**（1 節・2 節。匿名化しない、無期限に残さない）でよいか。推奨: はい。
2. **Application Role に、Provenance の 6 Table への DELETE を新たに与える**（Migration `0088`。UPDATE は与えない）ことを承認するか。DB が「`purge_projects` からだけ」に絞れない点（Table 単位の付与）を了承の上でよいか。推奨: はい（Research Scratch が既に同じ形を採用している）。
3. **「唯一の Provenance を失う Memory」の扱いは、対応する Schema がまだ存在しないため、今回は実装せず、将来 Memory ↔ Research の対応を作る Issue への制約（4 節）として引き継ぐ**でよいか。推奨: はい。
4. **Outbox（Decision 0008 の 8 節と同じ冪等な Processor）を新設せず、`RepositoryService.purge_projects` と同じ「呼び出しごとに `projects.status` を確認するだけ」の形にする**（3 節）でよいか。クラッシュ時に一部の Project の Purge が次回まで持ち越されず失われる（次の `purge_expired` はそのIDをもう返さない）リスクを許容するか。推奨: はい（先例と揃え、必要になれば別 Decision で Outbox 化する）。
5. **Audit は `audit_events` への行を書かず、削除した Project 数だけを `logging` の INFO に残す**（5 節）ことでよいか。推奨: はい（`purge_expired` / `ProjectTaskStopper` / `RepositoryService.purge_projects` と同じ扱い）。

## 承認時の決定（2026-09-27）

Human は、作業 Session で上の 5 点について推奨つきの説明を受け、「全部推奨どおり」と回答して承認した（5 点を一括で。個別の変更はない）。**5 点すべてが推奨どおりで、設計の変更はない。**

承認後に方針を変える場合は、この Decision を書き換えず、新しい Decision から `Supersedes` する。
