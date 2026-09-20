# Web の配置先

Personal AI Workspace の Web UI を置くディレクトリです。
現在は配置と役割を示す README のみで、Application の実装はまだ始めていません。

[UI Design](../../docs/UI_DESIGN.md) に基づき、認証、Project / Repository、Task / Agent、Memory、管理画面を整備します。
通常の Workspace 操作は [Backend](../backend/README.md) の API を利用し、[CLI](../cli/README.md) と共通の状態を扱います。
画面上の操作可否の表示に加えて、権限の最終判定と強制は Backend が行います。
Web 側で独立した認可ルールを確定する構成にはしません。

最初の実装 Issue は [PAW-060 — Web UI Application Shell / Authentication UI](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/46) です。
同 Issue は PAW-022 と PAW-023 の認証機能に依存します。
言語、Framework、依存 Package はこの配置の整備では選定していません。

Acceptance Criteria と後続 UI の Issue は [Implementation Backlog](../../docs/IMPLEMENTATION_BACKLOG.md)、
Backend との境界は [Architecture](../../docs/ARCHITECTURE.md) を参照してください。
