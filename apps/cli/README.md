# CLI の配置先

Personal AI Workspace の CLI を置くディレクトリです。
現在は配置と役割を示す README のみで、Application の実装はまだ始めていません。

[Architecture](../../docs/ARCHITECTURE.md) に従い、CLI と [Web](../web/README.md) は同じ [Backend](../backend/README.md) API を利用します。
Task の状態、権限判定、Tool Broker、Agent Orchestrator を Backend 側で共有し、CLI 固有の認可ルールで権限を迂回しません。

初期 Owner 作成・復旧の CLI は [PAW-021 — Initial Owner Setup / Recovery CLI](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/18) に定義されています。
同 Issue は PAW-020 の Backend Skeleton に依存します。
コマンド仕様と利用経路は該当 Issue の Acceptance Criteria に従って実装します。
言語は Backend と同じ Python です（[Decision 0003](../../docs/decisions/0003-backend-cli-web-implementation-stack.md)、Approved）。
CLI は Backend の公開 HTTP API だけを呼び、権限の最終判定は Backend に置きます。依存 Package は実装 Issue で選びます。

関連する要件は [REQUIREMENTS.md](../../REQUIREMENTS.md)、
作業順序は [Implementation Backlog](../../docs/IMPLEMENTATION_BACKLOG.md) を参照してください。
