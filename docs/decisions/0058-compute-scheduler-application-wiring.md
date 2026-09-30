# Compute Scheduler を Application に組み込む方針（有効にする方法、VRAM の警告の Sink、Full GPU Mode の HTTP の経路: 非同期の開始・開始の取りやめ・認可・応答の形、Local の Runtime の配線、終了時）

- Status: Proposed
- Date: 2026-09-30
- Scope: Issue [#165](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/165)。`paw_backend/compute/wiring.py`（`ComputeSetup`、`LocalRuntime`、`RecentVramWarnings`、`FullGpuController`、`build_compute`）、`paw_backend/app.py`（`create_app(compute=..., local_runtimes=...)` と Lifespan）、`paw_backend/orchestrator/composition.py`（`build_task_execution(scheduler=..., local_runtimes=...)`）、`paw_backend/api/v1/compute.py`（`/api/v1/admin/compute/full-gpu`）。Migration はない
- Supersedes: なし。[Decision 0037](0037-gpu-compute-scheduler.md)（Scheduler）、[Decision 0042](0042-gpu-free-vram-admission.md)（空き VRAM と警告）、[Decision 0055](0055-kaggle-full-gpu-mode.md)（Full GPU Mode）、[Decision 0047](0047-task-execution-composition-and-task-end-effects.md) / [Decision 0056](0056-production-worktree-wiring.md)（本番の組み立て）はどれも書き換えない。0042 の 6 と 0055 の 8 が「Scheduler を Application に組み込むとき」に残した点を決める

## 背景

`ComputeScheduler`（PAW-036、Decision 0037）、空き VRAM の警告（Decision 0042）、`FullGpuMode`（PAW-037、Decision 0055）は Service として Merge 済みだが、Application の Lifespan に組み込まれていなかった。次のことはすでに決まっている。

- Scheduler はプロセスに 1 つで、状態はプロセス内（0037 の 1）。`serve()` が `refresh()` を定期に呼ぶ。
- 警告は Scheduler が Log に出し、注入した `VramWarningSink` にも渡す。Scheduler は Audit を書かない。Sink の実装は Lifespan への組み込みのときに作り、推奨は System Health の Event（0042 の 6、Approved）。
- Full GPU Mode の開始・終了は Owner / Admin の `admin.compute.full_gpu`（委任不可、Audit 必須）で、Passkey の Step-up は求めない（0055 の 1、Approved）。HTTP の経路と UI は「Scheduler を Application に組み込む Issue」で作る（0055 の 8、Approved）。
- `CloudPolicy` は注入しない（0037 の 14、Approved。本番の Cloud の組み立てができるまで）。

**次の選択は、要件も上の Decision も決めていない。** 実装は各点で下の推奨を採っている。

## 提案（番号は「決めてほしいこと」の番号）

### 1. 有効にするのは `create_app(compute=ComputeSetup(...))` の注入だけ（環境変数・設定 File は作らない）

- `ComputeSetup(config, probe, control=None, refresh_seconds=5, kept_vram_warnings=50)` を渡したときだけ、`create_app` が Scheduler を作り（`app.state.compute`）、Lifespan が `scheduler.serve(stop)` を動かす。Database があれば `FullGpuMode(scheduler, PostgresTaskHolds(tasks, queue), authorizer)` も作って `serve(stop)` を動かす。渡さなければ何も起動しない。
- Database がない構成では Scheduler だけが動き、Full GPU Mode は作らない（Task の Hold に PostgreSQL が要る）。HTTP の経路は 503 を返す。
- 理由: Model の構成（Footprint の値、Load / Unload の Command）は Benchmark（PAW-017 / PAW-019）と Runtime の Adapter で決まり、まだ決まっていない。Agent の Runtime（`agent_runtimes`）と git の Runner（Decision 0056 の 3）と同じく、Deployment が組み立てて渡す。`ModelControl` の Command（`systemctl` など）を環境変数や File から読むと、それを書ける人が Backend に任意の Command を実行させられるので、その形式と保護（所有者・権限の確認）は Runtime の Adapter の Issue で決める。
- 代わりに、`PAW_COMPUTE_CONFIG_FILE`（JSON / TOML で Deployment と Command を書く）をこの Issue で作る案もある。

### 2. `VramWarningSink` は、最新の警告をプロセス内に 50 件まで持ち、Full GPU Mode の `GET` に出す（DB・Audit・Event Bus には書かない）

- `RecentVramWarnings`（`compute/wiring.py`）は、受け取った `VramDeferral` を時刻（UTC）と一緒にプロセス内に最新 50 件まで持ち、受け取った総数を数える。Event Loop の上で呼ばれ、ブロックしない。
- Log は Scheduler 自身が `paw_backend.compute` の WARNING として出している（0042 の 5。同じ種類は 300 秒に 1 回）。Sink は Log を重ねない。
- Admin は `GET /api/v1/admin/compute/full-gpu` の `vram_warnings` / `vram_warnings_total` で見る。中身は仕事の種類・Class・Byte 数・諦めたかだけで、PID や Process 名はない。
- 理由: 0042 の 6 の推奨は System Health（PAW-066）の Event だが、System Health はまだない。Audit（`audit_events`）は人と Agent の行為の記録で、GPU の混み具合を書くと Audit の保存期間と量を押し上げる。Event Bus（SSE / WebSocket）は今は System の Event だけで、認可なしで配っている（`events.py`）。System Health ができたら、この Sink をその Event に差し替える。
- 代わりに、`audit_events` に書く案、Event Bus に新しい種類として流す案（認可を足す必要がある）もある。

### 3. `POST` は開始を Background で始めてすぐ `202` を返し、結果は `GET` で読む

- Full GPU Mode の開始は、走っている Local GPU の仕事の Drain を `drain_seconds`（既定 600 秒）まで待ち、さらに Unload と確認に時間がかかる。HTTP の Request をその間開いたままにしない。
- `POST` は Body（任意）の `preempt`（既定 `false`）・`drain_seconds`（0〜86,400）・`vram_bytes`（1 以上）を確かめ、`FullGpuController.start` が `FullGpuMode.start` を Background の Task で始めて、`202` と今の状態（`state: "starting"`、`start_pending: true`）を返す。開始が済めば `GET` の `state` が `on`、失敗すれば `last_failure`（`drain_timeout` など）が立ち、Hold した Task は再開する（0055 のとおり）。
- 開始の途中・`on` の間の `POST` は `409`（`full_gpu_mode_state`）。同じプロセスで同時に 1 つだけ。
- 代わりに、開始が済むまで `POST` を待たせる案もある（Client と Proxy の Timeout を 10 分以上にする必要がある）。

### 4. `DELETE` は、開始の途中なら開始を取りやめる

- `on` のときは `FullGpuMode.end` で終える（Model が Load し直され、Main が戻ってから Hold した Task を再開する。0055 の 6）。
- 開始の途中（Drain を待っている間など）の `DELETE` は、Background の開始を Cancel する。`FullGpuMode` は Cancel を受けて Scheduler を通常へ戻し（何も Unload していなければそのまま、Exclusive の Lease が渡っていれば返す）、Hold した Task は次の `tick()` で再開する。`200` と状態を返す。
- どちらでもないとき（`off` / `resuming`）は `409`。
- 理由: 開始は長く、始めた人が途中でやめられないと、Drain の期限まで Local の仕事が止まったままになる。
- 代わりに、開始の途中は `409` を返して期限まで待たせる案もある。

### 5. 認可は 3 つの操作とも `admin.compute.full_gpu`（`GET` も）。開始と終了は `FullGpuMode` がもう一度判定する

- Route の Guard（`require_capability(Capability.ADMIN_COMPUTE_FULL_GPU)`）が 401 / 403 / 503（Audit に書けない）を返し、判定を Audit に残す。開始と終了は `FullGpuMode` 自身も同じ Capability を判定して Audit に残す（0055 の 1。Service の認可を HTTP の経路に頼らない）。1 回の `POST` / `DELETE` で Audit の行が 2 つになる。開始の途中の取りやめ（4）は `FullGpuMode.end` を通らないので、Route の Guard の判定（と Audit）だけで行う（Cancel は `FullGpuMode` の公開の操作ではなく、開始を始めた Task の Cancel）。
- `GET` も同じ Capability で、Audit は `REQUIRED` なので読むたびに行が残る。Issue #165 が `admin.compute.full_gpu` での認可を指定している。UI が数秒ごとに読むと行が増えるので、UI は間隔を空けて読む（または System Health ができたら読み取り専用の経路へ移す）。
- Passkey の Step-up は求めない（0055 の 1 で承認済み。変えない）。
- 代わりに、読み取り専用の Capability（例: `admin.compute.view`、Audit は拒否だけ）を新しく置いて `GET` に使う案もある（Policy の変更）。

### 6. 応答の形: 状態と GPU の概要と最新の警告。Task の ID・PID・Process・Command・Model の名前は出さない

- `GET` / `POST` / `DELETE` はどれも同じ形を返す: `state`（`off` / `starting` / `on` / `resuming`。開始を受け付けて済んでいない間は `starting`）、`start_pending`、`held_tasks`（このプロセスが Hold した数）、`preempted`、`on_seconds`、`last_failure`、`needs_human`、`gpu`（Scheduler の `mode`、`probe_ok`、`vram` の `total_bytes` / `observed_free_bytes` / `external_bytes` / `headroom_bytes` / `available_bytes`、`vram_waiting`、`exclusive_waiting_for_vram`）、`vram_warnings`、`vram_warnings_total`。
- Scheduler がない・Database がない構成は `503`（`compute_not_configured`）。
- Model ごとの状態（Residency）や Class ごとの Lease の数は System Health（PAW-066）で出す。

### 7. Local の Model で走る Runtime は `local_runtimes` で渡し、組み立てが `HybridRuntime` で包む

- `create_app(..., compute=..., local_runtimes={"local": LocalRuntime(runtime, deployment="main", local_model=..., resource_class=CODING, wait_seconds=600)}, orchestrator_config=...)`。`build_task_execution` が各 `LocalRuntime` を `HybridRuntime(scheduler, runtime, deployment=..., late_gpu_charge=TrackerLateGpuCharge(budget))` で包み、`agent_runtimes`（Scheduler を通らない Runtime）と同じ Label の集合に入れる。Label の重複は `TypeError`。
- `CloudPolicy` と Cloud の Runtime は渡さない（0037 の 14）。GPU 時間の遅い計上（Decision 0050）は、Orchestrator と同じ `BudgetTracker` に行う。
- `local_runtimes` は `compute` と Database と `orchestrator_config` がないと `TypeError`（Scheduler を通らずに Local の Model を使う経路を作らない）。
- `ScheduledMemoryWorker` と `PlacedEmbedder` は、Memory Worker と Embedder がまだ Application に組み込まれていないので、ここでは配線しない（それらを組み込む Issue で同じ Scheduler を渡す）。

### 8. 終了時: 開始の途中なら取りやめ、`resuming` なら Unload した Model を戻してから Loop を止める。`on` のままならプロセスとともに終わる。起動時は Main が GPU に見えてから Task を再開する

- Lifespan の終わりに、開始の途中なら 4 と同じく取りやめる。開始はすでに Model を Unload しているかもしれない（`_empty_gpu()` の途中や、Unload の後の確認の間）。前に取りやめた開始（`DELETE`、失敗）と、終えた直後（`end`）も同じで、Full GPU Mode は `resuming` のまま、Loop がまだ Main を戻していないことがある。そこで Full GPU Mode が `resuming` なら（開始を取りやめたときに限らない）、`serve` の Loop を止める**前に**、Scheduler の `refresh()`（Main を Load し直す）と `FullGpuMode.tick()`（Main が戻れば Hold した Task を再開する）を、Full GPU Mode が `off` になるまで繰り返す（間は 0.1 秒）。全体を `shutdown_timeout_seconds`（既定 5 秒）で打ち切り、間に合わなければ WARNING を出す。残り（Main の Load、Hold した Task の再開）は次のプロセスが行う（0055 の 7。Hold した Task は Task の履歴から見つける）。その後で Loop を止める。
- 当初の案は「Loop を先に止め、開始を取りやめるだけ」だったが、Codex review（PR #168、P1）が、Unload の後に取りやめると、このプロセスでは Main も Task も戻らないまま終わると指摘した（再現した）。Main の Load は数分かかることがあり、既定の 5 秒で終わるとは限らない。終わらない分は次のプロセスに任せる。Unload の途中で取りやめた Model は `failed` になり、`failed_retry_seconds`（60 秒）後まで Load し直されないので、その場合も次のプロセスが戻す。
- `on` のまま終わると、Exclusive の Lease はプロセスとともに消える。次のプロセスの Scheduler は Probe で空きがあるときだけ Main を Load し、Hold した Task は Main が戻ってから再開する（0055 の 7、Approved。変えない）。`on` のときは終了時に Model を Load し直さない（Exclusive の仕事がまだ GPU を使っているかもしれない）。
- 起動時: 設定で `initial=gpu` の Main は、Scheduler の状態では最初から `gpu` になっている。前のプロセスが Main を GPU から外したまま終わっていても同じである（Codex review、PR #168 の 2 回目・3 回目、P1）。
  - **Full GPU Mode は、GPU にあるという肯定的な証拠を得てから Hold した Task を再開する**（`DeploymentStatus.observed_on_gpu` が `True`）。`True` になるのは、最後の読み取りで Main の Process が GPU に見えたとき、またはその後にこの Scheduler が Main を GPU に置いたときである。次のときは `False` で、再開しない: まだ読み取りがない、Process を尋ねられなかった（Command の失敗・不正な答え）、Process が GPU にない。`reload_seconds`（既定 900 秒）を過ぎると `needs_human` が立ち、WARNING が出る。この時間は新しいプロセスでも最初の tick から数える。Hold された Task が残っていなければ `needs_human` は立てずに `off` にする（#167 で main に入った実装。Codex review、PR #168 の 4 回目の P1 もこれで満たす）。Model の Control がない構成（`None`）だけは、Scheduler の状態どおりとする。読み取りが古くなっても、最後の読み取りの結果を使う。
  - **Scheduler は、状態が `gpu` なのに Runtime が「Process はない」と答えた Model を `unloaded` に改め、WARNING を出す**（`_reconcile`）。常駐させる Model（Main）は、VRAM が空いていれば次の `refresh()` で Load し直す。これは 0055 の 7（Approved: 次のプロセスは空きがあれば Main を Load する）のとおりである。Lease がある間と Drain の間は改めない（使っている仕事が先に終わる）。Process を尋ねられなかったときは、止まっているとも分からないので改めない。その場合 Main は戻らず、Task は Hold されたままで、`needs_human` になる。
- 代わりに、取りやめだけをして Load し直しと再開をすべて次のプロセスに任せる案（当初の案）、`on` のときも終了時に Load し直す案もある。

## 代替案

- **設定 File で有効にする**: 1 のとおり、Command を File から読む形式と保護を先に決める必要がある。
- **警告を Audit / Event Bus に書く**: 2 のとおり。
- **同期の `POST`**: 3 のとおり、10 分を超える Request になる。
- **開始の途中の `DELETE` を拒む**: 4 のとおり。
- **読み取り専用の Capability**: 5 のとおり。

## リスク

- 本番の起動（`python -m paw_backend`）は `compute` を渡さないので、この PR だけでは Scheduler は本番で動かない（1）。Runtime の Adapter の Issue で `ComputeSetup` を組み立てる。
- 警告はプロセス内だけにあり、再起動で消える（2）。Log には残る。
- `GET` を頻繁に呼ぶと `audit_events` が増える（5）。
- Background の開始が拒否された場合（開始の間に権限が外された、など）は Log に型の名前だけが残り、`GET` の `last_failure` は立たない（Exclusive の失敗だけが `last_failure` になる）。

## 決めてほしいこと

1. **Scheduler を有効にするのは `create_app(compute=ComputeSetup(...))` の注入だけにし、環境変数・設定 File は Runtime の Adapter の Issue で作る。Database がない構成では Scheduler だけ動かす**（1）でよいか。推奨: はい。この Issue で `PAW_COMPUTE_CONFIG_FILE` を作る案もある。
2. **`VramWarningSink` の実装は、最新 50 件をプロセス内に持って `GET` に出すだけで、DB・Audit・Event Bus に書かない（Log は Scheduler が出す）。System Health ができたらその Event に差し替える**（2）でよいか。推奨: はい。
3. **`POST` は開始を Background で始めて `202` を返し、結果は `GET` で読む**（3）でよいか。推奨: はい。同期で待たせる案もある。
4. **開始の途中の `DELETE` は開始を取りやめる（Scheduler は通常へ戻り、Hold した Task は再開する）**（4）でよいか。推奨: はい。`409` で拒む案もある。
5. **`GET` / `POST` / `DELETE` のすべてを `admin.compute.full_gpu` で認可し（`GET` も毎回 Audit）、開始と終了は `FullGpuMode` がもう一度判定する**（5）でよいか。推奨: はい。`GET` 用に読み取り専用の Capability を新しく置く案もある。
6. **応答の形は 6 のとおり（状態・GPU の概要・最新の警告。Task の ID・PID・Model の名前は出さない）、Scheduler か Database がなければ `503`（`compute_not_configured`）**（6）でよいか。推奨: はい。
7. **Local の Model で走る Runtime は `local_runtimes`（`LocalRuntime`）で渡し、組み立てが `HybridRuntime`（Cloud なし、遅い GPU 時間は同じ `BudgetTracker`）で包む。Memory Worker と Embedder は後の Issue**（7）でよいか。推奨: はい。
8. **終了時は、開始の途中なら取りやめる。Full GPU Mode が `resuming` なら、Loop を止める前に Unload した Model を Load し直し、Hold した Task を再開する（`shutdown_timeout_seconds` まで。残りは次のプロセス）。`on` のままならプロセスとともに終わる（Load し直しは次のプロセスの Scheduler）。起動時は、Main の Process が GPU に見えるまで Hold した Task を再開しない**（8）でよいか。推奨: はい。取りやめだけにして、戻すのはすべて次のプロセスに任せる案もある。

## 承認後の扱い

承認されたら `Approval` に記録し、Status を Approved に改める。数値（警告を持つ件数 50、`refresh_seconds` 5 秒）は `ComputeSetup` の引数で変えられる（DB に書いたものはない）。
方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。
[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。
