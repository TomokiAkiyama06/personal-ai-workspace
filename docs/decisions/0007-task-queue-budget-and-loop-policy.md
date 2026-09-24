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

- Queue が「Lease が切れたか」の判定と、保存する時刻（`enqueued_at`、`claimed_at`、`lease_expires_at`、`finished_at`）に使う時計は、**Database の時計（`now()`）だけ**とする。Worker が各自の時計を渡す方式は、時計が進んでいる Worker や誤った未来の時刻が有効な Lease を奪い、同じ Task を 2 つの Worker で始めさせ得るため採らない。
- 明示の時刻は Test のための継ぎ目（`TaskQueue(..., allow_explicit_now=True)`）に限る。本番の Queue は呼び出し側の時刻を拒否する。
- Lease の長さ（既定 60 秒、最大 86,400 秒）は、Preset の数値と同じく仮の値である。

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

## 選定理由

- 数値は、1 台の GPU Server で個人〜小規模チームが使うことを想定した、桁を合わせるための仮の値であり、実測に基づかない。
  Benchmark（PAW-016 / 017）と実運用の記録で見直す前提で、データとして 1 か所に置いた。
- Budget 超過を Loop より優先するのは、Escalation が予算を追加で消費するため。
- Lease の世代に `claim_count` を使うのは、既存の列で足り（Migration も Grant も変えない）、Claim のたびに必ず増え、Worker の id・時刻・乱数のような呼び出し側の値に頼らずに、古い Claim を判別できるため。
- 試行の Fencing に PAW-032 の `tasks.attempt` を使うのは、Restart が既に増やす唯一の Counter で、Step・Log・Tool の書き込みも同じ規則（古い試行は `StaleAttemptError`）で拒否しているため。失敗の行へ試行の Column を足して現在の試行だけを読む案は、当初は「`clear` を Restart の後に呼ぶ規則で足りる」として採らなかったが、独立したレビューで、Restart の Commit の後・`clear` の前に新しい試行が記録した失敗が、Task 全体を消す `clear` で失われる（または古い履歴と一緒に判定される）と指摘され、その規則では足りないと分かったため採る。Grant は変わらない（`tasks` の SELECT は付与済みで、行の UPDATE は不要）。Restart と掃除を 1 つの Transaction にする案は、Restart（PAW-032 の Command）と Loop（PAW-033）の境界を壊すため採らない。新しい試行の Dispatch を掃除の完了まで止める案は、Orchestrator（PAW-034）に順序の規則を課すだけで、それを守らない呼び出しを防げないため採らない。
- Lease の時計を Database に一本化するのは、複数の Process（Worker）が同じ Entry を巡って競うため、判定の基準が呼び出し側ごとに違うと Lease の排他が成り立たないため。

## 代替案

- 数値を Preset に固定せず、Admin が設定する: Preset の名前を要件が定める以上、まず既定の値が要る。設定 UI と認可は別の Issue。
- 超過時にすべて `FAIL` にする: 人間が上限を上げて続けられなくなる。`retries` だけを `FAIL` にした。
- Aging を入れる: 要件に規則がなく、`LOW` の待ち時間の上限を決める必要がある。
- Worker id に Process 固有の値（PID、起動時刻）を含めさせる: 呼び出し側の規則に頼ることになり、Queue が古い Claim を拒否する保証にならない。別の Token 列を追加する案は、Claim のたびに増える `claim_count` で足りるため採らない。
- 呼び出し側が時刻を渡す（または Process の時計を使う）: 時計のずれや誤った時刻で Lease を奪える。Constructor で時計を注入する案は、Test の呼び出しの書き換えが大きいため、既定で拒否する引数の継ぎ目を選んだ。

## 承認後の扱い

承認された値と選択を、この Decision の `Approval` に記録して Status を Approved に改める。
値が変わる場合は `domain.PRESET_LIMITS` / `LoopPolicy` のデータと Test の期待値を、承認された値に合わせる。
承認されるまで、この値を前提にした運用（Budget の上限に頼る自動停止の設計など）をしない。
