# User の招待・端末の Pairing・User Lifecycle の方針

- Status: Proposed
- Date: 2026-09-28
- Scope: PAW-024（User Invite / Device Pairing、Issue [#21](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/21)）。[Decision 0015](0015-login-session-password-policy.md) の 14 節が「この Issue に含めない」とした複数端末の追加（QR / Link の Pairing、Owner / Admin の既存端末での承認）と、User の作成・招待・削除待ち・復元
- Supersedes: なし（0005・0015・0025 は書き換えない。0015 の 14 節が後の Issue に送った点を、この Decision で埋める）
- Approval: 未承認（Human の承認を待つ。承認されるまで、この Decision の推奨は方針として使わない）

## 背景

Issue #21 には Goal の本文がなく、受け入れ条件は次の 5 つだけである。

- invite-only registration
- one-time invite / pair token
- QR/link pairing
- Owner/Admin pairing は既存 trusted device 承認
- Invited / Active / Pending deletion / Deleted 状態

[REQUIREMENTS.md](../../REQUIREMENTS.md) の `[FIXED]`「User Invitation / Multi-device Login」「User Lifecycle」「User Deletion Retention」は、次を定めている（抜粋）。

- Self registration はしない。Owner / Admin が User を作成して招待する。招待はアカウントの初回作成時だけ使う。招待のリンク / コードは 1 回限り・期限付き・Revoke 可能。
- 2 台目以降は、ログイン済みの信頼済み端末から「新規端末を追加」する方式を基本導線とし、QR コードか共有リンクを発行する。QR / リンクは 1 回限りの Pairing Token を使い、有効期限は初期値 **10 分**、成功後は即失効、発行元の信頼済み端末から未使用の Token を手動で失効・再発行でき、同じ User で同時に有効な Token は 1 つまで（新規発行で旧 Token を失効）。
- 新端末で QR / リンクを開いた後、本人確認、端末名の登録、必要に応じた Passkey の登録を行う。一般 User は有効な QR / リンクで端末を追加できる。**Owner / Admin は QR / リンクに加えて、既存の信頼済み端末からの明示承認が必須**。
- Pairing Token の発行・使用・失効・新規端末の登録を Audit に残す。
- User の状態は `Invited` / `Active` / `Pending deletion` / `Deleted`（Suspended はない）。削除した時点で新規 Login 不可・既存 Session を即失効・新規 Agent の実行不可・実行中 Agent の安全停止・外部認証の利用停止。削除後は 30 日間 `Pending deletion` で保留し、30 日以内の復元は Owner だけ。30 日後に個人データを完全削除する。消去と検証が終わるまで `Deleted` と表示しない。

要件は、次を定めていない。実装（この Issue の PR）は動かすために値と選択を置いたので、**その全部を 1 か所に集め、各点に推奨を付けて**、Human が 1 回で承認または変更できるようにする。

- 招待 Token の有効期限、試行回数、形式。招待で作れる Role と、誰が作れるか。招待の発行に Step-up を要るか。
- Pairing の状態遷移（とくに Owner / Admin の承認の流れ）、「本人確認」の意味、QR / リンクに載せる形、承認の待ち時間、Pairing で作った Session の Passkey Gate。
- User の状態遷移の一覧、削除・復元の権限、`Pending deletion` から `Deleted` への遷移をこの Issue に含めるか。

実装は [Backend README](../../apps/backend/README.md) の「User Invite / Device Pairing / Lifecycle」に書いている。数値は設定か定数で、承認で変わっても Schema は変わらない（Token の有効期限の上限だけは DB の CHECK 制約でもある）。**Web の画面（招待の受け取り、QR の表示・読み取り、承認の画面）は作っていない**（Backend の API・Service・DB だけ）。

## 提案

### 1. 招待（invite-only registration）

- **Owner / Admin だけが User を招待できる**（`POST /api/v1/auth/invitations`、`admin.users.manage`）。招待は `users` に `invited` の行（Password も Passkey もない）を作り、1 回限りの招待 Token を 1 度だけ返す。**Self registration の経路はない**（公開 Route は Token を使う 1 つだけで、User を作れない）。
- **作れる Role は `user` と `admin`**。`admin` の招待は Owner だけ（既存の `decide_role_change` と同じ規則: Admin が関わる変更は `owner.admins.manage`）。Owner は招待で作らない（Owner は Server の CLI だけ。Decision 0005）。
- **招待の発行・再発行・失効は Owner / Admin の重要操作として、直近の Passkey の Step-up を要求する**（Account の Lock の解除と同じ。Decision 0025 の 7。Passkey が設定されていない環境では招待できない。Fail Closed）。
  - 代案: Step-up を要求しない（`admin.users.manage` だけ）。Admin の Session を盗んだ者が、自分の Admin を作れてしまうため採らない（判断点 1）。
- **Web の Role は `users` に INSERT できない**（0021・0022 の方針）。招待は `SECURITY DEFINER` の関数 `paw_invite_user`（`user` / `admin` の `invited` の行だけを作る）を使う。Web の Role には EXECUTE だけを与える。
- **招待 Token**: 形式は `pawiv1.<ID>.<Secret>`（Owner の Token と同じ作り。Secret は 256 bit、DB には Salt つき HMAC-SHA256 だけ、ID は検索の鍵で Audit には `audit_ref` だけ）。
  - **有効期限の既定は 72 時間**（`PAW_INVITATION_TTL_SECONDS`、600 秒〜14 日）。代案: 24 時間、7 日（判断点 2）。72 時間は「招待を送った週末をまたいでも使える」「漏れた Token が長く生きない」の間を取った。
  - **試行の上限は Token ごとに `PAW_SETUP_TOKEN_MAX_ATTEMPTS`（既定 5）**（Owner の Token と同じ値を共有する。上限に達した Token は二度と使えない）。受け取りの Route は Owner の Token と同じ接続元ごと・全体の Rate Limit（`redeem_source` / `redeem_global`）を数える。
  - **1 User に未使用の招待 Token は 1 つまで**（Partial Unique Index）。再発行（`POST /invitations/{id}/reissue`）は旧 Token を失効してから新しい Token を作る。失効（`POST /invitations/{id}/revoke`）は Token だけを失効し、User は `invited` のまま（再発行できる）。
- **受け取り**（`POST /api/v1/auth/invitations/redeem`、公開）: Token と新しい Password を受け取り、Token の消費と同じ Transaction で Password を設定し、User を `invited` → `active` にする。**Session は作らない**（Owner の Token と同じく、設定の後に通常の Login をする。Login の時点で Passkey の Policy の Gate がかかる）。「招待は初回作成時のみ」: Token は User が `invited` の間だけ使える（`active` になった User の Token は使えない）。
  - 代案: 受け取りで Session も作る（1 手減る）。Login と同じ Gate の判定・Throttle を 2 か所に持つことになるので採らない（判断点 3）。

### 2. Pairing（QR / リンク）

**状態遷移**（`device_pairings.state`。1 回の「新規端末を追加」が 1 行）:

```text
issued ──(一般 User の新端末が Token を出す)──────────────→ completed（新しい Session）
issued ──(Owner / Admin の新端末が Token を出す)──→ claimed ──(信頼済み端末が承認)──→ approved ──(新端末が完了)──→ completed
                                                   claimed ──(信頼済み端末が拒否)──→ rejected
issued / claimed / approved ──(本人が失効 / 新しい発行で置き換え / User の削除)──→ revoked
（どの状態も、期限を過ぎれば何にも使えない。期限切れは時刻で判定し、状態は変えない）
```

- **発行**（`POST /api/v1/auth/pairing`、`account.manage`）: 信頼済み端末（**Passkey の Gate が開いた有効な Session**）が発行する。同じ User の生きている Pairing（issued / claimed / approved）をすべて失効してから、新しい Token を 1 度だけ返す（「同時に有効な Token は 1 つまで」）。有効期限は **10 分**（要件の初期値。`PAW_PAIRING_TOKEN_TTL_SECONDS`、60〜3600 秒）。
- **QR / リンクに載せる形**: 応答の `link_path` は `/pair#<Token>`（Token は URL の **Fragment** に置く。Fragment は Server へ送られず、Access Log・`Referer` に残らない）。QR は Client がこのリンク（公開 Origin + `link_path`）を符号化する。Backend は Origin を知らないので Path だけを返す。
  - 代案: Query（`/pair?token=...`）。Proxy の Access Log に Token が残るので採らない（判断点 4）。
- **新端末の Token の提出**（`POST /api/v1/auth/pairing/claim`、公開）: Token、**端末名（必須）**、`remember_me` を受け取る。接続元ごと・全体の Rate Limit（新しい `pairing_source` / `pairing_global`。値は `redeem_*` の設定を使い、**正しい Token・Claim の要求は数えた回数を戻す**。承認待ちの Polling で Lock されないため）と、Token ごとの試行の上限（5）を数える。**Token ごとの試行は Token が `issued` の間だけ数える**（使用済みの Token への誤った提出で、承認待ちの行が Lock されないため）。
  - **一般 User**: Token が正しく期限内なら、その場で新しい Session を作り（Cookie を設定）、Token を使用済みにする（`completed`）。
  - **Owner / Admin**（**提出の時点の** Role で判定する）: Token を使用済みにし（`claimed`。同じ QR をもう一度使えない）、新端末に**別の 1 回限りの Claim**（`pawpc1.<Claim ID>.<Secret>`）を返す。**Claim ID は Pairing の ID（QR に載る）とは別の新しい乱数**で、QR を見た者が Claim を作って試行の回数を使い切り、正しい端末の完了を妨げることはできない。承認の待ち時間は Claim の時点から 10 分（同じ設定）。
- **本人確認の意味（推奨）**: 一般 User の新端末では、**信頼済み端末にだけ表示された 1 回限り・10 分の Token を持っていること**を本人確認とする（要件の「一般 User は有効な QR / 共有リンクによるペアリングで新規端末を追加可能」）。Password の再入力は求めない。Owner / Admin は、これに加えて信頼済み端末での明示承認（下記）を要る。
  - 代案 A: 新端末で Password も入力させる（QR が肩越しに盗み見られても足りない）。Pairing の利点（Password を打たない）が消える。代案 B: 新端末に短い確認 Code を表示し、信頼済み端末で入力させる（一般 User にも承認を求める形）。要件は一般 User に承認を求めていない（判断点 5）。
- **承認**（`GET /api/v1/auth/pairing/pending`、`POST /pairing/{id}/approve`、`POST /pairing/{id}/reject`）: 同じ User の信頼済み端末が、承認待ちの端末（端末名、提出の時刻、期限）を見て承認または拒否する。**Owner / Admin の承認は、承認する Session の直近の Passkey の Step-up を要る**（重要操作。Decision 0025 の 7。Passkey が設定されていない環境では、Owner / Admin は Pairing で端末を追加できず、Password（と Passkey）の通常の Login が代わりの経路として残る）。拒否は Step-up を要らない（安全な側の操作）。
- **完了**（`POST /api/v1/auth/pairing/complete`、公開）: 新端末が Claim を出す。承認済みなら Session を作って `completed`、承認待ちなら `202 pending_approval`（何も変えない）、拒否・失効・期限切れ・使用済みはすべて同じ `400 invalid_token`。
- **失効**（`DELETE /api/v1/auth/pairing`、`account.manage`）: 同じ User の生きている Pairing をすべて失効する。**発行元の端末に限らず、同じ User の信頼済み端末ならどれからでも失効できる**（要件は「発行元の端末から失効できる」で、失効は安全な側の操作なので広げた）。再発行は新しい発行（旧 Token は自動で失効）。
- **Pairing で作った Session**: `auth_method = pairing`（新しい値。`auth_sessions` の CHECK 制約を変える）。Passkey の Gate は、
  - 一般 User: Password の Sign-in と同じ規則（Policy の要求と登録数で決める。既定の `optional` なら `open`）。
  - Owner / Admin: **`open`**。承認した信頼済み端末の Passkey の Step-up が、新端末での Passkey の認証の代わりになる（端末に紐づく Passkey は新端末から使えないため、`assertion_required` にすると行き止まりになる）。
  - Session は **Step-up を持たない**（重要操作には、新端末で改めて Step-up が要る）。
- **Passkey の登録（「必要に応じた Passkey 登録」）**: Pairing の Session から、既存の Passkey の Ceremony（`/auth/passkeys/enroll/*`）で登録する。**既存の規則（Decision 0025）のまま**で、Passkey を 1 つも持たない User は「直近の認証」（Session の作成が有効時間内）で登録でき、**既に Passkey を持つ User は Passkey の Step-up が要る**。そのため、端末に紐づく Passkey しか持たない Owner / Admin は、Pairing した新端末で 2 つ目の Passkey を登録できない（同期される Passkey なら新端末で Step-up できる）。これを緩めるかは判断点 6。
- 同時実行: 発行・失効・提出・承認・完了は、User の行を `FOR UPDATE` で Lock してから Pairing の行を Lock する（順序が一定なので互いを待たない）。同じ Token の同時の 2 回の提出は、片方だけが成功する。

### 3. User の状態遷移（Lifecycle）

| 遷移 | 操作 | 権限 |
| --- | --- | --- |
| （なし）→ `invited` | 招待（1 節） | Admin（`user`）、Owner（`user` / `admin`） |
| `invited` → `active` | 招待 Token の受け取り | 招待された本人（Token） |
| `invited` → `deleted` | 招待の取消（`DELETE /api/v1/auth/users/{id}`） | Admin（`user`）、Owner（`user` / `admin`） |
| `active` → `pending_deletion` | 削除（同じ Route） | 同上 |
| `pending_deletion` → `active` | 復元（`POST /api/v1/auth/users/{id}/restore`） | **Owner だけ**（`owner.user_restore`）、削除から 30 日以内 |
| `pending_deletion` → `deleted` | 30 日後の完全削除 | **この Issue に含めない**（下記） |

- Owner はどの遷移の対象にもならない（Owner の置き換えは CLI の `--replace-non-live-owner`。Decision 0005）。自分自身は削除できない。
- **削除・招待の取消・復元はすべて Owner / Admin の重要操作として、直近の Passkey の Step-up を要る**（1 節と同じ。判断点 1）。
- **`users.status` の変更は `SECURITY DEFINER` の関数 `paw_change_user_status(user_id, from, to, now, actor)` だけ**（上の表の 4 つの遷移だけを許し、Owner の行は変えない）。Web の Role は EXECUTE だけを持ち、`users.status` の UPDATE 権限は持たない（0022 の方針のまま）。関数は同じ文で、変更の履歴（`user_status_changes`。追記専用、Web の Role は SELECT だけ）を 1 行書く。30 日の起点は、この履歴の `pending_deletion` への最新の変更の時刻である。
- **削除（`active` → `pending_deletion`）が同じ Transaction で行うこと**: 全 Session の失効（`account_closed`）、生きている Pairing の失効。既存の `SessionPrincipalProvider` は `active` 以外の User を匿名にするので、Session は行が残っていても効かない（0022 の `revoke_all_sessions_of` の意図のとおり、行も終わらせる）。
- **所有権の移譲**（要件「共有 Project 等を所有していれば削除前に移譲を要求」）: 対象が、Active / Archived の Project の**唯一の生きた Manager**（受諾済みで、本人の Account が `active`。`pending_deletion` の共同 Manager は数えない）なら削除を拒否する（409 `ownership_transfer_required`）。確認の前に、対象が Manager である Project の行を Project の Service と同じ `FOR UPDATE` で Lock する（同時の 2 人の Manager の削除、Manager の退出・降格と直列にするため）。Repository は Project に属するので、Project の Manager の確認で足りる（推測。判断点 7）。
- **削除した User の Project の Membership**: 削除（`active` → `pending_deletion`）は `project_members` の行を**消さずに残す**（復元で同じ Role に戻すため。消すと復元に要るデータを失う）。その代わり、Project の Manager の規則はすべて**生きた Manager**（受諾済みで、本人の Account が `active`）だけを数える: 最後の Manager の退出・除名・降格の拒否（`LastManagerError`）、Pending deletion の Project の復元の「Manager が残る」確認（`NoManagerError`、Decision 0008 の 5 と 4）、上の削除の前の所有権の確認。削除中の User の行を数えると、残りの Manager が退出・降格でき、誰も管理できない Project が残る（Codex の指摘）。Project の Service は Project の行の Lock の中で `users.status` を読み、削除は同じ行を Lock してから状態を変えるので、両者は直列になる。削除中の Manager の除名・降格は、生きた Manager が残るので拒否しない（判断点 13）。
- **招待の取消を `deleted` にする理由**: `invited` の User には Password・Passkey・Chat・Memory がなく、消す個人データがない（`users` の行と招待 Token の履歴だけ）。30 日の保留を置く意味がないので、直ちに `deleted` にする（代案: `invited` も `pending_deletion` を経る。判断点 8）。
- **Login name の再利用**: `users.login_name` は状態によらず一意（既存の制約）なので、招待の取消・削除の後も Login name は予約されたままで、同じ Login name で招待し直せない（取消した `alice` を招待し直すと `409 login_name_taken`）。実装はこのまま（推奨。過去の Audit・履歴の行が指す人を取り違えない）。代案: 取消・消去の完了で Login name を解放する（判断点 10）。
- **`users` の行は物理削除しない（Tombstone）**: `user_status_changes`（追記専用）の `user_id` の外部キーは `RESTRICT` で、履歴を持つ User の `users` の行は DELETE できない。後続の消去（`pending_deletion` → `deleted`）は、`users` の行を消さずに個人データを消して行を残す設計になる（推奨）。代案: 消去の経路だけが履歴の Trigger を意図して外す（判断点 11）。
- **この Issue に含めないこと（推奨。後続の Issue にする）**:
  - `pending_deletion` → `deleted`（30 日後の個人データの完全削除）。要件は Password hash・Passkey・Private Chat・Private Memory・個人設定・GitHub 認証情報・個人用 Files、さらに Recovery Projection・Recovery Git 履歴・clone / cache・DB backup / WAL の消去と検証を求め、**消去と検証が終わるまで `Deleted` と表示しない**。この Issue で状態だけを `deleted` にすると、この要件に反する。関数 `paw_change_user_status` もこの遷移を許さない。
  - 実行中 Agent の安全停止、GitHub / Codex / Claude の外部認証の停止（Session の失効と `active` でない User の匿名化で、新規の Login・新規の Agent の実行（Session を要る経路）は止まる。Agent の委任は `DatabasePrincipalDirectory` が `active` 以外を拒否するかを、後続の Issue で確かめる）。
  - User の一覧・検索の Endpoint、Web の画面。

### 4. Audit

すべて `audit_events` に ID と列挙値だけで残す（Token、Token ID、Claim、Login name、端末名は入れない。Token は `audit_ref` で呼ぶ）。変更は同じ Transaction（Fail-closed）、拒否は別の短い Transaction で Best Effort。

| `action` | 内容 |
| --- | --- |
| `auth.invitation.issue` | allow `issued` / `reissued`、deny `role_not_allowed`・`step_up_required`・`step_up_method_insufficient`・`login_name_taken`・`invalid_state` |
| `auth.invitation.revoke` | allow `revoked`（本人の操作）/ `superseded`（再発行）/ `cancelled`（招待の取消） |
| `auth.invitation.redeem` | allow `redeemed` / deny `token_mismatch`・`token_expired`・`token_used`・`token_revoked`・`attempts_exhausted`・`user_not_eligible` |
| `auth.pairing.issue`、`auth.pairing.revoke`（`revoked`・`superseded`・`account_closed`） | 発行と失効 |
| `auth.pairing.claim` | allow `pending_approval` / deny（招待の受け取りと同じ Token の理由） |
| `auth.pairing.approve`、`auth.pairing.reject` | 承認（deny `step_up_required` など）と拒否 |
| `auth.pairing.complete` | allow `completed`（新しい端末の登録。`resource_id` は新しい Session） |
| `auth.user.delete` | allow `deletion_pending` / `invitation_cancelled`、deny `role_not_allowed`・`step_up_required`・`ownership_transfer_required`・`invalid_state` |
| `auth.user.restore` | allow `restored`、deny `retention_expired`・`invalid_state`・`step_up_required` |

## 影響

- Migration `0124`（Revision ID は Decision 0024 との混同を避けるため 124）: `user_invitations`、`device_pairings`、`user_status_changes` の 3 つの Table、関数 `paw_invite_user` と `paw_change_user_status`、`auth_sessions.auth_method` の CHECK に `pairing`、`auth_throttles.scope` の CHECK に `pairing_source` / `pairing_global` を足す。
- 新しい設定 `PAW_INVITATION_TTL_SECONDS`（既定 259200）、`PAW_PAIRING_TOKEN_TTL_SECONDS`（既定 600）。
- 新しい公開 Route 3 つ（`/auth/invitations/redeem`、`/auth/pairing/claim`、`/auth/pairing/complete`）を `tests/test_authz_routes.py` の公開一覧に理由つきで載せる。

## リスク

- **Application が侵害されれば**、Web の Role が持つ権限（`paw_invite_user` の EXECUTE、招待・Pairing の Token の INSERT、`password_credentials` の書き込み）で、Admin の作成や他の User の Session の作成ができる。0005・0022・0025 が Password と Passkey で受け入れたものと同じ種類の限界で、この Issue で新しく塞ぐものではない。
- 一般 User の Pairing は Token の所持だけで Session ができる。QR を盗み見られる・リンクが別の人に転送されると、10 分以内なら別の人の端末が User として Sign-in する（判断点 5）。Pairing の完了は Audit に残り、User は端末の一覧から個別に Logout できる。
- 実際の Browser・QR の読み取り・Reverse Proxy を通した動作は確かめていない（`TestClient` と実 PostgreSQL まで）。

## 決めてほしいこと

1. 招待・招待の取消・削除・復元・Owner / Admin の Pairing の承認に、**直近の Passkey の Step-up を要求する**（推奨）か、Capability だけにするか。
2. 招待 Token の有効期限の既定: **72 時間**（推奨）/ 24 時間 / 7 日。
3. 招待の受け取りで Session を作らない（**推奨。受け取りの後に通常の Login**）か、その場で Session を作るか。
4. QR / リンクの Token を **URL の Fragment** に置く（推奨）か、Query に置くか。
5. 一般 User の Pairing の本人確認を **Token の所持だけ**（推奨）にするか、Password の再入力（代案 A）、信頼済み端末での確認 Code（代案 B）にするか。
6. 端末に紐づく Passkey しか持たない Owner / Admin が、Pairing した新端末で Passkey を追加できるようにするか（推奨は**この Issue では変えない**。変えるなら、承認済みの Pairing の Session を「登録の直近の認証」に数える案を後続の Issue にする）。
7. 削除の前の所有権の確認を「Active / Archived の Project の唯一の受諾済み Manager なら拒否」とする（推奨）か、別の基準にするか。
8. 招待の取消で `invited` を直ちに `deleted` にする（推奨）か、`pending_deletion` を経るか。
9. `pending_deletion` → `deleted`（30 日後の完全削除と消去の検証）、実行中 Agent の停止、外部認証の停止を**後続の Issue にする**（推奨）か、この Issue に含めるか。後続にする場合は、承認の後に Issue を作る。
10. 招待の取消・削除の後も Login name を**予約したままにする**（推奨）か、解放して再利用できるようにするか。
11. `users` の行を**物理削除しない（Tombstone）**（推奨。`user_status_changes` の外部キーは `RESTRICT`）か、後続の消去で行を消せるようにするか。
12. Owner / Admin の Pairing の承認で、承認する端末に見えるのは新端末が名乗った端末名だけで、承認と新端末を結びつけるもの（両方に表示する確認 Code など）はない。QR を見た者が正しい端末より先に提出すると、承認待ちはその者の 1 件になり、Step-up つきの承認でその者が Gate `open` の Admin の Session を得る。**この Issue では確認 Code を入れない**（推奨。承認の画面で提出の時刻と端末名を確かめる運用）か、確認 Code（判断点 5 の代案 B を Owner / Admin の承認に適用）を入れるか。

13. 削除中（`pending_deletion`）の User の `project_members` の行を**残し、Manager の規則では数えない**（推奨。復元で Role が戻る。削除中の User は Member の一覧には残り、生きた Manager が除名・降格できる）か、削除で行を消す（復元で Role が戻らない）か、削除中の Member を一覧から隠すか。あわせて、唯一の Manager が削除中の Pending deletion の Project は、その User を復元するまで Owner / Admin も Project を復元できない（`NoManagerError`）。これでよいか（推奨）、Owner / Admin の復元を許して Manager のいない Archived の Project を認めるか。

## 推測した点（実装者が要件から解釈した点）

- 「信頼済み端末」を「Passkey の Gate が開いた有効な Session」と解釈した（Passkey が設定されていない環境では、有効な Session すべて）。
- 「発行元の信頼済み端末から失効」を「同じ User の信頼済み端末から失効」に広げた。
- 承認の待ち時間（Claim から 10 分）は要件にない。Pairing Token と同じ設定を使った。
