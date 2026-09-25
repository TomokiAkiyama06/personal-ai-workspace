# Passkey（WebAuthn）と Step-up の方針

- Status: Approved
- Date: 2026-09-26
- Scope: PAW-023（Passkey / Step-up Authentication、Issue [#20](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/20)）。[Decision 0015](0015-login-session-password-policy.md) の 12 節・13 節・「承認時の決定」が PAW-023 に残した点、[Decision 0005](0005-owner-setup-and-recovery.md) の 7 節（Recovery で Passkey を無効にする）、[Decision 0006](0006-tool-broker-policy.md)（強い承認の Step-up）との接続点
- Supersedes: なし。Decision 0015 は書き換えない（0015 の 12 節が「PAW-023 で決める」とした `users.passkey_required` 列の扱いは、この Decision の 9 節で決める）
- Approval: 2026-09-26、Humanが作業Session内の質問Toolで、この Decision を「推奨どおり」として承認（末尾の「承認時の決定」）

## 背景

[REQUIREMENTS.md](../../REQUIREMENTS.md) は Passkey について次を定める（`[FIXED]`。Decision 0015 の 12 節が「Owner が変えられる設定の既定値」へ読み替え済み）。

- Owner・Admin は Passkey 必須、User は任意（UI で強く推奨）。
- Owner・Admin の重要操作は、直近 30 分以内の Passkey による Step-up を要求する（Owner が 5〜240 分に変えられる）。
- 全 Passkey・端末を失った Owner は、Ubuntu の sudo 経由の Recovery で復旧できる。

Decision 0015（承認済み）は、PAW-023 の受け入れ条件に次を加えた（Issue #20 に追記済み）。

1. Owner が設定で変える Policy に従って Passkey を強制する。
2. `required` で未登録の状態を、Passkey の登録だけができる状態にする。行き止まりにしない（Password の Login と `owner-recover` を残す）。
3. Passkey の `StepUpVerifier` を `AuthService(step_up_verifiers={AuthMethod.PASSKEY: ...})` に登録し、`PUT /auth/policy` を使えるようにする（Password の Step-up は数えない）。
4. Owner・Admin の他の重要操作（Admin による Lock の解除など）に Passkey の Step-up を要求し、Tool Broker の強い承認の `StepUpVerifier`（`tools/approvals.py`）にも結び付ける。
5. `users.passkey_required` 列（Migration 0021）と `auth_policy` の関係を整理する。

さらに Decision 0005 の 7 節は、Recovery の Token を受け取るとき、既存の Passkey をすべて失効させることを求める。Issue #20 の元の受け入れ条件は、Owner・Admin の Passkey 必須、User は任意、重要操作の 30 分の Step-up、複数の Passkey の登録、端末（Device）の Revoke である。

要件は次を定めていない。実装（PAW-023）は動かすためにこれらへ値と選択を置いたので、**その全部を 1 か所に集め、各点に推奨を付けて**、Human が 1 回で承認または変更できるようにする。

- WebAuthn の Library。Attestation・User Verification・Resident Key・署名 Counter の扱い。
- 「Passkey 必須」を Login のどの段階でどう強制するか。Passkey の設定がない環境の扱い。
- 重要操作の一覧。Passkey を失効するときに何が終わるか。
- `users.passkey_required` 列の扱い。

実装は [Backend README](../../apps/backend/README.md) の「Passkey / Step-up」に書いている。数値（Challenge の有効時間、Passkey の上限）は設定または定数で、承認で変わっても Schema は変わらない。

**実機の検証はしていない。** Browser・実際の Authenticator（Touch ID、Windows Hello、Security Key）で登録・認証したことはない。Test は、W3C の仕様書から書いた Software Authenticator（`cryptography` で実際に署名し、CBOR・Authenticator Data を自前で組み立てる。検証する Library と Code を共有しない）で、実際の WebAuthn Payload を作って通している。

## 提案

### 1. WebAuthn の Library

**`webauthn`（py_webauthn、Duo Labs）3.0.1 を完全一致で固定する。** `cryptography` も Test で直接 import するため、`cryptography==50.0.1` を固定する。固定は `apps/backend/pyproject.toml`、`.pre-commit-config.yaml` の `additional_dependencies`、`.github/requirements-ci.txt` の 3 か所（既存の規則。`.github/scripts/test_dependency_pins.py` が一致を検査する。Import 名と Package 名が同じなので、Import 名の対応表への追加は要らない）。

最新の情報（2026-09-26 に PyPI と GitHub から取得。Freshness が要る判断のため、古い知識では決めていない）:

| 項目 | `webauthn`（py_webauthn） | `fido2`（Yubico python-fido2） |
| --- | --- | --- |
| 最新 Version（公開日） | 3.0.1（2026-09-25）。3.0.0 は 2026-06-29、2.8.0 は 2026-06-13 | 2.2.1（2026-06-29） |
| 既知の脆弱性 | PyPI、GitHub Advisory Database とも 0 件 | 同、0 件 |
| License / 状態 | BSD-3-Clause。Archive されていない。Issue 0 件。最終 Push は 2026-09-25 | BSD-2-Clause。Archive されていない。最終 Push は 2026-06-29 |
| 依存 | `pyasn1`、`pyasn1-modules`、`cbor2>=6.1.2`、`cryptography>=49`、`pyOpenSSL>=26.3` | `cryptography`（`<52`）だけ |
| 用途 | Relying Party（Server）側の検証に特化した関数（`verify_registration_response`、`verify_authentication_response`）。期待する Origin（複数可）、RP ID、User Verification の要求、Challenge を引数で受ける | Client と Server の両方の Library。Server 側の Helper（`Fido2Server`）は、Origin の検証を Callback で受ける |

`cryptography` の Advisory は 50.0.0 未満に PKCS#7 の Bleichenbacher Oracle（この Backend は使わない）などがあり、最新の 50.0.1 に固定した。

`webauthn` を選ぶ理由: (1) Server 側の検証に絞った単純な API で、Origin の完全一致（複数）、RP ID、User Verification、Challenge を引数で渡せる。(2) 3.0.0 / 3.0.1 は不正な入力の扱い（同じ Key を 2 回含む CBOR の拒否、`fmt` が文字列でない Attestation の拒否）を強化していて、保守が続いている。(3) 暗号の検証を自前で書かない（自前の実装は、署名の形式・COSE Key・Authenticator Data の解析の誤りが Passkey の突破になるため採らない）。

**採らなかった案**: `fido2`（依存が小さい利点があるが、Server 側の検証を Origin の Callback と自前の Flag 検査で組み立てる部分が多く、`webauthn` の方が検証の責務が明確）。自前の実装（上記）。

**注意（リスク）**: 3.0.1 は固定した日の前日の公開で、実運用の時間が短い。3.0.0（3 か月前）へ下げる選択もあるが、3.0.1 は Attestation の `fmt` の型の検査の修正だけの差で、この Backend は `fmt` を自前で先に検査するため、3.0.0 でも同じ結果になる（判断点 1）。

**Library の呼び出しは 1 つの Module（`auth/passkeys/ceremony.py`）に閉じる。** Library が扱わない、または緩い点は、その Module が補う。

- Attestation は `none` だけを受け付ける（3 節）。Library に渡す前に、CBOR を厳密に解析し、Member が `fmt`・`attStmt`・`authData` の 3 つだけで、`fmt` が `none`、`attStmt` が空であることを確かめる（Library は `none` の `attStmt` にある未知の Member を無視する）。
- `clientDataJSON` の `crossOrigin: true` と `topOrigin` を拒否する（Cross-origin の Frame からの利用）。
- 署名 Counter の判定は Library に任せない（4 節）。
- 登録された User Handle と応答の User Handle の一致を検査する。
- Browser が送る値の型・文字集合・大きさを、Library より前に厳密に検査する（Base64url は正準形だけ。Byte 数の上限は Credential ID 1023、Client Data 2,048、Attestation Object 4,096、Authenticator Data 1,024、署名 1,024、User Handle 64）。Library へは、検査した値から作り直した Dictionary だけを渡す。
- Library の例外の文（Client の Origin と Challenge を含む）は、返しも Log もしない。固定の 1 行だけを残す。

### 2. 設定と、未設定のときの扱い

新しい設定（`PAW_` の環境変数）:

| 変数 | 既定 | 内容 |
| --- | --- | --- |
| `PAW_PASSKEY_RP_ID` | なし | WebAuthn の Relying Party ID（公開 Host 名の登録可能な Domain、または `localhost`。IP は不可） |
| `PAW_PASSKEY_ORIGINS` | 空 | Browser が Ceremony を実行してよい Origin（Comma 区切り、最大 8。`https://host[:port]`、`localhost` だけ `http://` も可。Scheme・Host・Port の完全一致。Host は RP ID か、その Sub-domain） |
| `PAW_PASSKEY_RP_NAME` | `Personal AI Workspace` | Authenticator に表示する名前 |
| `PAW_PASSKEY_CHALLENGE_TTL_SECONDS` | `300` | Challenge に答えられる秒数（30〜900） |

- `PAW_PASSKEY_RP_ID` と `PAW_PASSKEY_ORIGINS` は**両方を設定するか、両方を設定しない**（起動時に検査。値は Error に含めない）。WebAuthn の Origin の検査は、この設定との完全一致で行う。`PAW_ALLOWED_ORIGINS`（CSRF と WebSocket の許可）とは別の設定にした（CSRF で許した Origin が、そのまま Passkey を使える Origin になるのを避けるため）。
- **未設定のときは Passkey の機能を切る。** 登録も認証もできず、したがって `required` の要求も**強制しない**（Passkey を登録できない環境で強制すると、Owner が「登録だけができる状態」から出られない行き止まりになる。0015 の 12 節の「Owner を締め出さない」に反する）。起動時に警告を 1 度出し、Session の応答は `passkey.available: false` を返す。この状態では、Passkey の Step-up が要る操作（Policy の変更、Account の Lock の解除）は使えない（Fail Closed）。

## 提案（続き）

### 3. Ceremony の方針

| 項目 | 選択 | 理由 |
| --- | --- | --- |
| User Verification | **required**（登録・認証の両方で、応答の UV Flag を検査する） | Passkey を「持っている」だけでなく、端末での本人確認（生体・PIN）まで証明させる。盗まれたロック解除済みの端末だけでは足りない |
| Attestation | **none**（`none` 以外の応答は拒否） | どの種類の Authenticator かを検証しない。信頼する Root の一覧を保守しなくてよく、User の Privacy を守る。W3C の仕様は、Client が `none` の要求に `none` で答える（匿名化する）ことを求める。`none` 以外を拒否すると、X.509・TPM・Android の解析の経路にも入力が届かない |
| Resident Key（Discoverable） | **preferred**（必須にしない） | Passkey だけの Sign-in（Username なし）はしない（13 節）。Credential は Sign-in した User で引くので、Discoverable でなくてもよい。Security Key（PIN 付き）も使える |
| 署名 Algorithm | EdDSA（-8）、ES256（-7）、RS256（-257） | 主要な Authenticator が対応する。それ以外は拒否 |
| Origin | 設定した Origin との**完全一致**（Scheme・Host・Port）。`https://h` と `https://h:443` は別の文字列として扱い、後者は拒否する | Browser は既定の Port を付けない。Host 名の大文字・末尾の `/`・別の Port・`http` はすべて拒否 |
| RP ID | 応答の RP ID Hash が設定の RP ID と一致すること | 別の RP 向けの応答は使えない（Phishing 耐性） |
| Challenge | 32 byte の乱数（`secrets`）。**Server 側**（`passkey_challenges`）に、**Session と用途（登録・認証）ごとに 1 行**保存する。再度 Begin すると置き換わる。**単回使用**（消費は `DELETE ... RETURNING`）。有効時間は既定 5 分で、期限は**Database の時計**で、行を Lock した後に判定する | 別の Session（同じ User の別端末、盗まれた Cookie）が答えたり上書きしたりできない。答えが誤りでも Challenge は消費する（1 つの Challenge に 1 回の試行）。行は Session ごとに最大 2 つで、増え続けない |
| 登録の上限 | 1 User に有効な Passkey を 10 個まで | DB Row と Options の大きさを抑える |
| User Handle | User の ID（16 byte） | 個人情報ではない不透明な値。同じ Authenticator に同じ Account を 2 回登録すると、Discoverable な Credential が置き換わる（`excludeCredentials` に登録済みの ID を入れて、重複を防ぐ） |

### 4. 署名 Counter と Clone の検知

- 認証の応答の Counter は、**保存した値より大きい**ことを要求する。ただし**両方が 0** のとき（Counter を持たない Authenticator。同期される Passkey に多い）は受け付ける。判定と保存は 1 つの条件付き `UPDATE` で行い、同じ Assertion の 2 つの同時の使用や再送が両方成功することはない。
- 満たさない場合は、その Step-up を**拒否**し（403 `invalid_credentials`、Audit は deny `sign_count_regression`）、固定の Warning を Log に残す（Credential の ID や Counter の値は出さない）。**Credential は自動では失効しない。** 保存した Counter も進めない。
- Backup の適格性（`backup_eligible`）は登録時の値で不変とし、認証で変わったら拒否する（`verification_failed`）。`backed_up` は認証のたびに更新する（表示用）。

自動で失効しない理由: Clone が本当に起きたときに Owner の Passkey を自動で失効すると、Owner を締め出す操作を攻撃者が誘発できる（Clone の Key を持つ者が、意図的に小さい Counter を送る）。同期される Passkey は Counter が不安定なことがあり、誤検知で自動失効すると正規の Owner を締め出す。Audit の行が、疑いの検知の手掛かりになる。**代案**: 自動で失効する（判断点 5）。

### 5. 「Passkey 必須」の強制: Session の Gate

**要件は「Owner・Admin は Passkey 必須」と定めるが、Login のどの段階で要求するかは定めていない。** 次の 2 つの読みがある。

- **R1（登録の強制だけ）**: `required` の Role は、Passkey を登録しなければ使えない。登録した後は Password の Login だけで Session が開く。重要操作だけが Passkey の Step-up を要る。
- **R2（推奨。Sign-in ごとに Passkey）**: `required` の Role は、Password で Sign-in して**制限された Session** を得て、Passkey の認証を完了するまで何もできない。

**R2 を推奨する。** R1 では、Password を盗まれるだけで Owner・Admin の Session が全面的に開く（重要でない操作、たとえば Project の読み取りや Agent の実行を含む）。「Passkey 必須」の意図（Password だけの侵害で Owner・Admin を乗っ取らせない）を満たすのは R2 である。

R2 の実装（Migration `0023` の `auth_sessions.passkey_gate`）:

| Gate | いつ | Session ができること |
| --- | --- | --- |
| `open` | 要求が `optional`、または Passkey の設定がない、または Passkey の手順を終えた | 制限なし |
| `enrollment_required` | 要求が `required` で、有効な Passkey が 0 | **Passkey の登録だけ**（`GET /session`、Logout、Passkey の一覧、登録の Begin / Finish、認証の Begin / Finish）。行き止まりではない（Password の Login と `owner-recover` は常に使える） |
| `assertion_required` | 要求が `required` で、Passkey が 1 つ以上 | **Passkey の認証だけ**（同じ一覧）。登録は拒否 |

- Gate は **Sign-in の Transaction の中で決める**（Policy の要求と有効な Passkey の数を、User の行の `FOR SHARE` の下で読む）。Passkey の登録・失効は User の行を `FOR UPDATE` で Lock するので、数え違えることはない。Audit の `auth.login` の理由は、制限された Session のとき `authenticated_passkey_pending` / `authenticated_enrollment_only` になる。
- 制限された Session は、**すべての Route で既定で拒否**する（403 `passkey_required`。`SessionPrincipalProvider.get_principal` が拒否する。WebSocket は 1008）。手順を終えるための Route だけが、`require_capability(..., allow_restricted=True)` で明示的に通す（Test が、その一覧が正確に 7 つであることを固定する）。Agent が User の名前で行う操作（`DatabasePrincipalDirectory`）は Session を持たないので、この Gate の対象ではない。
- **手順の完了**: 登録の Finish は Gate を開き、Session ID を作り直し（権限が変わるため）、その Session を登録した Passkey に結び付ける。**登録は Step-up として記録しない**（Password だけで登録した Credential は、まだ何も証明していないため。Password を盗んだ者が「登録して、すぐ Owner の Step-up を得る」ことを防ぐ）。認証の Finish は Gate を開き、Session ID を作り直し、**Passkey の Step-up として記録**し、Session を認証した Passkey に結び付ける。
- Gate が `assertion_required` の Session で、Passkey がすべて失効していた場合（別の Session が失効させた後）は、`enrollment_required` として扱う（登録を許す。行き止まりにしない）。
- **既存の Session は影響を受けない。** Migration は既存の行を `open` にする（0015 の「変更は新しい Sign-in・新しい Session から」）。Owner が Policy を変えても、動いている Session は失効も降格もしない。
- **既知の限界**: Owner が最初に Passkey を登録する前の間は、Password だけが要素なので、Password を盗んだ者が先に自分の Passkey を登録できる（最初の 1 つは Password の信頼に依存する）。登録後は、Password だけでは Session が開かない。

**R1 を選ぶ場合**: `assertion_required` の Gate をなくし、登録した User は Password の Login で `open` になる。実装は小さくなるが、上の理由で推奨しない。

### 6. Step-up が要る操作

すべて**Passkey の Step-up**（Password の Step-up は数えない。Password を盗んだ者が `POST /auth/step-up` で得られるため）。有効時間は Policy の値（5〜240 分、既定 30 分）。判定は共通の関数（`auth/stepup.py`）で行う: **Session の行を `FOR SHARE` で Lock してから**、Database の時計（`greatest(Service の時計, clock_timestamp())`）で `stepup_at + 有効時間 > now` を見る。Lock の待ちで期限切れの Step-up が通ることも、判定と Commit の間に Session が失効することもない。

| 操作 | 要求 | 状態 |
| --- | --- | --- |
| Policy の変更（`PUT /auth/policy`） | Passkey の Step-up | 0015 で実装済み。Passkey の Verifier の登録で、本番でも動く |
| Account の Lock の解除（`POST /auth/users/{id}/unlock`） | Passkey の Step-up。**対象を調べる前に判定する**（Step-up のない Session に、Account の存在を教えない） | この Issue で追加 |
| Passkey の追加（すでに Passkey がある User） | Passkey の Step-up | この Issue |
| Passkey の追加（Passkey が 0 の User） | 直近の認証（Sign-in が有効時間内、または任意の Step-up） | この Issue |
| Passkey の失効 | 要求が `required` の Role: Passkey の Step-up。それ以外: 任意の Step-up | この Issue |
| Tool Broker の強い承認 | 下記 | この Issue |

- **Owner・Admin の重要操作は、Role の Passkey の要求が `optional` に変えられていても、Passkey の Step-up を要求する**（要件の `[FIXED]` は Owner・Admin の重要操作に Passkey の Step-up を求める。設定で変えられるのは Passkey の「要求」と「有効時間」であり、Step-up の要否ではない）。その結果、Passkey を持たない Admin は Lock の解除ができない（Fail Closed）。判断点 7。
- **Tool Broker の強い承認**（`ApprovalService(step_up=...)`）: `PasskeyApprovalStepUp`（`AuthServices.approval_step_up`）を渡すと、承認する User の**有効な Session のどれかに**、Policy の有効時間内の Passkey の Step-up があるとき `True` を返す（Password の Step-up、Gate が開いていない Session、`active` でない User は数えない）。`ApprovalService` の既定は `FailClosedStepUp` のまま変えない（Verifier を渡した Deployment だけが有効にする）。**限界**: Step-up は User と時間に結び付き、承認そのもの・承認を決める Session には結び付かない（`ApprovalService` が Session も Challenge も渡さないため）。承認の Endpoint（未実装）は、決める Session で `AuthService.step_up` を先に行うか、承認に結び付いた Challenge を使うべきである（判断点 8）。Passkey を持たない User の強い承認は Pending のまま残る。
- `POST /auth/step-up` は Password だけを受け付ける（`method: passkey` は 422）。Passkey の Step-up は `/auth/passkeys/authenticate/*`（Begin と Finish）の専用の Ceremony で行う。

### 7. Passkey の失効（Device の Revoke）

`DELETE /auth/passkeys/{id}`（本人だけ。他人の ID と存在しない ID は同じ 404）。1 つの Transaction で次を行う。

1. User の行を `FOR UPDATE` で Lock し、Session の Gate が `open` であることと、6 節の Step-up を確かめる。
2. Passkey を失効する（`revoked_reason = revoked_by_user`。行は消さない。Credential の ID を他人が再登録できないように、Audit の ID が引けるように）。
3. **要求が `required` の Role の最後の Passkey は失効できない**（409 `last_passkey`。Rollback する）。Passkey だけで守られた Account が、Password だけの Account に下がること、そして最初の 1 つの登録が Password の信頼に戻ることを防ぐ。先に代わりを登録する。
4. **その Passkey が開けた Session（Sign-in の認証、または最初の登録）を終える**（`revoked_reason = passkey_revoked`）。呼んだ Session 自身が該当すれば、その Session も終わり（応答の `signed_out: true`、Cookie を消す）。盗まれた Device の Passkey で開いた Session が、失効の後も残らない。
5. **User のすべての Session の Passkey の Step-up を忘れる**（`stepup_at`・`stepup_method` を空にする）。どの Credential でどの Step-up をしたかは持たず、簡単で安全な側に倒した（Step-up をやり直す）。
6. その User の開いている Challenge を消す。
7. Audit を同じ Transaction で書く。

- **Passkey を失った User の復旧**: Owner は `owner-recover`（0005）。Admin と User には、この Issue では経路がない（**Admin が全 Passkey を失うと、Owner の介入手段がない**）。Admin による他 User の強制 Reset（0015 の 14 節が別の Issue とした）を先に作る必要がある。判断点 10。
- **最後の Owner を締め出さない**: (a) 設定がなければ Gate は開く。(b) `required` で Passkey が 0 なら登録の Session（Password の Login と `owner-recover` が常に使える）。(c) 最後の Passkey は失効できない。(d) Recovery は全 Passkey を失効して、次の Sign-in を登録の Session にする。

### 8. Recovery（Decision 0005 の 7 節）

`AuthService(credential_invalidators=...)` に `PasskeyRegistry.revoke_all_in` を登録する。Token の消費と**同じ Transaction**で、User の全 Passkey を失効し（`revoked_reason = recovery`）、開いている Challenge を消す。全 Session の失効は 0015 の実装が行う。どれか 1 つが失敗すれば、全体を Rollback し Token は消費されない（Test 済み）。Recovery の後の Sign-in は `enrollment_required` の Session になり、Password の再設定と Passkey の再登録が必須になる。

### 9. `users.passkey_required` 列（Migration 0021）

0015 の 12 節は、この列は Owner・Admin では CHECK 制約で `false` にできず、Login の処理は列を見ず `auth_policy` を見る、列を設定へ合わせる（CHECK を外す、または列を捨てる）かは PAW-023 で決める、とした。

| 選択肢 | 内容 | 評価 |
| --- | --- | --- |
| A（推奨） | 列も CHECK も**そのまま**。「強制は `auth_policy` だけが決める。列は Role の既定を表す不変の印」と定め、その規則を Test で固定する | 変更が最小。列を読む Code は Token の受け取りの応答（`Redemption.passkey_required`）だけで、強制には関わらない |
| B | CHECK を外す | 列が設定と食い違ってよくなる代わりに、列の意味がさらに曖昧になる |
| C | 列を捨てる | 意味は最も明確だが、`users` の Model、Migration 0021、Owner の作成の Code、8 つの Test File の `INSERT`（`passkey_required` の指定）に触れ、並行する他の Migration・Issue と衝突する |

**A を実装した。** Test（`test_passkey_gate`）は、(1) Owner の列が `true` のまま、設定が `optional` なら Gate が開くこと、(2) User の列が `false` のまま、設定が `required` なら Gate が閉じることを固定する。将来 C にする場合は、他の Lane が落ち着いてから、新しい Decision で扱う（判断点 9）。

### 10. 認証の試行の制限

- `enroll/finish` と `authenticate/finish` の失敗（Challenge の欠落・期限切れ・不正な応答を含む）は、Password の誤りと**同じ Account と接続元の Backoff の Counter** に数える。成功すると Account の Counter が消える。5 回目から Lock（30 秒〜1 時間）。Lock 中は 429 と `Retry-After`。Passkey の Ceremony を、Password の総当たりの代わりに使えないようにする。
- `enroll/begin` と `authenticate/begin` は Session ごとの行の置き換えで、Challenge の行は増えない。長く期限切れの行は、Begin のたびに最大 50 行ずつ消す。
- 限界: 盗まれた Session（Password を知っている）が失敗を重ねると、Owner の Password の Login も Lock される（0015 の 7 節の限界と同じ DoS。最大 1 時間、`owner-recover` で戻れる）。

### 11. Audit

`audit_events` に ID と列挙値だけで残す（Credential の ID、公開鍵、Challenge、名前、Origin は入らない）。

| `action` | 内容 |
| --- | --- |
| `auth.passkey.register` | allow `registered` / deny `challenge_invalid`、`verification_failed`、`already_registered`、`limit_reached`、`gate_not_allowed`、`step_up_required`、`step_up_method_insufficient` |
| `auth.passkey.authenticate` | allow `verified` / deny `challenge_invalid`、`unknown_credential`、`verification_failed`、`sign_count_regression`、`invalid_credentials`（Session の失効など） |
| `auth.passkey.revoke` | allow `revoked` / deny `step_up_required`、`step_up_method_insufficient`、`last_passkey`、`not_found`、`gate_not_allowed` |
| `auth.login` | 制限された Session のとき、allow `authenticated_passkey_pending` / `authenticated_enrollment_only` |
| `auth.unlock` | deny に `step_up_required`、`step_up_method_insufficient` を追加 |

変更を伴う出来事は変更と同じ Transaction で書き、拒否は別の短い Transaction で Best Effort に書く（0015 の 9 節と同じ）。制限された Session が他の Route を呼んで得る 403 は、Audit に書かない（Sign-in の行に理由が残る）。

### 12. HTTP Route

`/api/v1/auth/passkeys/`（すべて `require_capability`。CSRF の Origin の検査と Body の 16 KiB の上限は、他の認証 Route と同じ）:

| Route | Capability | 制限された Session |
| --- | --- | --- |
| `GET /auth/passkeys` | `account.read` | 可 |
| `POST /auth/passkeys/enroll/begin`、`/enroll/finish` | `account.manage` | 可 |
| `POST /auth/passkeys/authenticate/begin`、`/authenticate/finish` | `account.manage` | 可 |
| `DELETE /auth/passkeys/{id}` | `account.manage` | 不可 |

Cookie は、Session の ID を作り直した応答（`enroll/finish` で Gate が開いたとき、`authenticate/finish`）で、Commit の後、応答の説明の読み取りより先に設定する（読み取りの失敗で新しい Cookie を失わない。0015 と同じ）。Commit が期限で中断された場合の結果は不明（`Database.run_abortable` の限界）で、Passkey が登録された可能性がある（一覧で確かめられる）。

### 13. この Issue に含めないこと

- **Passkey だけの Sign-in（Password なし）**。認証前に Server 側の Challenge を作る Endpoint は、誰でも行を作れ（DoS）、Discoverable Credential の扱い（Username なしの Login）を決める必要がある。別の Issue と Decision で扱う。
- 他の User の Passkey の Reset（Admin・Owner による強制の再設定）。複数端末の追加（QR / Link の Pairing、既存端末での承認）。信頼済みの端末への警告。Passkey の名前の変更。Recovery Code。
- 承認そのものに結び付いた Step-up（承認の Endpoint と同時）。
- Attestation の検証（Authenticator の種類の制限。たとえば Owner に Device-bound の Key だけを要求すること）。`backup_eligible` は記録している。

### 14. DB の権限（Decision 0005 の条件、Migration 0023）

Web の Role（`PAW_APP_DATABASE_ROLE`）の権限は、実際に実行する文だけに絞る（`tests/test_passkey_grants.py` が、非 Superuser の Role で Service と HTTP の Test を通し、権限を列まで固定する）。

| Table | 権限 |
| --- | --- |
| `user_passkeys` | SELECT、INSERT、`sign_count`・`last_used_at`・`backed_up`・`revoked_at`・`revoked_reason` の UPDATE。DELETE なし。`credential_id`・`public_key`・`user_id` は変えられない |
| `passkey_challenges` | SELECT、INSERT、DELETE、`challenge`・`created_at`・`expires_at` の UPDATE |
| `auth_sessions`（0022 に追加） | `passkey_gate`・`passkey_id` の UPDATE。`auth_method` は変えられない |

**守れないもの（Decision 0005 が Password で受け入れたものと同じ）**: Web の Role は Passkey の行と Session の Gate を書けなければならないので、**Application が侵害されれば、その Application は自分の Passkey を登録し、Gate を開けられる**。防ぐ手段は Application の侵害を防ぐこと（Operator の Role との分離、Agent の Runtime に DB の接続を渡さない、0006）。

### 15. 同時実行（Lock の順序）

- User の行: 登録と失効は `FOR UPDATE`、Sign-in は `FOR SHARE`（Gate を決める Passkey の数を、Sign-in が読む間は変わらない）。
- **Passkey の行を、Session の行より先に Lock する。** Step-up は Credential を `FOR SHARE` で確かめてから Session を更新する。失効と Recovery は、Session の行を読む・終える前に Passkey の行を Lock する（Recovery は、Passkey の失効を Session の失効より先に行う）。これで、同じ Session の Step-up と失効、Step-up と Recovery が、互いの Lock を待つ循環（Deadlock）にならない（Lock を保持して競わせる Test が、順序を逆にすると失敗することを確かめている）。
- Commit が期限で中断されたときの結果は不明（`Database.run_abortable` の限界）。登録は、Passkey ができていて、Session の ID が作り直されている可能性がある。その場合、Challenge は消費済みなので再送は二重に登録せず（`challenge_invalid`）、次の Sign-in は「登録した Passkey の認証を待つ」Session になり、完了すれば開く（Test 済み）。

## 選定理由

- Gate を Session の性質にし、Sign-in の Transaction の中で決めるのは、「新しい Sign-in から効く」（0015）、「行き止まりにしない」（0005）を、Race なしに満たす最小の形だから（Policy を毎回 Request で読むと、既存の Session を黙って降格し、Owner を締め出す）。
- 制限された Session の拒否を Provider（`get_principal`）に置くのは、すべての Route（今の Route、他の Lane の Route）に既定で効き、通す Route を明示的な 1 つの引数にするため。認可の Policy（`Reason`）に足す案は、Authorizer と全 Capability の対応表への変更が大きい。
- Passkey の Step-up の判定を 1 つの関数にするのは、Policy の変更、Lock の解除、Passkey の管理、Tool Broker が同じ規則を同じ Lock と時計で使うため。
- Challenge を Session ごとに Server へ持つのは、Cookie や URL に Challenge を載せず、別の Session の答えを受け付けないため。

## 代案

- 自前で WebAuthn を実装する: 署名・COSE・Authenticator Data の誤りが認証の突破になる。
- `fido2` を使う: 1 節。
- Attestation を `direct` にして Authenticator の種類を制限する: 信頼する Root の一覧の保守が要り、User の Privacy を下げ、実機の検証なしに入れると、正規の端末を拒否する危険が大きい。
- Resident Key を必須にする: Passkey だけの Sign-in をしないので不要。Security Key が使えなくなる。
- 署名 Counter の後退で自動失効する: 4 節。
- Passkey の未設定のとき起動を拒否する: 開発・Test・初期の運用で起動できない。Passkey を設定しない選択（強制しない）を、警告つきで許した。
- Host Header から RP ID と Origin を導く: Request が影響できる値で、信頼する Origin を決めることになる。
- R1（登録の強制だけ）: 5 節。
- 失効時に、どの Credential でどの Step-up をしたかを持って、その Step-up だけを消す: 列が増え、判断が複雑になる。全部を消す方が安全で単純。
- Password の Step-up でも Lock の解除ができる: Password の侵害だけで Owner・Admin の重要操作ができてしまう（0015 の 12 節と同じ理由）。
- Tool Broker の Step-up を承認ごとの Challenge にする: 承認の Endpoint がまだない。

## 人間の判断が必要だった点（推奨つき。すべて「承認時の決定」で決まった）

1. **Library**: `webauthn` 3.0.1（推奨）／3.0.0 へ下げる／`fido2` 2.2.1。
2. **強制の意味**: R2（`required` の Role は Sign-in ごとに Passkey。推奨）／R1（登録の強制だけ）。
3. **設定がないとき**: Passkey を切り、強制しない（警告つき。推奨）／起動を拒否する。
4. **Ceremony**: Attestation `none`、User Verification required、Resident Key preferred（推奨）／Attestation `direct`、Device-bound の Key を Owner・Admin に要求する。
5. **署名 Counter の後退**: 拒否し Audit に残す、自動失効はしない（推奨）／自動で失効する。
6. **失効の規則**: `required` の Role の最後の Passkey は失効できない、Passkey の Step-up が要る（推奨）／制限しない。
7. **Owner・Admin の重要操作**: 要求が `optional` でも Passkey の Step-up を要求する（推奨。要件どおり）／`optional` の Role は Password の Step-up でよい。
8. **Tool Broker の強い承認**: User 単位・時間単位の Passkey の Step-up（推奨。承認の Endpoint が入るときに、決める Session で Step-up する形にする）／承認に結び付いた Challenge を今作る。
9. **`users.passkey_required` 列**: そのまま（A。推奨）／CHECK を外す（B）／列を捨てる（C）。
10. **別の Issue にする範囲**: Admin・Owner による他 User の Passkey の Reset（Admin が全 Passkey を失う場合の経路。強く推奨）、Passkey だけの Sign-in、複数端末の追加（Pairing）、Passkey の名前の変更。
11. **数値**: Challenge の有効時間 5 分、Passkey の上限 10 個（推奨。どちらも設定・定数で変えられる）。

## リスク

- Library の 3.0.1 は公開の翌日に固定した（1 節）。実機（Browser、Authenticator）で登録・認証したことがない。Software Authenticator の Test は、仕様どおりの Payload を通すが、実際の Browser・OS・Authenticator の癖（Counter、Flag、Attestation の匿名化、`transports`）は確かめていない。
- 設定がないと Passkey の要求は強制されない（2 節）。Owner が要件どおりに動いていると思い込んで設定を忘れると、Password だけの Sign-in のままになる（起動時の警告と `passkey.available: false` が手掛かり）。
- Attestation を検証しないので、Owner・Admin の Passkey がクラウドで同期される Passkey（`backup_eligible: true`）でもよい。そのクラウド Account の侵害は Passkey の侵害になる。
- Admin が全 Passkey を失うと、この Issue では復旧できない（7 節）。
- 最初の Passkey の登録は Password の信頼に依存する（5 節）。
- Tool Broker の Step-up は User 単位で、別の Session の Step-up が承認に使われうる（6 節）。
- 署名 Counter の誤検知（同期される Passkey）で、Owner が Step-up できなくなる。別の Passkey か `owner-recover` で戻る。
- Application が侵害されれば、Passkey の登録と Gate の解除ができる（14 節。Decision 0005 が Password で受け入れたものと同じ限界）。
- 実際の Browser、Reverse Proxy、TLS を通した動作は確かめていない（`TestClient` と実 PostgreSQL まで）。

## 承認後の扱い

- 2026-09-26 に承認された。`Approval` に記録し、Status を Approved に改めた。PAW-023 の PR（[#107](https://github.com/TomokiAkiyama06/personal-ai-workspace/pull/107)）は本 Decision を参照する。
- `REQUIREMENTS.md` の `[FIXED]`「Passkey Policy」の注記（Decision 0015 の 11 節で追記したもの）のうち、「Passkey実装（PAW-023）まで、この設定変更は本番では使えない」と「PAW-022はPasskeyを強制しない」の 2 か所を、実装の状態（Passkey を設定した環境では Passkey の Step-up で変更できる。強制は制限された Session）に合わせて更新した。`[FIXED]` の箇条書き（規則）と、Human が指示した他の注記の文は変えていない。
- 承認された数値（Challenge の有効時間 5 分、Passkey の上限 10 個）と各選択は、変えるときにこの Decision を書き換えず、新しい Decision から `Supersedes` する。数値は設定（`PAW_PASSKEY_CHALLENGE_TTL_SECONDS`）と定数（`MAX_PASSKEYS_PER_USER`）で、Schema は変わらない（Migration は不要）。
- 運用: `PAW_PASSKEY_RP_ID`、`PAW_PASSKEY_ORIGINS` を設定してから、Owner が Sign-in して Passkey を登録する（設定前は要求が強制されない）。
- 別の Issue にする範囲（判断点 10）は、次の「後続の Issue」を Issue として起票する（起票は Human または Coordinator が行う。この Decision は本文だけを用意する）。

### 後続の Issue: Owner による Passkey の Reset（Passkey を全部失った Admin・User の復旧）

**Title**: Owner による他 Account の Passkey の Reset を実装する（PAW-023 の後続）

**Goal**: Passkey の Device を全部失った Admin（と User）が、Owner の操作で Passkey を登録し直せるようにする。PAW-023 では、Owner は `owner-recover` で戻れるが、Admin と User には経路がない。要求が `required` の Admin が Device を全部失うと、その Passkey は有効なまま残るので、Password で Sign-in しても「認証だけができる」Session（`assertion_required`）から出られず、Passkey を登録し直すことも失効することもできない（行き止まり。Owner の介入手段がない）。

**背景**: Decision 0025 の 7 節と「リスク」。Decision 0015 の 14 節は「Admin による強制 Reset の Token の発行（1 回限りの再設定フロー）」を User 管理の Issue とした。この Issue はそれと組み合わせて扱ってよい。

**受け入れ条件（案。Issue の作成時に確定する）**

- [ ] Owner は、Admin と User の Passkey をすべて失効できる（`POST /api/v1/auth/users/{id}/passkeys/reset` など。Owner 専用で Agent に委任できない Capability。Admin が User の Passkey を Reset できるかは Issue で決める。Unlock と同じ規則（Admin は User だけ）を推奨）。Owner 自身の Passkey は対象にしない（Owner は `owner-recover`）。
- [ ] 操作した Owner の直近の Passkey の Step-up を要求する（対象を調べる前に判定する）。
- [ ] 同じ Transaction で、対象の全 Passkey を失効し（`revoked_reason` に `admin_reset` を足す。`user_passkeys` の CHECK 制約の Migration が要る）、開いている Challenge を消し、対象の全 Session を失効する（`revoked_reason = admin`）。次の Sign-in は、登録だけができる Session（Gate）になる。
- [ ] Password の扱いを決める（Passkey だけを Reset すると、登録の最初の 1 つが Password の信頼に戻る。0025 の 5 節の限界）。既定は、Passkey の Reset と同時に、0015 の 14 節の 1 回限りの Password の再設定を必須にする（Owner が対象の Password を見ることはできない）。
- [ ] Audit（`auth.passkey.reset`。allow / deny の理由は列挙値）。Owner・対象の ID だけを残す。
- [ ] 最後の Owner を巻き込まない（Owner を対象にしない）。同時の操作（Reset と、対象の Sign-in・登録・Step-up）の Race を、Passkey の行を Session の行より先に Lock する順序（0025 の 15 節）で防ぐ。
- [ ] Test: 全 Method の引数検査、Race（別の接続で競わせる）、権限（非 Superuser の Web Role の最小権限）、Migration の上げ下げと差分なし。

**依存**: PAW-023（[#20](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/20)）、PAW-022（[#19](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/19)）、User 管理の Issue（0015 の 14 節）。

## 承認時の決定（2026-09-26）

Human は、作業 Session 内の質問 Tool で、この Decision を「推奨どおり」として承認した（判断点 1〜11 を一括で。個別の回答ではない）。すべて推奨どおりで、本文の変更はない。

1. **Library**: `webauthn`（py_webauthn）3.0.1 を完全一致で固定する（`cryptography==50.0.1` も）。3.0.0 への引き下げと `fido2` は採らない。
2. **強制の意味**: R2。`required` の Role は Password で Sign-in して制限された Session を得て、Passkey の登録（未登録）または認証（登録済み）を終えるまで、許された少数の Route だけが使える。R1（登録の強制だけ）は採らない。
3. **設定がないとき**: `PAW_PASSKEY_RP_ID` と `PAW_PASSKEY_ORIGINS` がなければ Passkey の機能を切り、要求を強制しない。起動時に警告し、`passkey.available: false` を返す。起動の拒否は採らない。
4. **Ceremony**: Attestation `none`（それ以外は拒否）、User Verification required、Resident Key preferred、Algorithm は EdDSA・ES256・RS256、Origin は設定との完全一致。Attestation `direct` や Device-bound の Key の要求は採らない。
5. **署名 Counter の後退**: 拒否して Audit に残す（`sign_count_regression`）。自動では失効しない。
6. **失効の規則**: `required` の Role の最後の Passkey は失効できない。失効には Passkey の Step-up（`required` の Role）または任意の Step-up が要る。失効した Passkey が開けた Session は終わり、User の Session の Passkey の Step-up は忘れる。
7. **Owner・Admin の重要操作**: 要求が `optional` に変えられていても Passkey の Step-up を要求する（Password の Step-up は数えない）。Passkey を持たない Admin は Lock を解除できない。
8. **Tool Broker の強い承認**: User 単位・時間単位の Passkey の Step-up（`PasskeyApprovalStepUp`）。`ApprovalService` の既定は `FailClosedStepUp` のまま。承認の Endpoint が入るときに、決める Session で Step-up する形にする。承認に結び付いた Challenge は今は作らない。
9. **`users.passkey_required` 列**: A（そのまま）。列も CHECK も変えず、強制は `auth_policy` だけが決める。CHECK を外す案（B）と列を捨てる案（C）は採らない。
10. **別の Issue にする範囲**: Owner による他 Account の Passkey の Reset（Passkey を全部失った Admin の経路）、Passkey だけの Sign-in、複数端末の追加（Pairing）、Passkey の名前の変更は、この Issue に含めない。**Reset は、上の「後続の Issue」の文で起票する**（Admin が全 Passkey を失うと復旧できないため、優先して扱う）。
11. **数値**: Challenge の有効時間 5 分、Passkey の上限 10 個。どちらも設定・定数で変えられる。

- 実機（Browser、Authenticator）での検証、Reverse Proxy と TLS を通した動作の確認は、この承認に含まれない（リスクとして残る）。
