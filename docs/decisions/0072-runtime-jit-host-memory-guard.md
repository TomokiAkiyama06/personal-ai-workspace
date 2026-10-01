# Model の Runtime の起動で、JIT ビルドの並列数を絞り、ホストの MemAvailable を確かめる値と範囲

- Status: Proposed
- Date: 2026-10-01
- Scope: Issue [#182](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/182)（[Decision 0039](0039-compute-scheduler-calibration.md) の 4。「この Decision では値を決めない」とした値）。関連: Decision 0039 の 2（Model の操作の上限 900 秒）と、Issue #90 に記録した #179 の Codex P2（設定の検証の上限）
- Supersedes: なし。[Decision 0037](0037-gpu-compute-scheduler.md)・[Decision 0039](0039-compute-scheduler-calibration.md)（ともに Approved）は書き換えない

## 背景

2026-09-30 15:22 のマシンの再起動は、ベンチマーク（PAW-017）の vLLM が最初の Load で始めた FlashInfer の JIT ビルドが原因だった。
`ninja` の既定の並列数（CPU 数 + 2 = 34）で `cicc` が 27 個同時に走り（1 個あたり最大 4.4 GiB、計 約 75 GiB）、ホストの RAM（121 GiB）が尽きて OOM になり、Desktop の Process まで止まった。
ベンチマークの Script は、その後 `MAX_JOBS=4` / `FLASHINFER_NVCC_THREADS=1` と、起動前の `MemAvailable` の確認（32 GiB）と、Run 中の監視（8 GiB を下回ったら自分の Server だけを止める）を入れて、残りの Run を完走した。

Decision 0039 の 4 は、これを Deployment の手順に入れる Issue（#182）を作ることだけを決め、値は決めなかった。
Workspace の Backend が Model の Runtime を起動するのは `CommandModelControl`（Admin が設定した Command を、Shell なしで実行する）で、典型的には `systemctl start` で systemd の Unit を起動する。

## 提案

### 1. JIT ビルドの並列数は `MAX_JOBS=4`、`FLASHINFER_NVCC_THREADS=1`

- 1 個の `cicc` が最大 4.4 GiB なので、4 並列で 約 18 GiB に収まる。ベンチマークの残りの Run は、この値で JIT を含めて完走した。
- ビルドは遅くなる（最初の Load だけ。結果は Cache に残る）。Load の上限 900 秒（Decision 0039 の 2）の中で収まらなければ、その Model だけ値を上げるか、Cache を先に作る（Admin の手作業）。
- `CommandModelControl` は自分の Command をこの 2 つの環境変数つきで実行する（Runtime を直接起動する Script のため）。systemd の Unit は Backend の環境を継がないので、Unit の `Environment=` に同じ値を書く（`docs/DEPLOYMENT_UPDATE.md`）。

### 2. GPU の Runtime を起動する前に、ホストの `MemAvailable` が 40 GiB 以上あることを確かめる

- 足りないとき、または `/proc/meminfo` が読めないときは何も起動せず、警告を Log に出し、`HostMemoryLowError`（`host_memory_low`）で失敗する。Scheduler はほかの失敗した Load と同じく `FAILED` にし、60 秒後に再試行する（Decision 0037）。
- 40 GiB は、4 の Runtime の上限（32 GiB。1 の JIT の約 18 GiB と Runtime 自身のホストの使用に余裕を足した値で、ベンチマークの起動の確認と同じ）に、ホストに残す 8 GiB（Kernel・Backend・Desktop の分。ベンチマークの Run 中の監視の下限と同じ）を足した値。`min_host_available_bytes` で変えられ、0 で確認をやめる。
- 読めないときに起動しない（Fail closed）のは、Probe が読めないときに GPU の仕事を通さない（Decision 0037）のと同じ考え方。

### 3. 確かめるのは GPU の Runtime の起動だけ

- Unload はメモリを空ける操作なので確かめない。CPU の Copy（Embedding / Reranker、VRAM の Pressure のときの縮退）は GPU の Kernel をビルドせず、小さい。止めると縮退が効かなくなるので確かめない。

### 4. Run 中の監視は Backend に作らず、Unit の `MemoryMax=` で抑える

- ベンチマークの Script は 5 秒ごとに `MemAvailable` を見て自分の Server を止めた。Backend で同じことをすると、Backend が Runtime の Process に Signal を送ることになり、Decision 0037 の「Scheduler は pid を読むだけで、Signal を送らない」に反する。
- 代わりに、Runtime の Unit に `MemoryMax=` を付け、**32 GiB**（`MemoryMax=32G`）にする。起動は 40 GiB 以上空いているときだけなので、Runtime と JIT のビルドが上限まで使っても、ホストには 8 GiB が残る。上限を超えそうになると Kernel はまずその Unit の Page cache（読んだ Weight）を回収し、それでも足りなければその Unit の中だけで OOM を起こす（Desktop やほかの User の Process を巻き込まない）。
- 「ホストの RAM − 16 GiB」のような全体からの値は採らない。起動の時点でほかの Process が RAM の大半を使っていれば、Runtime が上限に届く前にホスト全体が尽きる。上限を起動の最小値と同じにすると、境界で起動したときにホストに何も残らない（どちらも PR #189 の Codex review）。
- Runtime がもっと要るときは、`MemoryMax=` と 2 の最小値（Unit の `ExecStartPre` と Backend の `min_host_available_bytes`）を一緒に上げる（最小値 = 上限 + 8 GiB）。

### 5. Model の操作の上限の検証の上限を 1,800 秒にする（Probe は 600 秒のまま）

- Decision 0039 の 2 で既定を 900 秒にした。これまでの検証の上限（600 秒、Probe と共通）のままでは 900 秒の設定が通らない（#179 の Codex P2）。
- 既定の 2 倍の 1,800 秒を上限にする。Probe の Command（`nvidia-smi` の Query、実測 最大 42 ms）は 600 秒のまま。

### 6. Footprint は実測のピーク + 2 GiB で与える（Decision 0039 の 1 のとおり、Code は変えない）

- `DeploymentSpec` の 4 つの値の合計（`gpu_bytes`）が `max(Run 中の GPU の使用量 − Load 前の使用量) + 2 GiB` になるように与える。`DEPLOYMENT_UPDATE.md` に手順として書く。

## 代替案

- **並列数をホストの RAM から計算する**（例: `MemAvailable ÷ 5 GiB`）: Machine に合わせられるが、起動の時点の空きで値が変わり、再現しにくい。固定の 4 を既定にし、Unit で上書きできるようにする。
- **JIT の要らない Backend を選ぶ / Cache を前もって作る**（`flashinfer-jit-cache` など）: 根本的だが、採用 Model と Runtime の版（Decision 0040）ごとに確かめる必要がある。別の Issue にする。
- **Backend に Run 中の監視を作る**: 4 のとおり、Signal を送らない原則に反する。採らない。

## リスク

- 40 GiB は 121 GiB のこの Server での値で、RAM の小さい Machine では GPU の Runtime が起動できなくなる。そのときは Admin が `min_host_available_bytes` を下げる（JIT の Cache があれば必要な RAM は小さい）。
- `MemAvailable` は確認の瞬間の値で、起動の後にほかの Process が使えば足りなくなる。4 の `MemoryMax=` は Runtime の分を抑えるだけで、ほかの Process の増加は防げない（そのときはホスト全体の OOM になりうる）。
- 32 GiB は vLLM の Process 自身のホストの使用（Weight の読み込みの一時領域、CUDA Graph の準備など）を実測していない値。足りなければ Weight の Load（Page cache も Unit に数えられる。ただし回収できる）や JIT が遅くなる、または Runtime が OOM で止まる。止まった Runtime は Scheduler が `unloaded` と見て、また Load する。採用 Model の確認 Run（Decision 0040）で Unit の `MemoryPeak` を記録して見直す。

## 決めてほしいこと

1. **JIT ビルドの並列数の既定を `MAX_JOBS=4`、`FLASHINFER_NVCC_THREADS=1` にし、Backend の Model の Command と Runtime の Unit の両方に付ける**（1）でよいか。推奨: はい。
2. **GPU の Runtime を起動する前に、ホストの `MemAvailable` が 40 GiB（4 の上限 32 GiB + ホストに残す 8 GiB）以上あることを確かめ、足りない・読めないときは起動せず警告して `FAILED` にする**（2）でよいか。推奨: はい。
3. **確かめるのは GPU の Runtime の起動だけで、Unload と CPU の Copy は確かめない**（3）でよいか。推奨: はい。
4. **Run 中の監視は Backend に作らず、Runtime の Unit に `MemoryMax=32G`（起動の最小値 − 8 GiB）を付ける**（4）でよいか。推奨: はい。
5. **Model の操作の上限の設定の検証の上限を 1,800 秒にし、Probe は 600 秒のままにする**（5）でよいか。推奨: はい。

## 承認後の扱い

承認されたら `Approval` に記録し、Status を Approved に改める。値は `apps/backend/paw_backend/compute/limits.py` にあり、PR（#182）で実装済み。
方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。
