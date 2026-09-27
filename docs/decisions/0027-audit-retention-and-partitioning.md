# `audit_events` の保存期間・Partition・退避の方針

- Status: Approved
- Date: 2026-09-27
- Scope: Issue [#86](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/86)（[Decision 0004](0004-rbac-capability-and-audit-policy.md) の 5 の 5「保存期間、Partition、古い行の退避は未決とする」を埋める）。Migration `0086`、`paw_backend/authz/retention/` を使う以降の Issue
- Supersedes: なし。[Decision 0004](0004-rbac-capability-and-audit-policy.md) の 5（Audit Table の保護）を書き換えず、その未決事項（5 の 5）に**答える**追補。追記専用の保証（Trigger が UPDATE / DELETE / TRUNCATE を拒否する、`PUBLIC` の権限を外す、`PAW_APP_DATABASE_ROLE` には INSERT と SELECT だけ）は変えない
- Approval: 2026-09-27、Humanが作業Session内で、判断点ごとの説明（推奨つき）を受けたうえで「全部推奨どおり」と回答して承認（末尾の「承認時の決定」）

## 背景

[Decision 0004](0004-rbac-capability-and-audit-policy.md) の 5「Audit Table の保護」は、`audit_events` が追記専用であること（Trigger による保護、権限の分離）を定めた一方、
5 の 5 で「保存期間、Partition、古い行の退避は未決とする（Table は削除できず、行数は増え続ける）」と明記し、別 Issue で決めることにした。2026-09-25 の承認時にも、
「Audit の保存期間・Partition・古い行の退避は未決のまま承認し、別Issue [#86](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/86) で決める」と記録している。

Issue #86 は、次の 3 点を決めて実装することを求めている。

1. **保存期間**: `audit_events` の行をいつまで「熱い」（アプリケーションから見える）状態に置くか。
2. **Partition方式**: 時間ごとの Table Partition などで、増え続ける行を管理できる形にする。
3. **退避先**: 古い行を、削除ではなく別の場所（別 Table への archive など）に移す、または十分に古いものを削除する。

要件（[REQUIREMENTS.md](../../REQUIREMENTS.md)、[SECURITY_RBAC_AUDIT.md](../SECURITY_RBAC_AUDIT.md)）は Audit Log の**最低項目**は定めるが、
保存期間・Partition・退避の方式や具体的な日数は定めていない。PAW-025 の実装（Decision 0004）も、法令上の保存義務やコンプライアンス要件を確認していない。
そのためこの Decision は、要件にない**推奨値**を提案し、Human の承認を求める。承認されるまで、実装は Decision 0023・0018・0021 などこれまでの Proposed 案件と同じパターンで
「コードは入るが、Decision が定める運用（保存期間の適用、実際の退避・削除の実行）は有効にしない」前提で進める。

実装は [Backend README](../../apps/backend/README.md) の「Audit の保存期間・Partition・退避（Issue #86）」に書く。

## 提案

### 1. Partition 方式: `recorded_at` による月ごとの Range Partition

- `audit_events` を、PostgreSQL のネイティブな宣言的 Partition（`PARTITION BY RANGE (recorded_at)`）に変える。Partition の単位は**暦月**（`audit_events_p<YYYY>_<MM>`、例 `audit_events_p2026_09`）。
- Partition Key に `occurred_at`（Application の時計。呼び出し側が渡す値で、過去・未来を含め自由に指定できる）ではなく、**`recorded_at`**（Database の時計。Migration `0025` の Trigger が `now()` に強制し、常に単調に増える）を使う。
  `occurred_at` は Partition の判断に使わない列のまま残る。
- Migration `0086` が、既存の `audit_events`（Migration `0025` が作り `0087` が `details` を足した、Partition化されていない単一 Table）を**その場で** Partition 化する。
  1. 既存の Table を `audit_events_p_legacy` に改名する（行、Index、CHECK制約はそのまま。PK だけ `(id)` から `(id, recorded_at)` に変える。PostgreSQL は、Partition化された Table の Unique / Primary Key 制約が
     Partition Key を含むことを要求するため）。
  2. 新しい `audit_events`（Partition の親）を、同じ列・同じ CHECK 制約で作り、`audit_events_p_legacy` を `FOR VALUES FROM (MINVALUE) TO (<cutover>)`（`cutover` は Migration 適用時の `now()`）で Attach する。
     **行の再書き込みは起きない**（`ATTACH PARTITION` は Metadata だけの操作）。
  3. `cutover` から、その月の終わりまでをカバーする最初の（暦月の途中から始まる）Live Partition を 1 つ作る。以降の Partition は `AuditRetentionService.ensure_partitions`（下記）が作る。
- 新しい Partition が今後も自動的に用意され続けるよう、`AuditRetentionService.ensure_partitions` が「今月 + `horizon_months` か月先」までの Partition を、まだ無ければ作る（既定の推奨値は 3 か月先。下記「決めてほしいこと」）。
  これは Migration では**ない**（Issue の指示どおり、実行スケジューラ自体は用意しない。Table / Function / 呼び出し用の Service だけを用意する）。

### 2. 退避先: 二つ目の Partition 親 `audit_events_archive`

- 古い Partition の退避先は、**もう一つの、同じ形の Partition 親 `audit_events_archive`**（`PARTITION BY RANGE (recorded_at)`）とする。
- 「退避」は、対象 Partition を `audit_events` から `DETACH PARTITION` し、同じ Table（1 つの物理 Table。行は増えていない）を `audit_events_archive` に `ATTACH PARTITION` するだけである。
  **行の読み出し・コピー・書き換えは一切起きない**（数百万行あっても、退避は一瞬で終わる Metadata 操作）。
- `audit_events_archive` も、`audit_events` と同じ 2 つの追記専用 Trigger（UPDATE/DELETE 拒否、TRUNCATE 拒否）を親レベルに持ち、退避されてきた Partition は自動的にそれを引き継ぐ
  （下記「4. 追記専用の保証との両立」）。**退避された行も、追記専用のまま残る**（Issue の要求「追記専用の保証と Partition の切り替え・退避が両立する」を満たす）。
- Application の Role（`PAW_APP_DATABASE_ROLE`）には、`audit_events` と同じ INSERT・SELECT ではなく、`audit_events_archive` には **SELECT だけ**を与える（退避された行を、将来の Admin 向け閲覧機能が読めるように。
  書き込みは起きないため INSERT は与えない）。今回のスコープでは、退避された行を実際に閲覧する API・UI は作らない（別 Issue）。

### 3. 保存期間（推奨値・要件なし）

REQUIREMENTS.md に保存期間の定めはなく、法令上の保存義務も確認していない。次を**推奨の初期値**として提案する（実測・コンプライアンス確認が無いままの暫定値であることを明記する）。

- **`archive_after_days = 180`**（約 6 か月）: Live（`audit_events` に付いた状態）でいられる期間。この日数が経った月の Partition から、`AuditRetentionService.archive_due_partitions` が退避の対象にする。
- **`purge_after_days = None`（既定で無効）**: 退避された Partition を物理的に削除する（`DROP TABLE`）操作。既定では**自動的に何も削除しない**。運用者が明示的にこの値を設定した場合だけ、
  退避後さらにこの日数が経った Partition が削除の対象になる（Issue の「十分に古いものの削除」に対応する、が既定は無効の安全側）。
- 上の 2 つの数値は `RetentionPolicy`（`paw_backend/authz/retention/records.py`）の値で、Migration ではない（値を変えても Schema は変わらない。値を変えるときはこの Decision を書き換えず、
  新しい Decision から `Supersedes` する。Decision 0018 の「暫定値として承認された」数値と同じ扱い）。

### 4. 追記専用の保証との両立

- 行レベルの 2 つの Trigger（UPDATE/DELETE 拒否、`recorded_at` を `now()` に強制）は、`audit_events` と `audit_events_archive` の**親**に 1 回だけ定義する。
  PostgreSQL は、親に定義された行レベルの Trigger を、既存のすべての Partition と、後から作られる・Attach されるすべての Partition に**自動的に複製する**（実 PostgreSQL 18 で確認済み。
  `tests/test_retention_postgres.py`）。
- **文レベルの Trigger（TRUNCATE 拒否）は複製されない。** これは PostgreSQL の既知の仕様で、独自の実装ミスではない。親に定義した TRUNCATE 拒否 Trigger は、`TRUNCATE audit_events`
  のように**親の名前で** TRUNCATE したときだけ効く。個々の Partition を名指しで `TRUNCATE audit_events_p2026_09` すると、Partition 自身に同じ Trigger が無い限り、素通りしてしまう
  （追記専用の保証の抜け穴になり得る。独自の Review で発見し、実測で確認した）。
  対策として、**新しい Partition を作る・退避で受け取るたびに、その Partition に明示的に同じ TRUNCATE 拒否 Trigger を作る**（Migration `0086` の `audit_events_p_legacy` と最初の Live Partition、
  `AuditRetentionService._create_partition` が作る Partition のすべてで行う）。ただし、`audit_events` / `audit_events_archive` に Table 単位の TRUNCATE 権限を持つ Role は
  Application の Role には存在しない（`PAW_APP_DATABASE_ROLE` には INSERT・SELECT だけ）ため、この抜け穴を突けるのは、そもそも Table の Owner（Migration の Role）だけであり、
  実害は限定的である（Decision 0004 の「守れないもの」と同じ整理）。
- **`recorded_at` を Partition Key にしたことで生じる、既存の動作の変化**: Migration `0025` の `recorded_at` 強制 Trigger（BEFORE INSERT で `NEW.recorded_at := now()`）は、
  呼び出し側が `recorded_at` に何を指定しても、常に成功して値を上書きしていた。Partition 化後は、PostgreSQL が **BEFORE ROW Trigger による Partition をまたぐ行の移動を許さない**ため、
  呼び出し側が「現在の月」と異なる Partition に着地するような `recorded_at` を明示的に指定すると、Trigger が `now()` へ書き換えようとした時点で **INSERT 自体が失敗する**
  （`moving row to another partition during a BEFORE FOR EACH ROW trigger is not supported`）。**既定値（列を指定しない INSERT）を使う限り、この失敗は起きない**
  （`recorded_at` を実際に指定する呼び出しはコードベースに無い。`paw_backend.authz.audit`、`paw_backend.auth.audit`、`paw_backend.authz.retention.audit` のいずれも、
  INSERT 文に `recorded_at` を含めない）。「Insert 可能な Role でも `recorded_at` を選べない」という Decision 0004 の保証自体は、**むしろ強くなった**
  （こっそり書き換えられる、から、そのような試みは INSERT ごと失敗する、に変わる）。既存の Test `tests/test_authz_postgres.py` の
  `test_even_an_insert_capable_role_cannot_choose_recorded_at` を、この新しい失敗モードを検証するように更新した（下記「決めてほしいこと」にも挙げる）。
- **退避・削除は、実際の壁時計の「今」を含む Partition には決して触れない。** `AuditRetentionService` が書く Audit 行自身も、`recorded_at` は常にデータベースの実時計であり、
  `AuditRetentionService` に渡した `clock`（Policy の判断に使う「今」）とは別物である。運用上この 2 つはほぼ一致するはずだが、両者が乖離した場合
  （実装中に Test で発見: 遠い未来の `clock` を渡すと、`archive_after_days` / `purge_after_days` の閾値次第で、実際の「今」を含む Partition まで対象に入り得る）、
  それを退避・削除すると、直後の Audit 書き込み（この操作自身のものを含む）が「今」を受け止める Partition を失って失敗し得る。
  そのため `archive_due_partitions` / `purge_due_partitions` は、`Policy` や `clock` が何と言おうと、**実際の壁時計が指す暦月の Partition を候補から常に除外する**
  （`AuditRetentionService._protected_partition_name`）。`audit_events_p_legacy` にはこの保護はない（`cutover` より前に限られ、実際の「今」を含むことはないため）。
- **Migration `0021`（`PAW_OPERATOR_DATABASE_ROLE` への `audit_events` への INSERT 権限）を、この Migration も引き継ぐ。** 0021 は「今 `audit_events` という名前の
  Table」に `GRANT INSERT` した。この Migration は同じ名前で**別の**Table を作るため（元の Table は `audit_events_p_legacy` に改名される）、0021 の付与はそのまま
  改名先（`audit_events_p_legacy`）に残り、新しい `audit_events` には及ばない。放置すると、Owner のサーバーローカルな操作（`PAW_OPERATOR_DATABASE_ROLE`）が
  自分の Audit を書けなくなる（実装中に `tests/test_owner_token_roles.py` の失敗で発見）。この Migration も、新しい `audit_events` に対して同じ付与をやり直す。

### 5. 退避・Partition 操作の Audit

- Partition の作成・退避・削除は、それぞれ `audit_events` へ 1 行ずつ記録する。新しい `action` の名前空間 `audit.retention.*`
  （`audit.retention.partition_created` / `partition_archived` / `partition_purged`。`paw_backend.authz.retention.audit.RetentionAction`）を、
  `paw_backend.auth.audit.AuthAction` と同じパターンで追加する。**`audit_events` に列や CHECK 制約は増やさない**: Partition 名は 64 文字以内の `reason` 列にそのまま入り
  （最長でも `audit_events_p2026_09` の 22 文字）、`resource_kind` は固定値 `audit_partition`、`decision` は常に `allow`。Migration `0087` が `research.external_send` のためにした
  `details`（JSONB）の登録簿の拡張は不要である。
- 「誰が」: `actor_id` / `actor_role`。無人のスケジュール実行（今回のスコープ。実行スケジューラ自体は用意しない）なら両方 `NULL`。将来 Admin が手動で実行する Capability
  ができた場合は、その User の ID とRole を渡せる（`RetentionActor`）。「いつ」: `occurred_at` / `recorded_at`（既存の列。後者は Database の時計）。「何を」: `action` と `reason`（Partition 名）。
- 監査の行は、その操作の DDL と**同じ Transaction**で書く（`AuditRetentionService` の各操作）。Decision 0010 の「Audit してから送る」（別の Transaction、Audit 優先）とは違う設計だが、
  ここでは「退避・削除という操作」自体と「その記録」を分ける意味が無く（外部への送信のように、記録なしでは取り消せない副作用が Database の外に起きるわけではない）、
  同じ Transaction にすることで「実行されたのに記録が無い」も「記録されたのに実行されていない」もどちらも起きない、より強い一貫性を選んだ。

### 6. 実行に必要な権限: Migration の Role（`PAW_MIGRATION_DATABASE_URL`）

- Partition の作成（`CREATE TABLE ... PARTITION OF`）、退避（`ATTACH` / `DETACH PARTITION`）、削除（`DROP TABLE`）は、いずれも DDL であり、Table の**所有者**しか実行できない
  （実 PostgreSQL で確認: `PAW_APP_DATABASE_ROLE` の Role でこれらを試みるとすべて `permission denied` / `must be owner of table`）。
  そのため `AuditRetentionService`（`paw_backend/authz/retention/service.py`）は、Web Application と同じ接続（`PAW_APP_DATABASE_ROLE`）では動かず、
  Migration と同じ、Table を所有する特権的な接続（`PAW_MIGRATION_DATABASE_URL`）が要る。これは Decision 0004 の 5 の 2（Migration の Role と Application の Role を分ける構成）の
  自然な延長で、新しい Role を増やさない。
- 呼び出し方法（Issue の指示どおり、実行スケジューラは無くてよい）: `AuditRetentionService(database).run_maintenance(policy, actor=...)` という素直な Python の呼び出し。
  cron・systemd timer・将来の Admin 専用 Capability のいずれからでも、この 1 メソッドを呼べば足りる。今回のスコープでは、それらの呼び出し元自体は実装しない。

### 7. 実装の構成: 純粋なロジック + Store（PAW-046 / PAW-053 と同じパターン）

- `records.py`: `PartitionStatus`、`PartitionWindow`、`RetentionPolicy`、`MaintenanceReport`。Database も時計も持たない、不変の値。
- `rules.py`: 純粋関数。`month_start` / `next_month_start`（暦月の境界）、`partition_name`（命名規則）、`plan_missing_partitions`（どの Partition が足りないか）、
  `partitions_due_for_archive` / `partitions_due_for_purge`（どの Partition が退避・削除の対象か）。Database に触れない、`RetentionPolicy` の値をそのまま信じる
  （`memory/shared/lifecycle.py` と同じ規律）。
- `audit.py`: `RetentionAction`、`RetentionActor`、Audit 行の書き込み（`paw_backend.auth.audit` と同じ、`audit_events` を直接使うパターン）。
- `models.py`: `audit_retention_partitions`（Bookkeeping。下記）の ORM Model。
- `service.py`: `AuditRetentionService`。上の全部を使って実際に SQL を実行する、唯一の場所。
- **Bookkeeping Table `audit_retention_partitions`**: どの Partition が存在し、今どの状態（live / archived / purged）かを、PostgreSQL の Partition Catalog
  （`pg_inherits` と Partition 境界の式を都度パースする）に頼らず、自前の小さな Table で管理する。追記専用ではない普通の Table で、`PAW_APP_DATABASE_ROLE` には何も与えない
  （Migration / Retention 保守の Role だけが読み書きする）。この設計は、PAW-046（`SharedMemoryCandidate` の状態）や PAW-053 のような、
  「決めるのは純粋な関数、状態を持つのは Table、実行するのは Service」という、このコードベースの既存パターンをそのまま踏襲する。

## 代替案

- **`occurred_at` を Partition Key にする**: Application の時計で、呼び出し側が任意の値を渡せる（Decision 0004 の「Application が真実性を保証しない」列）。過去や未来の日付が混在すると、
  Partition の境界が Application の主張どおりに散らばり、「最近作られた行は最近のPartitionにある」という運用上の前提が崩れる。**採らない。**
- **`ATTACH` / `DETACH` ではなく、行を新しい Table へコピーしてから元を消す**: 大きな Table では、コピーが長時間ロックを取るか、多数回のバッチ処理が要る。宣言的 Partition の
  `ATTACH` / `DETACH` は Metadata だけの操作で、行数に関わらず一瞬で終わる。**採らない。**
- **退避先を「同じ `audit_events` の中の、古い Partition のまま」（退避という操作自体をしない）**: 増え続ける Table を分割はできるが、「熱い」領域と「冷たい」領域を運用上区別できず、
  Table 単位の VACUUM・バックアップ・将来のストレージ階層化（安価なディスクへの移動）の単位にならない。別の親を用意する方が、Issue の要求（退避先を決める）に素直に応える。**採らない。**
- **保存期間・Partition の粒度を「日ごと」にする**: 監査ログとしては行数が多すぎない限り月ごとで十分細かく、Partition の数が増えすぎない（月ごとなら 1 年で 12、日ごとだと 365）。
  実際の書き込み量が判明したら、新しい Decision で日ごとや週ごとに変更できる（`rules.py` の月境界のロジックを差し替えるだけで、既存の Partition には影響しない）。**今回は月ごとを推奨。**
- **既定で自動的に Purge（物理削除）を有効にする**: 要件にも法令にも保存期間の定めが無い状態で、既定で自動的にデータを消す実装は攻撃的すぎる。既定は退避まで（無期限保持）とし、
  削除は運用者の明示的な設定を要求する。**既定で有効にする案は採らない。**

## リスク

- **保存期間の数値（180 日）に根拠が無い。** コンプライアンス・法令上の要件を確認していない暫定値であり、Decision 0018 の Queue の数値と同じ位置づけ（実測に基づかない）。
- **Purge は不可逆である。** `DROP TABLE` は、その Partition のすべての行を完全に失う（Audit 行は残るが、退避された内容そのものは戻らない）。既定で無効にしているが、
  設定した運用者が意図せず古いデータを失う可能性は残る。
- **`recorded_at` を Partition Key にした結果、`recorded_at` を明示的に指定する INSERT は、Partition をまたぐと失敗するようになった。** 実際のコードはどこも指定しないため実害は無いが、
  将来 `recorded_at` を指定するコードが追加されると、原因の分かりにくいエラー（`FeatureNotSupported`）になる。この Decision と Migration の docstring に明記して備える。
- **Bookkeeping Table (`audit_retention_partitions`) と実際の PostgreSQL Partition 構成がずれる可能性。** 今回の実装は、`AuditRetentionService` だけがこの Table を書く前提で、
  同時に 2 つの保守処理が走る競合（2 つの Cron が同時に `ensure_partitions` を呼ぶ、など）への Lock は入れていない（Issue のスコープ外、スケジューラ自体が無いため）。
  将来スケジューラを追加する Issue で、Advisory Lock などの排他制御を検討する。
- **`audit_events_archive` の行を実際に読む Admin 向け機能はまだ無い。** SELECT 権限だけ用意したが、UI・API は別 Issue。
- **月境界をまたいで「今月の Partition が無い」状態で INSERT すると失敗する。** `ensure_partitions` を運用で定期的に呼ばないと、来月になった瞬間に監査の記録が全滅する
  （Fail-closed ではあるが、Decision 0004 の `REQUIRED` の Audit Mode と同じ「Audit が書けないと操作も止まる」の一種）。実行スケジューラを用意する別 Issue が必須になる
  （このDecisionが承認された後、実際に運用する前に解決する必要がある）。

## 決めてほしいこと

推奨の答えを添えて Human に問う。

1. **保存期間の数値**: `archive_after_days = 180`（約 6 か月）、`purge_after_days` は既定で無効（`None`）。
   推奨: 提案どおり（コンプライアンス要件が判明したら、新しい Decision で変える）。
2. **Partition の粒度**: 暦月（`audit_events_pYYYY_MM`）、`horizon_months = 3`（常に 3 か月先まで Partition を用意しておく）。
   推奨: 提案どおり。
3. **退避先の設計**: 別の Partition 親 `audit_events_archive`（`DETACH` / `ATTACH`、行のコピーなし）。Application の Role には SELECT だけを与える。
   推奨: 提案どおり。
4. **`recorded_at` を明示的に指定した INSERT が、Partition 境界をまたぐと（変換ではなく）失敗するようになる変更**: 既存の Test
   （`tests/test_authz_postgres.py` の `test_even_an_insert_capable_role_cannot_choose_recorded_at`）を、「値が黙って書き換えられる」から「INSERT 自体が拒否される」に
   期待値を変更した。実際のコードはどこも `recorded_at` を指定しないため、通常の動作に影響はない。
   推奨: 提案どおり（PostgreSQL の制約であり、他に採れる手段がない。「拒否」は「書き換え」より弱くない保証である）。
5. **Partition・退避操作の Audit に、新しい `details` の Schema や CHECK 制約を追加しない**（Partition 名を既存の `reason` 列に収める）。
   推奨: 提案どおり（Migration 0087 が用意した拡張の仕組みを使うほどの複雑さが無い内容のため）。
6. **実行スケジューラは今回作らない**（`AuditRetentionService.run_maintenance` という呼び出し可能なインターフェースだけを用意する）。
   推奨: 提案どおり（Issue の指示どおり）。ただし、上の「リスク」にあるとおり、実運用に入る前に、これを定期的に呼ぶ仕組み（cron・systemd timer・Admin Capability のいずれか）を
   別 Issue で用意する必要がある。この Decision の承認は、その別 Issue の実装を待たずに与えてよいか、それとも両方揃うまで待つべきか。

## 承認後の扱い

承認された場合、この Decision が Migration `0086` と `paw_backend/authz/retention/` の設計判断を裏付ける。Decision 0004 の 5 の 5（保存期間・Partition・退避は未決）は、
この Decision によって埋まったものとして扱う（0004 の本文は書き換えない）。方針を変える場合は、この Decision を書き換えず、新しい Decision から `Supersedes` する。
承認されるまで、`AuditRetentionService` を実運用で呼び出す（cron・Admin Capability などに配線する）ことはしない。

## 承認時の決定（2026-09-27）

Human は、作業 Session で上の 6 点について推奨つきの説明を受け、「全部推奨どおり」と回答して承認した（6 点を一括で。個別の変更はない）。**6 点すべてが推奨どおりで、設計の変更はない。**

6 点目（別 Issue の定期実行の仕組みを待つか）は、**待たずに今承認する**。ただし `AuditRetentionService.run_maintenance` を定期的に呼ぶ仕組み（cron・systemd timer・Admin Capability のいずれか）は、実運用に入る前に別 Issue で用意する。それまでは誰も呼ばないため、保存期間の適用・退避は実際には起きない。

承認後に方針を変える場合は、この Decision を書き換えず、新しい Decision から `Supersedes` する。
