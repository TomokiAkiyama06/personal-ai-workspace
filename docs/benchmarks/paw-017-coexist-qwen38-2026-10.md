# Qwen3.8-27B-FP8 を Main として同時に動かす確認 Run（0.51）の報告（2026-10）

- Issue: [#180](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/180)（[Decision 0073](../decisions/0073-main-coexistence-confirmation.md) の 2 の確認 Run）。KV・Preemption・待ちの数字は [#200](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/200) の材料
- 実施: 2026-10-07 18:38〜2026-10-08 02:32（Smoke 18:38〜18:54、本 Run 18:54〜02:32）
- 合否の規則: [Decision 0074](../decisions/0074-seed-v2-and-qwen-27b-comparison.md) の 5 と承認時の決定（Main は Qwen3.8-27B-FP8。paw-seed-v1 で 3 回。Resolved@3 13 以上かつ平均 13.0 以上で合格。基準は Qwen3.8-27B-FP8 の単独の v1 の部分 14 / 14 / 14）
- 前の報告: [PAW-017 の報告](paw-017-main-coding-2026-09.md) の 6（10-01 の確認 Run、0.61）、[paw-seed-v2 の比較 Run の報告](paw-017-seed-v2-2026-10.md)
- 生の結果（Trace・Patch・Server の Log・VRAM と `/metrics` の記録）は Repository に入れない（Hidden check の内容を含むため。Decision 0041 の 4）。Server の `/data/results/paw-bench-2026-10-07` にある。機械で読める集計は `aggregate_180b.json`
- Footprint の値と、Scheduler の予約の勘定で足りない 0.5 GiB（477 MiB） の扱いは [Decision 0075](../decisions/0075-coexistence-footprints-qwen38.md)（2026-10-08 Approved）で決めた

## 1. 条件

- **Harness・Prompt・Tool・上限・Evaluator・固定した seed の snapshot（`1d0f683`）は 10-01 の同時に動かす Run と同じ**（paw-seed-v1 の 24 Task、4 Task 並列、Task ごとに 60 step・45 分、Prompt の上限 120,000 token）。vLLM 0.30.0。Sampling は `generation_config` の既定（温度 0 ではない）。Weight は NVMe に Stage したもの。
- 4 つの Deployment を Decision 0037 の Load 順（Main → Memory Worker → Embedding → Reranker）で 1 つずつ起動し、Run の間ずっと置いた。3 回とも同じ Server を起動したまま続けて走らせた。

| Deployment | Model | Runtime / 設定 |
| --- | --- | --- |
| Main | Qwen3.8-27B-FP8 | vLLM、`--gpu-memory-utilization 0.51 --max-model-len 131072 --max-num-seqs 8`、Reasoning parser `qwen3`、Tool parser `qwen3_coder`（0.51 のほかは 10-01 と同じ） |
| Memory Worker | Qwen3.5-4B | vLLM、KV 4 GiB、16k context、4 seqs、`json_schema`（10-01 と同じ） |
| Embedding | Qwen3-Embedding-0.6B | torch（CUDA、bf16）の**独立した Process** |
| Reranker | Qwen3-Reranker-4B | torch（CUDA、bf16）の**独立した Process**（127.0.0.1 の HTTP で Embedding の Process から呼ぶ） |

- **10-01 との違い**: Main の `gpu-memory-utilization`（0.61 → 0.51）と Model（Qwen3.6 → Qwen3.8）、Embedding と Reranker を別々の Process（別々の CUDA Context）にしたこと（Decision 0073 の 2。Scheduler では別々の Deployment のため）。Retrieval の処理（Embedding で上位 30 件 → Reranker で並べ替え）と設定は同じ。
- Memory Worker と Retrieval には、10-01 と同じく Benchmark の Dataset で 2 秒に 1 回ずつ負荷をかけ続けた（Idle で置いただけではない）。
- 安全の設定（Decision 0039 の 4）: `MAX_JOBS=4`、`FLASHINFER_NVCC_THREADS=1`、各起動の前に `MemAvailable` 32 GiB 以上（各 Run の開始時 93〜102 GiB）、8 GiB を下回ったら自分の Process group だけを止める Watchdog（止めたことはない）、各起動の前に GPU に他の Process がないことの確認（Main の起動前は空き 97,231 MiB）。Run 中の GPU にはこの Run の Process だけがいた。
- **Smoke**（本 Run の前、同じ構成で 2 Task）: 4 つが同時に GPU に収まり（GPU 全体の Peak 78,190 MiB）、2 Task とも解けた。その後すべてを止めてから本 Run を起動し直した。

### Scheduler の Admission は通していない

Decision 0073 の 2 は、確認 Run を Scheduler の Admission（`kv_safety` と Class の上限）を通した構成で行い、待ちの時間を記録するとしている。この Run の Harness は 10-01 と同じく vLLM に直接 Request を送り、**PAW の Compute Scheduler の Lease・Admission は通していない**（通すには、Harness の Request を Scheduler の Lease に対応させる Proxy と Deployment・Probe の設定を新しく作る必要があり、この Run では作らなかった）。

代わりに、Main と Memory Worker の vLLM の `/metrics` を 5 秒ごとに記録した（KV の使用率、実行中・待ちの Request、`reason="capacity"` の待ち、Preemption の累計）。
なお、今の Scheduler（`HybridRuntime.run_node`）は Node の開始時に最初の入力から見積もった token（と応答の予約 8,192）で Lease を 1 回取るだけで、会話の伸びを予約に反映しない（[paw-seed-v2 の報告](paw-017-seed-v2-2026-10.md) の「Context の使い方」）。4 つの Agent の開始時の見積もりは、Coding に許す予約（Pool 305,081 token × `kv_safety` 0.90 × Coding の上限 0.95 ≈ 260,800 token）より十分に小さいため、Scheduler を通していても Admission で待つことはなかったと見込まれる（実測ではない）。下の待ちと Preemption は vLLM の KV Pool の中で起きたもの。

## 2. 結果

### Resolved（3 回）

| 構成 | run1 | run2 | run3 | 平均 | **Resolved@3** | 3 回とも解けた Task |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| **Qwen3.8-27B-FP8（同時に動かす、0.51）** | **14** | **14** | **14** | **14.0** | **14** | **14** |
| Qwen3.8-27B-FP8（単独、0.90、10-05 の v1 の部分。合否の基準） | 14 | 14 | 14 | 14.0 | 14 | 14 |
| Qwen3.8-27B-FP8（単独、0.90、10-01） | 14 | 14 | 14 | 14.0 | 14 | 14 |
| Qwen3.6-27B-FP8（同時に動かす、0.61、10-01） | 14 | 13 | 14 | 13.7 | 14 | 13 |

- 数字は 24 Task のうち解けた数。Resolved@3 は [BENCHMARK_EVALUATOR.md](../BENCHMARK_EVALUATOR.md) の定義どおり「最初の 3 回のいずれかで解けた Task」。
- **解けた Task の集合は 3 回とも単独と同じ 14 Task**（Injected Bug 7・Spec 6・hist-08）。残りの Historical 10 Task はどの構成・どの Run でも解けていない。
- Harness の Error は 0。

**Decision 0074 の規則での判定: 合格**（Resolved@3 14 ≥ 13、平均 14.0 ≥ 13.0。基準の 14 / 14 / 14 から下がった Task はない）。

### 振る舞いと速さ

| 構成 | 24 Task の所要（run1 / run2 / run3、分） | 出力 token（run ごと） | submit で終了 | 60 step 到達 | 45 分の timeout | Context 不足 | 生成速度の平均（tok/s） |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| **Qwen3.8-27B-FP8（同時に動かす、0.51）** | **152 / 152 / 148** | 626k / 605k / 630k | 17 / 17 / 16 | 2 / 2 / 3 | **5 / 5 / 5** | **0 / 0 / 0** | **70** |
| Qwen3.8-27B-FP8（単独、10-01） | 124 / 114 / 114 | 783k / 762k / 759k | 15 / 17 / 16 | 2 / 3 / 4 | 1 / 1 / 0 | 6 / 3 / 4 | 112 |
| Qwen3.8-27B-FP8（単独、10-05 の v1 の部分） | - | 787k / 775k / 687k | 16 / 18 / 18 | 5 / 3 / 2 | 1 / 0 / 1 | 2 / 3 / 3 | 111 |
| Qwen3.6-27B-FP8（同時に動かす、0.61、10-01） | 43 / 43 / 45 | 200k / 201k / 218k | 16 / 17 / 16 | 8 / 7 / 8 | 0 | 0 | 79 |

- 同時に動かすと **約 1.3 倍遅い**（単独の 10-01 の 114〜124 分 → 148〜152 分。生成速度の平均 112 → 70 tok/s）。GPU の利用率は Run 中の平均 98〜99%。
- **45 分の timeout が毎回 5 Task**（単独は 0〜1）。すべて、どの構成でも解けない Historical（hist-03・04・06・07・09・10 のうち 5 つ）。単独では同じ Task が Context 不足（120k）や 60 step で終わっていたが、遅くなったため、その前に 45 分に達した。そのため Context 不足は 0 回になった（Context の使い方が変わったのではない。1 Task の Prompt の最大は 97k〜105k で、単独の 114k より小さいのは途中で打ち切られたため）。timeout の Task は打ち切られた Request が LLM の Error として 1 つずつ数えられる（run ごとに 4〜5）。
- **timeout も Context 不足も、どの Resolved の数も変えていない**（timeout の Task はどの Run でも解けていない Task）。ただし、より長い Task では、遅くなった分だけ 45 分の上限に当たりやすくなる。

### KV Cache・Preemption・待ち（Main、vLLM の `/metrics`、5 秒ごと）

Main の KV Pool は 19.4 GiB・**305,081 token**（vLLM の Log。単独の 0.90 では 888,125 token）。

| Run | KV 使用率の最大 | KV 使用率の平均 | KV 使用率 95% 以上の時間の割合 | 実行中の Request の最大 | 待ちの Request の最大（うち KV 不足による待ち `capacity`） | 待ちがあった時間 | Preemption の回数 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| run1 | **100.0%** | 59.4% | 2.3% | 4 | 1（1） | 4.7 分 | 8 |
| run2 | **99.8%** | 59.0% | 3.4% | 4 | 2（2） | 7.1 分 | 21 |
| run3 | **99.8%** | 59.0% | 2.5% | 4 | 1（1） | 2.6 分 | 11 |
| 計 | | | | | | 14.4 分 | **40** |

- **3 回とも KV Pool を使い切った**（10-01 の Qwen3.6-27B-FP8 の 0.61 では最大 51.5%、Qwen3.8-27B-FP8 の単独（0.90、Pool 888k token）では最大 45.1%）。4 つの Agent の Context が同時に長くなると、305k token に収まらない。
- その間 vLLM は実行中の Request を Preemption（KV を捨てて後で再計算）し、新しい Request を待たせた（3 回で計 40 回、待ちの最大 2 Request、待ちがあった時間は Run の 2〜5%）。**OOM や Error にはならず、Resolved も下がらなかった**が、再計算と待ちの分だけ遅くなる（上の約 1.3 倍の一部。Memory Worker・Retrieval と GPU の計算を分けることとの寄与の内訳は測っていない）。
- paw-seed-v2 の報告の見積もり（Qwen3.8 で 4 並列の必要量は約 270k、p90 で約 430k token。共存時の予約は約 259k）のとおり、Pool の不足は Admission ではなく vLLM の中の待ち・Preemption として現れた。対策の比較は [#200](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/200) で行う。
- Memory Worker の vLLM は KV 使用率の最大 3.2〜4.5%、待ち 0、Preemption 0。

### VRAM と Footprint

1 秒ごとの `nvidia-smi`（GPU 全体）と 2 秒ごとの Process ごとの使用量（Process group で Deployment に対応させた）。GPU は 97,887 MiB（95.6 GiB）。

| Deployment | Load 直後 | Peak（Load と Run の全体） | Footprint（Peak + 2 GiB、Decision 0039 の 1） | 10-01 からの見積もり（Decision 0073 の 2） |
| --- | ---: | ---: | ---: | ---: |
| Main（0.51） | 49,124 MiB | **52,104 MiB**（50.9 GiB） | **54,152 MiB（52.9 GiB）** | 52.2 GiB |
| Memory Worker | 13,456 MiB | **13,948 MiB**（13.6 GiB、Load の途中。Run 中は 13,456 MiB） | **15,996 MiB（15.6 GiB）** | 15.6 GiB |
| Embedding | 2,002 MiB | **2,002 MiB**（2.0 GiB） | **4,050 MiB（4.0 GiB）** | （Embedding と Reranker 計 17.1 GiB） |
| Reranker | 12,286 MiB | **12,286 MiB**（12.0 GiB） | **14,334 MiB（14.0 GiB）** | |
| 計 | 76,868 MiB | | **88,532 MiB（86.5 GiB）** | 84.9 GiB |

- **GPU 全体の Peak は 79,892 MiB（78.0 GiB）**。Safety Headroom（4 GiB と GPU の 5% の大きい方 = 4,894 MiB、4.8 GiB）を足して 82.8 GiB で、GPU に収まる（OOM なし。Decision 0040 の VRAM の条件を満たす）。
- Main の Peak は `gpu-memory-utilization` の予算（0.51 × 95.6 = 48.7 GiB）を 2.1 GiB 上回った（Decision 0039 の 1 の「予算を超える分」。10-01 の 0.61 では 1.4 GiB）。Peak は run3 の途中（10-08 01:01、KV の使用率は 47〜85% の時間。原因は特定していない）。
- Embedding と Reranker を別々の Process にしたことで、2 つの Peak の和（14.3 GiB）は 10-01 の 1 つの Process（13.1 GiB）より 1.2 GiB 大きい（CUDA Context が 2 つになるため）。
- **Scheduler の予約の勘定（Decision 0037 の 3）**: 最後に Load する `IF_ROOM` の Model（Load 順で Reranker）は、Load 後に Headroom の外にもう 1 つ `restore_margin_bytes`（既定は Headroom と同じ 4,894 MiB）が残ることを求める（`compute/scheduler.py` の `_fill`）。Footprint の計 88,532 + `external` 44 + Headroom 4,894 + Margin 4,894 = 98,364 MiB で、GPU の 97,887 MiB を **477 MiB（0.5 GiB）超える**。つまり上の Footprint をそのまま与えると、Scheduler は 4 つ目（Reranker）を Load しない。Memory Worker の Peak を Run 中の値（13,456 MiB）で数えると 15 MiB だけ収まる。Scheduler の勘定には、Probe（`nvidia-smi` の `memory.used`）に見える自分の Deployment 以外の使用（`external`、`compute/accounting.py` の `account`）も入る。この Run では、何も起動していない GPU の `memory.used` が 20 MiB（Desktop の Process 8 MiB を含む）、4 つを置いた後は `memory.used` 76,912 MiB に対して 4 つの Process の計が 76,868 MiB で、`external` は 20〜44 MiB だった（下の勘定は 44 MiB で数える）。なお、何も起動していない GPU の空き（`memory.free`）は 97,231 MiB で、`memory.total` との差 656 MiB のうち 636 MiB は Driver が取る分で `memory.used` に出ない。Scheduler は `memory.total` と `memory.used` で数えるため、この分は勘定に入らず、Safety Headroom が吸収する。扱いは [Decision 0075](../decisions/0075-coexistence-footprints-qwen38.md) で決めた。

### Memory Worker と Retrieval（同時に動かしている間、Run ごとの平均）

| | 10-07（Qwen3.8、0.51、Embedding と Reranker は別々の Process） | 10-01（Qwen3.6、0.61、1 つの Process） |
| --- | --- | --- |
| Retrieval | Recall@5 0.985・MRR 0.983・nDCG@5 0.971・失敗した Query 0・Latency p95 458〜461 ms（Pass ごとの最大 502 ms） | Recall@5 0.985・MRR 0.983・nDCG@5 0.971・p95 448 ms |
| Memory Worker | Schema の遵守率 0.974・抽出の Recall 0.829・Latency p95 1.51〜1.53 秒（最大 1.59 秒） | 0.974・0.829・p95 1.46 秒 |

- 品質は 10-01 と同じ。Latency は少し長い（Retrieval の p95 +10〜13 ms、Memory Worker の p95 +0.05〜0.07 秒）。Retrieval は Process 間の HTTP（127.0.0.1）が 1 回増えている。

## 3. 読み方

- **Decision 0074 の規則では合格**（Resolved@3 14、平均 14.0。単独と同じ 14 Task の集合を 3 回とも解いた）。Decision 0074 の承認時の決定どおり、合格したので 4 つの Deployment の Footprint を与える段階に進める（値は Decision 0075）。
- **VRAM の実際の使用**（Peak 78.0 GiB + Headroom 4.8 GiB = 82.8 GiB）は GPU に収まる。一方、**Footprint（Peak + 2 GiB）と Scheduler の復帰の Margin で数える予約の勘定では 0.5 GiB（477 MiB）足りない**。10-01 からの見積もり（Decision 0073 の 2 では 1.1 GiB の余裕）より、Main が 0.7 GiB、Embedding と Reranker を別々にした分が 0.9 GiB 大きかった。
- **0.51 の KV Pool（305k token）は Qwen3.8-27B-FP8 の 4 並列には足りない**。3 回とも Pool を使い切り、計 40 回の Preemption と最大 2 Request の待ちが起きた。正解数は変わらなかったが、1 回の Run は単独より約 1.3 倍遅く、45 分の timeout に当たる Task が増えた（解けない Task だけ）。Interactive の応答の速さへの影響は測っていない。対策（古い reasoning を履歴から外す・Main の割り当てを増やす・同時に動かす Agent を減らす・会話の伸びを予約に反映する Scheduler）は #200 で比べる。
- 3 回 × 24 Task で、v1 の Historical の 10 Task はどの構成でも解けず、Model の差を測れない（Decision 0074 のリスクと同じ）。Context の長い Task での影響は、v2 の新しい Task（長い Spec）で測る方が見えやすい（この Run では測っていない）。

## 4. 測っていないこと

- Scheduler の Admission を通した Run（1 の「Scheduler の Admission は通していない」）。
- Interactive の Request（Chat）の応答の速さ。Coding の Agent が KV を使い切っているときに Interactive が来ると、vLLM の中で待つと見込まれる。
- paw-seed-v2 の新しい 25 Task での共存時の結果。
- 約 1.3 倍の遅さのうち、Preemption・待ちと、Memory Worker・Retrieval との GPU の計算の共有の、それぞれの寄与。

## 5. Run の Script（Repository の外）

`/data/results/paw-bench-2026-10-07/_code` にある。10-01 の Script からの変更は次だけ。

- `serve_lib.sh` は 10-05 の版（Server を止める関数の loop の変数を `local` にした修正を含む）。Process ごとの VRAM の記録に Process group の列を足し、vLLM の `/metrics` を 5 秒ごとに記録する関数を足した。
- `rr_server.py`（新規）: Reranker を独立した Process で動かし、`side_load.py` の Retrieval（Embedding だけを持つ）から HTTP で呼ぶ。Reranker の計算は 10-01 の `_Reranker` と同じ。
- `run_180b.sh`（Smoke と本 Run）、`aggregate_180b.py`（集計）。
