# Inferred Preference の確認 Flow の方針（観測と候補の置き場所、根拠の計り方、いつ聞くか、推奨 Scope、確認・保存しないの書き方、Repo への確定の権限、高リスクの扱い、自由入力の構造化）

- Status: Approved
- Approval: 2026-10-08、Humanが作業Session内で、判断点ごとの説明（推奨つき）を受けたうえで直接回答して承認（すべての判断点を推奨どおり承認。末尾の「承認時の決定」）
- Date: 2026-10-07
- Scope: Issue [#38](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/38)（PAW-044: Inferred Preference Confirmation Flow）の Backend と API。`apps/backend/paw_backend/memory/preferences/`、`api/v1/memory_preferences.py`（`/api/v1/memory/preferences/*`）、Migration `0192`
- Supersedes: なし。[Decision 0018](0018-memory-journal-consolidation-policy.md)（Journal）・[0034](0034-memory-versioning-freshness.md)（Versioning）・[0045](0045-memory-edit-sources.md)（出典）・[0068](0068-memory-http-api.md)（Memory の HTTP API）（いずれも Approved）を変えない

## 背景

REQUIREMENTS.md「Inferred Preference / Confirmation Flow」と docs/MEMORY_ARCHITECTURE.md の 9 は次を FIXED としている。

- `Conversation / Action → Observation → Inferred Preference Candidate → evidence 蓄積 / scope 推測 → User Confirmation → Confirmed Memory`。
- 判断材料は `frequency`・`scope_diversity`・`language_strength`・`consistency`・`risk_level`。
- Scope の基本: 同じ Repo の中だけで反復 → Repo、同じ Project の複数 Repo → Project、複数 Project → User。
- 確認 UI は `[このRepoだけ] [このProject] [すべてのProject] [保存しない] [その他...]`。`その他...` は自然文で、LLM が構造（Scope、内容、例外、強さ、Risk、期限）に変える。
- Merge 権限・Delete・公開範囲の拡大・ACL / Role / Permission・Credential / Secret・外部送信は、推測だけで永続 Policy や実行権限にしない。高リスクの解釈は構造化して見せ、明示確認の後に適用する。

既に決まっていること: Journal の Worker は候補を必ず本人の `user` Scope に `observed` / `inferred` で書き、広げるのは確認 Flow（PAW-044）が新しい Version で行う。高リスク・Confirmed を置き換える・広げた Memory を変える候補は Memory にせず Entry の `outcome` に保留する（Decision 0018 の 2・7）。手動の Version は Decision 0034、出典の写し方は Decision 0045。

**次のことは要件も既存の Decision も決めていない。** 観測と候補をどこに置くか、5 つの判断材料の具体的な計り方、いつ確認を出すか、確認と「保存しない」をどう書くか、Repo Memory に確定する権限（Decision 0034 の 4 で未決）、高リスクの確定の手順、自由入力を構造にする手段と `project_group` の扱い。
[AGENTS.md](../../AGENTS.md) の「仕様変更」に従い、実装は下の推奨で置き、この Decision で承認を求める。UI は承認済みの Design board がないので作らない（13）。

## 提案

### 1. 観測（Observation）は Journal の記録を使い、新しく保存しない

Observation は、本人の Consolidated な Journal Entry（`memory_journal_entries`）の `outcome.items` のうち、Worker の key を名指す項目（結果が `created` / `updated` / `duplicate` / `held_*`）とする。Entry には会話の Project / Repository と時刻がある。Evidence は読むときに計算する（本人の行だけ。Message の本文は言葉の強さの計算にだけ使い、返さない）。

**推奨: これ。** 代替案は観測の Table を足す案で、会話の削除・User の消去の対象が増える（Decision 0018 の 7 と同じ理由で避ける）。会話を消すと観測も消え、根拠が減るのは意図どおり。

### 2. 候補（Candidate）は 2 種類

- **memory**: 本人の `user` Scope、`active`、`observed` / `inferred` の Version で、Journal の key に登録された Memory（`memory_consolidation_keys`）。
- **held**: key ごとに、まだ答えていない最新の保留項目（Decision 0018 の `held_high_risk` / `held_confirmed` / `held_widened`）。その後に同じ key の観測が書かれた（`created` / `updated` / `duplicate`）なら古いとみなして出さない。

**推奨: これ。** 手動で作った Memory（`confirmed`）は候補にしない。

### 3. 根拠（Evidence）の計り方と、いつ聞くか（`ready`）

| 材料 | 計り方 |
| --- | --- |
| `frequency` | その key を名指す Entry の数（key ごとに新しい方から最大 100） |
| `scope_diversity` | 観測の Project の数・Repository の数・Project 外の観測の数 |
| `language_strength` | 本人の Message の語: 「今後」「基本的に」「いつも」「毎回」「always」「from now on」等 → `standing`、「今回」「今だけ」「this time」等 → `once`。1 つでも `standing` なら `standing`、全部 `once` なら `once` |
| `consistency` | `conflicts_with` の関係が候補に付いている、または Confirmed と食い違う（`held_confirmed`）→ `conflicting`。Worker が内容を改めた（`updated`）→ `changed`。他は `consistent` |
| `risk_level` | Journal の高リスク語彙（`journal/rules.py` の `is_high_risk`）が key・内容・構造に当たる、または `held_high_risk`、または「必須」の規則（11）→ `high` |

`ready`（チャットで確認を出す）: `frequency >= 3`、または `standing` の発言が 1 回でもある。ただし全部 `once` なら出さず、`conflicting` なら出さない（候補には残り、矛盾として見せる）。高リスクも同じ規則（早く聞く理由にしない）。

**推奨: これ（3 回と語の一覧は暫定。Migration なしで変えられる）。** 代替案は回数だけで決める案で、「今後は」の 1 回目を 3 回待たせ、「今回は」を 3 回で聞いてしまう。

### 4. 推奨 Scope とボタン

- 推奨: 観測がすべて同じ Repository → Repo、すべて同じ Project（Repository が複数、または Project 内の Repository なしの会話を含む）→ Project、複数 Project または Project 外の観測を含む（観測なしも）→ User。
- ボタン: Repo は最も多く観測した Repository（同数なら最新）、Project は最も多く観測した Project、User は常に。観測のない Project / Repository のボタンは出さない。`[保存しない]` と `[その他...]` は別の API。
- 本人が以前に広げた Memory への保留（`held_widened`）は、その Project に留める、または本人の Memory に狭めるだけ（6）。

**推奨: これ。**

### 5. 確認（Confirmation）の書き方

- **memory 候補**: 同じ Memory の `n + 1` を `active`・`confirmed`・本人が Actor で、選んだ Scope に書く。`n` は `superseded`。`supersedes` と `confirmed_from` を `n + 1` から `n` に張り、出典は Decision 0045 のとおり写して本人の `user_confirmation` を足す。`memory_type = preference`、鮮度は `permanent`（要件: User Preference は permanent）、構造に期限があれば `expiring`。`attributes.preference` に key・選択・対象・Risk・確認したか・`policy_effect: "none"`・構造（11）を残す。
- **held 候補**: key の Memory があれば、その次の Version として同じ規則で書く（内容は保留された候補、出典はその観測の会話と `user_confirmation`）。なければ新しい Memory の Version 1 を書き、key に登録する（以後 Worker の同じ key の候補は Decision 0018 の規則で保留される）。key の順序の印は、保留された観測の方が新しければ進める。key の Memory が `deprecated`（本人が保存しない・廃止にした）なら、本人の明示の確定でその次の Version を書いてよい（復元と同じ）。他の Memory に置き換えられた（`superseded` / `history`）Memory への保留は「保存しない」だけを出す。保留項目には答え（`memory_preference_resolutions`）を残す。
- 広げる前の Private な Version は Private のまま（Decision 0034 の `history` のとおり、広げた後の読者には見えない）。
- 楽観 Lock: memory 候補は `expected_version`、held 候補は `(entry_id, item_index)` が key の最新の未回答の保留であること（違えば 409 `preference_candidate_changed`）。
- Lock の順: Journal の key の Advisory Lock → Memory の Advisory Lock → 行。

**推奨: これ。**

### 6. 広げてよい範囲

- 本人の Private な候補は、本人の Memory（`user`）・Project・Repository のどれにでも確定できる。Project は `project.memory.use`（Database から読んだ Role と状態。Contributor 以上、Active）。
- 本人が以前に広げた Memory（今の版が Project）への保留は、同じ Project に留めるか、本人の Memory に狭めるだけ（別の Project・Repository に移すのは 422 `not_allowed`）。今の版が Repository の Memory は Decision 0034 の 4 のとおりこの Flow でも変えない（404。「保存しない」だけができる）。

**推奨: これ。**

### 7. Repo Memory に確定する権限（Decision 0034 の 4 で未決だった点）

Repository の Scope に確定するには、その Repository の登録済みの行と保存された ACL で `project.memory.use` を判定する（Authorizer の `REPO_PERMISSION_OF` で `read` に対応。Override で `read` を外した Repository は拒否、Project が違う・存在しない Repository は `not_project_member`）。

**推奨: これ。** 代替案は Repository の `write`（`project.repo.write`）を要する案。Repo Memory はコードへの書き込みではなく、その Repository を読める人に見える知識なので、読める人の中で Memory を書ける人（Contributor 以上）に揃える。手動の Repo Memory の編集（Decision 0034 の 4）は引き続き別の Issue で、この判断を参照してよい。

### 8. 保存しない（Reject）

- **memory 候補**: `n + 1` を `confirmation_state = rejected`・`status = deprecated`（内容は同じ）で書き、`n` を `superseded`、`supersedes`（理由 `preference rejected`）を張る。内容が同じなので出典も写す（会話の削除の Flow が見つけられるように。確認ではないので `user_confirmation` は足さない）。以後 Journal は同じ key を作り直さない（Decision 0018 の `blocked_by_user`）。
- **held 候補**: その key の未回答の保留項目すべてに `rejected` を記録する。後で新しい保留項目が来たら、新しい質問として出す。

**推奨: これ。** 代替案は `deprecate_memory`（今の版の状態だけを変える）で、`rejected`（「保存しないと判断済み」、MEMORY_ARCHITECTURE.md の 9）が記録に残らない。

### 9. 保留への答えの Table（Migration 0192）

`memory_preference_resolutions`（`entry_id`、`item_index`、`owner_user_id`、`resolution` = `confirmed` / `rejected`、`memory_id`、`resolved_at`）。Entry と一緒に消える（`ON DELETE CASCADE`）、確定で書いた Memory は先に消えてよい（`SET NULL`）。Application の Role は SELECT と INSERT だけ。User の消去の確認の対象に入れる。あわせて `memory_journal_entries (owner_user_id, recorded_at)` の Index を足す（本人の Entry を新しい順に読むため）。

**推奨: これ。** 代替案は Entry の `outcome` に答えを書き足す案で、Consolidator の記録を書き換えることになる。

### 10. 高リスクの推測は自動で実行しない

- 高リスク（3 の `risk_level = high`）の候補は、確定の要求に `acknowledge_high_risk: true` がなければ 409 `preference_high_risk_unacknowledged` で何も書かない。Journal は高リスクの候補を Memory にしない（Decision 0018）ので、推測のまま弱い参考情報として LLM に渡ることもない。
- 確定しても結果は Memory だけで、`attributes.preference.policy_effect = "none"` を記録する。ACL・Role・Merge Policy・Approval・権限は Memory から読まれず、この Flow も変えない（Tool Broker と Approval が決める）。高リスクの Policy として実際に適用する Flow は作らない。
- 確定に Passkey の Step-up は求めない（自分の Memory の操作。Decision 0068 の 11 と同じ）。

**推奨: これ。** 代替案は高リスクの確定に Step-up を求める案で、権限を何も変えない操作に対して重い。

### 11. 自由入力（その他）の構造化

- `interpret` は何も書かず、構造（`scope` = `repo` / `project` / `user` / `project_group`、`apply_to`、`rule`、`exceptions`、`strength` = `default` / `required`、`expires_at`）と Risk のプレビューを返す。
- 構造にするのは、設定されていれば **モデルの Interpreter**（Port。`preference-interpretation-v1` の JSON を返す。Memory Worker と同じく出力は全体を検証し、ID は名指せない。Risk はモデルの値と Backend の判定の高い方）、なければ、またはモデルが失敗・時間切れ・契約違反なら **規則の Interpreter**（「このRepo」「このProject」「すべてのProject」「〜系のProject」、「ただし」「except」の後を例外、「必ず」「must」を `required`）。どちらが答えたかを `interpreted_by` で返す。
- Project / Repository は Backend が候補の観測から決める（4 のボタンと同じ）。人はプレビューを直して確定してよく、確定の要求の構造は Backend がもう一度検証し、書く本文から Risk を計り直す。
- `strength = required`（必須の規則のつもり）は Memory では強制できないので、本文に「強さ: 必須（Memory は実行や権限を強制しない）」と書き、高リスクとして明示確認を求める。
- モデルの Interpreter の本番の配線（どの Model・Runtime で動かすか）はこの Issue では作らない（`app.state.preference_interpreter` を置けば使う）。

**推奨: これ。** 代替案はモデルなしでは `その他...` を 503 にする案で、Local Model の配線まで自由入力が使えない。

### 12. `project_group`（「開発系の Project だけ」）

Project Group の実体はまだない（`memory_versions` の `project_group` は Group の ID しか持たず、Decision 0068 も出さない）。構造の `scope = project_group` は、**本人の Memory（`user`）に条件（`適用対象: …`）を書いて保存**する。

**推奨: これ。** 代替案は、Group を Project の一覧に展開して Project ごとに Memory を作る案（Group の定義がないので展開できない）、または Group の実体を作る案（Issue の範囲を超える）。

### 13. UI は作らない（後続の Issue）

承認済みの Design board がないため、この PR は Backend と API だけ。必要な画面は PR に書く（チャット内の確認カード、`その他...` の入力とプレビュー・高リスクの確認、Memory 画面の候補の一覧と根拠の表示、保留の候補の扱い）。

**推奨: これ。**

## 選定理由

- Decision 0018・0034・0045 の規則（Worker は Confirmed を作れない、広げるのは確認 Flow の新しい Version、出典を写す）をそのまま使い、新しい保存は保留への答えだけにする。
- 根拠は本人の行からその場で計算し、会話の削除・User の消去と矛盾しない。
- 高リスクの推測は、Journal（Memory にしない）と確認 Flow（明示確認なしに書かない）の 2 段で止め、確定しても権限には何も効かない。

## リスク

- 3 の語の一覧と回数は暫定で、聞くのが早すぎる・遅すぎることがある（書く内容には影響しない）。
- 規則の Interpreter は少数の言い回ししか分からない。分からない文は推奨 Scope のまま、本文は候補の内容のままになる（人がプレビューで直す）。
- 高リスクの語彙は英日の暫定の一覧（Decision 0018 と同じ）。見逃した候補は明示確認なしで確定できるが、確定しても権限は変わらない。
- 根拠の計算は本人の Entry を読む。Entry が非常に多いと遅くなる（key ごとに 100 件、候補は 200 件まで。Owner の Index を足した）。
- 確定の要求ではモデルの Risk の判定は分からない（プレビューは状態を持たない）。モデルだけが高リスクと言い、Backend の語彙が当たらない構造は、確定のときに明示確認を求めない（UI はプレビューの `requires_acknowledgement` に従って確認を出す前提）。
- 保留の理由が高リスクでない（`held_confirmed` / `held_widened`）候補は、内容が高リスクの語彙に当たらなければ明示確認なしで確定できる。
- 候補の一覧は 1 つの Snapshot ではない（READ COMMITTED）。並行して確定された候補が一瞬残ることがあり、その確定は 409 になる。

## 判断が必要な点（推奨つき）

1. 観測を Journal の `outcome` から読み、新しく保存しないこと（1）。推奨: 承認。
2. 候補を「未確認の Private な Memory」と「key ごとの最新の未回答の保留」の 2 種類にすること（2）。推奨: 承認。
3. 根拠の計り方と `ready` の規則（3 回、または `standing` の発言 1 回。全部 `once` と `conflicting` は聞かない）（3）。推奨: 承認（数と語は暫定）。
4. 推奨 Scope の規則とボタンの決め方（4）。推奨: 承認。
5. 確認を同じ Memory の新しい confirmed の Version（`supersedes` + `confirmed_from`、出典を写す、`memory_type = preference`、`permanent`）で書き、held 候補は key の Memory の次の Version か新しい登録済みの Memory にすること（5）。推奨: 承認。
6. 広げるのは本人の Private な候補からだけで、広げた Memory への保留は同じ Project に留めるか本人に狭めるだけにすること（6）。推奨: 承認。
7. Repo Memory への確定を、Repository の ACL つきの `project.memory.use`（`read` 相当）で許すこと（7。Decision 0034 の 4 の未決点）。推奨: 承認（代替: Repository の `write` を要する）。
8. 保存しないを `rejected` + `deprecated` の新しい Version（held は答えの記録）で書くこと（8）。推奨: 承認。
9. 保留への答えの Table と Journal の Owner の Index（Migration 0192）（9）。推奨: 承認。
10. 高リスクは `acknowledge_high_risk` なしでは書かず、確定しても `policy_effect: none` の Memory だけで、Step-up は求めないこと（10）。推奨: 承認。
11. 自由入力を、モデルの Interpreter（Port、未配線）と規則の Interpreter の Fallback で構造にし、プレビューを人が直して確定でき、Backend が検証と Risk を計り直すこと（11）。推奨: 承認（モデルの本番の配線は後続の Issue）。
12. `project_group` を本人の Memory に条件を書いて保存すること（12）。推奨: 承認。
13. UI を作らず、必要な画面を後続の Issue にすること（13）。推奨: 承認。

## 承認時の決定（2026-10-08）

Human は、作業 Session で判断が必要な点について推奨つきの説明を受け、直接回答して承認した（すべての判断点を推奨どおり承認）。
