# Agent の OOM と Escalation の記録と System Health での数え方（OOM の見分け方と報告の仕方、Error Class の名前、閾値）

- Status: Proposed
- Date: 2026-10-01
- Scope: Issue [#183](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/183)（Decision 0059 の 1 の後続）。`apps/backend/paw_backend/orchestrator/`（`errors.AGENT_OUT_OF_MEMORY`・`OUT_OF_MEMORY_CLASSES`、`domain.IncidentKind`、`models.AgentIncidentRow`、`DagStore.fail_node`・`DagStore.record_incident`、Planner の失敗）、`apps/backend/paw_backend/health/`（`TaskQueueSource`、`limits.AGENT_OOM_ERROR`、`HealthStore.roll_up` の Purge）、Migration `0183`（`agent_incidents`）
- Supersedes: なし。[Decision 0059](0059-system-health-observability.md)（Approved）の 1・2 が後の Issue に委ねた「OOM と Escalation の数」を決める。[Decision 0021](0021-dag-orchestrator-policy.md) の 3（Runtime が名乗れる Error Class は固定の名前だけ）の一覧に 1 つを加える（書き換えない）

## 背景

[REQUIREMENTS.md](../../REQUIREMENTS.md) は次を FIXED にしている。

- 「Observability / System Health baseline」: 常時監視の対象に「Agent failure / loop / retry / OOM等」。Severity の `ERROR` の例に「OOM連発」。
- 「Observability retention / downsampling」: 重要 Event の例に「Agent escalation」と「OOM」。重要 Event は時系列と分けて集約せずに保持する。
- [docs/OBSERVABILITY.md](../OBSERVABILITY.md) の Core signals に「OOM / model load failure / unload failure」と「Escalation」。

Decision 0059（Approved）の 1 は、OOM と Escalation の実行の数を「それを残す記録がまだないので、記録ができてから加える（別 Issue）」とし、承認時の決定でそれを #183 に回した。

今の実装では次のとおりで、どちらも数えられない。

- Runtime（Adapter）が報告できる失敗の Class は `errors.RUNTIME_ERROR_CLASSES`（Python の標準の例外の名前と `AdapterError`）だけで、OOM を表す名前がない。Python の `MemoryError` はあるが、別の Process の Model Server の CUDA OOM や OOM Killer はこれにならない。
- Escalation は `DagStore.fail_node` が Node の `agent_index` を上げるだけで、その事実は残らない（Node の行は上書きされ、試行の行は次の手を持たない）。
- `loop_failure_signatures` は Window の分しか残らず、Class 名も持たない。

**次のことは要件も既存の Decision も決めていない。** OOM をどこで見分け、誰がどう報告するか。その Error Class の名前。記録をどこに残すか。System Health のどの Component で、どこからを `WARNING` / `ERROR` とするか。
[AGENTS.md](../../AGENTS.md) の「仕様変更」に従い、実装はこれらを下の推奨で置き、この Decision で承認を求める。

## 提案

### 1. OOM の見分け方と報告の仕方

- OOM を見分けるのは、その Model・Process を呼ぶ Agent の Runtime（Adapter）とする。Adapter は、Model の Server が GPU の Memory 不足を返したとき（vLLM / SGLang などの CUDA OOM の応答）と、呼んだ Process が Memory 不足で落とされたとき（Kernel の OOM Killer）に、`NodeOutcome.failed("AgentOutOfMemory", ...)` を返す。失敗の文（`message`）は今までどおり Hash にするだけで、保存しない。
- Runtime が Python の `MemoryError` を上げた（または報告した）ときも OOM として数える（Backend の Process の中で Memory が尽きた）。
- Orchestrator 自身は OOM を推測しない（Exit Code や文の中身を見ない）。文の中身は Adapter の選んだデータで、見分けを Orchestrator に置くと Adapter ごとの形式に依存するため。
- Compute Scheduler の Model の Load / Unload の失敗は、今までどおり `compute` の Component（Decision 0059 の 2）で扱い、ここでは数えない。
- 他の案: (a) Orchestrator が失敗の文から `out of memory` などの語を探す。文の形式が Runtime ごとに違い、文を読むこと自体が「失敗の文は保存も Log もしない」方針に近づくので推奨しない。(b) Compute Scheduler が GPU の空き VRAM の急減から推測する。どの Agent の失敗かと結び付かず、誤検知が多いので推奨しない。
- 実際の Adapter（vLLM の Client、Codex / Claude の CLI）はまだ Repository にないので、この Issue では報告の口（名前と記録）までを作り、各 Adapter が見分ける実装はその Adapter の Issue で行う。

### 2. Error Class の名前

- `AgentOutOfMemory`（`errors.AGENT_OUT_OF_MEMORY`）を `RUNTIME_ERROR_CLASSES`（Runtime が名乗れる閉じた一覧、Decision 0021 の 3）に加える。Node の試行の `error_class` にもこの名前で残る。
- OOM として数える Class は `OUT_OF_MEMORY_CLASSES` = `AgentOutOfMemory` と `MemoryError` の 2 つ。
- OOM でも Retry・Alternative・Escalation の扱いは変えない（Decision 0007 のまま。`retryable=False` を返すかは Adapter が決める）。
- 他の案: `OutOfMemoryError`（PyTorch の `torch.cuda.OutOfMemoryError` と紛れる）、`CudaOutOfMemory`（OOM Killer の場合を含まない）。

### 3. 記録の場所（`agent_incidents`、Migration `0183`）

- 新しい Table `agent_incidents`（`id`、`kind` = `out_of_memory` / `escalation`、`occurred_at` = DB の `now()`）に、1 件ごとに 1 行を入れる。Task・Node・Agent・文は持たない（失敗した試行そのものは `agent_dag_node_attempts` に残り、Admin がそこから辿れる）。
- Node の失敗では、`DagStore.fail_node` の Transaction の中で書く。Class が `OUT_OF_MEMORY_CLASSES` なら `out_of_memory`、次の手が `escalate` なら `escalation`（OOM で Escalation したときは 2 行）。古い Epoch・Attempt の報告は拒まれ、何も書かない（同じ失敗を 2 回数えない）。
- Planner（Node がない）の OOM は `DagStore.record_incident` で別の Transaction に書く。書けなかったら Log に Class 名だけを残し、Run は止めない。
- 保存期間は Decision 0059 の 4・5 と同じ `PAW_HEALTH_RETENTION_DAYS`（既定 400 日）とし、時系列の Roll-up と一緒に消す（重要 Event は 1 年以上）。Application の Role には SELECT / INSERT / DELETE だけを与え、更新はさせない。
- 他の案: (a) `agent_dag_node_attempts` に「次の手」の列を足し、OOM は `error_class` から数える。Planner の OOM を数えられず、大きくなる Table に Index を足すことになるので推奨しない。(b) `health_events` に入れる。あれは Component の Severity の変化の記録で、件数を数える用途と混ざるので推奨しない。(c) `task_events` に Command を足す。Task の状態遷移の記録で、Escalation は状態を変えないので推奨しない。

### 4. System Health での数え方と閾値

- Component は Decision 0059 の `task_queue` とする（Agent の failure / loop / retry と同じ所。新しい Component を足すと Severity の Event と UI の一覧も変わる）。
- 数値: `oom_last_hour`、`oom_last_day`、`escalations_last_hour`、`escalations_last_day`（時系列にも残る）。理由の Code: `agent_out_of_memory`、`agent_escalations`。
- 閾値（`health/limits.py`、暫定値の定数）: 直近 1 時間の OOM 1 件＝`WARNING`、3 件以上（`AGENT_OOM_ERROR`）＝`ERROR`（要件の「OOM連発」）。直近 1 時間の Escalation 1 件以上＝`WARNING`（Retry と同じく「単発の軽微な」異常。Escalation は Ladder の正常な動きでもあるので、それだけでは `ERROR` にしない。続く失敗は失敗・Loop の数が `ERROR` にする）。
- 他の案: 新しい Component `agents` を作る。Escalation を数値だけにして Severity を変えない（`INFO`）。OOM の `ERROR` を 2 件からにする。

## 決めてほしいこと

1. **OOM を見分けるのは Agent の Runtime（Adapter）とし、Model Server の CUDA OOM と OOM Killer による終了を `AgentOutOfMemory` で報告する。Python の `MemoryError` も OOM に数える。Orchestrator は失敗の文を見て推測しない**（1）でよいか。推奨: はい。
2. **Error Class の名前を `AgentOutOfMemory` とし、Runtime が名乗れる閉じた一覧（Decision 0021 の 3）に加える。OOM でも Retry・Escalation の扱いは変えない**（2）でよいか。推奨: はい。
3. **記録は新しい Table `agent_incidents`（種類と時刻だけ）に、Node の失敗と同じ Transaction で 1 件 1 行で残し（Planner の OOM は別に）、`PAW_HEALTH_RETENTION_DAYS`（既定 400 日）で消す**（3）でよいか。推奨: はい。
4. **`task_queue` で数え、直近 1 時間の OOM 1 件＝`WARNING`・3 件以上＝`ERROR`、Escalation 1 件以上＝`WARNING`（`ERROR` にはしない）**（4）でよいか。推奨: はい（数値は暫定値）。
5. **Migration の Revision を `0183`（`down_revision = 0066`）とする**でよいか。推奨: はい（Merge の時に振り直してよい）。

## 承認後の扱い

承認されたら `Approval` に記録し、Status を Approved に改める。閾値は `apps/backend/paw_backend/health/limits.py` で変えられる。
方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。
[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。
