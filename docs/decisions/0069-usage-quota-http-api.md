# 使用状況 / Quota の HTTP API の方針（集計の数え方と期間、Workspace の User の行、認可、Quota の変更の Passkey Step-up、User の一覧）

- Status: Approved
- Date: 2026-10-01
- Scope: Issue [#187](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/187)（UI #50・PR #175 の接続先）の `apps/backend/paw_backend/api/v1/usage.py`、`connections/report.py`（集計の Read Model）、`ConnectionService.usage_report` / `workspace_usage_report`、`auth/user_directory.py`（User の一覧）、`AuthService.require_passkey_step_up`、`apps/web/src/usage/api.ts`（本番の `UsageSource`）
- Supersedes: なし。[Decision 0016](0016-shared-connection-adapter-policy.md)（Approved）の Quota の規則（未設定は無制限、期間は Asia/Tokyo の暦、指標と単位、認可）に従い、HTTP の経路と集計だけを足す（書き換えない）
- Approval: 2026-10-01、Humanが作業Session内で、判断点ごとの説明（推奨つき）を受けたうえで直接回答して承認（判断点1〜8、すべて推奨どおり。末尾の「承認時の決定」）

## 背景

UI #50（PR #175）の「管理 › 使用状況」と「設定 › 自分の使用状況」は `UsageSource` でデータを受け取り、Backend の API がないため「使用状況はまだ表示できません」を表示している。
Issue #187 は次を求める: 1) 集計 `GET /api/v1/usage?scope=self|workspace&range=last14|last30|month`、2) `GET /api/v1/quotas/me` と `GET /api/v1/users/{id}/quotas`、3) `PUT` / `DELETE /api/v1/users/{id}/quotas/{kind}/{metric}/{period}`（Passkey の Step-up つき）、4) `GET /api/v1/users`、5) Local の使用量・GPU 時間・Escalation の記録。

Quota の意味・期間・指標・認可は Decision 0016（Approved）が決めている。**次のことは要件も既存の Decision も決めていない。**
集計で何を「タスク」と数えるか、期間と「前の期間」の取り方、Workspace の表にどの User を出すか、各 Endpoint の Capability、Quota の変更の Step-up をどこで確かめるか、User の一覧の範囲。
[AGENTS.md](../../AGENTS.md) の「仕様変更」に従い、実装はこれらを下の推奨で置き、この Decision で承認を求める。

## 判断が必要な点（各点に推奨）

### 1. 集計が数えるもの（Codex / Claude の呼び出しだけ。Local は「記録なし」）

- **推奨: 集計は `connection_usage`（共有 Connection の呼び出し）だけから作る。** 「タスク」は期間内に 1 回以上呼び出した Task の数（同じ Task は 1。状態を問わず `in_flight` も数える。Decision 0016 の 5 の `tasks` 指標と同じ）、「トークン」は期間内に**始まった**呼び出しの入力 + 出力（不明は 0）。
  - Codex と Claude の両方を使った Task は、合計では 1、種類ごとには両方に 1。2 日にまたがる Task は各日に 1。
  - 用途別は Decision 0016 の 6 の閉じた Category だけ（自由な文字列は返さない）。Model 名・Prompt・応答は返さない。
- **Local の Agent は使用量を記録していない**（Issue #187 の 5。後続）。そのため Response の `tokens.local`・`gpu_seconds`・`escalations` は `null`（UI は「—」「記録なし」）、日別の `local` は 0、Agent 別に `local` の行はない（UI は 0 を表示）。
- 代替: `tasks` 表の Task を全部数える（Local だけの Task も「タスク」に入るが、Agent 別の内訳と合計が合わなくなる。Local の記録ができてから見直す）。
- **Human に確認したいこと:** Local の記録がない間、Agent 別の棒と日別のグラフで Local が 0 と表示される。**推奨: このまま（Item 5 の後続 Issue で記録を足す）。** 0 ではなく「記録なし」と出し分けたい場合は UI の型（`DailyTasks.local`）を `number | null` にする変更が要る。

### 2. 期間と「前の期間」

- **推奨:** 暦日は Quota と同じ時間帯（`Asia/Tokyo`、Decision 0016 の 4）。`last14` / `last30` は今日を含む 14 / 30 日、`month` は今月の 1 日から今日まで。日の境界は時間帯の 0 時（夏時間のある時間帯でも 0 時から 0 時）。
- 「前の期間」（UI の「前の 14 日比」「前月比」）は、`last14` / `last30` は直前の同じ日数、**`month` は前月の 1 日から今日と同じ日まで**（今日が 17 日なら前月の 1〜17 日。前月が短ければ前月末まで）。
- 代替: `month` を前月全体と比べる（月の途中では常に減って見える）。

### 3. Workspace の User の行

- **推奨:** 削除されていない User 全員（招待中・削除待ちを含む）と、期間内に呼び出しがある削除済みの User（合計に入っているため）を、Owner → Admin → User、Login 名の順に返す。各行に Task 数・Token 数・現在の Quota（`quota_status` と同じ）。**Pagination はしない**（家庭・小規模の Workspace を前提。人数が増えたら Cursor を足す）。
- 1 回の集計は 1 つの Transaction・Database の 1 つの時刻で読む（合計・日別・User 別・Quota が同じ瞬間の値）。

### 4. 認可（新しい Capability は追加しない）

- **推奨（Decision 0016 の 7 と同じ Capability）:**
  - `GET /usage?scope=self`、`GET /quotas/me`、自分の `GET /users/{id}/quotas`: `agent.use`（本人の Resource）。
  - `GET /usage?scope=workspace`、他の User の `GET /users/{id}/quotas`: `admin.usage.view`（Owner / Admin）。
  - `PUT` / `DELETE` の Quota: `admin.quota.manage`。Owner の Quota は Owner だけ（Decision 0016 の 7。既存の実装）。
  - User の一覧: `admin.users.manage`（User の管理画面のためのもの）。代替: `admin.usage.view`。
- Route の入口は `require_capability`（読み取りは全 Role が持つ `account.read`、変更と一覧は管理の Capability）で、Service が本来の Capability で認可し直して Audit する。
- 存在しない User の `GET /users/{id}/quotas` は、他の User の Quota を見てよい人にだけ 404、見てはいけない人には 403（存在を漏らさない）。

### 5. Quota の変更の Passkey Step-up

- **推奨:** `PUT` / `DELETE` は、Session（本人のもの）に認証 Policy の時間内（既定 30 分）の **Passkey の Step-up** を求める（招待・削除・端末の承認と同じ。Password の Step-up は `step_up_method_insufficient`）。
- 確かめる場所: `AuthService.require_passkey_step_up`（新しい公開 Method）が、変更の**直前に別の Transaction で**確かめる（Quota の書き込みは Connection の Store の Transaction で、認証の Transaction と一緒にできないため）。確かめた後、書き込むまでの間に Session が失効する場合は通る（数ミリ秒の差。招待などは同じ Transaction で確かめている）。
- 拒否は Audit に `connection.quota.set` / `.remove` の Deny（reason `step_up_required` / `step_up_method_insufficient`、対象の User）を書く（Best Effort）。
- 順序: Capability（403 `forbidden`）→ Step-up（403 `step_up_required`）→ Service の認可と Owner の規則 → 書き込み。

### 6. Endpoint の細部

- `PUT` の Body は `{"limit": <0 以上 10^12 以下の整数> | "unlimited"}`（`0` は新しい Task を止める。Decision 0016 の 3）。未知の Field は 422。
- **User の一覧の Path は `GET /api/v1/admin/users`（推奨）。** Issue は `GET /api/v1/users` と書いているが、`/api/v1/users` は PAW-021 の Test（`tests/test_owner_no_web_path.py`）が「初回 Setup の URL として推測される Path」として、どの Method でも 404 であることを固定している（Owner の Setup を Web に出さない、Decision 0005）。Test を緩めず、管理の経路（`/api/v1/admin/compute` と同じ `admin` の下）に置いた。
  代替: `/api/v1/users` にして、その Test の推測の一覧から外す（Setup の経路でないことは Route の一覧の Test が別に確かめている）。
- `DELETE` は、消した時は 204、その Quota が無い時は **404**（推奨。代替: 冪等に 204）。消すと、その指標・期間は無制限（Decision 0016 の 2）。
- 存在しない・削除済みの User への `PUT` は 404。
- Database がない構成は 503 `service_unavailable`。

### 7. HTTP 用の ConnectionService

- **推奨:** `create_app` が Database のある構成で、Application の Authorizer と Audit Sink を使う `ConnectionService` を 1 つ作る（`app.state.connections`）。Adapter は登録せず、Secret の Resolver はすべて拒否する（`NoSecretStore`）。この Service は Quota と使用量を読み書きするだけで、呼び出し（`execute`）はしない。
- Orchestrator が実 Adapter つきの Service を組み立てるようになったら、同じものを使うように寄せる（後続）。

### 8. Item 5（Local の使用量・GPU 時間・Escalation の記録）は後続

- **推奨:** この PR では実装しない（記録の Schema、Local Runtime と Compute Scheduler からの計上、Escalation の事象の定義が要り、Migration と Orchestrator の変更になるため）。Response には `null` の欄を置き、記録ができたら値を入れる。Escalation の数は Issue #183（System Health）とも関係する。

## 結果

- UI の `UsageSource` を本番で `/api/v1/usage` につないだ（「使用状況はまだ表示できません」ではなく実データ）。上限の変更の UI は AdminUser の画面の Issue で作る（PR #175 の確認事項 2 のとおり）。
- Migration はない。

## リスク

- 集計は呼び出しの表を期間で走査する（Workspace 全体では `user_id` の Index を使えない）。小規模の前提。大きくなったら日別の集計表を足す。
- Step-up の確認と Quota の書き込みが別の Transaction（5 を参照）。

## 承認時の決定（2026-10-01）

Human は、作業 Session で判断が必要な点について推奨つきの説明を受け、直接回答して承認した（判断点1〜8、すべて推奨どおり）。
