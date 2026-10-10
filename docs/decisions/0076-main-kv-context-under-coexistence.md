# Qwen3.8-27B-FP8 を Main にしたときの共存時の KV / Context の不足への対策（古い Reasoning を外す、会話の伸びを予約に反映する、割り当てと並列の上限は変えない）

- Status: Proposed
- Date: 2026-10-08
- Scope: Issue [#200](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/200)（[Decision 0074](0074-seed-v2-and-qwen-27b-comparison.md) の後続）。根拠は [KV / Context の不足の分析](../benchmarks/paw-017-kv-context-qwen38-2026-10.md)、[共存の確認 Run（0.51）の報告](../benchmarks/paw-017-coexist-qwen38-2026-10.md)（[Decision 0075](0075-coexistence-footprints-qwen38.md)）、[paw-seed-v2 の報告](../benchmarks/paw-017-seed-v2-2026-10.md)。Reasoning の履歴の方針の差し込み口は [Decision 0083](https://github.com/TomokiAkiyama06/personal-ai-workspace/pull/209)（Proposed）の 3 の `ReasoningHistoryPolicy` を前提にする
- Supersedes: [Decision 0037](0037-gpu-compute-scheduler.md) の 2 のうち「各 Request は `context_tokens` を Pool から予約する。Pool の `kv_safety`（推奨 90%）までしか予約できない」の部分だけ（承認されたら、この Decision の 3 で置き換える: 新しい Node の Admission はこれまでどおり上限までしか予約できないが、実行中の Node の予約の増加は上限を超えてよい）。0037 のほかの点（Admission の単位、Class の上限、待ち行列、縮退、9 の開始時の見積もり）、[Decision 0039](0039-compute-scheduler-calibration.md)、[Decision 0073](0073-main-coexistence-confirmation.md) の 2（0.51）、[Decision 0074](0074-seed-v2-and-qwen-27b-comparison.md)、[Decision 0075](0075-coexistence-footprints-qwen38.md)（Footprint と `restore_margin_bytes`）、[Decision 0021](0021-dag-orchestrator-policy.md) の `max_parallel_nodes` は変えない

## 背景

Decision 0074 で Main は Qwen3.8-27B-FP8 になった。共存時（Main 0.51）の Main の KV Pool は 305,081 token で、10-07 の確認 Run（paw-seed-v1、4 並列）は 3 回とも Pool を使い切り、Preemption 計 40 回・待ちの最大 2 Request・単独の約 1.3 倍の所要になった（正解数は変わらない。Decision 0075）。

分析（報告の 1〜3）でわかったこと:

- Qwen3.8-27B-FP8 の出力の 62〜70% は Reasoning で、Harness は Reasoning を履歴に残す。Task ごとの Prompt の最大は v2 で中央値 67k / p90 107k / 最大 115k、その最大の Prompt の中央値 29%（p90 46%）はそれまでの Reasoning。
- Chat template の `preserve_thinking: false` では、1 つの Task の Tool loop の Reasoning は外れない。外すには Adapter が `reasoning_content` を消す必要がある。
- 4 つの Agent の Context の和の再現（10-07 の実測と相関 0.88〜0.89、Pool の 95% 以上の時間は実測の約 2 倍に出る安全側）では、今の構成は Pool の 95% 以上が v1 で 6.3%・v2 で 10.1% の時間。
- 今の Scheduler（`HybridRuntime.run_node`）は Node の開始時の見積もりで 1 回だけ予約し、会話の伸びを予約に反映しない。足りない分は Admission ではなく vLLM の中の Preemption と待ちになり、Interactive の Request もその中で待つ。

Issue #200 は次の 4 つの候補を比べて提案することを求めている。どれを採るかは承認済みの Decision が決めていない（0083 の 3 は差し込み口だけを作り、方針をこの Decision に委ねている。0083 の 12 は `--max-model-len` もここに委ねている）。

## 比べた結果（報告の 2・3）

| 候補 | 0.51・4 並列での KV（Pool の 95% 以上の時間、v1 / v2） | 新しい Node が予約の上限で待つ時間（3 を入れた場合） | 費用・危険 |
| --- | ---: | ---: | --- |
| 何もしない | 6.3% / 10.1% | （予約に反映しない） | Preemption・待ち・約 1.3 倍の所要。Interactive も vLLM の中で待つ |
| **古い Reasoning を外す（直近 2 Step を残す、32k から）** | **0% / 0%**（Peak 267k） | **23.6% / 10.3%** | 正解数への影響は未測定（GPU で約 13 時間の Run）。Prefix cache に乗らない Prefill が 1 呼び出しあたり約 0.8k → 3.2k token |
| Main に割り当てを足す: Memory Worker の KV 4 → 2 GiB（0.53） | 0.4% / 5.2% | | Memory Worker の同時処理（16k × 4）が減る |
| 同: Embedding を CPU へ（0.55） | 0.0% / 2.0% | | Embedding の CPU の Latency は未測定 |
| 同: Embedding と Reranker を CPU へ（0.69） | 0% / 0% | | Reranker（4B）の CPU の Latency は未測定で、秒の単位になりうる（今は p95 約 460 ms） |
| 並列を 3 に固定する | 0.0% / 0.7% | 3.3% / 5.6% | 要件（並列 Agent 数を固定値にしない）に反する。所要が延びる |
| **会話の伸びを予約に反映する（Reasoning は残す）** | （Admission で抑える） | 51.1% / 49.2% | 実質 3 並列に近い。Interactive の分が Admission で守られる |

## 提案

### 1. Main の Local Agent の Reasoning の履歴は、直近 2 Step を残し、それより古い Reasoning を外す（Prompt が 32,768 token を超えてから）。ただし 2 の Run に合格した場合に限る

- 方針: `DropOlderReasoning(keep_last=2, min_prompt_tokens=32_768)`。この Request の Prompt の見積もり（直前の `usage.prompt_tokens` + 直前の出力 + 新しい Tool の結果の Byte ÷ 3。3 の予約と同じ式）が 32,768 を超えたら、この Request から、直近 2 つより古い Assistant の Message の `reasoning_content`（と `reasoning`）を消す。消した Message は元に戻さない（Prefix cache が 1 回ごとに 1 Step 分だけ外れる形にする）。Message の本文（`content`）と Tool 呼び出し・Tool の結果は消さない。
- Decision 0083 の 3 の `ReasoningHistoryPolicy` の実装として入れ、Main（Qwen3.8-27B-FP8）の Local Agent の既定にする。Benchmark と同じ「残す」（`KeepAllReasoning`）は設定で選べるまま残す。消した Reasoning は保存も Log もしない（0083 の 3 のとおり）。
- 見積もり（Model の振る舞いが変わらないと仮定）: Prompt の最大の p90 は 107k → 71k、最大は 115k → 84k。4 並列の KV の必要量の Peak は v2 で 382k → 267k（Pool 305k の 88%）。
- 32,768 からにするのは、32k 未満なら 4 並列でも Coding の予約の上限（約 260.8k）に収まり、外す理由がないため。v2 の解けた 111 Task-run のうち 28 はこの範囲で終わり、振る舞いが変わらない。
- `keep_last=2` にするのは、0〜4 で Prompt の減り方がほぼ同じ（p90 69k〜72k）で、直前の考えを消す 0・1 より振る舞いの変化が小さいと見込むため。

### 2. 1 の正解数への影響を、GPU で paw-seed-v2 を 3 回走らせて確かめる（実施は Human の許可の後）

- 条件: 10-05 / 10-06 の Qwen3.8-27B-FP8 の単独の比較 Run（paw-seed-v2 の固定した 49 Task、`/data/results/paw-bench-2026-10-05/_dataset`）と、Reasoning の扱いのほかはすべて同じにする。vLLM 0.30.0、Stage した Weight（`/home/Tomoki-home/ai/models/paw-bench-stage-1001/Qwen__Qwen3.8-27B-FP8`）、`--gpu-memory-utilization 0.90 --max-model-len 131072 --max-num-seqs 8 --reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser qwen3_coder --language-model-only`、4 Task 並列、60 Step・45 分・Prompt の上限 120,000 token・応答 16,384 token、Evaluator は seed の snapshot `1d0f683`、GPU には Main だけを置く。安全の設定は Decision 0039 の 4 と [Decision 0072](0072-runtime-jit-host-memory-guard.md)（`MAX_JOBS=4`、`FLASHINFER_NVCC_THREADS=1`、**起動前の `MemAvailable` は 0072 の 2 の 40 GiB 以上**（10-05 の Script の 32 GiB から上げる。`serve_lib.sh` の `MEM_MIN_START_MIB` を 40960 にする）、8 GiB を下回ったら自分の Process group だけを止める Watchdog、起動前の GPU の確認）。
- Harness の変更（Repository の外の `coding_harness.py` の複製だけ）: 環境変数 `PAW_REASONING_KEEP_LAST`（未設定なら今と同じ）と `PAW_REASONING_DROP_MIN_PROMPT` を読み、毎回の Request の前に、その Request の Prompt の見積もり（直前の `usage.prompt_tokens` + 直前の `completion_tokens` + 新しい Tool の結果の UTF-8 の Byte 数 ÷ 3）が後者を超えていれば、`messages` の Assistant のうち直近 `PAW_REASONING_KEEP_LAST` 個より前のものから `reasoning_content` と `reasoning` を消す。Trace には各 Step で消した Message の数と消した Reasoning の文字数を足す。Main の vLLM の `/metrics` を 5 秒ごとに記録する（10-07 の `serve_lib.sh` の関数。Prefix cache の Hit 率を含む）。
- 置き場所と手順: `/data/results/paw-bench-2026-10-XX/_code` に 10-05 の `_code` を複製して上を変え、`run_198.sh` の `compare` の Qwen3.8-27B-FP8 の行だけを `PAW_REASONING_KEEP_LAST=2 PAW_REASONING_DROP_MIN_PROMPT=32768` 付きで run1〜run3 に走らせる（毎回 Server を起動し直す）。
- 費用: 基準の 3 回は 250〜265 分ずつだったので、**GPU を単独で約 13 時間**（Load と Unload を含めて約 13.5 時間）。その間ほかの Model は GPU に置けない。Prompt が短くなる分だけ速くなる可能性があるが、Prefill が増える分と相殺するかは分からない。
- 合格の規則（Decision 0074 の 5 の読み方と同じ）: 基準（37 / 37 / 37、Resolved@3 37）に対して **Resolved@3 36 以上かつ平均 36.0 以上**。あわせて、Task ごとの Prompt の最大（見積もりは中央値 48k / p90 71k）、`context_exhausted` の回数、出力 token、所要、Prefix cache の Hit 率を記録して報告する。
- **KV の不足を解いたことの条件**（正解数とは別に、すべて満たすこと）: (a) Task ごとの Prompt の最大の p90 が 80k 以下（基準 107k）、(b) `context_exhausted` が 0 回（見積もりでは Prompt の最大は約 84k で上限に達しない）、(c) その Run の Trace で報告の 3 と同じ再現をしたとき、0.51 の Pool（305k）の 95% 以上の時間が 1% 以下、(d) 3 回の所要の平均が基準（約 258 分）の 1.15 倍以下。
- 正解数の規則と KV の条件の両方を満たしたら 1 を Main の既定にする。正解数は満たすが KV の条件のどれかを満たさないときは、既定にせず、結果を示して Human に別に判断を求める。正解数を満たさないなら 1 は採らず（`KeepAllReasoning` のまま）、3 だけで Admission により並列を抑え、4 の順で割り当ての変更を別に提案する。

### 3. Scheduler は会話の伸びを予約に反映する（実行中の Node の予約は増やせるが待たせない。新しい Node は上限で待つ）

- `ComputeLease` に予約の大きさを変える操作（例: `resize(context_tokens)`）を足す。Local の Agent の Runtime（Decision 0083 の `LocalAgentRuntime`）は、毎回の Request の前に「直前の `prompt_tokens` + 直前の出力 + 新しい Tool の結果の Byte ÷ 3 − 1 の方針でこの Request から外した Reasoning の token（外した Step の応答の `usage` の `reasoning_tokens`）+ その Request の `max_tokens`」で予約を更新する（減らすこともある）。予約は `ReasoningHistoryPolicy` を通した後の履歴で数え、外した Reasoning を 1 回余分に予約しない。応答の分は Decision 0039 の 3 のとおり、Runtime が渡す `max_tokens`（Benchmark と同じなら 16,384）を使い、8,192 の既定は使わない。
- **実行中の Node の予約の増加は待たせず、Class の上限と `kv_safety` を超えても認める**。増加を待たせると、4 つの Agent が互いの終わりを待って止まりうる（どれも予約を返さない）。超えた分は vLLM の中の Preemption として現れる（今と同じ）が、新しい Node の Admission は、予約の合計が上限を下回るまで待つ（0037 の 4 の待ち行列のまま）。このため 0037 の 2 の「`kv_safety` までしか予約できない」を、新しい Node の Admission に限る形に置き換える（Supersedes）。
- 予約の増加は、Prompt と `max_tokens` の和が Deployment の Context の上限（`max_context_tokens`、131,072）を超えたら断る（`CONTEXT_TOO_LONG`。Agent はその Node を Context 不足で終える。vLLM が断る Request を Scheduler も通さない）。
- vLLM の `/metrics` の `vllm:kv_cache_usage_perc` を `ModelControl.kv_usage` に配線する（今の `CommandModelControl.kv_usage` は常に `None`）。0037 の 2 の「予約と観測の大きい方」が実際に効くようにする。
- **上の Class の取り分は、下の Class の伸びで減らさない**（Decision 0037 の 4 の Class の上限は変えない）: Class C の新しい Request の Admission では、C と同じか上の Class の予約はそのまま数え、C より下の Class の予約は (i) それぞれその Class の上限までとして数え（伸びで上限を超えた分は数えない）、(ii) その合計を下の Class の中で最も高い上限までとして数える（Interactive に対しては Coding・Support・Background の合計を 95%、Coding に対しては Support・Background の合計を 85%、Support に対しては Background を 70% まで）。Class ごとに切るだけでは、例えば Support と Background の伸びの合計が 155% と数えられ、上の Class を締め出すため。このため Interactive の新しい Request は、ほかの Class が伸びていても、少なくとも Pool × 0.90 × 5%（約 13.7k token）から Interactive の予約を引いた範囲で Admission を通る。同じ Class の新しい Request（新しい Coding の Node など）は、伸びて超えた分も含めて数えて待つ。
- 観測した KV の使用率（`kv_usage`）が高いときは、0037 の 2 のとおり予約と観測の大きい方で判断するので、Interactive も Admission で待ちうる。そのときも 0037 の 4 の待ち行列で Interactive が先頭に並ぶ（Coding を追い越す）。
- 見積もり（報告の 3、`max_tokens` 16,384）: 1 と組み合わせると、4 並列の上限のまま新しい Node が待つのは Run の 10〜24% の時間。1 なしでは約 49〜51%（実質 3 並列に近い）。
- Decision 0075 の 3 のとおり、この変更の確認では Scheduler の Admission を通した共存の Run を行う（実装の PR で計画し、GPU を使う前に Human の許可を得る）。

### 4. Main の割り当て・Memory Worker・Embedding / Reranker の置き場所、並列の上限、Context の長さは変えない

- 共存時の Main は 0.51 のまま（Decision 0073 の 2・0075）。Memory Worker の KV は 4 GiB のまま、Embedding と Reranker は GPU のまま。1 が合格すれば、0.51・4 並列で足りる見込み（報告の 3）。
- Orchestrator の `max_parallel_nodes`（既定 4、Decision 0021）は変えない。並列を固定値で減らすのではなく、3 の Admission で動的に抑える（要件の「並列 Agent 数を固定値にしない」）。
- Main の `--max-model-len` は 131,072 のまま（Decision 0083 の 12 が委ねた点）。1 の後の Prompt の最大は約 84k で足りる。262,144 にしても、1 つの Request が共存時の Pool（305k）の大部分を使い、Admission の上限（Coding 260.8k）を超えるので使えない。
- 1 が不合格だったときの次の手（別の Decision で提案する）: (a) Memory Worker の KV を 2 GiB にして Main を 0.53 にする（Memory Worker の KV の使用の最大は 3.2〜4.5%）、(b) Embedding と Reranker の CPU での Latency を GPU を使わずに測り、許せる範囲なら CPU へ移して Main を 0.69 にする。

### 5. 実装は承認と 2 の結果の後に、別の PR で行う

- PR 1: `ReasoningHistoryPolicy` に `DropOlderReasoning`（1）を足す（Decision 0083 の 11 の 1 の PR の範囲。既定を変えるのは 2 の合格の後）。GPU なしの Test のみ。
- PR 2: Scheduler の予約の更新と `kv_usage` の配線（3）。GPU なしの Test と、Admission を通した共存の確認 Run の計画（Run は Human の許可の後）。
- Deployment の設定（Footprint、`restore_margin_bytes`、vLLM の引数）は Decision 0075 のとおり別の Issue のまま。

## 代替案

- **Reasoning を残し、Main の割り当てを足す（Embedding と Reranker を CPU へ、0.69）**: KV は足りる（Pool 約 576k）が、Retrieval の Latency が分からず、Interactive の Memory の検索が遅くなりうる。縮退の 3 段目（0037）を常態にすることになる。1 が不合格のときの候補として残す。
- **並列を 3 に固定する**: 要件に反し、短い Task が多いときにも並列を減らす。3 の Admission で動的に抑える方を採る。
- **Reasoning をすべて外す（`keep_last=0`）**: Prefix cache への影響が最も小さい（約 1.4 倍）が、直前の Step の考えまで消えるため振る舞いの変化が最も大きいと見込む。Prompt の減り方は `keep_last=2` とほぼ同じ。採らない。
- **Reasoning を Prompt の大きさに関係なく外す（32k の閾値なし）**: Prompt の最大は同じで、短い Task の振る舞いまで変える。採らない。
- **`reasoning_effort` を下げる**（Template は `xhigh`（既定）/ `medium` / `low` を受け付ける）: 出力そのものが減り所要も短くなりうるが、Decision 0074 の選定は既定の `xhigh` で測っており、正解数への影響が 1 より大きいと見込む。この Decision では提案しない。
- **KV Cache を FP8 にする**（`--kv-cache-dtype fp8`）: 同じ割り当てで Pool が約 2 倍（約 610k）になるが、Main の Model の構成の変更（0037 の 6 で人が決める）で、精度の測り直しに 1 と同じ規模の GPU の Run が要る。1 が不合格のときの候補として残す。
- **Node の開始時に、伸びた後の大きさ（例: p90 の 107k）を予約する**: 伸びの途中で待つことはないが、短い Task にも大きく予約し、4 並列でも 2 つしか入らない。採らない。
- **実行中の Node の予約の増加も上限で待たせる**: 4 つの Agent が互いに待って止まりうる（増加を待つ Node は予約を返さない）。採らない。

## リスク

- 1 の見積もりは、Reasoning を外しても Model が同じ手を打つと仮定している。実際には Step 数・正解が変わりうる（2 の Run で測る）。
- 3 の再現は Trace の時刻から組み立てたもので、実測の 95% 以上の時間の約 2 倍に出る（安全側）。並列を減らしたときの速さの変化は入れていない。
- Prefix cache に乗らない Prefill が増える分の所要の変化は未測定（2 の Run で記録する）。
- 3 で実行中の Node の予約が上限を超えている間は、Interactive は Admission を通っても vLLM の中で待ちうる（Pool の空きは vLLM が決める。超えた分を Interactive の Admission で数えないため）。1 と組み合わせれば Pool に収まる見込み。
- Interactive の Request の応答の速さは、どの案でも測っていない。

## 決めてほしいこと

1. **Main の Local Agent の既定の Reasoning の履歴の方針を `DropOlderReasoning(keep_last=2, min_prompt_tokens=32_768)` にする（2 の Run に合格した場合に限る）**（1）か。推奨: はい。代わりの案は、残したまま（`KeepAllReasoning`）にして 4 の割り当ての変更で解く。
2. **1 の正解数への影響を確かめる Run（paw-seed-v2 の 49 Task × 3 回、単独 0.90、GPU を約 13 時間）を行い、Resolved@3 36 以上かつ平均 36.0 以上、かつ KV の条件（Prompt の最大の p90 80k 以下・`context_exhausted` 0 回・再現で Pool の 95% 以上が 1% 以下・所要 1.15 倍以下）で合格とする**（2）か。推奨: はい（GPU を使う日時は Human が決める）。
3. **Scheduler が会話の伸びを予約に反映し、実行中の Node の予約の増加は上限を超えても待たせず、新しい Node だけを上限で待たせる。上の Class の Admission では、下の Class の予約をそれぞれの上限まで、かつ合計を下の Class の中で最も高い上限までとして数える。vLLM の `/metrics` の KV の使用率を `kv_usage` に配線する**（3。Decision 0037 の 2 の「`kv_safety` までしか予約できない」を新しい Node の Admission に限る形に `Supersedes`）か。推奨: はい（1 の結果に関係なく行う）。
4. **共存時の Main 0.51・Memory Worker の KV 4 GiB・Embedding / Reranker は GPU・`max_parallel_nodes` 4・`--max-model-len` 131,072 は変えない**（4）か。推奨: はい（1 が不合格なら、Memory Worker の KV を 2 GiB にする案と Embedding / Reranker の CPU の Latency を測る案を別の Decision で提案する）。
5. **実装は承認と 2 の結果の後に、`ReasoningHistoryPolicy` の PR と Scheduler の PR に分けて行う**（5）か。推奨: はい。

## 承認後の扱い

承認されたら `Approval` に記録し、Status を Approved に改める。2 の Run の結果は報告（`docs/benchmarks/paw-017-kv-context-qwen38-2026-10.md`）に追記し、1 の既定の変更はその結果で決める。
方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。
