# Benchmark の準備領域

Benchmark Task の仕様、候補 Agent の比較、公開可能な評価用データを整備するためのディレクトリです。
現在は配置と役割を示す README のみで、Benchmark の実装・実行はまだ始めていません。

実装順序は [Benchmark / Evaluator 設計](../docs/BENCHMARK_EVALUATOR.md) と
[Implementation Backlog](../docs/IMPLEMENTATION_BACKLOG.md) に従います。

- [PAW-010 — Benchmark Task Schema](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/7)：Task の機械可読な形式を定義する。PAW-001 に依存する。
- [PAW-012 — 隔離 Worktree Runner](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/9)：同一の開始状態から候補 Agent を実行する。
- [PAW-015 — Candidate Adapter Interface](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/12)：モデルや Runtime の違いを共通 Interface で扱う。
- [PAW-016 — Seed Benchmark Dataset](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/13)：仕様と検証可能な挙動に基づく初期評価セットを作る。

実装時に必要となる下位ディレクトリは、各 Issue の作業で追加します。
機械的な判定と結果の扱いについては [Evaluator](../evaluator/README.md) を参照してください。

実データを追加する際は [Security Policy](../SECURITY.md) に従います。
Private Dataset、実際の会話・Memory、Credential、Model Weights はこの Public Repository に保存しません。
