# GPU / Compute Resource Scheduler の暫定値を PAW-017 / PAW-018 / PAW-019 の実測で見直す（Decision 0037 の較正）

- Status: Approved
- Date: 2026-09-30
- Scope: Issue [#14](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/14)（PAW-017: Main Coding Model 比較 Run）の実測値。対象は [Decision 0037](0037-gpu-compute-scheduler.md) が「実測に基づかない暫定値」とした数値と、[Decision 0042](0042-gpu-free-vram-admission.md) が 0037 と同じとした Headroom
- Supersedes: なし。[Decision 0037](0037-gpu-compute-scheduler.md)（Approved）・[Decision 0042](0042-gpu-free-vram-admission.md)（Approved）は書き換えない。0037 の「リスク」が「Benchmark（PAW-017 / PAW-019）で Model と Runtime が決まったら見直す」とした値への答えで、承認されたら値だけを変える（方針は変えない）
- Approval: 2026-09-30、Humanが作業Session内で、判断点ごとの説明（推奨つき）を受けたうえで直接回答して承認（判断が必要な点 1〜5 の全点。末尾の「承認時の決定」）

## 背景

0037 は、Safety Headroom（4 GiB と GPU の 5% の大きい方）、Model の操作（Load / Unload）の上限 300 秒（`DEFAULT_CONTROL_TIMEOUT_SECONDS`）、Probe の古さの許容 15 秒、Context の見積もり（Byte ÷ 3 + 回答の予備 8,192 Token）などを暫定値とし、Benchmark の後に見直すとした。
Footprint（weights + KV Cache Pool + Runtime Buffer + Temporary Workspace）は Admin が与える値で、Scheduler は測らない（0037 の 3）。

2026-09-28 と 2026-09-30 の比較 Run（詳細は [PAW-017 比較 Run の報告](../benchmarks/paw-017-main-coding-2026-09.md)）で、次を測った。

- GPU: NVIDIA RTX PRO 6000 Blackwell Workstation Edition（97,887 MiB）1 枚。Runtime は vLLM 0.30.0（torch 2.13.0+cu130）と llama.cpp（server-cuda の Container）。
- Model ごとの Load 時間、Load 直後と Run 中の GPU 全体の使用量（1 秒ごと）、Unload の時間、KV Cache の Pool と実際の使用率、Request ごとの回答 Token。
- Model ごとの Tokenizer で数えた Byte / Token（日本語の docs、`apps/backend` の Python、Seed の Issue 文、Memory Worker の入力）。
- 0037 の Probe（`nvidia-smi` の 2 つの Query）の所要時間（coding Run の最中に 30 回）。
- `/data`（HDD）の順次読み出しの速さ。

## 実測

### Load / Unload と VRAM

| Model | Runtime | `gpu-memory-utilization` の予算（GiB） | Load 後（GiB） | Run 中のピーク（GiB） | ピーク − 予算（GiB） | Load（秒） | Unload（秒） |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| gpt-oss-120b | vLLM | 87.9（0.92） | 87.4 | 95.0 | +7.0 | 412 | 10 |
| Qwen3.8-27B-FP8 | vLLM | 86.0（0.90） | 83.4 | 94.4 | +8.4 | 180 | 1 |
| Devstral-Small-2-24B | vLLM | 86.0（0.90） | 84.6 | 94.0 | +8.0 | 197 | 2 |
| Qwen3-Coder-Next-FP8 | vLLM | 90.8（0.95） | 90.2 | 91.0 | +0.2 | 404 | 2 |
| KAT-Coder-V2.5-Dev | vLLM | 87.9（0.92） | 87.3 | 88.4 | +0.4 | 308 | 2 |
| Nemotron-3.5-Lightning-30B-A3B-NVFP4 | vLLM | 86.0（0.90） | 85.3 | 87.2 | +1.2 | 142 | 1 |
| Qwen3.6-35B-A3B-FP8 | vLLM | 86.0（0.90） | 85.6 | 86.3 | +0.3 | 69 | 2 |
| Qwen3-Coder-30B-A3B-Instruct | vLLM | 86.0（0.90） | 85.7 | 85.7 | -0.3 | 91 | 1 |
| Qwen3.5-27B-FP8 | vLLM | 86.0（0.90） | 83.4 | 85.6 | -0.4 | 189 | 2 |
| gpt-oss-20b | vLLM | 86.0（0.90） | 85.4 | 85.5 | -0.5 | 102 | 1 |
| Qwen3.6-27B-FP8 | vLLM | 86.0（0.90） | 83.4 | 85.5 | -0.6 | 211 | 2 |
| Qwen3-Coder-Next-Q5_K_M-llamacpp | llama.cpp | - | 65.8 | 65.9 | - | 224 | 2 |

- vLLM の `gpu-memory-utilization` × GPU 全体を予算とすると、12 Run のうち 3 Run で GPU 全体の使用量のピークが予算を **7.0〜8.4 GiB 上回った**。09-30 の Run は Process ごとの使用量も 5 秒ごとに記録し、Devstral の超過（+8.0 GiB）は **vLLM の EngineCore 自身の分**だった（Run 中に GPU にいた Process はそれだけ）。09-28 の Qwen3.8-27B-FP8（+8.4 GiB、35 分続いた）と gpt-oss-120b（+7.0 GiB、3 秒だけ）は GPU 全体の使用量しか記録しておらず、他の Workload の分を含むかは分からない。原因は特定していない（vLLM が KV Pool の外に取る一時領域と考えられる）。
- Load の時間は、HDD（281 MB/s）からの初回の Load で最長 **412 秒**（gpt-oss-120b、Weight 66 GiB）。Page cache が温まっていると 1〜2 分。Unload は 1〜10 秒。

### Context の見積もり

| Tokenizer | 日本語の docs | Python | Seed の Issue 文（日本語） | Memory Worker の入力 |
| --- | ---: | ---: | ---: | ---: |
| Qwen3.5 / 3.6 / 3.8、KAT-Coder-V2.5 | 3.99 | 4.13 | 4.14 | 3.90 |
| Qwen3-Coder-Next | 3.57 | 4.41 | 3.74 | 3.91 |
| Devstral Small 2、Nemotron 3.5 | 3.46 | 4.16 | 3.55 | 3.74 |
| gpt-oss | 3.59 | 4.42 | 3.81 | 3.84 |

（単位: Byte / Token。小さいほど同じ文章が多くの Token になる）

- すべての Tokenizer・文書で 3.46 以上。0037 の「Byte ÷ 3」は Token を **最大 15%** 多めに見積もり、少なく見積もった例はない。
- 1 回の回答（Request ごとの `completion_tokens`）の p95 は Model ごとに 777〜3,788 Token。最大は各 Run の上限 16,384 に達した（Reasoning が長い Model）。

### Probe

- `nvidia-smi` の 2 つの Query の所要は p50 35.5 ms、最大 41.7 ms（n = 30、coding Run の最中）。0037 の読み取り間隔（5 秒）と古さの許容（15 秒）に対して十分に小さい。

## 提案

### 1. Safety Headroom は「4 GiB と 5% の大きい方」のまま

- 実測で Headroom 自体を広げる根拠はない（Probe は十分速く、Load / Unload で VRAM が残った例はない: 全 Run で Unload 後の使用量は 20 MiB）。
- ただし **Footprint は「`gpu-memory-utilization` × 全体」ではなく、Benchmark で測ったピークに Margin を足した値**を Admin が与える。vLLM では、予算を超える分（Sampler、長い Prefill の一時領域、CUDA Graph の外の Allocation）が数 GiB 出る。Footprint を予算で与えると、0037 の 3 のとおり `committed` は実使用で数えるので Admission は安全側に寄るが、Load の判断（予約で数える）は誤る。
- 推奨する与え方: `Footprint = max(Run 中の GPU 使用量のピーク − Load 前の使用量) + 2 GiB`。下の「採用時の値」は 0040 の採用 Model の実測から出す。

### 2. Model の操作の上限を 300 秒から 900 秒へ

- HDD からの初回の Load が 412 秒かかった（gpt-oss-120b）。300 秒では、正常な Load を失敗（`FAILED`、60 秒後に再試行）として扱い、同じ Load を繰り返す。
- 900 秒は、実測の最長の約 2 倍。Load 中も Probe を読み続け、GPU にある Main の仕事は止めない（0037 の 5）ので、長くしても Interactive は待たない。

### 3. Context の見積もりは「Byte ÷ 3 + 8,192」のまま

- Byte ÷ 3 は全 Tokenizer で安全側（最大 15% 多め）。3.5 に上げると Devstral / Nemotron の日本語（3.46）で少なく見積もる。
- 回答の予備 8,192 は、p95（最大 3,788）を上回る。上限まで出す Request（16,384）は少ないが存在するので、Runtime が `max_tokens` を渡せる場合はその値を使う（0037 の 9 の「Runtime が正確に数えられるなら、自分の `estimate` を渡す」のとおり）。

### 4. Probe の読み取り間隔 5 秒・古さの許容 15 秒のまま

- Probe の所要（最大 42 ms）は間隔の 1% 未満。

### 5. 変えない値

- `kv_safety` 90%、Class の上限（100 / 95 / 85 / 70%）、縮退の 5 段目（Context を 50%）、再試行の 60 秒、Exclusive の確認 60 秒、Node の待ち 600 秒は、今回の Benchmark（1 つの Agent Harness を並列 4 で走らせる）では検証できない。KV Cache の実際の使用率のピークは Pool の 1.3〜41.7% で、並列 4 では Pool が足りなくなった Run はない。これらは本番の運用の観測（Observability の Metric）で見直す。

## 代替案

- **Headroom を 8 GiB（予算の超過分）に広げる**: 超過は Footprint の過小申告として扱うのが 0037 の勘定の考え方で、Headroom で吸収すると Main が常駐していないとき（Exclusive など）にも一律に 8 GiB を空ける。採らない。
- **Model の操作の上限を Model ごとに設定する**: より正確だが、設定が増える。0037 の `ComputeConfig` は 1 つの値を持つ。まず 1 つの値（900 秒）にし、Model ごとの値が要れば別の Decision にする。
- **Byte / Token を Model ごとに設定する**: 0037 の 9 のとおり、Runtime が数えられれば `estimate` で渡せる。共通の既定値は最も小さい比率より小さい 3 のままでよい。

## リスク

- 09-28 の VRAM のピークには、他の Workload（同じ GPU の別の User）の分が混ざっている可能性がある（Process ごとの内訳を記録していない）。Footprint の推奨は安全側（大きめ）に出る。
- 今回の Load の時間は `/data`（HDD）からで、[BENCHMARK_EVALUATOR.md](../BENCHMARK_EVALUATOR.md) の「比較対象 model は原則 NVMe SSD へ配置する」に反する（NVMe の空きは足りるが、Stage しなかった）。本番の運用も HDD の Model Store から Load する（同じ文書）ので、900 秒は本番の値として使える。
- Host の RAM: 2026-09-30 の Run で、FlashInfer の JIT（`ninja` の既定の並列数 = CPU 数 + 2）が 27 個の `cicc`（計 約 75 GiB）を同時に走らせ、Host の RAM が尽きて Machine が再起動した。これは Scheduler の VRAM の勘定の外の問題で、Runtime の設定（`MAX_JOBS` で JIT の並列数を絞る、JIT の要らない Backend を選ぶ）で防ぐ。Deployment の手順（`DEPLOYMENT_UPDATE.md`）に入れるかは別の Issue で扱う（決めてほしいこと 4）。

## 決めてほしいこと

1. **Safety Headroom は「4 GiB と 5% の大きい方」のまま、Footprint は `gpu-memory-utilization` ではなく実測のピーク + 2 GiB で与える**（1）でよいか。推奨: はい。
2. **Model の操作（Load / Unload）の上限を 300 秒から 900 秒にする**（2）でよいか。推奨: はい。
3. **Context の見積もり（Byte ÷ 3 + 8,192）と Probe の間隔・古さの許容（5 秒・15 秒）は変えない**（3・4）でよいか。推奨: はい。
4. **Runtime の JIT の並列数の上限（`MAX_JOBS`）と、Host の RAM の確認を Deployment の手順に入れる Issue を作る**（リスク）か。推奨: はい（この Decision では値を決めない）。
5. **検証できなかった値（5）は変えず、本番の観測で見直す**でよいか。推奨: はい。

## 承認後の扱い

承認されたら `Approval` に記録し、Status を Approved に改める。2 は `apps/backend/paw_backend/compute/limits.py` の `DEFAULT_CONTROL_TIMEOUT_SECONDS` を 900 にする変更（別の PR）で反映する。1 の Footprint は Admin の設定で、Code は変えない。
方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。

## 承認時の決定（2026-09-30）

Human は、作業 Session で判断が必要な点について推奨つきの説明を受け、直接回答して承認した（判断が必要な点 1〜5 の全点）。4 のデプロイ手順の Issue は #182。
