# Evaluator の準備領域

Agent の成果を Test や Acceptance Criteria で機械的に検証するためのディレクトリです。
現在は配置と役割を示す README のみで、Evaluator の実装はまだ始めていません。

判定方針と実装順序は [Benchmark / Evaluator 設計](../docs/BENCHMARK_EVALUATOR.md) に従います。
Agent 自身の完了報告や自己評価を成功判定に使わず、実行可能な検証結果を記録します。

- [PAW-011 — Evaluator Result Schema](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/8)：Test、回帰、禁止変更等の判定結果を保存する形式を定義する。PAW-010 に依存する。
- [PAW-013 — Test / Hidden Acceptance Runner](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/10)：Visible Test と Hidden Test を分離し、実行結果を記録する。PAW-010 と PAW-012 に依存する。

Hidden Test の保管方式は設計文書の未決事項です。
PAW-013 の「Candidate から Hidden Test 本文を読めない」という条件を満たす配置を、同 Issue の作業で検討します。
この初期整備では Hidden Test の保存先や隔離方式を定義していません。

比較 Task と候補 Agent の実行については [Benchmark](../benchmarks/README.md)、
各 Issue の Acceptance Criteria は [Implementation Backlog](../docs/IMPLEMENTATION_BACKLOG.md) を参照してください。
