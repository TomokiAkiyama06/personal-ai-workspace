# 観測した空き VRAM による Admission の追加の安全確認（GPU 利用率は使わない、足りなければ待たせて警告する）

- Status: Proposed
- Date: 2026-09-28
- Scope: Issue [#145](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/145)。PAW-036 の Compute Resource Scheduler（`apps/backend/paw_backend/compute/`）の Admission の経路と Exclusive の手順
- Supersedes: なし。[Decision 0037](0037-gpu-compute-scheduler.md)（Approved）は書き換えない。0037 の承認後の補足が「GPU 利用率を Admission に使う方針は、この Decision では決めておらず、別途提案する」とした点への答えで、0037 の 3（VRAM の勘定）・6（Probe が使えないとき）・7（Exclusive）に追補する
- Direction: 2026-09-28、Human が作業 Session 内で方向を**直接決めた**（下の「Human が決めたこと」）。この Decision はその記録と、残りの細部の提案である
- Approval: 未承認（細部の 1〜8 について承認を求める）

## 背景

0037 の Admission は KV Cache の Token で決まる。常駐 Model への Request は Model の Footprint（Weight + KV Cache Pool + Runtime Buffer + Workspace）の内側で走るので、新しい VRAM を要らない。VRAM は、Model の Load（0037 の 3 と 5）と Exclusive（0037 の 7）のときだけ `available = total − headroom − committed` で確かめている。`committed` は Probe が見る使用量を下回らない（Scheduler の外の Workload も `external` として数える）。

一方で、**Scheduler の外で動く Process は Lease の勘定に見えない**。実際に起きた例として、Workspace の外の vLLM が 96 GB の GPU のうち 74 GB を持った。このとき 0037 の実装は次のように振る舞う。

- 常駐 Model への Request は KV の空きだけで通る。Pressure は 1 回の読み取り（5 秒）に 1 段ずつ縮退を進め、4 段目（Admission の抑制）までに約 20 秒かかり、Interactive は抑制されない。
- Exclusive は Model をすべて Unload してから、空かないことに 60 秒後に気づいて `NOT_FREED` で断る（Unload した Model は次の `refresh()` が戻すが、その間の Local の仕事は止まる）。
- Model の Footprint の外に自分で VRAM を確保する仕事（Background の GPU Job など）を、要る VRAM で待たせる手段がない（`ComputeRequest.vram_bytes` は Exclusive にしか使えない）。

## Human が決めたこと（2026-09-28、作業 Session で直接）

1. **GPU 利用率（%）は Admission に使わない。**
2. **既存の読み取り専用の Probe が見る空き VRAM だけ**を、追加の安全確認に使う。
3. 空き VRAM が**要求量 + 0037 の Safety Headroom** より小さければ、その仕事は**延期する**（待ち行列に入れる。拒否しない）。
4. そのとき**警告を出す**（Log / Audit と、通知の Hook があればそれにも）。

以下は、この方向の中で要件も 0037 も決めていない細部の提案である。実装はこの推奨で置いた。

## 提案

### 1. 「要求量」は仕事が自分で確保する VRAM とし、常駐 Model の事前確保を二重に数えない

- `ComputeRequest.vram_bytes` を Exclusive 以外の Class にも使えるようにする。意味は「その仕事が **Model の Footprint の外に**自分で確保する VRAM」（0 以上、1 PiB 以下）。KV Cache だけを使う普通の Request は 0 のまま。
- 空き VRAM の確認は `vram_bytes` > 0 の仕事にだけ効く。
- 理由: vLLM / SGLang は Load のときに KV Cache Pool を丸ごと確保するので（`gpu_memory_utilization`）、Probe は常駐 Model の Footprint を**使用中**と見る。この Memory は 0037 の勘定で予約として数えている。KV だけを使う Request にも「空き ≥ Headroom」を求めると、自分の常駐 Model の事前確保をもう一度数えることになり、何も確保しない Request を理由なく止める（96 GB で Footprint を 90% とすると、Scheduler の外に何もなくても空きは約 9.6 GB で、Memory Worker と Embedding を置けば Headroom の 4.8 GB を割りうる）。Footprint の内側の仕事は、外の Workload が増えても自分の Memory は既に持っているので OOM にならない。Pressure への対応は 0037 の縮退の段のままにする。
- 判定: `観測した空き − (Scheduler が約束したが Probe にまだ見えない量) ≥ 要求量 + Headroom`。「まだ見えない量」は Load 中の Model の予約や、Admission 済みでまだ確保していない仕事の `vram_bytes`（同じ読み取りの間に 2 つの仕事が同じ空きを数えないため）。0037 の `committed` は Probe の使用量を下回らないので、これは `available ≥ 要求量` と同じ値になる（Test が固定する）。
- `vram_bytes` を持つ Lease は、その量を予約として数える。仕事が実際に確保した VRAM（どの Model の Process でもない）は、0037 の Exclusive と同じく、Lease を与えた時点の `external` を超えた分だけ Lease の予約で吸収し、`external` に二重に数えない（それ以前からある他の Workload の分は `external` のまま）。Lease を返した後に残った Memory は `external` になる。Lease が複数あるときは、どの Lease の Process がどれだけ確保したかは分からないので、返した Lease はその予約の範囲で吸収していた分をすべて持っていたとみなして `external` に移す（残りの Lease がまだ確保していない予約を、返した Lease の残った Memory で埋めない）。残った Lease が確保した分がその Lease の終わりまで二重に数えられることはあるが、GPU を過剰に約束することはない。Probe が読めない間に返した Lease は、読めるようになった最初の読み取りで同じように移す（その間に返した Lease の予約を合わせた範囲で）。
- CPU / Cloud に置いた Lease は VRAM を持たない（`vram_bytes` は 0 になる）。

### 2. 足りない仕事は待ち行列で待ち、VRAM を要る後の仕事に追い越させない

- 空きが足りない仕事は `Refusal.INSUFFICIENT_FREE_VRAM` で待つ（0037 の 4 の待ち行列。拒否しない）。
- VRAM は GPU 全体のものなので、**VRAM を要る仕事の待ちは、同じか下の Class の、後から来た VRAM を要る仕事を（Model に関係なく）止める**（小さい Job が続いて大きい Job が永久に待つことを防ぐ。0037 の 4 と同じ考え方）。
- VRAM を要らない仕事（Footprint の内側）と上の Class の仕事は、VRAM を待つ仕事の後ろに並ばない。同じ Model の、VRAM だけを待っている待ちも、VRAM を要らない新しい仕事を止めない。
- 他の Model の仕事を止めるのは、VRAM を待っている（最後の拒否が `INSUFFICIENT_FREE_VRAM`）待ちだけとする。自分の Model の空き（`NOT_RESIDENT` / `SEQUENCES_FULL` など）を待つ待ちは、他の Model の VRAM を要る仕事を止めない。CPU の複製に置かれる仕事は VRAM を持たないので、この順番に加わらない。

### 3. Probe の古さは 0037 の 15 秒のまま、読み取りの間は約束した量で埋める

- 追加の確認も 0037 の 6 と同じ読み取り（`refresh()` の既定 5 秒ごと、最大の古さ 15 秒）を使う。15 秒より古ければ 0037 の 6 のとおり `PROBE_UNAVAILABLE` で待つ。
- 読み取りの間に Admission した仕事は、1 の「まだ見えない量」として数えるので、次の読み取りまでの間に同じ空きを二度与えない。
- 代わりに、VRAM を要る仕事ごとに Probe をその場で読む案もあるが、`nvidia-smi` は 1 回に数百 ms かかることがあり、Admission が Probe を待つので採らない。

### 4. 延期の上限は呼び出し側の待ち時間とし、Scheduler に別の上限を置かない

- 共有の Class: 0037 の `acquire(wait_seconds=...)`（`HybridRuntime` の既定 600 秒）までそのまま待ち、超えたら `ComputeUnavailableError(INSUFFICIENT_FREE_VRAM)`（Retry 可）で失敗し、Orchestrator の Retry に従う。`allow_cloud` で Cloud が許された仕事は 0037 の 8 のとおり Cloud へ回る（そのときは警告しない）。
- 待ちが切れたときは、下の 5 の警告を「諦めた」（`gave_up`）として出す（エスカレーション）。
- Exclusive: 6 のとおり、同じ `wait_seconds` を「VRAM を待つ時間」と「走っている仕事の Drain」で共有する（合わせての上限）。

### 5. 警告は Log と注入した Sink に出し、同じ種類は 300 秒に 1 回まで

- `paw_backend.compute` の Logger に WARNING を出す: 何が待つか（`request` / `exclusive` / `model_load`）、Class、要求量・観測した空き・他の Workload が使う量・Headroom（MiB）、待つか諦めたか。**PID・Process 名・Command の出力は出さない**（他の User の Process でありうるため。0037 の 11 と同じ）。
- 注入した `VramWarningSink.vram_deferred(VramDeferral)` にも同じ内容を渡す（通知や System Health（PAW-066）が実装する Hook。Sink の例外は型の名前だけを Log に残し、Admission を止めない）。
- 頻度: 同じ種類（何が待つか・Class・諦めたか）は `vram_warning_interval_seconds`（既定 **300 秒**、暫定値）に 1 回まで。待ちが続く間、毎回の読み取りで Log を埋めないため。
- **Audit**: Scheduler は 0037 の 1 のとおり DB を持たないので、自分で Audit を書かない。Audit に残す場合は、Scheduler を Application の Lifespan に組み込むとき（まだ組み込んでいない）に Sink の実装として書く。推奨は、System Health の Event として書き、Task の Audit（AGENTS.md 13）には延期された Node の失敗理由（`INSUFFICIENT_FREE_VRAM`）として既存の経路で残すこと。
- Model の Load（0037 の 5 の常駐）も、**他の Workload の VRAM がなければ入る**のに入らないときは `model_load` として警告する（自分の Model だけで埋まっているときは警告しない）。Load の可否そのものは 0037 の勘定のまま（既に観測した使用量を数えているので、判定は変えない）。

### 6. Exclusive: 他の Workload の VRAM が空かない間は、何も Drain・Unload せずに待つ

- Exclusive は、Workspace の Model をすべて降ろしても要求量が空かないとき（`total − headroom − external < vram_bytes`）、**新しい仕事を止めず、Model を Unload せずに待つ**（Unload しても空かないので）。その間 Scheduler は通常の状態で、Local の仕事は続き、`status().exclusive_waiting_for_vram` が立つ。2 つ目の Exclusive は 0037 どおり `BUSY`。
- 空いたら 0037 の 7 の手順（Admission を止める → Drain → Unload → Probe で確認 → Lease）に進む。Drain の間に他の Workload が VRAM を取ったら、何も Unload せずに通常へ戻して、また待つ。
- `wait_seconds` を過ぎたら `ExclusiveUnavailableError(NOT_FREED)`（Probe が読めない間に過ぎたら `PROBE_UNAVAILABLE`）で断る。どちらも何も Unload していない。
- 自分の常駐 Model は降ろせば空くので、Exclusive を待たせる理由に数えない（1 と同じく二重に数えない）。
- 始める前に Probe が読めなければ、0037 の 7 のとおり何も Unload せずに断る。

### 7. Probe が使えないときは 0037 の 6 のとおり Fail closed（待たせる）

- VRAM を要る仕事も、Probe が使えない・古いときは入れない（`PROBE_UNAVAILABLE` で待つ。拒否しない）。空きを確かめられないまま VRAM を確保させない。
- この場合の警告は 0037 の「GPU probe is unavailable」の Log（状態が変わったときに 1 回）に任せ、5 の警告は重ねない。待ちが切れたときに最後の読み取りがあれば、それを使って `gave_up` の警告を出す。

### 8. GPU 利用率は表示だけ

- Probe が読む `utilization_percent` は、これまでどおり `status()` と `compute-status` に出すだけで、Admission にも縮退にも使わない（Human の決定 1）。Test が「利用率 100% でも入る、0% でも空きが足りなければ待つ」ことを固定する。

## 代替案

- **GPU 利用率を Admission に使う**: Human の決定 1 で採らない。
- **すべての Local の Request に「空き ≥ Headroom」を求める**: 自分の常駐 Model の事前確保を二重に数え、何も確保しない Request を止める（1）。Pressure への対応は 0037 の縮退の段がある。
- **Pressure（`available < 0`）の間は、Footprint の内側の Request も直ちにすべて待たせる**: 0037 の 5（4 段目まで 1 段ずつ、Interactive は抑制しない）を Supersede することになり、外の Workload の一時的な増加で Interactive まで止まる。事前確保の Runtime では何も守らない。採らない（必要なら別の Decision）。
- **延期に Scheduler 独自の上限を置く**: 呼び出し側の待ち時間（Retry と Cloud の判断）と二重になる。
- **Exclusive は 0037 どおり Unload してから確かめる**: 外の Workload がいる間、Unload しても空かず、Local の仕事を理由なく止める。

## リスク

- 300 秒と、1 の判定式（空き − まだ見えない量 ≥ 要求量 + Headroom）の Headroom は 0037 と同じ暫定値で、Benchmark 後に見直す。
- `vram_bytes` は呼び出し側の申告で、Scheduler は測らない。過小に申告された仕事は、確保した分が次の読み取りで Lease の予約を超えて `external` に見え、以後の Admission が安全側に寄るだけで、その仕事自体は止めない。
- 1 の吸収は 0037 の Exclusive と同じ近似で、Lease を与えた後に他の Workload が増え、仕事がまだ確保していない間は、その増加の一部（最大で Lease の予約分）を予約で吸収して少なく数える。`committed` は Probe の使用量を下回らない。
- Footprint の内側の Request は、外の Workload が増えても 0037 の縮退の段にしか止められない。On-demand で KV を確保する Runtime（Footprint を事前に確保しない構成）では、予約はあっても Memory はまだ無いので、外の Workload が先に取れば OOM になりうる。V1 の Main は vLLM / SGLang（事前確保）を想定し、そうでない Runtime を使うときは見直す。

## 決めてほしいこと

1. **「要求量」を Footprint の外に自分で確保する VRAM（`vram_bytes`）とし、Exclusive 以外の Class にも使えるようにする。確認は `vram_bytes` > 0 の仕事にだけ効き、KV だけを使う Request は 0037 のまま**（1）でよいか。推奨: はい（常駐 Model の事前確保を二重に数えないため）。
2. **判定を「空き − まだ見えない約束 ≥ 要求量 + Headroom」とし、Lease の `vram_bytes` を予約として数え、確保した分は Lease 時点の `external` を超えた分だけ吸収する**（1）でよいか。推奨: はい。
3. **VRAM を要る仕事の待ちは、後の VRAM を要る仕事に（Model に関係なく）追い越させず、VRAM を要らない仕事と上の Class は止めない**（2）でよいか。推奨: はい。
4. **Probe の古さの許容は 0037 の 15 秒のまま、読み取りの間は約束した量で埋める**（3）でよいか。推奨: はい。仕事ごとにその場で読む案は採らない。
5. **延期の上限は呼び出し側の `wait_seconds`（Node は既定 600 秒）とし、切れたら Retry 可の `ComputeUnavailableError(INSUFFICIENT_FREE_VRAM)` と `gave_up` の警告。Exclusive は同じ `wait_seconds` を VRAM の待ちと Drain で共有する**（4・6）でよいか。推奨: はい。
6. **警告は Log と注入した Sink（`VramWarningSink`）に出し、同じ種類は 300 秒に 1 回まで。Scheduler は Audit を書かず、Lifespan への組み込み時に Sink の実装として System Health の Event に書く**（5）でよいか。推奨: はい。他の Workload が原因の Model の Load の見送りも警告する。
7. **Probe が使えないときは 0037 の 6 のとおり Fail closed（待たせる）で、5 の警告は重ねない**（7）でよいか。推奨: はい。
8. **Exclusive は、Workspace の Model を降ろしても空かない間は Drain・Unload せずに通常のまま待ち、Drain 中に他の Workload が VRAM を取ったら通常へ戻して待ち直す。期限を過ぎたら何も Unload せずに `NOT_FREED`**（6）でよいか。推奨: はい。

## 承認後の扱い

承認されたら `Approval` に記録し、Status を Approved に改める。数値は `apps/backend/paw_backend/compute/limits.py` と `ComputeConfig`（`vram_warning_interval_seconds`、0037 の Headroom と Probe の古さ）で変えられる（DB に書いたものはない）。
方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。
[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。
