# Owner の初期設定と復旧の方針

- Status: Approved
- Date: 2026-09-24
- Scope: PAW-021（Initial Owner Setup / Recovery CLI）と、その Token を受け取る PAW-022（Login / Session）、PAW-023（Passkey / Step-up）の接続点
- Supersedes: なし
- Approval: 2026-09-25、Humanが作業Session内で、判断メモ（Artifact）の各点について「推奨どおり」と回答して承認（末尾の「承認時の決定」）

## 背景

[要件](../../REQUIREMENTS.md) は、Self registration を行わないこと、Owner に Passkey を必須とすること、
全 Passkey / 信頼済み端末を失った Owner は Ubuntu の sudo 経由の Recovery で復旧できることを定めている。
[PAW-021 の受け入れ基準](../IMPLEMENTATION_BACKLOG.md) は、最初の Web 訪問者を Owner にしないこと、CLI からの Owner 初期作成、
1 回限りの Setup / Recovery Token、Audit への記録、Owner Passkey の必須化への接続である。
要件は、Token の有効期限、試行の上限、Login name の形式、Database の権限、Recovery で無効にするものを定めていない。
実装は次の選択をした。Repository には、RBAC と Audit の方針を定める [Decision 0004](0004-rbac-capability-and-audit-policy.md)（Human が 2026-09-25 に承認。承認の記録は PAW-025 のブランチ `claude/paw-025-rbac-capability`（PR #69）にある）がある。
独立 Review が、Database の権限の分離、Token ID の扱い、`apply` Hook の Transaction を指摘し、実装を改めた。
**この Decision は 2026-09-25 に Human が承認した（Approved）。** 下の各選択は、承認された方針である（承認時の決定は末尾を参照）。

## 提案

1. **Owner の作成・復旧は Server 上の管理コマンドだけ。** Web にも HTTP API にも経路を作らない。
   コマンドは Backend の Package（`python -m paw_backend.cli`）に置き、`apps/cli/` の Client（公開 HTTP API だけを呼ぶ）には作らせない。
   Owner の作成と Token の発行は Operator 側のコード（`paw_backend.identity.operator`）だけにあり、Web 側は `TokenRedeemer`（Token を使うだけ）を使う。
   Test が、Import Graph の全体で、それ以外が Operator 側に届かないことを固定する。
2. **Token。** 256 bit の乱数、Salt 付き HMAC-SHA256 だけを保存、定数時間で比較、1 回限り、期限付き。
   **既定の有効期間は 30 分（60 秒〜4 時間）、Token ごとの試行の上限は 5 回（1〜20）。** どちらも設定で変えられる。数値の根拠は要件になく、Setup を始めてから Web で入力するまでに要する時間と、推測の余地を天秤にかけた暫定値である。
   Human は、この 2 つの数値を暫定値として承認した（2026-09-25）。設定で変えられ、運用で不便が出れば見直す。
3. **Token ごとの Lockout。** 試行は Secret を比較する前に予約し、上限を使い切った試行が `locked_at` を記録する。設定を後から変えても Lock された Token は開かない。
   失敗はすべて同じ失敗として返し、理由は Audit にだけ残す。
   Token 内の検索用の ID は、知っていれば試行を使い切らせられる（Owner の DoS）ため、**Audit と Log には書かず**、Token ごとの別の乱数（`audit_ref`）で呼ぶ。
   未知の ID や形式不正の Token は、誰でも Audit の行を作れることになるため DB に書かない（Log だけ）。
   この Lockout は Token ごとにしか効かない。**PAW-022 は、`redeem` を呼ぶ公開 Endpoint を出す前に、接続元ごとと全体の Rate Limit を付ける責務を負う。**
4. **Login name は ASCII のみ。** 小文字の英数字と `.` `_` `-`（3〜64 文字、先頭と末尾は英数字）、Unicode は NFKC で正規化し、それ以外は受け付けない。DB の CHECK 制約でも強制する。
   紛らわしい文字（Homoglyph）や制御文字を避けるためで、日本語の名前や Email を許すかは要件にない。Human は ASCII のみを承認した（2026-09-25）。
5. **Database の Role を Web と Operator に分ける。** Token の行を INSERT できる Role は、Owner を乗っ取れる（Review が実演した）。
   Web の Role（`PAW_APP_DATABASE_ROLE`）には `users` / `setup_tokens` の SELECT と、`redeem` が書く列（`attempts`、`used_at`、`locked_at`、行 Lock のための `users.updated_at`）の UPDATE だけを与え、
   INSERT と、Role・Salt・Hash・期限・`revoked_at` の UPDATE は与えない。
   Operator の Role（新しい設定 `PAW_OPERATOR_DATABASE_ROLE`）には INSERT と、`users.system_role` / `updated_at`、`setup_tokens.revoked_at` の UPDATE、`audit_events` の INSERT を与える。
   管理コマンドは `PAW_OPERATOR_DATABASE_URL` で接続し、未設定のときだけ `PAW_DATABASE_URL` に戻って警告する。Application は起動時に、接続 Role が Token を作れる場合に警告する。
   Trigger が、どの Role に対しても、Token の行を寿命に必要な変更だけにする（使用済みを復活させられない）。
   **守れないもの:** Schema の Owner と Superuser、Web の Role が Token を使用済みにして Owner を締め出すこと（`owner-recover` で回復する可用性の問題）、
   そして PAW-022 / PAW-023 が追加する Password と Passkey の Table を Application が書けること（Application の侵害で認証情報を変えられる可能性は残る）。
   Human は、これらの守れないものを受け入れて承認した（2026-09-25）。
6. **Recovery は管理コマンドだけ。** 確認フラグ（`--confirm-owner-recovery`）を要求し、未使用の Token をすべて無効にして新しい Token を発行し、Audit に残す。
   要件（`[FIXED]`）は Ubuntu の sudo 経由の Recovery なので、**`owner-recover` は実効 uid が 0（root。`sudo` が実行する User）でなければ拒否する**（`RecoveryNotPrivilegedError`、Audit に deny `not_privileged`、何も変更しない）。root かどうかは `OwnerOperator.recover_owner` が**自分の Process の `os.geteuid()` を読んで**決め、呼び出し側が渡した Identity は受け取らない（受け取れば、Library として呼ぶ非 root の Process が `OperatorIdentity(uid=0)` を渡して迂回できるため。Review の指摘）。
   `SUDO_UID` は環境変数で誰でも設定できるため認可に使わず、実行した Process の uid と `SUDO_UID` を（上と同じ、Service 自身の読み取りで）Token の行に数値で記録し、stderr に出す（調査の手掛かり）。`setup_owner` も呼び出し側からの Identity を受け取らない（記録を偽装させないため）。
   この確認は、DB の認証情報を持つ Process が誤って実行することを防ぐもので、**境界そのものではない**。境界は `PAW_OPERATOR_DATABASE_URL` を root だけが読めるファイルに置くこと。root の Process や User Namespace の中の uid 0 は通る。
   PAW-025 の `AuditEvent` に自由な項目がないため、Audit の Event へは Token の `audit_ref` から Join する。Human は、Audit に専用の項目を足さず、この Join のままにすることを承認した（2026-09-25）。
   PAW-022 / PAW-023 で専用の項目が必要と分かれば、Audit の Migration と Decision 0004 の変更が要るため、新しい Decision から `Supersedes` する。
   `owner-setup`（初回の Owner 作成）には OS User の確認を付けない（要件は Recovery だけを sudo と定めている）。Human は、付けないことを承認した（2026-09-25）。
   境界は「Operator 用の DB 接続先を root だけが読めるファイルに置く」ことである。
7. **Recovery の Token を受ける側の Contract（PAW-022 / PAW-023）。** `purpose = recovery` の `redeem` は、同じ Transaction で、既存の全 Session を失効させ、
   現在の Password を無効にして新しい Password を設定させ（または再設定を必須にし）、既存の Passkey をすべて失効させて（または再登録を必須にして）から Commit する。
   復旧が必要な状況は認証情報が盗まれた可能性を含むためである。`passkey_required` の Owner が Passkey を 1 つも持たない間は、Passkey の登録以外を許さない。
   `redeem` の `apply` Hook が Session を Commit・Rollback・Close すると Audit との整合が崩れるため、`redeem` はそれを拒否し全体を Rollback する。
8. **削除待ち・削除済みの Owner の置き換え。** その行が Owner の Unique Index を占め続けるため、`owner-setup` も `owner-recover` も進めなくなる。
   実際の状態を報告して拒否し、`owner-setup --replace-non-live-owner` を明示した場合だけ、古いアカウントを `user` に降格して新しい Owner を作る（1 つの Transaction、Audit 付き）。生きている Owner は置き換えない。
9. **`downgrade()` は `users` と `setup_tokens` を Table ごと破棄する**（Owner を含む全 User と全 Token が失われる）。開発・Test 用で、本番では実行しない。README に警告する。
10. **Token の期限は、消費の瞬間に判定する。** `redeem` は Owner の行を Lock してから Token を消費するため、別の Transaction が Lock している間は待たされる。待つ前に測った時刻で期限を判定すると、期限を過ぎた Token が使えてしまう（Review の指摘）。
    そのため、開始時の判定は「期限切れの Token が Lock を待たない」ための早道にとどめ、正本は、Owner の行と Token の行の両方を Lock した後の、消費する `UPDATE` の条件（`expires_at > greatest(<Lock の後に読み直した Process の Clock>, clock_timestamp())`）とする。`clock_timestamp()` はその文が行を判定する時点の DB の時計で、Transaction の開始時刻（`now()`）ではない。
    Process の Clock を残すのは、Test が時計を動かせるようにするためで、どちらか一方でも期限切れと言えば期限切れ（Clock のずれは Token の寿命を短くする側にしか働かない）。単一の Host の想定なので、Clock のずれを補正する仕組みは置かない。
    **Token の行も、Clock を読む前に Lock する（Review の指摘）。** 消費する `UPDATE` 自身も、Token の行を別の Transaction が持っていれば待たされる（Web の Role も、`redeem` が書く列の UPDATE 権限があるので、行を `SELECT ... FOR UPDATE` で持てる）。
    PostgreSQL の `UPDATE` は、行の Lock を待ったあと、持ち主が行を変えずに手放した場合、待つ前に評価した条件を評価し直さずに続行しうる。そのため、`expires_at > clock_timestamp()` を待つ前に評価した文は、待つ間に期限が切れた Token を消費し、待つ前の時刻を `used_at` に記録してしまう（Test で再現した）。
    そこで `redeem` は、Owner の行の次に Token の行を `SELECT ... FOR UPDATE` で Lock し、**その後で** Process の Clock を読み、消費する文（`clock_timestamp()` を含む）を走らせる。消費する文は自分が Lock 済みの行に対して動くので、もう待たない。Lock の順は Owner の行、Token の行で、発行する側（Owner の行、旧 Token の行）と同じなので、互いに Deadlock しない。
    試行を予約する `UPDATE` も同じ行を待ちうるが、その条件は `locked_at IS NULL AND attempts < 上限` という状態だけで、時刻ではない（待つ間に別の試行が行を更新すれば、PostgreSQL が条件を評価し直す）。その `locked_at` は待つ前の時刻のままだが、記録用で、判定には使わない。
    消費できなかった理由は、Token の行を Lock した後の状態で決める: 使用済み・無効化済みなら `token_unavailable`（Clock は読まず、消費する文も走らせない）、未使用・未無効のまま消費する文が何も更新しなければ `token_expired` として Audit に残す（外へ返す失敗は他と同じ）。
    **発行する側も同じ理由で、寿命は保存の瞬間から数える（Review の指摘）。** `owner-setup --replace-non-live-owner` と `owner-recover` は Owner の行を Lock し、古い Token を無効にしてから新しい Token を保存する。
    別の Transaction がその Lock を持つ間は待たされるため、待つ前に測った時刻から `created_at` / `expires_at` を決めると、待ちが TTL に近ければ、保存・表示された時点で期限切れの Token ができてしまう。
    そのため Process の Clock は Owner の Lock を得た後に読み、旧 Token の行も先に `SELECT ... FOR UPDATE` で Lock してから読み直す（無効化の `revoked_at`、旧 Owner の降格、新 User の `created_at` / `updated_at` にはこの値を使う。旧 Token の行を別の Transaction が持つ間の待ちの後になる）。Token の `created_at` / `expires_at` と、返す `IssuedToken.expires_at` は、待ちうる文（古い Token の無効化、新しい Owner の INSERT）がすべて終わった後にもう一度読んだ値から決める。
    Audit の時刻は、Event を作る時点（Lock の後）の Clock で、元から待ちの後である。発行する側は Process の Clock だけを使い、`clock_timestamp()` は使わない: Clock のずれは、受け取る側の `greatest(...)`（上の判定）が Token の寿命を短くする側にだけ働かせる。
    Owner がまだいない最初の `owner-setup` では、Lock する行がなく、待つ可能性があるのは、競合する別の未 Commit の Owner の INSERT が Unique Index を占めている間の新 User の INSERT だけである。その INSERT の `users.created_at` は待つ前の時刻のままだが、これは記録用で、Token の寿命には関わらない。

## リスク

- 既定値（30 分、5 回）と Login name の規則は、運用で不便が出れば変更が必要になる。設定で変えられるのは前者（暫定値として承認）だけで、後者は Migration が要る。
- 分離した Role は、`0021` が `0025`（`audit_events`）より前になる順序で統合されると、Operator の Audit への INSERT を後から与える必要がある。
- Web の Role が Token の行で Owner を締め出せる。回復は Operator の手作業に依存する。
- Token を保持するのは stdout の 1 回だけで、書き込みに失敗すると Token は失われる（終了コード 3 で知らせ、`owner-recover` で回復する）。

## 代替案

- Web の Setup 画面で最初の訪問者を Owner にする: Requirements と PAW-021 の基準に反する。
- 1 つの Database Role のまま運用する: 実装は単純だが、Application の侵害や SQL 経由の書き込みで Token を偽造できる。
- Token を Argon2 で Hash する: Secret が 256 bit の乱数のため、遅い Hash は効果がなく、依存を増やす。
- Login name に Unicode を許す: 要件に合うが、Homoglyph の対策を決めるまで保留する。
- `owner-recover` を root（sudo）に限らず、DB の認証情報を読める人なら誰でも実行できるままにする: 当初はこれを選んだが、要件（`[FIXED]`: Ubuntu の sudo 経由の Recovery）に反し、認証情報を得た非 root の Process が Owner の Recovery Token を発行できる（Review の指摘）ため、root を要求する形に改めた。Container で root 以外として動かす構成では Recovery できない（要件が Ubuntu を前提とする）。

## 承認後の扱い

2026-09-25 に承認された。この Decision を PAW-021 の実装の根拠として参照し、PAW-022 / PAW-023 は 3、5、7 を受け入れ条件に含める（Issue [#19](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/19) と [#20](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/20) に追記済み）。
承認後に方針を変える場合は、この Decision を書き換えず、新しい Decision から `Supersedes` する。
ただし、暫定値として承認した Token の有効期間と試行の上限（本文の 2）は、設定で変えられる。

## 承認時の決定（2026-09-25）

- 本文の各点を、提案どおり承認した。
- Token の有効期間 30 分（60 秒〜4 時間）と Token ごとの試行の上限 5 回（1〜20）は、暫定値として承認した。要件に根拠はなく、設定で変えられる。
- Login name は ASCII のみ（小文字の英数字と `.` `_` `-`、3〜64 文字、先頭と末尾は英数字）とする。
- `owner-setup` には root（sudo）の確認を付けない。`owner-recover` は実効 uid が 0 の Process だけとし、Container を root 以外として動かす構成では Recovery できない制約を認める。
- Database の Role の分離で守れない点（Schema の Owner と Superuser、Web の Role が Token を使用済みにして Owner を締め出すこと、PAW-022 / PAW-023 が追加する Password・Passkey の Table を Application が書けること）を受け入れる。
- 復旧の Audit の Event は、Token の `audit_ref` からの Join で調べる方式のままとし、Audit に専用の項目は足さない。
- PAW-022 / PAW-023 の受け入れ条件（`redeem` を公開する前の、接続元ごとと全体の Rate Limit。Web 用 Role の最小権限。復旧 Token の受け取り時の全 Session の失効と、Password・Passkey の無効化）は、Issue [#19](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/19) と [#20](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/20) に追記済みである。
