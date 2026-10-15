# Scheduler の Admission を通した共存の確認 Run の報告（#200、2026-10-15）

- Issue: [#200](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/200) の残り（[Decision 0075](../decisions/0075-coexistence-footprints-qwen38.md) の 3 と [Decision 0076](../decisions/0076-main-kv-context-under-coexistence.md) の 3 の「Admission を通した確認」）。計画は #220 の「Admission を通した共存の確認 Run の計画」
- 実施: 2026-10-15 17:50〜2026-10-16 00:29（Human の許可: 2026-10-15「GPU 使ってよい」）。1 回（計画のとおり。3 回に増やすかは Human が決める）
- 見る点（計画の (a)〜(e)。合否ではなく報告）: (a) Preemption、(b) 新しい Node の Admission の待ち、(c) Interactive の Admission の待ち、(d) 正解数、(e) Pool の 95% 以上の時間
- 前の報告: [共存の確認 Run（10-07、Admission なし）](paw-017-coexist-qwen38-2026-10.md)、[KV / Context の分析と 0076 の 2 の Run](paw-017-kv-context-qwen38-2026-10.md)
- 生の結果（Trace・Patch・Log・`/metrics`・Scheduler の状態）は Repository に入れない（Hidden check を含むため。Decision 0041 の 4）。Server の `/data/results/paw-smoke-2026-10-15/sched`（集計は `aggregate_run1.json`）にある。

## 1. 条件

### GPU の上の 4 つの Deployment（10-07 の共存の確認 Run と同じ）

| Deployment | Model | 設定 |
| --- | --- | --- |
| Main | Qwen3.8-27B-FP8 | vLLM 0.30.0、`--gpu-memory-utilization 0.51 --max-model-len 131072 --max-num-seqs 8`、`--reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser qwen3_coder --language-model-only`、Served name `main`。KV Pool 19.4 GiB・305,081 token |
| Memory Worker | Qwen3.5-4B | vLLM、KV 4 GiB（106,927 token）、16k、4 seqs。`json_schema` の負荷を 2 秒に 1 回 |
| Embedding | Qwen3-Embedding-0.6B | 独立した Process。Retrieval の負荷を 2 秒に 1 回 |
| Reranker | Qwen3-Reranker-4B | 独立した Process |

GPU の使用は全部の Load の後 76,912 MiB、Run 中の Peak 80,198 MiB。安全の設定（Decision 0039 の 4・0072）は前の Run と同じで、Watchdog が止めたことはない。GPU にはこの Run の Process だけがいた。

### Scheduler と Agent（本番の部品）

- **Scheduler**: Backend の `ComputeScheduler` に、4 つの Deployment を Decision 0075 の Footprint（Main 54,152・Memory Worker 15,996・Embedding 4,050・Reranker 14,334 MiB。Main と Memory Worker の KV Pool は vLLM の Log の値）、`restore_margin_bytes` 4 GiB、`kv_safety` 0.90 と Class の取り分は既定で与えた。Probe は `NvidiaSmiProbe`、Model の Control は `CommandModelControl`（Process の一覧と Main / Memory Worker の `metrics_url` だけ。Load / Unload の Command は持たせない: 観測と Admission だけ）。
- **Node**: paw-seed-v2 の 49 Task（10-08 に固めた Copy）をそれぞれ 1 つの Worker の Node の試行として、`HybridRuntime(scheduler, LocalAgentRuntime, deployment="main", CODING)` で 4 つずつ並列に動かした（`max_parallel_nodes` 4 に当たる）。`LocalAgentRuntime` は既定の `DropOlderReasoning(keep_last=2, min_prompt_tokens=32768)` を通し、毎回の Request の前に `lease.resize` で予約を「Prompt の見積もり + `max_tokens` 16,384」にする（Decision 0076 の 3）。Tool は本番の Tool Broker と Sandbox（[GPU の Smoke の報告](https://github.com/TomokiAkiyama06/personal-ai-workspace/pull/246) と同じ構成。Podman の代わりに Docker）。
- **Interactive**: 30 秒ごとに 1 回、`scheduler.acquire(INTERACTIVE, context 576)` で Lease を取ってから短い Chat（`max_tokens` 512）を送った（783 回）。
- **計画との違い**:
  - Orchestrator・DB は使っていない（Node の試行を直接 `HybridRuntime` に渡した。失敗した Node の再試行・Escalation はしない。Admission で断られた Node は再試行する作りにしたが、一度も起きなかった）。
  - Node の時間の上限は、比べる 0076 の 2 の Run（Harness の 45 分）に合わせて 2,700 秒にした（本番の Orchestrator の既定は 1,800 秒）。60 Step で止まった Node も、Harness と同じく最後の Worktree の状態を評価した。
  - Memory Worker と Retrieval の負荷は 10-07 と同じく各 Server に直接送った（Scheduler の Lease は通していない。Main の KV とは別の Deployment なので、Main の Admission には関わらない）。

## 2. 結果

### (d) 正解数

| 構成 | Resolved | 49 Task の所要 |
| --- | ---: | ---: |
| **この Run（共存 0.51・Scheduler の Admission・`LocalAgentRuntime`・本番の Sandbox）** | **35** | **392 分** |
| 0076 の 2 の Run（単独 0.90・Harness・DropOlderReasoning、10-10〜11） | 36 / 36 / 36（Resolved@3 37） | 280〜290 分 |

- 0076 の Run で 3 回とも解けた 34 Task のうち、解けなかったのは **spec-04（v1）の 1 Task** だけ（45 分の上限で 37 Step。0076 の Run では 3 回とも 37〜48 Step で解けている。うち 1 回は 45 分の上限の間際）。0076 の Run で 3 回中 2 回解けた v2 の spec-08 も解けなかった（60 Step、Hidden の受け入れの Test の 91% は通った）。新しく解けた Task はない。
- 終わり方: `submit` 25、時間の上限 12、60 Step 12（0076 の run1 は `submit` 34、60 Step 10、時間の上限 5）。時間の上限が多いのは、次の「所要」のとおり 1 Step が遅いため。

### (a) Preemption と (e) KV の使用率

| | この Run | 10-07（共存 0.51、Admission なし、v1 24 Task × 3） |
| --- | ---: | ---: |
| vLLM の Preemption | **0 回** | 40 回 |
| KV の使用率の最大 | **87.1%** | 99.8〜100% |
| Pool の 95% 以上の時間 | **0%** | 2.3〜3.4% |
| `capacity` の理由で待つ Request がいた時間 | 0.27% | - |

### (b) 新しい Node の Admission の待ち

- 49 回の Coding の Admission はすべて **待ち 0 秒**（断り・再試行も 0）。
- 一方、走っている Node の予約の合計（`resize` で伸びたもの）は、Coding の上限（Pool × 0.90 × 0.95 ≈ 260.8k token）を **Run の 9.2% の時間**、`kv_safety`（≈ 274.6k）を 6.7% の時間超えていた（最大 311,333 token、Pool より大きい: 0076 の 3 のとおり伸びは待たせない）。新しい Node はいつも前の Node が終わって Lease を返した直後に来るので、そのとき合計は上限の下に戻っていて、待つことがなかった。0076 の分析の見積もり（上限を超える時間 10.3%、新しい Node の待ちは Run の 10〜24%）より、超える時間は近く、待ちは小さい。
- `resize` は 2,043 回、`CONTEXT_TOO_LONG`（131,072 超え）は 0 回。1 回の予約の最大は 106,345 token。Task ごとの Prompt の最大は中央値 47.2k・p90 63.8k・最大 90.1k（0076 の Run の p90 69.5k と同じくらい）。

### (c) Interactive

- 783 回すべて **Admission の待ち 0 秒**、Error 0。応答の時間は中央値 7.8 秒・p90 17.7 秒・最大 42.5 秒（Reasoning を含む出力が 512 token の上限に達したのが 50 回）。待ちは Admission ではなく、vLLM の中で 4 つの Agent と GPU を分け合う時間。

### 所要

- 49 Task で 392 分（0076 の単独の Run の約 1.4 倍）。共存そのもので約 1.3 倍遅くなる（10-07 の報告）ことに加え、本番の Sandbox に本物の git がないため Repository の Test が 89 件失敗して見え、Model が Test を何度も走らせる（[GPU の Smoke の報告](https://github.com/TomokiAkiyama06/personal-ai-workspace/pull/246) の 3。Smoke では所要が Harness の 1.9 倍）。spec-04 の時間切れもこの遅さによる見込み。扱いは Decision 0090（Proposed、[#245](https://github.com/TomokiAkiyama06/personal-ai-workspace/pull/245)）。

## 3. まとめ

- Scheduler の Admission と `resize`・`DropOlderReasoning` を通した共存で、**Preemption 0 回（10-07 は 40 回）、KV の使用率の最大 87%、Pool の 95% 以上 0%**。新しい Node と Interactive の Admission の待ちは 0。
- 予約の合計は Coding の上限を 9.2% の時間超えたが、伸びを待たせない規則（0076 の 3）のとおり走っている Node は止まらず、新しい Node も待たなかった。
- 正解数は 35（0076 の Run の 36 / 36 / 36 から 1 少ない。減った 1 Task は時間切れ）。所要は 1.4 倍で、その一部は Sandbox の git の問題（Decision 0090 で扱う）。
- Human に決めてほしいこと: この 1 回で #200 の Admission の確認を終えるか、3 回に増やすか（計画の (d) は 1 回の差。Decision 0090 を入れた後に測り直す選択もある）。
