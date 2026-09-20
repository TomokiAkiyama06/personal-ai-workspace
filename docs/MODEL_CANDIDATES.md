# Personal AI Workspace - Model Candidates

更新日: 2026-09-16\
Status: [OPEN / BENCHMARK REQUIRED]

## 1. Selection policy

要件定義完了後、最初の実装フェーズとしてローカルモデル比較試験を行う。

採用モデルは公開ベンチマークだけでは決めず、Personal AI Workspace自身の実Repo / Issue / PR / Test / Agent workflowで評価する。

評価対象は少なくとも:
- Main Coding Agent
- Memory Worker
- Embedding
- Reranker

## 2. Memory Worker candidates

第一候補:
- Qwen3.5-2B

比較対象:
- Qwen3.5-4B

超軽量候補:
- Qwen3.5-0.8B

評価指標:
- Memory抽出Recall
- 不要Memory生成率
- User / Project / Repo Scope分類精度
- Confirmed / Inferred分類精度
- Conflict / Supersedes判定精度
- JSON Schema遵守率
- Latency
- VRAM
- GPU停止時の再開性

Memory WorkerはMain Coding Agentを圧迫しないことを優先する。
必要なら常駐せず、Pending ObservationをQueueして後処理する。

## 3. Main Coding Agent candidates

### A. Qwen3.6-35B-A3B

位置付け:
- 最新世代の有力候補
- 35B total / 3B active MoE
- Agentic coding / repository-level reasoningを強化
- Multimodal
- Apache-2.0

メリット:
- 新しいQwen世代
- Repo-level codingとAgentic Codingを明示的に強化
- Visionを利用できる
- 3B activeで計算効率が期待できる
- 長いContextを持つ

デメリット:
- 公式weight artifactが約72GB級で、96GB GPUではKV Cache / runtimeの余白が大きくない
- 新しいarchitectureのためruntime成熟度を実機確認する必要
- Coding専用post-trainingモデルではない

優先度:
- 最優先Benchmark候補

### B. KAT-Coder-V2.5-Dev

位置付け:
- Coding / Agent特化
- 35B total / 3B active
- Qwen3.6-35B-A3Bベース
- Text-only open-weight
- Apache-2.0

メリット:
- Agentic Coding特化
- Local Coding Agent用途と目的が非常に近い
- vLLM / SGLang等を想定
- 3B active

デメリット:
- Visionなし
- 約69GB級weightで96GB GPUではContext / KV余白に注意
- 新しいhybrid/MoE architectureのruntime compatibility要検証

優先度:
- 最優先Benchmark候補

### C. Qwen3-Coder-Next

位置付け:
- Coding Agent専用
- 80B total / 3B active
- Apache-2.0
- 262K context

公式GGUF例:
- Q4_K_M: 約48.4GB
- Q5_K_M: 約56.7GB
- Q6_K: 約65.5GB
- Q8: 約84.8GB
- FP8: 約80.4GB

メリット:
- Agentic Coding用として成熟度が高い
- Q5/Q4なら96GB GPUに十分な余白を残しやすい
- 3B active
- Tool/Agent用途を強く意識

デメリット:
- Qwen3.6 / KAT V2.5より世代が古い
- Quantizationによる品質低下を自前Benchmarkで確認する必要
- FP8版は96GBではKV Cache余白が小さい

優先度:
- 最優先Benchmark候補
- Q5_K_Mを特に比較

### D. Qwen3.5-27B-FP8

位置付け:
- General / Agent / Codingのバランス型
- Dense 27B
- FP8 artifact 約30.9GB
- Multimodal
- Apache-2.0

メリット:
- 96GB GPUで大きなVRAM余白
- Memory Worker / Embedding等との同時常駐が容易
- Visionあり
- 長Context
- Coding以外のResearch / General Agentにも使いやすい

デメリット:
- Coding専用モデルではない
- Dense 27Bなので、MoE 3B activeモデルより演算量が大きい可能性
- 専門Coding Agentとしての最終品質は実Repo Benchmarkで確認必須

優先度:
- 常時稼働Main Agent候補

### E. Devstral Small 2 24B

位置付け:
- Software Engineering Agent特化
- Dense 24B
- Apache-2.0
- Long Context
- Multimodal

メリット:
- Coding Agent専用設計
- 24B級で扱いやすい
- Vision利用可能
- Mistral系Agent ecosystemとの相性

デメリット:
- DenseモデルなのでMoE 3B activeより計算コストが高い可能性
- 2025末世代で、2026最新モデルとの性能差を比較する必要

優先度:
- Coding特化の比較基準として有力

### F. NVIDIA Nemotron 3.5 Lightning 30B-A3B NVFP4

位置付け:
- 30B total / 3B active
- Blackwell向けNVFP4
- 1M context
- Long-running autonomous agent / sub-agent向け
- OpenMDW License

メリット:
- 約21.6GB級で非常にVRAM効率が良い
- RTX PRO 6000 Blackwellとの相性を期待できる
- 3B active
- Memory Worker等との同時常駐余裕が大きい
- 長時間Agent用途を強く意識

デメリット:
- Coding専用モデルではない
- Apache/MITではなくOpenMDW License
- NVIDIA最適化kernel / runtime条件を確認する必要

優先度:
- 高速Agent / Parallel Worker候補
- Main Coder比較にも含める

### G. gpt-oss-120b

位置付け:
- 117B total / 5.1B active
- MXFP4
- Apache-2.0
- Reasoning / Tool use / Agentic general model

メリット:
- 1枚80GB級GPUに収まる設計
- Agentic operation / Function Calling / Reasoning
- 96GB RTX PRO 6000で利用可能性が高い
- General Agentとして強い比較対象

デメリット:
- Coding専用モデルではない
- Harmony format前提
- 約65GB級weightでMemory Workerや長KV Cacheとの共存余裕は中程度
- 2026の最新Coding-specialized modelsよりCoding性能が優れるとは限らない

優先度:
- General Agent / reasoning baseline
- Main Coding Agent候補としてもBenchmark

### H. gpt-oss-20b

位置付け:
- 21B total / 3.6B active
- MXFP4
- Apache-2.0

メリット:
- 約14GB級original weights
- 軽量
- Tool / agent用途
- 高速fallback / parallel workerに向く

デメリット:
- Main Coding Agentとしては上位候補より能力不足の可能性
- Harmony format前提

優先度:
- Fast worker / baseline


## 3A. Benchmark refresh (2026-09-16)

公開ベンチマークの再確認により、`Qwen3.6-27B-FP8` を最重要候補へ追加する。

### Qwen3.6-27B-FP8

- Dense 27B
- Official FP8 artifact: 約30.9GB
- Native context: 262,144
- Extended context: 最大約1,010,000
- Visionあり
- Apache-2.0

Qwen公式Agentic Coding benchmark:
- SWE-bench Verified: 77.2
- SWE-bench Pro: 53.5
- SWE-bench Multilingual: 71.3
- Terminal-Bench 2.0: 59.3
- SkillsBench Avg5: 48.2
- NL2Repo: 36.2
- Claw-Eval Avg: 72.4
- Claw-Eval Pass^3: 60.6

現時点では、96GB単体GPU上で
「高いCoding Agent性能 + 大きなVRAM余裕」を両立する候補として非常に重要。

### Benchmark caveat

Agentic coding benchmarkはモデル単体能力だけでなく、
以下に強く依存するため、vendor間の数値をそのまま横比較しない。

- Agent scaffold
- Tool schema
- Prompt
- Context length
- Harness version
- Test-set correction
- Sampling settings
- Timeout / CPU / RAM
- Thinking preservation

KAT-Coder側の統一再評価では、Qwen3.6-35B-A3BのSWE-bench Verifiedが
Qwen公式73.4に対して64.4まで下がっており、KAT側もharness差を原因候補として明示している。

したがってPersonal AI Workspaceでは最終的に同一Harness・同一Runtime・同一Tool setで
自前Benchmarkを行う。

### Public benchmark snapshot

| Model | SWE Verified | SWE Pro | SWE Multilingual | Terminal Bench |
|---|---:|---:|---:|---:|
| Qwen3.6-27B | 77.2 | 53.5 | 71.3 | 59.3 |
| Qwen3.6-35B-A3B | 73.4 | 49.5 | 67.2 | 51.5 |
| KAT-Coder-V2.5-Dev | 69.4 | 45.96 | 63.0 | 41.02 (TB 2.1) |
| Qwen3-Coder-Next | 70.6 | 42.7-44.3 | 62.8 | 36.2 |
| Devstral Small 2 24B | 68.0 | - | 55.7 | 22.5 |
| Nemotron 3.5 Lightning NVFP4 | 52.8 | - | 36.47 | 23.46 (TB 2.1) |
| gpt-oss-120b (high reasoning) | 62.4 | - | - | - |

注意:
- 上記は異なるvendor/harnessが混在しているため、順位表として扱わない。
- Qwen3.6-27B / 35BはQwen公式比較。
- KATはKwaipilot統一pipeline。
- Qwen3-Coder-Nextはtechnical report / SWE-Agent等。
- DevstralはMistral model card。
- NemotronはNVIDIA evaluation。
- gpt-ossはOpenAI model card。


## 4. Models that are interesting but too large for primary single-GPU local use

以下は性能比較対象として面白いが、96GB単体GPUのMain Local Modelとしては公式weight規模が大きすぎる。

- MiniMax M2.5: 公式repository約230GB
- Kimi K2.5: 約595GB
- GLM-4.7: 約717GB

これらはAPI / cloud comparison、または将来のmulti-GPU候補として扱う。

## 5. Initial benchmark order

Main Coding Agentの初期試験順候補:

1. Qwen3.6-27B-FP8
2. Qwen3.6-35B-A3B
3. KAT-Coder-V2.5-Dev
4. Qwen3-Coder-Next Q5_K_M
5. Qwen3.5-27B-FP8
6. Devstral Small 2 24B
7. NVIDIA Nemotron 3.5 Lightning 30B-A3B
8. gpt-oss-120b
9. gpt-oss-20b

この順序は採用順位ではなく、Benchmark着手順の候補。

## 6. Coding benchmark

公開Benchmarkのみでは決定しない。

必須評価:
- 実Repo Issue修正
- Repo探索
- Multi-file edit
- Multi-Repo Task
- Test生成 / Test修正
- Hidden acceptance tests
- FAIL_TO_PASS
- PASS_TO_PASS
- PR品質
- Review指摘修正
- Tool Call精度
- Infinite retry / loop率
- Human correction time
- Wall clock time
- Tokens
- Peak VRAM
- KV Cache余裕
- Concurrent Agent性能

特に重要:
- Resolved@1
- Human correction time
- 96GB内でMemory Worker / Reranker等と共存できるか

## 7. Runtime / VRAM policy

Main Coding Agentの性能だけでなく、同時常駐する以下を含めて評価する。

- Memory Worker
- Embedding
- Reranker
- Agent parallelism
- KV Cache
- vLLM / SGLang runtime overhead

Main ModelがVRAMを使い切る構成は避ける。
大モデルを使う場合はMemory Workerを停止 / unloadしてPending Observationを後処理する設計を許容する。

## 8. Decision timing

最終採用モデルは要件定義完了後にBenchmarkして決定する。
要件定義中は特定モデルへArchitectureを固定しない。
