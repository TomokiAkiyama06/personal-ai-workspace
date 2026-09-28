# Web

Personal AI Workspace の Web UI です。
[PAW-060 — Web UI Application Shell / Authentication UI](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/46) で、Application Shell と認証の画面を実装しました。
言語と Framework は React + TypeScript + Vite です（[Decision 0003](../../docs/decisions/0003-backend-cli-web-implementation-stack.md)、Approved）。
配信・Session / CSRF・Tooling などの選択は [Decision 0044](../../docs/decisions/0044-web-app-serving-and-session.md)（**Proposed**。推奨どおりに実装）にまとめています。

画面のレイアウト・Design Token・文言・役割ごとの Menu・Breakpoint は、Human が承認した **PAW-060 の Design Canvas**（claude.ai Artifact `BkiwvDocfuuXxfU9TA14pa`、36 Artboard、v10）に合わせています。Backend の API や承認済みの Decision と食い違う箇所は Backend / Decision を優先し、その一覧を Decision 0044 の「Design Canvas との差分」に置いています。
画面の方針は [UI Design](../../docs/UI_DESIGN.md)、Backend との境界は [Architecture](../../docs/ARCHITECTURE.md)、後続の UI の Issue は [Implementation Backlog](../../docs/IMPLEMENTATION_BACKLOG.md) を参照してください。
通常の Workspace 操作は [Backend](../backend/README.md) の API（`/api/v1`）を利用し、[CLI](../cli/README.md) と共通の状態を扱います。
**権限の最終判定と強制は Backend が行います。** Web は `GET /api/v1/auth/session` の Role と Passkey の Gate を、表示の出し分け（`管理` の Navigation を Owner / Admin にだけ出すなど）にだけ使い、独自の認可規則を持ちません。Backend の 401 / 403 はそのまま Message として表示します。

## 実装した範囲

| 受け入れ条件 | 実装 |
| --- | --- |
| 日本語 Default / i18n-ready | 文言はすべて Catalog（`src/i18n/ja.ts` がキーの正本で Design Canvas の文言、`en.ts` は同じ型）。V1 の表示言語は日本語に固定（Design の SettingsLanguage。English の Catalog は同梱するが画面に切り替えを出さない）。日時は `Intl`。Backend のエラーは `error.code` から文言へ変換 |
| login / passkey / device management | サインイン（Brand Panel とシステム状態、ユーザー名・パスワード（表示切替）・この端末を信頼する。429 は待ち時間を表示）、Passkey の Gate（`enrollment_required` は登録、`assertion_required` は確認）、設定 › 端末とセッション（信頼済み端末・個別 / 他のすべての端末からサインアウト、新しい端末を追加の QR / リンクと残り時間、承認待ちの端末を確認コードの入力と Passkey の Step-up で承認・拒否、Passkey の一覧・この端末に追加・削除）、新しい端末の `/pair#<token>`（端末名を入れて続ける、承認待ちは確認コードを表示して完了を待つ。拒否・期限切れの `invalid_token` で終える） |
| responsive layout | Design の 3 段階: 1280px 以上は Sidebar（252px）と Header の検索、768–1279px は Icon Rail（78px、短い Label）、768px 未満は Drawer（320px）と下部 Tab（チャット / タスク / メモリ / 通知 / 設定）、通知は全画面。Theme はシステム / ライト / ダーク（User Menu・言語と外観・サインイン画面。`localStorage` に保存）。Font は IBM Plex Sans JP / IBM Plex Mono を Build に同梱 |
| Notification Center shell | Header の Bell（未読数）、Non-modal の Dropdown（未読 N・すべて既読・すべて / 未読 / 重要 / タスクの絞り込み・同種の通知をまとめて件数表示・すべての通知を見る / 通知ルール）、スマートフォンでは `/notifications` の全画面、ERROR / CRITICAL の Non-modal Banner。通知の Data は `NotificationSource` で受け、Backend に通知の API がないため今は接続していない（Decision 0044 の 11） |

Navigation は Design の Roles のとおりです: 新しいチャット、チャット、プロジェクト、エージェント / タスク、メモリ、プルリクエスト、区切り、管理（Owner / Admin だけ。役割の Badge つき）、設定。User Menu はプロフィール・設定・端末とセッション（承認待ち N）・使用状況・キーボードショートカット・ヘルプ・テーマ・サインアウト・Version。設定は「ワークスペースへ戻る」の Header と、アカウント /（Owner / Admin は）ワークスペースの Sidebar です。後続の Issue の画面は Placeholder です。

Step-up が要る操作（Passkey の登録・削除、新しい端末の承認など）は、まず Request を送り、Backend が `step_up_required` / `step_up_method_insufficient` を返したときだけ、同じ画面の中に本人確認（Passkey、許されれば Password）を出して、確認後に再送します。新しい端末の承認は常に Passkey の Step-up が要るので Passkey だけを出し、Password の Step-up のあとの再送が `step_up_method_insufficient` なら Passkey だけでもう一度確認を求めます。

含めていないもの（後続の Issue）: 招待の受け取り・Owner / Password Reset の Token の使用・Password の変更の画面、管理画面、Chat / Project / Agent / Memory / PR / Usage の中身（Placeholder）、PWA / Tauri、実 Browser と実 Authenticator の E2E Test。

## 構成

```text
apps/web/
├─ index.html
├─ public/favicon.svg
├─ src/
│  ├─ main.tsx / App.tsx       # Provider と、Session の状態による画面の切り替え
│  ├─ theme.tsx                # システム / ライト / ダークの Theme（localStorage）
│  ├─ router.tsx               # History API の小さな Router
│  ├─ api/                     # fetch の Wrapper（同一 Origin、Cookie、エラー）と認証 API の型つき呼び出し
│  ├─ auth/                    # Session の状態、WebAuthn の変換、Step-up
│  ├─ i18n/                    # Catalog（ja / en）、translate、エラーの文言
│  ├─ notifications/           # Notification Center の状態と Bell / Banner
│  ├─ shell/                   # Header・Navigation（Sidebar / Rail / Drawer / 下部 Tab）・User Menu・Icon・QR Code
│  ├─ pages/                   # サインイン、Passkey Gate、設定（プロフィール / 端末とセッション / 言語と外観）、Pairing、Placeholder
│  ├─ styles.css               # Design Token（ダーク / ライトの CSS 変数）と Breakpoint
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
