# 設計判断の記録

重要な仕様・設計判断の提案と、承認された判断の経緯を保存するディレクトリです。
判断は次の表のとおりです（`Proposed` は人間の承認を待つ提案で、承認されるまで方針としては使いません）。

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
| [0016](0016-shared-connection-adapter-policy.md) | Shared Codex / Claude Connection の Quota・Usage・Credential の方針（Quota 未設定は無制限、期間の既定は Asia/Tokyo） | Approved |
| [0017](0017-repository-registration-policy.md) | Repository の登録と Checkout の方針（置き場所、Linux Account との対応、既存 Repository の検証、削除の意味、Scope を作るときの Root の再確認など） | Approved |
| [0018](0018-memory-journal-consolidation-policy.md) | Memory の Immediate Journal と Background Consolidation の方針（Worker 出力の Scope と State、優先度、Queue の数値、高リスク領域の保留） | Approved |
| [0019](0019-hybrid-retrieval-policy.md) | Hybrid Retrieval の方針（権限を先に、Keyword・Vector・Rerank、暫定値、部品の失敗） | Approved |
| [0020](0020-project-state-gate.md) | Task / Queue の Project 状態 Gate の適用範囲（Gate の必須化、Restore 後の Restart、Active でない Project の Claim と Start） | Approved |
| [0022](0022-project-lifecycle-capabilities.md) | Project の作成・招待への応答・退出の Capability と Audit の方針（0008 の 5 を置き換え、0004 を拡張） | Approved |
| [0023](0023-audit-events-details-for-external-send.md) | 外部送信の Audit を `audit_events.details`（JSONB）へ永続化する方針（Decision 0010 の永続 Sink。0010・0004 への追補） | Approved |
| [0024](0024-memory-read-capability.md) | 読み取り専用の Capability `memory.read`（`DENIED_ONLY`）を足し、Retrieval の User Scope の認可に使う方針（Retrieval の Audit の量。Decision 0019 の 1 の具体化） | Approved |
| [0025](0025-passkey-webauthn-policy.md) | Passkey（WebAuthn）と Step-up の方針（Library、Attestation・User Verification・Resident Key・署名 Counter、Session の Gate、重要操作の Step-up、失効、`users.passkey_required` 列の扱い） | Approved |
| [0026](0026-memory-status-change-history.md) | Memory Version の Status・Stale 状態の変更履歴を Audit の記録と併存させる方針（Decision 0009 の 7・12・13 の一部を Supersede） | Approved |
| [0027](0027-audit-retention-and-partitioning.md) | `audit_events` の保存期間・Partition（月ごとの Range Partition）・退避先（`audit_events_archive`）の方針（Decision 0004 の 5.5 への追補） | Approved |
| [0028](0028-project-deletion-research-data.md) | Project 削除時の調査結果（Evidence / Claim Provenance、Research Scratch）の扱い（削除・DELETE 権限の追加・唯一の Provenance を失う Memory の扱い） | Approved |
| [0029](0029-per-user-git-runner-ssh.md) | User ごとの Linux User で git を実行する `SshGitRunner`（SSH 経由。Wrapper の許可副コマンド、鍵の発行・失効・回転、エラー処理、移行。Decision 0017 の 4 への追補。Issue #105） | Approved |
| [0030](0030-task-working-set-model.md) | Task Working Set（Multi-Repo）の単位・承認・Write範囲・完了条件（Decision 0014「決まっていないこと」1〜5を埋める） | Approved |
| [0032](0032-passkey-owner-reset.md) | 他の Account の Passkey の Reset と 1 回限りの Password 再設定の方針（誰が誰を、Password を消して再設定 Token を発行、`setup_tokens` の `password_reset` と Owner を拒否する関数、有効期間。Issue #108） | Proposed |

運用は [AGENTS.md](../../AGENTS.md) の「仕様変更」に従います。

- 重要判断は `NNNN-<slug>.md` に提案し、人間 / Admin の承認を得ます。
- 既存 Decision の変更は新しい Decision から `Supersedes` で示し、既存の判断記録を直接書き換えません。

実装時選択や Benchmark 後に決める事項は [Requirements Freeze Review](../REQUIREMENTS_FREEZE_REVIEW.md)、
作業の順序と依存関係は [Implementation Backlog](../IMPLEMENTATION_BACKLOG.md) を参照してください。
ただし、承認済みのDecisionはBacklogの順序より優先します。たとえば [Decision 0002](0002-start-workspace-implementation-before-model-comparison.md) は、
PAW-020 以降の着手をPAW-017の完了から切り離しています。
