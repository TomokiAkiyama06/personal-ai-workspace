# Qwen3.8-27B-FP8 を Main にしたときの共存時の KV / Context の不足の分析（2026-10）

- Issue: [#200](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/200)（[Decision 0074](../decisions/0074-seed-v2-and-qwen-27b-comparison.md) の後続）。提案は [Decision 0076](../decisions/0076-main-kv-context-under-coexistence.md)（Proposed）
- 材料（どれも既にある Run の記録で、この分析のために GPU は使っていない）:
  - 10-07 の共存の確認 Run（Qwen3.8-27B-FP8、0.51、paw-seed-v1 の 24 Task × 3 回。[#180 の報告](https://github.com/TomokiAkiyama06/personal-ai-workspace/pull/210) の `docs/benchmarks/paw-017-coexist-qwen38-2026-10.md`）: Trace と vLLM の `/metrics`（5 秒ごと）。`/data/results/paw-bench-2026-10-07`
  - 10-05 / 10-06 の paw-seed-v2 の比較 Run（Qwen3.8-27B-FP8、単独 0.90、49 Task × 3 回。[paw-seed-v2 の報告](paw-017-seed-v2-2026-10.md)）: Trace。`/data/results/paw-bench-2026-10-05`
- 分析の Script と出力は Repository の外の `/data/results/paw-analysis-200-2026-10-08`（`load.py`・`a_tasks.py`・`b_sim.py`・`c_grid.py`・`d_resv.py`・`e_thr.py`・`f_resv16k.py`、出力は `output.txt`）。Trace は Hidden check の内容を含むため Repository に入れない（Decision 0041 の 4）

## 1. 何が足りないか

- 共存時の Main の KV Pool は **305,081 token**（0.51、19.4 GiB）。10-07 の Run は 3 回とも Pool を使い切り（最大 99.8〜100%）、Preemption 計 40 回・待ちの最大 2 Request・約 1.3 倍の遅さになった（正解数は変わらない）。
- Trace の各 Step には vLLM の `usage`（`prompt_tokens`・`completion_tokens`・`completion_tokens_details.reasoning_tokens`）がある。

| | 1 Step の出力（中央値 / 平均） | 出力のうち Reasoning | Task ごとの Prompt の最大（中央値 / p90 / 最大） | 最大の Prompt のうち、それまでの Reasoning の割合（中央値 / p90） |
| --- | ---: | ---: | ---: | ---: |
| v2（10-05、単独、147 Task-run） | 236 / 852 token | 62.5% | 67.4k / 106.8k / 114.6k | 29% / 46% |
| v1（10-07、共存、72 Task-run） | 217 token | 69.5% | 58.6k / 97.0k / 105.0k | 28% / 47% |

- 前の報告の「1 Step の出力の中央値 766 token」は Task ごとの平均の中央値（同じ Trace で 766）。Step ごとの中央値は 236 token で、少数の長い Reasoning の Step が平均を押し上げている。
- **Reasoning が履歴に残る仕組み**: Harness は `reasoning_content` を Assistant の Message に戻す。Qwen3.8-27B-FP8 の Chat template（`chat_template.jinja`）は `preserve_thinking` が未指定なら全ての Assistant の Reasoning を Prompt に描く。`preserve_thinking: false` にしても、Reasoning を外すのは最後の User の質問より前の Assistant だけで、1 つの Task の Tool loop（最初の User の後に Assistant と Tool が続く）では何も外れない。**外すなら Adapter 側で `reasoning_content` を消す必要がある**（Decision 0083 の 3 の `ReasoningHistoryPolicy`）。
- Prompt の伸びが Reasoning を含むことは Trace でも確かめた: 次の Step の Prompt − 今の Step の Prompt − 今の Step の出力 が負になったのは 5,826 回のうち 1 回だけ（Reasoning が Prompt に戻っている）。

## 2. 古い Reasoning を外したときの Prompt の見積もり

方法: Step i の呼び出しで、直近 `keep_last` 個より古い Assistant の Reasoning token（`reasoning_tokens`）を Prompt から引く（Template は空の `<think></think>` を残す）。**Model の振る舞いは変わらないと仮定した見積もり**で、外した後に Model が別の手を打つ（Step 数・正解が変わる）ことは測っていない（4 の Run で測る）。

| 方針 | v2: Prompt の最大（中央値 / p90 / 最大） | v1 共存: 同（中央値 / p90 / 最大） | Cache に乗らない Prefill（1 呼び出しあたり、v2） |
| --- | ---: | ---: | ---: |
| 残す（今） | 67.4k / 106.8k / 114.6k | 58.6k / 97.0k / 105.0k | 778 token（1.0 倍） |
| 直近 4 Step を残す | 48.5k / 71.7k / 92.3k | 37.5k / 76.9k / 85.9k | 6,831（8.8 倍） |
| **直近 2 Step を残す** | **47.7k / 71.0k / 83.6k** | 36.5k / 73.2k / 85.0k | 4,113（5.3 倍） |
| 直近 1 Step を残す | 46.5k / 70.6k / 83.2k | 36.1k / 72.8k / 84.9k | 2,623（3.4 倍） |
| すべて外す | 45.8k / 69.0k / 82.5k | 35.4k / 70.8k / 84.8k | 1,080（1.4 倍） |
| **直近 2 Step を残す、Prompt が 32,768 token を超えてから** | **47.7k / 71.0k / 83.6k** | 36.5k / 73.2k / 85.0k | **3,192（4.1 倍）** |

- **直近 2 Step を残すだけで、Prompt の最大の p90 は 107k → 71k（−34%）、最大は 115k → 84k** になる。`keep_last` を 4 → 0 と減らしても、それ以上はほとんど減らない（減る分の大部分はずっと古い Step の Reasoning）。
- 32,768 token を超えてから外す形にすると、Prompt の最大は同じで、外す対象の Task は 147 のうち 119（解けた 111 Task-run のうち 28 は 32k に達しないので影響を受けない）、外す呼び出しは 66% になる。32k 未満では 4 並列でも Coding の予約の上限（約 260.8k）に収まるので、外す理由がない。
- **Prefix cache への影響**: 外す境界が毎 Step 進むと、その位置より後ろの Prompt を計算し直す（vLLM の Prefix cache の Hit が減る）。今は 1 呼び出しあたり約 0.8k token を新しく計算しているのが、直近 2 Step を残す形で約 3.2k〜4.1k token になる。1 呼び出しの出力の平均は約 850 token（約 8 秒）なので、Prefill の数 k token は秒未満の追加と見込むが、**実測していない**（4 の Run で Prefix cache の Hit 率と所要を記録する）。「すべて外す」は Cache に優しい（毎回外れるのは直前の Step の Reasoning だけ）が、直前の考えまで消えるため振る舞いの変化が最も大きい。
- `context_exhausted`（v2 で 9 回、Prompt が 114,689 token を超えた）: 外した場合の最大は 84k で、上限に達しない見込み。ただしその Task はどれも解けていない Task で、上限に当たらなくなると Step 数・時間が延びる（60 Step か 45 分で終わる）。

## 3. 4 並列の KV の必要量の再現と、対策ごとの見積もり

### 再現の方法と確からしさ

- 各 Task の開始時刻（Trace の最後の書き込みの時刻 − 最後の Step の `t`）と Step ごとの完了時刻から、「各 Agent が今の Context（その Step の Prompt + 出力の半分）を KV に持つ」として、5 秒ごとに 4 つの Agent の和を出した。
- 10-07 の実測（`/metrics` の `kv_cache_usage_perc`）と比べると、平均の使用率は再現 0.61〜0.64 / 実測 0.59、時系列の相関は **0.88〜0.89**。Pool の 95% 以上の時間は再現 4.1〜8.5% / 実測 2.3〜3.4% で、**再現の方が約 2 倍厳しい**（実測は 100% で頭打ちになり、Preemption で伸びが遅れる）。以下の数字は安全側の見積もりとして読む。
- 3 並列・2 並列は、各 Task の所要を変えずに、同じ順に 3 つ・2 つの枠へ詰め直した（並列が減ると速くなる分は入れていない）。

### KV の必要量（Pool の 95% 以上になる時間の割合）

Pool の大きさ（Footprint の勘定は Decision 0075 と同じ。Main の予算を超える分 2.18 GiB は変わらないと仮定）:

| Main の割り当て | 何を変えるか | Main の KV Pool |
| --- | --- | ---: |
| 0.51（今） | なし | 305k token |
| 0.53 | Memory Worker の KV を 4 → 2 GiB | 約 335k |
| 0.55 | Embedding を CPU へ | 約 365k |
| 0.69 | Embedding と Reranker を CPU へ | 約 576k |

| 並列 | Reasoning | 必要量の Peak（v1 共存 / v2） | 0.51 で 95% 以上 | 0.53 | 0.55 | 0.69 |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 4 | 残す（今） | 320k / 382k | **6.3% / 10.1%** | 0.4% / 5.2% | 0.0% / 2.0% | 0% / 0% |
| 4 | 直近 2 Step | 246k / 266k | **0% / 0%** | 0% / 0% | 0% / 0% | 0% / 0% |
| 4 | 直近 2 Step（32k から） | 247k / 267k | **0% / 0%** | | | |
| 3 | 残す | 319k / 374k | 0.0% / 0.7% | 0% / 0% | 0% / 0% | 0% / 0% |
| 2 | 残す | 268k / 306k | 0% / 0% | | | |

- **Reasoning を直近 2 Step にすれば、0.51・4 並列のまま、v2 の長い Task でも Pool の 95% に達しない**（Peak 267k、Pool 305k の 88%）。
- Main の割り当てを増やす案は、Memory Worker の KV を減らすだけ（0.53）では v2 で足りず（5.2%）、Embedding を CPU へ（0.55）でも 2.0% 残る。Embedding と Reranker の両方を CPU へ（0.69）なら足りるが、CPU での Retrieval の Latency は測っていない（今の GPU での p95 は約 460 ms。Reranker は 4B の Model で、30 件の並べ替えを CPU で行う）。
- 3 並列にすると v1 では足り、v2 では 0.7% 残る。

### Scheduler が会話の伸びを予約に反映した場合

各 Agent の予約を「今の Prompt + その Request の応答の上限（`max_tokens`）」とし、Node の開始から終わりまで（Tool の実行中も）持つとして、Coding の予約の上限（Pool × `kv_safety` 0.90 × Coding 0.95 = 260.8k token）を超える時間を数えた。超えている間は、新しい Node の Admission が待つ。応答の上限は Harness と同じ 16,384 token を主に示し、Runtime が 8,192 に絞った場合も参考に示す（Decision 0039 の 3: Runtime が `max_tokens` を渡せるときはその値で予約する）。

| 並列の上限 | Reasoning | 予約の Peak（v1 共存 / v2） | 上限を超える時間（`max_tokens` 16,384、v1 共存 / v2） | 参考: `max_tokens` 8,192 |
| ---: | --- | ---: | ---: | ---: |
| 4 | 残す | 455k / 472k | **51.1% / 49.2%** | 32.7% / 31.2% |
| 4 | 直近 2 Step（32k から） | 366k / 349k | **23.6% / 10.3%** | 6.8% / 3.1% |
| 3 | 残す | 324k / 357k | 14.2% / 12.7% | 3.3% / 5.6% |
| 3 | 直近 2 Step（32k から） | 262k / 277k | 0.4% / 0.5% | 0% / 0% |

- Reasoning を残したまま伸びを予約に反映すると、Run の約半分の時間は新しい Node が待つ（実質 3 並列に近い）。直近 2 Step にすると待つ時間は 10〜24% に減る。応答の上限を 8,192 にすれば 3〜7% だが、Qwen3.8 は 16,384 に達する応答もある（Decision 0039 の実測）ので、上限を絞るかどうかはこの分析では決めない。
- 予約は応答の上限まで数えるので、実際の KV の使用（上の表の Peak 267k < 305k）より大きい。予約の Peak が上限を超えるのは、実行中の Node の伸びを止めない（Decision 0076 の 3 の案）ため。

## 4. 測っていないこと

- **古い Reasoning を外したときの正解数**（Model の振る舞いの変化）。GPU で paw-seed-v2 を走らせる必要がある（Decision 0076 の 2 の Run の計画）。
- Prefix cache の Hit 率の変化と、それによる所要の変化。
- Embedding / Reranker を CPU に置いたときの Retrieval の Latency（GPU は使わずに測れる）。
- Interactive の Request（Chat）の応答の速さ。
- Main の割り当てを 0.51 から変えたときの、予算を超える分（今は 2.18 GiB）。
