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
| [0021](0021-dag-orchestrator-policy.md) | DAG Agent Orchestrator の方針（Plan の形、Role、Escalation、並列数、結果の受け渡し、Sub-Agent の権限と予算） | Approved |
| [0022](0022-project-lifecycle-capabilities.md) | Project の作成・招待への応答・退出の Capability と Audit の方針（0008 の 5 を置き換え、0004 を拡張） | Approved |
| [0023](0023-audit-events-details-for-external-send.md) | 外部送信の Audit を `audit_events.details`（JSONB）へ永続化する方針（Decision 0010 の永続 Sink。0010・0004 への追補） | Approved |
| [0024](0024-memory-read-capability.md) | 読み取り専用の Capability `memory.read`（`DENIED_ONLY`）を足し、Retrieval の User Scope の認可に使う方針（Retrieval の Audit の量。Decision 0019 の 1 の具体化） | Approved |
| [0025](0025-passkey-webauthn-policy.md) | Passkey（WebAuthn）と Step-up の方針（Library、Attestation・User Verification・Resident Key・署名 Counter、Session の Gate、重要操作の Step-up、失効、`users.passkey_required` 列の扱い） | Approved |
| [0026](0026-memory-status-change-history.md) | Memory Version の Status・Stale 状態の変更履歴を Audit の記録と併存させる方針（Decision 0009 の 7・12・13 の一部を Supersede） | Approved |
| [0027](0027-audit-retention-and-partitioning.md) | `audit_events` の保存期間・Partition（月ごとの Range Partition）・退避先（`audit_events_archive`）の方針（Decision 0004 の 5.5 への追補） | Approved |
| [0028](0028-project-deletion-research-data.md) | Project 削除時の調査結果（Evidence / Claim Provenance、Research Scratch）の扱い（削除・DELETE 権限の追加・唯一の Provenance を失う Memory の扱い） | Approved |
| [0029](0029-per-user-git-runner-ssh.md) | User ごとの Linux User で git を実行する `SshGitRunner`（SSH 経由。Wrapper の許可副コマンド、鍵の発行・失効・回転、エラー処理、移行。Decision 0017 の 4 への追補。Issue #105） | Approved |
| [0030](0030-task-working-set-model.md) | Task Working Set（Multi-Repo）の単位・承認・Write範囲・完了条件（Decision 0014「決まっていないこと」1〜5を埋める） | Approved |
| [0031](0031-audit-retention-scheduler.md) | `audit_events` の保存期間・退避を定期実行する仕組み（systemd timer + Server ローカルの Command `audit-retention-run`、失敗の検知・通知、実行ごとの Audit、Advisory Lock。Decision 0027 の 6 への追補。Issue #117） | Approved |
| [0032](0032-passkey-owner-reset.md) | 他の Account の Passkey の Reset と 1 回限りの Password 再設定の方針（誰が誰を、Password を消して再設定 Token を発行、`setup_tokens` の `password_reset` と Owner を拒否する関数、有効期間。Issue #108） | Approved |
| [0033](0033-user-invitation-and-device-pairing.md) | User の招待・端末の Pairing（QR / リンク、Owner / Admin の信頼済み端末の承認）・User Lifecycle の方針（招待 Token の期限、Pairing の状態遷移、削除・復元の権限、含めないこと。PAW-024） | Approved |
| [0034](0034-memory-versioning-freshness.md) | Memory の Relation の意味・手動編集の Version（誰がどの Scope を変えられるか）・手動で書ける鮮度・Stale Candidate / 期限切れ / Session 終了の処理（PAW-042。Issue #36） | Approved |
| [0035](0035-working-set-capability-grants-and-legacy-tasks.md) | `project.task.working_set.manage` の付与先と委任、作成時の Working Set、Revision 0085 より前の Task の状態の退避（Issue #85。Decision 0030 が決めていない点） | Approved |
| [0036](0036-parallel-worktree-integration.md) | Parallel Worktree / Integration Node の方針（Worker ごとの worktree・branch の置き場所と名前、Worker の branch の起点、統合の base・順序・`--no-ff`、Conflict と未 Commit の変更で `waiting`、統合後の Test → Evaluator → Review、PR / Push を別 Issue にすること、Wrapper の許可リストへの追加。Issue #31） | Approved |
| [0037](0037-gpu-compute-scheduler.md) | GPU / Compute Resource Scheduler の方針（状態をプロセス内に持つ、KV Cache の Token による Admission、Actual / Reserved の VRAM と Safety Headroom の暫定値、Class の優先、縮退の段と復帰、Exclusive、Local / Cloud の振り分け。PAW-036、Issue #32） | Approved |
| [0038](0038-memory-markdown-projection.md) | Memory Markdown Projection の出力先（`PAW_MEMORY_PROJECTION_DIR`、git の Work Tree・Home の拒否、Marker）・公開範囲ごとの配置・投影する Version・diff-friendly な形式・Secret の置換・`0700` / `0600` の権限・実行と失敗の通知（Audit・終了コード・`OnFailure=`）（PAW-045。Issue #39） | Approved |
| [0041](0041-seed-benchmark-dataset.md) | Seed Benchmark Dataset（paw-seed-v1）の構成（出典: この Repository の Merge 済み PR・Spec・Injected Bug、規模 24、難易度・カテゴリ、Hidden test の非公開の保管、候補に見せる Repository、学習データへの混入、License、人の修正時間の測り方。PAW-016） | Approved |
| [0045](0045-memory-edit-sources.md) | Memory の編集・復元・Revalidate でできた新しい Version に出典を写し、人の `user_confirmation` を足すこと・会話 / Task から由来する Version の検索（Decision 0034 の 9。Issue #128） | Approved |
| [0047](0047-task-execution-composition-and-task-end-effects.md) | 本番の Task 実行の組み立て（本番の `TaskAuthority` の Scope と Grant、App の起動時の `TaskService` / Tool Broker / Orchestrator）、Task 終了時の承認の取り消しと `session_only` の退役（Commit 後の Listener と、残りを探す再実行の Sweep）、鮮度の Job の定期実行（Issue #125） | Approved |
| [0048](0048-node-placement-audit.md) | Node の実行 Placement（local / cloud・Agent / Model）を Orchestrator の記録（`agent_dag_node_attempts` の列、1 回だけ）と外部送信の Audit（`audit_events` の既存の列だけの行、同じ Transaction）に残す方式、記録できなければ走らせない、Planner は Cloud へ回さない（Decision 0037 の 14 の実装。Issue #133） | Approved |
| [0049](0049-manual-write-reservation-release.md) | 落ちた Process が残した Repository の書き込み予約を人が解除する操作（誰が: Project の Manager と Owner / Admin、Passkey Step-up、生きている Worker の Lease がある間は拒否、解除した Repo は書き込まれたものとして扱う、Audit と Task の Event。Issue #129。Decision 0035 の 5 節の最後の一文を Supersede） | Approved |
| [0046](0046-tool-call-lease-fencing.md) | Tool 呼び出しを Queue の Lease（`claim_count`）で Fencing し、Broker の `tool_calls` を Run に計上する（Decision 0021 への追補。Issue #126、PAW-034 の B10 / B11） | Approved |
| [0042](0042-gpu-free-vram-admission.md) | 観測した空き VRAM による Admission の追加の安全確認（GPU 利用率は使わない、Footprint の外に VRAM を要る仕事は空きが要求量 + Headroom に足りなければ待たせて警告、Exclusive は外の Workload の VRAM が空くまで Unload せずに待つ。Human が方向を直接決定、細部を提案。Decision 0037 への追補。Issue #145） | Approved |
| [0050](0050-late-gpu-charge-without-fence.md) | Node の Attempt が閉じた後に、Cancel しても止まらなかった Local の呼び出しの GPU 時間を、`TrackerLateGpuCharge` で Attempt / Run の Fence なしに Task の Budget へ計上する（PAW-036、PR #131。Decision 0037 を補う） | Approved |
| [0051](0051-integration-worktree-ignored-files.md) | integration worktree の無視された File（`.gitignore`）を未 Commit の変更として扱う方針（`status --ignored` の形を Wrapper の許可リストに足す。Decision 0036 の 7・9・13 への追補。PR #130） | Approved |
| [0052](0052-integration-push-and-pull-request.md) | Integration Gate を通った integration branch の Push と PR の作成（検査した Commit を `paw/` の branch へ Force なしで Push、`target` だけ、作成者の `gh auth login`、`project.pr.create` を作成者の Agent の行為として判定、作れなければ `evaluating` のまま。Wrapper の許可リストに `push` を足す。Decision 0036 の 10 の後続、#132） | Approved |
| [0056](0056-production-worktree-wiring.md) | 本番の組み立てで Orchestrator に Parallel Worktree / Integration Node（`GitWorktreeCoordinator`）を配線する方針（無効にする設定を持たない、worktree の Root の設定と起動時の確認を持たない、git の Runner は `create_app(git_runner=...)`、worktree の要らない Task は Account を尋ねない、`IntegrationGate` は組み立てない。Issue #155） | Approved |
| [0057](0057-connection-and-node-charge-lease-fencing.md) | Shared Connection の呼び出し（`ConnectionService.execute`）を Queue の Lease で Fencing し（Fail closed。Admission の前）、`NodeBudgetHandle.charge` と GPU 時間の遅い計上は Lease で Fencing しない（Decision 0046 の 6 への追補。Issue #153） | Approved |
| [0053](0053-memory-content-length-limit.md) | Memory の本文（`memory_versions.content`）の DB の長さの上限（20,000 文字の CHECK、Migration 0147）・上限を超える既存の行があれば Migration を止めて何も変えない・Projection の切り詰め（Decision 0038 の 5・10）は防御として残す（Issue #147） | Approved |
| [0054](0054-recovery-repository-projection-restore.md) | Recovery Repository の Backup と Restore の方針（Checkout の置き場所と Marker、1 Entity 1 File の JSON と Manifest・Checksum、列の Allow-list と Secret の置換、削除中の User は削除記録だけ、30 分ごとの 1 Commit と Fast-forward の Push、Restore は Server ローカルで既定は Dry run・空の Workspace へ全体を 1 Transaction で・上書きと削除なし、戻さないもの、Credential の再登録。PAW-047、Issue #41） | Approved |
| [0055](0055-kaggle-full-gpu-mode.md) | Kaggle / Full GPU Mode の方針（始める・終えるのは Owner / Admin だけ: Capability `admin.compute.full_gpu`、走っている Task は `waiting`（Resource）にして Drain、Queue 中の Task は GPU を求めた時点で Hold、Preempt は明示したときだけ、Main LLM が戻ってから再開、再起動で解除、HTTP の API は後の Issue。PAW-037、Issue #33。Decision 0037 の 7・13 を補う） | Approved |

運用は [AGENTS.md](../../AGENTS.md) の「仕様変更」に従います。

- 重要判断は `NNNN-<slug>.md` に提案し、人間 / Admin の承認を得ます。
- 既存 Decision の変更は新しい Decision から `Supersedes` で示し、既存の判断記録を直接書き換えません。

実装時選択や Benchmark 後に決める事項は [Requirements Freeze Review](../REQUIREMENTS_FREEZE_REVIEW.md)、
作業の順序と依存関係は [Implementation Backlog](../IMPLEMENTATION_BACKLOG.md) を参照してください。
ただし、承認済みのDecisionはBacklogの順序より優先します。たとえば [Decision 0002](0002-start-workspace-implementation-before-model-comparison.md) は、
PAW-020 以降の着手をPAW-017の完了から切り離しています。
