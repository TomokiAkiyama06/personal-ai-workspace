# 読み取りだけの Task の結果を Task の API と画面に出す（誰が読めるか、Repository の ACL と Redact、API の形と大きさ、出す時、画面の置き場所、読んだことの監査、読めない Repository があることの開示）

- Status: Approved
- Approval: 2026-10-23、Humanが作業Session内で、判断点ごとの説明（推奨つき）を受けたうえで承認（2 の Redact だけ推奨と違い「そのまま出して下さい。見にくくなるので」、他は推奨どおり: 1 は A（Project に `project.read` があり、Working Set に一度でも入ったすべての Repository を今の ACL で読める人。作成者も同じ、System Role だけでは読めない）、2 は除外 Path の追加の Filter をせず、**返す時の Redact をしない**（2 の C。結果の経路・DAG・Board の `title` は `redact_text` に通さず、`redacted` の数と画面の注記もない。表示の安全のための制御文字の Escape は残す。保存した行は変えない）、3 は A（別の経路 `GET /tasks/{id}/result` と詳細の `result`）、4 は A（読み取りだけの `completed` の Task だけ）、5 は A（詳細の DAG の上の「結果」の Panel）、6 は A（拒否だけを監査）、7 は A（`hidden` で読めない Repository があることだけを明示して開示）、8（2 の中の Human への問い）は A（Plan の受け入れで `redact_text` が変える `key` / `title` を拒み、Planner の Prompt で代入・Flag の形を書かせない。受け入れの時だけの規則で、表示は変えない））
- Date: 2026-10-22（承認 2026-10-23）
- Scope: Issue [#290](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/290)（[#279](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/279) の後続）。この Decision は設計だけで、コードは承認の後の PR で書く。関係する既存の口: `apps/backend/paw_backend/orchestrator/orchestrator.py`（`_read_only_result`）、`tasks/service.py`（`TaskService.execute(..., result=...)`、`MAX_RESULT_BYTES`）、`orchestrator/plan.py` と `orchestrator/limits.py`（`KEY_PATTERN`、Plan の受け入れ）、`api/v1/tasks.py`（`GET /tasks/{task_id}`）、`api/v1/task_views.py`（`may_read_repository`、`readable_repositories`）、`tools/credentials.py`（`redact_text`）、`integration/changes.py`（制御文字の Escape）、Web の `apps/web/src/tasks/TasksPage.tsx`（`TaskDetailView`）と `apiSource.ts`
- Supersedes（一部。3 は A で承認）: [Decision 0067](0067-task-pr-http-api.md)（Approved）の 2 の「Task の入力、Log、Node の Goal / 結果、Event の詳細は返さない」のうち、**読み取りだけの Task が `completed` で終わったときの `complete` の Event の `detail.result`（成功した各 Node の `key`・`role`・`title`・`summary`）だけ** を、新しい経路で返す。0067 の他（Task の入力・Log・Node の Goal・普通の Task の Node の結果・他の Event の詳細は返さない、Node の詳細にツール呼び出しを出さない、見える範囲、操作の権限）は変えない。[Decision 0094](0094-task-create-entry-open-points.md)（Approved）の 1 の (c)「結果（各 Node の要約）を Task に残す」は変えない（残したものを読む口を足すだけ）

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

## 決定（承認された全体）

- `GET /api/v1/tasks/{task_id}/result` を足す。Guard は `GET /tasks/{task_id}` と同じ（Task の Project に `project.read`）。加えて、**読む人が、Task の Working Set に一度でも入ったすべての Repository（`task_repositories` の行。`removed_at` で外れた行を含む）を今の ACL で読めるときだけ** 返す（1）。返すのは `complete` の Event の `detail.result` の Node の `key`・`role`・`title`・`summary` を、**`redact_text` に通さず**、表示の安全のため Tab と改行以外の制御文字だけを見える Escape にしたもの（2。保存した行は変えない）。結果がないときは 404 `task_result_not_found`、Repository の条件で読めないときは 403 `task_result_forbidden`（7）。
- Plan の受け入れ（`orchestrator/plan.py`）で、`redact_text` に通すと変わる（高い確度の形に加え `DB_PASSWORD=...` の代入の形と `--password ...` の Flag の形も含む）Node の `key` と `title` を拒み（`would_redact`）、Planner の Prompt で `key` / `title` に代入や Flag の形を書かせない（2 の問い 8）。受け入れの時だけの規則で、`GET /tasks/{id}` の DAG と Board の Reviewer の `title` は今のまま Redact なしで返す。
- `GET /tasks/{task_id}` の答えに `result`（`"none"` / `"available"` / `"hidden"`）だけを足す。`hidden` は「読み取りだけの `completed` の Task だが、読めない Repository が Working Set にあるので見せない」で、その事実（名前・数・中身ではない）を見せることを明示して受け入れる（7）。詳細は Task を開くたび・Control の後・動いている間は 5 秒ごとに読まれる軽い答えなので、最大約 1 MiB の本文は載せない。
- 出すのは読み取りだけの Task が `completed` のときだけ。
- Web の Task の詳細で、`result` が `available` のとき、DAG の Panel の上に「結果」の Panel を出し（`hidden` のときは同じ場所に「読めないリポジトリを含むため、結果を表示できません」とだけ出す）、Node ごとに Title・役割・要約を Plan の順に、ただの文（Markdown にしない、Link にしない、改行は保つ）で出す。読み込みは Panel を開いた時に 1 回。Redact の注記はない。
- 読んだことは、他の読み取りと同じく拒否だけを監査する。

## 判断点（承認の時の説明。決定は上の「決定」と Approval の行）

### 1. 誰が結果を読めるか

結果は Agent が Working Set の Repository を読んで書いた自由な文なので、どの文がどの Repository から来たかは分けられない。一方 0067 の 2 は、ACL で読めない Repository を名前も含めて見せない。

- **A（推奨）: Task の Project に `project.read` があり、かつ Task の Working Set に一度でも入ったすべての Repository を今の ACL で `project.read` で読める人。** 「一度でも入った」は `task_repositories` のその Task のすべての行（`removed_at` のある行を含む）。Task の動いている間に `change_working_set` で `referenced` の Repository が外されても、外される前に Node がそれを読んで要約に書いているかもしれず、今の Working Set（`TaskSnapshot.working_set`、`removed_at` のない行だけ）で判定すると、外された Repository を読めない人に要約が見えてしまう（Codex review）。`task_repositories` の行は消えず（外すのは `removed_at`、入れ直しは同じ行）、Task ごとの集合は増えるだけなので、完了の後に変わらない判定の材料になる。前の Attempt（Retry / Restart の前）で入っていた Repository も含む（Retry は同じ Context で続けるので、前の Attempt の読んだものが要約に残りうる。広い側に倒す）。Task の作成者も同じ条件（作成の後に ACL が狭められたら、作成者でも読めない）。Owner / Admin の System Role だけでは読めない（0004）。Viewer は読める（REQUIREMENTS の Viewer は「Task の閲覧」を持つ）。
- 判定の材料の代案（A の「一度でも入った」の取り方）:
  - A-2: 成功した Attempt の `task_attempt_repositories` の行（外されても残る）。A より狭いが、前の Attempt の分を落とし、Attempt の途中で入った Repository に必ず行がある、という不変条件に頼る。
  - A-3: 完了の時に、その時点までの Repository の ID の集合を `complete` の Event の `detail` に写す（不変の Snapshot）。良い点: 判定が Event だけで済む。悪い点: 0094 の (c) の記録の形を変え、#284 の後に保存された行には無いので A に戻る道が要る。
  - 推奨は A（Migration なし、既存の行にそのまま効き、いちばん広い側）。
- B: Task の作成者だけ（と、A の条件）。良い点: 狭い。悪い点: 同じ Project の Member に調べた結果を共有できず、読み取りだけの Task の使い道（調べものの共有）に合わない。
- C: Project に `project.read` があれば、Repository の ACL を問わず読める。悪い点: ACL で拒否された Repository の中身が、要約を通して読めてしまう（0067 の 2 と REQUIREMENTS の ACL override の意味を崩す）。推奨しない。
- D: Owner / Admin は所属しなくても読める。悪い点: 0004 の「System Role は Project を開かない」を崩す。推奨しない。

### 2. Repository の ACL・除外 Path・Redact

- Repository の ACL: 1 の A のとおり、一度でも入ったすべてを読めるときだけ出す（一部を隠して残りを出すことはしない。文を Repository ごとに分けられないため）。
- 除外 Path: **追加の Filter はしない（推奨）。** `excluded_paths` と `protected` は System の除外で、Sandbox の Mask と Broker が Agent に読ませないので、結果にも入らない。利用者が決める Path の単位の ACL は要件にない。もし後で Path の単位の ACL を入れるなら、そのとき結果の扱いをこの Decision の Supersede で決め直す（自由な文から Path の中身を取り除くことはできないので、「その Path を隠される人には結果を出さない」になるはず）。
- Redact（**Human の選択は C の「返す時の Redact をしない」**。理由「そのまま出して下さい。見にくくなるので」。下の A と、A の一部の「DAG で返す時にも Redact する」は採らない。制御文字の Escape は読みやすさを損なわず表示の安全のためなので残す。採らなかった案の記録として A・B を残す）:
  - **A（推奨だったが採らない）: 返す時に、各 Node の文字列の Field をすべて（`key`・`role`・`title`・`summary`）`tools.credentials.redact_text` に通し、Tab と改行以外の制御文字を見える Escape にする（0078 の 2 の差分と同じ規則、`integration/changes.py` の関数を共用）。** 保存した Event の行は変えない（記録・Evidence として残す。DB を読めるのは Operator だけ）。既に保存された結果にもそのまま効く。答えに Redact した数（`redacted`）を付け、画面は 1 以上のとき「認証情報らしい文字列を隠しました」と出す。`key` は `KEY_PATTERN`（`[a-z][a-z0-9_-]{0,31}`）なので `sk-` や `glpat-` と 20 文字のような Credential の形を含みうる（Codex review）。`role` は列挙の値だが、Field を選ばずにすべて通す（後で Field が増えても漏れない）。
  - **加えて（推奨。Human は受け入れで拒む段だけを採った。DAG で返す時の Redact は 2 の C により採らない）: Node の `key` と `title` の Credential は、Plan の受け入れで拒み、DAG で返す時にも Redact する（2 段）。** `key` と `title` は 0067 の DAG の Panel（`GET /tasks/{id}` の `dag`、`api/v1/tasks.py`）と Board の Reviewer の一覧（`api/v1/boards.py`）でも Redact なしに返るため（Codex review）。
    - 判定の基準（重要）: `tools/credentials.py` の `contains_credential_plaintext` は `_HIGH_CONFIDENCE`（`ghp_...`・`sk-...` など形の分かる Token）だけを見る。一方 `redact_text` はそれに加えて `_ASSIGNMENT`（`DB_PASSWORD=hunter2`・`"api_key": "..."` のような名前に Keyword を含む代入）と `_FLAG`（`--password hunter2` のような Flag の次の語）も置き換える。`contains_credential_plaintext` を基準にすると `DB_PASSWORD=hunter2` や `--password hunter2` の `title` が受け入れられ、DAG に Redact なしで出る。そこで **基準は「`redact_text(value)` の置き換えの数が 1 以上」**（結果の Redact が変える文字列はすべて拒む）とし、`credentials.py` に `would_redact(text) -> bool` のような述語を足して、結果の Redact と同じ規則を 1 か所で持つ（片方だけ形が増える食い違いを防ぐ）。`contains_credential_plaintext` は Tool の引数の拒否（0006）の基準のまま変えない（Source code の Test の Password を拒まないため、そちらは高い確度だけ）。
    - 受け入れ（`orchestrator/plan.py`）: `key` または `title` が上の基準に当たる Node を含む Plan を拒む（Planner の失敗として次の Planner へ。`worker_in_read_only_task` と同じ扱い。理由の Code は例えば `credential_in_plan`）。読み取りだけでない Task の Plan にも効く。`key` は `KEY_PATTERN`（`[a-z][a-z0-9_-]{0,31}`）なので `=`・`:`・空白を含まず `_ASSIGNMENT` / `_FLAG` には当たらないが、`sk-` + 20 文字のような高い確度の形には当たりうる。基準は Field を選ばず同じにする。
    - 返す時（`GET /tasks/{id}` の `dag` の各 Node の `title`、Board の Reviewer の `title`）: `redact_text` に通す（保存した行は変えない）。`title` は Node の識別子ではない（識別子は `key`）ので、書き換えても DAG と結果の対応は崩れない。結果の経路の `title` も同じ関数を通すので、両方で同じ文字列になる。この規則より前に保存された Plan と、受け入れの規則が見逃す形のための二重の守り。`key` は識別子なので DAG では書き換えない（受け入れで拒むことで守る。規則より前の行の `key` は結果の経路では Redact される）。
    - 代案 K-2: 受け入れでは拒まず、返す時の Redact（DAG・Board・結果の `title`）だけにする。良い点: `token: refresh 処理` のような普通の題（`_ASSIGNMENT` は Keyword の後の `:` / `=` と 6 文字以上の語で当たる）で Planner が失敗することがない。悪い点: `key` は書き換えられないので `key` の高い確度の形は結局受け入れで拒む必要があり、規則が 2 つになる。Title を出す経路が増えるたびに Redact を足し忘れる危険が残る。
    - 代案 K-3: 受け入れで拒むだけ（返す時の Redact をしない）。悪い点: 規則より前に保存された Plan の `title` が DAG に出る。推奨しない。
    - **Human への問い（8。回答は A = 推奨どおり、受け入れで拒む）: 受け入れの基準を `redact_text` と同じ（`_ASSIGNMENT` / `_FLAG` を含む）にすると、`token: refresh 処理` や `--token-file abcdef` のような Credential でない題でも Plan が拒まれ、次の Planner に進む（全 Planner が同じ題を書けば Task が失敗する）。推奨は、この誤検知を受け入れて拒む（推奨の 2 段）。Planner の Prompt に「Title に代入や Flag の形を書かない」を足して誤検知を減らす。誤検知を避けたいなら K-2（返す時の Redact だけ、`key` は高い確度の形だけで拒む）。**
  - B: 保存の時（`_read_only_result`）にも Redact する。良い点: DB に Credential が残りにくい。悪い点: 0094 の 1 の (c) の記録の形を変える。Tool の結果は既に Broker が Redact しているので、Model が要約に Credential を書く経路は狭い。A に足して後から入れられる（A は冪等）。今は推奨しない。
  - C: Redact しない。推奨しなかったが、**Human はこれを選んだ**（決定）。ただし受け入れの時の `key` / `title` の拒否（8）と制御文字の Escape は行う。受け入れたリスクは「リスク」に記す。後で返す時の Redact（A）や保存の時の Redact（B）を入れるなら、新しい Decision からこの 2 を Supersede する。
- 結果は Agent が書いた信頼しない文として扱い、Web は HTML・Markdown・Link にしない（3 の画面）。

### 3. API の形・大きさ・切り詰め（0067 の 2 の一部を Supersede）

- **A（推奨）: 別の経路 `GET /api/v1/tasks/{task_id}/result`。** 答えは `{"nodes": [{"key", "role", "title", "summary"}], "completed_at": <時刻>}`（2 の C により `redacted` の数は付けない）。`GET /tasks/{task_id}` には `result`（`none` / `available` / `hidden`、7）だけを足す。理由: 詳細は Task を開くたび・Control の後・動いている間は 5 秒ごと（`REFRESH_MS`。`completed` は読み直さない）に読まれる軽い答えで、最大約 1 MiB をそのたびに運ばない。`completed` は終わりの状態なので、結果は一度読めば変わらない。経路を分けると、1 の条件を詳細と別に判定できる。
  - 大きさ: 保存の上限（1 MiB）の中で全部を返す。Page 送りと切り詰めはしない（Node は最大 32、Summary は 2,000 文字で、Escape で伸びても数 MiB を超えない）。
  - 答えは `Cache-Control: no-store`（他の API と同じ）。
  - 結果がない（読み取りだけでない・`completed` でない）ときは 404 `task_result_not_found`。1 の条件で読めないときは 403 `task_result_forbidden`（7 の A。7 で B を選ぶなら 404 で区別しない）。Task そのものが見えないときは今と同じ 403 `forbidden`。
- B: `GET /tasks/{task_id}` の答えに `result` を足す。良い点: 経路が 1 つ。悪い点: Task を開くたびに最大約 1 MiB。詳細の判定（`project.read`）と結果の判定（1 の Repository の条件）が 1 つの答えに混ざる。
- C: B だが詳細では先頭だけ（例 Node ごとに 300 文字）を返し、全部は別の経路。悪い点: 2 つの形を保つ。今の大きさでは要らない。
- Supersede の範囲: 0067 の 2 の「Event の詳細・Node の結果は返さない」のうち、上の経路で読み取りだけの `completed` の Task の `complete` の Event の `detail.result` を返すことだけ。普通の Task の Node の結果、Node の Goal、Task の入力、Log、他の Event の詳細は今のまま返さない。Task の一覧（`GET /tasks`）にも足さない。`api/v1/tasks.py` の Module の説明（「The answers carry no ... node goal or result and no event detail」）を実装の PR で改める。

### 4. どの Task のどの状態で出すか

- **A（推奨）: 読み取りだけの Task が `completed` のときだけ。** 結果が保存されるのはその時だけ（0094 の 1 の (c)）。
- B: `failed` / `cancelled` の読み取りだけの Task でも、その時までに成功した Node の要約（DAG の Node の行にある）を「途中までの結果」として出す。良い点: 失敗した調べものでも途中が読める。悪い点: 0094 の (c) の外の新しい読み取りの Model（DAG の行を読む）で、Retry で Node が上書きされる時の扱いが要る。要るなら別の Issue（未決）。
- 普通の（書く）Task の Node の結果は、この Decision では出さない（PR の画面と Review の要約は 0078 の範囲）。

### 5. Web の置き場所

- **A（推奨）: Task の詳細（`TaskDetailView`）で、`result` が `available` のとき、「いまの作業」の帯と DAG の Panel の間に「結果」の Panel（`task-card-panel` の形）を出す。** 中身は Plan の順の Node ごとの区切りで、見出しに Title と役割の Badge、本文に要約を `white-space: pre-wrap` のただの文で出す（Markdown にしない、URL も Link にしない）。Panel を表示した時に結果を 1 回読み、Task が変わらない間は読み直さない。`hidden` のときは同じ場所に「読めないリポジトリを含むため、結果を表示できません」の一行だけを出す（Repository の名前・数は出さない）。`none` と、読み込みが 404 のときは何も出さない。Phone の幅でも同じ Panel を縦に並べる。（2 の C により Redact の注記はない。）
  - DAG の Node を選んだ時の詳細（0067 の 5）には要約を出さない（出す場所を 1 つにする）。
  - Design canvas（PAW-060）に「結果」の Panel はないので、既存の `task-card-panel`・見出し・Badge・文字の Token だけで組み、新しい色や部品を足さない。UI の PR は Human が画面を確かめてから Merge する（Screenshot を PR に付ける）。
- B: 別の Page（`/agents/<id>/result`）。悪い点: 画面が 1 つ増え、Design canvas の外の Navigation が要る。
- C: DAG の各 Node の詳細に要約を出す。悪い点: 0067 の 5 の「Node の詳細には出さない」に触れ、全体を通して読みにくい。

### 6. 読んだことの監査

- **A（推奨）: 他の読み取り（Task の詳細・PR の差分、0004・0078 の 3）と同じく、`project.read` の判定の拒否だけを監査する。** 1 の Repository の条件で読めないときの 403 `task_result_forbidden` も、Repository の判定の拒否として Capability の監査（`audit_events`）に残す（`may_read_repository` の判定と同じ扱い）。結果の本文は Log・Audit に出さない。
- B: 成功した読み取りも、新しい Action（例 `task.result.read`、Task の ID・Node の数・`redacted` の数だけ。本文なし）で監査する。良い点: Repository の中身の要約を誰が読んだか残る。悪い点: 差分の閲覧（同じく Repository の中身）を監査しない 0078 と揃わず、5 秒ごとではないが画面を開くたびに行が増える。差分と一緒に「中身の閲覧の監査」を決める別の Decision にするなら、そのとき両方に入れる。

### 7. 読めない Repository があることを見せるか（Metadata の開示）

1 の A で読めない Member に対し、読み取りだけの `completed` の Task の結果だけがない（または 404）と、それ自体が「この Task の Working Set に（今、または以前に）自分の読めない Repository がある」ことの印になる（Codex review）。中身・名前・数は出ないが、存在は分かる。

ただし今（0067）でも同じ事実は推し量れる: Task の Title と指示は作成者が書いた自由な文で全 Member に見え、読み取りだけの Task は `referenced` を 1 つ以上必ず持つので、詳細の Repository の表が空なら読めない Repository があると分かる。普通の Task も `target` を必ず持つので同じ。

- **A（推奨）: 開示を明示して受け入れる。** 詳細の `result` を `none` / `available` / `hidden` の 3 つにし、`hidden` の時は画面に「読めないリポジトリを含むため、結果を表示できません」と出す。結果の経路は `hidden` のとき 403 `task_result_forbidden`（監査される）。開示するのは「読めない Repository が少なくとも 1 つある」ことだけで、名前・数・どの役割かは出さない。良い点: 0067 で既に推し量れる事実を、利用者が誤解しない形（「結果がない」ではなく「見せられない」）で出す。悪い点: 存在の事実を API の値として明示する。
- B: 開示を受け入れるが区別しない（`has_result` の `bool` と、ない・読めないを同じ 404、前の版の案）。開示の量は A と同じ（読み取りだけの `completed` で `false` なら分かる）で、画面は理由を言えない。
- C: 読み取りだけの Task で、Working Set のすべての Repository を読めない Member には、Task そのものを見せない（一覧から外し、詳細・操作は 403 `forbidden`。存在しない Task と区別しない）。良い点: この Task については印がなくなる。悪い点: 0067 の 2（Task は Project の Member に見える）をさらに Supersede し、Stop・Cancel を `project.task.run` の Member が誰でも送れる 0067 の 3 が、その Member には効かなくなる。普通の Task の推し量り（空の Repository の表）は残るので、全体としての開示はなくならない。
- D: C をすべての Task に広げる。0067 の 2 の見える範囲の大きな変更で、この Issue の範囲を超える。要るなら別の Decision。

## Test の方針（実装の PR）

- Backend: 読み取りだけで `completed` の Task は結果を返す（Plan の順、4 つの Field）。詳細の `result` が `none` / `available` / `hidden` を正しく返す。普通の Task・`running` / `failed` / `cancelled` の読み取りだけの Task は 404 `task_result_not_found`。Working Set の 1 つの Repository を ACL で拒否された Member（作成者を含む）は 403 `task_result_forbidden`。**外してから完了した Repository の回帰 Test**: `referenced` の Repository 2 つで始めた読み取りだけの Task から、動いている間に 1 つを `change_working_set` で外し、その後に DAG が成功して `completed` になったとき、外した Repository を ACL で読めない Member は 403 で詳細は `hidden`（今の Working Set だけなら読めるのに、結果は出ない）。外した後に入れ直した場合と、Retry の前の Attempt にだけあった Repository の場合も同じ で、詳細は `hidden`（Repository の名前・数は答えのどこにも出ない）、拒否は監査される。所属しない Owner / Admin と存在しない Task は 403 `forbidden`。Viewer は読める。結果の経路は `redact_text` に通さない（2 の C）: Summary に `ghp_...` や `DB_PASSWORD=hunter2` を含む保存した結果は、そのまま返り（`[REDACTED]` にならない）、答えに `redacted` の Field はない。保存した Event の行は変わらない。Plan の受け入れは Credential の形の `key` / `title` を `credential_in_plan` で拒み、次の Planner へ進む（読み取りだけの Task と普通の Task の両方）。**拒む `title` の回帰 Test は少なくとも次の 3 つの形を含む: 高い確度の形（`ghp_` + 36 文字）、代入の形（`DB_PASSWORD=hunter2`）、Flag の形（`--password hunter2`）**（後の 2 つは `contains_credential_plaintext` では偽で、基準が `redact_text` と同じであることを確かめる）。`key` は `sk-` + 20 文字。拒む基準の述語（`would_redact`）が `redact_text` の置き換えの数と一致することを、`credentials.py` の既存の Test の入力で確かめる。`GET /tasks/{id}` の `dag` と Board の Reviewer の `title` は今のまま `redact_text` に通さない（既存の Test を変えない）。制御文字は Escape される。最大の結果（32 Node・最悪の文字）を返せる。Route の Guard の一覧（`tests/test_authz_routes.py`）に足す。
- Web: `available` のときだけ Panel を出し、1 回だけ読む（Poll では読み直さない）。`hidden` は一行の説明だけ。要約の中の `<script>`・Markdown・URL がただの文で出る。`none` と 404 で何も出さない。Redact の注記は出さない。日本語と英語の文言。Phone の幅。
- 既存の Test を消さない・緩めない。

## PR の分け方

1. Backend: 経路・詳細の `result`・1 の条件・すべての Field の制御文字の Escape（`redact_text` は通さない）・`would_redact` と Plan の受け入れの Credential の形の `key` / `title` の拒否・Planner の Prompt の指示・Test・`api/v1/tasks.py` の説明と `apps/backend/README.md`。
2. Web: 「結果」の Panel・`apiSource.ts`・i18n・Test。Screenshot を付け、Human が画面を確かめてから Merge。
3. `docs/DEPLOYMENT_UPDATE.md` の段 3 の確認を、DB の代わりに画面（または `GET /tasks/{id}/result`）で見る手順に改める（2 の PR に含めてよい）。

Migration は要らない（`task_events` の `(task_id, seq)` の Index で `complete` の Event を読む）。

## 未決（この Decision では決めない）

- `failed` / `cancelled` の読み取りだけの Task の途中までの結果（4 の B）。
- 返す時の Redact（2 の A）・保存の時の Redact（2 の B）を後で入れるか。入れるなら新しい Decision から 2 を Supersede する。
- Repository の中身の閲覧の監査（6 の B）を差分と一緒に決めること。
- 結果の Markdown の表示・写し（Copy）・書き出し。
- Path の単位の ACL を入れるときの結果の扱い（2）。

## リスク

- 1 の A では、Working Set に一度でも入った 1 つの Repository を読めない Member は結果の全部を読めない（途中で外した Repository も数える）（一部だけ出すことはしない）。安全側の振る舞いで、要るなら作成者が読める Repository だけで Task を作り直す。
- **受け入れたリスク（2 の C、2026-10-23 の Human の回答）**: Model が要約（`summary`）に Credential を書いた場合、それは 1 の ACL の判定を通った読む人（Project の Member で、Working Set に一度でも入ったすべての Repository を読める人）にそのまま見える。Plan の受け入れより前に保存された Plan の `title` の Credential も DAG・Board・結果にそのまま出る。緩和: Agent の Tool の結果は既に Broker が Redact している（0006）ので Model が Credential を見る経路が狭い、Plan の受け入れで `redact_text` が変える `key` / `title` を拒む（8）、1 の ACL で Repository を読める人だけが読む。返す時の Redact（2 の A）や保存の時の Redact（2 の B）は、新しい Decision からこの 2 を Supersede して後から足せる（どちらも保存した行に後からかけられる）。
- 形の分からない秘密（社内の名前など）は、Redact の有無によらず、Repository を読める人だけが読むこと（1 の A）で守る。

## 承認後の扱い

2026-10-23 に承認され、Status を Approved に改め、承認の内容を Approval の行と「決定」に記録した。0067 の本文は書き換えず、この Decision が一部を Supersede することを README の表に記す。
方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。
