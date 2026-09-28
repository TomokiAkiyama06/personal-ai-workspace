# Web App の配信・Session / CSRF・Frontend の Tooling の方針

- Status: Proposed
- Date: 2026-09-28
- Scope: PAW-060（Web UI Application Shell / Authentication UI、Issue [#46](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/46)）の `apps/web` と、それを配信する `apps/backend/paw_backend/web.py`。後続の Web の Issue（PAW-061 以降）が同じ前提を使う
- Supersedes: なし。[Decision 0003](0003-backend-cli-web-implementation-stack.md) の表のうち「実装Issueが選ぶ既定値」（pnpm、Web の Lint / Format Tool）を、0003 が認める範囲で理由を付けて選び直す（0003 の承認範囲の React + TypeScript + Vite は変えない）
- Approval: 未承認（Human の回答待ち）

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

一方で、**次のことは要件・Backlog・既存の Decision のどれも決めていない。**
Web App をどこからどう配信するか（同じ Origin か、別の Origin + CORS か）、Page の CSP と Cache、Session と CSRF の扱いを Web 側で足すか、Client 側の Routing、Package 管理・Lint / Format・Test の Tool と CI での扱い、i18n の仕組み、Step-up の画面の出し方、Notification Center の Data の出どころ。
[AGENTS.md](../../AGENTS.md) の「仕様変更」に従い、実装（この Issue の PR）はこれらを下の推奨で置き、この Decision で承認を求める。数値と選択の多くは設定か定数で、変えても Schema は変わらない（Migration はない）。

## 提案

### 1. Web App は Backend が API と同じ Origin で配信する

- **推奨**: Build 済みの `apps/web/dist` を Backend（Uvicorn）が配信する（`PAW_WEB_DIST_DIR` に絶対 Path を設定したときだけ。未設定では従来どおり API だけ）。`/api` の外への `GET` / `HEAD` だけを扱い、Build の File、Client 側の Route（最後の Segment に `.` がない Path）には `index.html` を返す。`/api/*` と状態を変える Method は従来どおり API に渡す。`.` で始まる Segment、Directory の外へ出る Path（`..`、Symlink）は配信しない。
- 理由: Session Cookie（`__Host-`、`SameSite=Strict`）と Origin 検査は同じ Origin を前提にしている。Backend が配信すれば、TLS を Uvicorn が終端する構成（`PAW_TLS_CERTFILE`）でも Reverse Proxy（`tailscale serve`、Caddy）が終端する構成でも、追加の設定なしで同じ Origin になる。Page の Security Header も Backend の 1 か所で決まる。
- 代案 A: Reverse Proxy が `dist` を配信し、`/api` を Backend へ転送する。同じ Origin にはなるが、Proxy がない構成（Uvicorn が TLS を終端）で配信する手段がなく、CSP などの Header を Proxy ごとに書くことになる。この推奨でも、Proxy で配信したい運用はそのまま取れる（`PAW_WEB_DIST_DIR` を設定しない）。
- 代案 B（取らない）: 別の Origin に置き、CORS で API を呼ぶ。`SameSite=Strict` の Cookie は Cross-site の Request に付かず、Origin 検査も拒否するため、Session の方式（Decision 0015）から変える必要がある。
- 開発時は Vite の Dev Server（`http://localhost:5173`）が `/api` を Backend（`http://127.0.0.1:8000`）へ転送する。Browser の `Host` を変えない（`changeOrigin: false`）ので、Cookie と Origin 検査は本番と同じく 1 つの Origin として動く。

### 2. Page の CSP と Cache

- **推奨**: Web App の Page には API とは別の CSP を付ける: `default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'`。Inline Script / Inline Style は許さない（Vite の Build は `<script src>` と `<link>` だけを出す。`assetsInlineLimit: 0`）。外部の CDN・Font は使わない。API の Response の CSP（`default-src 'none'; frame-ancestors 'none'`）は変えない。その他の Header（`X-Frame-Options: DENY`、`nosniff`、`Referrer-Policy: no-referrer`、HSTS）は API と同じ。
- **推奨**: `index.html` などは `Cache-Control: no-cache`（毎回再検証。更新がすぐ届く）、Content Hash つきの `assets/*` は `public, max-age=31536000, immutable`。

### 3. Session / CSRF は既存の仕組みに依拠し、Web 側に CSRF Token を足さない

- **推奨**: Web App は `fetch` を `credentials: "same-origin"`、`redirect: "error"`、`cache: "no-store"` で送るだけにし、CSRF Token（Double Submit Cookie、Header Token）は足さない。CSRF は既存の 2 層（`SameSite=Strict` の Cookie と、状態を変える Request の Origin 検査）で防ぐ。Session の識別子は `HttpOnly` のままで、JavaScript からは読めない。
- **推奨**: Web App は Credential・Token・Session の情報を `localStorage` / `sessionStorage` に保存しない（保存するのは表示言語の選択だけ）。Pairing の Token は Fragment から読んだ直後に Address Bar と履歴から消す（`history.replaceState`）。
- 代案: 追加の CSRF Token を導入する。現在の 2 層で防げない攻撃（同じ Site の別の Origin からの Request）は、Origin 検査が Origin の完全一致で拒否するため、得るものがない。Backend の変更（Token の発行・検証）も要る。

### 4. 権限の最終判定は Backend だけが行い、Web は表示を選ぶだけにする

- **推奨**: Web は `GET /api/v1/auth/session` の `system_role` と Passkey の `gate` を、表示の出し分け（`Admin` の Navigation を Owner / Admin にだけ出す、Gate が `open` でない Session に Passkey の画面だけを出す）にだけ使う。Web に独自の認可規則を持たず、Backend の 401 / 403 はそのまま Message として出す（401 は Login 画面へ戻す）。これは [apps/web/README.md](../../apps/web/README.md) と Decision 0003 の方針の確認で、新しい判断ではない。

### 5. Client 側の Routing は History API と Backend の Fallback で行う

- **推奨**: Router の Library を使わず、History API の小さな Router（`src/router.tsx`）で固定の Path（`/`、`/projects`、`/agents`、`/memory`、`/pulls`、`/usage`、`/admin`、`/settings`、`/settings/security`、`/settings/devices`、`/pair`）を扱う。Backend は Client 側の Route に `index.html` を返す（1）。
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

- **推奨**: i18n の Library を使わず、日本語の Catalog（`src/i18n/ja.ts`）をキーの正本にし、他の言語はその型に合わせる（キーの漏れは型検査と Test が検出する）。V1 は日本語が既定で、英語の Catalog も同梱し、設定画面で切り替えられる（選択は `localStorage` に保存。使えなければ保存しないだけ）。日時は `Intl.DateTimeFormat` で Locale に合わせる。Backend のエラーは安定した `error.code` から Catalog の文言に変換する（Backend の英語の `message` は表示しない）。
- 理由: [UI Design](../UI_DESIGN.md) の `[FIXED]`「V1 の基本 UI 言語は日本語」「i18n-ready」を満たし、依存を増やさない。複数形・性などの複雑な規則が要る言語を足す時点で、ICU MessageFormat を持つ Library を検討する。

### 10. Step-up は「まず操作し、要求されたら同じ画面の中で確認する」

- **推奨**: Step-up が要る操作（Passkey の登録・削除、Owner / Admin の新しい端末の承認など）は、まず Request を送り、Backend が `step_up_required` / `step_up_method_insufficient` を返したときだけ、同じ画面の中（Modal ではない）に本人確認の欄を出す。Passkey が登録済みで使えるなら Passkey の確認を出し、`step_up_method_insufficient` でなければ Password の確認も出す。確認が済めば元の操作を 1 回だけ再送する。取り消せば何もしない。
- 理由: Step-up の窓（Decision 0015 / 0025）の中なら確認を求めずに済み、要否の判断を Backend だけに置ける。UI Design の「通常操作では画面を不要な Modal で遮らない」に合わせる。

### 11. Notification Center はこの Issue では Shell だけにする

- **推奨**: Header の Bell（未読数の Badge）、一覧の Panel（Non-modal。Severity の表示、同じ種類の通知をまとめて件数を出す、すべて既読）、ERROR / CRITICAL の Non-modal Banner（閉じられる）を実装する。通知の Data は `NotificationSource` の Interface で受け、**この Issue では Source を接続しない**（Backend に通知の API がまだない。Event の経路 `/api/v1/events/*` は認証がなく System Event だけを流すと Backend が定めている）。CRITICAL の Modal（UI Design の「必要に応じ」）は作らない。
- 通知の API（保存・既読・Event の配信と認可）は別の Issue で扱う。そのとき Source を 1 つ足せば、この Shell がそのまま使える。

### 12. Pairing の画面の待ち方

- **推奨**: 新しい端末は、承認待ちのあいだ確認 Code を大きく表示し、`POST /api/v1/auth/pairing/complete` を **3 秒ごと**に呼ぶ（正しい Claim は Rate Limit の試行を返すので、待つ端末は Lock されない。429 のときは `Retry-After` だけ待つ。404 は期限切れか拒否として終わる）。承認する信頼済み端末は、QR を表示して承認が要る（`approval_required`）あいだ、承認待ちの一覧を **5 秒ごと**に読み直す。承認は確認 Code の入力を必須にする（Decision 0033 の 12）。
- QR Code は `uqr`（依存のない小さな Library、正確な Version で固定）で行列を作り、SVG の Path として描く（`innerHTML` を使わない）。QR の中身は `location.origin` と Backend の `link_path` をつないだ URL。

## この Issue に含めないこと

- 招待の受け取り（`POST /api/v1/auth/invitations/redeem`）、Owner の Setup / Recovery Token と Password Reset Token の使用（`POST /api/v1/auth/token/redeem`）、Password の変更の画面。Backend の API はあるが Issue #46 の受け入れ条件（login / passkey / device management）の外で、後続の Issue で足す。
- 管理画面（User の招待・削除・復元、Auth Policy、Passkey の Reset）と、Chat / Project / Agent / Memory / PR / Usage の中身（Navigation の行き先は「後続の Issue で実装します」の Placeholder）。
- Desktop（Tauri）と Mobile の PWA 化（Manifest、Service Worker）。
- 実際の Browser と実際の Passkey（Authenticator）を使った E2E Test。Unit Test は WebAuthn の Browser API を Fake にしている。

## 承認後の扱い

承認されたら Status を Approved にし、判断点を変える回答があれば、その点だけを実装と文書に反映する。
代案を採る場合の影響: 1 の代案 A は `PAW_WEB_DIST_DIR` を使わない運用手順を書くだけ、6 の代案（pnpm）は Lockfile と CI の導入手順の置き換え、8 の代案（ローカルでも必須）は `run_ci.py` の 1 行、10・12 は画面の変更だけで、いずれも Backend の API と Schema は変わらない。
