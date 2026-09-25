# Memory の Immediate Journal と Background Consolidation の方針

- Status: Proposed
- Date: 2026-09-25
- Scope: PAW-041（Immediate Journal / Background Consolidation。Migration `0041`、`paw_backend/memory/journal/`）と、Journal・Consolidation Queue を使う以降の Issue（PAW-042 Conflict / Versioning、PAW-043 Retrieval、PAW-044 Inferred Preference Confirmation Flow、PAW-045 Markdown Projection）
- Supersedes: なし
- Approval: 未承認（Human の承認待ち。承認前は下の値と選択を暫定の実装として扱う）

## 背景

[REQUIREMENTS.md](../../REQUIREMENTS.md) の「Immediate Journal / Background Consolidation」は、次を定めている。

- User Message を受けたら、Raw Conversation と Pending Observation（`turn_id` / `conversation_id` / `event_sequence` / Project・Repo の Context / 時刻 / 処理状態）を**即時に**保存し、GPU や重い LLM 処理に依存させない。
- 重い整理（Candidate 分類、Scope 判定、Duplicate / Conflict 判定、Embedding、Projection）は非同期の Worker が行う。
- 順序は、Worker の完了順ではなく、`conversation_id` / `turn_id` / `event_sequence` / `created_at` / `base_memory_version` を基準に適用する。古い Turn が後から終わっても、新しい Memory を上書きしない。
- Memory Worker は HIGH / NORMAL / LOW の優先度 Queue を持つ。GPU が止まっていても Raw と Pending Observation は保存し、GPU 復帰後に整理を再開する。

一方で、**次のことは要件も Backlog も定めていない**。PAW-041 の実装は、動かすために暫定の選択と数値を置いた。
[AGENTS.md](../../AGENTS.md) の「仕様変更」に従い、Human が承認または変更できるように一覧にする。

- Worker の出力（Benchmark の `memory-worker-output-v1`）の `scope` と `state` を、Backend がどう扱うか。要件は「LLM は Scope や公開範囲を推奨してよいが、ACL の最終決定は Backend」「Inferred を自動で Confirmed にしない」と述べるだけである。
- 優先度（HIGH / NORMAL / LOW）を誰が、何を基準に決めるか。
- Queue の Lease、再試行、Backoff、Dead Letter、Batch の数値。
- Raw Conversation と Pending Observation の保持（要件は Raw を「無期限」とだけ述べる）。
- 高リスクの領域（Merge、Delete、公開範囲、ACL、Credential、外部送信）を Worker の出力がどう扱うか（要件は「推測だけで昇格させない」）。

実装は [Backend README](../../apps/backend/README.md) の「Immediate Journal / Background Consolidation」に書いている。

## 提案

### 1. 構成: Entry と Pending Observation は 1 つの Table、Queue は別

- `memory_journal_entries` は、User Message 1 件につき 1 行。`conversation_id` / `message_id` / `turn_id` / `event_sequence` / `owner_user_id` / `project_id` / `repo_id` / `recorded_at` は書き換えない。`state`（`pending` / `consolidated`）、`consolidated_at`、`outcome` だけが更新できる。**`state = 'pending'` の行が Pending Observation** である。
- 本文は複製せず、Raw の `messages` を指す（複合 Foreign Key、`ON DELETE CASCADE`）。**Conversation を消すと Entry、Job、Outcome も一緒に消える**（Raw の削除が Journal に本文を残さない）。
- `memory_consolidation_queue` は Job（Lease、優先度、再試行、Dead Letter）。1 つの Entry にある有効な（`queued` / `claimed`）Job は最大 1 つ（Partial Unique Index）。
- `memory_consolidation_keys` は、Worker の `key`（Owner ごと）が指す Memory と、その現在の Version を作った Entry の順序の印を持つ（下の 6）。
- Pending Observation を持つのは User Message だけ。Assistant / Tool / Agent / Task の Message は Raw だけで、同じ Sequence を使う（`append_message`）。

### 2. Event Sequence

- 会話ごとに 0 から始まる連番で、欠番がなく、Commit の順に並ぶ。Conversation の行を `FOR NO KEY UPDATE` で Lock し、その会話の最大値 + 1 を割り当てる。
- **会話の Message は、Journal を通してだけ追加する**（同じ Lock を取るか、`MemoryJournal` を使う）。自分で番号を選ぶ書き込みは、衝突するか順序を壊す。
- Project 単位の Sequence は持たない。要件が挙げるのは `conversation_id` / `turn_id` / `event_sequence` で、会話をまたぐ順序は、時刻（DB の時計）と `base_memory_version` 相当の保護（下の 6）で決める。

### 3. Raw と Journal の保持

- Raw Conversation は要件どおり無期限（削除は Conversation の削除だけ）。Journal Entry と Outcome は Conversation と同じ期間で、Conversation と一緒に消える。
- 完了・Dead Letter の Job は履歴として消さない。古い Job の圧縮は本 Issue の範囲外（必要なら別 Issue）。

### 4. 優先度の割り当て

- **呼び出し側（Chat の層）が決める。** `record_user_message(..., priority=...)`。Journal は本文を読んで判断しない（本文の Keyword で HIGH にしない）。
- HIGH: ユーザーが明示的に Preference / Decision を保存した、または次の Turn から必要な設定。NORMAL: 既定。LOW: 再処理・Requeue（Embedding 再生成、Repo 再解析、履歴圧縮は本 Issue の範囲外）。
- Aging はない（Decision 0007 と同じ。LOW は HIGH / NORMAL が来る限り待つ）。実行中の Job を HIGH が中断することもない。

### 5. Queue の数値（暫定値）

| 項目 | 値 | 理由 |
| --- | --- | --- |
| Lease | 300 秒 | Worker の呼び出し（120 秒）と書き込みが十分収まる |
| Worker の呼び出しの上限 | 120 秒 | Lease の半分以下（Heartbeat せずに待つため。Constructor が検査） |
| 失敗の上限（`max_attempts`） | 5 回 | 5 回目の失敗で Dead Letter |
| Backoff | 30 秒 × 2^(n-1)、上限 900 秒（15 分） | 決め打ち。Jitter なし（Worker が 1 つの間は不要） |
| Batch | 1 回の Batch で最大 10 Job | Worker が停止中は、最初の 1 件で Batch を止める |
| Lock の待ち | 3 秒 | 待ち切れなければ何も変えずに `JournalBusyError` |

- **GPU が使えない（`WorkerUnavailableError` / `ConnectionError`）のは失敗として数えない**（`attempts` に加えず `deferrals` に加える）。遅延は同じ式で伸び、Dead Letter にはならない（GPU 復帰後に再開する要件のため）。
- Timeout、Worker の他の例外、出力の契約違反、書き込みの失敗は数える。5 回で Dead Letter。
- Lease が切れた Job を再度 Claim するのは、前の Worker が報告せずに消えたので、失敗 1 回として数える（Worker を落とし続ける Job を無限に再実行しない）。
- Dead Letter の Job は消さず、Entry は `pending` のまま（**Pending は失われない**）。戻すのは `ConsolidationQueue.enqueue`（新しい Job）。User 向けの「再試行」の Endpoint は本 Issue に含めない。

### 6. 順序の保証（古い Turn が新しい Memory を上書きしない）

- Claim の順は優先度に従う（HIGH の意味を保つため、会話ごとの直列化はしない）。**適用時に**、Event の順で守る。
- Memory の現在の Version を作った Entry の順序の印（会話、`event_sequence`、記録時刻）を `memory_consolidation_keys` に持つ。Candidate は、その Entry より**新しい**ときだけ適用する。同じ会話なら `event_sequence`、会話が違えば記録時刻（同時刻は Conversation ID）で比べる。古ければ `stale` として記録し、書かない（Raw は残る）。
- Version の書き込みは、`(memory_id, version_number)` の Unique と、`active` は 1 つの Unique Index、`UPDATE ... WHERE status = 'active'` の行数で、手動編集などの別の書き込みとの Lost Update を失敗（`ApplyConflict`）にする。失敗した Job は、新しい状態を読み直して再試行される。

### 7. Worker の出力と契約

- 契約は Benchmark の `memory-worker-output-v1` と同じ（`key`、`scope`、`state`、`supersedes`（必須、`null` 可）、任意の `content`、任意の `conflicts_with`）。Benchmark で評価した Worker をそのまま接続できる。`tests/test_journal_worker_contract.py` が Schema File と比べる。
- `state` は Schema どおり `confirmed` / `inferred` の 2 値（**`observed` は Schema に無い**）。Worker は `async def extract(input_text: str) -> str`（Benchmark は同期。Event Loop を止めないため）。
- Schema にない上限を Backend が足す: 1 出力 20 件、`key` 200 文字（Version の `title` の上限）で制御文字なし、`content` 8,000 文字、`conflicts_with` 10 件、出力全体 400,000 文字。**1 つでも違反があれば出力全体を破棄する**（Benchmark の `schema_adherence` と同じ）。
- 出力は書き込みの前に全体を検証する。エラーの文言には出力を含めない（閉じた Code だけ）。

### 8. Scope と State（Worker の主張は「主張」であり決定ではない）

- **Candidate は必ず `user` Scope（会話の Owner だけが読める）に書く。** Owner は DB の Conversation から取り、Worker の出力には Owner を指定する手段がない。Worker の `project` / `repo` は `attributes.recommended_scope` に記録するだけで、範囲を広げる（`User → Project` は要件の「確認必須」）のは確認 Flow（PAW-044）が新しい Version で行う。
- `shared` は拒否する（Shared Memory へは自動で昇格しない。PAW-046 の承認だけが作る）。
- **Worker は Confirmed を作れない。** `inferred` → `inferred`、`confirmed` → `observed`（単発の明示的な発言）。元の主張は `attributes.worker_state` に残す。`confirmed` にするのは User の確認だけ。
- **Confirmed の Memory を置き換えない。** Candidate の内容が違えば `held_confirmed`（Outcome に保存し、Memory にしない）。同じ内容なら重複。User が却下・無効化した（`deprecated` / `history` / `rejected`）Memory は再び作らない（`blocked_by_user`）。
- 高リスクの領域（Merge、Delete / 破壊的操作、公開・公開範囲、ACL・Role・権限、Credential・Secret、外部送信）を key または内容に含む Candidate は、Memory にせず `held_high_risk`（Outcome に候補を保存し、PAW-044 が User に提示する）。語彙は英語の語（`merge`、`delete`、`permission`、`token` など）と日本語の句（「マージ」「削除」「権限」など）の暫定の一覧（`rules.py`）。**これは補助の網であり、保証の本体ではない**: どの状態の Memory も権限や実行を与えない（Tool Broker と Approval が決める）。
- `freshness_policy` は `permanent`、`memory_type` は `worker_candidate`、`actor_type` は `system`、`importance` は 50 で固定（鮮度の見直しは PAW-042）。

### 9. Version の関係（最小）

- 同じ key の新しい Version は、直前の `active` を `superseded` にし、`supersedes` の関係を追加する。Worker の `supersedes` が別の key を指し、その Memory が Confirmed でなければ、同様に置き換える（Confirmed なら `held_confirmed`）。`conflicts_with` は関係を追加するだけで、どちらの Memory も変えない。未知の key は無視する。
- 本格的な Conflict / Freshness の扱いは PAW-042。本 Issue は、Consolidator が Schema の規則を破らない最小の範囲に留める。

### 10. 権限

- 既存の Capability **`memory.use`（`Scope.SELF`、委任可）だけを使う。新しい Capability は追加しない。** `record_user_message` は人間の `Principal` だけ（Agent がユーザーの発言を作れない）。`append_message`、`pending_observations`、`sync_status` は Agent（委任元の Grant に `memory.use`）も可。他の User（Admin を含む）の Conversation は「見つからない」。
- `ConsolidationQueue` と `Consolidator` は Backend の内部部品（HTTP Endpoint も認可もない。Task Queue と同じ）。呼び出す Worker Process は本 Issue の範囲外。

### 11. Privacy

- Log、Audit、エラー、`repr` に会話の本文・key・内容を出さない（Job の ID と閉じた Code だけ）。Worker の例外の文言は読まない。
- Outcome（`held` の Candidate の本文を含む）は Owner の行にあり、Conversation と一緒に消える。Admin にも見せない。

## 却下した代替案

- **Worker の `scope` をそのまま使う。** 他の User に見える範囲を、モデルの出力だけで広げることになる（要件が禁じる）。
- **Worker の `confirmed` をそのまま `confirmed` にする。** モデルが会話中の貼り付けなどを誤って「確認済み」にでき、Confirmed が Inferred より優先される要件で、上書きに使われる。
- **Pending Observation と Entry を別 Table にする。** 1 対 1 で、状態の二重管理になる。Queue と分けたのは、Job は消えても（Dead Letter の入れ直し）Observation は残るため。
- **会話ごとに Job を直列化する（Head-of-line）。** HIGH が古い NORMAL の後ろに並び、優先度の意味がなくなる。順序は適用時の保護で守る。
- **本文の Keyword で HIGH を決める。** 誤判定が Product の動作になる。呼び出し側の明示に任せる。
- **Sequence を Sequence Object / Advisory Lock で採番する。** Sequence Object は Commit 順を保証せず欠番ができる。Row Lock は Conversation の削除とも直列化できる。
- **Unavailable も失敗として数える。** GPU 停止が数時間続くと Job がすべて Dead Letter になり、復帰後に人が戻す必要がある（要件は自動で再開）。

## 人間に決めてほしい点

| # | 決めてほしいこと | 推奨 |
| --- | --- | --- |
| 1 | Worker の `confirmed` を `observed` に下げる（Confirmed は User の確認だけ） | 下げる。将来、User が明示的に保存を操作した Message に限り `confirmed` を許す拡張は、別 Decision で |
| 2 | Candidate をすべて `user` Scope にし、`project` / `repo` は推奨として残す | そうする（公開範囲の拡大は確認 Flow） |
| 3 | 高リスクの語彙による保留（key と内容、英日） | 保留する。語彙の一覧は承認後も見直せる |
| 4 | 優先度を呼び出し側が決める（本文の Keyword で決めない） | そうする |
| 5 | Timeout を失敗として数える（Unavailable は数えない） | 数える。Timeout は入力が原因の可能性もあるため |
| 6 | Queue の数値（Lease 300 秒、失敗 5 回、Backoff 30 秒から 15 分、Batch 10、Worker 120 秒） | 暫定値として承認し、実測で見直す |
| 7 | 保留した Candidate の本文を Entry の `outcome` に持つ（PAW-044 が読む） | 持つ（別 Table にすると Privacy の対象が増える） |
| 8 | Dead Letter の復旧は運用（`enqueue`）とし、自動の再投入は置かない | そうする。User 向けの再試行は PAW-044 か API の Issue |

## リスク

- 高リスクの語彙は英日だけで、見逃しと過剰な保留がありうる。過剰な保留は User の確認を 1 回増やすだけだが、見逃した Candidate は `observed` / `inferred` の弱い Memory として `active` になる（権限は与えない）。
- `key` の正規化はしない（Benchmark と同じ、完全一致）。同じ意味の別の key は別の Memory になる。整理は PAW-042。
- Advisory Lock の Hash が衝突すると、無関係な key を直列化するだけ（Deadlock は Hash の順で取るため起きない）。
- `outcome` は最大 20 件 × 8,000 文字。`held` の Candidate の本文を含むので、1 行が大きくなりうる。
- Worker の呼び出しの間、Lease を延ばさない。Worker が上限を超えて実行を続けても、適用は Fencing で拒否される（結果は捨てられる）。
- 別の Process が Message を Journal を通さずに書くと、Sequence が衝突する（Unique 制約が失敗させる）。

## 承認後の扱い

- 承認されたら、Status を Approved にし、README の該当の節の「提案」を「承認された判断」へ移す。
- 数値と語彙は `journal/limits.py` と `rules.py` のデータで、変更しても Migration は不要。ただし CHECK 制約が繰り返す数値（`key` の 200 文字）を変える場合は新しい Migration が要る。
- 変更するときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。

## 決めてほしいこと

1. Worker の `confirmed` を `observed` に下げる方針（表の 1）。
2. すべての Candidate を `user` Scope に置く方針（表の 2）。
3. 高リスクの語彙による保留（表の 3）と、その語彙の一覧。
4. 優先度を呼び出し側が決める方針（表の 4）。
5. Timeout の扱い（表の 5）。
6. Queue の数値（表の 6）。
7. 保留した Candidate を Entry の `outcome` に持つこと（表の 7）。
8. Dead Letter の復旧の運用（表の 8）。
