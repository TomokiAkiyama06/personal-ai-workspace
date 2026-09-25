# Shared Codex / Claude Connection の Quota・Usage・Credential の方針

- Status: Proposed
- Date: 2026-09-25
- Scope: PAW-030（[#26](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/26)）と、Codex / Claude を使う以降の Issue（PAW-034 Orchestrator、PAW-022 / 023 の認証・Step-up、Admin UI）
- Supersedes: なし
- Approval: 未承認（Human の判断待ち）

## 背景

[REQUIREMENTS.md](../../REQUIREMENTS.md) は、Codex と Claude を Workspace 全体で共有する System-level Connection とし（「Shared Codex / Claude system connection」）、
次を確定している。

- Owner / Admin が接続を設定し、Credential の平文を一般 User と Agent に見せない。Credential の更新・削除は Security-sensitive な操作。
- 共有の Credential でも、利用量は User / Task 単位で記録する（request count、runtime、token / context、Task 数、失敗と Retry、Project との関係）。
- 既存の User 別 Quota はそのまま適用する。共有 Connection は Quota の共有も無制限も意味しない。
- Quota は数値のほかに **Unlimited** を選べる。Quota に達したら、**実行中の Task は原則完了させ、新規の Task だけを止める**。Owner / Admin は一時的に緩和できる。

一方で、要件は次を**決めていない**。PAW-030 の実装は、動かすためにこれらへ仮の選択を置いた。
[AGENTS.md](../../AGENTS.md) の「仕様変更」に従い、その選択を一覧にして、人間 / Admin が承認または変更できるようにする。

- Quota が未設定の User を、拒否するか、無制限とみなすか
- Quota に達した時に、実行中の呼び出し・実行中の Task がどうなるか
- 「日 / 週 / 月」の境界（暦か、直近の時間か、どの時間帯か）と、UI 設計に出る 5 時間 / 週の Window の扱い
- Quota で数える指標の種類と単位
- 利用の Category、記録に残す内容、保存期間
- Credential を扱う操作の権限（既存の Capability で足りるか）と Step-up

実装は [Backend README](../../apps/backend/README.md) の「Shared Codex / Claude Connection」に書いている。
**この Decision は未承認である。** 下の各選択は、承認されるまで暫定の実装であり、変えるときは定数・小さな関数・Test の期待値の書き換えで済むように置いてある
（Migration が要る変更は、該当の節に書いた）。

## 前提（決めたこと）

**実 Adapter は作らない。** Codex / Claude を実際に呼ぶ Adapter（CLI、API、Subscription の認証）は、この Issue に含めない。
Adapter の Interface（`ConnectionAdapter`）、登録時の検証、Credential の解決の境界だけを実装し、Test は In-memory の代役で行う。
要件も「Provider 仕様と利用規約を満たす Codex / Claude の具体的な認証実装」を実装時の選択としている。
**1 つの Subscription の Credential を複数の User が共有して使うことが、Provider の利用規約で許されるかは、確認していない**
（AGENTS.md の「最新情報」により、古い知識で断定しない）。実 Adapter を作る前に、Human が最新の規約を確認する必要がある。

## 提案

### 1. Credential の平文の置き場所

- DB の行、Audit、Log、Error、Repr、Response には、Credential の **Handle**（`cred_` + 32 桁の 16 進。[Tool Broker](0006-tool-broker-policy.md) が使う形）だけを置く。
  平文は Secret Store（製品と保存方式は要件どおり実装時の選択のまま）の Resolver の内側にだけあり、Adapter を呼ぶ 1 回の間だけ `Secret` として Adapter に渡す。この Package は Secret Store を持たない。
- Adapter の返した文は、Credential の値そのものと、形のわかる Credential（[Tool Broker](0006-tool-broker-policy.md) の検出）を取り除いてから、User / Agent に返す。
- Connection の Handle を Task の `credential_handles` に入れない（Orchestrator が TaskScope を作るときの規則。Tool の引数から Connection の Handle は使えない）。

### 2. Quota が未設定の User

- **推奨: 拒否する（Fail-closed）。** User と Connection の種類に Quota が 1 つもなければ、新しい Task の開始を `QuotaNotConfiguredError` で拒否する。
  無制限にするには、Owner / Admin が **Unlimited を明示して設定する**。最後の Quota を削除しても無制限にはならない。
  `BudgetTracker`（Task の Budget が無いと拒否する）と同じ考え方。
- 代替: 未設定を無制限とみなす（設定を忘れると無制限になる）。Workspace の既定 Quota を設ける（既定値の決定と Admin の設定が要る。Admin UI ができてから）。
- 一部の指標だけを設定してよい（要求数だけ、など）。設定した指標・期間だけを判定し、他は判定しない。

### 3. Quota に達したとき: 実行中の呼び出しと Task

- **呼び出しは途中で止めない。** Quota は呼び出しを始める前（Admission）にだけ判定する。始まった呼び出しは Quota に達しても完了し、結果を返し、使用量を記録する。
- **実行中の Task を止めない（推奨。要件のとおり）。** 判定の対象は、その Connection をまだ使っていない Task の最初の呼び出し（新規の Task）だけ。
  すでにその Connection を使った Task の以降の呼び出しは、Quota に達していても通し、記録する（超過して数える）。
  そのため Quota は「新規の Task を止める」Quota であり、実行中の Task が使う量の上限は、Task 単位の Budget（PAW-033 の `tokens`）が担う。`execute` は Task の Budget を先に確かめる。
- 代替（厳格）: すべての呼び出しで判定する。Quota に達すると、実行中の Task の次の呼び出しが拒否され、Task は途中で止まる（Waiting へ遷移して次の期間まで待つなど）。上限は正確になるが、要件の「既存 Task を強制終了しないのを基本とする」に反する。
- 代替（緩い）: 実行中の Task には上限の 1.5 倍までなど、超過の上限を設ける。数値の決定が要る。
- Owner / Admin の「一時的な上限緩和」は、期限付きの Override としては**実装していない**。Admin は `set_quota` で上限を上げ、後で戻せる。期限付き Override は後続。
- 拒否は型付きの Error（`QuotaExceededError`: 指標、期間、暦の期間なら再開時刻）で、Audit に `connection.use` の Deny（reason `quota_exceeded`）を 1 行書く。Orchestrator は、拒否するか、`resets_at` まで Task を待たせるかを選べる。
  拒否のたびに 1 行の Audit を書くので、拒否された呼び出しを繰り返すと Audit が増える。待たせる側で繰り返さないこと。

### 4. Quota の期間（Window）

- 期間は閉じた集合とする: `rolling_5h`（直近 5 時間）、`day`、`week`（月曜始まり）、`month`。1 つの指標に複数の期間を同時に設定でき、どれか 1 つでも達すれば拒否する（Metric、期間の宣言順に判定し、最初に達したものを報告する）。
- **推奨: `day` / `week` / `month` は暦の期間で、時間帯は Workspace の時間帯に設定する。** 既定の実装は UTC。
  日本語 UI を基準にする要件（「UI言語」）に合わせて、運用では `Asia/Tokyo` を推奨する（`ConnectionService(period_timezone=...)`。Migration は不要）。
  暦の期間は、切り替わりの時刻が決まっていて Admin と User に説明しやすい（`resets_at` を返せる）。
- `rolling_5h` は、UI 設計に出る 5 時間の Window に対応する。直近の窓なので、再開の時刻は 1 つに決まらない（`resets_at` は `None`）。
- **Provider 側の 5 時間 / 週の Window は扱わない。** それは共有 Subscription 自体の利用制限で、この Quota（User 別の配分）とは別である。
  Provider が制限したときは Adapter が `RATE_LIMITED` で失敗を返し、その呼び出しは失敗として記録される。Provider の残量の表示（「Account / plan label（取得可能な範囲）」）は、実 Adapter ができてから。
- 代替: `week` を「直近 7 日」にする（暦の週より Provider の見せ方に近い）。Provider の Window を User 別に按分する（Provider が残量を返すことが前提）。

### 5. 数える指標と単位

`requests`（Admission を通った呼び出しの数。失敗・取り消しも数える）、`tasks`（その期間にその Connection を使った Task の数。同じ Task の複数回は 1）、
`tokens`（Adapter が返した入力 + 出力。返さない呼び出しは 0）、`runtime_seconds`（呼び出しの時間の合計。**Database の時計**で測り、秒未満は切り捨て）。

- 判定は「使った量 >= 上限」の 1 つの比較（上限 0 はすべて拒否）。`requests` と `tasks` は厳密に守られる（同時に開始した Task が上限を超えることはない。Row Lock で直列化する）。
- `tokens` と `runtime_seconds` は、呼び出しが終わってから分かるので、終わった呼び出しの分だけを数える。同時に始まった新規の Task は、使った分だけ上限を超えうる（Task の Budget が抑える）。
  代替: Admission で `max_tokens` などを予約し、終了時に精算する（予約が残る Crash の後始末が要る）。実測で超過が問題になってから足す。
- **実装していない指標:** 同時実行数（実行中の呼び出しを Lease で管理する必要があり、Orchestrator の Worker の Lease と一緒に扱うのがよい）、GPU 時間（Cloud の Connection には無関係）、Local の指標。

### 6. 利用の記録（Attribution）と Privacy

- 記録は、User、Task、Project（Task から写す）、Connection の種類、Model 名、**用途の Category**、状態、失敗の種類、Token 数、時刻と経過時間だけ。Prompt、応答、要約は**保存しない**。
- Category は閉じた集合 `chat` / `coding` / `review` / `research` / `evaluation` / `other`（**要件は一覧を定めていないため、この Decision で提案する**）。
  要件の「非機密の短い目的要約」は保存しない（自由な文字列は漏えいの経路になる。必要になったら、Research の Privacy Filter（PAW-053）と同じ規則で別途）。
- Model 名は `[A-Za-z0-9._:/-]` の 100 文字までの名前だけを受け付ける（Routing の設定から来る値で、Prompt や Credential を紛れ込ませない）。Model の一覧との照合はしない。
- **保存期間は定めていない。** 使用量の行は削除しない（Application の Role に DELETE を与えない）。推奨: Audit と同じ長期保持とし、古い期間は集計へ置き換える（後続）。
- 呼び出しの Task は、User が作った Task で、実行中（終了していない）、かつ呼び出した Worker の Run（Attempt と Retry 回数）が現在のものであること。Project の権限と External Agent の利用権限は別に扱う（要件のとおり）ため、Project の状態は見ない。

### 7. 認可と Audit

- 新しい Capability は**追加しない**。既存の Capability を使う: 接続の管理は `admin.config.manage`、Quota の設定は `admin.quota.manage`、他の User の利用量・Quota の閲覧は `admin.usage.view`、
  自分の利用量・Quota・利用可否の閲覧と呼び出しは `agent.use`（`Scope.SELF`）。呼び出しは Orchestrator（Backend の内部）が User のために行い、Agent 自身は呼べない（`agent.use` は委任不可のまま）。
- Authorizer の行は Capability 名だけを持つため、操作の種類が分からない。そこで、変更の**結果**（`connection.connect` / `.replace` / `.enable` / `.disable` / `.disconnect` / `.quota.set` / `.quota.remove`）、
  状態の変化（`connection.status`）、呼び出しの**拒否**（`connection.use`）を、この Package が ID と Enum だけで書く。結果の行は変更の後に書く Best Effort（書けなくても変更は戻らない。拒否は拒否のまま）。
- **推奨する後続:** 操作名を持つ専用の Capability（`admin.connections.manage` など）と、状態の閲覧用の読み取り専用 Capability（`DENIED_ONLY`）を、Capability の Decision（[#82](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/82)）で加える。
  今は、利用可否の閲覧のたびに Authorizer が `REQUIRED` の Audit を 1 行書く。
- Credential の差し替えと削除は Security-sensitive な操作とされているが、Step-up（PAW-023）がまだ無いため、今は Owner / Admin の通常の認可だけで行える。
  推奨: PAW-023 の後に、差し替えと削除を Step-up の対象にする。Admin が Owner の Quota を変えられるか（Role の変更では Admin は他の Admin を管理できない）も決める。今は `admin.quota.manage` を持つ全員が全 User の Quota を変えられる。

### 8. Connection の状態と Health Check

- 状態は `connected` / `unavailable` / `expired`。新しく作った、または Credential を差し替えた Connection は、確認するまで `unavailable`。Health Check（`check_health`）が `connected` にする。
  呼び出しが Provider に「Credential が無効」と拒否されたとき（`FailureCode.EXPIRED`）は `expired` にする。それ以外の失敗（Rate Limit、Timeout など）では状態を変えない。
- Admin の「無効化」は状態とは別の `enabled` で持ち、Health Check が再有効化することはない。無効化と差し替えは、進行中の Admission の完了を待つので、返った後に新しい呼び出しは始まらない。
- User には「利用可 / 不可」だけを見せ、理由（無効、期限切れ、未設定）は見せない。
- Health Check を動かす頻度と、状態の変化を Owner へ通知する規則（[NOTIFICATION_POLICY](../NOTIFICATION_POLICY.md)）は Orchestrator / 通知の Issue。

### 9. 完了しなかった呼び出し

- Process が Admission と精算の間で落ちると、使用量の行が `in_flight` のまま残る。要求数には数えられるが、Token と時間は無い。**掃除（Reaper）は実装していない。**
  推奨: 最大の呼び出し時間より十分長く `in_flight` の行を `failed`（`internal_error`）へ変える処理を、Orchestrator の Worker の Lease と一緒に足す。
- Task の Budget への Token の加算は、精算とは別の Transaction で行う（間で落ちると加算が漏れる）。

## 選定理由

- Fail-closed と Unlimited の明示は、設定漏れが無制限の利用になる事故を避けるため（Budget と同じ規則）。
- 実行中の Task を止めない選択は、要件の「実行中Taskを原則完了させ、新規Taskのみ停止する」をそのまま実装するため。厳格な判定は、Quota の設定が変わっただけで進行中の作業が壊れる。
- 判定と記録を 1 つの Transaction にし、Quota の行を `FOR UPDATE` で Lock するのは、同時の Admission が上限を超えないようにするため。時計は Database の 1 つで、Lock を持った後に 1 回だけ読む（Decision 0007 の 10 節と同じ理由）。
- 期間を閉じた集合にするのは、Admin が自由な文字列や式を入力して判定を壊せないようにするため、また `resets_at` を計算できるようにするため。

## 代替案

各節に併記した。追加で採らなかった案:

- Credential の平文を DB に暗号化して保存する: Secret Store の製品・方式が未決で、鍵の管理が新しい Secret になる。Handle だけを持つ。
- Quota を User 単位ではなく Project 単位にする: 要件は User 別。Project 別の利用量は集計（Usage の `project_id`）で見られる。
- 呼び出しの Prompt の要約を保存して Admin の分析に使う: 要件は Raw Chat 本文を Admin に見せない。自由な文字列は保存しない。

## リスク

- Provider の規約: 共有 Subscription が許されるかは未確認（前提を参照）。許されない場合、Connection の設計（Workspace 共有）自体が見直しになる。
- 実行中の Task を止めないため、Quota は上限を超えて使われうる。Task の Budget と Admin の監視（Quota 使用率）が前提。
- `tokens` は Adapter が返す値に依存し、返さない Provider では数えられない（0 として扱う）。
- Audit の Table は削除できず、拒否の 1 行ずつの記録は量が増える（Rate Limit は PAW-022 以降）。
- Admin は Owner を含む全 User の Quota を変えられる（7 節）。

## 承認後の扱い

承認されたら、この Decision の Status を Approved にし、Backend README の「Shared Codex / Claude Connection」の「提案のまま」の記述を承認済みへ更新する。
変更を求められた選択は、次の場所で直せる: Quota が未設定の扱い、期間と時間帯（`domain.py`、`ConnectionService(period_timezone=)`）、指標の集計（`store.py` の SQL 定数）、
Category（`domain.UsagePurpose` と Migration の CHECK）、実行中の Task の扱い（`store.admit`）。Category の変更と期間の追加は、CHECK 制約を差し替える Migration が要る。
値を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。

## 決めてほしいこと

1. **Quota が未設定の User** を拒否するか（推奨: 拒否。Unlimited は明示）、無制限にするか、Workspace の既定 Quota を設けるか。
2. **Quota に達した時の実行中の Task** を、止めない（推奨。要件どおり。Task の Budget が抑える）か、厳格に止めるか、超過に上限を設けるか。期限付きの「一時 Override」を今回入れるか（推奨: 後続）。
3. **暦の期間の時間帯**（推奨: Workspace の時間帯。運用は `Asia/Tokyo`）と、`week` を暦の週（推奨）にするか直近 7 日にするか。`rolling_5h` を User 別 Quota の期間として持つか（推奨: 持つ）。Provider 側の 5 時間 / 週の Window を扱うのは実 Adapter の後でよいか（推奨: 後）。
4. **数える指標**（`requests` / `tasks` / `tokens` / `runtime_seconds`。推奨: このまま）と、`tokens` / `runtime_seconds` を終了後に数えること（推奨）、同時実行数を Orchestrator の Lease と一緒に後で入れること（推奨）。
5. **用途の Category**（`chat` / `coding` / `review` / `research` / `evaluation` / `other`。推奨: このまま）と、目的の要約を保存しないこと（推奨）、使用量の**保存期間**（推奨: Audit と同じ長期保持）。
6. **認可:** 既存の Capability を使うこと（推奨: 今回はこのまま）、専用の Capability と閲覧用の DENIED_ONLY の Capability を #82 で加えること（推奨）、Credential の差し替え・削除を Step-up の対象にすること（推奨: PAW-023 の後）、Admin が Owner の Quota を変えられるか（推奨: Owner の Quota は Owner だけ）。
7. **Provider の利用規約の確認**を、実 Adapter の実装の前提条件にすること（推奨: はい。担当と時期を決める）。
8. **完了しなかった呼び出しの掃除**を、Orchestrator の Issue（PAW-034）の受け入れ条件に置くこと（推奨: はい）。
