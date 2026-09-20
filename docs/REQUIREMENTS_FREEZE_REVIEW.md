# Requirements Freeze Review

更新日: 2026-09-20
Status: [FREEZE CANDIDATE]

## Goal

実装開始前に、未決事項を以下へ分類する。

- `BLOCKING`: 実装開始前に決める必要がある
- `BENCHMARK`: Benchmark / Evaluator実装後に決める
- `IMPLEMENTATION_CHOICE`: 要件を満たす範囲で実装時に選べる
- `FUTURE`: V1外
- `STALE`: 既に決定済みだが旧記述が残っている

## Known intentionally deferred items

- Main Coding Model selection -> BENCHMARK
- Memory Worker Model selection -> BENCHMARK
- Embedding / Reranker model selection -> BENCHMARK
- Memory Context token budget -> BENCHMARK
- vLLM / SGLang / llama.cpp role -> BENCHMARK / IMPLEMENTATION_CHOICE
- Concrete deployment packaging (Docker/systemd etc.) -> IMPLEMENTATION_CHOICE
- Exact Credential Vault implementation -> IMPLEMENTATION_CHOICE, security requirements already fixed
- Benchmark seed task count / hidden-test storage / runtime details -> BENCHMARK

## Next action

旧 `OPEN ITEMS` を一つずつ棚卸しし、
本当にRequirements blockingなものだけを残す。



## IDE extension decision

`Dedicated IDE Extension` -> `FUTURE`

V1はExisting Remote SSH GUI + CLI + Personal AI Workspace GUIで運用する。
IDE固有Context取得はV1 blocking requirementではない。



## Shared Memory

`Shared Memory permissions` -> `FIXED`

- Workspace-wide read for Active Users
- Write / delete / restore / promotion approval = Owner/Admin
- Agent auto-promotion = prohibited
- System/Security Policy remains outside and above Memory precedence


## Blocking requirement review (v51)

現時点で、実装開始前にUser判断が必要な `BLOCKING OPEN` は見つかっていない。

残る未確定値は意図的に以下へ延期する。

### BENCHMARK
- Main Coding Model
- Memory Worker Model
- Research Worker Model
- Embedding / Reranker
- Memory Context token budget
- Runtime / quantization / KV Cache具体値
- Benchmark seed task数・hidden-test保管方式等

### IMPLEMENTATION CHOICE
- Credential Vaultの具体製品 / storage方式
- Docker / systemd / release directory等のdeployment mechanism
- PostgreSQL backup utilityの具体選定
- Backend / frontend frameworkの最終選択
- Observability storage implementation

### FUTURE
- Dedicated IDE Extension
- Artifact / Slide generation
- MIGによる強いGPU partition
- Full off-server PostgreSQL replica / object-storage backup

要件定義は `Requirements Freeze candidate` として扱える状態。
次フェーズはBenchmark / Evaluator Harnessの仕様確定と実装。
