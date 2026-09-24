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

## 提案

### 1. Role と Capability

1. Owner ⊃ Admin ⊃ User、Manager ⊃ Contributor ⊃ Viewer とする。System Role と Project Role は独立で、System Role だけでは所属していない Project を開けない。
2. Owner / Admin は、Project の `project.lifecycle.manage`（Archive、Unarchive、Delete 開始、削除待ちからの復元）と `admin.projects.manage`（Running Agent / Project / Repo の管理）を、Member でなくても持つ。
   `project.read`、`project.chat` など Project の内容の閲覧・利用は持たない（要件「管理上必要な操作」の解釈）。
3. 自分のデータ（Chat、Workspace、GitHub、Memory）の Capability は、所有者本人だけが使える。Owner でも他の User の Private Data は使えない。
4. Project の状態: Archived は `project.read` と `project.lifecycle.manage` だけ、Pending deletion は `project.lifecycle.manage` だけ許可する
   （復元は Manager または Owner / Admin。Pending deletion の復元を誰に許すかは要件に明記がない）。
5. `Resource.repo_id` を持つ判定は、Repository の ACL Override が実装されるまで常に拒否する（Project Role だけで Repository を許可しない）。
6. Role の変更: Admin の追加・削除・変更と、Owner に関わる変更は Owner だけ。Admin は通常の User だけを管理できる。自分自身の Role は誰も変更できない。`system` identity は割り当てない。

### 2. Agent への委任

1. Agent の操作の権限は、委任した User の権限と `AgentGrant` の**積集合**とする。Grant は権限を狭めるだけである。
2. 委任できる Capability は**許可リスト**とする。Capability を追加するときは、委任の可否を必ず明示する（既定値なし）。
   - 委任可: `chat.use`、`agent.use`、`workspace.use`、`github.use`、`memory.use`、`pr.create`、`shared_memory.read`、
     `project.read`、`project.chat`、`project.task.run`、`project.repo.write`、`project.agent.use`、`project.pr.create`、`project.memory.use`
   - 委任不可: `shared_memory.manage`、`admin.*`、`owner.*`、`project.memory.manage`、`project.settings.manage`、`project.repo.add`、
     `project.members.manage`、`project.agent_policy.manage`、`project.lifecycle.manage`
3. Grant の Project 範囲は必須とし、既定の「User の全 Project」は置かない。全 Project を許す場合は `ALL_PROJECTS` を明示する。
4. Agent の操作ごとに、委任元の User の Principal を User Store から引き直す。Role の変更・User の削除は Agent の次の操作から効く。

Tool Broker の Approval（Human Approval、Step-up）で、委任不可の操作を承認付きで Agent に許す仕組みは、この Decision の範囲外とする（PAW-031 / PAW-033 / PAW-023 で決める）。

### 3. Audit の記録と Fail-closed

1. Audit Mode は Capability ごとに決め、既定は `REQUIRED` とする。
   - `REQUIRED`: 許可も拒否も記録する。記録できなければ、許可を拒否に変える（HTTP 503）。
   - `DENIED_ONLY`: 許可リストの読み取り専用（`project.read`、`shared_memory.read`）だけ。拒否のみ Best Effort で記録し、許可した読み取りは記録せず、Audit の障害でも止めない。
2. 認証されていない Request の拒否は Database に書かず、Log に出す（誰でも作れる行になり、Table を削除できないため）。
3. **Break-glass の問い**: `admin.audit.view` と `admin.usage.view` も `REQUIRED` なので、Audit Table が使えないとき Admin は管理画面で Audit も Usage も見られず、原因を UI から調べられない。
   `REQUIRED` の対象は Chat と副作用のある操作のほとんどでもあるため、Audit Table だけが使えない障害では、書き込み系の操作がほぼ止まる（PostgreSQL 全体の障害では元々すべて止まる）。選択肢:
   (a) このまま。復旧は Database / Host への直接の操作（Owner の Ubuntu sudo Recovery）で行う（現在の実装）。
   (b) `admin.audit.view` / `admin.usage.view` を `DENIED_ONLY` にする（読み取りの Audit を諦める）。
   (c) Step-up（PAW-023）付きの Owner 専用 Break-glass Capability を設け、その使用を別の Log に残す。

### 4. ID と匿名化

1. User、Project、Repository、Agent などの ID はすべて UUID（不透明な ID）とする。Audit には ID だけを残す。
2. User 削除後の匿名化（`Deleted User`）は、Audit の行を書き換えず、User Store 側で ID と個人の対応を消して行う。

### 5. Audit Table の保護

1. `audit_events` は追記専用とする。Trigger が UPDATE / DELETE / TRUNCATE を拒否し、`PUBLIC` の権限を外す。
2. Migration の Role（Table の Owner。`PAW_MIGRATION_DATABASE_URL`）と Application の Role（`PAW_APP_DATABASE_ROLE`。INSERT と SELECT だけ）を分ける構成を推奨する。分けない開発の既定では、Application の誤った DML からしか守れず、起動時に WARNING を出す。
3. 保証しないもの: Superuser と Table の Owner による改ざん、Server や Backup の侵害、改ざんの検知（Hash Chain）。
4. Migration の `downgrade` は監査履歴を破棄する。開発・Test 用とする。
5. 保存期間、Partition、古い行の退避は未決とする（Table は削除できず、行数は増え続ける）。認証前の回数制限は PAW-022 で決める。

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
