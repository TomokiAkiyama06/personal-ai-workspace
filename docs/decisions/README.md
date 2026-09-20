# 設計判断の記録

重要な仕様・設計判断の提案と、承認された判断の経緯を保存するディレクトリです。
現在は配置を示す README のみで、この整備による新しい設計判断の承認や Application 実装はありません。

運用は [AGENTS.md](../../AGENTS.md) の「仕様変更」に従います。

- 重要判断は `NNNN-<slug>.md` に提案し、人間 / Admin の承認を得ます。
- 既存 Decision の変更は新しい Decision から `Supersedes` で示し、既存の判断記録を直接書き換えません。

実装時選択や Benchmark 後に決める事項は [Requirements Freeze Review](../REQUIREMENTS_FREEZE_REVIEW.md)、
作業の順序と依存関係は [Implementation Backlog](../IMPLEMENTATION_BACKLOG.md) を参照してください。
