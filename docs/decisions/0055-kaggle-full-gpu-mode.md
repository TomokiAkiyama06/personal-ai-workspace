# Kaggle / Full GPU Mode の方針（誰が始められるか、走っている Task の止め方と Preemption、Queue 中の Task、戻すときの順、再起動のとき）

- Status: Approved
- Date: 2026-09-29
- Scope: PAW-037（[#33](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/33)）の `FullGpuMode`（`apps/backend/paw_backend/compute/full_gpu.py`）、`PostgresTaskHolds`（`compute/holds.py`）、Capability `admin.compute.full_gpu`、Scheduler の `ComputeRequest.task_id` / `gpu_task_ids()` / `revoke_local_gpu()`
- Supersedes: なし。[Decision 0037](0037-gpu-compute-scheduler.md)（Approved）は書き換えない。0037 の 7（Exclusive は走っている仕事を**止めずに**待つ。走っている Task の Safe pause / Drain は PAW-037、認可は PAW-037 で Owner / Admin に限る）と 13（Exclusive の Lease に期限を置かない）が PAW-037 に残した点を決める
- Approval: 2026-09-30、Humanが作業Session内で、判断点ごとの説明（推奨つき）を受けたうえで「推奨どおり」と回答して承認（1〜8 の全点。末尾の「承認時の決定」）

## 背景

[REQUIREMENTS.md](../../REQUIREMENTS.md) は次を FIXED にしている。

- 「Kaggle / 研究計算を GPU 利用の最優先とする」「Local AI は停止して VRAM を解放できる」「Local AI は余剰 GPU サービス」。
- Kaggle / Full GPU Mode の開始時: 1. 新規 Local GPU Task の受付停止、2. Queue 中の Local Task を `Waiting for Resource` へ、3. Running の Local Task を Safe pause / Drain、4. Memory Worker の Unload、5. Embedding / Reranker を Unload または CPU へ、6. Main LLM の Unload、7. VRAM 解放の確認、8. Exclusive Job の開始。終了後: 1. Main LLM の Reload、2. 必要な Support Model の Reload、3. Queued / Paused の Task の Resume。Task の state / branch / worktree / Pending Observation は保持する。
- Preemption（走っている Task を止めること）は Explicit Preempt・Stop Now・**Kaggle / Full GPU Mode**・Critical safety に限る。Priority と Preemption は別。
- Task の状態 `Paused` の例に「Kaggle Mode 等」がある。

4〜8 は Decision 0037 の 7 が Scheduler の Exclusive として実装済み（Probe は読み取りだけ、Model の操作は注入した `ModelControl` だけ）。**次のことは要件も 0037 も決めていない。** 誰が始められるか、走っている Task をどう止めるか（Pause か Waiting か）、Drain が終わらないときに Preempt するか、Queue 中の Task をどう扱うか（Task の状態遷移の表には Queued → Waiting がない）、戻すときにどこまで待ってから Task を再開するか、Backend が再起動したときの扱い、HTTP の API。
[AGENTS.md](../../AGENTS.md) の「仕様変更」に従い、実装はこれらを下の推奨で置き、この Decision で承認を求める。

**GPU の安全性（この Decision の対象外の、守るべき条件）**: Full GPU Mode は GPU を Scheduler を通してだけ扱う（0037 の読み取り専用の Probe と、注入した `ModelControl`）。Process に Signal を送らず、GPU の設定を変えない。Preempt も協調的で、Lease に `revoked` を立てるだけ（Local の呼び出しは Cancel され、Process は殺さない）。Test はすべて Fake を使う。

## 提案

### 1. 始める・終えるのは Owner / Admin だけ（新しい Capability `admin.compute.full_gpu`）

- `FullGpuMode.start(principal, ...)` と `end(principal)` は、`Authorizer` で `admin.compute.full_gpu`（`Scope.SYSTEM`、委任不可、Audit `REQUIRED`）を判定する。Owner と Admin に与え、User と Agent には与えない。判定は Audit に残り、Audit に書けなければ拒否する（Fail closed）。
- 理由: Full GPU Mode は**すべての User** の Local GPU の Task を止める Workspace 全体の操作で、0037 の 7 と 13 が「Owner / Admin に限る」と推奨していた。既存の `admin.config.manage` に含めると、設定の変更と「他の User の仕事を止める」ことを分けて Audit できない。
- Passkey の Step-up は求めない（推奨）。Full GPU Mode はデータを消さず、終えれば元に戻る。代わりに求める案もある（Decision 0049 の手動解除と同じ扱い）。

### 2. 走っている Task は Pause ではなく `waiting`（Resource）にして止める（Graceful: Drain）

- Local GPU の Lease を持つ、または待つ仕事の Task（`ComputeRequest.task_id` で分かる。`HybridRuntime` が Node の Task を渡す）を、Policy の Actor で `wait`（`wait_reason = resource`、理由は固定の `Kaggle / Full GPU Mode`）にする。これを「Hold」と呼ぶ。
- `waiting` は Orchestrator にとって Pause と同じ Graceful な停止で、新しい Node を始めず、走っている Node は終わるまで走らせる（**Drain**）。Task の状態・Branch・Worktree・DAG・結果はそのまま残る。
- 理由: 要件の開始手順の 2 が「`Waiting for Resource` へ」と書いている。Pause（Operator の操作）にすると、人が Pause した Task と区別できず、終了後に**人が止めた Task まで再開する**おそれがあり、人が途中で Resume すると Node が GPU を待って失敗する。`unblock` は人の操作（Control Command）ではないので、Hold した Task を人が途中で再開することもない（Cancel / Stop Now はいつでもできる）。
- Hold する Task の見分けは Task の履歴（最後の `wait` の Event が Policy の Actor で固定の理由）から行う。専用の Table は作らない（Migration なし）。
- 代わりに `Paused` を使う案もある（要件の状態の説明の例）。その場合も終了後に再開するのは Full GPU Mode が止めた Task だけにする。

### 3. Queue 中の Task は Queue に置いたままにし、GPU を求めた時点で Hold する

- Task の状態遷移（PAW-032、Decision 0007）には Queued → Waiting がなく、Queue 中の Task が Local GPU を使うかどうかは、走り始めて Node が Local の Model を求めるまで分からない（Cloud や CPU で走る Node もある）。
- そこで Queue 中の Task は触らず、Full GPU Mode の間に Worker が取り出して Node が Local GPU を求めると（Scheduler は `exclusive_mode` で断り、その Request は待つ）、Full GPU Mode の定期の確認（`tick()`、既定 5 秒）がその Task を Hold する。結果として「GPU を使う Queue 中の Task は `Waiting for Resource` になる」。
- 代わりに、状態遷移の表に Queued → Waiting を加えて Queue 中の Task をすべて Hold する案もある。PAW-032 の表を変え、GPU を使わない Task まで止める。

### 4. Drain の待ちは既定 600 秒、Preempt は始める人が明示したときだけ

- 既定では走っている Local GPU の仕事の終わりを `drain_seconds`（既定 **600 秒**、暫定値。Node が Local で待てる上限と同じ）まで待つ。終わらなければ Full GPU Mode は始まらず（`DRAIN_TIMEOUT`）、何も Unload せずに通常へ戻り、Hold した Task は再開する。
- `start(..., preempt=True)` を明示したときは、Drain の時間が過ぎた時点で残りの Local GPU の Lease に `revoked` を立てる（`revoke_local_gpu()`）。`HybridRuntime` は Local の呼び出しを Cancel して Node を `ComputeUnavailable`（Retry 可）で終える。さらに `preempt_seconds`（既定 **60 秒**）待っても Lease が返らなければ `DRAIN_TIMEOUT` で諦める（止まらない呼び出しの GPU を奪わない。0037 と同じ）。
- 理由: 要件は Full GPU Mode の Preemption を**許す**が（「安全停止 / drain / unload を試みる」）、止めた Node は途中の生成を失い、Retry を 1 回使う。既定は失うもののない Drain にし、失ってでも GPU が要るかは始める人が決める。
- 代わりに、既定で Preempt する案（Kaggle を最優先にする要件に近い）と、Preempt を作らない案がある。

### 5. 求める VRAM の既定は「GPU から Safety Headroom と他の Workload の分を除いたすべて」

- `start` に `vram_bytes` を渡さなければ、最新の読み取りの `total − headroom − external`（Workspace が空けられる分のすべて）を Exclusive の Lease で求める。渡せばその量。
- 他の User の Workload が持つ Memory は Workspace が空けられないので、全体を求めると必ず `NOT_FREED` になる。Headroom は 0037 の 3 のとおり残す。

### 6. 終えるときは Main LLM が GPU に戻ってから Task を再開する

- `end` は Exclusive の Lease を返す。Scheduler の `refresh()` が Main → Memory Worker → Embedding / Reranker の順に Load し直す（0037 の 7）。
- `tick()` は Main LLM（`ModelRole.MAIN` の Deployment すべて）が GPU に戻ったのを見てから、Hold した Task を再開する（Policy の Actor で `unblock`、同じ Transaction で最後の Queue Entry と同じ Priority の Entry を足す。まだ Worker が Entry を持っていれば足さない）。Support Model の Load は待たない（Memory Worker の Job は延期されて後で走る。Decision 0018）。
- `reload_seconds`（既定 **900 秒**、暫定値）を過ぎても Main が戻らなければ、Task は待たせたまま `needs_human` を立てて Log に残す（戻ればその時点で再開する）。
- 理由: Main がないまま再開すると、Node は GPU を待って `ComputeUnavailable` で失敗し、Retry を使う。
- 代わりに、終えた時点で直ちに再開する案もある。

### 7. Full GPU Mode の状態はプロセス内に持ち、再起動で解除される。Hold した Task は次のプロセスが再開する

- 0037 の 1（Scheduler の状態はプロセス内）に合わせ、Full GPU Mode もプロセス内に持つ（Migration なし）。Backend が再起動すると Full GPU Mode は解除され、Scheduler は Main を Load しようとする（Probe で空きがあるときだけ。Kaggle の Job が VRAM を持っていれば Load しない）。
- Hold した Task は履歴から見つかるので、新しいプロセスの `FullGpuMode` が最初の `tick()` から、Main が GPU にあれば再開する。
- 再起動の後も Kaggle の Job が続くなら、Owner / Admin がもう一度 `start` する。
- 代わりに、Full GPU Mode の状態を DB に持ち、再起動の後も続ける案もある（Migration が要る。0037 の 1 の範囲を越える）。

### 8. 認可はこの Service で行い、HTTP の API はまだ作らない

- 0037 の 7 は「認可ができるまで Exclusive を API で公開しない」とした。認可は 1 のとおり `FullGpuMode` 自身が行う。
- ただし Scheduler はまだ Application の Lifespan に組み込まれていない（0037 の README の「Application への組み込み」は未実装）。HTTP の経路（例: `POST` / `DELETE /api/v1/admin/compute/full-gpu`、`GET` で状態）と UI（`docs/UI_DESIGN.md` の Full GPU Mode）は、Scheduler を Application に組み込む Issue で、この Service の上に作る（推奨）。
- 代わりに、今 HTTP の経路を作り、Scheduler が無い構成では 503 を返す案もある。

## 代替案

- **Pause で止める**: 2 のとおり、人が止めた Task と区別できない。
- **Queue 中の Task をすべて Waiting にする**: 3 のとおり、状態遷移の表を変え、GPU を使わない Task も止める。
- **既定で Preempt する / Preempt を作らない**: 4 のとおり。
- **終えてすぐ再開する**: 6 のとおり、Main が戻る前に Node が失敗して Retry を使う。
- **専用の Table に Hold を記録する**: 再起動に強いが、Task の履歴で足りる（Migration を増やさない）。

## リスク

- Hold した Task の Node が Local GPU を待っている間（`HybridRuntime` の既定 600 秒）に Full GPU Mode が終わらなければ、その Node は `ComputeUnavailable`（Retry 可）で終わり、その Node の Retry を 1 回使う。Preempt した Node も同じ。
- Preempt は協調的で、Cancel を無視する Runtime は Lease を持ったままになり、Full GPU Mode は始まらない（`DRAIN_TIMEOUT`）。
- 再起動の後、Kaggle の Job が VRAM を少ししか使っていなければ、Scheduler が Main を Load し直し、Hold した Task も再開する（7。0037 の 1 と同じ性質）。
- Main が戻らない限り Hold した Task は待ち続ける（6。`needs_human` で知らせる）。Main を Load できない構成（`ModelControl` なし）では、そもそも Full GPU Mode が始まらない（0037 の 7）。
- 数値（Drain 600 秒、Preempt の猶予 60 秒、Reload 900 秒、確認の間隔 5 秒）は実測に基づかない暫定値。
- `NvidiaSmiProbe` の読み取りが、`nvidia-smi` の出力が複数回に分かれて届くと最初の断片しか読まず、Probe が使えない（`probe_unavailable`）と判断していた（0037 の実装の不具合。このサーバーの実 GPU で再現）。VRAM 解放の確認（開始手順の 7）がこれに依存するため、この PR で出力の最後まで読むように直した。

## 決めてほしいこと

1. **始める・終えるのは Owner / Admin だけとし、新しい Capability `admin.compute.full_gpu`（System、委任不可、Audit 必須）を置く。Passkey の Step-up は求めない**（1）でよいか。推奨: はい。Step-up を求める案もある。
2. **走っている Task は Pause ではなく `waiting`（Resource、Policy の Actor、固定の理由）にして Drain し、終了後に再開するのは Full GPU Mode が Hold した Task だけ**（2）でよいか。推奨: はい。`Paused` を使う案もある。
3. **Queue 中の Task は Queue に置いたままにし、Node が Local GPU を求めた時点で Hold する**（3）でよいか。推奨: はい。状態遷移に Queued → Waiting を加えてすべて Hold する案もある。
4. **既定は Drain だけ（600 秒）で、終わらなければ始めずに元へ戻す。Preempt（Lease の `revoked`、協調的）は始める人が `preempt=True` を明示したときだけ**（4）でよいか。推奨: はい。既定で Preempt する案、Preempt を作らない案もある。
5. **求める VRAM の既定を「GPU − Safety Headroom − 他の Workload の使用量」とする**（5）でよいか。推奨: はい。
6. **終えた後、Main LLM が GPU に戻ってから Task を再開し（Support Model は待たない）、900 秒戻らなければ `needs_human` で知らせて待たせ続ける**（6）でよいか。推奨: はい。直ちに再開する案もある。
7. **Full GPU Mode はプロセス内に持ち、再起動で解除される（Hold した Task は次のプロセスが Main の復帰後に再開する）**（7）でよいか。推奨: はい。DB に持って再起動の後も続ける案もある。
8. **HTTP の API と UI は、Scheduler を Application に組み込む Issue でこの Service の上に作る**（8）でよいか。推奨: はい。今 API を作る案もある。

## 承認後の扱い

承認されたら `Approval` に記録し、Status を Approved に改める。数値は `apps/backend/paw_backend/compute/limits.py` と `FullGpuMode` の引数で変えられる（DB に書いたものはない）。
方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。
[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。

## 承認時の決定（2026-09-30）

Human は、作業 Session で判断が必要な点について推奨つきの説明を受け、「推奨どおり」と回答して承認した（1〜8 の全点）。**すべて推奨どおり**で、個別の変更はない。
