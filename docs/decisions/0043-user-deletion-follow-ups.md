# User 削除の後続（30 日後の消去・実行中の Task の停止・外部の認証）と、Pairing した新しい端末での Passkey の追加

- Status: Proposed
- Date: 2026-09-28
- Scope: Issue [#127](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/127)。[Decision 0033](0033-user-invitation-and-device-pairing.md)（Approved）が後続とした判断点 9（`pending_deletion` → `deleted` の 30 日後の消去、実行中 Agent の停止、外部の認証の停止）と判断点 6（端末に固定された Passkey しか持たない Owner / Admin が、Pairing した新しい端末で Passkey を追加できない）
- Supersedes: なし（0033 は書き換えない。0033 の判断点 10・11（Login name の予約、`users` の行の Tombstone）はそのまま守る）

## 背景

[REQUIREMENTS.md](../../REQUIREMENTS.md) の `[FIXED]`「User Lifecycle」「User Deletion Retention」は、削除した時点で新規 Login 不可・既存 Session の即失効・新規 Agent の実行不可・**実行中 Agent の安全停止・外部認証の利用停止**を、30 日後に**個人データの完全削除**（Password hash、Passkey、Private Chat、Private Memory、個人設定、GitHub 認証情報、個人用 Files。さらに Recovery Projection・Recovery Git 履歴・管理下の clone / cache・DB backup / WAL）を求め、**消去と検証が終わるまで `Deleted` と表示しない**、消去の失敗は `Pending deletion` のまま Owner へ通知する、と定めている。Audit Log は消さない。

Decision 0033（PR #123）は削除を `pending_deletion` にするところまでを実装し、その Transaction で全 Session と Pairing を失効した。30 日経っても個人データは消えず、実行中の Task も止まらない。

要件は、消去の実行者・対象の Table・拒否の条件・「検証」の意味、Task の止め方、外部の認証の止め方を定めていない。この Issue の PR は動かすために次の選択を置いた。**判断点 1〜10 は、PR が推奨どおりに実装済み**（承認で変える点があれば、PR を直す）。**判断点 11（0033-6）は提案だけで、承認まで実装しない。**

## 提案

### A. 削除の開始時（`active` → `pending_deletion`）

- 既存（0033）: 全 Session の失効（`account_closed`）、生きている Pairing の失効、Audit。`SessionPrincipalProvider` は `active` でない User を匿名にし、`DatabasePrincipalDirectory`（Agent の委任）は `active` 以外を解決しない（Issue #127 で確かめた）。
- 追加: 同じ Transaction で、その User の開始済みの Passkey の Ceremony（`passkey_challenges`）を消す。
- **Password と Passkey は開始時には消さない・失効しない**（判断点 2）。Login は `active` 以外を拒否するので使えない。30 日以内の復元で同じ Account に戻れるように残し、30 日後の消去で消す。
- Password の Reset Token（`setup_tokens` の `password_reset`）は、受け取り（`TokenRedeemer`）が `invited` / `active` 以外を拒否するので使えない。Web の Role には `setup_tokens` の失効の権限がないため、開始時には触れず、消去で消す。

### B. 実行中の Task の停止（判断点 3）

- Backend の中の定期の Loop（`paw_backend/orchestrator/user_sweep.py`、`PAW_USER_TASK_STOP_INTERVAL_SECONDS`、既定 60 秒、0 で止める）が、`pending_deletion`（と `deleted`）の User が作った Active な Task（queued・running・waiting・paused・evaluating）と、その User の Task の Active な Queue Entry を探して止める。
- 止め方は Project の削除（Decision 0008 の 8 節）と同じ **Cancel**（安全な停止。Worker は今の Step を区切りで終え、次の Step を始められない。Branch・Worktree・途中の結果は残る）。`Actor.policy()`、理由 `User deletion started`。Task の Cancel と Queue Entry の Cancel を 1 つの Transaction で行い、その Transaction で User の行を `FOR SHARE` で Lock する。復元は同じ行を `FOR NO KEY UPDATE` で Lock するので、**復元の後に Task を止めることはない**。
- Outbox の Table は作らず、**状態から探す**（Migration が要らない。削除と競って後から現れた Task も次の周期で止まる。復元した User は一覧に出ない）。
- 共有 Project の中の、その User の Task も止める（要件の「実行中 Agent」はその人の Agent）。復元しても Cancel した Task は Cancel のまま（Restart できる）。
- 1 つ止めるたびに `auth.user.task_stop` / `user_deletion`（Actor なし、`resource_kind` は `task`、Project つき）を Best Effort で書く。
- 削除から止まるまでの遅れは最大で周期（既定 60 秒）。削除の Transaction の中で Worker は止められない（Project の削除と同じ理由）。

### C. 外部の認証（判断点 4）

- GitHub（`gh auth`、`~/.config/gh`）と SSH の鍵（Decision 0029 の User ごとの鍵と `authorized_keys`）は DB になく、各 User の Linux Account の中にある。Codex / Claude の Connection は Workspace 共有（Admin の管理）で、User ごとの Credential は DB にない。
- Backend がその User の Linux Account として `git` / `gh` を動かす経路（`LoginNameAccountDirectory`）は `status = 'active'` の User だけを引くので、**削除の時点でその User として外部に出る経路は止まる**。Agent の委任も止まる（上記）。
- 鍵そのものの失効（その User の `authorized_keys` から Backend の鍵の行を消す、`gh auth logout`）は配備側の作業とし、Backend は User の HOME に触れない。0029 の「鍵の失効」の手順を運用に使う。

### D. 30 日後の消去（判断点 1・5〜10）

- **実行者**: `python -m paw_backend.cli user-erasure-run` を systemd の Timer（`deploy/systemd/paw-user-erasure.*`、`OnCalendar=daily`、`Persistent=true`）が 1 日 1 回、**Table の Owner**（`PAW_MIGRATION_DATABASE_URL`、専用の OS User `paw-maint`）で動かす。Decision 0031（`audit-retention-run`）と同じ形。Web の Role は消去もできず（多くの Table に DELETE がない）、`deleted` にもできない（`paw_change_user_status` は `pending_deletion` → `deleted` を許さない）。**侵害された Web の Process が、Account を早く消す・消さずに `deleted` と表示することはできない**。
- **対象**: `pending_deletion` になってから 720 時間以上経った User（`user_status_changes` の最新の `pending_deletion` への変更から。`greatest(now, clock_timestamp())` で判定）。**復元が `retention_expired` で拒否される User とちょうど同じ**で、復元できる User を消すことはない。Owner は対象外。
- **User ごとに 1 つの Transaction**: User の行を `FOR NO KEY UPDATE`（`lock_timeout` 5 秒）で Lock し、対象かを確かめ直す。
  1. 拒否（何も変えない）: その User の Active な Task / Queue Entry が残る（`tasks_active`。B の Loop が止めるまで消さない。走っている Agent と競わない）。管理下の Checkout（`repository_checkouts`）が残る（`checkouts_remaining`。判断点 7）。
  2. 消す（判断点 5）: Password の Hash、Passkey、Passkey の Challenge、Session、Pairing、招待、Setup / Reset の Token、本人の Conversation とその Message・Session State・Journal（Private Chat。これを出典にした Memory の Source には `source_deleted_at` を付け、参照は外部キーの `SET NULL` で外れる）、`user` Scope の Memory の Version と Version が 1 つも残らない Memory、Consolidation の Key、本人が提案して**承認されなかった** Shared Memory の候補（`shared_memory_candidates` の `pending`・`rejected`。候補は出典の Memory の Title・本文の複製を持つ。`pending` を残すと消去の後に Admin が承認して全員に見える Shared Memory になり、`rejected` を残すと本文がずっと残る）（Private Memory。後から Project へ広げた Memory は広げた Version が残る）、`connection_quotas`（個人設定）、Project の Membership（判断点 9）。
  3. 検証: 同じ Transaction で、それらの Table（と `repository_checkouts`、`user` Scope の Memory、本人の `pending`・`rejected` の候補）にその User の行が 0 行であることを数え直す。違えば全体を戻す（`verification_failed`）。
  4. **DB の外の複製の消去を運用者が確認したときだけ**（`--copies-erased <user id>`。DB の Backup / WAL、User の Linux Account の中の Files と GitHub / SSH の認証情報、Recovery の複製）: `users.status` を `deleted` にし、`user_status_changes` に 1 行（`changed_by` は NULL = System）、`auth.user.erase` / `copies_confirmed` と `erased`（Actor なし）を書く。状態・履歴・Audit は消去と一緒に Commit するか、何も残らない。
  5. 確認がないとき: DB の中の消去と検証は Commit する（`auth.user.erase` allow `data_erased`。DB の個人データは運用者を待たずに消す）が、User は `pending_deletion` のまま（deny `copies_pending`、終了コード 3 で Owner に通知）。確認を付けた後の実行が検証をやり直して（消すものは残っていない）`deleted` にする。要件の「消去と検証が終わるまで `Deleted` と表示しない」を守るため（判断点 6）。消去の後の User は 30 日を過ぎているので復元もできない。
- **User を指す列の全体と、消去での扱い**（判断点 5）。Schema の User を指す列（`user_id`・`owner_user_id`・`created_by`・`requester_user_id`・`proposer_user_id`・`actor_user_id`・`actor_id`・`approver_id`・`decided_by`・`changed_by`・`invited_by`・`revoked_by`・`updated_by`・`added_by`）を持つ Table をすべて挙げる。「ID だけ残す」は、残る行が User の ID を指すが本人の書いた本文を持たないもの。「本文ごと残す」は、本人が書いた・本人に由来する本文が消去の後も残るもの。

  | Table（列） | 扱い |
  |---|---|
  | `password_credentials`・`user_passkeys`・`passkey_challenges`・`auth_sessions`・`device_pairings`・`user_invitations`（`user_id`）・`setup_tokens`（`user_id`） | 消す |
  | `conversations`（`owner_user_id`）と `messages`・`session_states`（Conversation の外部キーで）、`memory_journal_entries`（`owner_user_id`）と `memory_consolidation_queue`（Journal の外部キーで） | 消す |
  | `memory_versions`（`scope = 'user'` の `owner_user_id`）と、その `memory_embeddings`・`memory_relations`・`memory_sources`・`memory_metadata_changes`（外部キーで）、Version が 1 つも残らない `memories`、`memory_consolidation_keys`（`owner_user_id`） | 消す |
  | `shared_memory_candidates`（`proposer_user_id`、`state` が `pending`・`rejected`） | 消す（上記） |
  | `connection_quotas`・`project_members`（`user_id`） | 消す |
  | `repository_checkouts`（`user_id`） | 運用者の申告の後に消す（判断点 7） |
  | `users`（ID・Login name・Role・時刻）、`user_status_changes`（`user_id`・`changed_by`） | ID だけ残す（最小限の削除記録。Login name は 0033 の判断点 10 のとおり予約のまま） |
  | `audit_events`（`actor_id`・`resource_id`） | ID だけ残す（表示は「Deleted User」） |
  | `auth_policy_changes`・`auth_policy`（`changed_by`・`updated_by`）、`user_invitations`（他人の招待の `invited_by`）、`shared_memory_candidates`（他人の候補の `decided_by`）、`tool_approvals`（`approver_id`・`revoked_by`）、`projects`・`repositories`（`created_by`） | ID だけ残す（本人が行った決定・操作の記録） |
  | `task_repositories`（`added_by`。Working Set に Repo を加えた User。Revision 0085） | ID だけ残す（Project の Task の記録。Repo の ID・役割・Commit だけで本文はない） |
| `connection_usage`（`user_id`） | ID だけ残す（使用量の記録。種類・Model・目的・状態だけで本文はない） |
  | `shared_memory_candidates`（`proposer_user_id`、`state = 'approved'`） | **本文ごと残す**（決定の記録。その本文は承認で Shared Memory になっている） |
  | `memory_versions`（`scope` が `user` 以外の `actor_user_id`）と `memory_metadata_changes`（`user` 以外の Version の `actor_user_id`） | **本文ごと残す**。特に、**本人が Private で作り後から Project へ広げた Memory は、広げた Version が Private の Version と同じ本文を持つ**（Private の Version を消しても、その本文は Project の Version に残る） |
  | `tasks`（`created_by`。`title`・`input`）、`task_events`（`actor_id`。`reason`・`detail`） | **本文ごと残す**（Project の記録。Task の入力は本人が書いた本文） |
  | `tool_approvals`（`requester_user_id`。`summary`・`targets`）、`tool_approval_events`（`actor_user_id`。`summary`） | **本文ごと残す**（Project の Task の承認の記録） |
  | `research_scratch_items`・`research_claims`・`research_claim_relations`・`research_claim_uses`・`research_source_relations`（`created_by`。Scratch の `title`・`query`・`content`・`summary`、Claim の `claim_text`） | **本文ごと残す**（Project の調査の記録） |
  | `auth_throttles`（`key_hash`） | 残す（Login name 等の Hash と時刻。本文はなく、期限で意味を失う） |

- **失敗・拒否**（判断点 8）: `auth.user.erase` の deny（`tasks_active`・`checkouts_remaining`・`verification_failed`・`erasure_failed`）を別の短い Transaction で Best Effort に書き、User は `pending_deletion`（Access なし）のまま、Command は終了コード 3 で終わる。`OnFailure=` の `paw-user-erasure-failure.service`（`crit` の Journal と `wall`。配備の通知経路に差し替える）が Owner に知らせる。翌日の実行で再試行する。**保留期間を延ばす意味ではない**（要件のとおり）。
- **再実行**: 消した User は `deleted` なので対象にならず、何も変わらない。2 つの実行は Advisory Lock で直列（2 つ目は終了コード 1、何もしない）。
- **Migration は足さない**（Index も足さない）。Task の検索は `tasks.created_by` を条件にするが、削除中の User は少なく、Personal Workspace の規模では Seq Scan で足りると判断した。必要になれば後で `(created_by, state)` の Index を足す。

### E. 0033-6: Pairing した新しい端末での Passkey の追加（判断点 11。**提案だけ、未実装**）

現状（Decision 0025 の規則のまま）: Passkey を 1 つも持たない User は「直近の認証」（Session の作成が Step-up の有効時間内）で登録できる。既に Passkey を持つ User は **Passkey の Step-up** が要る。端末に固定された Passkey（同期されない Security Key、Platform Authenticator）しか持たない Owner / Admin は、Pairing した新しい端末ではその Passkey を使えないので、新しい端末で 2 つ目の Passkey を登録できない（同期される Passkey なら新しい端末で Step-up できる）。

- **推奨（案 A）**: **承認つきの Pairing で作った Session を、その Session での最初の 1 つの Passkey の登録に限って「直近の Passkey の認証」とみなす。**条件をすべて満たすときだけ:
  - Session の `auth_method = pairing` で、それを作った `device_pairings` の行が `approval_required = true` かつ `completed`（信頼済み端末の **Passkey の Step-up** と、新しい端末に表示された**確認 Code の入力**を経た承認。0033 の判断点 12）。
  - Session の作成（Pairing の完了）から Step-up の有効時間（Policy の `stepup_window_minutes`、既定 30 分）以内。
  - その Session でまだ Passkey を登録していない（1 回だけ）。登録した Passkey は Step-up として記録しない（0025 の「登録は Step-up ではない」のまま）。
  - Audit は `auth.passkey.register` の allow に新しい理由（例 `pairing_approved`）を付けて区別する。
  - 理由: その承認は、既存の信頼済み端末での Passkey の Step-up と、両方の端末を手元に持つ人だけが入力できる確認 Code を要り、Password の Sign-in より強い本人確認である。これを使えないと、端末に固定された Passkey の Owner / Admin は新しい端末ごとに Password の Sign-in → 既存の Passkey（その端末では使えない）の行き止まりになり、0032 の Reset（他の Admin / Owner の操作）に頼ることになる。
  - Schema: `auth_sessions` か `device_pairings` から「Pairing の Session で登録済みか」を判定する列（または `passkey_challenges` の目的の値）が要る見込みで、Migration が要る。
- 案 B: 変えない。端末に固定された Passkey の Owner / Admin は、同期される Passkey を使うか、既存の端末から 0032 の Reset / 別の経路で対応する。
- 案 C: 新しい端末で Password の再入力を Step-up として受け付ける。Owner / Admin の重要操作は Passkey の Step-up を要る方針（0025）と合わないので推奨しない。

## 影響

- 新しい Module: `paw_backend/orchestrator/user_sweep.py`（Task の停止と Loop）、`paw_backend/auth/onboarding/erasure.py`（消去）、`paw_backend/cli/erasure.py`（Command）。`paw_backend/cli/dispatch.py` が `user-erasure-run` を振り分ける。
- 新しい設定 `PAW_USER_TASK_STOP_INTERVAL_SECONDS`（既定 60、0 で止める、10〜3600）。
- 新しい systemd の Unit（例）: `paw-user-erasure.service`・`.timer`・`-failure.service`。Environment File は `/etc/paw/user-erasure.env`（`chmod 600`。`audit-retention.env` と同じ File でもよい）。
- 新しい Audit の値: `auth.user.task_stop`（`user_deletion`）、`auth.user.erase`（`erased`・`checkouts_released`・`data_erased`・`copies_confirmed`・`tasks_active`・`checkouts_remaining`・`copies_pending`・`verification_failed`・`erasure_failed`）。`audit_events` の列・CHECK は変えない。
- Migration なし。

## リスク

- **DB の外の複製は Job が消さず、確かめられない**: DB の Backup / WAL、Recovery Projection・Recovery Git の履歴（まだこの System にない）、User の Linux Account の中の Files と認証情報。要件は、これらの消去と検証まで `Deleted` と表示しないことを求めるので、Job は**運用者の確認（`--copies-erased`）があるまで `deleted` にしない**（判断点 6）。確認は運用者の申告を信じる（Audit の `copies_confirmed` に残る）。運用者が確認しない限り、その User は毎日 `copies_pending` で通知される。Backup / Recovery の機能を入れる PR は、自分の消去の手順をこの Job に足して、確認の範囲を狭められる（例: PR #138 の Memory Markdown Projection は Private Memory を Files に書くので、マージの前に消去の手順が要る）。
- 消去は取り消せない。30 日の判定を誤る（時計の誤り）と早く消す。判定は DB の時計と `now` の遅い方（復元の判定と同じ）で、復元できる User は消さない。
- Checkout の Directory が本当に消えたかは、Job からは確かめられない（`ProtectHome=true`、別の Linux User）。運用者の `--checkouts-removed` の申告を信じ、Audit（`checkouts_released`）に残す。
- 実際の systemd・通知経路での動作は確かめていない（Unit File は Test で読むだけ）。

## 決めてほしいこと

1. 30 日後の消去を、**Table の Owner の定期の Command（systemd の Timer、1 日 1 回）**にする（推奨。0031 と同じ形）か、Backend の中の Loop にする（Web の Role に DELETE と `deleted` への変更を与えることになる）か。
2. 削除の開始時に Password と Passkey を**消さず・失効せず、30 日後に消す**（推奨。復元で同じ Account に戻れる。Login は `active` 以外を拒否する）か、開始時に失効する（復元した Owner / Admin は Passkey を登録し直す）か。
3. 実行中の Task を、Project の削除と同じ **Cancel で、状態から探す定期の Loop（既定 60 秒）**で止める（推奨）か、Stop Now（即時中断）にするか、Outbox の Table を作るか。共有 Project の中の、その User の Task も止める（推奨）か。
4. GitHub / SSH の鍵そのものの失効を**配備側の作業**とし、Backend は `active` 以外の User として外部に出ないことで止める（推奨）か、Backend が鍵の失効まで自動で行う（User の HOME / `authorized_keys` に触れる仕組みが要る）か。
5. 消す個人データと残すもの（D 節の表）はこれでよいか。特に:
   - Connection の使用量の記録（`connection_usage`）、Task の入力と Event、Tool の承認の要約、Research Scratch と Claim、本人が書いた Project の Memory の Version を**本文ごと残す**（推奨。Project に属する記録）点。
   - **Private から Project へ広げた Memory は、広げた Version が Private の本文を持ったまま残る**（推奨。広げた時点で Project の記録になった）点。
   - 本人の Shared Memory の候補のうち、`pending`・`rejected` を**消し**、`approved` を**本文ごと残す**（推奨。`approved` の本文は既に Shared Memory）点。候補の Table の Migration（0046）は「候補は決定の記録で消さない」としているが、Web の Role の権限は変えず、消すのは消去の Job（Table の Owner）だけである。
   - （追加の選択）削除の開始から消去までの 30 日間、本人の `pending` の候補を Admin が承認できる。これを止める（`approve_candidate` が `active` でない提案者の候補を拒否する、または削除の開始時に `rejected` にする）か、復元できる期間なので止めない（推奨。この PR では変えない）か。
6. `deleted` にする条件を、**DB の中の消去と検証が済み、管理下の Checkout が残らず、運用者が DB の外の複製（Backup / WAL、Linux Account の中の Files と認証情報、Recovery）の消去を `--copies-erased` で確認したこと**とする（推奨。要件の「消去と検証が終わるまで `Deleted` と表示しない」のとおり。確認がなくても DB の中の個人データは 30 日で消し、User は `pending_deletion` のまま毎日通知する）か、DB の中の消去と検証だけで `deleted` にする（要件からの逸脱。Codex Review の P1 の指摘で、推奨をこの案から変えた）か。
7. 管理下の Checkout が残る User を**拒否し、運用者の `--checkouts-removed <user id>` の申告で続ける**（推奨）か、Checkout の行を消して `deleted` にするか、Checkout の Directory の削除まで自動で行うか。
8. 消去の拒否・失敗を、**User を `pending_deletion` のまま Audit の deny と終了コード 3（`OnFailure=` の通知）で知らせ、毎日再試行する**（推奨）か、別の方法（Notification Policy の経路）にするか。
9. 消去で Project の Membership（`project_members`）の行を**消す**（推奨。Account はもう戻らない。Manager の規則は既に `active` の User だけを数える）か、残すか。
10. Active な Task / Queue Entry が残る User の消去を**拒否する**（推奨。B の Loop が止めてから翌日に消す）か、消去の Job が自分で Task を止めてから消すか。
11. （0033-6）承認つきの Pairing で作った Session を、その Session での最初の 1 つの Passkey の登録に限って「直近の Passkey の認証」とみなす（**推奨: 案 A**。条件は E 節）か、変えない（案 B）か、Password の再入力を認める（案 C）か。**承認されるまで実装しない。**承認されれば、後続の PR で実装する（Migration が要る見込み）。

## 推測した点（実装者が要件から解釈した点）

- 要件の「個人設定」を、今の Schema では `connection_quotas`（User ごとの上限）と解釈した。User ごとの設定の Table はまだない。
- 要件の「個人用 Files」「GitHub 認証情報」は User の Linux Account の中にあり、DB の中の消去の対象ではないと解釈した（C 節、判断点 4・7）。
- 「実行中 Agent の安全停止」を、その User が作った Task の Cancel と解釈した（Agent の実行は Task に属する）。
