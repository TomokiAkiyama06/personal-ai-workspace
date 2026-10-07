# Deploy / Update / Rollback の仕組み（Release の形、手動の Update、保守と Drain、互換な Migration の印、DB の復旧点、Rollback、Audit の保存期間の Timer）

- Status: Proposed
- Date: 2026-10-07
- Scope: Issue [#54](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/54)（PAW-068: Deployment / Update / Rollback 機構）。`apps/backend/deploy/release/`（`paw_release.py`、`release.example.toml`）、`apps/backend/paw_backend/deploy/`、`apps/backend/paw_backend/cli/deploy.py`、Migration `0191`（`deploy_maintenance`）、`TaskQueue.claim_next`、`PostgresTaskHolds` の理由の引数、`apps/backend/deploy/systemd/paw-backend.service` と既存の Unit の Path
- Supersedes: なし。[Decision 0031](0031-audit-retention-scheduler.md)（Approved）・[Decision 0054](0054-recovery-repository-projection-restore.md)（Approved）・[Decision 0055](0055-kaggle-full-gpu-mode.md)（Approved）・[Decision 0043](0043-user-deletion-follow-ups.md) を書き換えない。それらの Command・Timer・Hold を Update の手順から使う

## 背景

[REQUIREMENTS.md](../../REQUIREMENTS.md) の「Deployment / Update / Rollback」（FIXED）と [DEPLOYMENT_UPDATE.md](../DEPLOYMENT_UPDATE.md) は次を決めている。

- 本体は Versioned release。V1 は Production を自動更新しない。Owner / Admin が明示的に Update を始める。
- 手順: 更新前の確認 → 新規 Task の受付停止 → Running Task を安全な Checkpoint まで Drain → （Schema / Data の Migration のときは）DB の Writer の停止 → Recovery Projection の最新化 → Migration の事前確認 → （Migration のときは）整合した DB の復旧点の取得と復元の検証 → 適用 → 新しい Version の起動 → Health check → 成功なら Queue の再開、失敗なら DB / Application を戻し、正常性を確かめてから再開。戻せなければ保守のまま Owner / Admin に通知。
- 更新前の確認: Recovery Repository が最新化できる、PostgreSQL の Health、Migration の互換性、Disk の空き、Running / Waiting の Task、現在と対象の Version、必要な Runtime / 依存。
- 復旧点: Migration の前に必須。Recovery Git に入れない。取得・容量・検証のどれかが失敗すれば Migration を始めない。DB を変えない Update には要らない。
- Rollback: 複数の Version を残し、直前の known-good へ戻せる。Migration は可能な限り後方互換（expand → 新旧両対応 → data → 旧の削除）。Application だけを戻して Schema と食い違う設計を避ける。戻した後は最新の User 削除状態を適用してから再開する。
- Critical Security Update では Safe drain を待たない `Stop Now` を使ってよい。
- 具体的な方式（Docker / systemd / Package の配置）は実装段階で決めてよい。

Issue #54 のコメント: Decision 0031 の Audit の保存期間の Timer（`paw-audit-retention.timer`）は配置して有効にするまで動かない。Deploy / Update の手順にこの Timer の配置と有効化を含め、Update の前の Health check で `audit-retention-check` の結果（翌月末までの Partition があるか）を確かめること。

**次のことは要件も既存の Decision も決めていない。** Release の形と名前、Update を誰がどこから始めるか、「受付停止」と「Drain」の具体的な意味、Drain の待ち時間と `Stop Now`、Migration が後方互換かをどう表すか、復旧点の取り方・検証の仕方・置き場所・保存期間・戻し方、Rollback の条件、記録の仕方。
[AGENTS.md](../../AGENTS.md) の「仕様変更」に従い、実装はこれらを下の推奨で置き、この Decision で承認を求める。

**この PR は実際のサーバーに何もしない。** Tool も Test も `systemctl` / `sudo` を実行せず、Service の停止・起動は設定の Command（例は `systemctl`）だけが行う。Test は一時 Directory と Fake（Service、Health、`pg_dump` / `pg_restore`）で行う。

## 提案

### 1. Release の形: 1 Commit = 1 不変の Release Directory と `current` の Symlink

- `paw-release build --repo <clone> --commit <ref>` が `<root>/releases/<version>/` を作る（`root` の例は `/opt/paw`）。中身は Commit の `git archive`、設定の `build` Command（例: `uv venv` / `uv pip install` / `npm ci --ignore-scripts` / `npm run build`）が作る Release 専用の venv と Web の Build、`release.json`（Commit、Schema の Head、Migration の鎖とそれぞれの互換性（4）、Source の Digest）。
- Version の名前は既定で `<Commit の日付 YYYYMMDD>-<SHA の先頭 12 桁>`（`--version` で別の名前も付けられる。`[A-Za-z0-9][A-Za-z0-9._-]{0,63}`）。
- **不変**: 同じ名前は作り直せない。Install / Update / Rollback の前に Source の Digest を確かめ、Build の後に変わった File があれば拒否する。
- Unit は `/opt/paw/current/...`（Symlink）を実行し、Version を名指ししない。切り替えは新しい Link を作って `rename` する（原子的）。既存の Timer の Unit（Audit の保存期間、Memory Projection、Recovery Backup、User の消去）の Path も `/opt/paw/current/` に変えた。Backend の Unit の例 `paw-backend.service` を足した。
- Release は自動では消さない（known-good へ戻れるように残す。古いものは運用者が消す）。
- Tool は標準 Library だけの Python（3.12 以上）で、Host の `python3` で動く（置き換える Release の venv に依存しない）。
- 代わりに、Docker Image（Tag が Version）にする案がある。GPU の Runtime・Linux User ごとの Git の Checkout（Decision 0029）・systemd の Timer と同じ Host で動く今の構成では、Image の境界が増えるだけで、Rollback の要件は Directory と Symlink で満たせる。

### 2. Update は Server 上の手動の Command だけ（HTTP API・画面・自動更新なし）

- Owner / Admin が Server で `sudo paw-release update <version>` を実行して始める。`paw-release precheck <version>` は何も変えずに確認と計画（Migration の有無）だけを表示する。
- Release の取得（`git fetch` と `build`）も手動。「Update available」を通知センターに出すことは、Release の配布の経路（Tag、署名）が決まってからの後続とする（今は配布の経路がない）。
- 理由: 要件は V1 の自動更新を禁じ、Owner / Admin の明示的な開始を求める。Web から始めると、Backend が自分を止める・DB の Owner の Credential を持つことになり、Decision 0031 の 6（Backend の User に Migration の Credential を持たせない）を弱める。

### 3. 受付停止と Drain: DB の保守の行、Running の Task の Hold、Drain の待ち、`--stop-now`

- **受付停止**: 保守の間、`deploy_maintenance` に 1 行がある（Migration 0191。Application の Role は SELECT だけ）。行がある間 `TaskQueue.claim_next` は何も渡さないので、**新しい Task は始まらない**。Task の作成と Queue への追加は拒否しない（Queue に残り、保守の後に順番どおりに始まる）。行は DB にあるので、どの Backend の Process にも効き、再起動の後も続き、保守中に取った復旧点を戻した DB でも保守は続く（明示的に終えるまで）。
- **Drain**: `running` の Task を Full GPU Mode と同じやり方（Decision 0055 の 2）で Hold する。Policy の Actor で `waiting`（Resource）、理由は固定の `Deploy / Update maintenance`。新しい Node は始まらず、走っている Node は終わるまで走る（Node の終わりが Checkpoint。結果・Branch・Worktree は残る）。**Drain の完了** = 有効な Lease の Claim が 0 で、`running` の Task が 0。Drain は Hold を繰り返す（保守の直前に Claim された Task も Hold される）。
- **再開**: 保守の終わりに、この保守が Hold した Task だけを `unblock` して同じ Priority で Queue に戻し（Full GPU Mode が Hold した Task は触らない。その逆も）、行を消す。途中で落ちても保守は続き（安全）、もう一度終えれば続きをする。
- **Writer の停止**: 設定の `stop` は Backend と、DB に書く Timer **とその Service**（Timer を止めても実行中の Job は止まらない）を止める。`systemctl stop` は止まるまで待つ。止められた Job は失敗を記録し、次の実行がやり直す。
- **Drain の待ち**: 既定 900 秒（設定 `drain_timeout_seconds`）。過ぎたら Update を中止し（何も切り替えない）、保守を終えて Task を再開する。
- **`--stop-now`**（Critical Security Update）: Drain が終わらなくても進む。走っている Node は Backend の停止で切られ、その Queue の Lease が切れ、Task は Hold されたまま保守の後に再開する（Task は Cancel しない）。
- 代わりに、保守の間は Task の作成を拒否する案（API が 503）がある。要件の「受付停止」により近いが、作った Task が失われないことの方が利用者には分かりやすいと考えた。

### 4. 後方互換な Migration の印: `paw_compatibility = "expand"`

- Migration の File は Module の変数 `paw_compatibility` で互換性を宣言できる。`"expand"` = **一つ前の Release がこの Schema の上で動く**（Table・Nullable な Column・Index の追加など。古い Code は新しいものを読まない）。`"contract"`（削除・改名・型の変更）、`"data"`（Data の書き換え）、**宣言なし**は互換ではないものとして扱う（既存の Migration はすべて宣言なし = 保守的）。
- `paw-release` は Build のときに各 File を（実行せずに）構文解析して `release.json` に鎖と互換性を残す。Rollback で DB が戻り先の Head より新しいとき、その間の Migration が**すべて `expand`** なら DB を戻さずに Application だけを戻す。1 つでも違えば復旧点（5）を要求する。
- 運用の方針（DEPLOYMENT_UPDATE.md の Rollback）: 削除・改名は、Code が古いものを使わなくなった次の Release で別の Migration（`contract`）にする。この PR の Migration 0191 は `expand` を宣言している。
- 後続（推奨）: 新しい Migration に `paw_compatibility` の宣言を Test で必須にする。並行して作られている他の Migration を壊さないよう、この PR では必須にしない。

### 5. DB の復旧点: Migration があるときだけ、`pg_dump`、隔離した DB で検証、新しい DB へ戻して名前を入れ替える、7 日で消す

- **いつ**: DB の Revision が対象の Head と違う（Migration がある）Update だけ。Writer（Backend と Timer）をすべて止めた後に取る（整合する）。
- **取り方**: Migration の Role（Table の Owner）で `pg_dump --format=custom` を `restore_point_dir`（例 `/var/lib/paw/restore-points`、0700。**Recovery Repository の中や上は拒否**）へ。一時名で書き、完了してから名前を付ける。`<label>.json` に DB の時計の取得時刻・Revision・Size・SHA-256 を残す。Credential は `PG*` 環境変数で渡し、Command 行・出力・Audit に出さない。
- **検証（隔離した環境）**: `PAW_DEPLOY_ADMIN_DATABASE_URL`（`CREATEDB` を持ち Workspace の DB を所有する Role。普通は Migration の Role で `postgres` DB に接続）で Scratch の DB を作り、`pg_restore --single-transaction --exit-on-error` で戻し、`alembic_version` が記録した Revision で Table があることを確かめてから DB を消す。結果を `.json` に残す。取得・容量・検証のどれかが失敗すれば Migration を始めない（Update を中止し、元の Release を起動し直して Task を再開する）。
- **戻し方**: 検証済みで Checksum が合う点だけ。新しい DB に戻して確かめてから、Workspace の DB を `<db>_replaced_<時刻>` に改名し、新しい DB にその名前を付ける。失敗した Migration が作ったものも残らない（`pg_restore --clean` では残る）。置き換えた DB は調べてから運用者が消す。
- **削除状態**: 復旧点を取った後に User が削除された（`user_status_changes` に `pending_deletion` / `deleted`）なら Restore を拒否する（要件: 戻した Backup が削除済みの個人データを復活させない）。Update の中の Rollback は、復旧点から戻すまで Writer が止まっているので該当しない。戻した後は設定の `post_restore`（例 `user-erasure-run`）を実行してから起動する。
- **保存期間**: 復旧点は個人データを含むので、`restore_point_keep_days`（既定 **7 日**）を過ぎたら消す（成功した Update / Rollback の後に自動、`paw-release prune-restore-points` で手動）。進行中の操作の点は消さない。
- DB を変えない Update（Application / Model だけ）は復旧点を取らない（要件どおり）。

### 6. Rollback: known-good だけへ、互換でなければ復旧点で

- Release は Update の Health check を通ると **known-good** になる（最初の Release は `install` で）。Rollback の戻り先は known-good だけ（既定は現在のものの直前の known-good）。
- **Update の中の自動の Rollback**: Migration・起動・Health check のどれかが失敗したら、新しい Release を止め、Migration をしていれば復旧点を戻し、`current` を元に戻し、`post_restore` を実行し、起動して Health check（Schema の Revision の一致と設定の Health の Command。既定 300 秒まで再試行）を通してから保守を終える（終了コード 4）。
- **手動の Rollback**（`paw-release rollback [--to V] [--restore-point L]`）: DB が戻り先の Head と同じか、間が `expand` だけなら Application だけ。そうでなければ戻り先の Head と同じ Revision の検証済みの復旧点を要求する（5 の削除の確認つき）。
- **戻せないとき**: 走っている Release の停止（設定の `stop`）・復元・起動・Health のどれかが失敗したら（停止に失敗したときは、まだ書き込むかもしれないので復元も切り替えもしない）、保守（Task は始まらない）のまま止め、設定の `notify`（例は Journal の `crit` と `wall`）を実行する（終了コード 5）。運用者が直した後、`paw-release end-maintenance` で再開する。
- 同時に 2 つの操作をしない（`state_dir/lock`）。中断した操作が残っている間は Update を拒否する（Rollback か `end-maintenance` で片付ける）。

### 7. 記録

- `state_dir/history.jsonl` に操作の各 Step（成功・失敗、終了コード、復旧点の Label）を 1 行ずつ残す。`state.json` に known-good の一覧と進行中の操作。
- `audit_events` に `resource_kind = deploy_update` の行を足す（Decision 0031 の 4 と同じく列・Migration は増やさない、`decision = allow`、Actor なし）: `deploy.maintenance.started`（`from=… to=… held=N`）/ `deploy.maintenance.ended`（`resumed=N remaining=N`）/ `deploy.restore_point.created`・`verified`・`restored`（Revision と数だけ。Path・URL は書かない）。DB を戻すとその後の行は消えるので、正本は History の File。

### 8. 更新前の確認と、Audit の保存期間の Timer（Issue #54 のコメント）

- `paw-release precheck` / `update` は次を確かめ、1 つでも失敗すれば何も変えずに拒否する（終了コード 1）: 対象の Release がある・Digest が合う・現在と違う、進行中の操作がない、Release と復旧点の場所の Disk の空き（`min_free_gib`、既定 5、例は 10）、現在の Release の `deploy-precheck`（DB に接続でき Revision が読める、保守中でない、**`audit_events` の Partition が翌月末まであること（`audit-retention-check` と同じ確認）**、最後の Recovery Backup が 90 分以内に成功していること（`recovery-backup-check` と同じ）、`PAW_RECOVERY_REPOSITORY_DIR` が設定されていること）、Schema の互換性（4）、設定の `precheck` の Command（例: `systemctl is-enabled --quiet paw-audit-retention.timer` と `paw-recovery-backup.timer`）。
- Running / Waiting / Queued の Task の数は `deploy-status` が表示する（Drain が扱うので拒否の条件にはしない）。
- **Deploy の手順**（DEPLOYMENT_UPDATE.md と Backend の README）に `paw-audit-retention.service` / `.timer` / `-failure.service` の配置と `systemctl enable --now paw-audit-retention.timer` を含め、Update の Command は Writer として Timer も止め、起動のときに Timer も起動する。
- Recovery Projection の最新化（`recovery-backup-run`）は Writer を止める前に毎回行う（失敗すれば中止）。

## 代替案

- **Docker / Compose で Image を切り替える**: 1 の理由で採らない。
- **Backend の中に Update の API を置く**: 2 の理由で採らない。
- **Migration を Alembic の `downgrade` で戻す**: Data を失う Migration・Data の Migration は戻せず、要件も復旧点を要求する。採らない（`downgrade` は Test でだけ使う）。
- **`pg_restore --clean` で同じ DB に上書きする**: 失敗した Migration が作った Table などが残り、次の Migration が失敗する。採らない（5 の名前の入れ替え）。
- **PostgreSQL の物理 Backup（`pg_basebackup` / PITR）**: Cluster 全体を戻し、他の DB にも及ぶ。V1 の 1 つの DB には論理 Dump で足りる。
- **Task を Hold せず、Drain を「Queue が空になるまで待つ」にする**: 長い Task があると Update が終わらない。Hold は Node の境目で止める。

## リスク

- `pg_dump` / `pg_restore` はサーバーの Major Version（18）以上が要る。Host にない場合は設定で Path を指す。この PR の Test は Fake で、実物での確認は作業環境の Docker（`pgvector/pgvector:pg18` の Client）で全 Schema の作成・検証・名前の入れ替えまでを手で 1 回行っただけ。
- 名前の入れ替えは DB への接続が残っていると失敗する（Writer を止め忘れた場合）。そのときは何も変えずに失敗し、保守のまま通知する。DB の名前に付いた権限（`GRANT CONNECT ON DATABASE`）と `ALTER DATABASE ... SET` は新しい DB に移らない（既定の `PUBLIC` の `CONNECT` で足りる構成を前提にする）。
- Health check の既定（DB の Revision と設定の Command）は浅い。`/api/v1/health/ready` は DB の Readiness だけを見る。より深い確認（System Health の Severity など）は設定の Command で足せる。
- `--stop-now` で切られた Node は途中の生成を失い、Retry を 1 回使う（Decision 0055 の 4 の Preempt と同じ）。
- 受付停止は Claim の直前に行を読む別の文で行う（Claim の文の Plan を変えないため）。読んだ直後に保守が始まった Claim は Task を始めるが、Drain の Hold が捕まえる。
- 最初の Install（`install`）は既存の DB の復旧点を取らない（Tool を使う前の配備から移るときは、運用者が手で `pg_dump` を取る）。
- Release の File の改ざんは Digest で見つけるが、venv と Web の Build は Digest の外（Build の Command が作るため）。Release は root が所有し、Backend の User に書かせない。

## 決めてほしいこと

推奨の答えを添えて Human / Admin に問う。

1. **Release の形**: `releases/<YYYYMMDD-sha12>/`（Commit の `git archive` + Release ごとの venv と Web の Build + `release.json`）と `current` の Symlink。不変（作り直し不可、Digest の確認）。自動では消さない。Tool は標準 Library だけの Python。
   推奨: 提案どおり。
2. **Update の始め方**: Server 上の手動の Command `paw-release update`（Owner / Admin、`sudo`）だけ。HTTP API・画面・自動更新なし。「Update available」の通知は配布の経路が決まってからの後続。
   推奨: 提案どおり。
3. **受付停止と Drain**: DB の保守の行（Migration 0191）がある間は Queue が何も渡さない（作成・Queue への追加は受け付ける）。Running の Task は Full GPU Mode と同じく Policy の Hold（理由 `Deploy / Update maintenance`）で Node の終わりまで Drain し、終わりに自分が Hold した Task だけ再開する。Drain の待ちは既定 900 秒で、過ぎたら中止。`--stop-now` は待たずに進む（Task は Cancel しない）。
   推奨: 提案どおり。保守の間の Task の作成を拒否したい場合は変更する。
4. **後方互換な Migration**: `paw_compatibility = "expand"` を宣言した Migration だけを互換とみなし、間がすべて `expand` なら DB を戻さずに Application を戻せる。宣言なしは非互換。新しい Migration への宣言の義務化（Test）は後続の PR。
   推奨: 提案どおり。
5. **DB の復旧点**: Migration があるときだけ、Writer を止めた後に `pg_dump`（0700 の専用 Directory、Recovery Repository の外）、`PAW_DEPLOY_ADMIN_DATABASE_URL` で作る Scratch の DB で検証、戻すときは新しい DB に戻して名前を入れ替え（置き換えた DB は残す）、復旧点の後に User が削除されていれば Restore を拒否、7 日で消す。
   推奨: 提案どおり。保存期間を変えるなら日数を決める（Decision 0043 の消去の 30 日より短くすること）。
6. **Rollback**: 戻り先は known-good だけ。Update の中の失敗は自動で戻す（復旧点の復元 → `post_restore`（`user-erasure-run`）→ 起動 → Health → 再開）。戻せなければ保守のまま `notify` を実行して止め、運用者が `end-maintenance` で再開する。
   推奨: 提案どおり。
7. **記録**: `history.jsonl` を正本にし、`audit_events` に `deploy.*`（`resource_kind = deploy_update`）を足す（列・Migration は増やさない）。
   推奨: 提案どおり。
8. **更新前の確認と Audit の保存期間の Timer**: 8 の確認（`audit_events` の翌月末までの Partition と Recovery Backup の鮮度を含む）に 1 つでも失敗すれば拒否。Deploy の手順に `paw-audit-retention.timer` の配置と有効化を含め、例の設定で Timer が有効であることを確かめる。
   推奨: 提案どおり。

## 承認後の扱い

- 承認されるまで、Tool・Command・Unit File はコードとして入るが、実運用の Server で `paw-release` による Update・Rollback を行わない（運用の切り替えは承認の後）。
- 承認されたら、運用者が README の「Deploy / Update / Rollback」の手順で `/opt/paw` の配置・`/etc/paw/release.toml`・`/etc/paw/deploy.env`・Unit File を用意し、最初の Release を `install` する。
- 方針を変える場合は、この Decision を書き換えず、新しい Decision から `Supersedes` する。
