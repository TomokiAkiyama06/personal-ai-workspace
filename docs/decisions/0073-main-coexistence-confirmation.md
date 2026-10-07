# Main Coding Model の共存の確認 Run（#180）の結果の扱い（採用条件の読み方、共存時の Footprint、KAT の FP8 版）

- Status: Approved（2 と 3。1 は保留し、Decision 0074 で決める）
- Approval: 2026-10-06、Humanが作業Session内で、判断点ごとの説明（推奨つき）を受けたうえで直接回答して承認（2 と 3 は推奨どおり承認。1 は保留し、Issue #198 の Qwen3.6-27B-FP8 / Qwen3.8-27B-FP8 の比較の結果（Decision 0074）で決める。末尾の「承認時の決定」）
- Date: 2026-10-02
- Scope: Issue [#180](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/180)（[Decision 0040](0040-main-coding-model-selection.md) の 1〜3 の確認 Run）。根拠は [PAW-017 の報告](../benchmarks/paw-017-main-coding-2026-09.md) の 6
- Supersedes: [Decision 0040](0040-main-coding-model-selection.md) のうち、3 の 1 つ目の「Resolved が今回（14/24）より下がらないこと」と、決めてほしいこと 1 の推奨の「3 の確認 Run で Resolved が下がらず」の採用条件の部分だけ（この Decision の 1 の読み方で置き換える。1 は 2026-10-06 に保留となったため、Decision 0074 で決まるまで効力を持たない）。0040 のほかの点（Peak + Headroom が GPU に収まる条件、Resolved@3 を測ること、2・4〜6）は変えない。[Decision 0037](0037-gpu-compute-scheduler.md)・[Decision 0039](0039-compute-scheduler-calibration.md) は変えない（2 はその規則に従って割り当てを決める）

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
- 一方、0039 の 1 の Footprint（Deployment ごとのピーク + 2 GiB）で数えると、Scheduler では Embedding と Reranker は別の Deployment（`ModelRole.EMBEDDING` / `ModelRole.RERANKER`）なので Margin が 2 つ要り、Main 61.7 + Memory Worker 15.6 + Embedding と Reranker 計 17.1（この Run は 1 つの Process で 13.1 GiB。別々の値は測っていない）= 94.4 GiB。さらに 0037 の 3 では、`IF_ROOM` の Model（Memory Worker）を Load するとき、Load 後に Headroom のほかにもう 1 つ `restore_margin_bytes`（既定は Headroom と同じ 4.8 GiB）が残ることを求める（`apps/backend/paw_backend/compute/scheduler.py` の `_fill`）。合計 94.4 + 4.8 + 4.8 = 104.0 GiB で、**Scheduler の予約の勘定では 4 つを同時に置けない**（GPU の 95.6 GiB に対して 8.4 GiB 不足。Footprint の合計は 86.0 GiB 以下にする必要がある）。
- KAT の FP8 版は、FP8 の Weight が手元になく Download していないため、BF16 の Weight を vLLM が Load 時に量子化した。Load の途中に Main だけで 94.9 GiB（約 16 秒）を使う。

次の 3 点は承認済みの Decision が決めていない。

## 提案

### 1. 採用条件は「満たした」と読み、Qwen3.6-27B-FP8 の採用を確定する

- 0040 の 3 の「Resolved が 14/24 より下がらないこと」を、1 回ごとの値ではなく下の読み方に置き換える（0040 の該当部分を `Supersedes` する）。

- 同時に動かした Run の 1 回（run2）が 13/24 だった。落ちた spec-02 は同じ構成の他の 2 回と単独の 3 回では解けており、`submit` した Patch が Hidden acceptance に落ちたもの（Context の不足でも Error でもない）。Resolved@3 は 14/24 で、単独と同じ Task の集合。
- 単独の KAT も 14 / 13 / 13 と同じ幅で揺れる。Sampling は温度 0 ではなく、1 Task の差は 1 回の Run の揺れの範囲。
- 条件を「Resolved@3 と平均が単独と同じ範囲（1 Task 以内）」と読む。

### 2. 共存時は Main の `gpu-memory-utilization` を 0.61 から 0.51 に下げ、Footprint をその値で与える

- 0.10 × 95.6 GiB ≈ 9.6 GiB を Main の KV Pool から減らす（28.9 → 約 19.3 GiB、約 303k token）。この Run の KV の使用の最大は 51.5%（約 15 GiB、約 234k token）。Scheduler が Coding の Request に予約を許すのは Pool の 0.90（`kv_safety`）× 0.95（Coding の上限）で約 259k token なので、余裕は約 1.1 倍しかない。足りないときは Scheduler が新しい Request を待たせる（Admission。0037 の 4）ので OOM にはならないが、4 並列の Agent が待つ時間が増えうる。
- Main の予算を超える分（0.61 で 1.4 GiB）が変わらないとみなすと、見込みの Footprint は Main 52.2・Memory Worker 15.6・Embedding と Reranker 計 17.1 GiB（合計 84.9 GiB）。Headroom と `restore_margin_bytes`（4.8 GiB ずつ）を足して 94.5 GiB で、GPU に 1.1 GiB の余裕が残る。
- 確認 Run は Scheduler の Admission（`kv_safety` と Class の上限）を通した構成で行い、待ちの時間も記録する。待ちが多ければ、0.51 ではなく Memory Worker の KV を減らす・Embedding / Reranker を CPU に置く（代替案）を組み合わせる。
- これは Run の実測からの見積もりで、Embedding と Reranker を別々の Process で測ってもいない。0.51 で、Embedding と Reranker を Scheduler と同じく別々の Runtime にして同時に動かす Run を 3 回行い、1 の読み方（Resolved@3 と平均が単独と同じ範囲）で Resolved が下がらないことと、4 つの Deployment それぞれの Footprint（ピーク + 2 GiB）を確かめてから Deployment の設定に入れる（別の Issue）。

### 3. KAT-Coder-V2.5-Dev は次点のまま、Load 時の FP8 量子化は Deployment に使わない

- Load 時の量子化は Load の途中に GPU をほぼ使い切るため、Memory Worker や Reranker が常駐している状態で Main を Load し直せない（0037 の縮退から戻すときなど）。
- Resolved は BF16 と同じ幅（13〜14）。事前に量子化した FP8 の Weight を使う場合だけ測り直す。その Weight の Download が 100 GB を超えるときは、Download の前に報告する。

## 代替案

- **1 を厳密に読む（1 回でも 14 を下回れば満たさない）**: もう 3 回走らせるか、採用を保留する。Run の揺れを考えると、次も 13 が出る可能性があり、決まらない。採らない。
- **2 を Memory Worker の KV を 4 GiB から 2 GiB にして解く**: 2 GiB 減るが、16k context × 4 seqs（約 64k token）を収められなくなる。Memory Worker の KV の使用の最大は 3.2% だったが、並列の上限を下げることになる。
- **2 を Embedding と Reranker を 1 つの Deployment にまとめて解く**: Margin が 1 つで済む（2 GiB）が、Scheduler の `DeploymentSpec` は 1 つの Role しか持たず、縮退の順（0037）も Role で決まるため、Scheduler の変更が要る。
- **2 を Embedding / Reranker を CPU に置いて解く**（0037 の縮退の 3 段目を常態にする）: GPU に 15 GiB 空くが、Retrieval の Latency は測っていない（今回の p95 は GPU で 448 ms）。
- **2 を 0039 の Margin（+ 2 GiB）を小さくして解く**: 0039（Approved）の変更になるため、ここでは採らない。
- **2 を `restore_margin_bytes` を小さくして解く**（`ComputeConfig` の設定で変えられる）: Main の KV を減らさずに済むが、0037 の 3 が防ぐ Pressure と復帰の往復が起きやすくなる。0 にしても 94.4 + 4.8 = 99.2 GiB で収まらず、Main の割り当ても減らす必要がある。

## リスク

- 2 の 0.51 は実測していない。KV Pool が小さくなるため、Context の長い Task が 4 並列で重なると KV が足りなくなる可能性がある（この Run の最大は 0.61 の Pool の 51.5%）。予算を超える分（0039 の 1）が変わらないとみなした見積もり。
- 3 回 × 24 Task で、上位の差は 1 Task 以内。Historical の 10 Task はどの Run でも解けず、Model の差を測れていない。
- 同時に動かすと Main は約 1.4 倍遅くなる（24 Task に 43〜45 分）。Interactive の応答の速さへの影響は測っていない。

## 決めてほしいこと

1. **採用条件を「Resolved@3 と平均が単独と同じ範囲」と読み、Qwen3.6-27B-FP8 の採用を確定する**（1）か。推奨: はい（MODEL_CANDIDATES.md への追記は別の PR）。
2. **共存時の Main の `gpu-memory-utilization` を 0.51 にし、その設定で（Embedding と Reranker を別々の Runtime にして）同時に動かす Run を 3 回行ってから Footprint を与える**（2）か。推奨: はい。
3. **KAT は次点のまま、Load 時の FP8 量子化は Deployment に使わない**（3）か。推奨: はい。

## 承認後の扱い

承認されたら `Approval` に記録し、Status を Approved に改める。2 の確認 Run と Deployment の設定（Model の Path、vLLM の引数、Footprint）は別の Issue で行い、DB には書かない。
方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。

## 承認時の決定（2026-10-06）

Human は、作業 Session で判断が必要な点について推奨つきの説明を受け、直接回答して承認した（2 と 3 は推奨どおり承認。1 は保留し、Issue #198 の Qwen3.6-27B-FP8 / Qwen3.8-27B-FP8 の比較の結果（Decision 0074）で決める）。1 は承認していないため、Main Coding Model の採用はまだ確定しない。上の Supersedes（Decision 0040 の採用条件の読み方の置き換え）も、1 が Decision 0074 で決まるまで効力を持たない。

2 の確認 Run（0.51 で 3 回）は行ってよいが、その合否（Resolved が下がらないことの判定）と Deployment の設定への反映は、1 の読み方が保留のため、Decision 0074 で承認された採用条件で判定し、0074 が決まるまで行わない。
