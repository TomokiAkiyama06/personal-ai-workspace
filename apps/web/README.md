# Web

Personal AI Workspace の Web UI です。
[PAW-060 — Web UI Application Shell / Authentication UI](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/46) で、Application Shell と認証の画面を実装しました。
言語と Framework は React + TypeScript + Vite です（[Decision 0003](../../docs/decisions/0003-backend-cli-web-implementation-stack.md)、Approved）。
配信・Session / CSRF・Tooling などの選択は [Decision 0044](../../docs/decisions/0044-web-app-serving-and-session.md)（**Proposed**。推奨どおりに実装）にまとめています。

画面の方針は [UI Design](../../docs/UI_DESIGN.md)、Backend との境界は [Architecture](../../docs/ARCHITECTURE.md)、後続の UI の Issue は [Implementation Backlog](../../docs/IMPLEMENTATION_BACKLOG.md) を参照してください。
通常の Workspace 操作は [Backend](../backend/README.md) の API（`/api/v1`）を利用し、[CLI](../cli/README.md) と共通の状態を扱います。
**権限の最終判定と強制は Backend が行います。** Web は `GET /api/v1/auth/session` の Role と Passkey の Gate を、表示の出し分け（`管理` の Navigation を Owner / Admin にだけ出すなど）にだけ使い、独自の認可規則を持ちません。Backend の 401 / 403 はそのまま Message として表示します。

## 実装した範囲

| 受け入れ条件 | 実装 |
| --- | --- |
| 日本語 Default / i18n-ready | 文言はすべて Catalog（`src/i18n/ja.ts` がキーの正本、`en.ts` は同じ型）。既定は日本語、設定の「一般」で English に切り替え（`localStorage` に保存）。日時は `Intl`。Backend のエラーは `error.code` から文言へ変換 |
| login / passkey / device management | Login（ログイン名・パスワード・端末名・ログインしたままにする。429 は待ち時間を表示）、Passkey の Gate（`enrollment_required` は登録、`assertion_required` は認証）、設定の「パスキー」（一覧・登録・削除）、「端末」（ログイン中の端末・個別 / 他のすべてのログアウト、新規端末の QR / リンクの発行と無効化、承認待ちの端末を確認 Code の入力で承認・拒否）、新しい端末の `/pair#<token>`（端末名を入れて Claim、承認待ちは確認 Code を表示して完了を待つ） |
| responsive layout | Header・Global Navigation・Main の Grid。幅 768px 以下では Navigation を Drawer にし、Header の Menu Button で開閉。Light / Dark は OS の設定に従う |
| Notification Center shell | Header の Bell（未読数）、Non-modal の一覧 Panel（Severity、同種の通知をまとめて件数表示、すべて既読）、ERROR / CRITICAL の Non-modal Banner。通知の Data は `NotificationSource` で受け、Backend に通知の API がないため今は接続していない（Decision 0044 の 11） |

Step-up が要る操作（Passkey の登録・削除、新しい端末の承認など）は、まず Request を送り、Backend が `step_up_required` / `step_up_method_insufficient` を返したときだけ、同じ画面の中に本人確認（Passkey、許されれば Password）を出して、確認後に 1 回だけ再送します。

含めていないもの（後続の Issue）: 招待の受け取り・Owner / Password Reset の Token の使用・Password の変更の画面、管理画面、Chat / Project / Agent / Memory / PR / Usage の中身（Placeholder）、PWA / Tauri、実 Browser と実 Authenticator の E2E Test。

## 構成

```text
apps/web/
├─ index.html
├─ public/favicon.svg
├─ src/
│  ├─ main.tsx / App.tsx       # Provider と、Session の状態による画面の切り替え
│  ├─ router.tsx               # History API の小さな Router
│  ├─ api/                     # fetch の Wrapper（同一 Origin、Cookie、エラー）と認証 API の型つき呼び出し
│  ├─ auth/                    # Session の状態、WebAuthn の変換、Step-up
│  ├─ i18n/                    # Catalog（ja / en）、translate、エラーの文言
│  ├─ notifications/           # Notification Center の状態と Bell / Banner
│  ├─ shell/                   # Header・Navigation・QR Code
│  ├─ pages/                   # Login、Passkey Gate、設定（一般 / パスキー / 端末）、Pairing、Placeholder
│  ├─ styles.css
│  └─ test/                    # Vitest の Setup と Test の補助（fetch の Fake など）
├─ biome.json / tsconfig.json / vite.config.ts
└─ package.json / package-lock.json / .npmrc / .node-version
```

Test は各 Module の隣の `*.test.ts(x)` です。

## 開発

Node.js は `.node-version` の Version（24 LTS）を使います。依存は `package-lock.json` に正確な Version で固定し、`.npmrc` で依存の Install Script を実行しません（`ignore-scripts=true`）。

```bash
cd apps/web
npm ci
npm run dev        # http://localhost:5173 。/api は http://127.0.0.1:8000 の Backend へ転送
npm run ci         # Biome（lint / format）、tsc、Vitest、vite build
npm run format     # Biome で整形
```

Dev Server は Browser の `Host` を変えずに `/api` を Backend へ転送するので、Session Cookie と Backend の Origin 検査は本番と同じく 1 つの Origin として動きます。
Backend は Loopback の既定（`127.0.0.1:8000`）で起動します。Passkey を試すときは Backend に `PAW_PASSKEY_RP_ID=localhost` と `PAW_PASSKEY_ORIGINS=http://localhost:5173` を設定します（`http` は `localhost` だけ許されます）。

## 配信

`npm run build` が `dist/` を作ります。Backend に `PAW_WEB_DIST_DIR=<dist の絶対 Path>` を設定すると、Backend が API と同じ Origin で配信します（Page 用の CSP、`index.html` の再検証、`assets/` の長期 Cache。Backend README の「Web App の配信」）。
Web App を別の Origin に置く構成は取りません（Session Cookie が `SameSite=Strict`、状態を変える Request は同じ Origin に限られるため）。

## CI

[run_ci.py](../../.github/scripts/run_ci.py)（pre-commit と GitHub Actions の共通の検証）が、最後に `npm ci` と `npm run ci` を実行します。
GitHub Actions では必須です。ローカルで `npm` がなければ、警告を出して Web の検証だけを Skip します（`PAW_REQUIRE_WEB_CHECKS=1` で必須にできます）。詳細は [CI](../../.github/CI.md) を参照してください。
