# 他の Account の Passkey の Reset と 1 回限りの Password 再設定の方針

- Status: Approved
- Date: 2026-09-28
- Scope: Issue [#108](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/108)（Owner による他 Account の Passkey の Reset。PAW-023 の後続）。[Decision 0025](0025-passkey-webauthn-policy.md) の 7 節・「リスク」・「後続の Issue」、[Decision 0015](0015-login-session-password-policy.md) の 14 節（Admin による強制 Reset の Token の発行）、[Decision 0005](0005-owner-setup-and-recovery.md)（Web の Role は Token の行を INSERT できない）との接続点
- Supersedes: なし（0025・0015・0005 を書き換えない。0025 の「後続の Issue」の受け入れ条件（案）で「Issue で決める」とされた点と、0015 の 14 節が別の Issue とした Token の発行を、ここで提案する）
- Approval: 2026-09-28、Human（Repository の Owner）が作業 Session 内で、判断点ごとの説明（推奨つき）を受けたうえで「推奨どおり」と回答して承認（末尾の「承認時の決定」）

## 背景

Decision 0025（承認済み）では、要求が `required` の Admin が Passkey の Device をすべて失うと、Password で Sign-in しても「認証だけができる」Session（`assertion_required`）から出られず、Owner の介入手段もない（行き止まり）。0025 はこれを「後続の Issue」として起票し（#108）、次を「Issue で決める」とした。

1. Admin が User の Passkey を Reset できるか（推奨は Lock の解除と同じ規則）。
2. Password の扱い（Passkey だけを Reset すると、最初の 1 つの登録が Password の信頼に戻る。既定は 0015 の 14 節の 1 回限りの Password の再設定を必須にする）。

REQUIREMENTS.md（`[FIXED]`）は次を定める。

- 「Passwordリセット、アカウント侵害対応、Owner/Adminによる強制リセット、Owner Recoveryでは、原則として既存Sessionを全失効させる。」
- 「Owner/Adminが他UserのPassword本文を閲覧することはできない。必要な場合は1回限りのPassword再設定フローを発行する。」
- 「Owner/Adminの重要操作では、直近30分以内のPasskeyによるStep-up Authenticationを要求する。」
- Owner / Admin の役割分離: Admin は「User管理」、Owner だけが「Adminの追加/削除」「全体復旧/非常時設定」。

要件は、Reset の Token をどう作り、どう渡し、誰が誰に使えるかを定めていない。実装（#108）は動かすためにこれらへ選択を置いたので、各点に推奨を付けて集め、Human が 1 回で承認または変更できるようにする。**実装は各点の推奨どおり**で、承認で変わっても Schema（Migration `0108`）の変更は小さい（下の「承認後の扱い」）。

## 提案

### 1. 誰が誰を Reset できるか

**推奨: Owner は Admin と User を、Admin は User だけを Reset できる（Lock の解除と同じ規則）。Owner と自分自身は対象にしない。**

- Capability は既存の `admin.users.manage`（Owner と Admin。Agent に委任できない）を使い、Role の規則（Admin は User だけ）は Service が User の行を Lock した後に判定する（Lock の解除 `POST /auth/users/{id}/unlock` と同じ形）。Endpoint は `POST /api/v1/auth/users/{id}/passkeys/reset`。
- Owner を対象にしない: Owner は `owner-recover`（Ubuntu の sudo 経由。0005）で戻る。Web の経路から Owner の Credential を消せると、最後の Owner を締め出せる。
- 要件の「Admin: User管理」「Owner/Adminによる強制リセット」に合う。Admin を対象にできるのは Owner だけ（「Adminの追加/削除」は Owner だけ、に揃える）。

**代案**: (a) Owner だけ（新しい Capability `owner.users.passkey_reset`）。Admin の権限が小さくなるが、User の復旧のたびに Owner が要る。(b) Admin が Admin も Reset できる。Admin 同士で乗っ取れるため採らない。

### 2. Password の扱い

**推奨: Passkey の Reset と同時に、対象の Password を消し、1 回限りの Password 再設定 Token を発行する（必須。Passkey だけの Reset はしない）。**

- 古い Password は Reset の時点で使えなくなる。Password を盗んだ者が、Reset の後に先に自分の Passkey を登録すること（0025 の 5 節の限界）を防ぐ。
- Token は操作した Owner / Admin への応答で **1 回だけ**返し（`Cache-Control: no-store`）、保存するのは Salt 付きの HMAC だけ。操作した人が別の経路で対象の人に渡し、対象の人が `POST /auth/token/redeem` で**自分で新しい Password を決める**（Owner / Admin は Password を見ることも決めることもできない。要件どおり）。
- 受け取ると、全 Session の失効（Reset の時点で済み）、Login の Lock の解除、`invited` の Account の `active` への変更を、Token の消費と同じ Transaction で行う（Owner の Token と同じ処理）。その後の Sign-in は、要求が `required` の Role なら `enrollment_required` の Session になる。

**代案**: (a) Passkey だけを Reset し、Password は残す（実装は小さいが、Password を盗まれて Device を失った場合の復旧にならない）。(b) Password を消すだけで Token を出さない（対象が戻れない行き止まり）。(c) 一時 Password を Owner / Admin が決める（要件の「Password本文を閲覧できない」に反する）。

### 3. Token の仕組み（Decision 0005 の境界を保つ）

**推奨: Owner の Setup / Recovery の Token と同じ Table（`setup_tokens`）・同じ形式（`pawst1.<id>.<secret>`）・同じ受け取り（`POST /auth/token/redeem`、試行回数の上限、Rate Limit）を使い、新しい `purpose = password_reset` を足す。Token の行は `SECURITY DEFINER` の関数 `paw_issue_password_reset_token` だけが作る。**

- 0005 は「Token の行を INSERT できる者は Owner の Account を取れる」ため、Web の Role に `setup_tokens` の INSERT を与えていない。**この境界は変えない**（Web の Role は今も INSERT できない）。関数は **Owner と、`invited` / `active` でない Account を拒否し**（何も変えずに `false`）、同じ処理の中で対象の未使用の Token を失効し、Password を消し、新しい Token を作る。有効期間は 72 時間までに制限する（`created_at` から数えるため、`created_at` 自体も Database の `clock_timestamp()` の前後 5 分以内に限る。そうしないと呼び出し側が `created_at` を未来にして期間を延ばせる）。`search_path` は `pg_catalog, pg_temp` に固定し、EXECUTE は Web の Role だけに与える（PUBLIC から取り消す）。
- 受け取る側（`TokenRedeemer`）は、Setup / Recovery の Token は Owner だけ、`password_reset` の Token は Admin と User だけに使わせる（役割は受け取るときに User の行の Lock の下で見直す。Token の発行の後に Owner が移譲された場合も拒否する）。
- 0015 の 10 節は「将来の Admin による強制 Reset の Token も同じ経路を使える」としていた。これに沿う。

**代案**: 別の Table（`password_reset_tokens`）を作る（Owner の Token と完全に分かれるが、試行回数・不変性の Trigger・「未使用は 1 つ」の Index・受け取りの処理を複製することになる）。

### 4. 数値

**推奨: Token の有効期間は既定 24 時間（`PAW_PASSWORD_RESET_TOKEN_TTL_SECONDS`、600〜259200 秒）。試行回数は Owner の Token と同じ `PAW_SETUP_TOKEN_MAX_ATTEMPTS`（既定 5）。**

- Owner の Token（既定 30 分、上限 4 時間）より長いのは、操作した人が別の経路で対象の人に渡す時間が要るため。Token は 256 bit の乱数で、試行は 5 回まで、Account は受け取るまで Sign-in できない（古い Password は消えている）。
- 代案: 30 分〜4 時間（Owner の Token と同じ）。短いほど漏れた Token の危険は減るが、渡し損ねると Reset のやり直しになる（やり直すと前の Token は失効する）。

### 5. Step-up と順序

- 操作した人の Session に、Policy の有効時間内の **Passkey の** Step-up を要求する（0025 の 6 節の「Owner・Admin の重要操作」。Password の Step-up は数えない）。**対象を調べる前に判定する**（Step-up のない Session に、Account の存在を教えない）。
- 1 つの Transaction で、対象の `users` の行を `FOR UPDATE` で Lock し、全 Passkey を失効（`revoked_reason = admin_reset`）、開いている Challenge を削除、全 Session を失効（`revoked_reason = admin`）、Password の削除と Token の発行、Audit を行う。**Passkey の行を Session の行より先に Lock する**（0025 の 15 節。対象の Step-up と Deadlock にならない。Lock を保持して待つ Test が、順序を逆にすると失敗することを確かめている）。対象の Sign-in・登録とは User の行の Lock で順番になる。

### 6. Audit

`audit_events` に ID と列挙値だけで残す（Token、その ID、Login name は入らない）。

| `action` | 内容 |
| --- | --- |
| `auth.passkey.reset` | allow `reset` / deny `step_up_required`、`step_up_method_insufficient`、`role_not_allowed`、`not_found`。操作した人の ID と Role、対象の User の ID |
| `user.password_reset_token.redeem` | Token の受け取り（理由の列挙値は `owner.token.redeem` と同じ）。対象の人の ID と Role |
| `auth.password.set` | 新しい理由 `password_reset`（対象の人の ID と Role） |

変更は同じ Transaction で、拒否は別の短い Transaction で Best Effort に書く（0015 の 9 節と同じ）。

### 7. Migration と権限

Migration `0108`（`down_revision` は `0041`）: `user_passkeys.revoked_reason` の CHECK に `admin_reset`、`setup_tokens.purpose` の CHECK に `password_reset` を加え、関数を作る。Table の権限は変えない。`downgrade()` は関数を消し、`password_reset` の Token を削除し（受け取る前なら、その User は Password も Token もない状態になる。Owner / Operator が作り直す）、`admin_reset` を `recovery` に付け替える。

## 選定理由

- Lock の解除と同じ規則・同じ Capability にすると、Owner / Admin の権限の表が増えず、要件の役割分離（Admin は User 管理、Admin の管理は Owner）に揃う。
- Password を同時に消して Token を出す形は、要件の「1回限りのPassword再設定フロー」「Password本文を閲覧できない」「強制リセットでは全 Session を失効」を同時に満たし、Passkey だけの Reset の弱点（最初の 1 つが Password の信頼に戻る）を塞ぐ。
- 既存の Token の仕組みを使うと、試行回数・不変性・受け取りの Transaction・Audit が、すでに Test された 1 つの経路にまとまる。`SECURITY DEFINER` の関数で Owner を拒否するので、0005 の境界（Web の Role は Owner の Token を作れない）は保たれる。

## 人間の判断が必要な点（推奨つき）

1. **誰が誰を**: Owner は Admin と User、Admin は User だけ（推奨。Lock の解除と同じ。`admin.users.manage`）／Owner だけ（新しい Capability）。
2. **Password**: Reset と同時に Password を消し、1 回限りの再設定 Token を必須にする（推奨）／Passkey だけを Reset する。
3. **Token の仕組み**: `setup_tokens` に `password_reset` を足し、Owner を拒否する `SECURITY DEFINER` の関数だけが作る（推奨）／別の Table。
4. **有効期間**: 既定 24 時間（600〜259200 秒の設定、関数の上限 72 時間。推奨）／Owner の Token と同じ 30 分（上限 4 時間）。
5. **Token の渡し方**: 操作した人への応答で 1 回だけ返し、別の経路で渡す（推奨。Mail などの配送の仕組みはまだない）／配送の仕組みを先に作る。

## リスク

- **Token を受け取った Owner / Admin は、その Token を自分で使って対象の Account を乗っ取れる**（Admin は User の、Owner は Admin と User の）。Audit の `auth.passkey.reset`（操作した人）と Token の受け取り（対象）の行が手掛かり。要件の「Owner/Adminによる強制リセット」に内在する権限で、配送の経路（本人だけが受け取れる Mail など）ができるまで残る。
- Token は HTTP の応答に 1 回だけ現れる（`no-store`）。Reverse Proxy や Browser の拡張が応答を記録すると漏れる。
- 対象の Account は、Token を受け取るまで Sign-in できない（古い Password は消えている）。Token を失ったら Reset をやり直す（前の Token は失効する）。
- Application が侵害されれば、Admin・User の Token を作れる（関数は Owner を拒否する）。Password を書ける Web の Role の限界（0005 が受け入れたもの）と同じ種類。
- 実際の Browser、Reverse Proxy、TLS を通した動作は確かめていない（`TestClient` と実 PostgreSQL まで）。

## 承認後の扱い

- 承認されたら `Approval` と末尾の「承認時の決定」に記録し、Status を Approved に改める（Agent は自分で Approved にしない）。
- 推奨と違う選択になった場合: 1 の「Owner だけ」は Capability と Route の依存の変更（Migration 不要）。2 の「Passkey だけ」は Service から Token の発行を外す（関数と `password_reset` は残してよい）。3 の「別の Table」は新しい Migration。4 の数値は設定の既定値と関数の上限（関数の上限を変えるなら新しい Migration）。
- 既存の Decision（0005・0015・0025）は書き換えない。

## 承認時の決定（2026-09-28）

Human は、作業 Session で上の「人間の判断が必要な点」5 点について推奨つきの説明を受け、「推奨どおり」と回答して承認した（5 点を一括で。個別の変更はない）。**5 点すべてが推奨どおり**である。

1. **誰が誰を**: Owner は Admin と User、Admin は User だけ（`admin.users.manage`）。
2. **Password**: Reset と同時に Password を消し、1 回限りの再設定 Token を必須にする。
3. **Token の仕組み**: `setup_tokens` に `password_reset` を足し、Owner を拒否する `SECURITY DEFINER` の関数だけが作る。
4. **有効期間**: 既定 24 時間（600〜259200 秒の設定、関数の上限 72 時間）。
5. **Token の渡し方**: 操作した人への応答で 1 回だけ返し、別の経路で渡す。

承認後に方針を変える場合は、この Decision を書き換えず、新しい Decision から `Supersedes` する。
