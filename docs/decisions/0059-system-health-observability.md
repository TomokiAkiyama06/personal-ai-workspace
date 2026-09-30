# System Health / Observability Backend の方針（見る対象と閾値、誰が何を読めるか、時系列の保存と Downsampling、重要 Event、通知との関係）

- Status: Proposed
- Date: 2026-09-30
- Scope: PAW-066（[#52](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/52)）の `apps/backend/paw_backend/health/`（Source、Monitor、Store）、`/api/v1/system/health*`、Capability `system_health.summary.read` と `admin.system_health.view`、Migration `0066`（`health_metric_samples`、`health_events`、`connection_usage` の部分 Index、`task_events` の失敗・Retry と `loop_failure_signatures` の時刻の Index、`tasks` の実行中・終了した Task の部分 Index）、`AbandonedCallReaper.stats`
- Supersedes: なし。[Decision 0004](0004-rbac-capability-and-audit-policy.md)（Approved）の Capability 表と読み取り専用の許可リストに 2 つを加える（書き換えない）
- Approval: なし（未承認）

## 背景

[REQUIREMENTS.md](../../REQUIREMENTS.md) は次を FIXED にしている。

- 「Observability / System Health baseline」: 常時監視の対象（GPU utilization、VRAM used / reserved / available、Main Model の residency / health、Memory Worker、Task Queue / Running / Waiting for Resource、PostgreSQL、Recovery Repository の最終 Projection / Commit / Push、Claude / Codex 等の接続状態、Agent の failure / loop / retry / OOM）。Severity は Notification Policy の `INFO` / `WARNING` / `ERROR` / `CRITICAL`（PostgreSQL 異常は `CRITICAL`、Backup / Recovery Push の継続失敗は `ERROR`）。通常時は静かにし、詳細は System Health / Admin 画面から展開する。
- 「Observability retention / downsampling」: 直近 24 時間は 10〜30 秒、7 日は 1 分、30 日は 5 分、それ以降は 1 時間の集計。重要 Event は時系列と分けて集約せずに保持（運用上の重要 Event は 1 年以上）。保存期間・粒度は変更できるようにする。
- [docs/OBSERVABILITY.md](../OBSERVABILITY.md) は「Metrics は運用のデータで、User の Private な内容を覗くものではない」とし、永続化する Metrics の範囲・保存の実装・User 別の見せ方を実装時の選択にしている。
- [docs/UI_DESIGN.md](../UI_DESIGN.md) は、Header の Compact な表示、Admin の System Health の詳細、一般 User には `Claude: Available / Unavailable` だけ、を FIXED にしている。

**次のことは要件も既存の Decision も決めていない。** どの Check を置き、どこからを `WARNING` / `ERROR` とするか。誰がどこまで読めるか（Capability）。時系列をどこにどう保存し、1 時間の集計をいつまで持つか。重要 Event として何を記録するか。通知（Notification Center）へどうつなぐか。
[AGENTS.md](../../AGENTS.md) の「仕様変更」に従い、実装はこれらを下の推奨で置き、この Decision で承認を求める。

**GPU の安全性（この Decision の対象外の、守るべき条件）**: System Health は GPU を読むだけで、Model の Load / Unload、vLLM、Container、Benchmark には触れない。Compute Scheduler がある構成ではその `status()`（メモリ上の値）だけを読み、ない構成では Scheduler と同じ読み取り専用の Probe（`nvidia-smi --query-*` の 2 つ）だけを使う。Process の ID と名前は返さない（他の User のものかもしれない）。Test はすべて Fake を使う。

## 提案

### 1. 見る対象（Component）と Source

一つの Component に一つの Source を置き、Source は読むだけで、返すのは Severity・閉じた Status（`ok` / `degraded` / `failing` / `stale` / `unavailable` / `not_configured` / `never_ran` / `check_failed`）・理由の Code・数値だけとする（依存先の Message、Path、URL、User の文、PID は返さない）。

| Component | 読むもの |
| --- | --- |
| `database` | `Database.check()`（`SELECT 1`）と所要時間 |
| `compute` | Compute Scheduler の `status()`（VRAM の actual / reserved / external / headroom / available、Utilization、Lease と待ちの数、Relief、各 Model の Role・状態＝Residency）と Full GPU Mode の `status()`。Scheduler がなければ、`PAW_HEALTH_GPU_PROBE` を有効にしたときだけ読み取り専用の Probe、どちらもなければ `not_configured` |
| `task_queue` | 全 User の Task の数（状態別、`waiting` は理由別、1 日の `completed` / `cancelled`）、直近 1 時間・1 日の失敗（`task_events` の `fail`。Retry された Task は失敗ごとに数える。Codex の P1、PR #170）、直近 1 時間の Retry（`task_events` の `retry`）と Loop（`loop_failure_signatures` で同じ Attempt・Approach の同じ Signature が Loop Policy の `repeat_threshold` 回以上＝検知器の TRY_ALTERNATIVE / ESCALATE の条件）。ID・題名・Project は返さない |
| `memory_worker` | Memory の Consolidation Queue（待ち・Lease 中・最古の待ち時間・直近 1 日の Dead letter）。Memory Worker の Model 自体は `compute` |
| `connections` | Shared Codex / Claude Connection の設定の有無・有効・状態・最後の確認からの時間・実行中の呼び出し。Credential と Handle は読まない |
| `connection_reaper` | `AbandonedCallReaper` の各 Cycle（片付けた件数、連続の失敗、最後の Error の型）。#52 の Comment（PR #106）への対応 |
| `recovery_backup` / `memory_projection` / `audit_retention` | Timer で動く 3 つの Job が Run ごとに書く `audit_events` の行から、最後の Run・最後の成功からの時間（DB の時計）・最後の成功以降の連続失敗 |

- Agent の failure / loop / retry は `task_queue` で数える（Loop は Task を失敗させないので、失敗の数とは別に数える。Codex の P1、PR #170）。OOM と Escalation の実行は、それを残す記録がまだないので、記録ができてから加える（別 Issue）。
- `audit_events` には Job の行を引く Index がない。Job の 3 つは 300 秒に 1 回だけ読む（他は 10 秒）。Index を足す案もあるが、Partition した Audit の Table への DDL は避けた。

### 2. Severity の閾値（`apps/backend/paw_backend/health/limits.py`）

- `database`: 応答しない＝`CRITICAL`（Monitor の 5 秒で終わらない Check も含む。`PAW_DATABASE_TIMEOUT_SECONDS` がそれより長くても `check_failed` にしない。Codex の P1、PR #170）、Check が 1 秒を超える＝`WARNING`。
- `compute`: Probe が読めない・Main の構成変更が必要（Relief 6）・Model の操作が失敗（`failed`）・通常 Mode で Main が GPU にいない・Full GPU Mode の終了後に Main が戻らない＝`ERROR`。VRAM の圧迫・Relief の実行中・VRAM 待ちの仕事・Full GPU Mode の開始失敗＝`WARNING`。
- `task_queue`: 直近 1 時間の失敗 1 件＝`WARNING`、5 件以上＝`ERROR`。Loop 1 件＝`WARNING`、3 件以上＝`ERROR`。Retry 1 件以上＝`WARNING`（Notification Policy の「retry」）。
- `memory_worker`: 直近 1 日の Dead letter 1 件以上＝`WARNING`。
- `connections`: 有効な接続が `expired`＝`ERROR`、`unavailable`＝`WARNING`。無効・未設定は `INFO`（どちらも使わない構成がある）。
- `connection_reaper`: Cycle の失敗 1〜2 回連続＝`WARNING`、3 回連続＝`ERROR`。1 行でも片付けた＝`WARNING`（Process が呼び出しの途中で落ちた印）。
- Job: 一度も動いていない＝`INFO`（`never_ran`。その構成で動かしていないことがある）。最後の Run の失敗＝`WARNING`、3 回連続＝`ERROR`（Notification Policy の「継続失敗」）。最後の成功が古い（Backup 2 時間／24 時間、Projection 1 時間／24 時間、Audit retention 2 日／7 日。`WARNING`／`ERROR`）。数値は `deploy/systemd` の Timer（30 分、5 分、毎日）から決めた暫定値。
- Check 自体が失敗・5 秒で終わらない＝`WARNING`（`check_failed`。`database` を除く）。全体の Severity は Component の最悪値。

### 3. 誰が何を読めるか（新しい Capability 2 つ）

- `system_health.summary.read`（`Scope.SYSTEM`、委任不可、読み取り専用＝Audit は拒否だけ）: User・Admin・Owner。`GET /api/v1/system/health/summary` が、全体の Severity と Codex / Claude の `available` / `unavailable` だけを返す（Header の Compact 表示と、一般 User 向けの `Claude: Available / Unavailable`）。
- `admin.system_health.view`（同じ性質）: Admin・Owner。`GET /api/v1/system/health`（全 Component の Severity・Status・理由・数値・部分）、`GET /api/v1/system/health/metrics/{name}`（時系列）、`GET /api/v1/system/health/events`（Severity の変化）。
- Agent と `system` Role には与えない。読み取り専用の許可リスト（Decision 0004）に 2 つを加える（内容は運用の数値と Code で、User の内容を含まない）。
- 代わりに、一般 User にも Component 別の Severity を見せる案、既存の `admin.config.manage` などに含める案もある。後者は「設定の変更」と「状態を見る」を分けて Audit できない。

### 4. 時系列の保存と Downsampling（PostgreSQL、Migration `0066`）

- Backend の Process が `PAW_HEALTH_SAMPLE_INTERVAL_SECONDS`（既定 30、10〜30。要件の「直近 24 時間: 10〜30 秒」が FIXED なので、止める値やそれより粗い値は受けない。Codex の P2、PR #170）ごとに全 Source を読み、数値（`bool` と文字列を除く）と各 Component の Severity の段階（0〜3）を `health_metric_samples` に 1 行ずつ入れる。時刻は DB の時計で Interval に揃え（`date_bin`）、同じ枠に 2 つ目の Process が入れても増えない（`ON CONFLICT DO NOTHING`）。最初の Sample は起動から 1 Interval 後。
- 20 Cycle ごと（既定で 10 分ごと）に、24 時間より古い生の行を 1 分へ、7 日より古い 1 分を 5 分へ、30 日より古い 5 分を 1 時間へ移す（件数・合計・最小・最大。平均は合計÷件数）。移す行の削除と次の粒度への Merge を 1 文で行うので、同時に 2 つの Process が動いても 1 つの Sample は 1 回だけ数える。
- 1 時間の集計は `PAW_HEALTH_RETENTION_DAYS`（既定 400 日、366〜3650）で消す。要件の「それ以降: 1 時間集計」は期限を書いていない。無期限にする案もあるが、容量が際限なく増える。
- 取り出すときは要求した範囲を最大 1,000 点に集約する（最小 10 秒）。
- 外部の時系列 DB（Prometheus 等）を使う案もある。V1 は既存の PostgreSQL だけで足り、Backup・権限・運用が増えないので推奨しない。Prometheus 形式の出力は後で加えられる。

### 5. 重要 Event（`health_events`）

- Component の Severity が前の Event と変わったときだけ 1 行を記録する（前後の Severity、Status、理由の Code。数値は持たない）。時系列とは別の Table で、集約しない。2 つの Process が同じ変化を二重に記録しないよう、Transaction の Advisory Lock の中で「最後の Event と違い、それより古くないときだけ」入れる（複数の Process が同じ停止を後から書くとき、古い変化が 2 度目の停止を作らない。Codex の P1、PR #170）。DB の `now()` より先の時刻（時計の進んだ Host）は `now()` として記録し、後の変化を止める壁にしない。
- PostgreSQL に書けなかった間の Report は Process の中に残し（Severity が変わったものだけ、最大 100）、書けるようになった最初の Cycle で順に、Report を作った時刻で記録する。PostgreSQL 自体の停止（`critical`）も、復旧の後に Event として残る（Codex の P1、PR #170）。Process が再起動すると、残していた分は失われる。
- 保存期間は 4 と同じ `PAW_HEALTH_RETENTION_DAYS`（1 年以上）。
- 要件の他の重要 Event（Task の開始・失敗、Permission denial、Security の操作、Recovery の Push 失敗）は、それぞれ既に `task_events`・`audit_events` に残っている。ここでは複製しない。

### 6. 通知（Notification Center）との関係

- Notification Center と Notification Rules はまだない（別 Issue）。この PR は Severity を Notification Policy の 4 段階で出し、変化を `health_events` に残すところまでとする。通知はその Event を元に後の Issue で作る（SSE / WebSocket の Event にするのも同じ）。

### 7. Index（#52 の Comment と、Source の読み取り）

- `connection_usage (started_at) WHERE status = 'in_flight'` の部分 Index を Migration `0066` で加える。実行中の行だけを持つので小さく、Reaper と `connections` の Source がこれで引く。
- 直近の失敗・Retry・Loop を数えるため、`task_events (created_at) WHERE command IN ('retry', 'fail')` の部分 Index と `loop_failure_signatures (created_at)` の Index も加える。
- Task の状態別の数は 10〜30 秒ごとに読むので、`tasks (state)` の実行中の Task（`queued` / `running` / `waiting` / `paused` / `evaluating`）の部分 Index と、`tasks (updated_at)` の完了・取消の Task の部分 Index を加え、終わって久しい Task を読まない（Codex の P2、PR #170）。

### 8. Migration の Revision 番号

- この Lane に割り当てられた `0052` は、既に `0052_research_provenance.py` が使っている（Alembic の Revision が重複すると History が壊れる）。そこで Issue の PAW 番号から `0066` とした（`down_revision = 0154`）。Merge の時に振り直してよい。

## 決めてほしいこと

1. **見る対象を 1 の 9 つの Component とし（Retry と Loop は `task_queue`）、OOM と Escalation の数は記録ができてから後の Issue で加える**（1）でよいか。推奨: はい。
2. **Severity の閾値を 2 のとおり（暫定値、定数）とする**（2）でよいか。推奨: はい。設定で変えられるようにする案もある。
3. **Compact な状態（全体の Severity と Codex / Claude の可否）は全 User（`system_health.summary.read`）、詳細・時系列・Event は Owner / Admin（`admin.system_health.view`）。どちらも委任不可・読み取り専用の許可リスト**（3）でよいか。推奨: はい。
4. **時系列は PostgreSQL の `health_metric_samples` に 30 秒ごと（10〜30 秒で設定可、止められない）に入れ、要件の粒度で移し、1 時間の集計は 400 日で消す**（4）でよいか。推奨: はい。無期限に残す案、外部の時系列 DB の案もある。
5. **重要 Event は Component の Severity の変化だけを `health_events` に残し（PostgreSQL に書けなかった間の変化は Process の中に残して後で書く）、400 日で消す。他の Event は既存の `task_events` / `audit_events` に任せる**（5）でよいか。推奨: はい。
6. **通知は後の Issue とし、ここでは Severity と Event まで**（6）でよいか。推奨: はい。
7. **Scheduler のない構成での GPU の Probe は既定で無効（`PAW_HEALTH_GPU_PROBE`）**でよいか。推奨: はい（GPU のない Host で常に `ERROR` になるのを避ける）。Scheduler を組み込む #165 の後は Scheduler の値を使う。
8. **Migration の Revision を `0066` とする**（8）でよいか。推奨: はい（Merge 時に振り直してよい）。

## 承認後の扱い

承認されたら `Approval` に記録し、Status を Approved に改める。数値は `apps/backend/paw_backend/health/limits.py` と `PAW_HEALTH_*` の設定で変えられる。
方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。
[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。
