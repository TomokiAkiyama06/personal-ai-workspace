# PAW-017 Main Coding Model 比較 Run の報告（2026-09）

- Issue: [#14](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/14)（PAW-017）
- 実施: 2026-09-28（8 Model）と 2026-09-30（09-28 に起動できなかった 4 Model の再開）
- Dataset: paw-seed-v1（[Decision 0041](../decisions/0041-seed-benchmark-dataset.md)、24 Task: Historical 11 / Spec 6 / Injected Bug 7）
- 判断の提案: [Decision 0040](../decisions/0040-main-coding-model-selection.md)（採用 Model）、[Decision 0039](../decisions/0039-compute-scheduler-calibration.md)（Scheduler の暫定値の較正）。どちらも Proposed
- 生の結果（Trace・Patch・Server の Log・VRAM の記録）は Repository に入れない（Hidden check の内容を含むため。Decision 0041 の 4）。Server の `/data/results/paw-bench-2026-09-28` と `/data/results/paw-bench-2026-09-30` にある

## 1. 条件（全 Model で同じ）

- **Harness**: 1 つの Agent loop（System prompt・Tool は `bash` / `str_replace` / `write_file` / `submit` の 4 つ・Tool の timeout 300 秒・出力の切り詰め 12,000 文字）。最大 60 step、Task ごとの wall clock 45 分、Prompt は 120,000 token まで、1 回の回答は 16,384 token まで。4 Task を並列に走らせる。Sampling は各 Model の `generation_config` の既定（Harness から温度などを渡さない）
- **Sandbox**: Task ごとに `--network none`・4 CPU・8 GiB の Container。候補が見られるのは starting commit の clone だけ
- **Evaluator**: 候補の差分を Host の新しい clone に当て、Hidden check（acceptance / regression）を `benchmarks.seed_dataset`（PR #139 の head `1d0f683` を固定した snapshot）で実行。PostgreSQL が要る check は専用の Container（pgvector）。全 12 Model の全 Patch を同じ Evaluator でもう一度評価し（`reeval`）、Harness の判定と **12 Model × 24 Task のすべてで一致**した。Golden patch の確認（4 Task）も 09-28 と同じ結果
- **Runtime**: vLLM 0.30.0（torch 2.13.0+cu130、FlashInfer 0.6.18.post1）、`--max-model-len 131072 --max-num-seqs 8`、`--gpu-memory-utilization` は 0.90（KAT・gpt-oss-120b は 0.92、Qwen3-Coder-Next-FP8 は 0.95）。Qwen3-Coder-Next の Q5_K_M だけ llama.cpp（server-cuda）
- **GPU**: RTX PRO 6000 Blackwell Workstation Edition（97,887 MiB）1 枚。各 Run の開始前に他の Process が GPU にないこと・空きが足りることを確かめた（他の Workload は止めていない）
- **1 回だけ**（Resolved@1）。Resolved@N は測っていない

### 条件から外れた点

- **NVMe への Stage をしていない**（Issue の受け入れ条件 1）。Weight は `/data`（HDD、281 MB/s）から Load した（Qwen3-Coder-30B-A3B だけ NVMe）。影響は Load 時間だけで、Agent の指標（Resolved・Token・Task の所要）には入らない。
- Model ごとの Runtime の差:
  - KAT-Coder-V2.5-Dev と Qwen3-Coder-30B-A3B（どちらも BF16 の MoE）は `--moe-backend triton`。既定の FlashInfer CUTLASS の MoE は JIT の Build が要り、下の 5 の問題で使えなかった。
  - Devstral Small 2 は Mistral 形式で Load し、vLLM 0.30 と transformers 5.17 の不整合（Pixtral の名前）を Harness 側の Shim で避けた（Text だけ。画像は無効）。
  - Nemotron 3.5 Lightning は `--kv-cache-dtype fp8 --moe-backend marlin --attention-backend TRITON_ATTN --mamba-cache-mode align`。
  - Reasoning parser / Tool parser は Model ごとに適切なもの（Qwen 系は `qwen3` / `qwen3_coder`、gpt-oss は `openai`、Devstral は `mistral`、Nemotron は `nemotron_v3` / `qwen3_coder`）。
- CPU: 同じ Server で他の User の CPU の重い Job が動いていた時間がある（load average 10〜54）。Task の所要時間は参考値。

## 2. 結果

### Resolved@1 と Agent の振る舞い

| Model | Resolved@1 | Historical | Spec | Injected Bug | Hidden test case 通過率 | submit で終了 | 60 step 到達 | Tool error 率 | 24 Task の所要（並列 4） | Task 所要 中央値 | 出力 token 合計 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| KAT-Coder-V2.5-Dev | **14/24**（0.58） | 1/11 | 6/6 | 7/7 | 0.79 | 20 | 2 | 0.005 | 30 分 | 241 秒 | 323k |
| Qwen3.6-27B-FP8 | **14/24**（0.58） | 1/11 | 6/6 | 7/7 | 0.77 | 19 | 5 | 0.000 | 31 分 | 254 秒 | 203k |
| Qwen3.8-27B-FP8 | **14/24**（0.58） | 1/11 | 6/6 | 7/7 | 0.79 | 16 | 6 | 0.003 | 117 分 | 1100 秒 | 751k |
| Qwen3.5-27B-FP8 | **13/24**（0.54） | 1/11 | 5/6 | 7/7 | 0.74 | 15 | 9 | 0.002 | 38 分 | 260 秒 | 225k |
| Qwen3.6-35B-A3B-FP8 | **12/24**（0.50） | 1/11 | 4/6 | 7/7 | 0.76 | 14 | 10 | 0.016 | 15 分 | 106 秒 | 245k |
| Qwen3-Coder-Next-FP8 | **12/24**（0.50） | 1/11 | 4/6 | 7/7 | 0.68 | 3 | 21 | 0.056 | 18 分 | 131 秒 | 219k |
| Qwen3-Coder-Next-Q5_K_M-llamacpp | **12/24**（0.50） | 1/11 | 4/6 | 7/7 | 0.73 | 8 | 16 | 0.053 | 32 分 | 215 秒 | 210k |
| gpt-oss-120b | **12/24**（0.50） | 1/11 | 4/6 | 7/7 | 0.64 | 4 | 11 | 0.126 | 28 分 | 185 秒 | 212k |
| Devstral-Small-2-24B | **12/24**（0.50） | 1/11 | 4/6 | 7/7 | 0.73 | 10 | 14 | 0.008 | 34 分 | 314 秒 | 263k |
| gpt-oss-20b | **10/24**（0.42） | 0/11 | 3/6 | 7/7 | 0.64 | 4 | 8 | 0.171 | 46 分 | 260 秒 | 229k |
| Nemotron-3.5-Lightning-30B-A3B-NVFP4 | **10/24**（0.42） | 1/11 | 2/6 | 7/7 | 0.65 | 13 | 11 | 0.009 | 13 分 | 90 秒 | 313k |
| Qwen3-Coder-30B-A3B-Instruct | **6/24**（0.25） | 1/11 | 1/6 | 4/7 | 0.64 | 21 | 3 | 0.067 | 28 分 | 119 秒 | 322k |


- 「Hidden test case 通過率」は acceptance の test case のうち通った割合の平均（部分点）。
- 生成が長い Model（Qwen3.8-27B-FP8）は Task の所要が他の約 4 倍で、出力 Token も 3 倍。

### 難易度別

| Model | easy（4） | medium（12） | hard（8） |
| --- | ---: | ---: | ---: |
| KAT-Coder-V2.5-Dev | 4 | 9 | 1 |
| Qwen3.6-27B-FP8 | 4 | 9 | 1 |
| Qwen3.8-27B-FP8 | 4 | 9 | 1 |
| Qwen3.5-27B-FP8 | 4 | 8 | 1 |
| Qwen3.6-35B-A3B-FP8 | 4 | 8 | 0 |
| Qwen3-Coder-Next-FP8 | 4 | 8 | 0 |
| Qwen3-Coder-Next-Q5_K_M-llamacpp | 4 | 7 | 1 |
| gpt-oss-120b | 4 | 8 | 0 |
| Devstral-Small-2-24B | 4 | 7 | 1 |
| gpt-oss-20b | 4 | 5 | 1 |
| Nemotron-3.5-Lightning-30B-A3B-NVFP4 | 4 | 6 | 0 |
| Qwen3-Coder-30B-A3B-Instruct | 3 | 3 | 0 |

### Resource

| Model | Weight（GiB） | KV Pool（GiB / token） | Load 後の VRAM（GiB） | Peak VRAM（GiB） | KV 使用率の最大 | 生成速度（平均 / 最大 tok/s、全 Request 合計） | Load（秒） | 最大 Prompt（token） |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| KAT-Coder-V2.5-Dev | 64.7 | 21.1 / 1047k | 87.3 | 88.4 | 31.5% | 191 / 373 | 308 | 114,200 |
| Qwen3.6-27B-FP8 | 27.6 | 54.7 / 860k | 83.4 | 85.5 | 29.1% | 108 / 167 | 211 | 86,034 |
| Qwen3.8-27B-FP8 | 27.6 | 54.7 / 860k | 83.4 | 94.4 | 41.7% | 109 / 162 | 180 | 114,323 |
| Qwen3.5-27B-FP8 | 27.6 | 54.7 / 860k | 83.4 | 85.6 | 21.7% | 106 / 168 | 189 | 71,836 |
| Qwen3.6-35B-A3B-FP8 | 33.4 | 50.7 / 2518k | 85.6 | 86.3 | 7.6% | 281 / 485 | 69 | 68,274 |
| Qwen3-Coder-Next-FP8 | 74.9 | 13.6 / 579k | 90.2 | 91.0 | 31.3% | 228 / 392 | 404 | 81,359 |
| Qwen3-Coder-Next-Q5_K_M-llamacpp | - | - / - | 65.8 | 65.9 | -% | - / - | 224 | 68,119 |
| gpt-oss-120b | 66.1 | 18.7 / 484k | 87.4 | 95.0 | 17.1% | 146 / 391 | 412 | 49,177 |
| Devstral-Small-2-24B | 23.3 | 60.4 / 396k | 84.6 | 94.0 | 34.6% | 128 / 193 | 197 | 51,592 |
| gpt-oss-20b | 13.8 | 69.2 / 2686k | 85.4 | 85.5 | 3.7% | 192 / 452 | 102 | 50,755 |
| Nemotron-3.5-Lightning-30B-A3B-NVFP4 | 17.8 | 65.8 / 18052k | 85.3 | 87.2 | 1.3% | 416 / 730 | 142 | 79,567 |
| Qwen3-Coder-30B-A3B-Instruct | 56.9 | 26.8 / 293k | 85.7 | 85.7 | 34.9% | 195 / 296 | 91 | 64,819 |


- Peak VRAM は GPU 全体の使用量（1 秒ごと）。09-30 の 4 Model は Process ごとの使用量も記録し、Run 中に GPU にいたのは自分の Server だけだった。Devstral の Server は `gpu-memory-utilization` 0.90 の予算（86.0 GiB）を **8.0 GiB** 超えた（94.0 GiB）。09-28 の Qwen3.8-27B-FP8（94.4 GiB）・gpt-oss-120b（95.0 GiB、3 秒だけ）は内訳を記録していない。Decision 0039 の 1 の根拠。
- 生成速度は vLLM の Log の 10 秒ごとの値の平均（全 Request の合計）。

### Task ごと

| Task | KAT-Coder-V2.5-Dev | Qwen3.6-27B-FP8 | Qwen3.8-27B-FP8 | Qwen3.5-27B-FP8 | Qwen3.6-35B-A3B-FP8 | Qwen3-Coder-Next-FP8 | Qwen3-Coder-Next-Q5_K_M-llamacpp | gpt-oss-120b | Devstral-Small-2-24B | gpt-oss-20b | Nemotron-3.5-Lightning-30B-A3B-NVFP4 | Qwen3-Coder-30B-A3B-Instruct |
| --- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| bug-01-ndcg-ideal-cutoff | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| bug-02-json-duplicate-keys | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| bug-03-latency-percentile | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| bug-04-capture-truncation-boundary | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | · |
| bug-05-loop-window | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | · |
| bug-06-ndcg-test-expectation | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| bug-07-admin-demotes-admin | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | · |
| hist-01-task-schema-validator | · | · | · | · | · | · | · | · | · | · | · | · |
| hist-02-result-schema-validator | · | · | · | · | · | · | · | · | · | · | · | · |
| hist-03-metrics-collector | · | · | · | · | · | · | · | · | · | · | · | · |
| hist-04-candidate-adapter | · | · | · | · | · | · | · | · | · | · | · | · |
| hist-05-worktree-unreaped-child | · | · | · | · | · | · | · | · | · | · | · | · |
| hist-06-retrieval-benchmark | · | · | · | · | · | · | · | · | · | · | · | · |
| hist-07-memory-worker-benchmark | · | · | · | · | · | · | · | · | · | · | · | · |
| hist-08-memory-read-capability | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | · | ✓ | ✓ |
| hist-09-ssh-git-runner | · | · | · | · | · | · | · | · | · | · | · | · |
| hist-10-admin-project-list | · | · | · | · | · | · | · | · | · | · | · | · |
| hist-11-memory-status-history | · | · | · | · | · | · | · | · | · | · | · | · |
| spec-01-result-summary | ✓ | ✓ | ✓ | ✓ | · | ✓ | ✓ | ✓ | · | · | · | · |
| spec-02-task-semantic-validation | ✓ | ✓ | ✓ | · | ✓ | ✓ | ✓ | ✓ | ✓ | · | · | · |
| spec-03-precision-at-k | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| spec-04-diff-size | ✓ | ✓ | ✓ | ✓ | ✓ | · | · | · | ✓ | · | · | · |
| spec-05-validator-directories | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | · | ✓ | · | ✓ | ✓ | · |
| spec-06-result-builder | ✓ | ✓ | ✓ | ✓ | · | · | ✓ | · | ✓ | ✓ | · | · |


## 3. 読み方

- **上位 3 つが 14/24 で並ぶ**（KAT-Coder-V2.5-Dev、Qwen3.6-27B-FP8、Qwen3.8-27B-FP8）。解けた Task の集合も同じ（24 Task すべてで 3 Model の結果が一致）。次の 13/24 の Qwen3.5-27B-FP8 との差は 1 Task で、1 回の Run では有意とは言えない。
- **Historical の 11 Task は 10 Task をどの Model も解けなかった**（hist-08 だけ 11 Model が解いた）。Golden patch はどれも通るので Evaluator の誤りではないが、この 11 Task は Model の差をほとんど測れていない。順位の差は Spec（6）と Injected Bug（7）の一部で決まっている。部分点（acceptance の test case の通過率）は hist-05（平均 0.86）・hist-07（0.58）・hist-11（0.49）では Model の差が出ている。
- **Injected Bug は 11 Model が 7/7**。Qwen3-Coder-30B-A3B だけ 4/7 で、`submit` までが短く（21 Task で自分から終了）、確かめずに終える傾向がある（Tool error 率も 6.7%）。
- gpt-oss（120b / 20b）は Tool を呼ばずに終わる回答（`no_tool_calls`）が 9 / 11 Task あり、Tool error 率も高い（12.6% / 17.1%）。Harness の Tool 形式（OpenAI の function calling）との相性の可能性がある。
- **速さ**: 同じ 14/24 でも、Qwen3.6-27B-FP8（31 分・出力 203k token）と KAT（30 分・323k token）は近く、Qwen3.8-27B-FP8（117 分・751k token）は遅い。A3B の MoE（Qwen3.6-35B-A3B、Nemotron）は 13〜15 分で最も速いが、Resolved は 12 / 10。
- **VRAM**: KAT は Weight が 64.7 GiB（BF16）で、KV Pool は 21 GiB しか取れない。Qwen3.6-27B-FP8 は Weight 27.6 GiB で KV Pool 54.7 GiB。Memory Worker（Qwen3.5-4B で 13 GiB）や Embedding / Reranker（2〜14 GiB、PAW-018 / PAW-019 の Run）と同じ GPU に置くと、KAT では KV Pool がほぼ残らない。

## 4. 測っていないこと

- **Human correction time**（Issue の受け入れ条件 3、Decision 0041 の 10 の測り方）。人が Patch を直す時間は、この Run では測っていない。Decision 0040 の判断点 5。
- Resolved@N（同じ Model の複数回の Run）、Multi-Repo Task、Review 指摘の修正、並列 Agent 数を変えたときの性能。
- Memory Worker / Embedding / Reranker と同じ GPU に置いたときの Main の性能（上の VRAM は別々の Run の値）。

## 5. Run で起きた問題と直したこと（Harness / 環境。Repository の Code ではない）

| 日時 | 対象 | 症状（rc=4 = Server が起動しない） | 根本原因 | 対処 |
| --- | --- | --- | --- | --- |
| 09-28 | Qwen3-Coder-30B-A3B、KAT、Nemotron、gpt-oss（最初） | Engine の初期化で失敗 | FlashInfer の JIT の Build で `CUDA compiler and CUDA toolkit headers are incompatible`（pip の nvcc 13.4 と pip の CUDA runtime の header 13.0 の不一致を、FlashInfer に同梱の CCCL が拒否） | gpt-oss は Attention を TRITON_ATTN にして回避（09-28）。他は 09-30 に `-DCCCL_DISABLE_CTK_COMPATIBILITY_CHECK` を FlashInfer の JIT に渡した |
| 09-28 | KAT（2 回目） | Weight の Load の途中で Server が消えた（Log に Error なし） | 外から SIGKILL された（09-28 の Kernel の Log は残っておらず、原因は確かめられない） | 09-30 に再実行して完了 |
| 09-28 | Devstral | `PixtralForConditionalGeneration` の検査で失敗 | vLLM 0.30 の `pixtral.py` が transformers 5.17 にない名前（`PixtralRotaryEmbedding` など）を import する | Harness 側の `sitecustomize` の Shim（Text だけ、画像は無効） |
| 09-30 14:23 | Qwen3-Coder-30B-A3B | Server が CUDA Graph の前で 55 分止まり、**Machine が 15:22 に再起動** | Header の対処で JIT の Build が通るようになり、`ninja` が既定の並列数（CPU 数 + 2）で **27 個の `cicc`（計 約 75 GiB）** を同時に走らせた。Host の RAM が尽き、Kernel の OOM Killer が 14:39〜15:11 に Desktop の Process を次々に止め、再起動に至った | JIT の並列数を `MAX_JOBS=4` に絞り、Server を起動する前に Host の空き RAM（32 GiB 以上）を確かめ、Run 中に 8 GiB を下回れば**自分の Server だけ**を止める Watchdog を足した。BF16 の MoE は `--moe-backend triton`（JIT の要らない Backend）を使った |
| 09-30 16:17 | Devstral、Nemotron | JIT の Link で `-lcudart` / `-lcublas` が見つからない | FlashInfer は `$CUDA_HOME/lib64` を Link の検索先にするが、pip の CUDA 13 は `lib/` に版つきの `.so.13` しか持たない | 版なしの名前の Symlink を置いた専用の Directory を `LIBRARY_PATH` に足した（venv は変えていない） |
| 09-30 15:22 | 全体 | 再起動で `/tmp` が消え、Harness の venv を失った | Harness の venv を `/tmp` に置いていた | 同じ Pin（CI の requirements）の venv を `/data/results` の下に作り直した。Golden の確認で Evaluator の結果が同じことを確かめた |

再起動は、この Run が Host の RAM を使い切ったことが原因である（他の User の Process は止めていないが、Desktop の Session は OOM Killer に止められた）。Scheduler（Decision 0037）は VRAM しか見ないので、Runtime の JIT の RAM は Deployment の設定で防ぐ必要がある（Decision 0039 の判断点 4）。
