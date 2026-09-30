# Main Coding Model の採用（PAW-017 の比較 Run の結果から）

- Status: Approved
- Date: 2026-09-30
- Scope: Issue [#14](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/14)（PAW-017: Main Coding Model 比較 Run）。根拠は [PAW-017 比較 Run の報告](../benchmarks/paw-017-main-coding-2026-09.md)
- Supersedes: なし。[MODEL_CANDIDATES.md](../MODEL_CANDIDATES.md) の「Main Coding Agent candidates」と「Decision timing」（最終採用 Model は Benchmark して決める）への答え。[Decision 0002](0002-start-workspace-implementation-before-model-comparison.md) の「Main Coding Model は Benchmark 後に決定する」を満たす
- Approval: 2026-09-30、Humanが作業Session内で、判断点ごとの説明（推奨つき）を受けたうえで直接回答して承認（判断が必要な点 1〜6 の全点。末尾の「承認時の決定」）

## 背景

MODEL_CANDIDATES.md は、採用 Model を公開 Benchmark だけで決めず、この Repository 自身の Task（paw-seed-v1、[Decision 0041](0041-seed-benchmark-dataset.md)）で評価すること、
特に Resolved@1・Human correction time・96 GB の GPU 内で Memory Worker / Reranker などと共存できるかを重視することを定める。
比較 Run は 12 構成（候補 8 Model と追加の 3 Model、Qwen3-Coder-Next は FP8 と Q5_K_M の 2 構成）を、同じ Harness・Prompt・Tool・timeout・Evaluator で 1 回ずつ走らせた。

| 順（12 構成で数える） | Model | Resolved@1 | 24 Task の所要（並列 4） | 出力 token | Weight（GiB） | KV Pool（GiB） |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | KAT-Coder-V2.5-Dev（BF16） | 14/24 | 30 分 | 323k | 64.7 | 21.1 |
| 1 | Qwen3.6-27B-FP8 | 14/24 | 31 分 | 203k | 27.6 | 54.7 |
| 1 | Qwen3.8-27B-FP8 | 14/24 | 117 分 | 751k | 27.6 | 54.7 |
| 4 | Qwen3.5-27B-FP8 | 13/24 | 38 分 | 225k | 27.6 | 54.7 |
| 5 | Qwen3.6-35B-A3B-FP8 | 12/24 | 15 分 | 245k | 33.4 | 50.7 |
| 5 | Qwen3-Coder-Next（FP8 / Q5_K_M） | 12/24 | 18 / 32 分 | 219k / 210k | 74.9 / - | 13.6 / - |
| 5 | gpt-oss-120b | 12/24 | 28 分 | 212k | 66.1 | 18.7 |
| 5 | Devstral Small 2 24B | 12/24 | 34 分 | 263k | 23.3 | 60.4 |
| 10 | gpt-oss-20b | 10/24 | 46 分 | 229k | 13.8 | 69.2 |
| 10 | Nemotron 3.5 Lightning 30B-A3B NVFP4 | 10/24 | 13 分 | 313k | 17.8 | 65.8 |
| 12 | Qwen3-Coder-30B-A3B-Instruct | 6/24 | 28 分 | 322k | 56.9 | 26.8 |

上位 3 つは同じ 14 Task を解いた（24 Task すべてで結果が一致）。Historical の 11 Task は 10 Task をどの Model も解けず、差は Spec と Injected Bug の一部で決まっている（報告の 3）。

## 提案

### 1. Main Coding Model は Qwen3.6-27B-FP8

- Resolved@1 は最上位（14/24）で、同点の 3 つの中で **出力 Token が最も少なく**（203k、KAT の 63%、Qwen3.8 の 27%）、Tool error が 0、`submit` で終えた Task が 19。
- **共存**: Weight が 27.6 GiB で、Memory Worker（Qwen3.5-4B で約 13 GiB）と Embedding / Reranker（2〜14 GiB、PAW-018 / PAW-019）を同じ GPU に置いても、KV Pool に 30 GiB 以上を残せる（Run 中の KV の使用のピークは 250k token ≒ Pool の 29%）。KAT は Weight が 64.7 GiB（BF16）で、Memory Worker と Reranker を置くと KV Pool がほぼ残らない。
- Qwen3.8-27B-FP8 は同じ結果に約 4 倍の時間（117 分）と 3.7 倍の出力 Token を使う。Interactive な Coding には遅い。
- Runtime は vLLM（FP8、`--max-model-len 131072`、Reasoning parser `qwen3`、Tool parser `qwen3_coder`）。`gpu-memory-utilization` と Footprint は共存させる Model に合わせて下げ、[Decision 0039](0039-compute-scheduler-calibration.md) の 1 に従い実測で与える（下の 3）。

### 2. KAT-Coder-V2.5-Dev を次点として残す

- 同じ Resolved@1 で、速さ（30 分）も同等。量子化（FP8）した版で KV Pool を広げられれば、共存の条件で Qwen3.6-27B-FP8 と比べ直す価値がある。今回は BF16 だけを測った。

### 3. 採用の前に共存構成の確認 Run を 1 回行う（別の Issue）

- Main（Qwen3.6-27B-FP8）・Memory Worker・Embedding / Reranker を同時に置いた構成で、paw-seed-v1 をもう一度走らせ、Resolved が今回（14/24）より下がらないこと、Run 中の GPU 使用量のピークに Safety Headroom（0037 の 3）を足しても GPU の容量に収まること（OOM がないこと）を確かめ、その実測から Footprint（0039 の 1）を与える。
- この Run で上位 3 つ（Qwen3.6-27B-FP8、KAT、Qwen3.8-27B-FP8）の Resolved@3（3 回）も測り、1 回の Run の揺れを見る。

### 4. 候補から外す Model

- **Qwen3-Coder-30B-A3B-Instruct**（6/24。Injected Bug も 4/7 で、確かめずに終える）、**gpt-oss-20b**（10/24、Tool を呼ばずに終わる回答が 11 Task）、**Nemotron 3.5 Lightning**（10/24。ただし最も速い: 13 分）。
- gpt-oss-120b（12/24）は Tool を呼ばずに終わる回答が 9 Task あり、Harness の Tool 形式との相性の可能性があるが、Weight 66 GiB で共存の条件も厳しいため、追加の調査はしない。

## 代替案

- **KAT-Coder-V2.5-Dev を採用する**: 同じ Resolved@1。ただし BF16 の Weight 64.7 GiB で、共存させると KV Pool が足りない。量子化した版を測ってからなら候補になる（2）。
- **Qwen3.8-27B-FP8 を採用する**: 同じ Resolved@1。約 4 倍遅く、Token の消費も多い。長い Task の品質が要る場合の切り替え先にはなる（Weight と KV は Qwen3.6-27B-FP8 と同じ）。
- **Qwen3.6-35B-A3B-FP8 を採用する（速さを優先）**: 12/24 で 2 Task 少ないが、2 倍速い（15 分）。Interactive の応答速度を最優先するなら候補。
- **Human correction time を測ってから決める**: Issue の受け入れ条件にあるが、この Run では測っていない（判断点 5）。

## リスク

- 1 回の Run・24 Task で、上位の差は 1〜2 Task。Historical の 11 Task は Model の差をほとんど測れていない。3 の確認 Run で揺れを見るまで、1 位の 3 つの間の順位は確かではない。
- NVMe への Stage をしていない（Issue の受け入れ条件 1）。影響は Load 時間だけで、Agent の指標には入らない。
- KAT と Qwen3-Coder-30B-A3B は `--moe-backend triton`、Devstral は互換 Shim で走らせた（報告の 1）。Runtime の違いが品質に影響した可能性は否定できない。
- 採用後に新しい Model が出ても、この Decision は自動で変えない。比較し直すときは新しい Decision から `Supersedes` する。

## 決めてほしいこと

1. **Main Coding Model を Qwen3.6-27B-FP8（vLLM、FP8）にする**（1）か。推奨: はい（3 の確認 Run で Resolved が下がらず、ピーク + Headroom が GPU の容量に収まることを条件に）。
2. **KAT-Coder-V2.5-Dev を次点として残し、FP8 版を 3 の確認 Run で比べる**（2）か。推奨: はい。
3. **採用の前に共存構成の確認 Run（Resolved@3 を含む）を別の Issue で行う**（3）か。推奨: はい。
4. **Qwen3-Coder-30B-A3B・gpt-oss-20b・Nemotron 3.5 Lightning を候補から外す**（4）か。推奨: はい。
5. **Human correction time を測らずに PAW-017 を閉じてよいか**（Issue の受け入れ条件 3）。推奨: 3 の確認 Run の後に、採用 Model の未解決の Spec / Historical の Patch について人が測る（別の Issue）。PAW-017 は条件つきで閉じる。
6. **NVMe への Stage をしなかったこと**（Issue の受け入れ条件 1）を、Load 時間にしか影響しないとして受け入れるか。推奨: はい（3 の確認 Run では Stage する）。

## 承認後の扱い

承認されたら `Approval` に記録し、Status を Approved に改め、[MODEL_CANDIDATES.md](../MODEL_CANDIDATES.md) に採用の結果を追記する（別の PR）。Deployment の設定（Model の Path、vLLM の引数、Footprint）は Deployment の手順で与え、DB には書かない。
方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。

## 承認時の決定（2026-09-30）

Human は、作業 Session で判断が必要な点について推奨つきの説明を受け、直接回答して承認した（判断が必要な点 1〜6 の全点）。1〜3 の確認 Run（同時に動かす Run、上位 3 構成の Resolved@3、KAT の FP8 版）は #180、5 の人が直す時間は #181 で行う。
