# Task Queue・Budget・Loop検知の方針

- Status: Proposed
- Date: 2026-09-24
- Scope: PAW-033 と、Budget・Loop検知・Escalationを使う以降の Issue（PAW-034 Orchestrator、PAW-036 GPU Scheduler など）
- Supersedes: なし
- Approval: 未承認（Humanの承認待ち）

## 背景

[REQUIREMENTS.md](../../REQUIREMENTS.md) は、Task の Budget の Preset の名前（Standard / Long / Unlimited）、
6 種類の上限（max runtime、steps、retries、tool calls、tokens、GPU time）、Loop 検知の必要性、Priority（HIGH / NORMAL / LOW）を定めている。
一方で、**具体的な数値、Loop の閾値、Budget を使い切ったときの遷移は定めていない**
（要件は「実装時の選択」としている）。

PAW-033 の実装は、動かすためにこれらを仮の値で置いた。Review（Codex）は、
承認前の Product Policy を既定として実装してよいのかを指摘した。
[AGENTS.md](../../AGENTS.md) の「仕様変更」は、重要判断を `docs/decisions/` に提案して人間 / Admin の承認を得ると定める。
そこで、仮に置いた値と選択を一覧にし、承認または変更を求める。

**この Decision は Proposed であり、Human の承認を得ていない。** 承認されるまで、次の値と選択は暫定である。
値は `domain.PRESET_LIMITS` と `LoopPolicy` のデータで、変更しても Schema は変わらない（Migration は不要）。
承認された値が変わる場合は、新しい Decision から `Supersedes` する。

実装は [Backend README](../../apps/backend/README.md) の「Task Queue / Budget / Loop 検知」に書いている。

## 提案

### 1. Preset の数値

| 種類 | Standard | Long | Unlimited |
| --- | --- | --- | --- |
| `runtime_seconds` | 3,600（1 時間） | 14,400（4 時間） | 上限なし |
| `steps` | 50 | 200 | 上限なし |
| `retries` | 10 | 20 | 上限なし |
| `tool_calls` | 300 | 1,200 | 上限なし |
| `tokens` | 1,000,000 | 4,000,000 | 上限なし |
| `gpu_seconds` | 3,600 | 14,400 | 上限なし |

- 上限ちょうどまで使うのは超過ではない（`消費量 + 予定 > 上限` が超過）。
- Preset を設定していない Task は、無制限とみなさず、エラーにする。
- 警告の閾値は要件にないため置かない（`WARN` はない）。

### 2. Unlimited

- 6 つの数値の上限を無くすだけとする。Loop 検知、Stop Now、Critical safety / resource protection による停止は Preset と無関係で、Unlimited の Task にも効く。
- 要件が Unlimited に別の数値の上限（たとえば絶対の Runtime）を定めていないため、設けていない。**設けるかは人間が決める。**

### 3. Loop 検知の閾値

- 直近 10 件（`window_size`）のうち、最後の失敗と Signature も試行番号（`approach`）も同じ件数が 3 回（`repeat_threshold`）以上で Loop とする（連続でなくてよい）。
- Loop のとき、`approach` が 1（`max_alternatives`）未満なら代替を試し（`TRY_ALTERNATIVE`）、そうでなければ上位の Agent へ渡す（`ESCALATE`）。
- Signature は、Error class・Step・正規化した Message（先頭 2,000 文字、NFKC、小文字化、数字と ID の置換）の Hash とする。Message の原文は保存しない。
- Loop の検知だけでは Task を `failed` にしない。

### 4. Budget を使い切ったときの遷移

`decide_next_action` は、次の順で最初に当てはまる規則を使う。

1. Budget が `EXCEEDED`: `retries` が超過した種類に含まれれば `FAIL`、それ以外は `WAIT_FOR_USER`（安全な区切りで停止し、人間が上限を上げるか終了する）。**Loop の判定より優先する**（使い切った予算をさらに使う Escalation はしない）。
2. Loop が `ESCALATE`: 上位の Agent を使えれば `ESCALATE_AGENT`、なければ `WAIT_FOR_USER`。
3. Loop が `TRY_ALTERNATIVE`: `TRY_ALTERNATIVE`。
4. それ以外: `CONTINUE`。

Command（PAW-032 の `wait` / `fail`）を発行するのは Orchestrator（PAW-034）で、PAW-033 は発行しない。

### 5. Queue の飢餓

- `HIGH` > `NORMAL` > `LOW`、同じ優先度では先着順とする。要件に Aging / 飢餓防止の規則がないため**置いていない**。`HIGH` / `NORMAL` が続く間、`LOW` は待ち続ける。
- 優先度は開始の順序にだけ影響し、実行中の Entry は中断しない（Preemption は Queue の責務ではない）。

### 6. Lease の時計

実装（`TaskQueue`）が実際に行うことを、そのまま提案する。

- **どの時計か。** Queue が「Lease が切れたか」の判定と、保存する時刻（`enqueued_at`、`claimed_at`、`lease_expires_at`、`finished_at`）に使う時計は、**Database の時計だけ**とする。Worker が各自の時計を渡す方式は、時計が進んでいる Worker や誤った未来の時刻が有効な Lease を奪い、同じ Task を 2 つの Worker で始めさせ得るため採らない。
- **`clock_timestamp()` を使い、`now()` は採らない。** 使う関数は PostgreSQL の `clock_timestamp()`（評価した瞬間の壁時計）である。`now()` は Transaction の開始時刻で固定され、行の Lock を待った後の判定に、待つ前の古い時刻を使う。Lock を待つ間に期限が過ぎても、期限内と判定してしまい、失った Lease を有効とみなす（`now()` の意味は PostgreSQL の仕様。この判定を Test が確認する）。
- **1 つの文の中では時計を 1 回だけ読む。** 時計を使う文は、先頭に `WITH clock AS (SELECT clock_timestamp() AS ts)` を付け、文の中の「現在の時刻」と「現在 + `lease_seconds`」は、全てこの CTE の 1 つの値を参照する。揮発性の関数を含む CTE は PostgreSQL が 1 回だけ評価（Materialize）するため、`claimed_at` と `lease_expires_at` はちょうど `lease_seconds` 離れ、Heartbeat の判定と新しい期限も同じ値になる（`clock_timestamp()` を文の中に 2 回書くと 2 回読まれ、数マイクロ秒ずれる）。
- **文をまたぐときは、文ごとに読み直す。** 1 つの Transaction の中の 2 つの文は、別々に時計を読む。`claim_next` は、`FOR UPDATE SKIP LOCKED` で先頭の行を選ぶ文（期限切れの判定）と、その行を更新する文（`claimed_at` と期限）の 2 文で、後者の値は前者の値以後である。`SKIP LOCKED` は待たず、選んだ行は Claimer 自身が Lock しているので、2 つの値の間に Lease の状態は変わらない。
- **Lease を判定する更新は、行の Lock を先に取る。** `UPDATE` は `WHERE` を行の Lock を待つ**前**に判定し、Lock を持っていた Transaction が Rollback したときは判定し直さない（実 PostgreSQL で確認した）。1 つの `UPDATE` では、待つ間に切れた Lease を有効と判定し得る。そこで `heartbeat` / `release` / `complete` は、まず `SELECT ... FOR UPDATE` で Entry の行を Lock し（待つのはこの文）、次の文で Lease の期限（`lease_expires_at > 読んだ時刻`）、Worker の id、Claim の世代（7 節）を判定して更新する。判定に使う時刻は、Lock を得た**後**に読む。
- **Lease を判定しない文の時刻は、待つ前の値になり得る。** `enqueue` の `enqueued_at` と `cancel` の `finished_at` は、それぞれ 1 つの文で書く。同じ Task の別の `enqueue`（未 Commit）や `cancel` の対象行の Lock を待つと、保存する時刻は、待つ前に読んだ値（待った時間だけ古い値）になる。この時刻は先着順と記録のためのもので、Lease の有効・失効を決めない。これは許容する。
- 明示の時刻は Test のための継ぎ目（`TaskQueue(..., allow_explicit_now=True)`）に限る。本番の Queue は呼び出し側の時刻を拒否する。
- Lease の長さ（既定 60 秒、最大 86,400 秒）は、Preset の数値と同じく仮の値である。
- この節の対象は Queue である。`BudgetTracker` の Runtime の時計も同じく Database の時計にした（10 節。以前は Process の時計だった）。`budget_usages.created_at` / `loop_failure_signatures.created_at`（行を作った時刻の `now()`。判定に使わない）は対象外である。基準は 1 台の PostgreSQL Server の時計で、Failover で別の Server の時計へ切り替わるときのずれは扱わない。

### 7. Lease の世代（Fencing token）

- Lease は Entry の `id` と Worker の id だけでは識別できない。Lease が切れた Worker の Entry が**同じ Worker id**に再び Claim される（設定で id を固定した Process の再起動など）と、まだ動いている古い実行が、`claimed_by` も新しい Lease の期限も満たし、新しい Claim の Heartbeat・返却・完了を行えてしまう。
- そこで `claim_count`（Claim のたびに 1 増え、減らず、戻らない。Reclaim と返却後の再 Claim も数える。Schema は変わらない）を Lease の世代とする。`claim_next` が返す `QueueEntry.claim_count` を、`heartbeat` / `release` / `complete` が**必須の引数**として受け取り、Entry の現在の `claim_count` と違う世代は Worker id が同じでも `LeaseLostError` にする。
- 世代の引数を任意にしない（省略した呼び出しだけが保護されなくなる）。呼び出し側の互換は、Orchestrator（PAW-034）がまだ無いため、Queue の Test だけである。

### 8. Loop の失敗記録の試行（Attempt）による Fencing

- 失敗の記録（`LoopDetector.record_failure`）は、Task の ID だけでは、どの試行の報告かを区別できない。Restart が新しい試行を始め、履歴の `clear` が終わった後に、古い試行の Worker が遅れて報告すると、その失敗が新しい試行の Window に入り、数件で `TRY_ALTERNATIVE` / `ESCALATE` を誤って引き起こす。
- そこで `record_failure` に**必須の** `attempt`（報告する Worker が開始された試行の番号。PAW-032 の Step・Log・Tool の書き込みが持つ `attempt` と同じ）を加え、`tasks.attempt`（Restart が増やす既存の Counter）と違えば `StaleAttemptError` で拒否して何も書かない。試行の番号の Counter は PAW-032 の既存のものを使い、新しい状態は作らない。
- 確認と書き込みの間に Restart が割り込まないよう、`record_failure` は Task の行を `FOR SHARE` で Lock して Transaction の終わりまで持つ（PAW-032 の Command は `FOR NO KEY UPDATE` を取るため、直列になる）。Restart が待つ時間は 1 回の記録の Transaction の間だけである。
- 失敗の行は、報告された試行の番号（`loop_failure_signatures.attempt`、1 以上の `INTEGER`、必須）を持つ（0033 は未 Merge のため、0033 の Migration に列を足した）。判定（`history`、`assess`、`record_failure` が返す判定）は、Task の**現在の試行**（`tasks.attempt`）の行だけを対象にする。Restart が Commit された瞬間から、新しい試行は空の履歴で始まり、古い試行の行と一緒に数えられない。
- 履歴の削除は `clear(task_id)`（Task の全行）をやめ、`clear_previous_attempts(task_id)`（**現在の試行より前**の試行の行だけ）にする。Restart の Command が Commit された後、この掃除が走る前に、新しい試行が失敗を記録できる（分散した Scheduler は、Restart の Commit の直後に新しい試行を始め得る）。その失敗は有効なので、掃除は削除しない。掃除は正しさに必要ではなく（古い行は読まれず、`window_size` の上限で新しい行に押し出される）、Table を小さく保つためのもの。Orchestrator（PAW-034）は Restart の後に呼ぶが、呼ぶ順序や間隔は正しさに影響しない。

### 9. 失敗の文字列の検証（Surrogate 文字）

- `record_failure` の `error_class`・`step`・`message` に Surrogate 文字（U+D800〜U+DFFF）が含まれると、Hash の UTF-8 への符号化が `UnicodeEncodeError` を漏らし、型付きの `InvalidQueueingArgumentError` の契約が破れる。3 つとも Database に触れる前に `InvalidQueueingArgumentError` で拒否する（値は返さない）。`message` は切り詰めの前の全体を検査する。
- `message` の NUL は拒否しない。`message` は Hash にするだけで保存せず、NUL で失敗しない。拒否すると、NUL を含む出力の失敗を記録できず、その繰り返しの Loop を見逃す。`error_class` と `step` は従来どおり制御文字（NUL を含む）を拒否する。
- 拒否か整形（Surrogate を置換して記録する）かは、仕様の選択である。ここでは、他の入力（PAW-032 の文字列）と同じく拒否とし、整形は呼び出し側（Worker）に任せる。整形して記録する方が失敗を取りこぼさない、という反対の考えもあるため、承認時に確認したい。

### 10. Runtime の Timer の世代（Fencing token）

- Runtime の Timer（`budget_usages` の `runtime_seconds` の行の `running_since`）は、Task の ID と種類だけで識別されていた。Lease が切れて Entry が Reclaim された（または Task が Restart された）後、まだ動いている古い実行が遅れて `stop_runtime` を呼ぶと、新しい Worker の Timer を終わらせて累積へ加えてしまい、その後の `check` は新しい Worker の Runtime を数えなくなる（Runtime の上限を回避できる）。
- そこで **Runtime の Session の世代**（`budget_usages.runtime_generation`、`runtime_seconds` の行だけが使う `BIGINT`、既定 0）を Timer の Fencing token とする。`start_runtime` は呼ぶたびに世代を 1 増やし、その値を返す。`stop_runtime` は世代を**必須の引数**として受け取り、現在の世代と違えば、何も書かずに `StaleRuntimeSessionError` にする（新しい Session の `running_since` と累積の Runtime は変わらない）。世代は減らず、Timer が止まっても戻らないため、停止済みの古い Session の重複した停止も、新しい Session を止められない。
- 世代が現在のもので、すでに停止済みの `stop_runtime` は、従来どおり何も変えず、現在の Runtime を返す（同じ Session の二重の停止は冪等）。Preset の変更は世代を変えない。0033 は未 Merge のため、0033 の Migration に列と CHECK（`runtime_generation >= 0`、`runtime_seconds` 以外の行は 0）を足し、`update_columns` に `runtime_generation` を加えた。
- すでに Timer が動いているときの `start_runtime` は、Session を**引き継ぐ**。`running_since` は変えず（それまでの時間は失われず、二重にも数えられず、死んだ Worker の Lease が切れるまでの時間も Runtime に入る）、世代だけを増やして前の Session を古くする。Lease に有効期限のない Timer が、止められないまま残って新しい Worker を締め出さないための選択である（新しい Worker は Queue の `claim_next` を通っている）。
- **Timer の開始・停止の時刻を、行の中で順序付ける。** 時計は文を実行する前（または Lock を待つ前）に読まれるため、読んだ後に呼び出しが遅れる（Process の停止、接続の待ち、Lock の待ち）ことがある。古い Session の `stop_runtime` が、より後の時刻（たとえば t=150）まで精算して `running_since` を `NULL` にした**後**に、より前の時刻（t=100）を読んで遅れていた置き換えの `start_runtime` が実行されると、`running_since` が `NULL` のため、新しい Timer は t=100 から始まり、100〜150 が新しい Session の停止で二重に数えられる（Runtime の上限を早く使い切る）。
- そこで `runtime_seconds` の行に **`settled_through`**（`TIMESTAMPTZ`、`NULL` 可、`runtime_seconds` 以外の行は `NULL`）を足し、`stop_runtime` が精算した Cutoff を `greatest(now, running_since)` として記録する（同じ文で `running_since` を `NULL` にする。`now` はこの文が読む時刻）。`start_runtime` は、動いている Timer がなければ `running_since` を `greatest(now, settled_through)` にする（`greatest` は `NULL` を無視するので、停止したことがなければ `now`）。どちらも行を Lock する 1 つの文の中で計算するので、Lock を待った後の最新の行に対して評価される。Cutoff は `running_since` 以上で前にしか進まず、次の Timer は Cutoff 以上から始まるため、数える区間は重ならない（Clock が後ろへ戻る場合も同じ。Process ごとの時計の食い違いは、下の Database の時計への一本化で無くした）。CHECK（`running_since IS NULL OR settled_through IS NULL OR running_since >= settled_through`、`settled_through` は `runtime_seconds` の行だけ）でも守り、`update_columns` に `settled_through` を加えた。0033 は未 Merge のため、0033 の Migration に足した。
- **Runtime の時計は Database の時計だけにする（6 節と同じ方式）。** 旧案は、`start_runtime` / `stop_runtime` / `usage` / `check` が注入した Process の時計（Python）を読み、それを文へ束縛していた。すると、`settled_through` は前へしか進まない Cutoff のため、時計の進んだ Host が書いた Cutoff（たとえば t=150）が、時計の遅れた Host（t=100）の置き換えの Session の `running_since` を、その Host にとっての未来へ押し出す。`stop_runtime` は `max(floor(110 - 150), 0)` で Session 全体を 0 秒と数え、`usage` / `check` もその Host の時計が 150 に達するまで実行中の Runtime を数えない。Runtime の上限を回避できる（独立した Review の指摘）。時刻が Process ごとに比べられない以上、順序付けだけでは直らない。永続する Runtime の端点には、共有する 1 つの時刻の基準が要る。
- そこで `start_runtime`・`stop_runtime`・実行中の Runtime の読み取り（`usage` / `check` / `set_preset` の返す値）は、行を Lock する 1 つの文の中で、Database の `clock_timestamp()` を（`WITH clock AS (SELECT clock_timestamp() AS ts)` の CTE で、文ごとに 1 回）読む。Python の時計は読まない。`BudgetTracker(database)` が本番の形で、時計を差し替える引数は Test のための継ぎ目（`clock=...` は `allow_explicit_clock=True` の Tracker だけが受け取り、その返す Timezone 付きの `datetime` を Database の時計の代わりに文へ束縛する。`TaskQueue(allow_explicit_now=True)` と同じ考え方）に限る。既定の Tracker は、呼び出し側の時刻を拒否する。継ぎ目の Tracker と Database の時計の Tracker を 1 つの Database に混在させない（時刻の出所が 2 つになるため）。
- `settled_through` は残す。Database の時計にすれば Host 間の食い違いには不要になるが、Database の時計も、文が Lock を待つ**前**に読まれる（6 節）ため、競合する `stop_runtime` の Cutoff より前の読み取りで開始する `start_runtime` があり得る。また壁時計は後ろへ戻り得る（NTP の Step、別の Server への Failover）。Cutoff は、この 2 つで数える区間が重なるのを防ぐ。Column・CHECK・Grant はすでにあり、削除は Migration と Test の変更を要するだけで、得るものがない。限界: 文が Lock の待ちの前に時計を読むので、待った時間（通常はミリ秒）は Runtime に数えない（整数秒に切り捨てるため影響は小さい）。
- 採らなかった案: Process の時計のまま、Cutoff の比較を外す（または Cutoff を `least` で頭打ちにする）案。重複して数える競合が再び開く。Lock を先に取り、次の文で Python の時計を読み直す案は、Lock の待ちの後の Process の時計が、別の Host の停止の時計より後であることを保証しない。Database の時計を `now()` で読む案は、Transaction の開始時刻で固定される（6 節）。
- **限界（承認時に確認したい）。** `BudgetTracker` は Queue を読まないため、`start_runtime` を呼んだ Worker が Lease を持つかは確かめない。Lease を失った古い Worker が `start_runtime` を呼ぶと Session を引き継げてしまう。その場合も、時間は数え続けるので Budget は回避されず、新しい Worker の `stop_runtime` が `StaleRuntimeSessionError` になるので気付ける。Lease を持つ Worker だけが呼ぶ規則は Orchestrator（PAW-034）に置く。

## 選定理由

- 数値は、1 台の GPU Server で個人〜小規模チームが使うことを想定した、桁を合わせるための仮の値であり、実測に基づかない。
  Benchmark（PAW-016 / 017）と実運用の記録で見直す前提で、データとして 1 か所に置いた。
- Budget 超過を Loop より優先するのは、Escalation が予算を追加で消費するため。
- Lease の世代に `claim_count` を使うのは、既存の列で足り（Migration も Grant も変えない）、Claim のたびに必ず増え、Worker の id・時刻・乱数のような呼び出し側の値に頼らずに、古い Claim を判別できるため。
- 試行の Fencing に PAW-032 の `tasks.attempt` を使うのは、Restart が既に増やす唯一の Counter で、Step・Log・Tool の書き込みも同じ規則（古い試行は `StaleAttemptError`）で拒否しているため。失敗の行へ試行の Column を足して現在の試行だけを読む案は、当初は「`clear` を Restart の後に呼ぶ規則で足りる」として採らなかったが、独立したレビューで、Restart の Commit の後・`clear` の前に新しい試行が記録した失敗が、Task 全体を消す `clear` で失われる（または古い履歴と一緒に判定される）と指摘され、その規則では足りないと分かったため採る。Grant は変わらない（`tasks` の SELECT は付与済みで、行の UPDATE は不要）。Restart と掃除を 1 つの Transaction にする案は、Restart（PAW-032 の Command）と Loop（PAW-033）の境界を壊すため採らない。新しい試行の Dispatch を掃除の完了まで止める案は、Orchestrator（PAW-034）に順序の規則を課すだけで、それを守らない呼び出しを防げないため採らない。
- Timer の世代に、Queue の `claim_count` ではなく専用の Counter を使うのは、`claim_count` が Entry ごとに 1 から数え直すため。Restart や再 Enqueue の新しい Entry は、古い Entry と同じ `claim_count` を持ち得て、古い実行の停止を通してしまう。`(Entry の id, claim_count)` を呼び出し側から受け取る案は、Budget の API が Queue の Entry を知る必要があり、渡し間違いを Tracker が確かめられないため採らない。`attempt` は Reclaim（同じ試行の別の Claim）を区別できず、同じ試行の再 Enqueue も区別できないため足りない。
- Lease の時計を Database に一本化するのは、複数の Process（Worker）が同じ Entry を巡って競うため、判定の基準が呼び出し側ごとに違うと Lease の排他が成り立たないため。関数に `clock_timestamp()` を選ぶのは、Lease の期限を判定する時刻が「判定した瞬間」であるべきで、Transaction の開始時刻（`now()`）では Lock を待った分だけ古くなり、失効した Lease を有効とみなすため。文の中で 1 回だけ読むのは、`claimed_at` と期限を正確に `lease_seconds` 離すためである。

## 代替案

- 数値を Preset に固定せず、Admin が設定する: Preset の名前を要件が定める以上、まず既定の値が要る。設定 UI と認可は別の Issue。
- 超過時にすべて `FAIL` にする: 人間が上限を上げて続けられなくなる。`retries` だけを `FAIL` にした。
- Aging を入れる: 要件に規則がなく、`LOW` の待ち時間の上限を決める必要がある。
- Worker id に Process 固有の値（PID、起動時刻）を含めさせる: 呼び出し側の規則に頼ることになり、Queue が古い Claim を拒否する保証にならない。別の Token 列を追加する案は、Claim のたびに増える `claim_count` で足りるため採らない。
- `now()`（Transaction の開始時刻）を Queue の時計にする: Lock を待った後の Lease の判定が、待つ前の古い時刻になり、失効した Lease を有効とみなす。`now()` は採らない。
- Lease を 1 つの `UPDATE ... WHERE lease_expires_at > <時刻>` だけで判定する（先に行を Lock しない）: `UPDATE` は Lock を待つ前に `WHERE` を判定し、Lock を持っていた側が Rollback すると判定し直さないため、待つ間に切れた Lease を有効と判定し得る。
- `start_runtime` が Queue の Entry（`entry_id`、`claim_count`）を受け取り、同じ Transaction の中で「Lease が有効な Claim か」を確かめてから Session を始める: 古い Worker の `start_runtime` も拒否できる。一方で、Budget が Queue に依存し（Tracker の Test も全て Queue の Entry を要する）、Lease の期限の判定に Database の時計と行の Lock を足す必要がある。まず Tracker 単体で世代の Fencing を入れ、Lease の確認が要るかは承認時に決める。
- Timer が動いている間の `start_runtime` を拒否する（引き継がない）: 死んだ Worker が止めずに残した Timer が、置き換えの Worker を永久に締め出す。
- 呼び出し側が時刻を渡す（または Process の時計を使う）: 時計のずれや誤った時刻で Lease を奪える。Constructor で時計を注入する案は、Test の呼び出しの書き換えが大きいため、既定で拒否する引数の継ぎ目を選んだ。

## 承認後の扱い

承認された値と選択を、この Decision の `Approval` に記録して Status を Approved に改める。
値が変わる場合は `domain.PRESET_LIMITS` / `LoopPolicy` のデータと Test の期待値を、承認された値に合わせる。
承認されるまで、この値を前提にした運用（Budget の上限に頼る自動停止の設計など）をしない。
