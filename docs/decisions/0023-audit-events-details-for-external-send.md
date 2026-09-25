# 外部送信の Audit を `audit_events.details`（JSONB）へ永続化する方針

- Status: Proposed
- Date: 2026-09-25
- Scope: Issue [#87](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/87)（Decision 0010 の承認時の条件）と、`audit_events` に「Id と Enum だけ」では足りない記録を足す以降の Issue
- Supersedes: なし（Decision 0010 と Decision 0004 への追補。どちらも書き換えず、選択も変えない）
- Approval: 未承認（Human の承認待ち。承認前の実装は、この Decision を参照する提案として入れている）

## 背景

[Decision 0010](0010-research-privacy-filter-policy.md)（Approved）の 5 は、Research Privacy Filter が外部の検索へ Query を送る**前**に、
`ExternalSendRecord`（Query の SHA-256、文字数、送る Provider の種類、Project ID、消した数など。Query の本文は持たない）を `ExternalSendAudit` に渡し、
受理されなければ送らない（`audit_failed`）ことを定めた。Human は、その承認の**条件**として、
「永続の Audit Sink（Issue #87）が接続されるまで、Private 由来の Context を Research へ自動で入れる設計は有効にしない」ことを承認した（2026-09-25）。

Issue #87 は、その永続の Sink を、Authorizer と同じ追記専用の `audit_events` Table（PAW-025、[Decision 0004](0004-rbac-capability-and-audit-policy.md)）へ書くものと定めた。
ところが `audit_events` の列は、UUID、Enum の値、時刻、固定の Reason Code だけで（Decision 0004 の 3。`AuditEvent` は「Secret、Prompt、本文を持たない」ように作られ、
Test も Field の一覧を固定している）、Query の SHA-256（64 桁）、文字数、Provider の種類の一覧、消した数を置く列がない。

要件（[SECURITY_RBAC_AUDIT.md](../SECURITY_RBAC_AUDIT.md) の Audit の項目）は最低項目に `metadata` を挙げるが、現在の Schema にはない。
Decision 0004 にも 0010 にも、「`audit_events` にどう足すか」は書かれていない。実装は動かすために、次を選んだ。
[AGENTS.md](../../AGENTS.md) の「仕様変更」に従い、その選択を一覧にして Human の承認を求める。

**承認済みの Decision 0010 と 0004 は書き換えない。** この Decision は、0010 の 5（Audit の Record と Fail closed）と、0004 の Audit の Schema に**足す**追補で、
0010 の Record の内容、Gate の順序（Audit してから送る）、拒否の理由、時間切れ（既定 5 秒）、0004 の追記専用の保証、Application の Role の権限（INSERT と SELECT だけ）は変えない。
そのため `Supersedes` はない。

実装は [Backend README](../../apps/backend/README.md) の「Research Privacy Filter」の「Audit の永続化（Issue #87）」と「Migration `0087`」に書いている。

## 提案

### 1. `audit_events` に、NULL 可の JSONB 列 `details` を 1 つ足す

- Migration `0087`（`down_revision` は `0026`。鎖の並びは統合時に Orchestrator が確認する）が、`audit_events` に `details`（JSONB、NULL 可）を足す。Table も Trigger も権限も作らず、変えない。
  Application の Role の権限は Table 単位の INSERT と SELECT（Migration `0025`）のままで、列は自動的に含まれる。`UPDATE` の列権限は増えない。
- `AuditEvent`（`paw_backend/authz/audit.py`）と `PostgresAuditSink` は**変えない**。`AuditEvent` は Id と Enum と時刻だけの型のままで、既存の Test（Field の一覧の固定）もそのまま通る。
  `details` に書くのは、`paw_backend/research/privacy/audit.py` の `PostgresExternalSendAudit` だけである。ほとんどの行では NULL のままである。
- 1 回の外部送信は 1 行になる。`action` は `research.external_send`、`reason` は `send_authorized`、`decision` は `allow`、`resource_kind` は `research_query`、
  `project_id` は Record の Project、`actor_role` は `system`（Backend が判断する。Record には User も Agent もない）、`actor_id` はなし、
  `occurred_at` は Gate の時計、`recorded_at` は Database の時計（既存の Trigger）、`id` と `correlation_id` は送信ごとの新しい UUID。
- `details` は、`query_fingerprint`（`sha256:` と 64 桁の小文字の 16 進数。Provider が受け取る**最小化済みの** Query のもの）、`query_chars`、`provider_kinds`、
  `withheld`（`private_source`、`private_memory`、`raw_conversation`、`secret` ごとに外した Piece の数）、`credentials_removed`、`pieces_matched`、`abstractions`、`truncated` の 8 つの Key だけを持つ。
  Decision 0010 の Record の Field と同じで、Query の本文、消した文字列、Context の本文はどこにも入らない。

### 2. `details` は自由記述の列にしない: 2 つの CHECK 制約

`details` が将来、本文や説明文の置き場になり、`audit_events` の「本文を持たない」という性質が崩れることを、Database の側で防ぐ。

- `ck_audit_events_details_object`: `details` は NULL か、JSON の Object で、テキストにして **2,048 Byte 以内**（実際の行は約 300 Byte）。全ての `action` に効く。
- `ck_audit_events_external_send_details`: `action` が `research.external_send` の行は、`decision` が `allow`、`reason` が `send_authorized`、`project_id` が非 NULL で、
  `details` が**ちょうど上の 8 つの Key**（`withheld` の中は 4 つの Key）を持ち、値が決まった形であること。指紋は `sha256:` と 64 桁の小文字の 16 進数、数は 0 以上の整数（数字だけ。文字列、小数、指数表記は不可）、
  `truncated` は Boolean、`provider_kinds` は `[a-z][a-z0-9_]{0,31}` の Token が 1〜8 個の配列。Key の追加も、Query を入れられる形の値も、Application の Role からの INSERT でも拒否される。
  値の型を確かめる式は、Cast をしない（PostgreSQL は `AND` の評価順を保証しないため）。欠けた Key（NULL）は `COALESCE` で違反にする（CHECK は NULL を通してしまうため）。
- 本文が入らないことは、3 つの層で守る。(1) `ExternalSendRecord` に本文の Field がない。(2) `external_send_event` が、Record の各 Field を、型と形（`type(x) is int`、`sha256:` の形、`ProviderKind` の Member など）で確かめ直し、
  新しいプレーンな値だけで `details` を作る（作られた後に書き換えられた Record、`str` / `int` の Subclass、Slot の消えた Record は、Database に触れる前に `TypeError` / `ValueError`）。
  (3) 上の CHECK 制約。
- 新しい種類の外部送信の記録（別の `action`）が `details` を使うときは、その `action` の形を、新しい Migration の CHECK 制約で登録する。`details_object` だけが、全ての `action` に効く。

### 3. 書き込みは Fail closed

- 書き込みは `Database.execute_abortable`（Authorizer の `PostgresAuditSink` と同じ、Pool を使わない 1 文専用の中断可能な接続と、同じ接続の枠の勘定）で行う。期限は Gate の既定と同じ 5 秒で、
  Gate と Sink に同じ値を渡す（`build_research_broker` / `build_privacy_gate`。本番の構築経路）。枠の待ちと実行は 1 つの期限を共有し、期限で接続の Socket を閉じる。
- 書けない、時間切れ、権限がない、接続できない場合は、Gate が `audit_failed` で拒否し、Provider を 1 つも呼ばない。
- 打ち切られた書き込みは Commit されたか分からない。Gate が拒否したのに行が残ることはある（「承認済み」と書かれた行があり、送っていない）。逆（送ったのに行がない）は起きない。

### 4. 拒否した要求は、今回は Audit しない（Decision 0010 のとおり）

Decision 0010 は「Record は『送ってよいと判断した』記録で、拒否した要求は Record を作らない」と定めた。実装はこれに従い、`deny` の行を書かない。
拒否の理由（`unclassified_context`、`credential_remains` など）は、`audit_events` に残らない。この扱いを変えるかは、下の「決めてほしいこと」の 6 で問う。

## 代替案

- **別の Table（たとえば `research_external_sends`）に書く:** 型付きの列を持て、`details` の CHECK が要らない。一方、Issue #87 と Decision 0010 は「`audit_events` へ書く」（Authorizer と同じ追記専用の Audit Log。
  管理者が 1 か所で読める）と定めており、Table が増えると、追記専用の Trigger、`recorded_at` の Trigger、権限、起動時の権限の診断（`warn_if_audit_table_is_mutable`）、保存期間の方針を Table ごとに複製する必要がある。
  また、Audit を読む側が 2 か所を突き合わせる。**採らない**（Human が別 Table を望むなら、新しい Decision で変える）。
- **`AuditEvent` に `details` を足す（Authorizer と全ての Sink が使える一般の Field にする）:** 全ての Audit の Event に、本文を入れられる型ができる。`AuditEvent` の Test は「Id と Enum だけで、本文の Field がない」ことを固定している。
  一般の Field にすると、その保証が型では守れなくなり（文字列の値は何でも入る）、Authorizer、Tool Broker、Owner の設定の全ての呼び出し側が影響を受ける。
  `details` は `research.external_send` の 1 つの `action` に限り、Table にだけ足す（Model は `AuditEventRecord`）。**採らない。**
- **既存の列に詰める（指紋を `client_request_id` や `reason`、`resource_id` に入れる）:** Schema を変えずに済む。ただし、指紋は 71 文字（`sha256:` と 64 桁）で `reason`（64 文字まで）に入らず、
  `resource_id` は UUID（16 Byte）で 32 Byte の SHA-256 が入らない。`client_request_id` は「Client が送った値で、偽造できる」列で、64 文字の 16 進数の指紋は入るが、意味が違う（列の意味を偽る）。
  文字数、Provider の種類、消した数は置き場がない。`reason` に数を符号化する案は、Reason Code が「固定の閉じた集合」である性質を壊す。**採らない。**
- **`details` を、CHECK 制約なしの JSONB にする:** 実装は簡単。ただし、Application の Role は INSERT できるため、Bug が本文を書いても Database は拒否せず、「本文を持たない」の保証が Application の Code だけになる。**採らない。**
- **型付きの列を 8 つ足す:** CHECK が単純になり、Column ごとに検索できる。一方、`audit_events` の列が、1 つの `action` のために増える（他の `action` では全て NULL）。別の `action` が別の値を持つたびに列が増える。
  JSONB は、`action` ごとの形を CHECK で閉じたまま、列を増やさない。指紋で検索するときは Expression Index を足せる（今回は足さない）。
- **CHECK 制約を `VALIDATE` する:** 既存の行がない最初の適用では常に通る。しかし、`downgrade`（列を落とす）の後の `upgrade` で、`details` を失った `research.external_send` の行が検証に失敗する（下の「リスク」）。採らない（`NOT VALID` のまま）。

## リスク

- **CHECK 制約は `NOT VALID` で足し、検証しない。** `NOT VALID` の制約も、その後に INSERT される行には効く（この Table は追記専用で UPDATE がない）。既存の行を走査しないので、追記専用で増え続ける Table に `ACCESS EXCLUSIVE` の Lock を長く持たない。
  代わりに、カタログ上は `convalidated = false` のままで、「Table の全ての行が制約を満たす」ことを Database は保証しない。最初の適用の時点で、この `action` の行は存在しないので、違反はない。違反し得るのは、下の `downgrade` の後の行だけである。
- **`downgrade` は `details` の列を落とすので、記録済みの外部送信の Query の指紋と数を破棄する。** 行は残る（追記専用の Trigger は列の削除を止めない。Migration Role は Table の Owner）。
  その後の `upgrade` では、`details` が NULL の `research.external_send` の行が残る。制約は `NOT VALID` なので、`upgrade` は失敗しないが、その行は制約を満たさない。Migration `0025` の `downgrade` が Table ごと履歴を破棄するのと同じ位置づけで、
  開発・Test 用であり、本番では実行しない。README に書いている。
- **Application が侵害されると、偽の行を足せる。** Application の Role は `audit_events` に INSERT でき、`actor_id`、`occurred_at` などを自由に決められる（Decision 0004 の「守れないもの」と同じ）。
  CHECK 制約で守るのは、`research.external_send` の行に**本文が入らないこと**と、行が書き換えられないことで、行が**真実であること**ではない。
- **塩なしの SHA-256 を永続化する。** Decision 0010 は、Query が Public な内容になってから送るため、塩なしの指紋を承認した。指紋を `audit_events` に永続化すると、
  Audit Log を読める人が、推測した Query の SHA-256 と突き合わせて、「その Query を送ったか」を確かめられる。何を検索したかは行から分からないが、推測が当たれば分かる。
  Query は Public な内容という前提が崩れたときの影響は、Audit Log を読める人（Admin）に及ぶ。塩を付けると、この突き合わせができなくなる（Query から行を探せなくなる）。
- **記録の内容の限界。** Record には Provider の応答の成否がない（「送ってよいと判断した」記録）。`actor_id` がなく、誰の操作で送ったかは行から分からない（`project_id` だけ）。
  行数は送信ごとに 1 行ずつ増え続ける（保存期間と Partition は未実装。Decision 0004 の既知の制限と同じ）。
- **Database が遅い、または落ちている間、Research の検索は全て止まる。** Fail closed の意図した帰結だが、Research が Audit Table に依存することになる（Decision 0004 の `REQUIRED` と同じ）。
- **形を変えるたびに Migration が要る。** `research.external_send` の Key を増やす、Provider の種類の Token の形を変える、上限を変える、は CHECK 制約を変える Migration を要する。
  Migration が適用された後の Migration は書き換えない。CHECK と Code が食い違ったまま出荷すると、全ての送信が拒否される（Fail closed）。`tests/test_privacy_audit_schema.py` が、Model、Migration、Code の食い違いを検出する。
- **`0087` の Revision ID は Issue の番号で、鎖の順序ではない。** 統合時に並びを確認する。

## 承認後の扱い

承認されたら、この Decision は Decision 0010 の 5 と 0004 の Audit の Schema への追補として扱う。`details` を持つ `audit_events` と、`research.external_send` の行の形が、承認された方針になる。0010 と 0004 は書き換えない。
承認前でも、実装（Migration `0087`、`authz/models.py`、`research/privacy/audit.py`、`research/privacy/factory.py`）はこの提案どおりに入っている。
Human が方針を変えたら、この Decision を更新してから、実装と Test を合わせる。**適用済みの Migration `0087` は書き換えず**、新しい Migration で変える。
承認後に方針を変える場合は、この Decision を書き換えず、新しい Decision から `Supersedes` する。

Decision 0010 の承認時の条件は「永続の Audit Sink が接続されるまで、Private 由来の Context を Research へ自動で入れる設計を有効にしない」である。
実装は Sink を接続した（`build_research_broker`）が、その方式はこの Decision が提案する `audit_events.details` である。
**この Decision が承認されるまで、条件が満たされたとは扱わない**（README の日付つきの追記も、そのように書いている）。承認されたら、Private 由来の Context を、
`build_research_broker` / `build_privacy_gate` で作った Broker を通して Research へ入れてよい。他の Broker（Sink の無い Broker、`unfiltered=True`）は、これまでどおり使えない。

## 決めてほしいこと

推奨の答えを添える。承認は Human が行い、承認されるまで Status は Proposed のままとする。

1. **保存先と方式**: 外部送信の Audit を、`audit_events` に NULL 可の JSONB 列 `details` を足して書く（`AuditEvent` は変えない）。別の Table にする、`AuditEvent` に一般の Field を足す、既存の列に詰める、は採らない。
   推奨: 提案どおり。これを承認すると、Decision 0010 の条件（永続の Sink の接続）が満たされたと扱う。
2. **`details` の制限**: 全ての `action` で JSON の Object・2,048 Byte 以内。`research.external_send` は、決まった 8 つの Key（`withheld` の中は 4 つ）と値の形だけ（Database の CHECK 制約）。
   推奨: 提案どおり（本文が入らないことを、Application の Code だけに頼らず Database でも守るため）。Key を増やすときは、新しい Migration と Decision の更新で行う。
3. **CHECK 制約は `NOT VALID` のまま検証しない**: 追記専用の大きな Table を長い Lock で走査しないため、また `downgrade` の後の `upgrade` を通すため。
   推奨: 提案どおり（新しい行には効く）。検証する案は、`downgrade` を使わない本番でだけ意味があり、その場合は既存の行に違反がないので、後から `VALIDATE` する Migration を足せる。
4. **`downgrade` が `details` を破棄すること**: 開発・Test 用とし、本番では実行しない（Migration `0025` と同じ）。
   推奨: 提案どおり。
5. **指紋を塩なしで永続化する**: Decision 0010 で承認された塩なしの SHA-256 を、そのまま `audit_events` に置く。塩付き（Query から行を探せなくなる）や、指紋を置かない案は採らない。
   推奨: 塩なしのまま（Query が Public な内容という前提と、Audit Log の閲覧が Admin に限られることによる。前提が崩れる設計になったら、新しい Decision で塩を導入する）。
6. **拒否した要求の Audit**: 今回は拒否した要求の行（`decision` が `deny`、`reason` に拒否の理由）を書かない（Decision 0010 のとおり）。書くには、Gate の契約（拒否のときも Sink を呼ぶ）と、Decision 0010 の 5（「拒否した要求は Record を作らない」）の変更、
   行に Draft の指紋を置かない規則（Draft は Private 由来の内容を含み得るため、塩なしの指紋は辞書攻撃で内容を漏らす。Project と理由だけを持つ）が要り、別の Decision（Decision 0010 を `Supersedes` または追補する）が必要になる。
   推奨: 今回は書かない。必要なら、別の Issue と Decision で、Project と拒否の理由だけを持つ `deny` の行として提案する。
7. **`actor_id` を持たない**: 行の `actor_role` は `system`、`actor_id` はなし。誰の操作で送ったかは、この行からは分からない。記録するには、Gate の入力（`PrivacyInput`）と Record に、操作した User と Agent を足す必要がある。
   推奨: 今回は足さない（Decision 0010 の Record の内容を変えないため）。Research を呼ぶ API の Issue が、Task や Request の `correlation_id` で認可の行と結び付ける設計を、その時に提案する。
8. **保存期間と削除**: `research.external_send` の行の保存期間、Partition、古い行の退避は決めない（`audit_events` の既知の制限と同じ）。
   推奨: 決めない（`audit_events` 全体の方針として、別の Issue で扱う）。
