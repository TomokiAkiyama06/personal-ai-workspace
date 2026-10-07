# System Health UI の方針（Monitoring Board と Backend の Component の対応、通常時の Compact と異常時の展開、Header の状態 Chip の出し分け、更新の間隔、通知からの導線）

- Status: Approved
- Approval: 2026-10-08、Humanが作業Session内で、判断点ごとの説明（推奨つき）を受けたうえで直接回答して承認（すべての判断点を推奨どおり承認。末尾の「承認時の決定」）
- Date: 2026-10-07
- Scope: PAW-067（[#53](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/53)）の `apps/web/src/health/`（`管理 › サーバー監視` の `MonitoringView`、Header の `HealthChip`、`HealthSource` と本番の `apiHealthSource`）、`apps/web/src/notifications/serverNotifications.ts`（System Health の通知の文言と Link）
- Supersedes: なし。[Decision 0059](0059-system-health-observability.md)（Approved）の API・Capability・閾値、[Decision 0070](0070-stored-notifications-and-event-stream.md) の通知に従い、画面の見せ方だけを決める（書き換えない）。[Decision 0044](0044-web-app-serving-and-session.md) の「Design Canvas との差分」D8（Header の状態 Chip を出さない）は、API ができたのでこの Decision の 4 で埋める

## 背景

Issue #53 の受け入れ条件は「normal 時は compact」「abnormal 時に詳細展開」「VRAM / queue / recovery / external service 状態」「Warning / Error / Critical 通知へ連携」。
[docs/UI_DESIGN.md](../UI_DESIGN.md) の「System Health / Observability」（FIXED BASELINE）は、通常時は Header 等に compact な health state だけを出し、詳細画面で GPU / VRAM・Model residency・Task Queue・Waiting for Resource・PostgreSQL・Recovery Repository・External Agent / Provider・recent failure / retry / OOM を確認できることを求める。
Human が承認した PAW-060 の Design Canvas には、`管理 › サーバー監視` の **Monitoring Board** と、ShellDark の Header の状態 Chip（`● GPU 38% · Queue 2`）がある。

Backend は PAW-066（Decision 0059、Approved）の `/api/v1/system/health*` で、9 つの Component（`database`・`compute`・`task_queue`・`memory_worker`・`connections`・`connection_reaper`・`recovery_backup`・`memory_projection`・`audit_retention`）の Severity・Status・理由の Code・数値、指標の時系列、Severity の変化（Event）を返す。通知は Decision 0070 が `system_health.component_changed` として保存し、Notification Center が表示している。

**次のことは要件も既存の Decision も Design も決めていない。**
Monitoring Board は「監視サービス（monitoring リポジトリ）」の Host（gpu01 / nas01 / rpi-sensor）と温度・ファン・消費電力を描いているが、Backend にはその Source がない（Decision 0059 の 1 にない）。そのため Board の各部分に Backend の何を入れるか。Compact と展開の具体的な規則。Header の Chip を一般の User にも出すか（Design の ApiContract Board が未決の問いとして挙げている）。更新の間隔。
[AGENTS.md](../../AGENTS.md) の「仕様変更」に従い、実装はこれらを下の推奨で置き、この Decision で承認を求める。

## 判断が必要な点（各点に推奨）

### 1. Monitoring Board の各部分と Backend の対応

Board の Layout（見出しの行・Banner・Chip の行・4 枚の Card・推移のグラフ・直近のアラート・右の詳細 Panel・読み取り専用の注記）はそのまま使い、中身を次のように Backend の値に置き換える。

| Board | この実装 |
| --- | --- |
| 「監視サービス（monitoring リポジトリ）から取得」 | 「System Health（Backend の監視）から取得」 |
| Host の Chip（gpu01 / nas01 / rpi-sensor） | UI_DESIGN.md の 5 つの領域: **GPU / VRAM**（`compute`）・**タスクキュー**（`task_queue`・`memory_worker`）・**PostgreSQL**（`database`）・**Recovery Repository**（`recovery_backup`・`memory_projection`・`audit_retention`）・**外部エージェント**（`connections`・`connection_reaper`）。点は領域の最悪の Severity、右は短い値（VRAM 38%・2 実行 / 1 待機・4 ms・2 / 2 利用可、異常なら Status） |
| Card: GPU 温度 / CPU 温度 / 室温 / 受信 | **VRAM**（使用率、使用 / 合計 GB・予約）・**GPU 使用率**（GPU 上のモデル数）・**タスクキュー**（実行・待機・Resource 待ち）・**受信**（この画面の読み込みの成否と失敗数） |
| 温度の推移（GPU / CPU / 室温、警戒 80 の線） | **GPU / VRAM の推移**（%）: GPU 使用率、VRAM 使用、VRAM 予約（合計に対する割合）。`compute.*` の時系列を期間ごとに約 120 点で読む。閾値の線は出さない（VRAM の圧迫は Scheduler の判定で、固定の % がない） |
| 直近のアラート（しきい値の説明と「しきい値を変更」） | `GET /system/health/events` の Severity の変化（「Backup が ERROR になりました · 続けて失敗しています」「… は正常に戻りました」）。閾値は Decision 0059 の 2 の定数なので「しきい値を変更」は出さない |
| 右の Panel（Host の使用率・ファン・電力・稼働時間・常駐モデル、ワークスペース側の 4 行） | 選んだ領域の詳細: GPU / VRAM では GPU 使用率・VRAM 使用・VRAM 予約の Meter、Lease・待ち・VRAM 待ち・常駐モデル、モデルの一覧（役割・名前・GPU / CPU / 未ロード / 失敗）。どの領域でも、その Component ごとの 1 行（点・名前・Status）と、理由と数値の詳細 |
| 「電源操作やファン制御はこの画面から行いません」 | 「モデルの Load / Unload や GPU・サービスの操作はこの画面から行いません」（Decision 0059 の GPU の安全性） |

- 温度・ファン・電力・稼働時間・NAS・室温センサーは出さない。Backend に Source を足すなら別の Issue と Decision で決める。
- 代替: Board の温度の欄を「—」で残す案もあるが、存在しない値の欄が並ぶので推奨しない。
- **推奨: 上の対応のとおり。**

### 2. 通常時は Compact、異常時は自動で展開

- **通常時**（全体が `info`）: Banner の代わりに「すべて正常です · 9 項目を監視中」の 1 行。Component の行は名前と Status だけで、詳細は閉じる（手で開ける）。
- **異常時**（どれかが `warning` 以上）: 最悪の Component の Banner（Severity・「Backup: 続けて失敗しています」・「ほか N 件」。`task_queue` なら「タスクを見る」）。最悪の領域を自動で選び、異常な Component の詳細（理由と数値）を自動で開く。新しい異常が最悪になったら選び直す。回復したら閉じる。
- 色だけで伝えない: 点の隣に必ず文字（正常 / 警告 / 異常 / 重大、Status、Severity の英字）を置く。
- 代替: 異常な Component だけを一覧に出し、正常なものは隠す案。全体の様子が見えなくなるので推奨しない。
- **推奨: 上のとおり。**

### 3. 文言は Code から作り、知らない Code はそのまま出す

- 理由の Code（`vram_pressure`、`expired:claude`、`model_failed:main` など）・Status・Component・Role は i18n の Catalog で文にする。この Version が知らない Code は Code のまま出す（隠さない、推測しない）。
- Job の `last_failure`（`push:GitCommandError` のような `<段階>:<例外の型>`）は段階だけを出す（「失敗した段階 push」）。
- 通知（Decision 0070）の本文と詳細も同じ Catalog で文にする（「状態 異常 · 以前の Severity WARNING」「Claude に接続できません」）。
- **推奨: 上のとおり。**

### 4. Header の状態 Chip の出し分け（Decision 0044 の D8 を埋める）

- **Owner / Admin**（`admin.system_health.view`）: 通常時は ShellDark のとおり `GPU 38% · Queue 3`（GPU 使用率と、実行中 + キュー待ちのタスク数）。異常時は最悪の Component を「Backup 異常」「Backup 異常 +1」と出し、点と枠を Severity の色にする。押すと `管理 › サーバー監視`。
- **一般の User**（`system_health.summary.read`）: Decision 0059 の 3 の Summary だけ。点は全体の Severity、文は「Codex · Claude 利用可」か「Claude 利用不可」。押しても何も開かない（詳細を読む権限がない）。
- スマートフォン（768px 未満）では点だけを出す（文は読み上げ用に残す）。
- 読めないとき（Source がない・拒否・失敗）は Chip を出さない（前に読めた状態は残す）。
- 代替: 一般の User には Chip を出さない案（ApiContract Board の未決の問い）。UI_DESIGN.md の「一般 User には `Claude: Available / Unavailable`」を満たす場所が他にないので推奨しない。
- **Human に確認したいこと:** 「Queue N」の N を「実行中 + キュー待ち」とした（Board の「2 実行 / 1 待機」なら 3）。**推奨: このまま。** 実行中だけ、または待機（承認・Resource 待ち）も含める案もある。

### 5. 更新の間隔

- サーバー監視の画面: Board のとおり 15 秒ごとに `GET /system/health`（Backend は 10 秒の Cache を持つ）。推移とアラートは期間を変えたときと 60 秒ごと。Tab が隠れている間は読まない。失敗しても前の表示を残し、「受信」の Card を「途切れています · 失敗 N」にする。
- Header の Chip: 30 秒ごとと、Tab が見えるようになったとき。
- 通知（Decision 0070）の Event Stream の `notification.changed` で読み直す案もあるが、通知は Severity の変化だけで、数値の更新には足りないので Poll にした。
- **推奨: 上のとおり。**

### 6. 通知からの導線（Warning / Error / Critical）

- System Health の通知の「詳細を見る」は `管理`（概要の Placeholder）ではなく `管理 › サーバー監視` を開く。開くと 2 のとおり異常な Component の詳細が開いている。
- 通知の Banner（ERROR / CRITICAL）と画面の Banner は両方出る（前者は Notification Center の既読の管理、後者はこの画面の現在の状態）。
- **推奨: 上のとおり。**

## 含めないもの（後続）

- サインイン画面のシステム状態（Decision 0044 の D8 の後半）: `GET /system/health/summary` は認証が要るので、今の `GET /api/v1/health/ready` のままにする。
- 閾値の変更（Decision 0059 の 2 は定数）、温度などの Host の Source（1）。
- `管理 › 概要` の Health の Card（Admin Board）: 概要の Issue で、この画面の部品を使って作る。

## 承認後の扱い

承認されたら `Approval` に記録し、Status を Approved に改める。方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。

## 承認時の決定（2026-10-08）

Human は、作業 Session で判断が必要な点について推奨つきの説明を受け、直接回答して承認した（すべての判断点を推奨どおり承認）。
