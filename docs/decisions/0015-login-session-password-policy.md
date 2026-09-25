# Login・Session・Password Policy の方針

- Status: Proposed
- Date: 2026-09-25
- Scope: PAW-022（Workspace Login / Session / Password Policy、Issue [#19](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/19)）と、その上に載る PAW-023（Passkey / Step-up、[#20](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/20)）、Owner の初期設定・復旧（[Decision 0005](0005-owner-setup-and-recovery.md)）との接続点
- Supersedes: なし。ただし 12 節は、`REQUIREMENTS.md` の `[FIXED]`「Passkey Policy」を「固定の方針」から「Owner が変えられる設定の**既定値**」へ読み替える提案を含む（`REQUIREMENTS.md` は書き換えない。承認後に別途、要件側の記述を直すかを決める）
- Approval: 未承認（Human の承認待ち）

## 背景

[REQUIREMENTS.md](../../REQUIREMENTS.md) は Login / Session / Password について次を定める（`[FIXED]`）。

- Password は最低 10 文字。文字種の組み合わせは強制しない。Argon2id 等でハッシュ化する。
- Login 失敗は段階的 Backoff（1〜4 回は通常、5 回目は約 30 秒待機、以降 1 分、5 分など）。永久 Lock はしない。Owner / Admin が一時 Lock を解除できる。Owner / Admin の異常な失敗は Audit へ記録する。
- 通常の Password 変更では他端末の Session を維持するか全端末を Logout するかを選べる（既定は維持）。Reset・Owner Recovery では原則として全 Session を失効する。本人の変更には現在の Password（または Passkey）を要求する。
- 通常 Session は 30 日間無操作で失効。「ログイン状態を維持」は最大 90 日。Session は端末単位で管理し、最終利用日時を見て個別に Logout できる。他のすべてを一括 Logout できる。
- Passkey: Owner / Admin は必須、User は任意（UI で強く推奨）。Owner / Admin の重要操作は直近 30 分以内の Step-up を要求する。

一方で要件は、**具体的な数値・Cookie の属性・Backoff の段階・Argon2id の Parameter・Session の絶対期限・Rate Limit の値・Audit に残す項目**を定めていない。
[AGENTS.md](../../AGENTS.md) の「仕様変更」は、重要判断を `docs/decisions/` に記録して人間 / Admin の承認を得ると定める。実装（PAW-022）は動かすためにこれらへ値を置いたので、**その全部を 1 か所に集め、各点に推奨を付けて**、Human が 1 回で承認または変更できるようにする。

Decision 0005 は、PAW-022 が満たす条件（Issue #19 に追記済み）として、次の 3 点を定めた。この提案はそれを実装で満たしている。

1. `redeem`（Token を使う処理）を公開する前に、接続元ごとと全体の Rate Limit を付ける。
2. Web 用 DB Role の権限を最小に保つ（Operator 用の Role と分ける）。
3. 復旧 Token の受け取り時に、全 Session を失効し、Password（と将来は Passkey）を無効にして、再設定を必須にする。

実装は [Backend README](../../apps/backend/README.md) の「Login / Session / Password Policy」に書いている。数値は設定（`PAW_` の環境変数）または定数で、**変更しても Schema は変わらない**（Migration は不要）。

## 提案

### 1. Argon2id の Parameter

| 項目 | 推奨 | 設定 |
| --- | --- | --- |
| Memory | 64 MiB（65,536 KiB） | `PAW_PASSWORD_HASH_MEMORY_KIB`（下限 19,456、上限 1,048,576） |
| 反復（time cost） | 3 | `PAW_PASSWORD_HASH_TIME_COST`（1〜10） |
| 並列度 | 4 | `PAW_PASSWORD_HASH_PARALLELISM`（1〜16） |
| 同時に計算する数 | 2 | `PAW_PASSWORD_HASH_CONCURRENCY`（1〜16）。待つ Job は最大 64、超えると 503 |
| Salt / Tag | 16 byte / 32 byte | 定数 |

- RFC 9106 の 2 番目の推奨（低 Memory 向け）と同じ値。1 台の GPU Server で個人〜小規模 Team が使う前提で、同時 2 件でも Memory は約 128 MiB、1 回は数十 ms（この開発機の実測で約 30 ms）。
- 下限は OWASP の最小構成（19 MiB）。それより弱い設定は起動時に拒否する。Test だけが最小値を使う。
- **Parameter を上げても既存の Password は壊れない。** Hash は自分の Parameter を持ち、Login に成功したときに現在の設定より弱ければ計算し直して保存する（同時の変更を上書きしない Compare-and-Swap）。
- 計算は専用の Thread で行い、Event Loop を止めない。Login しようとする名前が存在しないときも、同じ Parameter の固定の Hash に対して検証し、時間の違いで存在が分からないようにする（限界は 9 節）。

### 2. Password の Policy

| 規則 | 推奨 |
| --- | --- |
| 最小長 | **10 文字（要件どおり）**。Unicode の文字数で数える（日本語の 10 文字は 30 byte でも足りる） |
| 最大長 | 256 文字、かつ 1,024 byte。Argon2 自体はもっと長くても扱えるが、認証前の Request が使える計算を抑えるための上限 |
| 正規化 | NFKC（全角と半角、合成と分解の違いを同じにする）。長さも Policy もその後の値で判定する |
| 使えない文字 | 制御文字（NUL、改行、Tab を含む）、Surrogate、未割り当ての Code Point |
| 文字種の強制 | **しない**（要件） |
| 拒否 | 広く知られた Password の短い一覧（10 文字以上の 87 個。大文字小文字を区別しない）、3 種類以下の文字しかない Password（`aaaaaaaaaa`、`1212121212`、`abcabcabcabc`）、Login name と同じ Password、6 文字以上の Login name を含む Password、現在と同じ Password（変更のとき） |

- 一覧は流出 Password の Corpus の代わりにはならない（外部の API へ問い合わせる案は、Password の Hash の一部でも外へ出すため採らない。Freshness と Privacy の判断が要る）。将来 Corpus を入れる場合は新しい Decision で扱う。
- 拒否した理由は Code（`too_short`、`too_long`、`invalid_character`、`too_common`、`contains_login_name`、`same_as_current`）で返す。入力は返さない。

### 3. Session の識別子と Cookie

- Session ID は OS の CSPRNG の **32 byte（256 bit）**。Client へは URL-safe Base64（43 文字）で Cookie にだけ載せる。**DB には SHA-256 だけ**を保存し、ID 自体は保存しない（DB を読まれても使える Session が得られない）。高 Entropy なので Salt は要らない。
- Cookie:

| 属性 | 推奨 | 理由 |
| --- | --- | --- |
| 名前 | `__Host-paw_session` | `__Host-` は Browser が、Secure、Path=/、Domain なしを強制する（兄弟の Sub-domain から植え付けられない） |
| `Secure` | 常に付ける（設定なし） | 要件（Secure + HttpOnly） |
| `HttpOnly` | 常に付ける | 要件。Script から読めない |
| `SameSite` | `Strict`（`PAW_SESSION_COOKIE_SAMESITE` で `Lax` に変えられる） | 6 節の CSRF の 2 層目。別 Site から始まる Request に Cookie を送らない。同じ Site の Web Client からの Fetch には送られる |
| `Path` | `/` | `__Host-` の条件 |
| Max-Age | 通常 Session: なし（Browser を閉じると消える）。Remember Me: 残りの寿命 | 4 節 |

### 4. Session の寿命

要件の文「通常 Session は 30 日間無操作で失効。Remember Me は最大 90 日」を、次の 2 つの数値で表す。

| | 無操作の上限 | 絶対の上限（開始から） | Cookie |
| --- | --- | --- | --- |
| 通常 | **30 日**（要件。使うたびに延びる） | **90 日**（この提案） | Session Cookie |
| Remember Me | **90 日**（要件の「最大 90 日」） | **90 日** | 永続 Cookie（Max-Age = 残り） |

- 設定: `PAW_SESSION_IDLE_DAYS`（30）、`PAW_SESSION_REMEMBER_DAYS`（90）、`PAW_SESSION_ABSOLUTE_DAYS`（90）。値は Session の行に写すので、設定を変えても既存の Session は変わらない。
- **判断が要る解釈が 2 つある。**
  - 「最大 90 日」を、無操作の上限であり同時に絶対の上限としている（使い続けても 90 日で Login し直す）。別の読みは「無操作の上限が 90 日で、絶対の上限はない」。
  - 通常 Session にも絶対の上限（90 日）を置いた。要件は無操作の 30 日しか定めていない。別の案は「通常 Session に絶対の上限を置かない（使い続ける限り続く）」。
- 有効かどうかは、行を判定する**その文の中で** Database の時計（`clock_timestamp()`）を読んで決める。Test は Process 側の時計を差し込むが、使うのは「Process の時計と Database の時計の**新しいほう**」なので、Process の時計が遅れていても寿命は**長くならない**。
- 「最終利用日時」は最大でも 60 秒に 1 回しか書かない（`PAW_SESSION_TOUCH_INTERVAL_SECONDS`）。毎回書くと読み取りごとに書き込みが起きる。端末の一覧の精度は 1 分。
- 終わった Session の行は、**終わってから 30 日**たつと（Login のたびに最大 50 行ずつ）消す。終わった時刻は、無操作の期限（最後の利用 + 無操作の上限。絶対の上限を超えない）と失効の時刻の早いほう。CHECK 制約が「無操作の期限 ≤ 絶対の期限」を保つので、絶対の期限に達した Session もこれで足りる（絶対の期限だけで判定すると、30 日で無操作になった通常 Session が 60 日目でなく 120 日目まで残る）。

### 5. Session ID の Rotation

- **Login のたびに新しい ID を作る。** Browser がまだ古い Session の Cookie を持っていれば、それは失効させる（`replaced`）。攻撃者が仕込んだ ID は決して採用されない（Session Fixation）。
- **Password の変更と Step-up のとき、同じ Session の ID を作り直す**（権限・認証の強さが変わる操作）。古い ID は即座に効かなくなる。2 つの Request が同時に同じ Session を Rotation しても、片方だけが成功する（Compare-and-Swap）。
- **Role の変更では ID を作り直さない。** Session の ID は Role を持たず、Role は毎回 `users` から読むので、昇格・降格は次の Request から効く。Role を変えた User の Session を終わらせたい処理（User 管理）は `AuthService.revoke_all_sessions_of` を呼ぶ。
- 猶予期間（古い ID を数秒だけ残す）は**置かない**。Password の変更の瞬間に別の Tab が古い Cookie で送った Request は 401 になる。置くと古い ID を盗んだ者にも猶予を与える。置くかは判断点。

### 6. CSRF

- 状態を変える Request（`POST`、`PUT`、`PATCH`、`DELETE`）で、Browser が別の Origin から送ったものを、Cookie の有無にかかわらず**受け付ける前に**拒否する（403 `forbidden_origin`）。Login も対象（Login CSRF は被害者を攻撃者のアカウントへ Login させる）。
  - `Origin` があれば、Request 自身の Host と同じ Origin、または `PAW_ALLOWED_ORIGINS` のどれかであること。`Origin: null` は拒否。
  - `Origin` がなく `Sec-Fetch-Site` があれば `same-origin` か `none`。
  - どちらもなければ Browser ではないと見なして通す（CLI、Script。Session ID を持っていなければ何もできない）。
- 1 層目は 3 節の `SameSite=Strict`。この 2 つで CSRF Token（Double Submit）を要らなくする。

### 7. Login の段階的 Backoff

| | 推奨 | 設定 |
| --- | --- | --- |
| Account | 5 回目の試行で Lock が始まる（1〜4 回は通常） | `PAW_LOGIN_ACCOUNT_FREE_ATTEMPTS`（5） |
| 接続元 | 20 回目の試行で Lock が始まる | `PAW_LOGIN_SOURCE_FREE_ATTEMPTS`（20） |
| Lock の長さ | 30 秒、1 分、5 分、15 分、**1 時間**（最後の値を繰り返す。各値は 1 日以下しか設定できない） | `PAW_LOGIN_BACKOFF_SECONDS` |
| 数え直し | 最後の試行（または Lock の終わり）から 24 時間で忘れる | `PAW_LOGIN_DECAY_SECONDS` |

- **永久 Lock はない。** Lock は最大 1 時間で、その間に試行しても延びない（Lock 中の試行は数えない）。
- **Account は Login name の Hash で数える。存在しない名前も、存在する名前と同じ Lock になる**（Login の失敗の応答で存在が分からない。Lock の応答も同じ）。接続元は IPv4 の Address、IPv6 は上位 64 bit（Reverse Proxy 経由では Uvicorn が解決した Client Address。`FORWARDED_ALLOW_IPS` の設定が前提）。
- **同時に大量に来ても Backoff が守られる。** 試行は Password を比べる**前**に 1 つの文で数えて予約する。上限に達した試行が Lock を作り、同時に来た残りは、比べられずに拒否される（5 回の上限なら、比べられる Password は同時に来ても 5 つまで）。成功すると Account の数はなくなり、接続元の数は自分の 1 回分だけ戻る（成功した 1 回では、その接続元の他の失敗は消えない）。
- Password の変更と Step-up の Password の間違いも同じ Account の数に加える（乗っ取られた Session から Password を総当たりできない）。
- **Owner / Admin が解除できる**（`POST /api/v1/auth/users/{id}/unlock`、`admin.users.manage`）。Admin は User の Lock だけを、Admin と Owner の Lock は Owner だけが解除できる。
- **Owner を永久に締め出す経路がない（Recovery Path）。** 上の通り Lock は最大 1 時間。それを待たない場合は `owner-recover` で Recovery Token を発行して受け取れば、Owner の Lock も消える。
- Owner や Admin に対する誰かの総当たりで、その人が最大 1 時間ごとに Login できなくなる（DoS）ことは残る。5 回で Lock が始まる要件どおりの構造の限界で、解除・Recovery で回復する。

### 8. `redeem` の Rate Limit（Decision 0005 の条件 1）

| | 推奨 | 設定 |
| --- | --- | --- |
| 接続元ごと | 5 回の試行で Lock。60 秒、5 分、15 分、1 時間 | `PAW_REDEEM_SOURCE_FREE_ATTEMPTS`（5）、`PAW_REDEEM_BACKOFF_SECONDS` |
| 全体 | 30 回で Lock。同じ段階 | `PAW_REDEEM_GLOBAL_FREE_ATTEMPTS`（30） |
| 数え直し | 最後の試行から 15 分 | `PAW_REDEEM_DECAY_SECONDS`（900） |

- **Token を見る前、Password の Hash を計算する前**に数える。拒否された試行は Token の試行回数（Decision 0005 の Token ごとの上限）を使わない。成功も数える。
- Token は 256 bit の Secret と 128 bit の ID なので、推測で当たる見込みはない。この Limit は Token ID を知る者が正規の使用を妨げること（試行を使い切らせる）と、認証前の Request が使える DB と CPU の量を抑えるためのもの。
- 全体の Limit は、攻撃者が全体の枠を使い切って Owner の Recovery を最大 1 時間止められる（DoS）代わりに、多数の接続元からの試行を止める。Recovery はまれな操作で、Operator は復旧できる（Token を発行し直しても Rate Limit は効くので、待つ）。この取り引きの妥当性は判断点。

### 9. Audit

**認証の出来事はすべて `audit_events` に ID と列挙値だけで残す。** Password、Hash、Session ID、Token、Login name は入れない。

| `action` | 内容 |
| --- | --- |
| `auth.login` | allow `authenticated` / deny `invalid_credentials`、`account_not_active`、`no_password`、`credentials_changed`（**存在する Account のときだけ**） |
| `auth.lockout` | deny `backoff_started`（Lock を始めた失敗 1 件につき 1 行。Lock 中に拒否された試行は書かない） |
| `auth.unlock` | allow `unlocked` / deny `role_not_allowed` |
| `auth.logout`、`auth.session.revoke`、`auth.session.revoke_others`、`auth.session.revoke_all` | Session の失効 |
| `auth.password.change`、`auth.password.set` | 変更（allow `changed`）、Token による設定（`setup`、`recovery`）、失敗（deny） |
| `auth.step_up` | allow `verified` / deny `invalid_credentials`、`step_up_required` |
| `auth.policy.update` | Owner による設定の変更（12 節）。deny `role_not_allowed`、`step_up_required`、`version_conflict` |

- 変更を伴う出来事は、**変更と同じ Transaction で書く**（書けなければ変更も起きない。起きなかった変更の行も残らない）。拒否は別の短い Transaction で Best Effort に書く（書けなくても拒否のまま）。
- **存在しない名前の失敗は DB へ書かず、Log に固定の 1 行**（名前を含まない）だけ残す。誰でも作れる行になり、Audit の Table は削除できないため（PAW-025 の未認証の拒否、Decision 0005 の未知の Token と同じ方針）。
- 失敗の行は、Account の ID（`actor_id`、`actor_role` は Account の Role）と、**接続元の Bucket を表す不透明な UUID**（`resource_kind = login_source`、`resource_id`）を持つ。UUID は Bucket の Hash から作る仮名で、同じ接続元は同じ ID になる（グループにできる）が Address そのものは保存しない。Address を知る Operator は計算できる（仮名であって秘匿ではない）。
- **限界（判断点）。** `AuditEvent` に「接続元」「名前の Hash」の項目がないため、次は Audit へ入らない。存在しない名前の失敗の接続元（Log にもない）、名前の Hash。専用の項目を足すには `audit_events` の Migration と Decision 0004 の変更（新しい Decision で `Supersedes`）が要る。この提案は足さない（推奨）。足す場合は接続元 Bucket と名前の Hash の 2 列。
- Owner / Admin の異常な Login 失敗は上の行で記録される。要件の「既存の信頼済み端末への警告」は、端末の登録（PAW-024 相当）と通知の経路がないため、この Issue では実装しない。

### 10. Password の変更・Reset・Recovery

- **変更**（`POST /auth/password/change`）: 現在の Password が要る（Passkey は PAW-023）。**既定は他の端末の Session を維持**（要件）。`revoke_other_sessions: true` で他の全端末を Logout する。どちらでも、この端末の Session ID は作り直す。現在の Password を間違えると Account の Backoff に数える。
- **Reset・Recovery**: 全 Session を失効し、Password を置き換え、その Account の Login の Lock を消す。共通の部品（`AuthService` の Token を受け取る処理。将来の Admin による強制 Reset の Token も同じ経路を使える）。
- **Token による設定**（`POST /auth/token/redeem`、公開）: Token と新しい Password を受け取り、Token の消費と同じ Transaction で Password を設定し、`invited` の Owner を `active` にする。**Session は作らない**（設定した後は通常の Login をする）。`purpose` が `recovery` のとき全 Session を失効する（Decision 0005 の 7、Issue #19 の条件）。Password の Policy は Token を使う**前**に検査する（Password が悪くても Token は消費されない）。Login name を含む Password だけは Token を読んだ後にしか分からないので、その場合は全体を Rollback し Token は消費されない。
- Passkey を無効にする処理の差し込み口を用意した（`credential_invalidators`）。PAW-023 が Passkey の失効をここに足す。

### 11. Web 用 DB Role の権限（Decision 0005 の条件 2）

Migration `0022` は、Web の Role（`PAW_APP_DATABASE_ROLE`）に各 Table の実際に使う最小の権限だけを与える（`grant_app_privileges`。正確な一覧と Test は `tests/test_auth_grants.py`）。

| Table | 権限 |
| --- | --- |
| `password_credentials` | SELECT、INSERT、`hash` と `changed_at` の UPDATE。DELETE なし |
| `auth_sessions` | SELECT、INSERT、DELETE（古い行の削除）、Session の寿命で変わる列（`token_hash`、`last_used_at`、`idle_expires_at`、`stepup_*`、`rotated_at`、`revoked_*`）の UPDATE。`user_id`、`created_at`、期限の上限は変えられない |
| `auth_throttles` | SELECT、INSERT、DELETE、カウンタの列の UPDATE |
| `auth_policy` | SELECT と Policy の列の UPDATE（INSERT、DELETE なし。1 行は Migration が作る。Trigger が、変更のたびに Version をちょうど 1 上げる） |
| `auth_policy_changes` | SELECT、INSERT（Trigger が UPDATE、DELETE を拒否） |
| `users`（0021、変更なし） | SELECT と `updated_at` の UPDATE のみ |

- **`users.status` を Web の Role に UPDATE させない。** Owner が Token で Password を設定するとき `invited` → `active` にする必要があるので、Migration が `SECURITY DEFINER` の関数 `paw_activate_invited_user(user_id, now)` を作り、Web の Role に **EXECUTE だけ**を与える。この関数は `status` が `invited` の行を `active` にするだけで、`search_path` を固定し、`users` を Schema 名つきで参照する（一時 Table で差し替えられない）。侵害された Application が、削除済みの User を復活させたり、有効な User を削除したりできない。
- **守れないもの（Decision 0005 と同じ）**: Web の Role は `password_credentials` を書けなければならないので、**Application が侵害されれば Password を変えられる**。Operator の Role（Token の発行、Owner の作成）とは別のままで、Web の Role は Token を作れない（Test 済み）。防ぐ手段は PAW-023 の Step-up。
- 代案: `users.status` の列の UPDATE を与え、Trigger で `invited` → `active` 以外を拒否する。今後の User Lifecycle（削除待ちへの遷移など）を同じ Trigger が邪魔するので採らない。

### 12. Passkey Policy を Owner が変えられる設定にする

**この節は、`REQUIREMENTS.md` の `[FIXED]`「Passkey Policy」（Owner 必須、Admin 必須、User 任意で強く推奨、Owner / Admin の重要操作は直近 30 分の Step-up）を、固定の方針から「Owner が変えられる Workspace の設定」の既定値に変える提案である。** Human の指示（「パスキーに関してもオーナー自身が変更できるようにしましょう、設定で」）による。

- **既定値は要件のまま**: Owner = required、Admin = required、User = optional、User へ Passkey を強く勧める = 有効、Step-up の有効時間 = 30 分。
- **保存**: `auth_policy`（1 行、`version` つき）と `auth_policy_changes`（変更のたびに 1 行。誰が、いつ、各項目の変更前と変更後。追記専用）。変更は Audit（`auth.policy.update`）にも 1 行。
- **誰が**: **変更は Owner だけ**（新しい Capability `owner.auth_policy.manage`。Owner 専用で、Agent に委任できず、Audit は REQUIRED）。**閲覧は Admin と Owner**（`admin.auth_policy.view`）。API は `GET` / `PUT /api/v1/auth/policy`。
- **検証**: 各項目は厳密な値だけ（`required` / `optional`、真偽値、5〜240 分の整数。範囲外や型違いは拒否）。更新は完全な置き換えで、Client が読んだ `expected_version` を送る。Row Lock の下で Version が違えば 409（`version_conflict`）で拒否し、**同時の 2 つの編集で更新が失われない**。同じ値の更新は Version を上げない。
- **Owner の再認証は Passkey の Step-up**: Policy の変更には、Owner 自身の Session の直近の **Passkey の Step-up**（Policy の Step-up 有効時間の内。判定は Row Lock の下で Database の時計）が要る。要件（`[FIXED]`: Owner / Admin の重要操作は Passkey の Step-up）のとおりで、**Password の Step-up は数えない**（403 `step_up_method_insufficient`、Audit は deny `step_up_method_insufficient`）。Password を盗んだ者は `POST /auth/step-up` で Password の Step-up を得られるので、それを受け付けると、盗まれた Password だけで Owner / Admin の Passkey の要求を `optional` に緩められてしまう（Review 指摘）。Step-up が全くないときは従来どおり 403 `step_up_required`。Step-up の方法は、その方法の Verifier だけが記録する（別の方法の鍵で登録した Verifier は `AuthService` が拒否する。Password の Step-up を Passkey として記録することはできない。後の Password の Step-up は直前の Passkey の Step-up を置き換える。強さは上がらず下がるだけ）。
- **Owner が変えられる Policy は、PAW-023 が入るまで本番では変更できない**（この節の提案は、PAW-023 で Passkey の Step-up ができて初めて端から端まで動く）。その間、`GET /policy` と要求の報告は動き、既定値（要件のまま）が効く。`PUT /policy` は、Test が Passkey の Step-up を書いた Session でだけ成功する（Passkey の Step-up を書けるのは、その方法の Verifier を登録した Code、または DB の直接の書き込みだけ）。Web の Role は `stepup_method` を書けるので、**Application が侵害されれば `passkey` と書ける**（Password を変えられるのと同じ、Decision 0005 で受け入れた限界）。
- **効く範囲**: **新しい Sign-in・新しい Session から**。**変更は既存の Session を失効も降格もしない**（厳しくしても黙って Logout されない。Test 済み）。Session の応答（`GET /auth/session`）が、その人の現在の要求（`required` か `optional`、未登録なら `enrollment_required`、勧めるか）を返す。
- **Owner を締め出さない（安全側の規則）**: この Issue は Passkey を**強制しない**（PAW-023 が強制する）ので、どの設定でも Owner は Password で Login できる。PAW-023 は「`required` で未登録」を**登録だけができる状態**（行き止まりではない）にし、Password Login と `owner-recover` を残さなければならない（Decision 0005 の 7 の「Passkey の登録以外を許さない」と同じ）。
- **`users.passkey_required` 列（0021）との関係**: この列は Owner / Admin では CHECK 制約で `false` にできない。設定が `optional` でも列は `true` のままで、**Login の処理は列を見ず、設定（`auth_policy`）を見る**。列を設定へ合わせる（CHECK を外す、または列を捨てる）かは PAW-023 で決める。
- **Step-up の有効時間**: 列と設定の項目を持ち、Owner が 5〜240 分で変えられる。この Issue が使うのは Policy 変更の判定だけ。Owner / Admin の他の重要操作への適用は PAW-023 と、各操作の Issue。
- **リスク**: (1) 緩める（Owner / Admin を `optional` にする）と、Password だけの侵害で Owner / Admin を乗っ取れる。要件が Owner / Admin の Passkey を必須にした理由を弱める。(2) Owner 自身を `required` にして Passkey を失うと締め出される恐れ（上の安全側の規則と `owner-recover` で戻れる）。(3) Owner の Session が Passkey の Step-up つきで奪われると Policy を緩められる（Passkey の Step-up を要求することで下げている。Password だけでは緩められない）。(4) PAW-023 が入るまで Policy を変えられない（上）。
- **却下した案**: 固定の方針のまま（要件どおり。Owner が運用に合わせて変えられない）。User ごとの上書き（Owner が個別の User に Passkey を必須または免除する。要件になく、権限の面が増える。将来 Decision で足せる）。設定をコードの定数にする（変更に再起動と Deploy が要り、変更の履歴が残らない）。

### 13. Step-up の差し込み口（PAW-023 へ）

- `StepUpVerifier`（`method`、`verify(user_id, login_name, evidence)`）と `StepUpEvidence` を用意した。Password の実装（`PasswordStepUpVerifier`）が入っている。PAW-023 は Passkey の Verifier を `AuthService(step_up_verifiers=...)` に登録するだけでよい。
- Step-up は `POST /auth/step-up` で、成功すると Session に時刻と方法（`stepup_at`、`stepup_method`）を記録し、**Session ID を作り直す**。Session の応答に `auth.step_up`（方法、時刻、有効な期限、満たしているか）を含める。誤りは Account の Backoff に数える。`satisfied` は「時間内に Step-up があった」だけを表し、**方法の強さは `method` で判断する**（何を許すかを決めるのは Backend で、Client の表示ではない。Policy の変更は Passkey だけを許す）。
- 「Passkey の登録の有無」は `PasskeyEnrollment`（既定は誰も登録していない）を通して尋ねる。PAW-023 が実装を差し込む。

### 14. この Issue に含めないこと

- **Passkey の登録・認証・強制**（PAW-023）。
- **複数端末の追加（QR / Link の Pairing、新規端末の Owner / Admin 承認）**。要件にあるが、この Issue の受け入れ条件にない。Session の端末単位の管理（一覧、個別の Logout、他の全端末の Logout）は含む。
- **Admin による強制 Reset の Token の発行**（1 回限りの再設定フロー）。Token を受け取る共通の部品はあるが、Owner 以外の Token の発行は User 管理の Issue。
- **`/api/v1/events` の 2 つの Endpoint の認証**。Session はできたが、まだ System Event しか流さないため、非公開の Event を足す Issue で `require_capability` を付ける（`tests/test_authz_routes.py` の公開一覧に理由つきで残している）。
- Owner / Admin の異常な失敗を、信頼済みの端末へ警告する処理。

## 選定理由

- 数値は、1 台の GPU Server で個人〜小規模 Team が使う前提で、OWASP / RFC 9106 / NIST SP 800-63B の一般的な推奨に合わせた暫定値。実測に基づく調整は運用で行い、設定で変えられる。
- Backoff を「予約してから比べる」形にしたのは、同時 Request が Lock の成立より前に大量の Password を比べるのを防ぐため（Decision 0005 が Token の試行で採った方式と同じ）。
- Session を Server 側に持ち、ID の Hash だけを保存するのは、要件（Server-side Session）と、DB の読み取り権限が Session の乗っ取りにならないようにするため。
- 存在しない名前も同じに扱うのは、User の存在が分かると、標的を絞った総当たりと Phishing がしやすくなるため。

## 代案

- Argon2id を OWASP の最小構成（19 MiB、t=2、p=1）にする: 軽いが余裕が小さい。設定で選べる。
- Password の最大長を無くす: 認証前の Request が使える計算が増える。
- 通常 Session に絶対の上限を置かない: 使い続ける限り再認証がない。置いた場合の不便は、90 日ごとの再 Login。
- Cookie を `SameSite=Lax` にする: 外部 Site からの遷移（Link）でも Cookie が付く利点。CSRF の 2 層目が弱くなる。設定で選べる。
- CSRF Token を発行する: 状態を持つ Token が要る。Origin の検査と `SameSite=Strict` で足りると判断。
- Rate Limit を Application の Memory に持つ: 再起動と複数 Process で失われる。DB に持つ。
- Login の失敗の行を、存在しない名前でも書く: 誰でも消せない行を増やせる。書かない。
- Session ID を Hash せずに保存する: DB の読み取りだけで Session を奪える。

## 人間の判断が必要な点（推奨つき）

1. **Session の寿命の解釈（4 節）**: 「最大 90 日」= 無操作と絶対の両方の上限 90 日（推奨）／無操作 90 日で絶対の上限なし。通常 Session の絶対の上限 90 日（推奨）／なし。
2. **Argon2id（1 節）**: 64 MiB、t=3、p=4、同時 2（推奨）／OWASP の最小構成。
3. **Password の Policy（2 節）**: 最大 256 文字、短い一覧、Login name の規則（推奨）／一覧なし。流出 Corpus（外部 API）は入れない（推奨）。
4. **Cookie（3 節）**: `SameSite=Strict`（推奨）／`Lax`。通常 Session は Session Cookie（推奨）／永続 Cookie。
5. **Backoff（7 節）**: Account 5 回、接続元 20 回、30 秒〜1 時間、24 時間で忘れる（推奨）。Lock の上限 1 時間（推奨）／もっと短い、または長い。
6. **`redeem` の Rate Limit（8 節）**: 接続元 5 回、全体 30 回（推奨）。全体の Limit による Recovery の DoS を受け入れる（推奨）／全体の Limit なし。
7. **Audit（9 節）**: 存在しない名前の失敗は Audit へ書かない、接続元は仮名の UUID（推奨）／Audit に「接続元」「名前の Hash」の列を足す（Decision 0004 を Supersede する）。
8. **Rotation（5 節）**: 猶予期間なし（推奨）／10 秒程度の猶予。
9. **DB Role（11 節）**: `SECURITY DEFINER` の関数（推奨）／`users.status` の列の UPDATE と Trigger。
10. **Passkey Policy を Owner が変えられる設定にする（12 節）**: 設定にする（推奨、Human の指示）／固定の方針のまま／User ごとの上書きも足す。Step-up の有効時間を Owner が 5〜240 分で変えられる（推奨）／30 分に固定。設定を緩めることを Owner だけに許す（推奨）。**変更に Passkey の Step-up を要求し、PAW-023 が入るまで本番では変更できない**（推奨。要件に沿う）／Password の Step-up でも変更できる（Password の侵害だけで Owner / Admin の Passkey の要求を緩められる）。
11. **`REQUIREMENTS.md` の記述**: 承認後に、`[FIXED]`「Passkey Policy」を「既定値であり、Owner が設定で変えられる」と直すか（推奨）、この Decision の参照だけを足すか。

## リスク

- 数値は暫定で、運用で見直す前提。
- Backoff と Rate Limit は、Owner / Admin への総当たりと全体の枠の消費で、一時的な DoS を起こせる。永久ではなく、解除と Recovery で戻れる（7、8 節）。
- Password の Hash 計算は 1 件あたり数十 ms・64 MiB を使う。認証前の Request が使える量は、接続元と全体の Rate Limit と、同時に計算する数の上限（`PAW_PASSWORD_HASH_CONCURRENCY`、待つ Job の上限）で抑える。DB へ届く前に拒否される Request もある。
- Login の失敗と成功で、Audit の書き込みの分だけ時間がわずかに違う（Argon2 の約数十 ms に対して数 ms）。存在しない名前と存在する名前を完全には同じ時間にしていない。
- 各認証の呼び出しは、その都度 DB へ新しい接続を作る（Pool を使わない。停止した DB に対して確実に失敗させるため）。Login 1 回で 2〜3 本、認証済みの Request ごとに 1 本。負荷が高くなれば、この方式の見直しが要る。
- Passkey の Policy を緩めると、要件が守ろうとした安全性が下がる（12 節）。

## 承認後の扱い

承認されたら、この Decision の Status を Approved にする（Status と承認の記録だけを更新する。既存の Decision の本文の方針を書き換えるときは新しい Decision から `Supersedes` する）。PAW-022 の PR は本 Decision を参照する。
値を変えるときは、この Decision を書き換えず新しい Decision から `Supersedes` し、設定の既定値（`config.py`）と Test の期待値を合わせる（Migration は不要）。
12 節が承認された場合は、PAW-023 の受け入れ条件に「Owner が設定で変える Policy に従って強制する」「`required` で未登録の状態を登録だけができる状態にし、行き止まりにしない」「Passkey の `StepUpVerifier` を `AuthService(step_up_verifiers={AuthMethod.PASSKEY: ...})` に登録して、Policy の変更（`PUT /auth/policy`）を使えるようにする」「Owner / Admin の他の重要操作（Admin による Lock の解除など）にも Passkey の Step-up を要求する」を加える。

## 決めてほしいこと

1. 4 節の Session の寿命の 2 つの解釈（無操作と絶対の上限、通常 Session の絶対の上限）。
2. 1〜3、5、7〜9 節の数値と選択を、推奨どおりにしてよいか。
3. 12 節: Passkey Policy を Owner が変えられる設定にすること、Owner 専用の変更、Passkey の Step-up の再認証（PAW-023 が入るまで本番では変更できないこと）、既存 Session に影響しないこと。
4. 12 節: `users.passkey_required` の扱い（設定を見る。列の整理は PAW-023）。
5. 9 節: Audit に接続元と名前の Hash の列を足すか（足す場合は Decision 0004 の変更）。
6. 8 節: 全体の Rate Limit と、Recovery の DoS の許容。
7. 11 節: `REQUIREMENTS.md` をどう直すか。
