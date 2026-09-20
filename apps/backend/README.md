# Backend の配置先

Personal AI Workspace の Core Backend を置くディレクトリです。
現在は配置と役割を示す README のみで、Application の実装はまだ始めていません。

[Architecture](../../docs/ARCHITECTURE.md) に基づき、以下の機能を Backend 側で扱います。

- Core API と Session、Project / Repository の管理
- Authentication、RBAC、Capability による権限判定
- Agent Orchestrator と Task の状態・実行管理
- Memory の保存・検索・アクセス制御
- Tool Broker を通じた Tool 実行と Credential の分離

[Web](../web/README.md) と [CLI](../cli/README.md) は同じ Backend API を利用します。
権限の最終判定は Backend が行います。
Core Backend は GPU 非依存とし、Local Model Runtime を停止できる構造にします。
具体的な Service / Package の分割は、Framework と実装フェーズに合わせて選定します。

最初の実装 Issue は [PAW-020 — Backend Application Skeleton](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/17) です。
同 Issue は PAW-017 の Model 比較 Run に依存します。
言語、Framework、依存 Package はこの配置の整備では選定していません。

Acceptance Criteria は [Implementation Backlog](../../docs/IMPLEMENTATION_BACKLOG.md)、
実装時に選択できる事項は [Requirements Freeze Review](../../docs/REQUIREMENTS_FREEZE_REVIEW.md) を参照してください。
