# Recovery Repository の Projection（Backup）と Restore の方針

- Status: Proposed
- Date: 2026-09-29
- Scope: Issue [#41](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/41)（PAW-047: Recovery Repository Projection / Restore）。`apps/backend/paw_backend/recovery/`、`apps/backend/paw_backend/cli/recovery.py`、`apps/backend/deploy/systemd/paw-recovery-backup*`。関連: [Decision 0038](0038-memory-markdown-projection.md)（Memory Markdown Projection。9 の「PAW-047 が写すときの条件」）、[Decision 0031](0031-audit-retention-scheduler.md)（定期実行と失敗の通知の型）、[Decision 0005](0005-owner-setup-and-recovery.md)（Owner の復旧）、[Decision 0032](0032-passkey-owner-reset.md)（他の Account の Reset）、Decision 0043（Proposed、PR #142。User 削除の消去）
- Supersedes: なし（既存の Decision を書き換えない。要件が決めていない点を埋める）

## 背景

[REQUIREMENTS.md](../../REQUIREMENTS.md)（「Memory Markdown Backup / Backup Authority」「Dedicated Recovery Repository」「User Deletion Retention」）と [MEMORY_ARCHITECTURE.md](../MEMORY_ARCHITECTURE.md)（5 節、「Dedicated Recovery Repository」）は次を `[FIXED]` として定める。

- PostgreSQL が Operational Source of Truth、Recovery Git は Disaster Recovery Source。通常時は Git から DB へ同期しない。障害時だけ Recovery Import する。
- 保存するもの: Memory Markdown Projection、Memory の Version / Relation を再構築する Machine-readable な Metadata、User（Secret を除く）・Project・Repo・Member / ACL・Agent Policy・Quota・Model / Router・Notification の設定、Task の Recovery Summary、Recovery Format の Version、Workspace の Schema の Version、Checksum / generated_at。
- 保存しないもの: DB の Binary・Dump・WAL、Password Hash、Passkey の Secret、API Token、SSH の秘密鍵、Secret の平文、暗号鍵、Runtime Cache。
- Git の Schedule: dirty flag、30 分ごとの Batch の確認、変更がなければ Push しない、複数の変更を 1 Commit にまとめる、手動の Backup、失敗の Retry と通知。
- 目標: 「新品の Ubuntu + Recovery Git + 外部 Account の再認証」から主要な状態を再構築する。Credential は再登録する。
- 復旧でも削除済みの個人データを再生成しない。復元元とは独立した最新の削除状態を適用し、確認できなければ復旧を完了扱いしない。

Issue の受け入れ条件は「30 分の dirty-check batch commit/push」「Machine-readable な User / Project / Repo / ACL / Policy / Quota の Metadata」「Schema / Recovery Format の Version」「Secret / DB dump / WAL を Git に保存しない」「Fresh Ubuntu から主要状態を再構築する Restore の流れ」「Credential は再登録」。
要件が決めていないのは、**Checkout の置き場所と安全の条件**、**ファイルの形式と配置**、**削除中の User の扱い**、**Restore の範囲・上書き・誰が実行するか**、**Restore の元の検証**、**戻さないもの**、**削除状態の独立した確認**である。
この Decision はその推奨を示す。PR は推奨どおりに実装しており、承認されない点があれば別の PR で直す。**Restore は Data に影響する操作なので、既定は Dry run で、破壊的な既定（上書き・削除）を持たない。**

## 提案

### 1. Checkout: `PAW_RECOVERY_REPOSITORY_DIR`（既定なし）と、拒否する場所

- Backup と Restore は、運用者が用意した**専用の Private Repository の Clone**（例 `/srv/personal-ai/recovery`）を `PAW_RECOVERY_REPOSITORY_DIR` で受け取る。**既定値はない**（未設定なら Command は何もせず終了コード 2）。Remote の作成・Private であることの確認・Push の鍵（Deploy Key）は配備の作業で、Job は Remote を作らず、変えない。
- 実行のたびに次を確かめ、満たさなければ何も書かずに失敗する（`check_repository:<理由>`）。
  - 絶対・正規の Path（Symbolic Link・`..` なし）、`/` ではない、自分の所有の Directory。
  - **人の Home の中でも上でもない**（Decision 0038 と同じ判定。Project の Checkout はすべて Home にある）。**Memory Projection の Directory と重ならない**。
  - **git の Work Tree の最上位**（`git rev-parse --show-toplevel` が同じ Path）で、`.git` が Directory（Linked worktree・Submodule ではない）。
  - **Recovery の Marker（`.paw-recovery-repository`、Commit される）を持つ**。Marker がなければ、`.git` 以外に何もない（空の Clone）ときだけ Marker を置いて自分のものにする。**他の内容があって Marker のない Checkout は拒否する**（設定の誤りで Project の Repository に書いたり Commit したりしない）。
  - 現在の Branch に Upstream（`branch.<name>.remote` と `.merge`）がある。
- Checkout の Root と Job が作る Directory は `0700`、File は `0600`。Symbolic Link は辿らない（あれば Link として消し、先には書かない）。`.git/paw-recovery.lock` の `flock` で Backup と Restore を直列にする（同時の 2 つ目は何もせず終了コード 1）。
- Job が書き換えるのは下の配置の名前だけ。`README.md` などそれ以外の File は読まず、消さず、Commit しない。

### 2. 配置と形式: 1 Entity 1 File の決定的な JSON

```text
<PAW_RECOVERY_REPOSITORY_DIR>/
├── .paw-recovery-repository        # Marker
├── manifest.json                   # recovery_format_version: 1, workspace_schema_version, workspace_version, generated_at, counts, checksums_sha256
├── recovery/checksums.sha256       # 他のすべての File の sha256（sha256sum -c で読める）
├── memory/                         # Memory Markdown Projection の写し（Decision 0038 の配置のまま）
├── users/<user-id>.json            # User（Credential なし）と Quota
├── deletions/users/<user-id>.json  # 削除中の User: id と status だけ（4）
├── projects/<project-id>.json      # Project と Member（ACL）
├── repos/<repo-id>.json            # Repository、Remote、ACL の上書き（acl_allowed）
├── memory-records/<memory-id>.json # すべての Version（本文を含む）、Relation、Source
├── policies/auth-policy.json, policies/shared-connections.json
└── tasks/<task-id>.json            # Task の Recovery Summary
```

- Machine-readable な File は **JSON**（Key を整列、2 Space の Indent、UTF-8、LF、末尾の改行 1 つ）。JSON は YAML として読めるので、要件の例（`manifest.yaml`、`policies/*.yaml`）の意図を満たし、YAML の Library を増やさない。Title・Login 名は Path に使わず ID だけ（Decision 0038 2 と同じ理由）。
- `manifest.json` の `generated_at` は**その内容を最初に Backup した時刻**で、他の File が変わらない限り `manifest.json` を書き直さない（変更のない実行は Commit しない）。
- `workspace_schema_version` はこの Release の Alembic の Head（Backup の SQL が前提とする Schema）。`recovery_format_version` は形式の Version で、Restore はまずこれを確かめる。形式を変えるときは Version を上げ、古い形式の Reader を残す（要件の「Migration Layer」）。
- **Memory の Restore は `memory-records/` から行う**（全 Version の本文と Metadata）。`memory/` の Markdown は人が読むための写しで、Restore は読まない（Restore の後に `memory-projection-run` が作り直す）。

### 3. 何を入れ、何を入れないか: 列の Allow-list と Secret の置換

- 読む SQL はすべて**列を名指しする**（`recovery/source.py`）。その列の一覧が Git に入り得るものの全部である。入れるもの: User の `id`・`login_name`・`system_role`・`status`・`passkey_required`・時刻、Connection の Quota、Project と Member、Repository と Remote（DB は `https://host/path` だけを許し、User 情報を含まない）、Memory の全 Version・Relation・Source（`source_ref` など）、Auth Policy の値、Shared Connection の `kind`・`status`・`enabled`、Task の Title・状態・現在の試行の Repository ごとの Branch・PR。
- 入れないもの: `password_credentials`、Passkey、Session、Setup / Reset / 招待 / Pairing の Token、`shared_connections.secret_handle`、Conversation と Message、Embedding、Checkout（Home の中の Path）、Task の入力・Log・Tool の記録、Usage、`audit_events`、DB の Dump / WAL。
- **自由記述の値**（Memory の Title・本文・Branch・種類・変更理由・`attributes` の値、Source の参照、Relation の理由、Project の名前と説明、Task の Title・Branch・PR の URL）は、Decision 0038 5 と同じ `tools.credentials.redact_text` / `redact_value` で認識できる Credential を `[REDACTED]` にしてから書く。Version は `redactions`・`truncated` を持つ。PostgreSQL は変えない。**そのため Restore した本文は `[REDACTED]` のまま**になる（Secret を Git に入れないことを優先する）。
- 最後の防御として、Record のすべての文字列の値（上に挙げない列、例えば長さだけを確かめる Task の `head_commit` を含む）も書く前に同じ検出で置換する。
- **Repository の名前・既定の Branch・Remote の URL** も同じく置換する（DB は文字の種類しか確かめず、Token の形の文字列が入り得る）。置換した Repository・Remote は有効な名前で戻せないので、Restore は戻さず、再登録の手作業として表示する（9）。
- **Login 名**は Identity なので `[REDACTED]` にできない（有効な Login 名ではない）。Login 名が Credential の検出に当たる User は、Login 名を **`redacted-<User ID の先頭 12 桁の 16 進>`**（有効な Login 名の形）にして書き、Record に `login_name_redacted: true` を付ける。Restore はその名前で User を戻し、「**the Owner renames this user**」（Owner がこの User の名前を付け直す）を手作業として表示する。
- `session_only` の Version（Long-term Memory ではない）と、除いた Version を指す Relation・Source は入れない。
- Model / Router・Notification の設定は、まだ DB にも設定 File の形式にもないので、形式 1 には入れない（入った時点で形式の Version を上げて足す）。

### 4. 削除中・削除済みの User: 最小の削除記録だけ

- `status` が `pending_deletion` / `deleted` の User は、`deletions/users/<id>.json`（`id` と `status` だけ）だけを残す。その User の User Record・Quota・Member の行・`user` Scope の Memory の Version・`memory/users/<id>/` は**現在の Backup に入れない**。Project・Repository の `created_by` などの ID は、Audit と同じく不透明な ID として残る。
- 過去の Commit には削除前の内容が残る。Git の履歴の消去（history rewrite・force push）はこの Job では行わない（User 削除の消去の流れ、Decision 0043（Proposed）と人の承認の範囲）。
- `pending_deletion` の間（30 日以内の Restore が可能な期間）にサーバーが失われると、その User の個人データは Recovery からは戻らない。個人データを戻さない側に倒す。

### 5. 実行と失敗の通知

- `python -m paw_backend.cli recovery-backup-run` を systemd timer（`paw-recovery-backup.timer`、`OnCalendar=*:02/30`、`Persistent=true`）が 30 分ごとに起動する。手で実行すれば手動の Backup になる。分は Memory Projection（5 分ごと、:00 から）とずらし、Projection の Lock を待つことを減らす。
- 1 回の実行: Checkout を確かめて Lock → **Projection の Marker の Lock を取り（Projection の実行中なら最大 120 秒待つ）、`.paw-memory-projection-incomplete` がなく、`projection_status` の最後の実行が `memory.projection.completed` のときだけ写す**（Decision 0038 9 のとおり。満たさなければ `copy_memory:<理由>` で失敗し、何も書かない）→ DB の 1 つの Snapshot（`REPEATABLE READ, READ ONLY`）→ Render → 書く → **Render した Bytes から、専用の Index（`.git/paw-recovery-index`）で Tree を作り、Tree が変わったときだけ 1 Commit**（`commit-tree`、元の `HEAD` を条件にした `update-ref` の Compare-and-swap） → `HEAD` が Remote-tracking Branch と違えば **Fast-forward の Push**（前回の Push の失敗は次の実行が Push し直す = Retry）。**`--force` は使わない**。Remote が先に進んでいれば `push:push_rejected` で失敗し、上書きしない。
- Commit には Render した Bytes だけが入る。書いた後に Work Tree の File が書き換えられても、人が Checkout で `git add` した File（`README.md`、Credential の File など）が Stage されていても、Commit にも Push にも入らない（Stage されたまま残す）。管理する名前の外の Path は `HEAD` のまま。Blob の ID は Repository の Object Format（SHA-1 / SHA-256）で求める。
- git は `core.hooksPath=/dev/null`（Checkout の Hook を実行しない）、署名なし、呼び出し元の `GIT_*` 環境変数なし、`GIT_TERMINAL_PROMPT=0`、Timeout（`PAW_RECOVERY_GIT_TIMEOUT_SECONDS`、既定 300 秒）で実行する。Commit の作者は固定（`Personal AI Workspace <recovery@personal-ai-workspace.invalid>`）。git の出力（Remote の URL を含み得る）は表示も記録もしない。
- **Audit**: 実行ごとに `audit_events` に 1 行（別の Transaction）。`recovery.backup.completed`（`reason = files=N written=N removed=N commit=0|1 push=0|1 redacted=N`）か `recovery.backup.failed`（`<step>:<code>`。`<step>` は `check_repository` / `copy_memory` / `read_database` / `render` / `write_files` / `commit` / `push`）。`resource_kind = recovery_backup_run`、Actor なし。列・Migration は増やさない。
- **終了コード**は Decision 0038 6 と同じ型: `0` 成功、`1` 拒否（使い方・同時実行）、`2` 環境（設定・DB に届かない）、`3` 失敗。0 以外で `OnFailure=paw-recovery-backup-failure.service`（`crit` の Journal と `wall`）。読み取りだけの `recovery-backup-check [--max-age-minutes N]`（既定 90 分 = 3 回分。経過時間は DB の時計（`recorded_at` と `now()`）で測り、Host の時計によらない）。
- 接続は `PAW_DATABASE_URL`（Application の Role）。必要なのは読む Table の SELECT と `audit_events` の INSERT / SELECT だけで、Migration も Grant も足さない（Split-role の Test で確かめる）。
- 書き込みの途中で失敗した実行は Checkout を一部だけ書き換えた状態で残し得るが、**すべての File を書き終えるまで Commit しない**ので、Remote に中途半端な状態は入らない。次の実行が全体を書き直してから Commit する。

### 6. Restore を誰が、どう実行するか: Server ローカルの Command、既定は Dry run

- Restore は `python -m paw_backend.cli recovery-restore [--apply]` だけで行う。**HTTP の API と画面は作らない**（新品の Install には Login できる Owner がまだいない。Owner の初期設定・復旧と同じく Server の運用者の操作。Decision 0005）。
- 接続は **`PAW_MIGRATION_DATABASE_URL`（Table の Owner。新品の Install で `alembic upgrade head` を実行した Role）**。Application の Role は User を INSERT できない（Test で確かめる）。実行できるのは、Server の OS の権限と DB の Owner の Credential を持つ人だけである。
- **既定は Dry run**: すべての確認をして、戻す件数と、戻した後の手作業を表示する。書くのは `recovery.restore.planned` の Audit の 1 行だけ。`--apply` を付けたときだけ書く。

### 7. Restore の範囲と衝突: 空の Workspace へ、全体を、1 Transaction で

- **戻す先は空でなければならない**: `users`・`connection_quotas`・`projects`・`project_members`・`repositories`・`repository_remotes`・`memories`・`memory_versions`・`memory_relations`・`memory_sources` のどれかに行があれば拒否する（`target_not_empty`）。**既存の行を上書きも削除もしない。Merge もしない。**
- **全体だけ**を戻す。User ごと・Project ごとの部分的な Restore は V1 では作らない（ID の衝突や、戻した User の Private Memory が他の User の Project に混ざる判断が要るため）。
- `--apply` は 1 つの Transaction で、上の Table を `SHARE ROW EXCLUSIVE` で Lock してから空であることを確かめ直し、すべての行と `recovery.restore.applied` の Audit を書く。途中の失敗はすべて Rollback し（`recovery.restore.failed` を別に記録）、何も残さない。

### 8. Restore の元の検証

- Checkout が 1 の条件（Marker を含む）を満たし、**Work Tree が Clean**（未 Commit の変更・未追跡の File がない。`not_clean`）で、**`HEAD` が Remote-tracking Branch と同じ**（最後に Push された状態。`not_latest`）であること。運用者は Restore の直前に Clone（または `git fetch`）する。
- `manifest.json` の `recovery_format_version` がこの Code の読める Version であること（`format_unsupported`）。`recovery/checksums.sha256` の Hash が Manifest と合い、列挙されたすべての File が存在して Hash が合い、**列挙されていない File がない**こと（`checksum_mismatch` / `missing_file` / `unlisted_file`）。すべての Record の Key と型が形式どおりで、File 名が ID と合うこと（`record_invalid`）。
- 戻す先の DB が**この Release の Head**（`alembic upgrade head` 済み）で、Backup の `workspace_schema_version` が**この Release の Migration の鎖にある Revision**であること（`target_schema_mismatch` / `source_schema_unknown`）。
- 削除中の User の個人データ（User Record、Member、`user` Scope の Version、`memory/users/<id>/`）が Source にあれば拒否する（`deleted_user_data`）。
- **Restore は確かめた Commit の Object（`ls-tree`・`cat-file --batch`）から File を読み、Work Tree は読まない**（確認の後に別の Process が Checkout を書き換えても、読む内容は変わらない）。Link・Submodule・上限を超える Blob は拒否する（`unsafe_object`）。
- Restore は Checkout の Lock を、確認から書き込みと Audit の記録が終わるまで持ち続ける（その間に Backup が Checkout を書き換えたり Push したりしない）。
- 拒否はすべて `recovery.restore.refused`（`reason` は閉じた語彙の Code）の Audit の行だけを書き、Workspace のデータは書かない（終了コード 1）。表示は「Workspace のデータは書いていない」と、Audit の行を書けたかどうかを分けて示す。

### 9. 何を戻し、何を戻さないか

- 戻す: User（`status` はそのまま。Credential なし）、Connection の Quota、Project と Member、Repository と Remote（`created_by` は戻した User でなければ `NULL`。外部キーの `SET NULL` と同じ）、Memory・全 Version・Relation・`conversation` 以外の Source。
- 戻さない（Restore の後の手作業として表示する）:
  - **Credential**（10）。
  - **Auth Policy**: 重要な Security の設定なので、Import で黙って変えない。Backup の値が新しい Install と違えば表示し、Owner が設定画面から Step-up で設定し直す。
  - **Shared Connection**（Codex / Claude）: 再登録する（Handle と Credential は Backup にない）。
  - **Checkout**: Repository を Clone し直し、各 User が `gh auth login` し直す。
  - **Task**: Summary だけで、Task としては戻さない（実行中の Task の状態の復旧は V1 で保証しない。要件）。
  - **`conversation` の Source**: Conversation を Backup しないので、指す先がない（新しい行は Conversation を名指す必要がある。DB の Trigger）。件数を表示する。
  - 名前・Branch・URL の Credential を置換した **Repository・Remote**（3）。再登録する。
  - Metadata の変更履歴（`memory_metadata_changes`）、`audit_events`（新しい Install の Audit は Restore の行から始まる）。

### 10. Credential の再登録

- 戻した User は Password も Passkey も持たない。**Owner は `owner-recover`（sudo。Decision 0005）で Recovery Token を受け取り、Passkey を登録する**。Admin / User は Owner（User は Admin も）が Decision 0032 の Reset で 1 回限りの Password 再設定の Token を発行する。
- GitHub・SSH の鍵、Shared Connection、Recovery Repository の Deploy Key は、各 User・運用者が再登録する。

### 11. 削除状態の独立した確認（V1 の扱い）

- 要件は「復元元とは独立した最新の削除状態」を求める。V1 では、**Restore の元を「最後に Push された状態」（8 の `not_latest`）に限り、その状態の削除記録（`deletions/`）を適用する**。古い Commit からの Restore は拒否されるので、古い Backup から削除済みの User を戻すことはない。
- ただし、最後の Push の後（最大 30 分）に削除が始まった User は、その Backup では削除中ではない。Recovery Repository とは別に保つ削除記録（Decision 0043（Proposed）の消去の記録）が決まったら、Restore はその記録も確かめる（後続）。それまでは、Restore の後の手作業の表示で、運用者が Backup の外の削除記録を確かめて適用してから運用を再開するよう求める。**この表示は、Backup に削除記録がないときも毎回出す**（最後の Push の後に始まった削除は、どの削除記録にもないため）。

### 12. Audit

- Dry run の `recovery.restore.planned` を書けなければ、Dry run は失敗（`audit_unrecorded`、終了コード 3）とする（実行ごとの Audit の行を黙って欠かさない）。

- Restore は実行ごとに `audit_events` に 1 行: `recovery.restore.planned`（Dry run）、`recovery.restore.applied`（戻した行と同じ Transaction）、`recovery.restore.refused`、`recovery.restore.failed`。`resource_kind = recovery_restore`、`reason` は件数（`users=N projects=N repos=N memories=N versions=N`）か閉じた語彙の Code。Path・URL・行の文字列は書かない。

## 選定理由

- Checkout に既定値を置かず、Marker のない空でない Checkout・Home・Projection の Directory を拒否するのは、「Project Repository へ Commit しない」「Private Memory を他の場所に出さない」を設定の誤りでも破らないため（Decision 0038 1 と同じ Fail-closed）。
- 列の Allow-list で読むと、Credential の列が「うっかり入る」ことがない（新しい列は、その SQL を変えない限り Git に入らない）。
- `manifest.json` を内容が変わったときだけ変え、変更がなければ Commit も Push もしないのは、要件の Git Schedule のとおり。
- Restore を空の Workspace への全体の 1 Transaction に限ると、上書き・削除・Merge がなく、失敗しても何も残らない。Dry run を既定にすると、運用者は書く前に件数と手作業を確かめられる。
- Restore の元を「最後に Push された、Clean な状態」に限ると、古い Backup から削除済みの個人データを戻す経路を閉じられる。

## 代替案

- **Projection の Directory 自体を Recovery Repository の Work Tree にする**: 写す手間はないが、Decision 0038 1（git の Work Tree の中に書かない）を変える必要がある。採らない（Decision 0038 9 の推奨どおり写す）。
- **YAML で書く**: 要件の例に近いが、Library が増え、決定的な出力の保証が難しい。JSON は YAML としても読める。採らない。
- **Markdown（`memory/`）から Memory を戻す**: 人が読める唯一の正本になるが、Version の履歴・Relation・Source がなく、切り詰め・置換の影響を受ける。採らない（Machine-readable な `memory-records/` から戻す）。
- **Restore で既存の行を上書き・Merge する**: 部分的な障害に使えるが、どちらが新しいか・削除された行をどうするかの判断が要り、誤ると Data を失う。V1 では採らない。
- **Restore を Web の管理画面から行う**: 新品の Install には Login できる Owner がいない。採らない。
- **Auth Policy・Shared Connection も戻す**: 手作業が減るが、Security の設定を Import で黙って変えることになる（Policy の変更は Owner の Step-up。Decision 0015・0025）。採らない。
- **Backup を Table の Owner の Role で実行する**: 権限が広すぎる。SELECT だけで足りる。採らない。
- **Push の失敗時に `--force` で上書きする**: 他の場所からの Push や履歴を失い得る。採らない。

## リスク

- **Recovery Repository はすべての User の Private Memory と、Credential を除く Workspace の状態を持つ。** Remote が Private であること、Deploy Key の管理、Clone の置き場所（`0700`）は配備の責任である。
- Secret の検出は最善の努力で（Decision 0038 5 と同じ）、検出できない形の Secret は Git に入り得る。Git に入った後は、履歴の消去が要る。
- Restore した本文は `[REDACTED]` を含み得る（元の Secret は戻らない）。1,000,000 文字を超える本文は切り詰められている（`truncated`）。
- 削除中の User の個人データは Backup に入らないので、30 日の保留中にサーバーが失われると、その User は戻せない。
- 最後の Push の後の変更（最大 30 分と、失敗が続いた間）は戻らない。
- 8 の `not_latest` は Clone の Remote-tracking Branch と比べるだけで、Remote に問い合わせない。古い Clone を `fetch` せずに使うと、古い状態を「最新」とみなす。Restore の直前に Clone または `fetch` することを手順に書く。
- Login 名を仮の名前にした User は、Owner が名前を付け直すまで元の名前で Login できない。仮の名前 `redacted-<ID の先頭 12 桁>` が、まれに既存の別の User の Login 名と重なると、Restore は一意性の違反で全体を Rollback する（何も書かない）。
- Commit を Render した Bytes から作るので、Checkout に手で置いた管理外の File は Commit されない（README などを Recovery Repository に入れたいときは、人が Checkout で手で Commit する。以後の Backup は `HEAD` のその File を保つ）。
- 1 回の Backup は全件を読み、全 File を比べる。件数が大きくなれば差分の方式が要る（形式は変わらない）。

## 決めてほしいこと

推奨の答えを添えて Human / Admin に問う。

1. **Checkout**: `PAW_RECOVERY_REPOSITORY_DIR`（既定なし）の専用 Private Repository の Clone。Home・Projection と重なる場所、git の Work Tree の最上位でない場所、Marker がなく空でない Checkout を拒否し、空の Clone だけを自分のものにする。Remote の作成と Private の確認は配備の作業。
   推奨: 提案どおり。
2. **形式**: 1 Entity 1 File の決定的な JSON（YAML ではなく）、`manifest.json` と `recovery/checksums.sha256`、`recovery_format_version: 1`、`workspace_schema_version` は Alembic の Head。Memory は `memory-records/` から戻し、`memory/` は人が読む写し。
   推奨: 提案どおり。
3. **入れるもの・入れないもの**: 列の Allow-list。自由記述の Credential は置換し、**Restore した本文は `[REDACTED]` のまま**。Model / Router・Notification の設定はまだ無いので形式 1 には入れない。
   推奨: 提案どおり。
4. **削除中の User**: `pending_deletion` / `deleted` の User は `id` と `status` の削除記録だけを残し、個人データを現在の Backup に入れない（保留中にサーバーが失われると、その User は戻らない）。
   推奨: 提案どおり（削除を優先）。
5. **実行と失敗の通知**: 30 分ごとの Timer（`*:02/30`）、変更があるときだけ 1 Commit、Fast-forward の Push だけ（`--force` なし）、次の実行が Push の Retry、実行ごとの Audit、終了コード 0〜3、`OnFailure=`、`recovery-backup-check`（既定 90 分）。Application の Role で実行。
   推奨: 提案どおり。
6. **誰が Restore するか**: Server ローカルの `recovery-restore` だけ（HTTP API・画面なし）、`PAW_MIGRATION_DATABASE_URL`（Table の Owner）、**既定は Dry run**、`--apply` で書く。
   推奨: 提案どおり。
7. **Restore の範囲と衝突**: 空の Workspace へ、全体だけを、1 Transaction で。**上書き・削除・Merge・部分的な Restore はしない**。
   推奨: 提案どおり（部分的な Restore が要るなら後続の Decision）。
8. **Restore の元の検証**: Clean な Work Tree、`HEAD` = Remote-tracking Branch（最後に Push された状態）、形式の Version、全 File の Checksum と列挙外の File の拒否、Record の型、DB がこの Release の Head、Backup の Schema がこの Release の鎖にあること。
   推奨: 提案どおり。
9. **戻さないもの**: Credential、Auth Policy（Owner が Step-up で設定し直す）、Shared Connection、Checkout、Task（Summary だけ）、`conversation` の Source、Metadata の変更履歴、Audit。
   推奨: 提案どおり。
10. **Credential の再登録**: Owner は `owner-recover`、Admin / User は Decision 0032 の Reset、外部の鍵は各自。
    推奨: 提案どおり。
11. **削除状態の独立した確認**: V1 は「最後に Push された状態」の削除記録を適用し（古い Commit からの Restore は拒否）、Backup の外の削除記録（Decision 0043）との突き合わせは後続。それまでは Restore の後の表示で運用者に確認を求める。
    推奨: 提案どおり。ただし要件の「確認できなければ復旧を完了扱いしない」をより厳しく満たすなら、Decision 0043 の記録ができるまで `--apply` を拒否する案もある（その間は Restore できない）。
12. **Audit**: `recovery.restore.planned` / `applied`（同じ Transaction）/ `refused` / `failed`、`resource_kind = recovery_restore`。
    推奨: 提案どおり。

## 承認後の扱い

- 承認前は、Command・Unit File はコードとして入るが、実運用の Server で Timer を有効化（`systemctl enable --now paw-recovery-backup.timer`）しない。
- 承認されたら、運用者が Private Repository・Deploy Key・Checkout・環境 File を用意し、Unit File を配備して Timer を有効にする（README の「Recovery Repository」の手順）。
- 方針を変える場合は、この Decision を書き換えず、新しい Decision から `Supersedes` する。
