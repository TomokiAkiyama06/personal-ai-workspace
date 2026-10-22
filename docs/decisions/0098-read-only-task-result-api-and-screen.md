# 読み取りだけの Task の結果を Task の API と画面に出す（誰が読めるか、Repository の ACL と Redact、API の形と大きさ、出す時、画面の置き場所、読んだことの監査）

- Status: Proposed
- Date: 2026-10-22
- Scope: Issue [#290](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/290)（[#279](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/279) の後続）。この Decision は設計だけで、コードは承認の後の PR で書く。関係する既存の口: `apps/backend/paw_backend/orchestrator/orchestrator.py`（`_read_only_result`）、`tasks/service.py`（`TaskService.execute(..., result=...)`、`MAX_RESULT_BYTES`）、`api/v1/tasks.py`（`GET /tasks/{task_id}`）、`api/v1/task_views.py`（`may_read_repository`、`readable_repositories`）、`tools/credentials.py`（`redact_text`）、`integration/changes.py`（制御文字の Escape）、Web の `apps/web/src/tasks/TasksPage.tsx`（`TaskDetailView`）と `apiSource.ts`
- Supersedes（一部、下の 3 が推奨どおりなら）: [Decision 0067](0067-task-pr-http-api.md)（Approved）の 2 の「Task の入力、Log、Node の Goal / 結果、Event の詳細は返さない」のうち、**読み取りだけの Task が `completed` で終わったときの `complete` の Event の `detail.result`（成功した各 Node の `key`・`role`・`title`・`summary`）だけ** を、新しい経路で返す。0067 の他（Task の入力・Log・Node の Goal・普通の Task の Node の結果・他の Event の詳細は返さない、Node の詳細にツール呼び出しを出さない、見える範囲、操作の権限）は変えない。[Decision 0094](0094-task-create-entry-open-points.md)（Approved）の 1 の (c)「結果（各 Node の要約）を Task に残す」は変えない（残したものを読む口を足すだけ）

## 背景

PR #284（#279、Decision 0094 の 1 の A）で、読み取りだけの Task（`tasks.read_only`）は DAG が成功すると `running` から `completed` になり、成功した各 Node の `key`・`role`・`title`・`summary` を `complete` の Event の `detail.result`（`{"nodes": [...]}`、Plan の順）に残す。大きさは `MAX_RESULT_BYTES`（`json.dumps` の ASCII で 1 MiB）で確かめる。実際の最大は Node 32・Title 100 文字・Summary 2,000 文字で約 810 KiB（#284 の Codex review）。`completed` は終わりの状態で（Retry / Restart は `failed` / `cancelled` だけ）、1 つの Task に `complete` の Event は高々 1 つ。

Decision 0067 の 2 は Task の API が Event の詳細と Node の結果を返さないと決めているので、結果は今 API にも Web にも出ず、確認は DB（`task_events`）を読む（`docs/DEPLOYMENT_UPDATE.md`）。2026-10-22、Human は #284 への回答で「結果を UI に出す件は別の Issue」とした（#290）。

既存の Decision と要件が決めていること:

- Task を見られるのは、その Project に `project.read` がある人（受諾した Membership。Viewer を含む）。System Role（Owner / Admin）だけでは所属しない Project を見られない（0004・0008・0022・0067 の 2）。存在しない Task と見られない Task はどちらも 403 `forbidden`。
- Repository の ACL override は Project の権限を狭めるだけ（REQUIREMENTS「Project roles」）。ACL で読めない Repository は Task の詳細・一覧・PR の一覧から外し、名前も出さない（0067 の 2）。
- **Path の単位の ACL は要件にない。** `TaskScope.excluded_paths` は System が決める除外（Worktree の置き場所、他の Checkout、`.git`、`NodeWorktree.protected`）で、Sandbox の Mask と Broker の Scope の判定が Agent に読ませない（0036 の 2、0083 の 6）。利用者が Repository の中の Path を隠す設定はない。
- Agent の Tool の結果は Broker が Credential を Redact する（0006）。ただし Node の `summary` は Model が書いた自由な文で、保存の時に Redact していない（`NodeResult` は長さと文字の種類だけを確かめる）。PR の差分は表示の前に `redact_text` と制御文字の Escape を通す（0078 の 2）。
- 読み取り専用の Capability の判定は拒否だけを監査する（0004）。PR の差分（Repository の中身）の閲覧も同じ（0078 の 3）。
- Web の画面は PAW-060 の Design canvas に従い、UI の PR は人が画面を確かめてから Merge する。

**次のことは決まっていない。** 結果を誰が読めるか（Working Set の一部の Repository を ACL で読めない Member がいるとき）。自由な文の結果に Redact をどうかけるか。API の形（Task の詳細に足すか、別の経路か）と大きさ。どの Task のどの状態で出すか。画面のどこに出すか。読んだことを監査するか。

## 提案（推奨の全体）

- `GET /api/v1/tasks/{task_id}/result` を足す。Guard は `GET /tasks/{task_id}` と同じ（Task の Project に `project.read`）。加えて、**読む人が Task の Working Set のすべての Repository を（今の ACL で）読めるときだけ** 返す。返すのは `complete` の Event の `detail.result` を、表示の前に `redact_text` と制御文字の Escape に通したもの（保存した行は変えない）。結果がない・読めないときは区別せずに 404 `task_result_not_found`。
- `GET /tasks/{task_id}` の答えに `has_result`（`bool`）だけを足す（同じ条件で `true`）。詳細は Task を開くたび・Control の後・動いている間は 5 秒ごとに読まれる軽い答えなので、最大約 1 MiB の本文は載せない。
- 出すのは読み取りだけの Task が `completed` のときだけ。
- Web の Task の詳細で、`has_result` のとき、DAG の Panel の上に「結果」の Panel を出し、Node ごとに Title・役割・要約を Plan の順に、ただの文（Markdown にしない、Link にしない、改行は保つ）で出す。読み込みは Panel を開いた時に 1 回。
- 読んだことは、他の読み取りと同じく拒否だけを監査する。

## 決めてほしいこと

### 1. 誰が結果を読めるか

結果は Agent が Working Set の Repository を読んで書いた自由な文なので、どの文がどの Repository から来たかは分けられない。一方 0067 の 2 は、ACL で読めない Repository を名前も含めて見せない。

- **A（推奨）: Task の Project に `project.read` があり、かつ Task の Working Set のすべての Repository を今の ACL で `project.read` で読める人。** Task の作成者も同じ条件（作成の後に ACL が狭められたら、作成者でも読めない）。Owner / Admin の System Role だけでは読めない（0004）。Viewer は読める（REQUIREMENTS の Viewer は「Task の閲覧」を持つ）。
- B: Task の作成者だけ（と、A の条件）。良い点: 狭い。悪い点: 同じ Project の Member に調べた結果を共有できず、読み取りだけの Task の使い道（調べものの共有）に合わない。
- C: Project に `project.read` があれば、Repository の ACL を問わず読める。悪い点: ACL で拒否された Repository の中身が、要約を通して読めてしまう（0067 の 2 と REQUIREMENTS の ACL override の意味を崩す）。推奨しない。
- D: Owner / Admin は所属しなくても読める。悪い点: 0004 の「System Role は Project を開かない」を崩す。推奨しない。

### 2. Repository の ACL・除外 Path・Redact

- Repository の ACL: 1 の A のとおり、すべてを読めるときだけ出す（一部を隠して残りを出すことはしない。文を Repository ごとに分けられないため）。
- 除外 Path: **追加の Filter はしない（推奨）。** `excluded_paths` と `protected` は System の除外で、Sandbox の Mask と Broker が Agent に読ませないので、結果にも入らない。利用者が決める Path の単位の ACL は要件にない。もし後で Path の単位の ACL を入れるなら、そのとき結果の扱いをこの Decision の Supersede で決め直す（自由な文から Path の中身を取り除くことはできないので、「その Path を隠される人には結果を出さない」になるはず）。
- Redact:
  - **A（推奨）: 返す時に、各 Node の `title` と `summary` を `tools.credentials.redact_text` に通し、Tab と改行以外の制御文字を見える Escape にする（0078 の 2 の差分と同じ規則、`integration/changes.py` の関数を共用）。** 保存した Event の行は変えない（記録・Evidence として残す。DB を読めるのは Operator だけ）。既に保存された結果にもそのまま効く。答えに Redact した数（`redacted`）を付け、画面は 1 以上のとき「認証情報らしい文字列を隠しました」と出す。
  - B: 保存の時（`_read_only_result`）にも Redact する。良い点: DB に Credential が残りにくい。悪い点: 0094 の 1 の (c) の記録の形を変える。Tool の結果は既に Broker が Redact しているので、Model が要約に Credential を書く経路は狭い。A に足して後から入れられる（A は冪等）。今は推奨しない。
  - C: Redact しない。推奨しない。
- 結果は Agent が書いた信頼しない文として扱い、Web は HTML・Markdown・Link にしない（3 の画面）。

### 3. API の形・大きさ・切り詰め（0067 の 2 の一部を Supersede）

- **A（推奨）: 別の経路 `GET /api/v1/tasks/{task_id}/result`。** 答えは `{"nodes": [{"key", "role", "title", "summary"}], "redacted": <数>, "completed_at": <時刻>}`。`GET /tasks/{task_id}` には `has_result` だけを足す。理由: 詳細は Task を開くたび・Control の後・動いている間は 5 秒ごと（`REFRESH_MS`。`completed` は読み直さない）に読まれる軽い答えで、最大約 1 MiB をそのたびに運ばない。`completed` は終わりの状態なので、結果は一度読めば変わらない。経路を分けると、1 の条件を詳細と別に判定できる。
  - 大きさ: 保存の上限（1 MiB）の中で全部を返す。Page 送りと切り詰めはしない（Node は最大 32、Summary は 2,000 文字で、Escape で伸びても数 MiB を超えない）。
  - 答えは `Cache-Control: no-store`（他の API と同じ）。
  - 結果がない（読み取りだけでない・`completed` でない）ときと、1 の条件で読めないときは、どちらも 404 `task_result_not_found`（区別しない。区別すると、読めない Repository が Working Set にあることが分かる。0067 の 2 は名前も出さない）。Task そのものが見えないときは今と同じ 403 `forbidden`。
- B: `GET /tasks/{task_id}` の答えに `result` を足す。良い点: 経路が 1 つ。悪い点: Task を開くたびに最大約 1 MiB。詳細の判定（`project.read`）と結果の判定（1 の Repository の条件）が 1 つの答えに混ざる。
- C: B だが詳細では先頭だけ（例 Node ごとに 300 文字）を返し、全部は別の経路。悪い点: 2 つの形を保つ。今の大きさでは要らない。
- Supersede の範囲: 0067 の 2 の「Event の詳細・Node の結果は返さない」のうち、上の経路で読み取りだけの `completed` の Task の `complete` の Event の `detail.result` を返すことだけ。普通の Task の Node の結果、Node の Goal、Task の入力、Log、他の Event の詳細は今のまま返さない。Task の一覧（`GET /tasks`）にも足さない。`api/v1/tasks.py` の Module の説明（「The answers carry no ... node goal or result and no event detail」）を実装の PR で改める。

### 4. どの Task のどの状態で出すか

- **A（推奨）: 読み取りだけの Task が `completed` のときだけ。** 結果が保存されるのはその時だけ（0094 の 1 の (c)）。
- B: `failed` / `cancelled` の読み取りだけの Task でも、その時までに成功した Node の要約（DAG の Node の行にある）を「途中までの結果」として出す。良い点: 失敗した調べものでも途中が読める。悪い点: 0094 の (c) の外の新しい読み取りの Model（DAG の行を読む）で、Retry で Node が上書きされる時の扱いが要る。要るなら別の Issue（未決）。
- 普通の（書く）Task の Node の結果は、この Decision では出さない（PR の画面と Review の要約は 0078 の範囲）。

### 5. Web の置き場所

- **A（推奨）: Task の詳細（`TaskDetailView`）で、`has_result` のとき、「いまの作業」の帯と DAG の Panel の間に「結果」の Panel（`task-card-panel` の形）を出す。** 中身は Plan の順の Node ごとの区切りで、見出しに Title と役割の Badge、本文に要約を `white-space: pre-wrap` のただの文で出す（Markdown にしない、URL も Link にしない）。Panel を表示した時に結果を 1 回読み、Task が変わらない間は読み直さない。読めなかった（404）ときは Panel を出さない。Phone の幅でも同じ Panel を縦に並べる。Redact した数が 1 以上なら Panel の下に注記。
  - DAG の Node を選んだ時の詳細（0067 の 5）には要約を出さない（出す場所を 1 つにする）。
  - Design canvas（PAW-060）に「結果」の Panel はないので、既存の `task-card-panel`・見出し・Badge・文字の Token だけで組み、新しい色や部品を足さない。UI の PR は Human が画面を確かめてから Merge する（Screenshot を PR に付ける）。
- B: 別の Page（`/agents/<id>/result`）。悪い点: 画面が 1 つ増え、Design canvas の外の Navigation が要る。
- C: DAG の各 Node の詳細に要約を出す。悪い点: 0067 の 5 の「Node の詳細には出さない」に触れ、全体を通して読みにくい。

### 6. 読んだことの監査

- **A（推奨）: 他の読み取り（Task の詳細・PR の差分、0004・0078 の 3）と同じく、`project.read` の判定の拒否だけを監査する。** 1 の Repository の条件で読めないときの 404 も、Repository の判定の拒否として Capability の監査（`audit_events`）に残す（`may_read_repository` の判定と同じ扱い）。結果の本文は Log・Audit に出さない。
- B: 成功した読み取りも、新しい Action（例 `task.result.read`、Task の ID・Node の数・`redacted` の数だけ。本文なし）で監査する。良い点: Repository の中身の要約を誰が読んだか残る。悪い点: 差分の閲覧（同じく Repository の中身）を監査しない 0078 と揃わず、5 秒ごとではないが画面を開くたびに行が増える。差分と一緒に「中身の閲覧の監査」を決める別の Decision にするなら、そのとき両方に入れる。

## Test の方針（実装の PR）

- Backend: 読み取りだけで `completed` の Task は結果を返す（Plan の順、4 つの Field）。`has_result` が同じ条件で `true` / `false`。普通の Task・`running` / `failed` / `cancelled` の読み取りだけの Task は 404 `task_result_not_found`。Working Set の 1 つの Repository を ACL で拒否された Member（作成者を含む）は 404 で、詳細の `has_result` は `false`、拒否は監査される。所属しない Owner / Admin と存在しない Task は 403 `forbidden`。Viewer は読める。Summary・Title の Credential（`ghp_...` など）は Redact され `redacted` が数える、保存した Event の行は変わらない。制御文字は Escape される。最大の結果（32 Node・最悪の文字）を返せる。Route の Guard の一覧（`tests/test_authz_routes.py`）に足す。
- Web: `has_result` のときだけ Panel を出し、1 回だけ読む（Poll では読み直さない）。要約の中の `<script>`・Markdown・URL がただの文で出る。404 で Panel を出さない。Redact の注記。日本語と英語の文言。Phone の幅。
- 既存の Test を消さない・緩めない。

## PR の分け方

1. Backend: 経路・`has_result`・1 の条件・Redact と Escape・Test・`api/v1/tasks.py` の説明と `apps/backend/README.md`。
2. Web: 「結果」の Panel・`apiSource.ts`・i18n・Test。Screenshot を付け、Human が画面を確かめてから Merge。
3. `docs/DEPLOYMENT_UPDATE.md` の段 3 の確認を、DB の代わりに画面（または `GET /tasks/{id}/result`）で見る手順に改める（2 の PR に含めてよい）。

Migration は要らない（`task_events` の `(task_id, seq)` の Index で `complete` の Event を読む）。

## 未決（この Decision では決めない）

- `failed` / `cancelled` の読み取りだけの Task の途中までの結果（4 の B）。
- 保存の時の Redact（2 の B）と、Repository の中身の閲覧の監査（6 の B）を差分と一緒に決めること。
- 結果の Markdown の表示・写し（Copy）・書き出し。
- Path の単位の ACL を入れるときの結果の扱い（2）。

## リスク

- 1 の A では、Working Set の 1 つの Repository を読めない Member は結果の全部を読めない（一部だけ出すことはしない）。安全側の振る舞いで、要るなら作成者が読める Repository だけで Task を作り直す。
- Redact は既知の形の Credential だけを見つける。形の分からない秘密（社内の名前など）は、Repository を読める人だけが読むこと（1 の A）で守る。

## 承認後の扱い

承認されたら Status を Approved に改め、承認の内容を記録する。0067 の本文は書き換えず、この Decision が一部を Supersede することを README の表に記す。
方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。
