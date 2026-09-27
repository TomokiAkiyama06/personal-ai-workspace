# Project の作成・招待への応答・退出の Capability と Audit の方針

- Status: Approved
- Date: 2026-09-25
- Scope: Issue [#82](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/82)（Decision 0008 の 5 の置き換えと、Decision 0004 の Capability 方針の拡張）と、この 3 操作を呼ぶ以降の Issue（PAW-022 の API、Project の Chat など）
- Supersedes: [Decision 0008](0008-project-membership-and-lifecycle-policy.md) の 5「Capability を持たない操作（暫定の作りで承認）」（表と本文。「承認時の決定」の同じ項目を含む）を**置き換える**。[Decision 0004](0004-rbac-capability-and-audit-policy.md) の 2 の 2「委任できる Capability の許可リスト」の委任不可の一覧と、1 の 3「自分のデータは所有者本人だけ」（`Scope.SELF`）の対象に、この Decision の 3 つの Capability を**加える**（0004 のほかの内容は変えない。範囲は「既存の Decision との関係」の表）
- Approval: 2026-09-26、Humanが作業Session内で、判断メモの各点に個別に回答し、残りは「推奨どおり」と回答して承認（末尾の「承認時の決定」）

## 背景

[Decision 0008](0008-project-membership-and-lifecycle-policy.md) の 5 は、Project の作成、自分宛ての招待の受諾・辞退、Project からの退出に Capability がなく、
PAW-026 はこの操作を **Authorizer を通さず、本人確認だけで許可し、Audit に残さない** 暫定の作りにした。Human は 2026-09-25 に、暫定として承認し、
Capability の追加を期限付きの Issue [#82](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/82) にすることを承認した。
Decision 0008 の決定は「`project.create`（`Scope.SYSTEM`、User 以上）、`project.invitation.respond`、`project.leave` を追加する」である。

一方、次のことは Decision 0004 にも 0008 にも書かれておらず、要件（[REQUIREMENTS.md](../../REQUIREMENTS.md)）にも定めがない。
[AGENTS.md](../../AGENTS.md) の「仕様変更」に従い、実装が置いた選択を一覧にして Human の承認を求め、2026-09-26 に承認された。

- `project.invitation.respond` と `project.leave` の Scope と、既定の付与先
- 3 つの Capability を Agent に委任できるか
- Audit Mode と、判定を行う時点
- 招待の受諾と辞退を Audit で区別するか

**この Decision は 2026-09-26 に Human が承認した（Approved）。** 下の各選択は、承認された方針である（承認時の決定は末尾を参照）。

**承認済みの Decision 0004 と 0008 は書き換えない**（AGENTS.md の「仕様変更」）。変更は、この新しい Decision の `Supersedes` に、置き換える節を挙げて示す。
0008 の 5 が定めた暫定の作り（本人確認だけ、Audit なし）は、この Decision が**置き換える**。0004 は、Capability の一覧、委任の許可リスト、自分のデータの範囲を**拡張**するだけで、
Role の階層、Audit Mode の既定、Fail-closed などの選択は変えない。0004 の「承認時の決定」と 0008 の 5 は、この追加を「新しい Decision から `Supersedes` で行う」と予告しており、この Decision がそれにあたる。
何が置き換わり、何がそのまま有効かは、次の「既存の Decision との関係」に表にした。

## 既存の Decision との関係（`Supersedes` の範囲）

| Decision と節 | 扱い | 内容 |
| --- | --- | --- |
| 0008 の 5「Capability を持たない操作（暫定の作りで承認）」の表と本文 | **置き換える** | 旧: 作成、招待の受諾・辞退、退出を、本人確認だけ（`system_role` が Owner / Admin / User）で許可し、Audit の行を書かない。新: 「提案」の 1 と 2（Authorizer を通し、Audit を `REQUIRED` で残す） |
| 0008 の「承認時の決定」の、Capability と Audit を暫定の作りで承認した項目 | **置き換える** | 暫定の作りは、この Decision の承認で終わる（Capability の追加を Issue #82 で行うという決定は、この Decision で実行する） |
| 0004 の 2 の 2（委任の許可リスト）の「委任不可」の一覧 | **拡張** | `project.create`、`project.invitation.respond`、`project.leave` を、委任不可の一覧に加える。委任可の一覧と、「Capability を追加するときは委任の可否を明示する」規則は変えない |
| 0004 の 1 の 3（自分のデータは所有者本人だけ。`Scope.SELF`） | **拡張** | 自分宛ての招待と、自分の Membership を、自分のデータの対象に加える（招待への応答と退出）。他人の行は、Owner でも動かせない |
| 0004 の「承認時の決定」の、Capability の追加を Issue #82 で行うという項目 | 実行 | 0004 が予告した「新しい Decision から `Supersedes` で追記する」を、この Decision で行う |
| 0004 の 1 の 4（Project の状態: Archived、Pending deletion の許可）、3 の 1（Audit Mode の既定と `DENIED_ONLY` の許可リスト）、既知の制限（`Scope.SELF` は Project の状態と Member 資格を見ない） | 変えない | 3 つは `Scope.SELF` / `Scope.SYSTEM` で状態の表を見ず、Audit Mode は既定の `REQUIRED` で、`DENIED_ONLY` の許可リストには加えない |
| 0008 の 3（Membership のルール。最後の Manager、Pending deletion からの退出、招待の期限）、4（存在を明かさない）、6（一覧） | 変えない | 規則は同じ。4 の「存在を明かさない」は、3 操作でも守られる（拒否は Project を読む前に出て、`ProjectPermissionDeniedError` になる。拒否されるのは `system` identity と Audit の障害のときだけで、`system` identity の `reason`（`capability_not_granted`）は旧来と同じ）。`list_projects` と `list_my_invites` は本人確認のまま |

実装は [Backend README](../../apps/backend/README.md) の「Project CRUD / Membership / Lifecycle」（「認可と Audit」）に書いている。

## 提案

### 1. Capability の名前と既定の Policy

| Capability | Scope | 既定の付与先 | 委任（Agent） | Audit Mode | 操作 |
| --- | --- | --- | --- | --- | --- |
| `project.create` | `Scope.SYSTEM` | User 以上（Owner / Admin / User。`system` identity は持たない） | 不可 | `REQUIRED` | `ProjectService.create_project` |
| `project.invitation.respond` | `Scope.SELF` | 同上 | 不可 | `REQUIRED` | `ProjectService.accept_invite`、`decline_invite` |
| `project.leave` | `Scope.SELF` | 同上 | 不可 | `REQUIRED` | `ProjectService.leave_project` |

1. **`project.create` は User 以上に与える。** 要件は Project の作成者を制限しておらず（「Project作成者は初期 `Manager` とする」だけ）、Decision 0008 も「Project の作成は誰でもできる」と承認した。
   Owner / Admin は User の権限を含むため（Decision 0004）、同じ規則で持つ。作成者は最初の Manager になる（要件）。
2. **`project.invitation.respond` と `project.leave` は `Scope.SELF`。** 招待は招待された User 自身のもの、Membership は Member 自身のものとして、Resource（`project_invitation` / `project_membership`）の持ち主が Actor 本人のときだけ許す。
   他人の招待や Membership を、Owner でも動かせない（Decision 0004 の「自分のデータ」と同じ扱い）。Service は Actor 自身の行だけを対象にするため、持ち主は常に Actor である。
   `Scope.SELF` は Project の状態と Member 資格を見ない。**Archived、Pending deletion の Project からも退出できる**（Decision 0008 の 3 の 4。最後の Manager が Pending deletion の Project から抜ける例外を含む）。
   Membership や招待の行があるかは、これまでどおり Service の Transaction（Project の行の Lock の下）が判定する（なければ `InviteNotFoundError` / `ProjectNotFoundError`）。
3. **3 つとも委任できない（`delegable=False`）。** 招待への応答、退出、Project の作成は、誰が Project に属するかを変える本人の意思表示である。
   Agent が Project を作る、招待を受ける、抜けることを許すと、User の Membership を Agent が動かせてしまう。Decision 0004 の 2 の 2 は、委任を許可リスト方式とし、Capability の追加時に必ず明示することを求める。
   許可リストに載せないため、Grant に書いてあっても、委任元の User が持っていても、Agent の操作は `agent_capability_forbidden` で拒否される（Audit に Agent の ID 付きで残る）。
4. **Audit Mode は `REQUIRED`。** 許可も拒否も 1 回ごとに 1 行を残し、記録できなければ許可を拒否に変える（`audit_unavailable`、HTTP 503 用。Decision 0004 の 3 の 1）。
   3 つとも Membership を作る・変える操作で、読み取り専用ではないため、`DENIED_ONLY` にしない。

### 2. 判定を行う時点と、Audit の行の内容

1. **判定は、状態を変える前に行う。** `create_project`、`accept_invite`、`decline_invite`、`leave_project` は、Actor と引数の検査（型・長さ）の後、**時計の読み取り、Transaction、Project の行の Lock、行の読み取りより前に** Authorizer を呼ぶ。
   拒否や Audit の書き込みの失敗は、何も変えず、Project や招待の存在も明かさない（存在しない Project でも同じ拒否になる）。Audit の書き込み（Authorizer の Timeout で有界）は、Project の行の Lock を持つ間には行わない。
   `accept_invite` は、招待の期限の判定に使う時刻を、判定の**後**に読む（Audit の書き込みに時間がかかっても、期限切れの招待が有効に見えないため）。
2. **Audit の行は「判定」を記録し、操作の結果は記録しない。** 許可された試行が後で規則に拒否されても（招待がない、期限切れ、最後の Manager、Lock の Timeout、DB の Error）、`allow` の行は残り、何も変わらない。
   逆に、状態が変わったのに `allow` の行がない、ということは起きない。要件の Audit の最低項目には `result` があるが、現在の Audit の Schema（Decision 0004 の 3。`decision` と `reason`）は判定だけを持つ。結果の記録は、この Decision では足さない（「承認を求めた点」の 6。「承認時の決定」で、記録しないと決めた）。
3. **行の内容。**

   | Capability | `resource_kind` | `resource_id` | `project_id` | `reason`（許可） |
   | --- | --- | --- | --- | --- |
   | `project.create` | `system` | なし | なし（Project はまだない） | `granted_by_system_role` |
   | `project.invitation.respond` | `project_invitation` | なし | 対象の Project | `granted_to_resource_owner` |
   | `project.leave` | `project_membership` | なし | 対象の Project | `granted_to_resource_owner` |

   `actor_id` と `actor_role` は Actor 本人。ID はすべて不透明な UUID で、Project 名や招待の内容は入らない。`system` identity の拒否は `capability_not_granted`。
4. **`list_projects` と `list_my_invites` は今のまま**（本人確認だけ、Audit なし）。Actor 自身の行を読むだけの操作で、Decision 0004 は読み取り専用の Capability（`project.read` など）の許可を記録しない（`DENIED_ONLY`）。Capability を足すかは「承認を求めた点」の 7 で問い、足さないと決めた。

## 代替案

- **`project.leave` を `Scope.PROJECT`（Project の Role が決める）にする:** Member でない人の退出を、Authorizer が `not_project_member` で拒否し、Audit に「拒否」と残せる。
  一方、Project の状態表（Archived は `project.read` と `project.lifecycle.manage` だけ、Pending deletion は `project.lifecycle.manage` だけ。Decision 0004 の 1 の 4）に `project.leave` を足す必要があり、
  判定が Project の状態と Actor の Role を先に読むことを要する（`system` identity の拒否も DB の読み取りの後になり、拒否が存在を明かさない性質が弱まる）。`Scope.SELF` は状態表を変えず、判定を DB の前に置ける。
- **`project.invitation.respond` を `accept` と `decline` の 2 つに分ける:** Audit の `action` が操作を区別する（Decision 0009 の先例。Shared Memory の承認と却下を別の Capability にした）。
  辞退は行の削除で履歴を持たない（Decision 0008 の 3 の 2。履歴は Audit に置く）ため、分けないと Audit から「受諾した」と「辞退した」を区別できない（受諾は Member の行が残るので状態から分かる）。
  Issue #82 と Decision 0008 の名前は `project.invitation.respond` の 1 つで、名前は Human が承認した。1 つのままにし、分ける案は「承認を求めた点」の 5 で問い、採らないと決めた。
- **`project.create` を Admin 以上に限る:** 要件にも Decision 0008 にも制限がない。User が Project を作れなくなり、招待制の入口が Admin に集中する。
- **3 つを委任可にする:** 上の 1 の 3 のとおり、Membership を Agent が動かせるため採らない。
- **`DENIED_ONLY` にする:** 副作用のある操作を、許可した記録なしに行える。Decision 0004 の 3 の 1 は、`DENIED_ONLY` を読み取り専用の許可リストに限る。
- **Audit を状態の変更と同じ Transaction に書く（結果も 1 行にする）:** `AuditSink` は独立した短い Transaction で書く設計（拒否された Request が自分の Transaction を巻き戻しても行が残るため。Decision 0004）で、
  Authorizer と Audit の Schema の変更が要る。判定を先に記録し、記録できなければ止める現在の作りは、「行がなければ変更もない」を守る。
- **判定を Project の行の Lock の下で行う（他の操作の `_guarded` と同じ）:** Audit の書き込みの間、Lock が続く。Project の存在を先に確かめると、拒否が存在を明かす。3 つの操作は判定に DB の状態を要らないため、先に判定する。

## リスク

- `REQUIRED` のため、Audit の Table が使えない間は、Project の作成、招待への応答、**退出ができない**（Pending deletion の Project からの退出を含む）。Decision 0004 の Break-glass の問い（3 の 4）と同じ帰結で、復旧は Owner の Recovery で行う。
- 存在しない招待や、Member でない Project への試行も、Policy は許可し（`allow` の行）、その後で Service が「見つからない」にする。試行ごとに 1 行が増える。認証済みの User の拒否の回数制限が PAW-022 までないこと（Decision 0004 の 3 の 3）と合わせ、行数の増加の要因になる。
- `project.create` の行は、作成された Project を指さない（作成の前に判定するため）。作成者と時刻は `projects.created_by` / `created_at` から分かる。
- 受諾と辞退は同じ `action` で、Audit の行だけでは区別できない（分ける案は「承認を求めた点」の 5。採らないと決めた）。
- 「`allow` は操作の成功を意味しない」ことを、Audit を読む人が知っている必要がある。README に書いている。

## 承認後の扱い

- 2026-09-26 に承認された。`Approval` に記録し、Status を Approved に改めた。
- 上の `Supersedes` の範囲が置き換わった。0008 の 5 の暫定の作り（本人確認だけ、Audit なし）は終わり、0004 の委任不可の一覧と `Scope.SELF` の対象は 3 つ分広がった。
  0004 と 0008 は書き換えず、この Decision を新しい参照先とする（README とコードの参照は、0008 の 5 ではなく 0022 を指す）。実装（`authz/capabilities.py`、`authz/policy.py`、`projects/service.py`）は、承認された方針のとおりである。
- 承認後に名前、Scope、付与先、委任、Audit Mode を変える場合は、この Decision を書き換えず、新しい Decision から `Supersedes` する。[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。

## 承認を求めた点

提案のときに Human へ問うた 9 点と、推奨の答え。Human の回答は「承認時の決定」に記録した。

1. **Capability の名前**: `project.create`、`project.invitation.respond`、`project.leave`。推奨: Issue #82 と Decision 0008 のとおり。
2. **既定の付与先**: 3 つとも User 以上（Owner / Admin / User）。`system` identity は持たない。推奨: このとおり（要件は Project の作成者を制限していない）。Admin 以上に限る案は採らない。
3. **委任**: 3 つとも Agent へ委任しない。推奨: このとおり。
4. **Audit Mode と記録**: `REQUIRED`（許可も拒否も 1 回ごとに 1 行、記録できなければ拒否）。推奨: このとおり。
5. **受諾と辞退の区別**: `project.invitation.respond` の 1 つのまま（Audit の `action` では区別しない）か、`project.invitation.accept` と `project.invitation.decline` に分けるか。推奨: 1 つのまま（Human が承認した名前。分けたければ、新しい Decision で分ける）。
6. **判定の記録**: Audit の行は判定だけを記録し、操作の結果（招待がない、最後の Manager、など）は記録しない。推奨: このまま（結果の記録は Audit の Schema の変更を伴うため、別の Issue と Decision で扱う）。
7. **`list_projects` / `list_my_invites`**: 本人確認のまま（Capability を足さない）。推奨: このまま（自分の行を読むだけで、許可の記録も Decision 0004 の対象外）。
8. **`project.leave` の Scope**: `Scope.SELF`（Project の状態と Role を見ない。Archived、Pending deletion からも退出できる）。推奨: このとおり（`Scope.PROJECT` にする案は、代替案のとおり採らない）。
9. **0008 の 5 の置き換えと 0004 の拡張**: 0008 の 5（暫定の作り）を、この Decision で置き換え、0004 の委任不可の一覧と `Scope.SELF` の対象に 3 つを加えてよいか（範囲は「既存の Decision との関係」の表）。推奨: このとおり（0008 の 5 と 0004 の「承認時の決定」が、この置き換えと追記を予定している）。

## 承認時の決定（2026-09-26）

- 本文の各点を、提案どおり承認した。点 2 と 5 は Human が個別に回答し、どちらも推奨のとおりだった。ほかの点は「推奨どおり」と回答した。
- 点 1（Capability の名前）: `project.create`、`project.invitation.respond`、`project.leave`（推奨どおり）。
- 点 2（既定の付与先。個別に回答）: 3 つとも User 以上（Owner / Admin / User）。`system` identity は持たない。Admin 以上に限る案は採らない（推奨どおり）。
- 点 3（委任）: 3 つとも Agent へ委任しない（推奨どおり）。
- 点 4（Audit Mode と記録）: `REQUIRED`。許可も拒否も 1 回ごとに 1 行を残し、記録できなければ拒否する（推奨どおり）。
- 点 5（受諾と辞退の区別。個別に回答）: `project.invitation.respond` の 1 つのまま。Audit の `action` では受諾と辞退を区別しない（推奨どおり。分けたければ、新しい Decision で分ける）。
- 点 6（判定の記録）: Audit の行は判定だけを記録し、操作の結果は記録しない。結果の記録は Audit の Schema の変更を伴うため、別の Issue と Decision で扱う（推奨どおり）。
- 点 7（`list_projects` / `list_my_invites`）: 本人確認のまま。Capability は足さない（推奨どおり）。
- 点 8（`project.leave` の Scope）: `Scope.SELF`。Project の状態と Role を見ず、Archived、Pending deletion の Project からも退出できる（推奨どおり）。
- 点 9（0008 の 5 の置き換えと 0004 の拡張）: 0008 の 5（暫定の作り）を置き換え、0004 の 2 の 2 の委任不可の一覧と、1 の 3 の `Scope.SELF` の対象に 3 つを加える。範囲は「既存の Decision との関係」の表のとおり（推奨どおり）。
