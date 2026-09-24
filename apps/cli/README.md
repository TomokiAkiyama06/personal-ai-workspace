# CLI の配置先

Personal AI Workspace の CLI を置くディレクトリです。
現在は配置と役割を示す README のみで、Application の実装はまだ始めていません。

[Architecture](../../docs/ARCHITECTURE.md) に従い、CLI と [Web](../web/README.md) は同じ [Backend](../backend/README.md) API を利用します。
Task の状態、権限判定、Tool Broker、Agent Orchestrator を Backend 側で共有し、CLI 固有の認可ルールで権限を迂回しません。

初期 Owner の作成と復旧のコマンド（[PAW-021](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/18)）は、この `apps/cli/` の Client では**ありません**。
Backend の Package に含まれる server-local の管理コマンド `python -m paw_backend.cli` で、Server 上で DB の認証情報を使って実行します
（使い方は [Backend README](../backend/README.md#owner-の初期設定と復旧) を参照）。
最初の Owner を公開 HTTP API 経由で作れてしまうと、最初に Web へ来た人が Owner になれてしまうため、この `apps/cli/` の Client は Owner を作成・復旧できません。
言語は [Decision 0003](../../docs/decisions/0003-backend-cli-web-implementation-stack.md) で Python と決定済みです。
この Client は Backend の公開 HTTP API だけを呼びます。依存 Package は実装時に選定します。

関連する要件は [REQUIREMENTS.md](../../REQUIREMENTS.md)、
作業順序は [Implementation Backlog](../../docs/IMPLEMENTATION_BACKLOG.md) を参照してください。
