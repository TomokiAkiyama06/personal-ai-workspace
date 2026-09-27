# `audit_events` の保存期間・退避を定期実行する仕組み（systemd timer + Server ローカルの Command）

- Status: Proposed
- Date: 2026-09-28
- Scope: Issue [#117](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/117)（[Decision 0027](0027-audit-retention-and-partitioning.md) の 6 と「承認時の決定」が残した「`AuditRetentionService.run_maintenance` を定期的に呼ぶ仕組み」）。`paw_backend/authz/retention/runner.py`、`paw_backend/cli/retention.py`、`apps/backend/deploy/systemd/`
- Supersedes: なし。Decision 0027 を書き換えない追補。0027 の保存期間・Partition・退避の方式、`RetentionPolicy` の値、Partition 単位の Audit 行（0027 の 5）は変えない
- Approval: 未承認（Human / Admin の承認待ち）

## 背景

Decision 0027（2026-09-27 承認）は、`AuditRetentionService.run_maintenance`（Partition の先行作成・退避・任意の Purge）を呼び出し可能なインターフェースとしてだけ用意し、
定期的に呼ぶ仕組みは作らなかった。Human は「待たずに今承認する。ただし実運用に入る前に、cron・systemd timer・Admin Capability のいずれかを別 Issue で用意する」と決めた（0027 の「承認時の決定」）。

Issue #117 の条件:

1. Migration の Role（`PAW_MIGRATION_DATABASE_URL`）で動かす（0027 の 6）。
2. `horizon_months = 3` の先行作成が途切れると、Partition のない月の INSERT が失敗する（その時点から、Audit を要するすべての操作が止まる）。そのため実行の失敗を**検知・通知**できること。
3. 実行と結果は 0027 の 5 のとおり Audit に残る。

要件（[REQUIREMENTS.md](../../REQUIREMENTS.md) の「Deployment implementation」、[DEPLOYMENT_UPDATE.md](../DEPLOYMENT_UPDATE.md) の「Open implementation choice」）は、
Docker / systemd などの具体的な方式を実装段階で決めてよいとしている。どの仕組みを使うか、失敗をどう扱い何を Audit に足すかは要件にないため、この Decision で提案する。

## 提案

### 1. 呼び出し元: Server ローカルの Command を systemd timer で 1 日 1 回

- `python -m paw_backend.cli audit-retention-run` という Server ローカルの Command を足す（PAW-021 の `owner-setup` / `owner-recover` と同じ `paw_backend.cli` の入口。HTTP API ではない）。
- 定期実行は **systemd timer**（`apps/backend/deploy/systemd/paw-audit-retention.timer`、`OnCalendar=daily`、`Persistent=true`）が、oneshot の Service
  （`paw-audit-retention.service`）を起動して行う。Unit File は例として Repository に置き、配備の Path・User に合わせて `/etc/systemd/system/` へ置く。
- 1 日 1 回にするのは、`horizon_months = 3` のもとで、実行が止まってから INSERT が失敗するまでに約 3 か月の余裕があり、その間毎日失敗が報告されるため。
- **cron でも同じ Command をそのまま使える**（終了コードが 0 以外なら cron の Mail などで気づける）。systemd を推奨するのは、`Persistent=true`（停止中に逃した実行を起動後に行う）、
  `OnFailure=`（失敗時の通知 Unit）、Journal への記録が標準で揃い、Ubuntu の Server（このプロジェクトの前提）で追加の Package が要らないため。

### 2. 接続: `PAW_MIGRATION_DATABASE_URL` だけを使い、`PAW_DATABASE_URL` へは Fallback しない

- Partition の DDL は Table の Owner しか実行できない（0027 の 6）。分離構成では `PAW_DATABASE_URL` は Application の Role で、どの Step も実行できないため、
  黙って Fallback すると「毎日失敗する」だけになる。`PAW_MIGRATION_DATABASE_URL` が無ければ、何もせずに終了コード 2 で止まる。
  単一 Role の開発環境では、両方に同じ URL を設定する。
- URL は root だけが読める `EnvironmentFile`（`/etc/paw/audit-retention.env`、`chmod 600`）に置き、引数・`ExecStart`・`Environment=` には書かない。
  Command は URL も例外の Message も表示しない（例外は型の名前だけ。`owner` の Command と同じ規律）。

### 3. 失敗の検知と通知

一回の実行（`paw_backend.authz.retention.runner.run_scheduled_maintenance`）は次の順に進む。

1. **Advisory Lock**（`pg_try_advisory_lock`、Session 単位）を取る。取れなければ（別の実行が進行中）何もせず終了コード 1。
   0027 の「リスク」が「スケジューラを追加する Issue で Advisory Lock などの排他制御を検討する」とした点への答え。Process が死ねば Connection とともに PostgreSQL が解放する。
2. `ensure_partitions` → `archive_due_partitions` → `purge_due_partitions` を 1 つずつ実行し、最初に例外を出した Step で止める（それより前の Step の結果は、それぞれの Transaction で確定したまま残る）。
3. **被覆の確認**: Live の Partition が「今」から翌月末まで途切れずにあるか（`rules.first_uncovered_moment`）。Step が例外を出さなくても、途切れていれば失敗とする。
4. 結果を Audit に 1 行書く（下記 4）。書けなければ、それも失敗とする。

終了コードは `0`（成功）・`1`（拒否: 使い方の誤り、不正な値、実行中の別の実行）・`2`（環境: 設定、URL 未設定、Database に到達できない）・`3`（保守の失敗: Step の例外、被覆の不足、結果を記録できない）。
0 以外はすべて systemd にとって失敗で、`OnFailure=paw-audit-retention-failure.service` が起動する。例の失敗 Unit は、優先度 `crit` の Journal Entry と `wall` の Message を出す
（Mail・Chat の Webhook などの通知経路は配備ごとに差し替える。この Repository は特定の通知先を前提にしない）。

加えて、読み取りだけの `python -m paw_backend.cli audit-retention-check [--months-ahead N]` を用意する。被覆が足りなければ終了コード 3。
Timer とは別の監視（外部の Monitoring、手動の確認）から、Partition が尽きる前に気づくために使える。

### 4. 実行と結果の Audit（0027 の 5 への追加）

0027 の 5 は Partition ごとの操作（`audit.retention.partition_created` / `partition_archived` / `partition_purged`）を記録するが、
何もすることがなかった実行や、失敗した実行は記録されない（失敗した Step の Transaction は、その Partition の Audit 行ごと Rollback する）。
「実行と結果が Audit に残る」ために、実行ごとに 1 行を足す。

- `audit.retention.maintenance_completed`: 全 Step が成功し、被覆も足りた。`reason` は `created=N archived=N purged=N`。
- `audit.retention.maintenance_failed`: どこかで失敗した。`reason` は `<step>:<例外の型の名前>`（例 `ensure_partitions:ProgrammingError`、被覆の不足は `verify_coverage:coverage_gap`）。
  例外の Message は記録しない（接続情報などを含み得るため）。
- 共通: `resource_kind = audit_retention_run`（Partition 1 つではないため、0027 の `audit_partition` とは分ける）、`decision = allow`（0027 と同じ固定値。結果は `action` で区別する）、
  `actor_id` / `actor_role` は無人の定期実行なので `NULL`。`reason` は列の 64 文字に切り詰める。列・CHECK 制約・Migration は増やさない（0027 の 5 と同じ方針）。
- この 1 行は、Step とは**別の Transaction** で書く。失敗した Step が Rollback した後でも、失敗は記録される（`PostgresAuditSink` が拒否を別の短い Transaction で記録するのと同じ理由）。
  Database 自体に到達できない失敗は記録できないが、その場合も終了コードで通知される。

### 5. Purge

`--purge-after-days N` を指定したときだけ Purge が有効になる（0027 の 3 のとおり、既定は無効）。`archive_after_days`（180）より短い値は拒否する（`RetentionPolicy` の検証）。
`archive_after_days` と `horizon_months` は Command から変えられない（0027 の値を変えるには新しい Decision が要るため）。

## 代替案

- **cron**: 同じ Command で動く。推奨しない理由は 1 のとおり（逃した実行の補完・失敗時の Hook・Journal が標準で揃わない）。systemd の無い配備では cron を使ってよい。
- **Admin Capability（Web UI / API からの実行）**: 人が押さない限り動かず、「定期的に」を満たさない。また Web Application の Role では DDL を実行できないため、
  Backend が Migration の Role の Credential を持つことになり、0004 の 5 の 2（Role の分離）を弱める。**採らない。** 手動実行が要るときは同じ Command を Server 上で実行する。
- **Backend Process 内の Scheduler（asyncio の定期 Task）**: 同じく Backend に Migration の Role の Credential が要る。複数 Worker・再起動のたびの重複実行の扱いも要る。**採らない。**
- **PostgreSQL 内の Scheduler（`pg_cron` など）**: 拡張の導入が要り、Python 側の規則（`rules.py`）・Audit の書き方と二重になる。**採らない。**
- **失敗時に Audit を書かず、終了コードだけにする**: Issue の「実行と結果は Audit に残る」を満たさない。**採らない。**

## リスク

- **Database に到達できない失敗は Audit に残らない。** 終了コードと Journal・`OnFailure=` だけが頼りになる。
- **通知先は配備に依存する。** 例の失敗 Unit は Journal と `wall` だけで、ログインしていない Owner には届かない。Mail・Chat などへの差し替えは配備の作業。
- **Timer 自体が無効化・削除された場合**、失敗 Unit も動かない。`audit-retention-check` を別の監視から呼ぶことで補える（この Repository は外部の監視を用意しない）。
- **被覆の確認は Bookkeeping Table（`audit_retention_partitions`）を信じる。** PostgreSQL の Catalog と食い違った場合（0027 の「リスク」）は検知できない。
- Unit File の Path（`/opt/paw/...`）・User（`paw`）は例であり、配備に合わせた変更が要る。

## 決めてほしいこと

推奨の答えを添えて Human / Admin に問う。

1. **呼び出し元**: systemd timer（1 日 1 回、`Persistent=true`）+ Server ローカルの Command `audit-retention-run`。cron でも同じ Command を使える。Admin Capability は採らない。
   推奨: 提案どおり。
2. **接続**: `PAW_MIGRATION_DATABASE_URL` だけを使い、`PAW_DATABASE_URL` へ Fallback しない。
   推奨: 提案どおり。
3. **失敗の扱い**: 終了コード 0 / 1 / 2 / 3、被覆（翌月末まで）の不足も失敗、`OnFailure=` の例の Unit（Journal + `wall`）、読み取りだけの `audit-retention-check`。
   推奨: 提案どおり。
4. **Audit の追加**: 実行ごとの 1 行（`audit.retention.maintenance_completed` / `maintenance_failed`、`resource_kind = audit_retention_run`、別 Transaction）。
   推奨: 提案どおり。
5. **排他**: Advisory Lock で同時実行を拒否する（2 つ目は終了コード 1）。
   推奨: 提案どおり。

## 承認後の扱い

承認されるまで、Command・Runner・Unit File はコードとして入るが、実運用の Server に Timer を有効化（`systemctl enable --now paw-audit-retention.timer`）しない。
承認されたら、運用者が Unit File を配備し、Timer を有効にする。方針を変える場合は、この Decision を書き換えず、新しい Decision から `Supersedes` する。
