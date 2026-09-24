# RBAC・Capability・Auditの方針

- Status: Proposed
- Date: 2026-09-24
- Scope: PAW-025 と、Role・Capability・Audit を使う以降の Issue（PAW-021 / 022 / 023 / 026 / 031 / 032 / 040 など）
- Supersedes: なし
- Approval: 未承認（Humanの承認待ち）

## 背景

[REQUIREMENTS.md](../../REQUIREMENTS.md) の「RBAC / Admin」「Audit Log」「Project roles and membership」「Project lifecycle」と
[Security / RBAC / Audit](../SECURITY_RBAC_AUDIT.md)、[Security / Tool Permissions](../SECURITY_TOOL_PERMISSIONS.md) は、
Role の名前、Owner / Admin の分担、Audit の最小項目、Agent が権限を超えられないことを定めている。
一方で、次のことは文書だけでは決まらず、PAW-025 の実装が選んだ。独立 Review もこれらを問題として指摘した。
承認前の Product Policy を実装が暗黙に確定させないよう、選択を一覧にして Human が承認または変更できるようにする。

実装は [Backend README](../../apps/backend/README.md) の「認可（RBAC / Capability）と Audit」に書いている。
**この Decision は Proposed であり、Human の承認を得ていない。** 承認されるまで、実装の選択は暫定である。

## 提案

### 1. Role と Capability

1. Owner ⊃ Admin ⊃ User、Manager ⊃ Contributor ⊃ Viewer とする。System Role と Project Role は独立で、System Role だけでは所属していない Project を開けない。
2. Owner / Admin は、Project の `project.lifecycle.manage`（Archive、Unarchive、Delete 開始、削除待ちからの復元）と `admin.projects.manage`（Running Agent / Project / Repo の管理）を、Member でなくても持つ。
   `project.read`、`project.chat` など Project の内容の閲覧・利用は持たない（要件「管理上必要な操作」の解釈）。
3. 自分のデータ（Chat、Workspace、GitHub、Memory）の Capability は、所有者本人だけが使える。Owner でも他の User の Private Data は使えない。
4. Project の状態: Archived は `project.read` と `project.lifecycle.manage` だけ、Pending deletion は `project.lifecycle.manage` だけ許可する
   （復元は Manager または Owner / Admin。Pending deletion の復元を誰に許すかは要件に明記がない）。
5. `Resource.repo_id` を持つ判定は、Repository の ACL Override が実装されるまで常に拒否する（Project Role だけで Repository を許可しない）。
6. Role の変更: Admin の追加・削除・変更は Owner だけ。Admin は通常の User だけを管理できる。対象の User を必ず指定し、自分自身の Role は誰も変更できない。`system` identity は割り当てない。
   Audit の行は対象の User と、変更前後の Role を持つ。
7. **Owner の Role は所有権の移譲（`authorize_ownership_transfer`）だけが動かす。** 通常の Role 変更では Owner に関わる操作を常に拒否する。
   移譲は 1 回の判定・1 件の Audit で、現在の Owner だけが、自分以外の User か Admin に対して行える。新しい Owner を持ち、移譲した人は Admin になる（Owner は常に 1 人）。
   2 つの変更は呼び出す側が 1 つの Transaction で適用する。

### 2. Agent への委任

1. Agent の操作の権限は、委任した User の権限と `AgentGrant` の**積集合**とする。Grant は権限を狭めるだけである。
2. 委任できる Capability は**許可リスト**とする。Capability を追加するときは、委任の可否を必ず明示する（既定値なし）。
   - 委任可: `chat.use`、`workspace.use`、`github.use`、`memory.use`、`pr.create`、`shared_memory.read`、
     `project.read`、`project.chat`、`project.task.run`、`project.repo.write`、`project.pr.create`、`project.memory.use`
   - 委任不可: `agent.use`、`project.agent.use`、`shared_memory.manage`、`admin.*`、`owner.*`、`project.memory.manage`、`project.settings.manage`、
     `project.repo.add`、`project.members.manage`、`project.agent_policy.manage`、`project.lifecycle.manage`
   - `agent.use` と `project.agent.use`（Agent を起動する操作）は、子 Agent の Grant を親の部分集合として導く仕組み（PAW-032）ができるまで委任不可とする。
3. Grant の Project 範囲は必須とし、既定の「User の全 Project」は置かない。全 Project を許す場合は `ALL_PROJECTS` を明示する。
4. Agent の操作ごとに、委任元の User の Principal を User Store から引き直す。Role の変更・User の削除は Agent の次の操作から効く。
   User Store が失敗する、遅い、別の User を返す、または委任元 ID が正規の UUID でないときは、Audit を書いたうえで拒否する（`delegator_not_active`）。

Tool Broker の Approval（Human Approval、Step-up）で、委任不可の操作を承認付きで Agent に許す仕組みは、この Decision の範囲外とする（PAW-031 / PAW-033 / PAW-023 で決める）。

### 3. Audit の記録と Fail-closed

1. Audit Mode は Capability ごとに決め、既定は `REQUIRED` とする。
   - `REQUIRED`: 許可も拒否も記録する。記録できなければ、許可を拒否に変える（HTTP 503）。
   - `DENIED_ONLY`: 許可リストの読み取り専用（`project.read`、`shared_memory.read`）だけ。拒否のみ Best Effort で記録し、許可した読み取りは記録せず、Audit の障害でも止めない。
   - **Agent の判定は Capability の Mode に関わらず常に `REQUIRED`** とする（Agent は記録なしに動かない）。
2. 認証されていない Request の拒否は Database に書かず、Log に出す（誰でも作れる行になり、Table を削除できないため）。
3. 残るリスク: (a) 人間の許可した読み取り（`DENIED_ONLY`）は記録されず、誰が何を読んだかは Audit から分からない。
   (b) 認証済みの User の拒否は 1 回ごとに 1 行を書き、回数制限は PAW-022 までない（未認証の拒否は Log だけ）。
4. **Break-glass の問い**: `admin.audit.view` と `admin.usage.view` も `REQUIRED` なので、Audit Table が使えないとき Admin は管理画面で Audit も Usage も見られず、原因を UI から調べられない。
   `REQUIRED` の対象は Chat と副作用のある操作のほとんどでもあるため、Audit Table だけが使えない障害では、書き込み系の操作がほぼ止まる（PostgreSQL 全体の障害では元々すべて止まる）。選択肢:
   (a) このまま。復旧は Database / Host への直接の操作（Owner の Ubuntu sudo Recovery）で行う（現在の実装）。
   (b) `admin.audit.view` / `admin.usage.view` を `DENIED_ONLY` にする（読み取りの Audit を諦める）。
   (c) Step-up（PAW-023）付きの Owner 専用 Break-glass Capability を設け、その使用を別の Log に残す。

### 4. ID と匿名化

1. User、Project、Repository、Agent などの ID はすべて UUID（不透明な ID）とする。Audit には ID だけを残す。
2. User 削除後の匿名化（`Deleted User`）は、Audit の行を書き換えず、User Store 側で ID と個人の対応を消して行う。

### 5. Audit Table の保護

1. `audit_events` は追記専用とする。Trigger が UPDATE / DELETE / TRUNCATE を拒否し、`PUBLIC` の権限を外す。
   INSERT のとき別の Trigger が `recorded_at` を Database の時計に上書きする。
2. Migration の Role（Table の Owner。`PAW_MIGRATION_DATABASE_URL`）と Application の Role（`PAW_APP_DATABASE_ROLE`。INSERT と SELECT だけ）を分ける構成を推奨する。
   分けない開発の既定では、Application の誤った DML からしか守れず、起動時に WARNING を出す。
   `PAW_APP_DATABASE_ROLE` は実在する Role でなければならず、`public`、`pg_*`、`postgres` などの予約名は拒否する（`public` を通すと全員が INSERT できてしまう）。
   Application の User に INSERT 権限がない（`PAW_MIGRATION_DATABASE_URL` だけを設定した場合など）ときも、起動時と Migration で WARNING を出す。
3. 保証しないもの: Superuser と Table の Owner による改ざん、Server や Backup の侵害、改ざんの検知（Hash Chain）、
   **INSERT できる Role による偽の行の追加**（`actor_id`、`decision`、`occurred_at` などを自由に決められる。`recorded_at` だけは固定される）。
4. Migration の `downgrade` は監査履歴を破棄する。開発・Test 用とする。
5. 保存期間、Partition、古い行の退避は未決とする（Table は削除できず、行数は増え続ける）。認証前の回数制限は PAW-022 で決める。

## 既知の制限と後続の課題

- 保存期間、Partition、古い行の退避は未実装（Table は削除できず、行数は増え続ける）。
- `Scope.SELF` の Capability は Project の状態と Member 資格を見ない（Pending deletion の Project の Chat、Member から外れた後の Memory など）。PAW-026 で Project との関係をモデル化する。
- 全 Route の保護を調べる Test は `/api/v1` だけを対象にし、FastAPI の内部に依存する。
- `create_app` が既定の Provider と Directory を組み込み、PAW-022 が `install_authz` を呼ぶまで全 Endpoint が 401 になる。
- 重要操作の Step-up 認証の項目は Audit にない（PAW-023 で追加する）。
- Migration `0025`、`0032`、`0040` は同じ `down_revision="0001"` を持ち、統合時に 1 本の鎖へつなぎ直す。

## リスク

- `REQUIRED` を既定にすると、Audit Table の障害が広い操作の停止になる（上の Break-glass の問い）。
- 委任の許可リストが狭すぎると Agent の作業が Approval 待ちで止まり、広すぎると権限の逸脱になる。運用で見直す。
- Owner / Admin に `project.read` を与えないため、管理者が Project の内容を調べるには Member になる必要がある。これは意図した制限である。
- Audit の行数は増え続ける。保存期間の設計が遅れると Table が大きくなる。

## 承認後の扱い

承認された場合、PAW-025 のPRは本Decisionを参照する。
Human が変更を指示した項目は、この Decision を更新してから実装を合わせる。
承認後に方針を変える場合は、この Decision を書き換えず、新しい Decision から `Supersedes` する。
[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。
