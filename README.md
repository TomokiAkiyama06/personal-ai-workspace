# Personal AI Workspace

コーディングエージェント、長期メモリ、リサーチ、GPUの実行調整を統合する、セルフホスト型・ローカル優先のAIワークスペースです。

> **開発状況:** 要件凍結候補（Requirements Freeze Candidate）／開発初期。\
> 最初の実装マイルストーンは、ローカルのコーディング用モデルとメモリ用モデルの選定に使う、再現可能なベンチマーク・評価基盤 **Benchmark / Evaluator Harness** です。

## 目標

Personal AI Workspaceは、以下を組み合わせたセルフホスト型の統合管理基盤として設計されています。

- ローカルLLMとコーディングエージェント
- 共有クラウドエージェントとしてのCodexとClaude Code
- 構造化された長期メモリ
- Git / GitHubを使ったタスクのワークフロー
- 分離したworktreeによるエージェントの並列実行管理
- 根拠を追跡できるWebリサーチ
- GPU / VRAMリソースのスケジューリング
- 復旧を目的とした、機械可読なワークスペースのProjection（投影データ）

本プロジェクトは、以下の原則に従います。

- **ローカル優先・特定モデルに依存しない設計**
- **自信より根拠を重視**
- **高リスク操作とマージ権限は人間が管理**
- **メモリを構造化データとして扱う**
- **依存関係が許す範囲で並列実行を優先**
- **シークレットをエージェントから隔離**
- **セルフホストとプライバシーへの配慮を前提に設計**

## 現在の開発体制

初期実装フェーズでは、以下の体制で開発します。

- **Codex**が主実装エージェントを担当します。
- **Claude Code**が独立したレビュアーとして、アーキテクチャ、セキュリティ、保守性、要件との整合性を確認します。
- **Evaluator**を、機械的に検証可能な成功判定の主な仕組みとします。
- 機密性やリスクに関わる操作とマージの最終権限は、人間が承認によって行使します。タスク内でマージ権限が明示的に与えられた場合は、その範囲でエージェントがマージできます。

ベンチマークフェーズ後は、適したローカルのコーディング用モデルを、主担当または並列ワーカーとして採用できます。

## 最初のマイルストーン

最初の開発マイルストーンは、**Benchmark / Evaluator Harness**です。

以下の条件をそろえて、候補モデルを比較します。

- リポジトリと開始時点のコミット
- Issue／タスクの仕様
- システムプロンプトとツールスキーマ
- 実行時間とリソースの上限
- テストと非公開の受け入れ基準

主な測定項目は、正確性、回帰不具合の発生率、人間による修正時間、ツールの失敗、実経過時間、VRAM使用量です。

詳細は[`docs/BENCHMARK_EVALUATOR.md`](docs/BENCHMARK_EVALUATOR.md)を参照してください。

## ドキュメント

プロジェクト要件の正本は[`REQUIREMENTS.md`](REQUIREMENTS.md)です。

主な設計文書:

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- [`docs/MEMORY_ARCHITECTURE.md`](docs/MEMORY_ARCHITECTURE.md)
- [`docs/SECURITY_RBAC_AUDIT.md`](docs/SECURITY_RBAC_AUDIT.md)
- [`docs/SECURITY_TOOL_PERMISSIONS.md`](docs/SECURITY_TOOL_PERMISSIONS.md)
- [`docs/NOTIFICATION_POLICY.md`](docs/NOTIFICATION_POLICY.md)
- [`docs/UI_DESIGN.md`](docs/UI_DESIGN.md)
- [`docs/MODEL_CANDIDATES.md`](docs/MODEL_CANDIDATES.md)
- [`docs/BENCHMARK_EVALUATOR.md`](docs/BENCHMARK_EVALUATOR.md)
- [`docs/OBSERVABILITY.md`](docs/OBSERVABILITY.md)
- [`docs/DEPLOYMENT_UPDATE.md`](docs/DEPLOYMENT_UPDATE.md)
- [`docs/REQUIREMENTS_FREEZE_REVIEW.md`](docs/REQUIREMENTS_FREEZE_REVIEW.md)

エージェントのルールは[`AGENTS.md`](AGENTS.md)で定義しています。

> 詳細設計文書の多くは、現在日本語で記述されています。

## リポジトリと復旧データの境界

この公開リポジトリには、アプリケーションのソースコードと公開設計文書を格納します。

実際のワークスペースの復旧データ、ユーザーメモリ、生の会話データ、認証情報、APIトークン、秘密鍵、本番環境のシークレットを**格納してはいけません**。実際に使うRecovery Repository（復旧用リポジトリ）は、別の**非公開**リポジトリとします。

## ライセンス

Apache License 2.0です。[`LICENSE`](LICENSE)を参照してください。

## セキュリティ

脆弱性を報告する前に[`SECURITY.md`](SECURITY.md)をお読みください。認証情報や非公開のワークスペースデータを公開Issueに投稿してはいけません。
