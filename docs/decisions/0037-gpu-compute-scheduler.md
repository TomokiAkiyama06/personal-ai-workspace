# GPU / Compute Resource Scheduler の方針（Admission の単位、VRAM の勘定と Safety Headroom、Class の優先、縮退の段と復帰、Exclusive、Local / Cloud の振り分け）

- Status: Approved
- Date: 2026-09-28
- Scope: PAW-036（[#32](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/32)）の Compute Resource Scheduler（`apps/backend/paw_backend/compute/`、`paw_backend/cli/compute.py`）と、それを使う PAW-037（Kaggle / Full GPU Mode）・PAW-066（System Health）
- Supersedes: なし。[Decision 0021](0021-dag-orchestrator-policy.md) の `max_parallel_nodes`（静的な上限）は変えない。0021 が「PAW-036 の担当」とした動的な並列数を、上限の内側で決める
- Approval: 2026-09-28、Human が作業 Session 内で、判断点ごとの説明（推奨つき）を受けたうえで「推奨どおり」と回答して 14 点すべてを承認。14 は推奨どおり、Placement を Audit に残す別の Issue が済むまで `CloudPolicy` を注入しない（末尾の「承認時の決定」）

## 背景

[REQUIREMENTS.md](../../REQUIREMENTS.md) の「GPU / Compute Resource Scheduler」（FIXED）は次を定めている。

- Agent 数と GPU 上の Model instance 数を分け、複数の Local Agent は共有の Main LLM Runtime へ Request を投げる。
- Resource class は `Interactive` / `Coding` / `Support` / `Background` / `Exclusive` の 5 つで、Interactive / Main Coding を Background より優先する。
- Task の開始可否と同時実行数は、VRAM・Reserved VRAM・Safety Headroom・GPU 利用率・KV Cache・Context 長・Kaggle / Full GPU Mode などから**動的に**決め、並列 Agent 数を固定値にしない。
- VRAM の判断は Model Weight だけで行わず、KV Cache・CUDA Graph / Runtime Buffer・Temporary Workspace・Memory Worker・Embedding / Reranker・Safety Reserve を含める。**Safety Headroom の GB / % は Benchmark 後に決める。**
- 常駐方針（Main Coding Model は原則常駐、Memory Worker は VRAM に余裕があれば常駐、Embedding / Reranker は軽量なら常駐・必要なら CPU fallback）と、VRAM pressure 時の縮退順（1. Background GPU Job 停止、2. Memory Worker unload、3. Embedding / Reranker を CPU へ、4. 新規 Local Request の Admission 抑制、5. KV Cache / Context Policy の調整、6. 必要なら Main Model 構成の変更）。
- Local GPU が混雑しているとき、Task の依存・Permission・Quota を満たす範囲で Codex / Claude へ一部 Subtask を割り当てられる。
- MIG は V1 では使わない。Priority は新規 Task の開始順だけに効き、実行中の Task を止めない（Preemption は Explicit Preempt・Stop Now・Kaggle / Full GPU Mode・Critical safety に限る）。

一方で、**次のことは要件も Backlog（PAW-036 の受け入れ条件は 5 項目）も決めていない。** Scheduler の状態をどこに持つか、Headroom などの数値、Class の間の余裕の取り方と待ち行列の順、縮退を進める・戻す条件、Main Model の構成変更を自動で行うか、Exclusive の手順と失敗時の扱い、Cloud へ回す条件と時期、Context の見積もり、GPU 時間の Budget への計上、Probe が使えないときの扱い。
[AGENTS.md](../../AGENTS.md) の「仕様変更」に従い、実装はこれらを下の推奨で置き、この Decision で承認を求める。

**GPU の安全性（この Decision の対象外の、守るべき条件）**: Scheduler は GPU を読むだけ（`nvidia-smi --query-gpu` と `--query-compute-apps` の 2 つの Command。Test が固定する）。Clock・Persistence・Power limit・Compute mode・MIG を変えず、GPU を Reset せず、Process に Signal を送らない。Model の Load / Unload / CPU fallback は注入された `ModelControl` を通してだけ行い、Test は Fake を使う（実際の Model を Load / Unload する Test はない）。実 GPU を読む Test は 1 つだけで、`PAW_TEST_REAL_GPU_PROBE=1` のときだけ動き、CI では Skip する。

## 提案

### 1. 状態はプロセス内に持ち、DB を使わない（Migration なし）

- `ComputeScheduler` は Backend の 1 プロセスに 1 つ置き、Lease・待ち行列・常駐状態をメモリに持つ。Migration は足さない。
- 理由: Lease の寿命は推論の 1 回（秒〜分）で、プロセスが落ちれば推論も止まる。永続化すると、落ちたプロセスの Lease を回収する仕組み（Queue の Lease と同じ Heartbeat）が要るが、要件にない。
- 制約: Orchestrator の Worker を**別プロセスで複数**動かすと、各プロセスが自分の Scheduler を持ち、同じ GPU を二重に数える。V1 では Scheduler を使う Worker を 1 プロセスに集める（Orchestrator の Worker は同じプロセスの中で並列に `serve` できる）。複数プロセスが要るときは、DB の Lease を持つ新しい Decision で扱う。

### 2. Admission の単位は「Model の KV Cache の Token」とする（動的な並列数）

- Local Agent は Model ごとに 1 つの Runtime を共有する（`DeploymentSpec`）。Runtime は Load 時に KV Cache の Pool を確保するので、同時に走れる数を決めるのは Agent の数ではなく、各 Request が要る Context（Prompt + 回答）の Token である。
- 各 Request は `context_tokens` を Pool から予約する。Pool の `kv_safety`（**推奨 90%**）までしか予約できない。Runtime が実際の KV 使用率を報告できるときは、予約と観測の大きい方を使う。Runtime の `max_sequences` も上限にする。
- その結果、長い Context の Task が多いと同時数が減り、短いものが多いと増える（`ComputeScheduler.parallelism()` が今の数を返す）。Orchestrator の `max_parallel_nodes`（Decision 0021、既定 4）は上限のまま残り、実際の同時数はその内側で Scheduler が決める。

### 3. VRAM は Actual と Reserved を分けて勘定し、Safety Headroom を残す

- **Actual**: Probe が見る使用量（Workspace の Process も、他の User の Workload も）。**Reserved**: Scheduler が約束した量（GPU に置いた Model の Footprint 全体。Load 中の Model は Memory が見える前から予約する。Exclusive の予約も含む）。
- Footprint は `weights + KV Cache Pool + Runtime Buffer（CUDA Graph など） + Temporary Workspace` の和で、Benchmark と Runtime の設定から Admin が与える（Scheduler は測らない）。
- `committed` = 各 Model の max(予約, その Process の実使用) + Workspace のものでない使用（`external`: 他の Workload、Unload したのに残った Memory）。Process が分からない Model は予約分を使っているとみなす。`available = total − headroom − committed`。0 未満が VRAM pressure。
- **Model の Process の意味**: `ModelControl.processes()`（`CommandModelControl` の `pids` Command）は、その Runtime の**全 Process**（systemd の Unit なら cgroup の `cgroup.procs`）を返すことを必須とする。vLLM / SGLang は GPU Memory を `MainPID` の子（EngineCore / TP Worker）が持つので、`MainPID` だけだと Model の Memory が `external` にも数えられ（二重計上）、存在しない Pressure で縮退が 6 段目まで進む。安全網として、返した Process が GPU に何も持たない Model は「Process が分からない」と同じ扱い（予約分を Probe が見る使用のうちに持つ）にする（Load 直後の Model もこれに当たる）。一部の Process だけを返す構成は他の Workload と区別できないので、構成で防ぐ（README の例は `cgroup.procs` を読む）。
- **Safety Headroom は「4 GiB と GPU の 5% の大きい方」を暫定値とする**（96 GB の GPU で約 4.8 GiB）。要件どおり Benchmark 後に見直す値で、`ComputeConfig` の設定で変えられる（DB に書かない）。
- Model を戻す・`IF_ROOM` の Model を Load するときは、Load 後にもう 1 つ Headroom 分（`restore_margin_bytes`、既定は Headroom と同じ）が残ることを条件にする（Pressure と復帰を往復しないため）。`ALWAYS` の Model（Main）は Headroom が残れば Load する。

### 4. Class の優先: 取り分の上限と、Class 順・到着順の待ち行列

- 各 Class が KV Pool（の `kv_safety` 分）を使える割合の上限を置く: **Interactive 100%、Coding 95%、Support 85%、Background 70%**（暫定値）。Background が Pool を埋めても、Interactive / Coding の分が残る。
- 入れない Request は待ち行列に入り、Class 順（Interactive → Coding → Support → Background）、同じ Class の中は到着順に通す。**容量不足で待つ Request は、同じ Model の後ろの Request を追い越させない**（小さい Request が続いて大きい Request が永久に待つことを防ぐ）。新しく来た Request も、同じ Model で同じか上の Class の待ちがあれば後ろに並ぶ。
- Priority は開始順だけに効く。高い Class が来ても走っている Request は止めない（要件の「PriorityとPreemptionは分離」）。

### 5. 縮退は要件の順に、1 回の読み取りで 1 段ずつ進め、余裕が戻ったら逆順に戻す

- `refresh()`（既定 5 秒ごと）ごとに Probe を読み、Pressure なら次の 1 段を行う。1 段ずつにするのは、Unload の効果を Probe で確かめてから次へ進むため。Model の操作（Load は最長 300 秒）の間も Probe を読み続け（Probe の最大の古さの 1/3、既定 5 秒ごと）、待ちの Request を通す。操作中の Model は何も受け付けないが、GPU にある Main の Interactive / Coding は止めない（止めると 15 秒で `probe_unavailable` になり、Cloud が許された Node が Load の間ずっと Cloud へ回る）。操作は同時に 1 つのまま。
  1. Background: 新しい Background の Admission を止め、GPU 上の Background の Lease に `revoked` を立てる（**Process は殺さない**。持ち主が止めて Release する）。
  2. Memory Worker: 新しい仕事を止め（Drain）、走っている仕事が終わってから Unload する。Journal は Worker が使えない間 `WorkerUnavailableError` を受け取り、失敗に数えずに延期する（Decision 0018）。
  3. Embedding / Reranker: CPU の Copy があれば CPU へ移し、なければ `IF_ROOM` のものを Unload する（`ALWAYS` で CPU の Copy がないものは残す）。
  4. 新規 Local Request の Admission を抑制する（Interactive だけは通す）。
  5. 新しい Request の Context を Model の最大の **50%** までに下げる（暫定値）。
  6. **Main Model の構成変更は自動で行わない。** Status の `needs_human` を立てて Log に残し、人が決める（Main Coding workload を保護する要件のため、Scheduler が Main を Unload・変更しない）。
- Pressure が消え、Headroom の外に `restore_margin_bytes` 以上の余裕があれば、逆順に 1 段ずつ戻す（6 → 5 → 4 → 3（Embedding / Reranker を GPU へ）→ 2（Memory Worker を Load）→ 1 → 0）。戻す Model が余裕に収まらなければ、その段にとどまる。
- Model の操作が失敗した Deployment は `FAILED`（Memory を持ったままとみなす）とし、Log に型の名前だけを残して、**60 秒**後（暫定値）にもう一度試す。縮退の段は先へ進む。
- `ModelControl` が無い構成では Scheduler は観察と Admission だけを行い（2・3 の段は飛ばす）、Model を Load / Unload しない。

### 6. Probe が使えないときは、新しい Local GPU の仕事を入れない（Fail closed）

- Probe が失敗した、または最後の読み取りが **15 秒**（暫定値）より古いときは、Local GPU の Admission を `probe_unavailable` で断り（Cloud が許されていれば Cloud へ）、Model の操作もしない。CPU に置いた Model の仕事は続ける。
- 走っている仕事は止めない。

### 7. Exclusive: 新しい仕事を止め、走っている仕事の終わりを待ち、全部 Unload し、Probe で空いたことを確かめてから渡す

- 手順: 新しい Local GPU の Admission を止める → 走っている Local GPU の仕事が終わるのを待つ（呼び出し側の `wait_seconds` まで。**止めない**: 走っている Task の Safe pause / Drain は PAW-037）→ Memory Worker、Embedding / Reranker（CPU の Copy があれば CPU へ）、Main の順に Unload → Probe で、Workspace の Process が GPU に 1 つもなく、要求した VRAM が空いていることを確かめる（**最長 60 秒**、2 秒ごと。暫定値）→ Lease を渡す。
- 始める前に Probe が読めなければ、何も Unload せずに断る。どこかで失敗したら（Drain の時間切れ、Unload の失敗、空かない）通常の状態に戻して断り、Unload 済みの Model は次の `refresh()` が戻す。
- Exclusive は同時に 1 つ。Lease を返すと通常に戻り、`refresh()` が Main → Memory Worker → Embedding / Reranker の順に Load し直し、待っていた Request が再開する。
- **誰が Exclusive を求められるか（認可）は Scheduler の外**で、API を作る Issue（PAW-037）で決める。推奨は Owner / Admin だけ。その認可ができるまで、Exclusive を API で公開しない。
- **Exclusive の Lease に期限を置かない**。自動で期限切れにすると、まだ走っている Job（Kaggle、Benchmark）の下で Main を Load し直し、Job か Main が OOM になりうる。代わりに `status().exclusive_age_seconds` で保持時間を見せ、持ち主が Release しないまま失われた Lease は管理操作 `force_release_exclusive()`（Lease に `revoked` を立てて Release し、通常に戻す。次の `refresh()` は Probe が空いていると見る分だけ Load する）で終わらせる。これも PAW-037 の API で Owner / Admin に限る。

### 8. Local / Cloud の振り分け: 注入した Policy が許すときだけ、Local が混んでいたら直ちに Cloud へ

- Orchestrator の Runtime として使う `HybridRuntime` が、Node ごとに Scheduler の Lease を取ってから Local の Model で走らせる。Local に入れないとき、**注入した `CloudPolicy` がその Node に許せば** Cloud の Runtime（Codex / Claude）で走らせる。Policy が無ければ Cloud へは回さない。
- Policy は「Task の依存・Permission・Quota を満たす範囲」を判断する（Node の内容を外へ送ってよいか、User の Quota、Node の依存）。Scheduler はそれを知らないので判断しない。Quota の消費そのものは Connection（PAW-030、Decision 0016）が記録する。
- 既定では**入れないと分かった時点で Cloud へ回す**（`cloud_after_seconds = 0`）。Local を少し待ってから回すことも設定できる。Cloud へ回した Node は、Orchestrator の記録上は Ladder の Label（Local の Agent）のままになる（Placement は Scheduler の Status と Log に出る）。これは Node の内容の外部送信と Task の Audit（AGENTS.md 13 の agent / model）の正確さに関わるので、決めてほしいことの 14 で扱いを尋ねる。推奨は、Orchestrator の Node の記録に実際の Placement（`local_gpu` / `local_cpu` / `cloud` と Cloud の Agent）を残すことを、Orchestrator の変更（Migration を伴う）として別の Issue で行い、それまでは `CloudPolicy` を注入しない（Cloud へ回さない）こと。
- Local で待つ場合の上限は既定 **600 秒**（暫定値）で、超えたら Node は `ComputeUnavailable`（Retry 可）で失敗し、Orchestrator の Retry に従う。

### 9. Context の見積もりと GPU 時間の計上

- Node の Context は、Title・Goal・Input・上流の結果の Byte 数 ÷ 3 に、回答の予備 **8,192 Token** を足して見積もる（暫定値。日本語と Code の混在で多めに出る比率）。Runtime が正確に数えられるなら、自分の `estimate` を渡す。
- `HybridRuntime` は Local の Lease を持っていた時間（秒、切り上げ）を Task の Budget の `GPU_SECONDS` に計上する（Budget の「max GPU time」を実際に効かせるため）。Local の Runtime が例外を投げた・Cancel されたときも計上する（失敗して Retry される Node が Budget を素通りしないため）。そのとき計上が `NodeStopped` などで失敗しても、伝えるのは Runtime の例外。Cloud で走った Node は計上しない。Runtime 自身は `GPU_SECONDS` を二重に計上しない。

### 10. 対象は 1 枚の GPU、MIG は使わない

- `ComputeConfig.gpu_index`（既定 0）の 1 枚を管理する。複数 GPU の割り当ては V1 の対象外。MIG は要件どおり使わない。

### 11. 確認用の Command は読み取りだけで、他の User の Process を表示しない

- `python -m paw_backend.cli compute-status` は同じ Probe で GPU を読み、Total・Used・Headroom・Available・利用率・Memory を持つ Process の**数**を JSON で出す。PID や Process 名は出さない（他の User の Process でありうるため）。DB に接続せず、何も変えない。

## 代替案

- **状態を DB に持つ**: 複数プロセスで GPU を共有できるが、Lease の回収（Heartbeat）が要り、推論 1 回ごとに DB へ書く。V1 は 1 プロセスで足りるため採らない（1 の制約として残す）。
- **VRAM を Model の Weight と固定の余白だけで判断する**: 要件（Weight だけで判断しない）に反する。
- **並列数を Model ごとの固定値にする**: 要件（固定値にしない）に反する。Context が長い Task が並ぶと KV Cache が溢れる。
- **縮退を Pressure を見た時点で一気に進める**: Unload の効果が Probe に出る前に次を行い、必要以上に Model を降ろす。
- **Main Model の構成変更（量子化・Context 長の変更、別の Model への切り替え）を自動で行う**: Main Coding workload の保護に反し、品質が黙って変わる。
- **Probe が使えないときも Admission を続ける**: 他の Workload が GPU を使っていても気づけず、OOM で推論が落ちる。
- **Exclusive で走っている Task を止める**: 要件は Kaggle / Full GPU Mode の Preemption を許すが、Safe pause / Drain の実装は PAW-037。ここでは待つだけにする。
- **Cloud を待ち行列の最後の手段にする（Local を一定時間待ってから Cloud）**: Quota の消費は減るが、Coding の Node が待つ。既定は直ちに、設定で待てるようにした。

## リスク

- 数値（Headroom 4 GiB / 5%、KV の 90%、Class の 100 / 95 / 85 / 70%、Context の 50%、Probe の 15 秒、再試行の 60 秒、Exclusive の確認 60 秒、待ちの 600 秒、見積もりの 3 Byte / Token と 8,192 Token）は**実測に基づかない暫定値**である。Benchmark（PAW-017 / PAW-019）で Model と Runtime が決まったら見直す。
- Footprint を Admin が過小に与えると、予約より実使用が多くなる。`committed` は実使用の方を数えるので Admission は安全側に寄るが、Load の判断（予約で数える）は誤る。
- Model の Process が分からない構成（`ModelControl` なし）では、予約分を使っているとみなすので、他の Workload との区別が粗い。
- `revoked` は協調的で、Background の仕事が無視すると VRAM は戻らない（縮退はそのまま次の段へ進む）。
- 1 の制約（複数プロセスで二重に数える）は構成で守るしかない。
- Cloud へ回した Node が Orchestrator の記録上 Local の Label のままになるため、Audit から Placement を追うには Scheduler の Log が要る。

## 決めてほしいこと

1. **状態をプロセス内に持ち、Migration を足さない**（1）。Scheduler を使う Worker を 1 プロセスに集める制約を受け入れるか。推奨: はい。
2. **Admission の単位を KV Cache の Token にし、`kv_safety` 90%**（2）でよいか。推奨: はい。
3. **Safety Headroom の暫定値「4 GiB と 5% の大きい方」と、戻すときの Margin（既定は Headroom と同じ）**（3）でよいか。推奨: はい（Benchmark 後に見直す）。
4. **Class の上限（100 / 95 / 85 / 70%）と、Class 順・到着順で追い越させない待ち行列**（4）でよいか。推奨: はい。
5. **縮退を 1 回の読み取りで 1 段ずつ進め、戻すときは逆順・Margin つき**（5）でよいか。Background の停止は協調的（`revoked`）で、Process を殺さない。推奨: はい。
6. **6 段目（Main Model の構成変更）は自動で行わず、`needs_human` で人に知らせる**（5）でよいか。推奨: はい。
7. **Probe が使えない・古いときは Local GPU の Admission を止める（Fail closed、15 秒）**（6）でよいか。推奨: はい。
8. **Exclusive は走っている仕事を止めずに待ち、全 Unload の後に Probe で確かめてから渡す。失敗したら通常へ戻す。認可は PAW-037 で Owner / Admin に限る**（7）でよいか。推奨: はい。
9. **Cloud へは注入した `CloudPolicy` が許すときだけ、既定は直ちに回す**（8）でよいか。推奨: はい。Local を待ってから回す既定にする案（`cloud_after_seconds` > 0）もある。
10. **Context の見積もり（Byte ÷ 3 + 8,192）と、Local の Lease の時間を `GPU_SECONDS` に計上する**（9）でよいか。推奨: はい。
11. **1 枚の GPU だけを管理し、確認用 Command は Process の数だけを出す**（10・11）でよいか。推奨: はい。`nvidia-smi` は PATH から探さず絶対 Path（既定 `/usr/bin/nvidia-smi`）で実行する。
12. **`ModelControl.processes()` は Runtime の全 Process（Unit の `cgroup.procs`）を返すことを必須にし、返した Process が GPU に何も持たない Model は Process が分からない Model として数える**（3）でよいか。推奨: はい。代わりに、Workspace の Model の予約の余り（予約 − 実使用）を超える分だけを `external` に数える案もあるが、本当の他の Workload を予約の余りに吸収して過小に数えるので採らない。
13. **Exclusive の Lease に期限を置かず、保持時間の表示と管理操作の強制 Release で扱う**（7）でよいか。推奨: はい。最長時間を過ぎたら自動で Release する案は、走っている Job の下で Model を Load し直すので採らない。
14. **Cloud へ回した Node の Placement を Orchestrator の記録（Audit）に残すか**（8）。推奨: 残す（Orchestrator の変更として別の Issue）。それまでは `CloudPolicy` を注入しない運用とする。代わりに、Scheduler の Log だけで追う現状を受け入れる案もある。

## 承認後の扱い

承認されたら `Approval` に記録し、Status を Approved に改める。数値は `apps/backend/paw_backend/compute/limits.py` と `ComputeConfig` の設定で変えられる（DB に書いたものはない）。
方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。
[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。

## 承認時の決定（2026-09-28）

Human は、作業 Session で上の 14 点について推奨つきの説明を受け、「推奨どおり」と回答して承認した（14 点を一括で。個別の変更はない）。**14 点すべてが推奨どおり**である。

1. 状態はプロセス内に持ち、Migration を足さない。Scheduler を使う Worker を 1 プロセスに集める制約を受け入れる。
2. Admission の単位は KV Cache の Token、`kv_safety` は 90%。
3. Safety Headroom の暫定値は「4 GiB と 5% の大きい方」、戻すときの Margin の既定は Headroom と同じ（Benchmark 後に見直す）。
4. Class の上限は 100 / 95 / 85 / 70%、待ち行列は Class 順・到着順で追い越させない。
5. 縮退は 1 回の読み取りで 1 段ずつ進め、戻すときは逆順・Margin つき。Background の停止は協調的（`revoked`）で、Process を殺さない。
6. 6 段目（Main Model の構成変更）は自動で行わず、`needs_human` で人に知らせる。
7. Probe が使えない・古い（15 秒）ときは Local GPU の Admission を止める（Fail closed）。
8. Exclusive は走っている仕事を止めずに待ち、全 Unload の後に Probe で確かめてから渡し、失敗したら通常へ戻す。認可は PAW-037 で Owner / Admin に限る。
9. Cloud へは注入した `CloudPolicy` が許すときだけ回し、既定は直ちに回す（`cloud_after_seconds` = 0）。
10. Context の見積もりは Byte ÷ 3 + 8,192、Local の Lease の時間を `GPU_SECONDS` に計上する。
11. 1 枚の GPU だけを管理し、確認用 Command は Process の数だけを出す。`nvidia-smi` は絶対 Path（既定 `/usr/bin/nvidia-smi`）で実行する。
12. `ModelControl.processes()` は Runtime の全 Process（Unit の `cgroup.procs`）を返すことを必須にし、返した Process が GPU に何も持たない Model は Process が分からない Model として数える。
13. Exclusive の Lease に期限を置かず、保持時間の表示と管理操作の強制 Release で扱う。
14. Cloud へ回した Node の Placement は Orchestrator の記録（Audit）に残す（Orchestrator の変更として別の Issue）。**その Issue で Placement が Audit に記録されるまでは `CloudPolicy` を注入しない**（どの Node も Cloud へ回さない）運用とする。

**承認後の補足（2026-09-28）。** 承認後、Codex Review（838d842）の指摘を反映した変更は、承認した方針を**厳しい方向にだけ**変えている。10 の計上に加えて、Task の `GPU_SECONDS` が残っていなければ Local で始めず、同じ Task の Local の呼び出しが合わせて残りの時間を使い切ったら止める（どちらも Budget の `NodeStopped`。Cancel しても止まらない呼び出しは、その待ちがもう一度 Cancel されても、止まるまで Lease を返さず、止まるまでの GPU 時間も計上する。Node の Attempt が閉じた後の分は、注入した `TrackerLateGpuCharge` で Task の Budget へ Fence なしに計上する）。Background の Lease が `revoked` になった Node の Local の呼び出しは止める（5 の協調的な停止を Orchestrator の Node と Memory Worker の Job にも及ぼす。Memory Worker の Job は `WorkerUnavailableError` で延期する。CPU へ移す・Unload する Embedding / Reranker の GPU の呼び出しも `revoked` で止め、Retrieval は縮退する）。このため Exclusive の Drain は Background の Lease にも `revoked` を立てず、走っている仕事の終わりを待つだけにする（7 の「止めない」のとおり）。`CloudPolicy` は Cloud で走らせる直前にも尋ね、答えられなければ Local に留める（9 の狭め）。Exclusive の Job が使う VRAM はその予約で吸収し、`external` として二重に数えない（3 の勘定の補正。吸収するのは Lease を与えた時点の `external` からの増分だけで、その前からあった他の Workload の VRAM は `external` のまま数える。使用量が `[N/A]` の Process を持つ Model も、予約の残りでその分を吸収する）。CPU に置いた Model の Lease には GPU の KV の取り分を当てない（2 の適用範囲の補正）。GPU 利用率を Admission に使う方針は、この Decision では決めておらず、別途提案する。

承認後に方針を変える場合は、この Decision を書き換えず、新しい Decision から `Supersedes` する。
