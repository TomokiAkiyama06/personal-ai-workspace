# Benchmark / Evaluator Design

更新日: 2026-09-16
Status: [FIXED DIRECTION / BENCHMARK DETAILS]

## Purpose

要件定義完了後、Personal AI Workspace本体より先に評価基盤を作る。

公開Benchmarkだけではなく、ユーザー自身のRepo / Issue / PR / Test環境で
候補Coding ModelとMemory Workerを比較し、採用モデルを決定する。

## Coding Agent evaluation hierarchy

1. Executable Evaluator — 主判定
2. Claude + Codex — 独立レビュー
3. Human — 最終確認

LLMレビューだけでPASS/FAILを決定しない。

## Executable checks

- Build
- Unit Test
- Integration Test
- Hidden Acceptance Test
- Regression Test
- Lint
- Type Check
- Security Check
- Forbidden Changes
- FAIL_TO_PASS
- PASS_TO_PASS

## Benchmark task sources

- Historical real bug / issue
- Spec-based feature task
- Injected bug

Historical taskでは:
- Buggy commitでtestがFAILする
- Known-good commitでtestがPASSする
- Candidate patchでもPASSできるか

を重視する。

## Comparison metrics

- Resolved@1
- Resolved@N
- FAIL_TO_PASS
- PASS_TO_PASS
- Hidden test pass rate
- Build success
- Tool failure rate
- Infinite loop / retry rate
- Wall clock time
- Tokens
- Peak VRAM
- KV cache headroom
- Human correction time
- PR / review findings
- Regression count

## Fair comparison rules

候補モデル間で以下を揃える:
- System Prompt
- Tool schema
- Repo / starting commit
- Issue text
- Context limit
- Agent loop
- Timeout
- Runtime resource limits
- Evaluator version

Agent harness差で公開ベンチマーク値が大きく変動し得るため、
最終判断はこの統一Harnessの結果を優先する。

## Initial implementation order

1. Task schema
2. Evaluator result schema
3. Repo reset / isolated worktree runner
4. Test runner
5. Hidden test runner
6. Metrics collector
7. Candidate adapter
8. Claude / Codex review adapter
9. Result report UI / export
10. Seed benchmark execution

## [BENCHMARK] Deferred details

- Seed benchmark task数
- 最初に使うRepo
- Hidden testの保管方式
- Container isolation方式
- Candidateごとのthinking/reasoning budgetの揃え方
- Quantization比較の扱い
- Model warmup / cold-startを評価へ含めるか



## Model storage during benchmark

[FIXED]

Benchmark fairness / throughputのため、比較対象modelは原則NVMe SSDへ配置する。

目的:
- HDD cold-load時間をBenchmark結果へ混在させない
- Model switchingを高速化する
- Candidate間のAgent / inference性能比較へ集中する

Model load latency自体を評価したい場合のみ、
別のStorage BenchmarkとしてHDD / NVMeを個別測定する。

Main Coding Agent採用後の本番運用ではHDD Model Storeへ戻す。
