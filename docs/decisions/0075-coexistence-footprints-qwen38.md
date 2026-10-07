# 共存時（Main Qwen3.8-27B-FP8、0.51）の 4 つの Deployment の Footprint と、Scheduler の予約の勘定で足りない 0.5 GiB の扱い

- Status: Proposed
- Date: 2026-10-08
- Scope: Issue [#180](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/180)（[Decision 0073](0073-main-coexistence-confirmation.md) の 2 の確認 Run の結果）。根拠は [共存の確認 Run（0.51）の報告](../benchmarks/paw-017-coexist-qwen38-2026-10.md)。KV の不足への対策は [#200](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/200) で別に決める
- Supersedes: なし。[Decision 0037](0037-gpu-compute-scheduler.md)・[Decision 0039](0039-compute-scheduler-calibration.md)・[Decision 0073](0073-main-coexistence-confirmation.md)・[Decision 0074](0074-seed-v2-and-qwen-27b-comparison.md) は変えない（この Decision はそれらの規則で値を決める）

## 背景

Decision 0074 の承認時の決定で、0.51 の確認 Run は Qwen3.8-27B-FP8・paw-seed-v1 で 3 回行い、Resolved@3 13 以上かつ平均 13.0 以上で合格、合格したら 4 つの Deployment の Footprint を与えることになった。

2026-10-07 の Run（報告の 2）の結果:

- Resolved は 14 / 14 / 14、Resolved@3 14、平均 14.0 で**合格**（基準の単独 14 / 14 / 14 と同じ 14 Task の集合）。
- 4 つの Deployment（Main・Memory Worker・Embedding・Reranker。Embedding と Reranker は別々の Process）を同時に置き、GPU 全体の Peak は 79,892 MiB。Safety Headroom（4,894 MiB）を足しても GPU（97,887 MiB）に収まり、OOM はなかった。
- Deployment ごとの Peak（Load と Run の全体、Process ごと）: Main 52,104・Memory Worker 13,948（Load の途中。Run 中は 13,456）・Embedding 2,002・Reranker 12,286 MiB。
- Main の KV Pool（305,081 token）は 3 回とも使い切り、Preemption 計 40 回、待ちの最大 2 Request。Run は単独より約 1.3 倍遅かった（正解数は変わらない）。

Decision 0039 の 1（Footprint = 実測のピーク + 2 GiB）で数えると計 88,532 MiB になり、Decision 0037 の 3 の予約の勘定（最後に Load する `IF_ROOM` の Model の Load 後に、Headroom の外にもう 1 つ `restore_margin_bytes` が残ること。既定は Headroom と同じ 4,894 MiB）では、Probe に見える他の使用（`external`。この Run では 20〜44 MiB、`compute/accounting.py` の `account`）も足して 88,532 + 44 + 4,894 + 4,894 = 98,364 MiB となり、GPU を **477 MiB 超える**。そのままでは Scheduler は Load 順で最後の Reranker を GPU に Load しない。

次の 3 点は承認済みの Decision が決めていない。

## 提案

### 1. Footprint は Load の途中を含む Process ごとの Peak + 2 GiB とする

| Deployment | Peak | Footprint |
| --- | ---: | ---: |
| Main（Qwen3.8-27B-FP8、`--gpu-memory-utilization 0.51`、ほかは報告の 1） | 52,104 MiB | **54,152 MiB（52.9 GiB）** |
| Memory Worker（Qwen3.5-4B、KV 4 GiB、16k、4 seqs） | 13,948 MiB | **15,996 MiB（15.6 GiB）** |
| Embedding（Qwen3-Embedding-0.6B、torch、bf16） | 2,002 MiB | **4,050 MiB（4.0 GiB）** |
| Reranker（Qwen3-Reranker-4B、torch、bf16） | 12,286 MiB | **14,334 MiB（14.0 GiB）** |
| 計 | | **88,532 MiB（86.5 GiB）** |

- Decision 0039 の 1 の推奨の式は「Run 中のピーク」だが、Scheduler は Load するときにその分が空いていることを求めるので、Load の途中の一時的な使用（Memory Worker の 13,948 MiB、vLLM の Profiling）も含める（安全側）。
- 値は、この Run の設定（Model・vLLM の引数・batch の大きさ）に対するもの。設定を変えたら測り直す。

### 2. 足りない 477 MiB は、`restore_margin_bytes` を 4 GiB（4,096 MiB）にして解く

- `ComputeConfig.restore_margin_bytes` を `None`（Headroom と同じ 4,894 MiB）から 4,096 MiB にする。Headroom（4 GiB と GPU の 5% の大きい方）は変えない。
- 勘定: 88,532 + `external` 44 + 4,894 + 4,096 = 97,566 MiB ≤ 97,887 MiB（321 MiB の余裕）。
- Scheduler は `memory.total`（97,887 MiB）と `memory.used` で数える。何も起動していない GPU でも `memory.free` は 97,231 MiB で、差のうち 636 MiB は Driver が取り `memory.used` に出ない分（勘定に入らず、Safety Headroom が吸収する）。この分を引いても、実際に Load できる空きは Footprint の計より大きい（Peak の計 80,340 MiB に対して 97,231 − 20 MiB）。
- `restore_margin_bytes` は Pressure と復帰の往復を防ぐためのもので（Decision 0037 の 3）、798 MiB 小さくしても、復帰の後に Headroom の外に 4 GiB が残る。実際の使用のピーク（79,892 MiB）では GPU に 18 GiB 近い空きがあった。
- 設定（Deployment と `ComputeConfig`）への反映は、Decision 0073 の「承認後の扱い」のとおり別の Issue で行う（この PR は docs だけ）。

### 3. Scheduler の Admission を通さなかったこの Run を、0073 の 2 の確認 Run として受け入れる

- Decision 0073 の 2 は、確認 Run を Scheduler の Admission を通して行い、待ちの時間を記録するとした。この Run の Harness は 10-01 と同じく vLLM に直接 Request を送った（Harness の Request を Scheduler の Lease に対応させる仕組みを新しく作る必要があり、作らなかった）。
- 今の Scheduler は Node の開始時の見積もりで Lease を 1 回取るだけで、4 つの Agent の開始時の見積もりは Coding に許す予約（約 260,800 token）より十分に小さい。Scheduler を通しても Admission で待つことはなく、KV の不足は vLLM の中の待ち・Preemption として現れたと見込まれる（実測ではない）。その vLLM の中の待ち・Preemption・KV の使用率は `/metrics` で記録した（報告の 2）。
- 会話の伸びを予約に反映する Scheduler（#200 の候補）を作る場合は、その変更の確認で Admission を通した Run を行う。

## 代替案

- **1 で Memory Worker の Peak を Run 中の値（13,456 MiB）にする**: 計 88,040 MiB で、`restore_margin_bytes` を変えずに 15 MiB だけ収まる。Load の途中の使用を数えないことになり、余裕も小さすぎる。採らない。
- **2 を Main の `gpu-memory-utilization` を 0.50 にして解く**: 約 1 GiB 減るが、KV Pool は 3 回とも使い切っており、さらに減らすと Preemption と待ちが増える。採らない。
- **2 を Reranker（または Embedding）を `ALWAYS` にして解く**: `ALWAYS` の Load は Margin を求めないので収まるが、縮退の 3 段目（Embedding / Reranker を CPU へ移す・Unload する）で `ALWAYS` かつ CPU の Copy がないものは残るため、Pressure のときに GPU を空けられなくなる。採らない。
- **2 を Embedding / Reranker を CPU に置いて解く**（Decision 0073 の代替案）: GPU に約 18 GiB 空き、Main の KV にも回せるが、CPU での Retrieval の Latency は測っていない。#200 の「Main に GPU Memory を多く与える」で比べる。
- **2 を Decision 0039 の Margin（+ 2 GiB）を小さくして解く**: 0039（Approved）の変更になるため、ここでは採らない。
- **3 で、Admission を通す仕組みを作って Run をやり直す**: 今の Scheduler の Lease の取り方では結果が変わらない見込みで、数時間の Run と新しい Code が要る。採らない。

## リスク

- Footprint は 1 回の Run（3 回 × 24 Task、約 7.5 時間）の Peak。Context の長い Task（paw-seed-v2 の新しい Task など）では、Main の予算を超える分がもっと大きくなる可能性がある。
- 2 の後も、Footprint で数えた余裕は 321 MiB しかない（`external` が 300 MiB ほど増えると、Reranker は Load されない）。Model・Runtime の版や設定が変わったら測り直す必要がある。
- KV の不足（Preemption と待ち、約 1.3 倍の遅さ）はこの Decision では解かない（#200）。Interactive の応答の速さへの影響は測っていない。

## 決めてほしいこと

1. **4 つの Deployment の Footprint を、Load の途中を含む Process ごとの Peak + 2 GiB（Main 54,152・Memory Worker 15,996・Embedding 4,050・Reranker 14,334 MiB）にする**（1）か。推奨: はい。
2. **Scheduler の予約の勘定で足りない 477 MiB を、`restore_margin_bytes` を 4 GiB（4,096 MiB）にして解く**（2）か。推奨: はい（Headroom は変えない。代わりの案は Main を 0.50 にする・Reranker を `ALWAYS` にする・Embedding / Reranker を CPU に置く）。
3. **Scheduler の Admission を通さず vLLM の `/metrics` で待ちを記録したこの Run を、Decision 0073 の 2 の確認 Run として受け入れる**（3）か。推奨: はい（今の Scheduler の Lease の取り方では Admission で待つことはない見込み。Admission を通した確認は、会話の伸びを予約に反映する変更（#200）の確認で行う）。

## 承認後の扱い

承認されたら `Approval` に記録し、Status を Approved に改める。Footprint と `restore_margin_bytes` を Deployment と `ComputeConfig` の設定に入れるのは別の Issue で行う（Model の Path、vLLM の引数を含む）。
方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。
