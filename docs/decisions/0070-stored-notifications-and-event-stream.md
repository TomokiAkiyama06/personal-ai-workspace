# 保存される通知と認証つきの Event の配信（通知の形と宛先、既読・非表示、Capability、Stream の認証と再確認、System Health からの通知、保存期間）

- Status: Approved
- Approval: 2026-10-01、Humanが作業Session内で、判断点ごとの説明（推奨つき）を受けたうえで直接回答して承認（判断点1〜10、すべて推奨どおり。末尾の「承認時の決定」）
- Date: 2026-10-01
- Scope: Issue [#188](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/188)（UI は #51 / PR #173）の `apps/backend/paw_backend/notifications/`、`/api/v1/notifications*`、`/api/v1/events/*` の認証、`paw_backend/events.py` の宛先、`health/store.py` の通知、Capability `notification.read` と `notification.manage`、Migration `0188`（`notifications`、`notification_receipts`）、`apps/web/src/notifications/serverNotifications.ts`
- Supersedes: なし。[Decision 0004](0004-rbac-capability-and-audit-policy.md)（Approved）の Capability 表と読み取り専用の許可リストに 2 つを加える（書き換えない）。[Decision 0044](0044-web-app-serving-and-session.md) の 11 / D9 で後回しにした通知の API を足す。[Decision 0059](0059-system-health-observability.md) の 6（通知は後の Issue）の続き

## 背景

[docs/NOTIFICATION_POLICY.md](../NOTIFICATION_POLICY.md) は、Notification Center が持つもの（未読件数、Severity、時刻、Source、Project / User の文脈、Message、関連する Resource、Action、既読 / 未読、非表示）、同種の通知の集約、Role ごとの配信（Owner / Admin には Backup・System Health・Security など、一般 User には自分の Task・Project・Memory と自分に影響する障害）、Action は通常の Permission / Step-up に従うこと、を決めている。UI（PR #173）は Bell・Panel・Banner・集約・Link だけの Action を実装済みで、Backend に通知の保存・既読の共有・認証つきの Event の配信がない。今の `/api/v1/events/*` は認証がなく、System Event だけを流している（その Module 自身が「非公開の Event を足す Issue は先に `require_capability` で守る」と定めている）。

**次のことは要件も既存の Decision も決めていない。** 通知をどんな形で保存するか（文か Code か）。宛先をどう表すか、Role が変わったらどうするか。既読・非表示の意味。誰がどの Capability で読む・変えるか。Stream の認証と、開いたままの Stream の Session の扱い。System Health の何を通知にするか。保存期間。一覧の Paging。
[AGENTS.md](../../AGENTS.md) の「仕様変更」に従い、実装はこれらを下の推奨で置き、この Decision で承認を求める。

## 提案

### 1. 通知は Code と数値だけを保存し、文は Web App が作る

`notifications` の行は `kind`（`system_health.component_changed` のような Code）、`params`（Code・数値・Code の List の JSON。2,000 Byte まで）、`severity`、`category`（`task` / `system`）、集約の `key`、`project_id`（文脈。外部 Key なし）、作成時刻。利用者や外部の Service の文は保存しない（[docs/OBSERVABILITY.md](../OBSERVABILITY.md) の「利用者の Private な内容を覗かない」と同じ考え）。Web App は `kind` ごとに i18n の Catalog で文にし、知らない `kind` は Code のまま出す。

### 2. 宛先は「1 人の User」か「System の Capability を持つ人」で、後者は読む時点の Role で決める

ちょうど一方を持つ（DB の CHECK）。`audience_capability` は `Scope.SYSTEM` の Capability だけ（例: System Health は `admin.system_health.view` = Owner / Admin）。読むたびに、その User の今の Role が持つ Capability（認可の Policy の純粋な判定。Audit しない）で絞るので、降格した Admin にはすぐ見えなくなり、新しい Admin には開いている通知が見える。User の行への Fan-out（Role を作成時に固定する）はしない。Project の Member 宛ては、Project の通知元ができる Issue で足す。

### 3. Capability: `notification.read`（読み取り専用）と `notification.manage`、どちらも全 Human Role・委任不可

`notification.read`（一覧と Event の Stream）は読み取り専用の許可リストに入れる（自分の通知で、中身は Code だけ）。`notification.manage`（既読・非表示）は `REQUIRED`（許可も Audit）。Passkey の Step-up は要らない（自分の見え方だけを変える）。Agent には与えない。

### 4. 既読・非表示は User ごと、非表示は「一覧の 1 項目」単位

`notification_receipts`（通知 × User）に `read_at` と `dismissed_at` を残し、端末をまたいで保つ。行がなければ未読。`POST /read` は `ids`（最大 200）か `all`、見えない ID は無視する。`POST /{id}/dismiss` は、その通知と同じ `key` のそれ以前の通知（UI がまとめて 1 項目にしているもの）を非表示かつ既読にし、より新しい通知が来れば一覧に戻る。見えない ID は 404（403 ではない。存在を漏らさない）。通知元が状態の終わりを知ったら `resolved_at` を付け、一覧から外す（`resolve_in`。System Health は使わない。6）。

### 5. Event の Stream は Session を必須にし、内容のない合図だけを宛先に送り、Session を定期的に確かめ直す

`/api/v1/events/stream` と `/ws` は `notification.read` を要求し、認証されない接続は枠を取る前に 401 / 1008。新しい Event `notification.changed` は**内容を持たず**、宛先（User の ID か、その時点の Role の Capability）の Stream にだけ届く。Client は合図を受けたら認可つきの `GET /api/v1/notifications` を読み直すので、何が読めるかは常に一覧の認可が決める。開いた Stream は `PAW_EVENT_SESSION_CHECK_SECONDS`（既定 60 秒、1〜3,600）ごとに Session を確かめ直し、**Idle の期限は延ばさない**（開いた Stream は利用者の操作ではない）。Sign-out・失効・期限切れ・User の停止・DB の無応答で Stream を終える（Fail closed。WebSocket は 1008）。Role の変更は次の確認から宛先の判定に効く。Bus はこれまでどおり Process の中だけで再送しない（Client は接続のたびに一覧を読む）。

### 6. 最初の通知元は System Health の Severity の変化（Owner / Admin 宛て）

`health_events` に変化を書く同じ Transaction で `system_health.component_changed`（Key `system_health:<component>`、Severity は変化後の値、`params` は Component・Status・前の Severity・理由の Code）を足し、Commit の後に `notification.changed` を送る。Advisory Lock の中なので、複数の Process でも 1 回だけ。Component の最初の Event が `info`（起動直後）なら通知しない。`info` へ戻ったときは前の通知を消さず、`info` の「正常に戻りました」を足す（UI の集約で 1 項目になり、最新の Severity が `info` になるので Banner は消える）。PostgreSQL が止まっていた間の変化は、戻った後にまとめて記録されるため、「止まった」と「戻った」が同時に届く。消してしまうと Admin が障害を知る手段がなくなるため、こうした。

### 7. 保存期間は作成から 90 日

System Health の Roll-up（既定 1 日 1 回相当）で、作成から 90 日を過ぎた通知を消す（既読・非表示の記録も一緒に消える）。User の削除（Erasure、Decision 0043）は、その User 宛ての通知と、その User の既読・非表示の記録を消す。

### 8. 一覧は新しい順の上限つきで、Cursor はまだ作らない

`GET /api/v1/notifications?limit=`（既定 100、最大 200。UI が持つ上限も 100）。非表示にしたものと解決したものは含めない。`unread` は一覧の外も含めた未読の総数。Severity・Project での絞り込みと Cursor は、通知が増える通知元ができてから足す。

### 9. この PR に含めないもの（後続）

- Task の `needs_human` と失敗の通知（Task の持ち主宛て）、Task の状態の HTTP の経路
- Tool の承認待ちの HTTP の経路と通知（`tools/approval_*`）
- 承認待ちの端末の通知の保存（今は UI が `GET /auth/pairing/pending` を読む）
- Header の「Backup 異常」の Chip（`GET /api/v1/system/health/summary` を UI へつなぐ）
- Project の Member 宛ての通知、通知ルール（設定画面）、UI の「一覧から非表示」の操作（Design にない）

### 10. Migration の Revision

予約された `0188`（`down_revision = 0066`）。Merge の時に鎖を付け替えてよい。Application の Role には `notifications` の SELECT・INSERT・DELETE と `UPDATE (resolved_at)`、`notification_receipts` の SELECT・INSERT と `UPDATE (read_at, dismissed_at)` だけを与える（通知の中身・宛先の書き換え、Receipt の削除・付け替えはできない）。

## 決めてほしいこと

1. **通知は Code と数値だけを保存し、文は Web App が作る**（1）でよいか。推奨: はい。文を保存する案もある（多言語にできず、外部の文が混ざる恐れがある）。
2. **宛先は 1 人の User か System の Capability の持ち主で、後者は読む時点の Role で決める**（2）でよいか。推奨: はい。作成時に User ごとへ Fan-out する案もある（降格後も見え続ける）。
3. **Capability `notification.read`（読み取り専用の許可リスト）と `notification.manage`（Audit は `REQUIRED`）を全 Human Role に与え、委任不可・Step-up なし**（3）でよいか。推奨: はい。
4. **既読・非表示は User ごとに保存し、非表示は同じ `key` のそれ以前の通知をまとめて非表示にする（新しい通知で戻る）。見えない ID は 404**（4）でよいか。推奨: はい。
5. **Event の Stream は `notification.read` を必須にし、内容のない `notification.changed` を宛先だけに送り、60 秒ごとに Idle の期限を延ばさずに Session を確かめ直して、無効なら Stream を終える**（5）でよいか。推奨: はい。Stream に通知の中身を載せる案（その都度の認可が要る）、Session を確かめ直さない案（Sign-out 後も Stream が残る）もある。
6. **System Health の Severity の変化を Owner / Admin 宛ての通知にし（同じ Transaction）、最初の `info` は通知せず、`info` へ戻ったら「正常に戻りました」を足す（前の通知は消さない）**（6）でよいか。推奨: はい。戻ったら前の通知を解決して消す案もある（PostgreSQL の停止が後から記録されたとき、Admin に届かない）。
7. **通知は作成から 90 日で消す**（7）でよいか。推奨: はい（数値は `notifications/store.py` の定数）。
8. **一覧は新しい順で最大 200 件、Cursor と絞り込みは後で足す**（8）でよいか。推奨: はい。
9. **9 の項目を後続の Issue にする**でよいか。推奨: はい。
10. **Migration の Revision を `0188` とする**（10）でよいか。推奨: はい（Merge 時に鎖を付け替えてよい）。

## 承認後の扱い

承認されたら `Approval` に記録し、Status を Approved に改める。
方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。
[REQUIREMENTS.md](../../REQUIREMENTS.md) と [docs/NOTIFICATION_POLICY.md](../NOTIFICATION_POLICY.md) の原文は書き換えない。

## 承認時の決定（2026-10-01）

Human は、作業 Session で判断が必要な点について推奨つきの説明を受け、直接回答して承認した（判断点1〜10、すべて推奨どおり）。
