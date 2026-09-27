# Task Working Set（Multi-Repo）の単位・承認・Write範囲・完了条件

- Status: Proposed
- Date: 2026-09-27
- Scope: Issue [#85](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/85)（Task Working Setの永続化）の実装に**先立って**決める、[Decision 0014](0014-task-working-set-persistence.md)「決まっていないこと」1〜5（Working Setの単位、Single-Repo Taskとの関係、Repo追加・役割変更の権限と承認、役割とWrite範囲の対応、Task全体の完了条件）。関連: PAW-027（#24）、PAW-031（#27）、PAW-032（#28、PR #70）、PAW-034（#30、PR #106、未Merge）、PAW-035（#31）、PAW-061 / 062
- Supersedes: なし（[Decision 0014](0014-task-working-set-persistence.md) を書き換えず、その「決まっていないこと」1〜5 を埋める補完 Decision。0014 の「提案」「想定する形」「承認後の扱い」はそのまま有効）
- Approval: 未承認

## 背景

[REQUIREMENTS.md](../../REQUIREMENTS.md)「Multi-Repo Task / Working Set」（`[FIXED]`）は次を定める。

- Taskごとに Working Set（Repoの集合）を持つ。通常は Single-Repo Task とし、必要な場合のみ Multi-Repo Task へ拡張する。
- Repo は Task 内で `referenced`（調査・検索・参照のみ）/ `working`（編集・テスト可能）/ `target`（PR作成まで行う対象）の役割を持てる。
- Read 範囲は比較的広く取ってよいが、Write 範囲は Working Set として明示・制御し、Agent が「ついでに」別 Repo を書き換えてはならない。Write 対象 Repo を増やす場合は Read 対象追加より慎重に扱い、曖昧な場合は User へ確認できるようにする。
- Multi-Repo Task では Repo ごとに Git 状態（worktree / branch、test、review、PR）を分離する。
- Task 全体の完了判定は、対象 Repo ごとの必要条件が満たされたかで判断する。

[Decision 0014](0014-task-working-set-persistence.md) は、Working Set の永続化を PAW-032（PR #70）の完了条件に含めず、担当を Issue #85（PAW-027 の後・PAW-034 の前）とすることを Human が 2026-09-25 に承認した。同時に、次の 5 点を「今は決めず、#85 の実装の前に別の Decision で決める」と承認した（特に 4 は保存より先に決める）。この Decision がその 5 点を扱う。

1. Working Set の単位（Task か、それ以外か）
2. Single-Repo Task との関係
3. Repo の追加と役割の変更の権限と承認
4. 役割と Write 範囲の対応
5. Task 全体の完了条件

PAW-034（Issue #30、PR #106、まだ Merge されていない）は、Decision 0006（Tool Broker）の義務として「Working Set は `TaskAuthority.parent_scope`（#85 の接続点）を通す」と述べており、暫定として「Working Set は呼び出し側が入力として渡す」(Decision 0021 背景) 前提で実装されている。#85 が Working Set を永続化した後は、この接続点が保存された表から解決する形に置き換わる想定であり、この Decision の 4 節はその置き換えが安全に行える設計を示す。

### 現状のコードから確認した事実

この Decision を書くにあたり、次のコードを実際に読んだ。

- `apps/backend/paw_backend/tools/scope.py`: `TaskScope.repositories` は `ScopedRepository`（`repo_id` / `project_id` / `root` / `acl: RepoAcl | None` / `remotes`）のタプルで、**role（referenced / working / target）に相当するフィールドを持たない**。
- `apps/backend/paw_backend/tools/broker.py::_resources`: Repository を対象とする Capability（`REPO_PERMISSION_OF` にある `project.read` / `project.repo.write` / `project.pr.create` / `project.task.run` / `project.agent.use`）は、`context.scope.repository(repo_id).acl`（= その Repo に対する呼び出し元の RBAC 上の RepoACL）だけを見て `Resource.repository(...)` を組み立て、認可判定 (`_authorize` → `authorize_agent_action`) に渡している。**Task の Working Set 上でその Repo が referenced / working / target のどれであるかを見る分岐は存在しない。**
- `apps/backend/paw_backend/tools/policy.py::ToolPolicy.level_for`: Approval Level は `ToolSpec.capabilities` / `environment` / Scope の状態（`ScopeStatus`）だけから決まり、Broker はそれと Tool ごとに固定の `ToolSpec.min_level` の厳しい方を取る。**呼び出しの引数（例えば要求された role）で Approval Level を変える入口はない。**
- `apps/backend/paw_backend/authz/policy.py::authorize`: `REPO_PERMISSION_OF` にない Capability に Repository 資源を渡すと `INVALID_RESOURCE` で拒否する。つまり Scope.PROJECT の新しい Capability を足すだけでは、`RepoAcl` による Repo ごとの判定は行われない。
- `apps/backend/paw_backend/tools/scope.py::classify_targets`: Path（symlink 解決後）・`REPOSITORY` 引数に加えて、Repo の登録済み remote の下にある URL 引数もその Repo への接触として `Classification.repositories` に数える。
- `apps/backend/paw_backend/authz/subjects.py::RepoAcl`: `allowed: frozenset[RepoPermission] | None`（`READ` / `WRITE` / `AGENT`）は、Project の Member が Repo に対して持つ RBAC 上の permission（Decision 0004）であり、**特定の Task がその Repo をどう扱ってよいかという概念は含まない**。
- `apps/backend/paw_backend/tasks/models.py`: `TaskAttemptRow` は 1 試行につき 1 組の `branch` / `worktree_path` / `head_commit` / `review_status` / `evaluation_result` / `pr_number` / `pr_url` / `pr_state` しか持てない（どの Repo の状態かを表す列がない）。同ファイルの docstring が「Multi-Repo working set … は PAW-032 の対象外」と明記している。
- PR #106（Decision 0021、未承認）は `ROLE_CEILING`（`domain.ROLE_CEILING`: Planner / Researcher / Reviewer は読み取り専用、Worker だけが書き込み可能な Capability を持つ）という「RBAC の上に、追加の Ceiling を掛けて絞る」パターンをすでに実装している。

つまり、**現状は「Working Set にその Repo が入っていて、かつ呼び出し元の RBAC が Write を許していれば、Task はその Repo へ書き込める」**という状態で、Task 内での役割（referenced かどうか）はまったく効いていない。#85 が Working Set を永続化しても、この 4 節を先に決めておかないと、「調査のためだけに `referenced` で追加した Repo に、（その Repo に対する通常の RBAC 権限を持つ Agent が）誤って書き込める」という穴をそのまま実装してしまう。Decision 0014 が「4 は保存より先に決める」とした理由はここにある。

## 提案

### 1. Working Set の単位: Task 単位とし、Restart でも維持する

**推奨: Working Set（Repo の集合とその役割）は Task に属し、Retry はもちろん、Restart（新しい試行）でも維持する。** Repo ごとの Git 状態（branch / worktree / review / evaluation / PR）だけが試行ごとに新しく作られる。

根拠:

- REQUIREMENTS.md は「Task ごとに Working Set を持つ」と述べ、試行ごとに持つとは述べていない。
- Decision 0014「想定する形」の `task_repositories` は `task_id`（`tasks` への FK）を主キーの一部として想定しており、試行への FK ではない。
- Working Set は「この Task が何に触れてよいか」という認可上の宣言（Mandate）であり、PAW-034 が Tool Broker へ渡す `TaskScope.repositories` の元になる。Restart は `starting_commit` と `input` から**同じ依頼を**やり直す操作であって、依頼の対象 Repo という Task の性質そのものを作り直す操作ではない。
- Restart のたびに Working Set の承認（3 節）をやり直す必要が生じると、要件の「Read 範囲は広く、Write 範囲は明示・慎重に」という運用と矛盾するコストが生まれる（同じ Repo に対して毎回 Approval を取り直す）。

Restart は Repo ごとの Git 状態（branch / worktree / head_commit / review / evaluation / PR）だけを新しい試行としてゼロから作る。Working Set の行（`task_repositories`）自体は Restart で削除・再作成しない。

### 2. Single-Repo Task との関係: 特別扱いしない

**推奨: すべての Task は Working Set を持つ。Single-Repo Task は「Working Set のサイズが 1 で、その 1 行の role が常に `target`」という特殊ケースにすぎず、永続化の形を Multi-Repo と分けない。**

- `task_repositories` は、Task ごとに **必ず 1 行以上** を持ち、そのうち **少なくとも 1 行は `target`** でなければならない（DB の CHECK ではなく、`target` が 0 件のまま Task を `running` にしないというサービス層の不変条件として持つ。0014「想定する形」の一意制約 `(task_id, repository_id)` に追加する形になる）。
- 現在の `TaskAttemptRow` の `branch` / `worktree_path` / `head_commit` / `review_status` / `evaluation_result` / `pr_number` / `pr_url` / `pr_state` は、#85 で `task_attempt_repositories`（`attempt_id` + `repository_id` を持つ新しい表。0014「想定する形」の「試行と Repo ごとの行」に対応）へ移す。Single-Repo Task も Multi-Repo Task も同じ経路（`task_repositories` に 1 行以上、`task_attempt_repositories` に試行ごとに 1 行以上）で保存する。
- `TaskAttemptRow` の該当列そのものをこの Decision の承認と同時に削除するかは、#85 の Migration 実装の詳細とする（本 Decision は「二重モデルを作らず統一する」という方針までを承認範囲とする）。

根拠: 独立 Review（PAW-032 の PR #70、Decision 0014 冒頭）が指摘したとおり、`task_attempts` は「どの Repo か」を表せない。Single-Repo を今の列のまま残し、Multi-Repo だけ新しい表を使う二重モデルにすると、`restore()` や Tool Broker、完了判定（5 節）が「Working Set があるかないか」で 2 つの経路に分岐し、実装・テストの分岐が増える。分岐が増えるほど、4 節で埋めようとしている「役割を見ずに書き込める」ような穴を作る側の分岐も増える。REQUIREMENTS.md の「通常は Single-Repo Task とし、必要な場合のみ Multi-Repo Task へ拡張する」は運用上の既定値（普段は 1 Repo しか Working Set に入れない）についての記述であり、永続化の形を分けるべきだとは述べていない、と読む。

### 3. Repo の追加・役割変更の権限と承認

**推奨: Working Set の変更（Repo の追加・削除、role の昇格・降格）は、Tool Broker / 認可層を通る操作として扱い、変更の種類ごとに必要な Approval Level と、対象 Repo に対する RBAC 上の permission を固定する。`referenced` としての新規追加だけを `SCOPED_AUTO` とし、それ以外の変更はすべて `STRONG_APPROVAL` とする。**

- 新しい Capability `project.task.working_set.manage`（Scope.PROJECT）を `authz/capabilities.py` に追加する。既存の `project.repo.add`（プロジェクトへ Repo を登録する Capability、Decision 0017）とは別物であり、こちらは「特定 Task の Working Set にどの Repo をどの role で入れるか」を扱う。この Capability は **Project 資源に対する判定**（Project Role がこの Task の Working Set を管理してよいか）であり、これだけでは対象 Repo の `RepoAcl` は一切見られない（`tools/broker.py::_resources` は `REPO_PERMISSION_OF` にない Capability には Project 資源しか組み立てず、`authz/policy.py::authorize` は `REPO_PERMISSION_OF` にない Capability に Repository 資源を渡すと `INVALID_RESOURCE` で拒否する）。そのため、次の「対象 Repo への認可」を**別の判定として必ず追加で**行う。
- **変更の種類ごとに Tool Spec を分ける（role を引数で選ばせない）。** 現在の `ToolPolicy.level_for()` は `ToolSpec.capabilities` / `environment` / Scope の状態しか受け取らず、`ToolSpec.min_level` も Tool ごとに固定なので、1 つの Tool の中で「要求された role」によって Approval Level を変えることはできない（`SCOPED_AUTO` に置けば `working` / `target` への昇格が Step-up なしで通り、`STRONG_APPROVAL` に置けば `referenced` 追加の自動化と矛盾する）。そこで、結果の role を Tool Spec 自体に固定した別々の Tool として登録する（名前は例）。
  - `task.working_set.add_referenced`: Working Set に**まだ無い** Repo を `referenced` で追加するだけの Tool。`ToolSpec.min_level = SCOPED_AUTO` とし、Policy 表の結果がそれより厳しければそちらに従う（`AUTO` にはならない）。既に Working Set にある Repo を指定した呼び出しは（`working` / `target` からの降格にあたり得るため）この Tool では拒否する。
  - `task.working_set.set_working` / `task.working_set.set_target`: 追加・昇格とも、`ToolSpec.min_level = STRONG_APPROVAL`。
  - `task.working_set.downgrade`（`target → working`、`target` / `working → referenced`）と `task.working_set.remove`（Working Set からの削除）: `ToolSpec.min_level = STRONG_APPROVAL`（下記）。
  - どの Tool も role を表す引数を持たず、結果の role は Tool 名（Spec）だけで決まる。`min_level` は Policy 表の結果を**厳しくする方向にしか**働かない（Broker は `most_restrictive(level_for(...), min_level)` を取る）ので、この分割で Approval Level を緩める経路は生まれない。これら以外の Tool が Working Set を変更することは、次項の登録時検査で構造的に禁止する。
- **対象 Repo への認可（動的な Repository 資源の判定）。** 追加・変更する Repo は、変更前は Working Set（`TaskScope.repositories`）に入っていないことがあるため、既存の `ArgumentKind.REPOSITORY`（`classify_targets` が Working Set 外として `OUT_OF_SCOPE` にする）では表せない。そこで、Working Set 変更の対象 Repo を表す専用の引数種別（例: `ArgumentKind.WORKING_SET_REPOSITORY`）を設け、Broker の明示的な拡張として次を行う。
  1. 対象 Repo は Task の Scope 内の Project（`TaskScope.projects`）に登録済みの Repo でなければならない（そうでなければ `OUT_OF_SCOPE` として拒否）。
  2. その Repo の `RepoAcl` を Repo 登録（Decision 0017）から解決し、`Resource.repository(...)` を組み立てる。解決できなければ既存どおり `repo_acl_unresolved` として拒否する（`inherit` と読まない）。
  3. その Repository 資源に対して、Tool Spec ごとに固定した**代理 Capability** で既存の `authorize_agent_action` を呼ぶ: `add_referenced` は `project.read`（`RepoPermission.READ`）、`set_working` / `set_target` は `project.repo.write`（`RepoPermission.WRITE`）、`downgrade` / `remove` は**変更前の** role が `working` / `target` なら `project.repo.write`（WRITE）、`referenced` なら `project.read`（READ）。
  4. Project 資源に対する `project.task.working_set.manage` の判定と、この Repository 資源に対する判定の**両方が許可したときだけ**変更を許す（AND）。どちらかが拒否すれば拒否。これにより、Project の Member であっても対象 Repo の ACL で READ / WRITE を拒否されている者は、その Repo の Working Set の行を変更できない。
  5. `WORKING_SET_REPOSITORY` 引数を持てるのは上記の Working Set 変更 Tool だけで、その `authz_capability` は `project.task.working_set.manage` でなければならない。逆に、この Capability を宣言する Tool はこの引数を必須で持たなければならない。どちらも Tool 登録時（`ToolSpec.__post_init__`）に検査し、違反は `ValueError` とする（他の Tool がこの引数種別を使って Scope 判定・Role Ceiling を迂回することを防ぐ）。
  6. 新しい `REPO_PERMISSION_OF` の項目は追加しない（1 つの Capability に対して READ / WRITE の 2 通りの写像は静的な表では表せないため）。代理 Capability の対応は Tool Spec 側で固定する。
- 必要な Approval Level:
  - `referenced` への新規追加: 対象 Repo に `RepoPermission.READ` があれば `SCOPED_AUTO`（要件「Read 範囲は比較的広く取ってよい」に対応）。
  - `working` への追加・昇格、`target` への追加・昇格（PR 作成まで許す）: 対象 Repo に `RepoPermission.WRITE` を要求し、どちらも **`STRONG_APPROVAL`**（Step-up 認証必須、Decision 0015）。REQUIREMENTS.md の Approval Level の固定表（`[FIXED]`、4節）が「ACL / Role / Permission変更」を`STRONG_APPROVAL`と定めており、Task が Repo に対して持てる Write / PR 作成の権限を広げるこの操作はまさにそれに当たる。`APPROVAL`（Step-up なし）では固定要件に満たない（**Codex Reviewの指摘で `APPROVAL` から訂正**）。
  - role を下げる（`target → working`、`target` / `working → referenced`）操作と、Working Set からの削除も **`STRONG_APPROVAL`** とする。固定表の「ACL / Role / Permission変更」には狭める方向の例外がなく、role の降格も Role / Permission の変更そのものだからである（**Codex Reviewの指摘で `SCOPED_AUTO` から訂正**）。固定表を緩める例外は、この Decision ではなく REQUIREMENTS.md の改訂としてしか作れない。
  - **唯一の `target` の保護**: その Repo がその試行の唯一の `target` である場合、`target` からの降格・削除は拒否する（`ScopeEscalation`と対になる `LastTargetRemovalRefused`のような理由）。2節の不変条件（Task は必ず 1 つ以上の `target` を持つ）は `running` への遷移時だけでなく、Working Set のあらゆる変更（このCapabilityの確定時）でも保つ。唯一の `target` を本当に外したいなら、先に別の Repo を `target` に上げてからでなければならない（同一操作内で 1 つ減らして 1 つ増やすのは許可してよい）。
  - **変更済み Repo の完了義務の保護**: 現在の試行でその Repo に変更が加わっている（5 節の「変更済み」）場合、降格・削除は、その変更が**検証可能な形で破棄されたとき**（Backend が確認する: worktree が clean で HEAD がその試行の `starting_commit` と一致し、その試行の branch が remote に push されておらず、その Repo に open の PR がない）にだけ許す。破棄されていなければ `STRONG_APPROVAL` があっても拒否する（例: `ModifiedRepositoryDowngradeRefused`）。Agent の「破棄した」という申告は判定に使わない（AGENTS.md 1.3）。これは 5 節の完了義務を降格・削除ですり抜けさせないための条件である（**Codex Reviewの指摘で追加**）。
- 承認は新しい待ち状態を作らず、既存の `TaskState.WAITING`（`wait_reason = WaitReason.APPROVAL`）を使う。`WaitReason.APPROVAL` は既に「Merge / Delete / ACL / permission change」のための理由として定義されており（`tasks/domain.py`）、Repo の role 変更はまさに「permission change」に当たる。`STRONG_APPROVAL`のStep-up確認自体は、既存の`StepUpVerifier`（PAW-023）が行う。
- 変更は `task_events`（追記専用）へ、Actor・変更前後の role・理由とともに記録する（Decision 0014 で既に決定済みの方針をそのまま踏襲する）。
- Planner / Orchestrator（PAW-034）は Working Set の変更を**提案**できるだけで、確定させるのは Backend の認可判定（および必要な Approval）である。Decision 0021 §2 の「Plan が親の持たない Capability・Working Set にない Repository を求めた Node は Escalation せず失敗させる（`ScopeEscalation`）」という既存方針と整合させる。

根拠: 要件が空白にしている「誰が・どう承認するか」を、新しい仕組みを作らず既存の Tool Broker の Approval Level と Task の Waiting 状態に載せることで、承認経路を 1 本化する。RBAC（そのユーザーがそもそもその Repo に Write できるか）と Task 側の承認（この Task の目的でこの Repo を書いてよいか）は別の質問であり、両方を要求する。`referenced`の新規追加以外（`working`/`target`への昇格、降格、削除）をすべて`STRONG_APPROVAL`にするのは、REQUIREMENTS.mdの固定表（「ACL / Role / Permission変更」に方向による例外がない）を実装が緩めないため（Approval Levelは「狭める方向にしか動かせない」という既存原則、Decision 0006の考え方と同じ）。Tool Specを変更の種類ごとに分けるのは、現在の`ToolPolicy.level_for()`が要求されたroleを入力に持たず、1つのToolでは役割ごとのApproval Levelを安全に表せないため。Project資源の判定に加えて対象RepoのRepository資源を代理Capabilityで判定するのは、Scope.PROJECTのCapabilityだけでは`RepoAcl`が見られないため（Codex Reviewの指摘）。Human が UI / API から Working Set を変更する場合も、同じ判定（Approval Level・Project 資源と Repository 資源の AND・唯一の target と変更済み Repo の保護）を行う同じ Backend の関数を通し、Tool Broker を経由しない経路で判定が緩まないようにする。唯一の`target`の降格を拒否するのは、2節の不変条件を「作成時だけ守ればよい」ものにしないため（Codex Reviewの指摘: そうしないと5節の完了条件が`target`のPR義務をすり抜けられてしまう）。

### 4. 役割と Write 範囲の対応（最重要点）

**現状の問題**: 上の「現状のコードから確認した事実」のとおり、今のコードには Task 内での Repo の役割という軸がなく、Working Set に入っていて RBAC (`RepoAcl`) が Write を許しているだけで書き込みが通ってしまう。#85 はこれを塞ぐ設計を持たなければならない。

**推奨する設計**:

1. `ScopedRepository`（`tools/scope.py`）に `role: RepoRole` フィールドを追加する（`RepoRole` は `REFERENCED` / `WORKING` / `TARGET` の `StrEnum`。REQUIREMENTS.md の 3 値そのまま）。値は Backend（Orchestrator / TaskContext を組み立てる層）が、永続化された `task_repositories.role` から解決して埋める。「触れる Repository は Backend が決める」という Decision 0006 §1 の原則の主体は変えない。モデルの出力が role を名指しすることはできない。
2. role が Write 系 Capability に許す上限を、固定の表 `ROLE_WRITE_CEILING` として定義する（PAW-034 / Decision 0021 の `ROLE_CEILING` と同じ「上限は絞るだけで広げない」パターンの再利用）。

   | role | 許される Capability（Repo 資源に対して） |
   | --- | --- |
   | `referenced` | `project.read`、`project.memory.use` のみ。`project.repo.write` と `project.pr.create` は常に拒否 |
   | `working` | 上記に加えて `project.repo.write`。`project.pr.create` は拒否 |
   | `target` | 上記に加えて `project.pr.create` |

   この表は `spec.authz_capability` が `project.repo.write` / `project.pr.create` である Tool だけを見る。`ToolSpec.capabilities` に `ToolCapability.WRITE` / `DESTRUCTIVE` を宣言していながら、対象が Repository で `authz_capability` がこの2つ**以外**（例: `project.task.run`）という Tool は、この表からは見えず Ceiling をすり抜ける（Codex Reviewの指摘: 全roleで拒否されるか、逆にreferencedでも素通りするかのどちらかになる）。これを防ぐため、Tool 登録時の一貫性検査（`ToolSpec.__post_init__`、既存の「Repository の書き込みは Path か Repository を要求する」検査と同じ場所）に次を追加する: **`environment` が `PROJECT_LOCAL` で、`capabilities` に `WRITE` か `DESTRUCTIVE` を含み、対象引数の種別（`ArgumentKind`）に `PATH` か `REPOSITORY` を含む Tool は、`authz_capability` が `REPO_PERMISSION_OF` で `RepoPermission.WRITE` に写る（= `project.repo.write` か `project.pr.create`）ものでなければならない**（さもなくば登録時に `ValueError`）。ただし、この登録時検査だけでは足りない。`tools/scope.py::classify_targets()` は、`PATH` / `REPOSITORY` だけでなく、**Working Set の Repo の登録済み remote（`ScopedRepository.remotes`）の下にある URL** もその Repo への接触として `Classification.repositories` に数える。URL だけを必須引数に持つ Network の書き込み Tool（`environment` は `PROJECT_LOCAL` でも `HOST` でもよい）は上の静的検査にかからず、`project.task.run` などを宣言して Ceiling の外で Repo を書き換えられてしまう（Codex Reviewの指摘）。どの URL がどの Repo に属するかは実行時の remote 登録で決まるため、静的な検査では判定できない。そこで、Tool Broker の呼び出し時に次の**実行時検査**を追加する: **`classify_targets()` の結果 `classification.repositories` が 1 つ以上の Repo を含み（Path・Repository・remote 配下の URL のどれによる接触かを問わない）、かつ `spec.capabilities` に `WRITE` か `DESTRUCTIVE` を含む呼び出しは、`spec.authz_capability` が `REPO_PERMISSION_OF` で `RepoPermission.WRITE` に写るものでなければ、`environment` に関係なく拒否する**（新しい `BrokerReason`、例: `repository_write_capability_mismatch`。Approval があっても通さない）。この検査は Approval Level の決定と認可判定の前に行い、`classify_targets()` がその呼び出しで Repo を特定できなかった場合は既存の扱い（`unbound_url` の `OUT_OF_SCOPE`、`repository_not_identified`）のまま拒否側に倒れる。静的検査（登録時）と実行時検査の両方により、role Ceiling の対象になり得る「Repositoryへの書き込み効果を持つ呼び出し」は、どの経路で Repo を指定しても、必ずこの表がカバーする2つの `authz_capability` のどちらかを通ることになり、すり抜けが構造的になくなる。

3. ある Repo 資源への Capability 行使は、**RBAC（`RepoAcl` が許す `RepoPermission`）と role Ceiling（上の表）の両方を満たしたときだけ許可する（AND、狭める方向にしか働かない）**。どちらか一方が拒否すれば拒否になる。実装は `tools/broker.py::_resources` / `_authorize` に、既存の RBAC 判定（`authorize_agent_action`）とは別に、role Ceiling を見る判定を 1 段追加する形になる（既存の RBAC 判定のコードは変更しない）。
4. **Approval があっても role Ceiling は超えられない。** `referenced` の Repo に対する書き込みは、Human が個別に Approval を与えても許可しない（Decision 0006 §1.2「Approval は認可・Scope・Budget を使うときにもう一度確認する」と同じ考え方で、role Ceiling は Scope の一部として扱う）。role を上げたいなら 3 節の Working Set 変更の手続きを通す。これによって「誤って `target` でない Repo に書き込めてしまう」穴を、個別の呼び出しの Approval では回避できない形にする。
5. role が解決できない（Working Set にその Repo がない、または壊れた状態で role が読めない・未知の値である）場合は、**その Repo に対するすべての Capability の行使を拒否する（fail-closed）**。`referenced` として扱うことはしない（`referenced` は `project.read` / `project.memory.use` を許す正当な role なので、未解決を `referenced` に読み替えると、古い `TaskScope` の項目や壊れた永続化の行が Repo と Memory への読み取りを保持し続けてしまう。**Codex Reviewの指摘で「`referenced` として扱う」から訂正**）。実装上は、role を `RepoRole | None` のように「未解決」を正当な role と区別できる形で持ち、未解決なら Ceiling の表を引かずに拒否する。これは `RepoAcl` が未解決のとき `inherit` と読まずに拒否する既存の設計（`authz/subjects.py::RepoAcl` の docstring、`repo_acl_unresolved`）と対称的である。新しい `BrokerReason` として、role が Ceiling を満たさない `repository_role_insufficient` と、role が解決できない `repository_role_unresolved` を分けて追加し、後者は `repo_acl_unresolved` と同様に「未解決は常に拒否」という性質を明示する。
6. `TaskService.restore()` が返す `TaskSnapshot` は Working Set（Repo・role）を含む（Decision 0014 で既に決定済み）。Tool Broker 以外（UI、Orchestrator）もこの表を「正」として参照し、role を二重に持たない。

**検討した他の案**:

- **A. Repo の RBAC 上の ACL（`RepoAcl.allowed`）自体を、role が変わるたびに動的に書き換える。** 却下。`RepoAcl` は Project の Member 全体に対する恒久的な RBAC 設定（Decision 0004）であり、特定 Task の一時的な制限をここに混ぜると、その Task と無関係な他の Task・UI からの認可判定まで巻き込む副作用が大きい。
- **B. role を Ceiling ではなく Working Set への membership だけの情報にとどめ、Write 可否は引き続き RBAC の `RepoAcl` だけに任せる。** 却下。これはまさに現状の穴であり、要件の「Write 範囲を Working Set として明示・制御する」を満たさない。
- **C. role Ceiling を Tool Broker ではなく Orchestrator（PAW-034）側だけで強制する。** 却下。Tool Broker は「Backend が最終判定する」層であり（Decision 0006）、Orchestrator を経由しない呼び出し経路が将来できたときにも穴を残さないよう、強制は Tool Broker 側に置く（Orchestrator 側で Plan の `repositories` / role を検査するのは追加の防御としてよい）。

### 5. Task 全体の完了条件

**推奨**: `complete` への遷移だけを、Working Set 内の Repo ごとの必要条件を集約して判定する。**`begin_evaluation`（`running` → 評価中）はこの節の対象外とし、PAW-032 が今すでに持つ前提条件（Repo・Working Setと無関係）から変えない**（Codex Reviewの指摘: 新しい試行は evaluation が `NOT_RUN` から始まるため、`begin_evaluation` の前提に「evaluation が PASS していること」を含めると、評価そのものが始められなくなる循環になる）。

- 各 `target` Repo: その試行の `task_attempt_repositories` 行の evaluation が PASS し、かつ **PR が存在し `pr_state` が `open` か `merged` のいずれかであること**（`draft` はまだ「作成された」と扱わず不十分、`closed`（unmerged）は目的を達しなかった終端として不十分。REQUIREMENTS.md の `target` は「PR 作成まで行う対象」であり Merge そのものは求めない: Mergeは常に人間の権限（AGENTS.md）で、通常のTaskは自分ではMergeしないため、`merged` だけを要求すると通常経路のTaskが永久に`complete`できなくなる。**Codex Reviewの指摘で「成功側の終端（`merged`）」から訂正**）。
- 各 `working` Repo（実際に変更が加わったもの）: evaluation が PASS していること。PR は要求しない（`target` ではないため）。
- `referenced` Repo: 完了条件に含めない（読み取り専用であり、evaluation も PR も発生しない）。**ただし、次の「変更済み Repo の義務」に当たるものを除く。**
- **変更済み Repo の義務（降格・削除で消えない）**: 完了条件の対象は「`complete` 判定の時点で Working Set にある role」だけで決めない。**現在の試行の中で一度でも変更が加わった Repo**（その試行で `project.repo.write` / `project.pr.create` の呼び出しが許可された Repo、または `starting_commit` からの差分・commit・push・PR のいずれかがある Repo）は、その後に降格されても Working Set から削除されても、**その試行中にその Repo が持った最も強い role** に応じた義務を負い続ける（その間に `target` だったなら evaluation PASS + PR、`working` だったなら evaluation PASS）。義務を外せるのは、3 節の「検証可能な破棄」を Backend が確認した上で降格・削除した場合だけであり、その確認結果は `task_events` に記録する。変更の有無を Backend が判定できない（worktree・branch の状態が読めない等）場合は「変更済み」として扱う（fail-closed）。Agent の申告は判定に使わない（**Codex Reviewの指摘で追加**: そうしないと、2 つの `target` の一方を変更してから降格し、その Repo の evaluation も PR もないまま `complete` できてしまう）。
- 1 つでも必要条件を満たさない Repo があれば、Task 全体は `complete` に遷移しない。新しい状態は作らず、既存の `Failed` / `Waiting` の状態機械（PAW-032）をそのまま使う。
- `restore()` の `TaskSnapshot` は Repo ごとの evaluation / PR 状態を全て返し、呼び出し側（UI、Orchestrator）が「どの Repo が未完了か」を判別できるようにする。

根拠: REQUIREMENTS.md の「Task 全体の完了判定は、対象 Repo ごとの必要条件が満たされたかで判断する」をそのまま実装した形であり（「対象 Repo」は判定時点の role だけでなく、試行中に実際に変更した Repo を含むと読む。完了判定の直前に role を下げれば義務が消える設計は、要件の「Write 範囲を明示・制御する」を形骸化させる）、Decision 0014「想定する形」がすでに Repo ごとの evaluation / PR 状態を持たせる設計を示している。`begin_evaluation`を対象外にするのは、既存の状態機械（PAW-032）の遷移条件をこのDecisionが書き換えない（このDecisionはWorking Setの5点を埋めるだけで、既存のTask Lifecycleの前提を変える権限を持たない）ことの帰結でもある。

**検討した他の案**:
- 全 Repo 一律で evaluation の PASS だけを完了条件とし、PR の有無は Task の完了条件に含めない（PR 作成を完了後の別処理にする）。却下。REQUIREMENTS.md の `target` の定義そのものが「PR 作成まで行う対象」であり、PR 作成を `target` の完了要件から外すと要件を弱めることになる。
- `target` の完了条件に `merged` を要求する。却下（Codex Reviewの指摘、上記）。

## 影響

- [Decision 0014](0014-task-working-set-persistence.md) の「提案」「想定する形」は変更しない。この Decision はその「決まっていないこと」1〜5 を埋めるだけである。
- PAW-034（PR #106、Decision 0021）は、この Decision が承認された後、「Working Set は呼び出し側が入力として渡す」という現状の暫定（Decision 0021 背景、Decision 0014 の見送り理由 4）を、「永続化された Working Set から解決する」形に置き換える必要がある。これは Decision 0021 自体の変更ではなく、#85 の実装が Orchestrator 側の組み立てを差し替えるだけである。
- `apps/backend/README.md`「Multi-Repo Task の Working Set … は PAW-032 に含みません」の記述は、#85 の実装時に「この Decision が示す形で持ちます」という記述へ更新する（この PR の対象外。#85 実装 PR で行う）。
- `authz/capabilities.py` に新しい Capability（`project.task.working_set.manage`）を追加し、`tests/test_authz_policy.py` の表を更新する必要がある（#85 実装 PR で行う）。
- 新しい `RepoRole` / `ROLE_WRITE_CEILING` は `tools/scope.py` と `tools/broker.py` に、`tools/capabilities.py` の `BrokerReason` に新しい値（`repository_role_insufficient` / `repository_role_unresolved` / `repository_write_capability_mismatch` 等）の追加が要る（#85 実装 PR で行う）。
- Working Set 変更用の Tool Spec（変更の種類ごと）、専用の引数種別（例: `WORKING_SET_REPOSITORY`）とその登録時検査、対象 Repo の Repository 資源を代理 Capability で判定する Broker の拡張、`classification.repositories` に対する実行時の書き込み Capability 検査が `tools/registry.py` / `tools/broker.py` に要る（#85 実装 PR で行う）。既存の `authz/policy.py::authorize` と `REPO_PERMISSION_OF` の表は変更しない。

## 承認後の扱い

承認されたら Status を Approved に改め、`Approval` に日付と承認の様子を記録する。#85 の実装 PR はこの Decision を参照する。
承認後に方針を変える場合は、この Decision を書き換えず、新しい Decision から `Supersedes` する。
[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。[Decision 0014](0014-task-working-set-persistence.md) も書き換えない。

## 推測した点（実装者が要件から解釈した点）

次は要件に明記がなく、実装者（この Decision の提案者）が既存コードの慣習（`REPO_PERMISSION_OF`、`WaitReason.APPROVAL`、PAW-034 の `ROLE_CEILING`）に合わせて解釈した細部である。1〜5 節の主要な決定より優先度が低く、#85 の実装時に Backend README で確定させる程度でよいと考える。

- 新しい Capability の名前 `project.task.working_set.manage` と、新しい `BrokerReason` の名前 `repository_role_insufficient`。命名規則（小文字ドット区切り、既存の列挙との整合）は #85 実装時に合わせる。
- Working Set からの `referenced` の Repo の削除（読み取り範囲を狭めるだけの操作）も `STRONG_APPROVAL` とした点（固定表の「ACL / Role / Permission変更」に方向による例外がないため、安全側に倒した）。
- 「検証可能な破棄」の判定条件（worktree が clean、HEAD が `starting_commit`、branch 未 push、open の PR なし）と、「変更済み」の判定条件の細部。いずれも判定できないときは破棄されていない・変更済みとして扱う方向でのみ #85 実装時に具体化する。
- Working Set 変更 Tool の名前（`task.working_set.add_referenced` 等）、専用の引数種別の名前、新しい `BrokerReason` の名前。
- `working` への昇格と `target` への昇格を同じ `STRONG_APPROVAL` レベルにそろえた点（要件はこの 2 つを区別する記述をしていない）。

## 決めてほしいこと

1. **Working Set は Task 単位とし、Restart でも維持する**（1 節）でよいか。推奨: はい。
2. **Single-Repo Task も `task_repositories` に 1 行（role=target）を持つ統一モデルにし、`task_attempts` の worktree / review / PR 列を Repo ごとの新しい表（`task_attempt_repositories`）へ移す**（2 節）でよいか。推奨: はい。
3. **Repo の追加・役割変更を新しい Capability `project.task.working_set.manage` 経由にし、変更の種類ごとに別の Tool Spec（結果の role を Spec に固定）とする。`referenced` の新規追加だけを `SCOPED_AUTO`、それ以外（`working` / `target` への追加・昇格、降格、削除）はすべて `STRONG_APPROVAL` とする（REQUIREMENTS.mdの固定表「ACL/Role/Permission変更」に合わせる）。Project 資源への判定に加えて、対象 Repo の Repository 資源を `referenced` なら READ、`working` / `target` なら WRITE（降格・削除は変更前の role による）で判定し、両方を満たすときだけ許す。唯一の`target`の降格・削除は拒否し、その試行で変更済みの Repo の降格・削除は変更が検証可能に破棄されるまで拒否する**（3 節）でよいか。推奨: はい（Codex Reviewの指摘で、`APPROVAL`から`STRONG_APPROVAL`へ、唯一のtarget保護の追加、降格も`STRONG_APPROVAL`へ、role ごとの Tool Spec 分割、対象 Repo の ACL 判定、変更済み Repo の保護を追加で訂正）。
4. **role（referenced / working / target）による Write 範囲の Ceiling を、RBAC（`RepoAcl`）とは別の軸として Tool Broker に追加し、両方を満たしたときだけ許可する（AND）。Approval があっても Ceiling は超えられない。Ceilingがカバーする2つの`authz_capability`以外でRepositoryへの書き込み効果を持つToolの登録を拒否する一貫性検査と、remote 配下の URL を含めて Repo に接触する書き込み呼び出しを同じ条件で拒否する実行時検査を追加する**（4 節）でよいか。推奨: はい（この Decision の核。一貫性検査と、URL を含む実行時検査はCodex Reviewの指摘で追加）。
5. **role が解決できないときは、その Repo に対するすべての Capability を拒否する（`referenced` として扱わない、fail-closed）**（4 節の 5）でよいか。推奨: はい（Codex Reviewの指摘で「`referenced` として扱う」から訂正）。
6. **Task全体の完了条件を、`complete`遷移でのみ判定する（`begin_evaluation`はこのDecisionの対象外）。`target` Repoはevaluation PASS + PRが`open`か`merged`のいずれか、`working` Repoはevaluation PASSのみ、`referenced` Repoは対象外、とする。ただし試行中に変更した Repo は、降格・削除後も試行中に持った最も強い role の義務を負う（検証可能な破棄を除く）**（5 節）でよいか。推奨: はい（Codex Reviewの指摘で、`begin_evaluation`を対象から外し、PR状態を`merged`のみから`open`/`merged`へ訂正し、変更済み Repo の義務の保持を追加）。
