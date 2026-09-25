# Shared Codex / Claude Connection の Quota・Usage・Credential の方針

- Status: Approved
- Date: 2026-09-25
- Scope: PAW-030（[#26](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/26)）と、Codex / Claude を使う以降の Issue（PAW-034 Orchestrator、PAW-022 / 023 の認証・Step-up、Admin UI）
- Supersedes: なし
- Approval: 2026-09-26、Humanが作業Session内で、判断メモの各点に個別に回答し、残りは「推奨どおり」と回答して承認（末尾の「承認時の決定」）

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
**この Decision は 2026-09-26 に Human が承認した（Approved）。** 下の各選択は、承認された方針である。
ただし 2 節（Quota が未設定の User）と 4 節（暦の期間の既定の時間帯）は、実装が置いた推奨案から、Human が選んだ案へ置き換えた（末尾の「承認時の決定」）。
変えるときは定数・小さな関数・Test の期待値の書き換えで済むように置いてある（Migration が要る変更は、該当の節に書いた）。

## 前提（決めたこと）

**実 Adapter は作らない。** Codex / Claude を実際に呼ぶ Adapter（CLI、API、Subscription の認証）は、この Issue に含めない。
Adapter の Interface（`ConnectionAdapter`）、登録時の検証、Credential の解決の境界だけを実装し、Test は In-memory の代役で行う。
要件も「Provider 仕様と利用規約を満たす Codex / Claude の具体的な認証実装」を実装時の選択としている。
**1 つの Subscription を複数の User が共有して使うことが、Provider の利用規約で許されるか**は、AGENTS.md の「最新情報」により古い知識で断定せず、実 Adapter を作る前に Human が確認する前提条件とした。
**Human は 2026-09-26 に、共有 Subscription の利用が問題ないことを確認した**（「問題ないことを確認しています」）。この前提条件は満たされた。
実 Adapter は別の Issue で作る（Secret Store と Network の Policy が決まってから）。この Issue は Interface と Quota・帰属だけを実装する。

## 決定

### 1. Credential の平文の置き場所

- DB の行、Audit、Log、Error、Repr、Response には、Credential の **Handle**（`cred_` + 32 桁の 16 進。[Tool Broker](0006-tool-broker-policy.md) が使う形）だけを置く。
  平文は Secret Store（製品と保存方式は要件どおり実装時の選択のまま）の Resolver の内側にだけあり、Adapter を呼ぶ 1 回の間だけ `Secret` として Adapter に渡す。この Package は Secret Store を持たない。
- Adapter の返した文は、Credential の値そのものと、形のわかる Credential（[Tool Broker](0006-tool-broker-policy.md) の検出）を取り除いてから、User / Agent に返す。
- Connection の Handle を Task の `credential_handles` に入れない（Orchestrator が TaskScope を作るときの規則。Tool の引数から Connection の Handle は使えない）。

### 2. Quota が未設定の User

- **未設定は無制限（Human の選択）。** Quota の行が無い User・指標・期間・Connection の種類は、判定しない。設定した上限は従来どおり判定し、`UNLIMITED` の明示も有効である。
  最後の Quota を削除すると、その User はその種類で無制限に戻る。一部の指標だけを設定してよい（要求数だけ、など）。設定した指標・期間だけを判定し、他は判定しない。
- 無制限でも、呼び出しは使用量の行として User と Task に帰属し、Audit（Authorizer の `agent.use`）に残る。
- **最初の実装は、これとは逆だった。** Quota が 1 つも無い User の新しい Task を `QuotaNotConfiguredError`（reason `quota_not_configured`）で拒否していた（推奨案。`BudgetTracker` と同じ Fail-closed）。
  Human が「未設定は無制限」を選んだため、この契約、その Error と理由、拒否を固定していた Test を、承認された契約に置き換えた（削除ではなく置換。README と commit message に記録）。
- 代替（採らなかった）: 未設定を拒否する（設定漏れが無制限の利用になる事故は防げるが、全 User に Quota を設定するまで使えない）。Workspace の既定 Quota を設ける（既定値の決定と Admin の設定が要る。後続で足せる）。

### 3. Quota に達したとき: 実行中の呼び出しと Task

- **呼び出しは途中で止めない。** Quota は呼び出しを始める前（Admission）にだけ判定する。始まった呼び出しは Quota に達しても完了し、結果を返し、使用量を記録する。
- **実行中の Task を止めない（要件のとおり。承認）。** 判定の対象は、その Connection をまだ使っていない Task の最初の呼び出し（新規の Task）だけ。
  すでにその Connection を使った Task の以降の呼び出しは、Quota に達していても通し、記録する（超過して数える）。
  そのため Quota は「新規の Task を止める」Quota であり、実行中の Task が使う量の上限は、Task 単位の Budget（PAW-033 の `tokens`）が担う。`execute` は Task の Budget を先に確かめる。
- 代替（厳格）: すべての呼び出しで判定する。Quota に達すると、実行中の Task の次の呼び出しが拒否され、Task は途中で止まる（Waiting へ遷移して次の期間まで待つなど）。上限は正確になるが、要件の「既存 Task を強制終了しないのを基本とする」に反する。
- 代替（緩い）: 実行中の Task には上限の 1.5 倍までなど、超過の上限を設ける。数値の決定が要る。
- Owner / Admin の「一時的な上限緩和」は、期限付きの Override としては**実装していない**。Admin は `set_quota` で上限を上げ、後で戻せる。期限付き Override は後続。
- 拒否は型付きの Error（`QuotaExceededError`: 指標、期間、暦の期間なら再開時刻）で、Audit に `connection.use` の Deny（reason `quota_exceeded`）を 1 行書く。Orchestrator は、拒否するか、`resets_at` まで Task を待たせるかを選べる。
  拒否のたびに 1 行の Audit を書くので、拒否された呼び出しを繰り返すと Audit が増える。待たせる側で繰り返さないこと。

### 4. Quota の期間（Window）

- 期間は閉じた集合とする: `rolling_5h`（直近 5 時間）、`day`、`week`（月曜始まり）、`month`。1 つの指標に複数の期間を同時に設定でき、どれか 1 つでも達すれば拒否する（Metric、期間の宣言順に判定し、最初に達したものを報告する）。
- **`day` / `week` / `month` は暦の期間で、既定の時間帯は `Asia/Tokyo`（Human の選択。最初の実装の既定は UTC だった）。** `ConnectionService(period_timezone=...)` で変えられる（Migration は不要）。
  `week` は暦の週（月曜始まり）。暦の期間は、切り替わりの時刻が決まっていて Admin と User に説明しやすい（`resets_at` を返せる）。
  名前つきの時間帯は OS の時間帯 Database（`tzdata`）を使う。無い Host では、Service の生成時に `InvalidConnectionInputError` になる（`"UTC"` は不要）。
- `rolling_5h` は、UI 設計に出る 5 時間の Window に対応する。直近の窓なので、再開の時刻は 1 つに決まらない（`resets_at` は `None`）。
- **Provider 側の 5 時間 / 週の Window は扱わない。** それは共有 Subscription 自体の利用制限で、この Quota（User 別の配分）とは別である。
  Provider が制限したときは Adapter が `RATE_LIMITED` で失敗を返し、その呼び出しは失敗として記録される。Provider の残量の表示（「Account / plan label（取得可能な範囲）」）は、実 Adapter ができてから。
- 代替（採らなかった）: `week` を「直近 7 日」にする（暦の週より Provider の見せ方に近い）。時間帯を UTC にする（最初の実装）。Provider の Window を User 別に按分する（Provider が残量を返すことが前提）。

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
- **保存期間は定めていない。** 使用量の行は削除しない（Application の Role に DELETE を与えない）。承認: Audit と同じ長期保持とし、古い期間は集計へ置き換える（後続の Issue）。
- 呼び出しの Task は、User が作った Task で、実行中（終了していない）、かつ呼び出した Worker の Run（Attempt と Retry 回数）が現在のものであること。Project の権限と External Agent の利用権限は別に扱う（要件のとおり）ため、Project の状態は見ない。

### 7. 認可と Audit

- 新しい Capability は**追加しない**。既存の Capability を使う: 接続の管理は `admin.config.manage`、Quota の設定は `admin.quota.manage`、他の User の利用量・Quota の閲覧は `admin.usage.view`、
  自分の利用量・Quota・利用可否の閲覧と呼び出しは `agent.use`（`Scope.SELF`）。呼び出しは Orchestrator（Backend の内部）が User のために行い、Agent 自身は呼べない（`agent.use` は委任不可のまま）。
- Authorizer の行は Capability 名だけを持つため、操作の種類が分からない。そこで、変更の**結果**（`connection.connect` / `.replace` / `.enable` / `.disable` / `.disconnect` / `.quota.set` / `.quota.remove`）、
  状態の変化（`connection.status`）、呼び出しの**拒否**（`connection.use`）を、この Package が ID と Enum だけで書く。結果の行は変更の後に書く Best Effort（書けなくても変更は戻らない。拒否は拒否のまま）。
- **後続（承認）:** 操作名を持つ専用の Capability（`admin.connections.manage` など）と、状態の閲覧用の読み取り専用 Capability（`DENIED_ONLY`）を、Capability の Decision（[#82](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/82)）で加える。
  今は、利用可否の閲覧のたびに Authorizer が `REQUIRED` の Audit を 1 行書く。
- Credential の差し替えと削除は Security-sensitive な操作とされているが、Step-up（PAW-023）がまだ無いため、今は Owner / Admin の通常の認可だけで行える。
  承認: PAW-023 の後に、差し替えと削除を Step-up の対象にする（後続の Issue）。
- **Owner の Quota は Owner だけが変えられる（承認。実装済み）。** Role の変更で Admin が Owner を管理できないのと同じ規則。`set_quota` / `remove_quota` は、対象の Role を Store から読み（呼び出し側の申告は使わない）、
  Owner で、呼んだ人が Owner でなければ、`connection.quota.set` / `.remove` の Deny（reason `owner_quota_owner_only`）を書いて `CAPABILITY_NOT_GRANTED` で拒否する。Owner 自身、Owner による Admin と User の Quota、Admin による Admin と User の Quota は変えられる。

### 8. Connection の状態と Health Check

- 状態は `connected` / `unavailable` / `expired`。新しく作った、または Credential を差し替えた Connection は、確認するまで `unavailable`。Health Check（`check_health`）が `connected` にする。
  呼び出しが Provider に「Credential が無効」と拒否されたとき（`FailureCode.EXPIRED`）は `expired` にする。それ以外の失敗（Rate Limit、Timeout など）では状態を変えない。
- Admin の「無効化」は状態とは別の `enabled` で持ち、Health Check が再有効化することはない。無効化と差し替えは、進行中の Admission の完了を待つので、返った後に新しい呼び出しは始まらない。
- User には「利用可 / 不可」だけを見せ、理由（無効、期限切れ、未設定）は見せない。
- Health Check を動かす頻度と、状態の変化を Owner へ通知する規則（[NOTIFICATION_POLICY](../NOTIFICATION_POLICY.md)）は Orchestrator / 通知の Issue。

### 9. 完了しなかった呼び出し

- Process が Admission と精算の間で落ちると、使用量の行が `in_flight` のまま残る。要求数には数えられるが、Token と時間は無い。**掃除（Reaper）は実装していない。**
  承認: 最大の呼び出し時間より十分長く `in_flight` の行を `failed`（`internal_error`）へ変える処理を、Orchestrator の Worker の Lease と一緒に足す。
- Task の Budget への Token の加算は、精算とは別の Transaction で行う（間で落ちると加算が漏れる）。

## 選定理由

- 未設定を無制限にするのは、Human の選択（Quota を設定していない User でも、まず使えること）。設定した上限は判定し、`UNLIMITED` の明示は残すため、「制限する」と「制限しない」を Admin が明示できる。
- 実行中の Task を止めない選択は、要件の「実行中Taskを原則完了させ、新規Taskのみ停止する」をそのまま実装するため。厳格な判定は、Quota の設定が変わっただけで進行中の作業が壊れる。
- 判定と記録を 1 つの Transaction にし、Quota の行を `FOR UPDATE` で Lock するのは、同時の Admission が上限を超えないようにするため。時計は Database の 1 つで、Lock を持った後に 1 回だけ読む（Decision 0007 の 10 節と同じ理由）。
- 期間を閉じた集合にするのは、Admin が自由な文字列や式を入力して判定を壊せないようにするため、また `resets_at` を計算できるようにするため。

## 代替案

各節に併記した。追加で採らなかった案:

- Credential の平文を DB に暗号化して保存する: Secret Store の製品・方式が未決で、鍵の管理が新しい Secret になる。Handle だけを持つ。
- Quota を User 単位ではなく Project 単位にする: 要件は User 別。Project 別の利用量は集計（Usage の `project_id`）で見られる。
- 呼び出しの Prompt の要約を保存して Admin の分析に使う: 要件は Raw Chat 本文を Admin に見せない。自由な文字列は保存しない。

## リスク

- Provider の規約: 共有 Subscription の利用が問題ないことは Human が確認した（前提を参照）。規約は変わりうるため、実 Adapter を作るときに再確認する。
- 未設定は無制限のため、Admin が Quota を設定し忘れた User は制限なしに使える。Task の Budget と、使用量・Quota 使用率の監視が前提。
- 実行中の Task を止めないため、Quota は上限を超えて使われうる。Task の Budget と Admin の監視（Quota 使用率）が前提。
- `tokens` は Adapter が返す値に依存し、返さない Provider では数えられない（0 として扱う）。
- Audit の Table は削除できず、拒否の 1 行ずつの記録は量が増える（Rate Limit は PAW-022 以降）。

## 承認後の扱い

- 2026-09-26 に承認された。`Approval` に記録し、Status を Approved に改めた。Backend README の「Shared Codex / Claude Connection」は、承認済みの方針として書き直した。
- 承認された選択を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。変更の場所: Quota が未設定の扱い（`store.admit`）、期間と時間帯（`domain.DEFAULT_PERIOD_TIMEZONE`、`ConnectionService(period_timezone=)`）、
  指標の集計（`store.py` の SQL 定数）、Category（`domain.UsagePurpose` と Migration の CHECK）、実行中の Task の扱い（`store.admit`）。Category の変更と期間の追加は、CHECK 制約を差し替える Migration が要る。
- 実 Adapter の実装は別の Issue（Provider の規約の確認は済み。Secret Store の製品と Network の Policy が前提）。
- 後続の Issue: 専用の Capability と閲覧用の `DENIED_ONLY` の Capability（#82）、Credential の差し替え・削除への Step-up（PAW-023 の後）、完了しなかった呼び出しの掃除（PAW-034 の受け入れ条件）、同時実行数、期限付きの一時 Override、使用量の保存期間と集計。

## 承認時の決定（2026-09-26）

Human は、判断メモの各点に個別に回答し、残りは「推奨どおり」と回答した。

1. **Quota が未設定の User: 無制限にする（推奨の「拒否」ではなく、Human が選んだ案）。** 行の無い User・指標・期間は判定しない。設定した上限は判定し、`UNLIMITED` の明示は有効。使用量の帰属と Audit は残る。最初の実装の拒否（`QuotaNotConfiguredError`、reason `quota_not_configured`）と、それを固定していた Test は、この契約へ置き換えた。
2. **Quota に達したときの実行中の Task: 止めない（推奨どおり）。** 呼び出しは途中で止めず、判定は新規の Task の最初の呼び出しだけ。実行中の Task が使う量は Task の Budget（PAW-033）が抑える。期限付きの一時 Override は後続。
3. **暦の期間の時間帯: 既定を `Asia/Tokyo` にする（推奨の運用値を既定にした。最初の実装は UTC）。** `week` は暦の週（月曜始まり）。`rolling_5h` は User 別 Quota の期間として持つ（推奨どおり）。Provider 側の 5 時間 / 週の Window は実 Adapter の後（推奨どおり）。
4. **数える指標: `requests` / `tasks` / `tokens` / `runtime_seconds`（推奨どおり）。** `tokens` / `runtime_seconds` は終了後に数える。同時実行数は Orchestrator の Lease と一緒に後で入れる。
5. **用途の Category: `chat` / `coding` / `review` / `research` / `evaluation` / `other`（推奨どおり）。** 目的の要約は保存しない。使用量の保存期間は Audit と同じ長期保持（推奨どおり。実装は後続）。
6. **認可: 既存の Capability を使う（推奨どおり）。** 専用の Capability と閲覧用の `DENIED_ONLY` の Capability は #82 で加える。Credential の差し替え・削除は PAW-023 の後に Step-up の対象にする。**Owner の Quota は Owner だけが変えられる**（推奨どおり。実装済み）。
7. **Provider の利用規約: Human が、共有 Subscription の利用が問題ないことを確認済み（2026-09-26）。** 実 Adapter を作る前提条件は満たされた。実 Adapter は別の Issue。
8. **完了しなかった呼び出しの掃除: PAW-034 の受け入れ条件に置く（推奨どおり）。**
