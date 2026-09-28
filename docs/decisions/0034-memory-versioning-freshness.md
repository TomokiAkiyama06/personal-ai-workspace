# Memory の Relation・手動編集の Version・Freshness と Stale Candidate の扱い

- Status: Approved
- Date: 2026-09-28
- Scope: Issue [#36](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/36)（PAW-042: Memory Conflict / Versioning / Freshness）。関連: PAW-040（#34、Schema）、PAW-041（#35、[Decision 0018](0018-memory-journal-consolidation-policy.md)）、PAW-043（Hybrid Retrieval、[Decision 0019](0019-hybrid-retrieval-policy.md)）、PAW-046（Shared Memory、[Decision 0009](0009-shared-memory-administration.md)）、[Decision 0026](0026-memory-status-change-history.md)（Status / Stale の変更履歴）、PAW-044（Inferred Preference の確認）、PAW-045（Markdown Projection）
- Supersedes: なし（既存の Decision を書き換えない。要件が決めていない点を埋める）
- Approval: 2026-09-28、Humanが作業Session内で、判断点ごとの説明（推奨つき）を受けたうえで「推奨どおり」と回答して承認（追加した 12 を含む全点。末尾の「承認時の決定」）

## 背景

[REQUIREMENTS.md](../../REQUIREMENTS.md)「Memory Conflict / Versioning / Retrieval」「Memory Freshness / Revalidate Policy」「Manual Memory Editing / Concurrency」と
[MEMORY_ARCHITECTURE.md](../MEMORY_ARCHITECTURE.md) の 10・11・15・17 節（いずれも `[FIXED]`）は次を定める。

- Memory は物理的に上書き・削除せず、`active` / `superseded` / `deprecated` / `history` の状態で履歴を残す。新しい Memory が古いものを置き換えるときは `supersedes` を保存する。通常の LLM Context は `active` だけ。
- LLM は新旧の関係を `same` / `extends` / `supersedes` / `conflicts` / `unrelated` に分類してよい。明確なら自動で新しい方を `active`、古い方を `superseded` にできる。曖昧な矛盾はユーザーに確認する。
- 鮮度は一律 TTL ではなく `permanent` / `revalidate` / `repo_commit` / `expiring` / `session_only`。`revalidate_after` の到達で即無効化せず `stale_candidate` にし、必要になった時点で再確認する。イベント（関連設定・メンバー・モデルの変更）でも再確認の対象にできる。Repo Memory は commit / branch / 差分で判定する。
- 人間が UI で編集した内容は原則 Confirmed。既存 Version を上書きせず新しい Version を作る。複数人の編集は Optimistic Lock。過去の Version に戻すときは、古い Version を `active` に戻さず、その内容で新しい Version を作る。
- 公開範囲を広げる Scope 変更は確認必須。本文の変更で権限や高リスク Policy を変えない。

Schema（PAW-040、Revision 0040 / 0071）は、Version・Relation（`supersedes` / `extends` / `conflicts_with` / `confirmed_from` / `revalidated_from` / `merged_from`）・鮮度の列（`verified_at`、`revalidate_after`、`revalidate_triggers`、`on_stale`、`expires_at`、`commit_sha`、`branch`、`stale_since`）と、Status / Stale の変更履歴（Trigger）を既に持つ。
Retrieval（PAW-043）は `active` だけを候補にし、`session_only` と期限切れの `expiring` を除き、Stale を印付きで低い Score にする。

要件が決めていないのは、**誰がどの Scope を手で変えられるか**、**Relation ごとに何が起きるか**、**手動で書ける鮮度**、**Stale Candidate や期限切れを誰がいつどう処理するか**である。この Decision はその推奨を示す。PR（Issue #36）は推奨どおりに実装しており、承認されない点があれば別の PR で直す。

## 提案

### 1. Relation の意味

| Relation | 新しい方 | 古い方 | 手動で記録できるか |
| --- | --- | --- | --- |
| `supersedes` | `active` | `superseded` にする | できる。**同じ公開範囲**（同じ Scope と同じ Owner / Project / Group / Repo）の Memory の間だけ |
| `extends` | `active` | `active` のまま | できる |
| `conflicts_with` | `active` | `active` のまま。Retrieval は Conflict Group として両方を示し、どちらも選ばない | できる。解消は人間が `supersedes` を記録するか、片方を `deprecated` にする |
| `confirmed_from` | — | — | できない。未確認（`observed` / `inferred`）の Version を人が編集したときに、編集が `supersedes` と一緒に書く |
| `revalidated_from` | — | — | できない。Revalidate（下の 5）が `supersedes` と一緒に書く |
| `merged_from` | — | — | できない。Merge は高リスク変更（REQUIREMENTS.md）で、この Issue の範囲外。後の Issue（PAW-044 など）で扱う |

- Relation は新しい方から古い方へ向く。`supersedes` / `extends` / `confirmed_from` / `revalidated_from` / `merged_from` の辺で**循環を作る記録は拒否**する。同じ Relation の重複（`conflicts_with` は逆向きも）も拒否する。
- 手動の Relation は、両方の Memory の**現在の Version**（最大の `version_number`）の間に記録する。両方とも `active` であること、両方の `expected_version` が現在の番号であること（Optimistic Lock）を要する。
- 循環の検査は Graph 全体を読むので、循環を作りうる種類（`supersedes` / `extends`）の手動の記録は、両方の Memory の Lock に加えて Graph 全体の Advisory Lock を 1 つ取り、1 件ずつ行う（別々の 2 組を同時に記録して一緒に循環を作ることがない）。`conflicts_with` と、編集などが新しい Version から張る Relation（新しい Version に入る辺はないので循環を作らない）はこの Lock を取らない。
- `extends` / `conflicts_with` も両方の Memory を変えられる人だけが記録できる。公開範囲をまたぐ `extends` / `conflicts_with` は確認なしで許す（どちらも何も退役させないため）。
- **手動の `supersedes` は、この Issue では取り消せない**。古い方の現在の Version は `superseded` のままで、編集・復元・廃止・Revalidate はできない（誤って記録した場合は、古い方の内容で新しい Memory を作る）。取り消しの操作（Relation を外して古い方を `active` に戻すか、古い方に新しい Version を書くか）は History Graph（UI）の Issue で決める。
- LLM の分類の意味（`rules.plan_relation`）: `same` は何も書かない。`extends` は新しい Memory を書き `extends` を張る。`supersedes` は新しい Memory を書き古い方を `superseded` にする。`conflicts` は新しい Memory を書き `conflicts_with` を張って**人の確認を要する**（何も退役させない）。`unrelated` は Relation なしで書く。**退役させるのは `supersedes` だけ**。Background Consolidation（PAW-041）は Decision 0018 の規則のまま（ここでは変えない）。

`supersedes` を同じ公開範囲に限るのは、置き換えで古い Memory を後継を読めない人から隠さないためである（例: User Memory が Project Memory を置き換えると、他のメンバーから Project Memory が消える）。例外は、編集による**公開範囲の縮小**（2・3）だけで、これは要件が即時反映を許す、人が明示的に行う操作である。

### 2. 手動編集・復元・廃止の Version

- **編集**（`edit_memory`）: 現在の Version `n`（`active`）を `superseded` にし、`n + 1` を `active`・`confirmed`・Actor は本人で書く。`supersedes`（理由は変えた項目名）を張り、`n` が未確認なら `confirmed_from` も張る。何も変えない編集は何も書かない。
  - 変えられる項目: Title、本文、種類、Importance、鮮度、**Scope の縮小**。
  - **Scope の縮小**（REQUIREMENTS.md「Scope変更」: 公開範囲を狭める変更は即反映可能）: `MemoryChanges(scope=user)` で `project` の Memory を**編集者本人の** `user` の Memory にする。同じ Memory の `n + 1` を `user`（Owner は編集者）で書き、`project` の `n` を `superseded` にする。これを**同じ Transaction** で行うので、両方が `active` の瞬間も、どちらも `active` でない瞬間もない。`supersedes` の理由には `scope` が入る。他の項目も同時に変えられる。以後 Project のメンバーには、この Memory は（履歴も）Not Found になる。`project` の `n` からの復元は公開範囲が違うので `SCOPE_MISMATCH`（復元で広げ直すことはできない）。
  - 縮小以外の Scope の変更（`user → project`、何でも `→ shared`、`→ repo` / `project_group`）は `InvalidMemoryInputError`（`scope`、`not_allowed`）。広げるのは確認の Flow（PAW-044）。
  - 変えなかった項目・Pin は引き継ぐ。`attributes` は引き継がず、`edited_from_version` だけを持つ（Worker の候補の情報を人の Version に残さない）。
  - `revalidate` の Memory は、人が保存した時点で確かめたとみなし、`verified_at` を今にし、Stale の印を外す。
  - 鮮度を変えない編集でも、人が書けない鮮度（4）は引き継がない: `session_only` の Version と、期限を過ぎた `expiring` の Version は、新しい鮮度を渡さない限り編集できない（復元と同じ）。
  - 編集・復元・Revalidate の新しい Version には `memory_sources` を写さない（出典は古い Version に残る）。そのため会話削除の Flow は、会話から来た Version の内容を写した `n + 1` を出典から見つけない。出典を写すか、Decision 0009 の Shared Memory のように `user_confirmation` の出典を足すかは未決（下の「決めてほしいこと」）。
- **復元**（`restore_version`）: 選んだ過去の Version の内容で `n + 1` を書く（`attributes.restored_from_version`）。現在の Version が `active` なら `superseded` に、`deprecated` ならそのまま。`supersedes`（理由 `restore`）を張る。公開範囲が違う Version からは戻さない。期限を過ぎた `expiring` は、新しい鮮度を渡さない限り戻さない。
- **廃止**（`deprecate_memory`）: 現在の Version を `deprecated` にする。何も消さない。`deprecated` の Memory は編集できず、復元で戻す。
- Status の変更はすべて `memory_metadata_changes` に本人を Actor として残る（Decision 0026）。

### 3. 誰がどの Scope を変えられるか

- 呼べるのは**人間の `Principal` だけ**（手動編集は人の行為で、書く Version は `confirmed`）。Agent と `system` は Journal（PAW-041）経由で候補を書く。
- `user`: Capability `memory.use`（Owner 本人だけ）。Owner / Admin でも他人の Private Memory は変えられない。
- `project`: Capability `project.memory.use`。Role と Project の状態は**Database から読む**（呼び出し側の `Principal` の Role は信じない）。Contributor 以上。Viewer と、Archived の Project では拒否。履歴の閲覧は `project.read`。
- `shared`: 扱わない（`SharedMemoryService`、Decision 0009）。
- `repo` と `project_group`: **この Issue では扱わない**（Not Found と同じに見せる）。Repo Memory の書き込みに Repo の ACL Override の `write` を要するか（`project.memory.use` は Repo では `read` に対応する）、Project Group とは何か、が決まっていないため。
- 「他人の Memory」「メンバーでない Project の Memory」への拒否は、存在しない ID と同じ Not Found にする（存在を教えない）。Authorizer は Project の状態をメンバーかどうかより先に見るので、メンバーでない人の Archived / Pending deletion の Project の Memory への拒否（`project_state_forbids`）も Not Found にする。
- **Scope の縮小**（2）: その Project の Memory を編集できる人（`project.memory.use`、Contributor 以上）で、かつ自分の Memory を持てる人（`memory.use`、`create_memory` と同じ）。Contributor は廃止（`deprecate_memory`）で Project Memory をメンバーから外せるので、縮小はそれ以上の力を与えない（廃止して自分の Memory を作るのと同じ結果を、1 つの Transaction で行う）。Manager だけに限るかは下の「決めてほしいこと」12。
- 履歴（`history`）は現在の Version を読める人に返し、公開範囲が現在と違う過去の Version（後の Flow で広げた Memory など）は、その公開範囲も読める人にだけ含める。広げた後の読者に、広げる前の Private な内容を見せないため。この絞り込みは SQL で行う（`memory/acl.py` の `readable_memory_versions` と `scope IN` を、Authorizer が許した公開範囲から作る）。読めない Version の内容は Backend にも届かない。認可の前に読むのは公開範囲の列（`scope` と Scope ID）だけで、これと循環検査（真偽だけを返す）を ACL の例外として文書化する。判断はすべて Authorizer が Audit に記録する（どちらの Capability も `REQUIRED`）。Shared Memory のような完了行（Decision 0009 の 13）は書かない。

### 4. 手動で書ける鮮度

| Policy | 手動で書けるか | 必要な値（暫定の範囲） |
| --- | --- | --- |
| `permanent` | できる | なし |
| `revalidate` | できる | `revalidate_after`（1 時間〜10 年）、任意で `revalidate_triggers`。`verified_at` は書いた時刻（呼び出し側は渡さない）。`on_stale` は `lower_priority` だけ |
| `expiring` | できる | `expires_at`（今より後、10 年以内） |
| `repo_commit` | **できない**（Repo Memory だけのもの。3 のとおり Repo Memory の手動編集はまだない） | `commit_sha`（40 / 64 桁の小文字 16 進）、任意で `branch` |
| `session_only` | **できない**（Long-term Memory ではない） | なし |

`revalidate_triggers` の語彙は閉じた集合にする: `related_setting_changed`、`member_changed`、`model_changed`、`external_service_changed`、`phase_changed`（要件の例: 関連設定、Project メンバー、メインの Local LLM、外部サービス、開発フェーズ）。Event と Memory が同じ綴りで待ち合わせるため。

### 5. Stale Candidate・期限・Session / Task 終了の処理

Backend 内部の Job（`FreshnessMaintenance`）として行う。呼び出しの認可はなく、変更は `system` を Actor として `memory_metadata_changes` に残る。1 回の呼び出しで最大 `batch`（既定 500）件、`FOR UPDATE SKIP LOCKED`（人が編集中の Version は次回に回す）。同じ Version に 2 回印を付けない（何度実行しても同じ）。

| 対象 | きっかけ | 何をするか |
| --- | --- | --- |
| `revalidate` | `verified_at + revalidate_after` を過ぎた（その瞬間を含む。UTC） | `stale_since` を付ける。**`active` のまま**。Retrieval は Stale の印つき・低い Score で返す |
| `revalidate` | 待っている Trigger の Event（対象: User / Project / Repo / Workspace 全体） | 同上 |
| `repo_commit` | Repository の Head が Memory の commit と違う | 同上。別の Branch の Memory は、その Branch の Head でだけ判定する（Branch のない Memory は判定する） |
| `expiring` | `expires_at` を過ぎた | `deprecated` にする（Retrieval は元々除く。UI で終わったことが分かるように） |
| `session_only` | Session の終了（その Conversation を `memory_sources` に持つもの） | `deprecated` にする。消さない（消すのは会話削除の Flow） |
| `session_only` | Task の終了（その Task を `memory_sources` に持つもの: `source_type = task` で `source_ref` が Task ID の正規の文字列 `str(task_id)`。大文字や前後の飾りなど別の綴りは一致させない） | 同上 |

Stale Candidate への人の答え:

- まだ正しい → **Revalidate**（`revalidate_memory`）: 同じ内容で `n + 1` を書き、`verified_at` を今に、Stale の印なし。`supersedes` と `revalidated_from` を張る。`revalidate` の Memory だけ。
- 変わった → 編集。もう正しくない → 廃止。

Stale の印は古い Version に残る（いつ Stale になったかの履歴）。Job を呼ぶ Scheduler・Event の配線は、この Issue では作らない（呼び出し口だけ）。Task 終了の `end_task(task_id)` も呼び出し口だけで、`TaskService` の終了の遷移（`TaskService(listeners=[...])` の Transition Listener）への配線はしない（本番で `TaskService` を組み立てる Composition Root がまだなく、Approval の `revoke_on_task_end` と同じく後続の Issue で配線する）。配線されるまで、Task 由来の `session_only` は Task が終わっても `active` のまま残る（Retrieval は `session_only` を元々返さないので、候補には出ない）。

### 6. Migration

Revision `0042` は Index を 1 つ足すだけ: `ix_memory_versions_freshness_due`（`memory_versions (freshness_policy) WHERE status = 'active' AND freshness_policy <> 'permanent'`）。Job が履歴全体を読まないため。Table・列・制約・Trigger・権限は変えない。

## 選定理由

- Relation ごとの意味を固定し、退役させるのを `supersedes` だけにすると、曖昧な場合に何かが勝手に消えることがない（要件「曖昧な conflict ではユーザー確認」）。
- 手動の変更を人間に限り、書く Version を `confirmed` にするのは、要件「人間が編集して保存した内容は原則 Confirmed」と、Decision 0018 の「Worker は Confirmed を作れない」に揃えるため。
- Repo / Project Group を外すのは、権限の対応が決まらないまま書き込みを許すと、読めるだけの人が書ける状態を作りうるため（Fail-closed）。
- Stale を `active` のまま印だけにするのは、要件「期限到達で即時無効化しない」のまま、Retrieval（PAW-043）が既に持つ `on_stale: lower_priority` をそのまま使えるため。

## 代替案

- **`supersedes` を公開範囲をまたいで許す**: 置き換えで他の人から Memory が消える。採らない。
- **Revalidate を `stale_since` を外すだけにする（新しい Version を作らない）**: `verified_at` は Version の不変の列で、いつ誰が確かめたかが履歴に残らない。採らない。
- **期限切れの `expiring` を `deprecated` にしない**: Retrieval からは消えるが、UI では `active` のままに見える。採らない（ただし推奨の強さは弱い）。
- **`session_only` を Session 終了時に物理削除する**: 「物理削除しない」の原則と、会話削除の Flow（確認・複数 Source の扱い）を迂回する。採らない。
- **Repo Memory の手動編集を `project.memory.use`（Repo では `read`）で許す**: Repo を読めるだけの人が書ける。Override の `write` を要する案と比べて決める必要がある。この Issue では扱わない。
- **Trigger の語彙を自由な文字列にする**: 綴りの違いで Event が Memory に届かない。採らない。

## リスク

- Repo Memory と Project Group Memory はまだ人が編集できない（Journal の候補も User Scope に書かれるため、実害は当面ない）。
- 手動の Relation に Actor の列はない（`memory_relations` に Actor 列がない）。誰が記録したかは Authorizer の Audit の行（`memory.use` / `project.memory.use`）と、`supersedes` の場合は Status 変更の履歴で分かる。Relation 自体に Actor を持たせるなら Migration が要る（History Graph（UI）の Issue で判断）。
- Job を定期的に呼ぶ仕組みはまだない。呼ばれるまで、`revalidate` の期限切れは Retrieval が時刻で判定して Stale として扱う（PAW-043 の既存の動作）ので、低い Score になることは変わらない。印（`stale_since`）と履歴が付くのが遅れるだけである。
- 暫定の範囲（1 時間〜10 年、Batch 500 など）は実運用で見直す可能性がある。Migration なしで変えられる。
- 手動の `supersedes` を誤って記録すると、この Issue の範囲では元に戻せない（1）。
- 循環を作りうる手動の Relation は Workspace 全体で 1 件ずつになる。手動の操作なので量は小さいとみなす。

## 決めてほしいこと

1. Relation の意味（1 の表）。退役させるのは `supersedes` だけで、同じ公開範囲に限ること（推奨: 承認）。
2. `confirmed_from` / `revalidated_from` を操作が自動で張り、`merged_from` をこの Issue で扱わないこと（推奨: 承認）。
3. 手動編集・復元・廃止の Version の作り方（2）。Scope は編集で**狭めることだけ**ができ（`project → user`、編集者本人の Memory になる。同じ Transaction で `project` の Version を退役させる）、広げることはできないこと（推奨: 承認）。
4. 手動の変更を人間の `user`（`memory.use`）と `project`（`project.memory.use`、Contributor 以上）に限り、`repo` / `project_group` を今は扱わないこと（推奨: 承認。Repo は別の Decision で `write` Override を要するかを決める）。
5. 手動で書ける鮮度（4 の表）と、`revalidate_triggers` の閉じた語彙（推奨: 承認）。
6. Stale Candidate・期限切れ・Session / Task 終了の処理（5 の表。Task の出典を `source_ref = str(task_id)` で照合することを含む）、Revalidate で新しい Version を作ること（推奨: 承認）。
7. Job を呼ぶ Scheduler / Event の配線を後の Issue にすること。Task 終了の `end_task` を `TaskService` の Transition Listener に配線することを含む（推奨: 承認。後続の課題）。
8. 手動の `supersedes` を今は取り消せないこと、取り消しの操作を History Graph（UI）の Issue で決めること（推奨: 承認）。
9. 編集・復元・Revalidate の新しい Version に `memory_sources` を写さないこと（推奨: この Issue では写さず、会話削除の Flow の Issue で「写す」か「`user_confirmation` の出典を足す」かを決める）。
10. `extends` / `conflicts_with` に両方の Memory の書き込み権限を要し、公開範囲をまたいでも許すこと（推奨: 承認）。
11. 履歴を公開範囲ごとに絞り、復元は同じ公開範囲の Version からだけ行うこと（推奨: 承認）。
12. Scope の縮小（`project → user`）を誰に許すか。案 A: Contributor 以上（その Project Memory を編集・廃止できる人。実装はこれ）。案 B: Manager だけ（メンバーから Memory を外す操作を管理者に限る）。推奨: **A**。Contributor は既に廃止で同じ結果を得られ、B にしても「廃止して自分で作る」で回避できるため。縮小先を編集者本人以外の User にすることは扱わない（他人の Private Memory を作ることになる）。

## 承認時の決定（2026-09-28）

Human は、作業 Session で上の 12 点について推奨つきの説明を受け、「推奨どおり」と回答して承認した（12 点を一括で。個別の変更はない）。**12 点すべてが推奨どおり**である。

1. Relation の意味（1 の表）。退役させるのは `supersedes` だけで、同じ公開範囲に限る: 推奨どおり承認。
2. `confirmed_from` / `revalidated_from` を操作が自動で張り、`merged_from` をこの Issue で扱わない: 推奨どおり承認。
3. 手動編集・復元・廃止の Version の作り方（2）。Scope は編集で狭めることだけができ（`project → user`、編集者本人の Memory になる。同じ Transaction で `project` の Version を退役させる）、広げることはできない: 推奨どおり承認。
4. 手動の変更を人間の `user`（`memory.use`）と `project`（`project.memory.use`、Contributor 以上）に限り、`repo` / `project_group` を今は扱わない: 推奨どおり承認（Repo は別の Decision で `write` Override を要するかを決める）。
5. 手動で書ける鮮度（4 の表）と、`revalidate_triggers` の閉じた語彙: 推奨どおり承認。
6. Stale Candidate・期限切れ・Session / Task 終了の処理（5 の表。Task の出典を `source_ref = str(task_id)` で照合することを含む）、Revalidate で新しい Version を作る: 推奨どおり承認。
7. Job を呼ぶ Scheduler / Event の配線（`end_task` を `TaskService` の Transition Listener に配線することを含む）を後の Issue にする: 推奨どおり承認（後続の課題）。
8. 手動の `supersedes` を今は取り消せず、取り消しの操作を History Graph（UI）の Issue で決める: 推奨どおり承認。
9. 編集・復元・Revalidate の新しい Version に `memory_sources` を写さない: 推奨どおり、この Issue では写さず、会話削除の Flow の Issue で「写す」か「`user_confirmation` の出典を足す」かを決める。
10. `extends` / `conflicts_with` に両方の Memory の書き込み権限を要し、公開範囲をまたいでも許す: 推奨どおり承認。
11. 履歴を公開範囲ごとに絞り、復元は同じ公開範囲の Version からだけ行う: 推奨どおり承認。
12. Scope の縮小（`project → user`）を許す相手: 推奨どおり**案 A（Contributor 以上）**。縮小先を編集者本人以外の User にすることは扱わない。

承認後に方針を変える場合は、この Decision を書き換えず、新しい Decision から `Supersedes` する。
