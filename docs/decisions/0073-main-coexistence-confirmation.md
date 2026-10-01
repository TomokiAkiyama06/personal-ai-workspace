# Main Coding Model の共存の確認 Run（#180）の結果の扱い（採用条件の読み方、共存時の Footprint、KAT の FP8 版）

- Status: Proposed
- Date: 2026-10-02
- Scope: Issue [#180](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/180)（[Decision 0040](0040-main-coding-model-selection.md) の 1〜3 の確認 Run）。根拠は [PAW-017 の報告](../benchmarks/paw-017-main-coding-2026-09.md) の 6
- Supersedes: なし。[Decision 0037](0037-gpu-compute-scheduler.md)・[Decision 0039](0039-compute-scheduler-calibration.md)・[Decision 0040](0040-main-coding-model-selection.md)（いずれも Approved）は書き換えない

## 背景

Decision 0040 は Main を Qwen3.6-27B-FP8 にすることを、次の条件つきで承認した: Memory Worker・Embedding / Reranker と同時に動かしても Resolved が 14/24 より下がらないこと、Run 中の GPU 使用量のピークに Safety Headroom を足しても GPU に収まること。その実測から Footprint（0039 の 1）を与える。

2026-10-01 の確認 Run（報告の 6）の結果:

| 構成 | run1 / run2 / run3 | Resolved@3 | 3 回とも解けた Task |
| --- | ---: | ---: | ---: |
| Qwen3.6-27B-FP8（単独） | 14 / 14 / 14 | 14 | 14 |
| KAT-Coder-V2.5-Dev（単独、BF16） | 14 / 13 / 13 | 14 | 13 |
| Qwen3.8-27B-FP8（単独） | 14 / 14 / 14 | 14 | 14 |
| Qwen3.6-27B-FP8（同時に動かす） | 14 / 13 / 14 | 14 | 13 |
| KAT-Coder-V2.5-Dev-FP8（同時に動かす、Load 時の量子化） | 13 / 14 / 13 | 14 | 13 |

- 同時に動かした Qwen3.6-27B-FP8 の GPU 全体の Peak は 86.1 GiB（Main 59.7・Memory Worker 13.6・Embedding + Reranker 13.1 GiB）。Headroom（4.8 GiB）を足して 90.9 GiB で、GPU（95.6 GiB）に収まり、OOM はなかった。
- 一方、0039 の 1 の Footprint（Process ごとのピーク + 2 GiB）で数えると 61.7 + 15.6 + 15.1 = 92.4 GiB、Headroom を足して 97.2 GiB で、**Scheduler の予約の勘定では 3 つを同時に置けない**（1.6 GiB 不足）。
- KAT の FP8 版は、FP8 の Weight が手元になく Download していないため、BF16 の Weight を vLLM が Load 時に量子化した。Load の途中に Main だけで 94.9 GiB（約 16 秒）を使う。

次の 3 点は承認済みの Decision が決めていない。

## 提案

### 1. 採用条件は「満たした」と読み、Qwen3.6-27B-FP8 の採用を確定する

- 同時に動かした Run の 1 回（run2）が 13/24 だった。落ちた spec-02 は同じ構成の他の 2 回と単独の 3 回では解けており、`submit` した Patch が Hidden acceptance に落ちたもの（Context の不足でも Error でもない）。Resolved@3 は 14/24 で、単独と同じ Task の集合。
- 単独の KAT も 14 / 13 / 13 と同じ幅で揺れる。Sampling は温度 0 ではなく、1 Task の差は 1 回の Run の揺れの範囲。
- 条件を「Resolved@3 と平均が単独と同じ範囲（1 Task 以内）」と読む。

### 2. 共存時は Main の `gpu-memory-utilization` を 0.61 から 0.58 に下げ、Footprint をその値で与える

- 0.03 × 95.6 GiB ≈ 2.9 GiB を Main の KV Pool から減らす（28.9 → 約 26 GiB、約 408k token）。この Run の KV の使用の最大は 51.5%（約 15 GiB、約 234k token）で、減らしても 1.7 倍が残る。
- 見込みの Footprint は Main 58.8・Memory Worker 15.6・Embedding + Reranker 15.1 GiB（合計 89.5 GiB）。Headroom を足して 94.3 GiB で、GPU に 1.3 GiB の余裕が残る。
- これは Run の実測からの見積もりなので、0.58 で同時に動かす Run を 1 回行い、Footprint をその実測（ピーク + 2 GiB）で確かめてから Deployment の設定に入れる（別の Issue）。

### 3. KAT-Coder-V2.5-Dev は次点のまま、Load 時の FP8 量子化は Deployment に使わない

- Load 時の量子化は Load の途中に GPU をほぼ使い切るため、Memory Worker や Reranker が常駐している状態で Main を Load し直せない（0037 の縮退から戻すときなど）。
- Resolved は BF16 と同じ幅（13〜14）。事前に量子化した FP8 の Weight を使う場合だけ測り直す。その Weight の Download が 100 GB を超えるときは、Download の前に報告する。

## 代替案

- **1 を厳密に読む（1 回でも 14 を下回れば満たさない）**: もう 3 回走らせるか、採用を保留する。Run の揺れを考えると、次も 13 が出る可能性があり、決まらない。採らない。
- **2 を Memory Worker の KV を 4 GiB から 2 GiB にして解く**: 2 GiB 減るが、16k context × 4 seqs（約 64k token）を収められなくなる。Memory Worker の KV の使用の最大は 3.2% だったが、並列の上限を下げることになる。
- **2 を Embedding / Reranker を CPU に置いて解く**（0037 の縮退の 3 段目を常態にする）: GPU に 15 GiB 空くが、Retrieval の Latency は測っていない（今回の p95 は GPU で 448 ms）。
- **2 を 0039 の Margin（+ 2 GiB）を小さくして解く**: 0039（Approved）の変更になるため、ここでは採らない。

## リスク

- 2 の 0.58 は実測していない。予算を超える分（0039 の 1）が変わらないとみなした見積もり。
- 3 回 × 24 Task で、上位の差は 1 Task 以内。Historical の 10 Task はどの Run でも解けず、Model の差を測れていない。
- 同時に動かすと Main は約 1.4 倍遅くなる（24 Task に 43〜45 分）。Interactive の応答の速さへの影響は測っていない。

## 決めてほしいこと

1. **採用条件を「Resolved@3 と平均が単独と同じ範囲」と読み、Qwen3.6-27B-FP8 の採用を確定する**（1）か。推奨: はい（MODEL_CANDIDATES.md への追記は別の PR）。
2. **共存時の Main の `gpu-memory-utilization` を 0.58 にし、その設定で同時に動かす Run を 1 回行ってから Footprint を与える**（2）か。推奨: はい。
3. **KAT は次点のまま、Load 時の FP8 量子化は Deployment に使わない**（3）か。推奨: はい。

## 承認後の扱い

承認されたら `Approval` に記録し、Status を Approved に改める。2 の確認 Run と Deployment の設定（Model の Path、vLLM の引数、Footprint）は別の Issue で行い、DB には書かない。
方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。
