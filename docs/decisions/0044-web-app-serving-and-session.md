# Web App の配信・Session / CSRF・Frontend の Tooling の方針

- Status: Approved
- Date: 2026-09-28
- Scope: PAW-060（Web UI Application Shell / Authentication UI、Issue [#46](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/46)）の `apps/web` と、それを配信する `apps/backend/paw_backend/web.py`。後続の Web の Issue（PAW-061 以降）が同じ前提を使う
- Supersedes: なし。[Decision 0003](0003-backend-cli-web-implementation-stack.md) の表のうち「実装Issueが選ぶ既定値」（pnpm、Web の Lint / Format Tool）を、0003 が認める範囲で理由を付けて選び直す（0003 の承認範囲の React + TypeScript + Vite は変えない）
- Approval: 2026-09-29、Humanが作業Session内で、判断点ごとの説明（推奨つき）を受けたうえで直接回答して承認（判断が必要な点の全点。末尾の「承認時の決定」）

## 背景

Issue #46 の受け入れ条件は次の 4 つだけである。

- 日本語 Default / i18n-ready
- login / passkey / device management
- responsive layout
- Notification Center shell

[Decision 0003](0003-backend-cli-web-implementation-stack.md)（Approved）は Web を React + TypeScript + Vite と決め、Lint / Format Tool は PAW-060 で選ぶとしている。
Backend の認証（PAW-022 / PAW-023 / PAW-024、[Decision 0015](0015-login-session-password-policy.md)・[0025](0025-passkey-webauthn-policy.md)・[0033](0033-user-invitation-and-device-pairing.md)）は、すでに次を決めて実装している。

- Session は `__Host-paw_session` の Cookie（`Secure`、`HttpOnly`、`SameSite=Strict`）。Server は Hash だけを持つ。
- 状態を変える Request（`POST` / `PUT` / `PATCH` / `DELETE`）は、Request 自身の Origin か `PAW_ALLOWED_ORIGINS` の Origin からでなければ `OriginCheckMiddleware` が 403 `forbidden_origin` で拒否する。
- Pairing の QR / リンクは `/pair#<token>`（Token は URL の Fragment で、Server の Log に残らない）。
- 重要な操作は最近の Step-up を要し、足りなければ 403 `step_up_required` / `step_up_method_insufficient` を返す。

画面そのもの（Layout、Design Token、文言、役割ごとの Menu、Breakpoint、状態遷移）は、Human が Comment を反映して承認した **PAW-060 の Design Canvas**（claude.ai Artifact `BkiwvDocfuuXxfU9TA14pa`「PAW-060 Web Shell UI/UX」、36 Artboard、v10）を正本にする。
この Issue に関わる Artboard は Login / LoginLight、PasskeyStates、DeviceAdd、ShellDark / ShellLight、NotificationCenter、UserMenu、Roles、SettingsDevices、SettingsLanguage、MobileLogin / MobileShell / MobileMenu / MobileNotifications、Tablet、StateFlows、Tokens、ApiContract である。
Design Canvas と実際の Backend の API・承認済みの Decision が食い違うところは **Backend / Decision を優先**し、下の「Design Canvas との差分」に 1 件ずつ挙げる。

一方で、**次のことは要件・Backlog・既存の Decision のどれも決めていない。**
Web App をどこからどう配信するか（同じ Origin か、別の Origin + CORS か）、Page の CSP と Cache、Session と CSRF の扱いを Web 側で足すか、Client 側の Routing、Package 管理・Lint / Format・Test の Tool と CI での扱い、i18n の仕組み、Step-up の画面の出し方、Notification Center の Data の出どころ。
[AGENTS.md](../../AGENTS.md) の「仕様変更」に従い、実装（この Issue の PR）はこれらを下の推奨で置き、この Decision で承認を求める。数値と選択の多くは設定か定数で、変えても Schema は変わらない（Migration はない）。

## 提案

### 1. Web App は Backend が API と同じ Origin で配信する

- **推奨**: Build 済みの `apps/web/dist` を Backend（Uvicorn）が配信する（`PAW_WEB_DIST_DIR` に絶対 Path を設定したときだけ。未設定では従来どおり API だけ）。`/api` の外への `GET` / `HEAD` だけを扱い、Build の File、Client 側の Route（最後の Segment に `.` がない Path）には `index.html` を返す。`/api/*` と状態を変える Method は従来どおり API に渡す。`.` で始まる Segment、Directory の外へ出る Path（`..`、Symlink。`index.html` 自身が外への Symlink の場合も、起動時と各 Request で拒否する）は配信しない。
- 理由: Session Cookie（`__Host-`、`SameSite=Strict`）と Origin 検査は同じ Origin を前提にしている。Backend が配信すれば、TLS を Uvicorn が終端する構成（`PAW_TLS_CERTFILE`）でも Reverse Proxy（`tailscale serve`、Caddy）が終端する構成でも、追加の設定なしで同じ Origin になる。Page の Security Header も Backend の 1 か所で決まる。
- 代案 A: Reverse Proxy が `dist` を配信し、`/api` を Backend へ転送する。同じ Origin にはなるが、Proxy がない構成（Uvicorn が TLS を終端）で配信する手段がなく、CSP などの Header を Proxy ごとに書くことになる。この推奨でも、Proxy で配信したい運用はそのまま取れる（`PAW_WEB_DIST_DIR` を設定しない）。
- 代案 B（取らない）: 別の Origin に置き、CORS で API を呼ぶ。`SameSite=Strict` の Cookie は Cross-site の Request に付かず、Origin 検査も拒否するため、Session の方式（Decision 0015）から変える必要がある。
- 開発時は Vite の Dev Server（`http://localhost:5173`）が `/api` を Backend（`http://127.0.0.1:8000`）へ転送する。Browser の `Host` を変えない（`changeOrigin: false`）ので、Cookie と Origin 検査は本番と同じく 1 つの Origin として動く。

### 2. Page の CSP と Cache

- **推奨**: Web App の Page には API とは別の CSP を付ける: `default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'`。Inline Script / Inline Style は許さない（Vite の Build は `<script src>` と `<link>` だけを出す。`assetsInlineLimit: 0`）。外部の CDN・Font は使わない。API の Response の CSP（`default-src 'none'; frame-ancestors 'none'`）は変えない。その他の Header（`X-Frame-Options: DENY`、`nosniff`、`Referrer-Policy: no-referrer`、HSTS）は API と同じ。
- **推奨**: `index.html` などは `Cache-Control: no-cache`（毎回再検証。更新がすぐ届く）、Content Hash つきの `assets/*` は `public, max-age=31536000, immutable`。

### 3. Session / CSRF は既存の仕組みに依拠し、Web 側に CSRF Token を足さない

- **推奨**: Web App は `fetch` を `credentials: "same-origin"`、`redirect: "error"`、`cache: "no-store"` で送るだけにし、CSRF Token（Double Submit Cookie、Header Token）は足さない。CSRF は既存の 2 層（`SameSite=Strict` の Cookie と、状態を変える Request の Origin 検査）で防ぐ。Session の識別子は `HttpOnly` のままで、JavaScript からは読めない。
- **推奨**: Web App は Credential・Token・Session の情報を `localStorage` / `sessionStorage` に保存しない（保存するのは Theme の選択だけ。13 を参照）。Pairing の Token は Fragment から読んだ直後に Address Bar と履歴から消す（`history.replaceState`）。
- 代案: 追加の CSRF Token を導入する。現在の 2 層で防げない攻撃（同じ Site の別の Origin からの Request）は、Origin 検査が Origin の完全一致で拒否するため、得るものがない。Backend の変更（Token の発行・検証）も要る。

### 4. 権限の最終判定は Backend だけが行い、Web は表示を選ぶだけにする

- **推奨**: Web は `GET /api/v1/auth/session` の `system_role` と Passkey の `gate` を、表示の出し分け（`Admin` の Navigation を Owner / Admin にだけ出す、Gate が `open` でない Session に Passkey の画面だけを出す）にだけ使う。Web に独自の認可規則を持たず、Backend の 401 / 403 はそのまま Message として出す（401 は Login 画面へ戻す。ただし、その Request を始めた Session が今も現在の Session のときだけ。起動時の `GET /auth/session` の 401 は「未サインイン」なので戻す合図にしない）。サインアウトは `POST /auth/logout` が成功したとき（または 401 で既に終わっていたとき）だけ画面をサインアウトにする。HttpOnly の Cookie は Server の Logout でしか失効しないため、失敗したらサインイン中のままエラーを出す。これは [apps/web/README.md](../../apps/web/README.md) と Decision 0003 の方針の確認で、新しい判断ではない。

### 5. Client 側の Routing は History API と Backend の Fallback で行う

- **推奨**: Router の Library を使わず、History API の小さな Router（`src/router.tsx`）で固定の Path（`/`、`/chat/new`、`/projects`、`/agents`、`/memory`、`/pulls`、`/admin`、`/notifications`、`/settings` と `/settings/<項目>`（`profile`、`devices`、`appearance` ほか後続の Issue の Placeholder）、`/pair`）を扱う。Backend は Client 側の Route に `index.html` を返す（1）。
- 理由: Backend がすでに Pairing のリンクを `/pair#<token>` の形で決めている（Hash Routing にすると `#` を Token と分け合えない）。Path の数が少なく、Library の依存を増やす理由がない。Path が増えて Nested Route・Loader などが要るようになったら、その Issue で Library（React Router など）を検討する。

### 6. Package 管理は npm にする（Decision 0003 の既定の pnpm を変える）

- **推奨**: npm（Node.js に同梱）と `package-lock.json`。`.npmrc` で `ignore-scripts=true`（依存の Install Script を実行しない。Supply Chain 対策。今の依存に Install Script が必要なものはない）、`engine-strict=true`、`save-exact=true`。依存はすべて正確な Version で固定する。CI は `npm ci`（Lockfile どおり）で導入する。
- 理由: 0003 は pnpm を「実装Issueが選ぶ既定値」とし、理由があれば変えてよいとしている。npm なら Node.js 以外の Tool（Corepack や pnpm の Action）の導入・固定が要らず、CI と開発者の手順が 1 つ減る。Lockfile の Integrity と `ignore-scripts` で、pnpm の主な利点（Script の既定無効化）も得られる。
- 代案: pnpm。Disk の効率と厳密な依存解決に優れるが、Corepack（Node.js 25 で同梱をやめた）か別の Action を固定して導入する必要がある。

### 7. Lint / Format は Biome、Test は Vitest + Testing Library、型検査は TypeScript

- **推奨**: Lint と Format は Biome（1 つの Tool で両方。`biome ci` が Format の差分と Lint の違反を失敗にする。Recommended の規則に Accessibility の規則が含まれる）。Test は Vitest + Testing Library（`@testing-library/react`、`user-event`、`jest-dom`）+ jsdom。型検査は `tsc --noEmit`（`strict`、`noUncheckedIndexedAccess`）。
- 理由: 0003 は「Biome または ESLint（PAW-060 で確定）」としている。Biome は依存が 1 つで設定が短く、ESLint + Prettier + Plugin の組み合わせより固定する Package が少ない。

### 8. Node.js は 24 LTS を固定し、CI では Web の検証を必須にする

- **推奨**: Node.js のVersion を `apps/web/.node-version`（`24.21.0`、Active LTS）に固定し、`package.json` の `engines` で `>=24.21.0 <25` を要求する。GitHub Actions は `actions/setup-node`（Commit SHA で固定）でこの Version を入れる。
- **推奨**: 共通の検証（`.github/scripts/run_ci.py`、pre-commit と GitHub Actions が同じものを実行）の最後に `npm ci` と `npm run ci`（Lint、型検査、Unit Test、Production Build）を足す。GitHub Actions（`GITHUB_ACTIONS=true`）と `PAW_REQUIRE_WEB_CHECKS=1` のときは必須で、`npm` がなければ失敗する。それ以外のローカル環境で `npm` がなければ、警告を出して Web の検証だけを Skip する（Node.js のない開発者が Python 側の Hook を使い続けられるように）。
- 代案: ローカルでも必須にする。Node.js のない環境の pre-commit Hook がすべて失敗するようになる。

### 9. i18n は型つきの自前の Catalog で行い、日本語を既定にする

- **推奨**: i18n の Library を使わず、日本語の Catalog（`src/i18n/ja.ts`）をキーの正本にし、他の言語はその型に合わせる（キーの漏れは型検査と Test が検出する）。文言は Design Canvas のもの（サインイン / サインアウト、ユーザー名、この端末を信頼する、端末とセッション、Passkey など）。**V1 の表示言語は日本語に固定し、他言語の選択肢は画面に出さない**（Design の SettingsLanguage）。英語の Catalog は i18n-ready のために同梱するが、切り替えの UI は出さない（出すかどうかは下の Q1）。日時は `Intl.DateTimeFormat` で Locale に合わせる。Backend のエラーは安定した `error.code` から Catalog の文言に変換する（Backend の英語の `message` は表示しない）。
- 理由: [UI Design](../UI_DESIGN.md) の `[FIXED]`「V1 の基本 UI 言語は日本語」「i18n-ready」を満たし、依存を増やさない。複数形・性などの複雑な規則が要る言語を足す時点で、ICU MessageFormat を持つ Library を検討する。

### 10. Step-up は「まず操作し、要求されたら同じ画面の中で確認する」

- **推奨**: Step-up が要る操作（Passkey の登録・削除、Owner / Admin の新しい端末の承認など）は、まず Request を送り、Backend が `step_up_required` / `step_up_method_insufficient` を返したときだけ、同じ画面の中（Modal ではない。文言は Design の PasskeyStates D）に本人確認の欄を出す。Passkey が登録済みで使えるなら Passkey の確認を出し、`step_up_method_insufficient` でなければ Password の確認も出す。確認が済めば元の操作を再送する。取り消せば何もしない。
- 新しい端末の承認は Backend が常に Passkey の Step-up を求める（Decision 0033、`StepUpGuard`）ので、最初から Passkey の確認だけを出す。それ以外の操作で、Step-up がない状態の `step_up_required` に Password で答えたあとの再送が `step_up_method_insufficient` なら、Passkey だけでもう一度確認を求める（Password の確認を無駄にさせたまま Error にしない）。
- 理由: Step-up の窓（Decision 0015 / 0025）の中なら確認を求めずに済み、要否の判断を Backend だけに置ける。UI Design の「通常操作では画面を不要な Modal で遮らない」に合わせる。

### 11. Notification Center はこの Issue では Shell だけにする

- **推奨**: Header の Bell（未読数の Badge）、一覧の Panel（Non-modal。Severity の表示、同じ種類の通知をまとめて件数を出す、すべて既読）、ERROR / CRITICAL の Non-modal Banner（閉じられる）を実装する。通知の Data は `NotificationSource` の Interface で受け、**この Issue では Source を接続しない**（Backend に通知の API がまだない。Event の経路 `/api/v1/events/*` は認証がなく System Event だけを流すと Backend が定めている）。CRITICAL の Modal（UI Design の「必要に応じ」）は作らない。
- 通知の API（保存・既読・Event の配信と認可）は別の Issue で扱う。そのとき Source を 1 つ足せば、この Shell がそのまま使える。

### 12. Pairing の画面の待ち方

- **推奨**: 新しい端末は、承認待ちのあいだ確認 Code を大きく表示し、`POST /api/v1/auth/pairing/complete` を **3 秒ごと**に呼ぶ（正しい Claim は Rate Limit の試行を返すので、待つ端末は Lock されない。429 のときは `Retry-After` だけ待つ）。拒否・取り消し・期限切れ・使用済み・Lock の Pairing に Backend が返す 400 `invalid_token`（`auth/onboarding/pairing.py` の `_finish`）と 404 `not_found` は終わりとして扱い、Poll をやめて「期限が切れたか、拒否または取り消された」と表示する（拒否された呼び出しは Rate Limit の試行を返さないので、続けると `rate_limited` になる）。QR / リンクの Claim が `invalid_token` のときも同じ表示にする。承認する信頼済み端末は、QR を表示して承認が要る（`approval_required`）あいだ、承認待ちの一覧を **5 秒ごと**に読み直す。承認は確認 Code の入力を必須にする（Decision 0033 の 12）。
- QR Code は `uqr`（依存のない小さな Library、正確な Version で固定）で行列を作り、SVG の Path として描く（`innerHTML` を使わない）。QR の中身は `location.origin` と Backend の `link_path` をつないだ URL。

### 13. Design Token・Font・Theme・Breakpoint は Design Canvas に合わせる

- **推奨**: Design の Tokens の値を CSS 変数として `src/styles.css` に置く（ダーク: 背景 `#14161A`・ナビ `#191C22`・カード `#1B1E24`・入力 `#232730`・罫線 `#2E333D` / `#3C424E`、文字 `#E8EBF0` / `#A6AEBC` / `#79818F`、アクセント `#9B87F5`、選択面 `#2A2540`。ライト: `#F7F6F3` / `#F1EFEA` / `#FFFFFF` / `#EEEBE5` / `#E2DFD8` / `#CBC7BE`、`#1B1D21` / `#59606B` / `#757C88`、`#5B45B8`、`#EDE8FB`。Severity は `#4CAF7D` / `#5B9DF0` / `#E0A34A` / `#E5675B` / `#E0486E`）。角丸は 6 Badge / 9 入力 / 13 Card / 999 Chip、Focus は 3px のアクセント 30% の Ring、Tap 対象は 44px 以上。
- **推奨**: Font は IBM Plex Sans JP（400 / 500 / 600）と IBM Plex Mono（400 / 500）を `@fontsource/*`（OFL-1.1、正確な Version で固定）で Build の `assets/` に同梱する。CSP の `font-src 'self'` のままにでき、外部の Font CDN を使わない。Unicode Range で分割されているので、Browser は使う文字の分だけを読む（`dist/` は約 12 MB、CSS は gzip で約 110 KB）。
- **推奨**: Theme はシステム / ライト / ダーク（既定はシステム。`prefers-color-scheme` がない環境では Design の既定のダーク）。User Menu、設定 › 言語と外観、サインイン画面の「表示」で切り替える。選択は **この Browser の `localStorage`** に保存する（使えなければ保存しないだけ）。Account の設定の API がまだないため、Design の「このアカウントのすべての端末に適用」にはしない（差分 D11）。
- **推奨**: Breakpoint は Design の Tablet のとおり 3 段階: 1280px 以上は Sidebar（252px）と Header の検索、768–1279px は Icon Rail（78px、短い Label）、768px 未満は Drawer（320px）と下部 Tab（チャット / タスク / メモリ / 通知 / 設定）、通知は全画面（`/notifications`）。情報構造はすべての幅で同じにする。下部 Tab は Design の ApiContract が V1 に含めるかを未確定としているため、Design どおりに作ったうえで Q2 で確認する。
- **推奨**: Navigation は Design の Roles: 新しいチャット、チャット、プロジェクト、エージェント / タスク、メモリ、プルリクエスト、区切り、管理（Owner / Admin だけ。役割の Badge つき）、設定。使用状況は管理と設定 › 自分の使用状況に置き、Main の Menu には出さない。Role の表示名は Owner / Admin / Member。User Menu と設定の Sidebar（アカウント / ワークスペース、権限制御は Owner だけ）も Design のとおりにし、後続の Issue の項目は Placeholder にする。

## Design Canvas との差分（Backend / Decision を優先したもの）

| # | Design Canvas | 実装（優先したもの） |
| --- | --- | --- |
| D1 | サインイン画面の「Passkey でサインイン」（Passkey を先に使うサインイン） | 出さない。Backend のサインインはユーザー名とパスワードだけで、Passkey はその後の Gate / Step-up（Decision 0025）。Design の「またはパスワードで続行」の区切りも出さない |
| D2 | 「この端末を信頼する（30 日間）」 | 「この端末を信頼する」。Backend の `remember_me` の期間は設定値（`PAW_SESSION_REMEMBER_DAYS`、既定 90 日）で、画面に固定の日数を書かない |
| D3 | 端末追加は既存端末に表示した 6 文字のコードを新しい端末に入力する（DeviceAdd 2）。新しい端末の記録情報（OS / UA / IP / 地域） | Backend は QR / リンクの Token（`/pair#<token>`）を新しい端末で開き、Owner / Admin では新しい端末に表示した確認コードを承認する端末で入力する（Decision 0033）。コードの向きが逆。OS / UA / IP / 地域は Backend の応答にないため出さない |
| D4 | 状態変更の Request に CSRF Token、Step-up は `X-Step-Up-Token` Header（5 分・1 操作）（ApiContract） | CSRF は `SameSite=Strict` の Cookie と Origin 検査（本 Decision の 3）、Step-up は Session に結び付いた窓（Decision 0015 / 0025）。Token と Header は使わない |
| D5 | 端末一覧の「IP / 地域」「Passkey」の列（SettingsDevices） | Backend の Session の応答に IP・地域・端末ごとの Passkey がないため、「有効期限」の列にする。Passkey は別の Card で一覧する |
| D6 | 「他のすべての端末からサインアウト」は Passkey の再認証（STRONG_APPROVAL）が必要 | Backend の `POST /auth/sessions/revoke-others` は Step-up を求めないため、その説明文を出さない |
| D7 | Passkey の失敗回数と「5 回で 15 分ロック」の表示（PasskeyStates B） | Backend は失敗回数を返さない（429 と `Retry-After` だけ）。待ち時間だけを表示する |
| D8 | Header の状態 Chip（GPU / Queue、Backup 異常）、サインイン画面のシステム状態（Local LLM・GPU・Memory Backup・Codex / Claude）、`GET /api/v1/health/summary` | Backend にその API がない。Header の Chip は出さず、サインイン画面は `GET /api/v1/health/ready`（認証なし、DB の状態だけ）で「Backend」の 1 行だけを出す |
| D9 | 通知の API（`/api/v1/notifications`、既読・非表示・SSE）と通知ごとの操作ボタン（今すぐ再試行、タスクを開くなど） | Backend に通知の API がない。Shell だけを作り、Source は接続しない（本 Decision の 11）。通知ごとの操作ボタンは API が決まる Issue で足す |
| D10 | Sidebar の実行中のタスク、Menu の件数 Badge、Project の選択、Header の検索（⌘K） | Task / Project / 検索の API は後続の Issue。実行中のタスクと件数は出さず、検索欄は無効の状態で置く（Global Search と Command Palette の範囲は ApiContract でも未確定） |
| D11 | 言語と外観の設定は「このアカウントのすべての端末に適用」、タイムゾーン・日付形式・配色・情報密度の選択 | Account の設定の API がない。Theme はこの Browser に保存し、タイムゾーンは端末の値を表示だけ、日付形式・配色・情報密度は後続の Issue |
| D12 | サインイン画面の「パスワードをお忘れですか」の再設定の Flow | Backend は Owner / Admin が発行する 1 回限りの再設定（Decision 0032）だけ。その案内を表示する（Token の使用の画面は後続の Issue） |
| D13 | Mobile のサインイン画面の言語切替（日本語 / English） | SettingsLanguage の「V1 は日本語に固定」を優先して出さない。代わりに Theme の切り替えを置く |

## Human への質問

- **Q1（English）**: V1 は表示言語を日本語に固定し、英語の Catalog を画面に出さない（Design の SettingsLanguage）でよいか。ApiContract の未確定事項「English (Beta) を翻訳が揃う前から設定画面に出すか」への回答として、出す場合は設定 › 言語と外観に切り替えを戻す。
- **Q2（下部 Tab）**: スマートフォンの下部 Tab（チャット / タスク / メモリ / 通知 / 設定）を V1 に含めてよいか（Design どおりに実装済み。ApiContract では未確定）。
- **Q3（Font）**: IBM Plex Sans JP / Mono を Build に同梱する（`dist/` が約 12 MB になる）方式でよいか。代案は System Font への Fallback だけにすること。
- **Q4（Theme の保存先）**: Theme の選択を Browser ごと（`localStorage`）にしてよいか。Account 全体に適用するには Backend に Account の設定の API が要る。
- **Q5**: 上の 1–12 の推奨（配信、CSP、CSRF、npm、Biome、Node.js、Step-up、Notification、Pairing）をそのまま承認するか。

## この Issue に含めないこと

- 招待の受け取り（`POST /api/v1/auth/invitations/redeem`）、Owner の Setup / Recovery Token と Password Reset Token の使用（`POST /api/v1/auth/token/redeem`）、Password の変更の画面。Backend の API はあるが Issue #46 の受け入れ条件（login / passkey / device management）の外で、後続の Issue で足す。
- 管理画面（User の招待・削除・復元、Auth Policy、Passkey の Reset）と、Chat / Project / Agent / Memory / PR / Usage / 設定の他の項目の中身（Navigation と設定の Sidebar の行き先は「後続の Issue で実装します」の Placeholder）。
- Desktop（Tauri）と Mobile の PWA 化（Manifest、Service Worker）。
- 実際の Browser と実際の Passkey（Authenticator）を使った E2E Test。Unit Test は WebAuthn の Browser API を Fake にしている。

## 承認後の扱い

承認されたら Status を Approved にし、判断点を変える回答があれば、その点だけを実装と文書に反映する。
代案を採る場合の影響: 1 の代案 A は `PAW_WEB_DIST_DIR` を使わない運用手順を書くだけ、6 の代案（pnpm）は Lockfile と CI の導入手順の置き換え、8 の代案（ローカルでも必須）は `run_ci.py` の 1 行、10・12 は画面の変更だけで、いずれも Backend の API と Schema は変わらない。

## 承認時の決定（2026-09-29）

Human は、作業 Session で判断が必要な点について推奨つきの説明を受け、直接回答して承認した（判断が必要な点の全点）。画面は、2026-10-01 に Human が確認し、問題ないと回答した（#143 と、その上に積んだ UI の PR）。
