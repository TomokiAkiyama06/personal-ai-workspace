# Owner の初期設定と復旧の方針

- Status: Proposed
- Date: 2026-09-24
- Scope: PAW-021（Initial Owner Setup / Recovery CLI）と、その Token を受け取る PAW-022（Login / Session）、PAW-023（Passkey / Step-up）の接続点
- Supersedes: なし
- Approval: 未承認（Human の承認待ち）。承認されるまで、この文書の各項目は暫定の実装選択

## 背景

[要件](../../REQUIREMENTS.md) は、Self registration を行わないこと、Owner に Passkey を必須とすること、
全 Passkey / 信頼済み端末を失った Owner は Ubuntu の sudo 経由の Recovery で復旧できることを定めている。
[PAW-021 の受け入れ基準](../IMPLEMENTATION_BACKLOG.md) は、最初の Web 訪問者を Owner にしないこと、CLI からの Owner 初期作成、
1 回限りの Setup / Recovery Token、Audit への記録、Owner Passkey の必須化への接続である。
要件は、Token の有効期限、試行の上限、Login name の形式、Database の権限、Recovery で無効にするものを定めていない。
実装は次の選択をした。Repository には、RBAC と Audit の方針を提案する [Decision 0004](0004-rbac-capability-and-audit-policy.md)（Proposed、PAW-025 のブランチ `claude/paw-025-rbac-capability` にあり、この文書と同じく未承認）がある。
独立 Review が、Database の権限の分離、Token ID の扱い、`apply` Hook の Transaction を指摘し、実装を改めた。

## 提案

1. **Owner の作成・復旧は Server 上の管理コマンドだけ。** Web にも HTTP API にも経路を作らない。
   コマンドは Backend の Package（`python -m paw_backend.cli`）に置き、`apps/cli/` の Client（公開 HTTP API だけを呼ぶ）には作らせない。
   Owner の作成と Token の発行は Operator 側のコード（`paw_backend.identity.operator`）だけにあり、Web 側は `TokenRedeemer`（Token を使うだけ）を使う。
   Test が、Import Graph の全体で、それ以外が Operator 側に届かないことを固定する。
2. **Token。** 256 bit の乱数、Salt 付き HMAC-SHA256 だけを保存、定数時間で比較、1 回限り、期限付き。
   **既定の有効期間は 30 分（60 秒〜4 時間）、Token ごとの試行の上限は 5 回（1〜20）。** どちらも設定で変えられる。数値の根拠は要件になく、Setup を始めてから Web で入力するまでに要する時間と、推測の余地を天秤にかけた暫定値である。
3. **Token ごとの Lockout。** 試行は Secret を比較する前に予約し、上限を使い切った試行が `locked_at` を記録する。設定を後から変えても Lock された Token は開かない。
   失敗はすべて同じ失敗として返し、理由は Audit にだけ残す。
   Token 内の検索用の ID は、知っていれば試行を使い切らせられる（Owner の DoS）ため、**Audit と Log には書かず**、Token ごとの別の乱数（`audit_ref`）で呼ぶ。
   未知の ID や形式不正の Token は、誰でも Audit の行を作れることになるため DB に書かない（Log だけ）。
   この Lockout は Token ごとにしか効かない。**PAW-022 は、`redeem` を呼ぶ公開 Endpoint を出す前に、接続元ごとと全体の Rate Limit を付ける責務を負う。**
4. **Login name は ASCII のみ。** 小文字の英数字と `.` `_` `-`（3〜64 文字、先頭と末尾は英数字）、Unicode は NFKC で正規化し、それ以外は受け付けない。DB の CHECK 制約でも強制する。
   紛らわしい文字（Homoglyph）や制御文字を避けるためで、日本語の名前や Email を許すかは要件にない。
5. **Database の Role を Web と Operator に分ける。** Token の行を INSERT できる Role は、Owner を乗っ取れる（Review が実演した）。
   Web の Role（`PAW_APP_DATABASE_ROLE`）には `users` / `setup_tokens` の SELECT と、`redeem` が書く列（`attempts`、`used_at`、`locked_at`、行 Lock のための `users.updated_at`）の UPDATE だけを与え、
   INSERT と、Role・Salt・Hash・期限・`revoked_at` の UPDATE は与えない。
   Operator の Role（新しい設定 `PAW_OPERATOR_DATABASE_ROLE`）には INSERT と、`users.system_role` / `updated_at`、`setup_tokens.revoked_at` の UPDATE、`audit_events` の INSERT を与える。
   管理コマンドは `PAW_OPERATOR_DATABASE_URL` で接続し、未設定のときだけ `PAW_DATABASE_URL` に戻って警告する。Application は起動時に、接続 Role が Token を作れる場合に警告する。
   Trigger が、どの Role に対しても、Token の行を寿命に必要な変更だけにする（使用済みを復活させられない）。
   **守れないもの:** Schema の Owner と Superuser、Web の Role が Token を使用済みにして Owner を締め出すこと（`owner-recover` で回復する可用性の問題）、
   そして PAW-022 / PAW-023 が追加する Password と Passkey の Table を Application が書けること（Application の侵害で認証情報を変えられる可能性は残る）。
6. **Recovery は管理コマンドだけ。** 確認フラグ（`--confirm-owner-recovery`）を要求し、未使用の Token をすべて無効にして新しい Token を発行し、Audit に残す。
   要件（`[FIXED]`）は Ubuntu の sudo 経由の Recovery なので、**`owner-recover` は実効 uid が 0（root。`sudo` が実行する User）でなければ拒否する**（`RecoveryNotPrivilegedError`、Audit に deny `not_privileged`、何も変更しない）。root かどうかは `OwnerOperator.recover_owner` が**自分の Process の `os.geteuid()` を読んで**決め、呼び出し側が渡した Identity は受け取らない（受け取れば、Library として呼ぶ非 root の Process が `OperatorIdentity(uid=0)` を渡して迂回できるため。Review の指摘）。
   `SUDO_UID` は環境変数で誰でも設定できるため認可に使わず、実行した Process の uid と `SUDO_UID` を（上と同じ、Service 自身の読み取りで）Token の行に数値で記録し、stderr に出す（調査の手掛かり）。`setup_owner` も呼び出し側からの Identity を受け取らない（記録を偽装させないため）。
   この確認は、DB の認証情報を持つ Process が誤って実行することを防ぐもので、**境界そのものではない**。境界は `PAW_OPERATOR_DATABASE_URL` を root だけが読めるファイルに置くこと。root の Process や User Namespace の中の uid 0 は通る。
   PAW-025 の `AuditEvent` に自由な項目がないため、Audit の Event へは Token の `audit_ref` から Join する。Audit に専用の項目を足すかは決めていない。
   `owner-setup`（初回の Owner 作成）には OS User の確認を付けていない（要件は Recovery だけを sudo と定めている）。付けるかは人間が決める。
7. **Recovery の Token を受ける側の Contract（PAW-022 / PAW-023）。** `purpose = recovery` の `redeem` は、同じ Transaction で、既存の全 Session を失効させ、
   現在の Password を無効にして新しい Password を設定させ（または再設定を必須にし）、既存の Passkey をすべて失効させて（または再登録を必須にして）から Commit する。
   復旧が必要な状況は認証情報が盗まれた可能性を含むためである。`passkey_required` の Owner が Passkey を 1 つも持たない間は、Passkey の登録以外を許さない。
   `redeem` の `apply` Hook が Session を Commit・Rollback・Close すると Audit との整合が崩れるため、`redeem` はそれを拒否し全体を Rollback する。
8. **削除待ち・削除済みの Owner の置き換え。** その行が Owner の Unique Index を占め続けるため、`owner-setup` も `owner-recover` も進めなくなる。
   実際の状態を報告して拒否し、`owner-setup --replace-non-live-owner` を明示した場合だけ、古いアカウントを `user` に降格して新しい Owner を作る（1 つの Transaction、Audit 付き）。生きている Owner は置き換えない。
9. **`downgrade()` は `users` と `setup_tokens` を Table ごと破棄する**（Owner を含む全 User と全 Token が失われる）。開発・Test 用で、本番では実行しない。README に警告する。

## リスク

- 既定値（30 分、5 回）と Login name の規則は、運用で不便が出れば変更が必要になる。設定で変えられるのは前者だけで、後者は Migration が要る。
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

承認された場合、この Decision を PAW-021 の実装の根拠として参照し、PAW-022 / PAW-023 は 3、5、7 を受け入れ条件に含める。
承認前は、実装は暫定の選択として扱い、変更する場合は新しい Decision から `Supersedes` する。
