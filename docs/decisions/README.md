# 設計判断の記録

重要な仕様・設計判断の提案と、承認された判断の経緯を保存するディレクトリです。
承認済みの判断は次の表のとおりです。

| Decision | 内容 | Status |
| --- | --- | --- |
| [0001](0001-hidden-check-boundary.md) | Hidden checkの実行境界（Python実装は防壁ではない） | Approved |
| [0002](0002-start-workspace-implementation-before-model-comparison.md) | Workspace本体の実装をModel比較Runより先に始める | Approved |
| [0003](0003-backend-cli-web-implementation-stack.md) | Backend / CLI / Web の実装スタック | Approved（承認範囲は本文を参照） |
| [0004](0004-rbac-capability-and-audit-policy.md) | RBAC・Capability・Auditの方針 | Approved |
| [0005](0005-owner-setup-and-recovery.md) | Owner の初期設定と復旧の方針 | Approved |
| [0006](0006-tool-broker-policy.md) | Tool Broker の Policy・承認・Credentialの方針 | Approved |
| [0007](0007-task-queue-budget-and-loop-policy.md) | Task Queue・Budget・Loop検知の方針 | Approved |
| [0008](0008-project-membership-and-lifecycle-policy.md) | ProjectのMembershipとLifecycleの方針 | Approved |
| [0009](0009-shared-memory-administration.md) | Shared Memory 管理の方針（Candidate、削除と復元、System Policy との優先関係） | Approved |
| [0010](0010-research-privacy-filter-policy.md) | Research Privacy Filter と Query 最小化の方針 | Approved |
| [0011](0011-research-provenance-model.md) | Researchの出典追跡（Evidence / Claim Provenance）の方針 | Approved |
| [0012](0012-research-provider-adapter-policy.md) | Research Provider Adapterの方針 | Approved |
| [0013](0013-research-scratch-task-relation.md) | Research Scratch の Task との関係と Pin / 保存の方針 | Approved |
| [0014](0014-task-working-set-persistence.md) | Task の Working Set（Multi-Repo）の永続化をPAW-032に含めず、新しいIssueで扱う | Approved |
| [0015](0015-login-session-password-policy.md) | Login・Session・Password Policy の数値と選択、Passkey Policy を Owner が変えられる設定にする方針 | Approved |
| [0020](0020-project-state-gate.md) | Task / Queue の Project 状態 Gate の適用範囲（Gate の必須化、Restore 後の Restart、Active でない Project の Claim と Start） | Approved |
| [0023](0023-audit-events-details-for-external-send.md) | 外部送信の Audit を `audit_events.details`（JSONB）へ永続化する方針（Decision 0010 の永続 Sink。0010・0004 への追補） | Approved |

運用は [AGENTS.md](../../AGENTS.md) の「仕様変更」に従います。

- 重要判断は `NNNN-<slug>.md` に提案し、人間 / Admin の承認を得ます。
- 既存 Decision の変更は新しい Decision から `Supersedes` で示し、既存の判断記録を直接書き換えません。

実装時選択や Benchmark 後に決める事項は [Requirements Freeze Review](../REQUIREMENTS_FREEZE_REVIEW.md)、
作業の順序と依存関係は [Implementation Backlog](../IMPLEMENTATION_BACKLOG.md) を参照してください。
ただし、承認済みのDecisionはBacklogの順序より優先します。たとえば [Decision 0002](0002-start-workspace-implementation-before-model-comparison.md) は、
PAW-020 以降の着手をPAW-017の完了から切り離しています。
