# 再開した後にまだ動いていない Task の `session_only` の Memory を、終わった Run のものとして退役させる（Decision 0047 の 1 の変更）

- Status: Proposed
- Date: 2026-09-30
- Scope: Issue [#90](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/90) の #151 `orchestrator/task_end.py:233` の保留（PR #163 の説明と #90 のコメント）。`TaskEndResidue`（Fence の `hold_ended` と Sweep の 2 つの Query）と `TaskEndCleanup.finish`（`apps/backend/paw_backend/orchestrator/task_end.py`）だけを扱う。Migration はない
- Supersedes: [Decision 0047](0047-task-execution-composition-and-task-end-effects.md) の **1 節だけ**（後処理と Sweep の対象を「終了状態の Task」とし、終了状態からの遷移（Retry / Restart）では承認だけを取り消す、という範囲）。0047 のそれ以外（2〜7 節）は変えない。0047 は書き換えない

## 背景

Decision 0047 の 1 は、Task の終了の後処理（開いた承認の取り消しと、Task から来た `session_only` の Memory の退役）を、遷移の Commit の直後の Listener と、保存された状態から残りを探す Sweep の 2 段で行うとした。PR #151 の Codex の P1 を受けて、後処理は Task の行を `FOR SHARE` で持ち（Fence）、その下で Task が終了状態のときだけ動く。Task の Memory と承認は Task の id だけで照合するので、再開の後の**新しい Run** が作ったものを消さないためである。

この形では、Retry / Restart が Fence より先に Task の行を取ると（Listener の後処理が Retry の Commit を待った、Sweep が見つけた後に再開された、など）、後処理は Task が終了状態でないのを見て何もしない。再開した Task は Sweep の対象でもないので、**終わった Run の `session_only` の Memory が退役されずに残る**（PR #151 の Codex の P2、`task_end.py:233`。PR #163 で再現を確認）。Retrieval は `session_only` を返さないので使われはしないが、REQUIREMENTS.md の「Memory Freshness」は `session_only` を Task の終了の後に残さないとしている。Memory の Source には Run の印がなく、Task Event の時刻は Process の時計なので、「終わった Run の Memory」を時刻で切り分けることはできない。

## 提案

1. **再開の後にまだ遷移がない間は、Task の `session_only` の Memory と開いた承認をすべて終わった Run のものとみなす。** Retry / Restart は `failed` / `cancelled` の Task を `queued` に戻すだけで、新しい Run は Start（`queued` → `running`）から始まる。Start の前には新しい Run が書いたものはない。
   - 推奨: この形（Human が 2026-09-30 に #90 で案 A を選択）。代替: Task の Source に Run（試行と Retry 回数）を記録する（Migration が要る）、後処理が終わるまで Retry / Restart を拒否する（利用者に見える挙動の変更）、今のまま（再開した Task が次に終わると退役する）。
2. **「Run が終わった」の判定は、Task が終了状態であるか、Task の最新の遷移（`from_state` と `to_state` が違う `task_events` の行のうち `seq` の最大のもの）の `from_state` が終了状態であること。** 状態を変えない Event（Working Set の変更、書き込みの予約の解除）は遷移に数えない。`seq` は Task の行の Lock の下で遷移が Commit された順である。
   - 推奨: この形。代替: 最新の Event（遷移でないものを含む）で判定する（Working Set の変更などで判定が外れ、Memory が残る）。
3. **Fence（`finish()`）と Sweep の 2 つの Query（開いた承認、`active` の `session_only`）を、2 の条件に広げる。** Fence は、まず Task の行を `FOR SHARE` で取り、**Lock を取った後の別の文で** 2 の条件を読む。READ COMMITTED の 1 つの文が Lock を待った場合、その文は遷移の後の Task の行を見るが、同じ遷移が書いた `task_events` の行はその文の Snapshot に入らない（Retry の Event を見落として何もしない、または Start の Event を見落として新しい Run の Memory を退役させ得る）ためである。Lock の下では遷移は Commit されないので、2 つ目の文は Commit 済みの最新の状態を読む。
   - 推奨: この形。代替: Transaction を REPEATABLE READ にする（Lock を待った後の更新で直列化の失敗になり、再試行が要る）。
4. **Listener は変えない。** 終了状態への遷移で `finish()` を、終了状態からの遷移で承認の取り消しを行う（0047 の 1 のまま）。Retry に Fence を先に取られた終了の Listener の `finish()` は、Retry の Commit の後に Lock を取り、3 の判定で後処理を行う。それも失敗すれば Sweep が見つける。
   - 推奨: この形。代替: 再開の Listener でも `finish()` を呼ぶ（Fence の外で遅れた Listener が、Start の後に新しい Run のものを取り消す余地を作らないよう、今の形を保つ）。

## リスク

1. 再開した Task が Start の前に別の遷移（Pause、Resource の `waiting` など）をすると、その後は「動いた」とみなし、残った Memory は次にその Task が終わるまで残る（今と同じ扱い。Retrieval は `session_only` を返さない）。
2. Start の前に Task を Source とする `session_only` の Memory を人が書いた場合、それも終わった Run のものとして退役する（`session_only` は Task の Session の間だけのものなので、実害は小さい）。
3. Sweep の Query は Task ごとに `task_events` の最新の遷移を読む（`ix_task_events_task_id`（`task_id, seq`）を使う）。対象は開いた承認か `active` の `session_only` を持つ Task だけで、1 回に最大 100 件。
