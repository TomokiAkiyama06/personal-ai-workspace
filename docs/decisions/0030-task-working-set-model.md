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

**推奨: Working Set の変更（Repo の追加、role の昇格 `referenced → working → target`）は、Tool Broker / 認可層を通る 1 つの操作として扱い、role に応じて必要な Approval Level を段階的に上げる。**

- 新しい Capability `project.task.working_set.manage`（Scope.PROJECT）を `authz/capabilities.py` に追加する。既存の `project.repo.add`（プロジェクトへ Repo を登録する Capability、Decision 0017）とは別物であり、こちらは「特定 Task の Working Set にどの Repo をどの role で入れるか」を扱う。
- 呼び出し元（Human、または Human の承認を経た Orchestrator の提案の確定）が、対象 Repo に対して RBAC 上（`RepoAcl`）どこまでの `RepoPermission` を持つかで、必要な Approval Level を決める（既存の `REPO_PERMISSION_OF` の考え方を流用する）。
  - `referenced` への追加: 対象 Repo に `RepoPermission.READ` があれば `SCOPED_AUTO`（要件「Read 範囲は比較的広く取ってよい」に対応）。
  - `working` への追加・昇格: 対象 Repo に `RepoPermission.WRITE` を要求し、`APPROVAL`（要件「Write 対象 Repo を増やす場合は Read 対象追加より慎重に」に対応）。
  - `target` への追加・昇格（PR 作成まで許す）: 同じく `RepoPermission.WRITE` を要求し、`APPROVAL`。`STRONG_APPROVAL` まで上げるかは「決めてほしいこと」に残す。
  - role を下げる（`target → working` 等）や `referenced` へ戻すことは、Write 範囲を狭める操作なので `SCOPED_AUTO` でよい。
- 承認は新しい待ち状態を作らず、既存の `TaskState.WAITING`（`wait_reason = WaitReason.APPROVAL`）を使う。`WaitReason.APPROVAL` は既に「Merge / Delete / ACL / permission change」のための理由として定義されており（`tasks/domain.py`）、Repo の role 変更はまさに「permission change」に当たる。
- 変更は `task_events`（追記専用）へ、Actor・変更前後の role・理由とともに記録する（Decision 0014 で既に決定済みの方針をそのまま踏襲する）。
- Planner / Orchestrator（PAW-034）は Working Set の変更を**提案**できるだけで、確定させるのは Backend の認可判定（および必要な Approval）である。Decision 0021 §2 の「Plan が親の持たない Capability・Working Set にない Repository を求めた Node は Escalation せず失敗させる（`ScopeEscalation`）」という既存方針と整合させる。

根拠: 要件が空白にしている「誰が・どう承認するか」を、新しい仕組みを作らず既存の Tool Broker の Approval Level と Task の Waiting 状態に載せることで、承認経路を 1 本化する。RBAC（そのユーザーがそもそもその Repo に Write できるか）と Task 側の承認（この Task の目的でこの Repo を書いてよいか）は別の質問であり、両方を要求する。

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

3. ある Repo 資源への Capability 行使は、**RBAC（`RepoAcl` が許す `RepoPermission`）と role Ceiling（上の表）の両方を満たしたときだけ許可する（AND、狭める方向にしか働かない）**。どちらか一方が拒否すれば拒否になる。実装は `tools/broker.py::_resources` / `_authorize` に、既存の RBAC 判定（`authorize_agent_action`）とは別に、role Ceiling を見る判定を 1 段追加する形になる（既存の RBAC 判定のコードは変更しない）。
4. **Approval があっても role Ceiling は超えられない。** `referenced` の Repo に対する書き込みは、Human が個別に Approval を与えても許可しない（Decision 0006 §1.2「Approval は認可・Scope・Budget を使うときにもう一度確認する」と同じ考え方で、role Ceiling は Scope の一部として扱う）。role を上げたいなら 3 節の Working Set 変更の手続きを通す。これによって「誤って `target` でない Repo に書き込めてしまう」穴を、個別の呼び出しの Approval では回避できない形にする。
5. role が解決できない（Working Set にその Repo がない、または壊れた状態で role が読めない）場合は、**`referenced` として扱う（fail-closed）**。これは `RepoAcl` が未解決のとき `inherit` と読まずに拒否する既存の設計（`authz/subjects.py::RepoAcl` の docstring）と対称的な安全側のデフォルトである。新しい `BrokerReason`（例: `repository_role_insufficient`）を追加し、`repo_acl_unresolved` と同様に「未解決は常に拒否」という性質を明示する。
6. `TaskService.restore()` が返す `TaskSnapshot` は Working Set（Repo・role）を含む（Decision 0014 で既に決定済み）。Tool Broker 以外（UI、Orchestrator）もこの表を「正」として参照し、role を二重に持たない。

**検討した他の案**:

- **A. Repo の RBAC 上の ACL（`RepoAcl.allowed`）自体を、role が変わるたびに動的に書き換える。** 却下。`RepoAcl` は Project の Member 全体に対する恒久的な RBAC 設定（Decision 0004）であり、特定 Task の一時的な制限をここに混ぜると、その Task と無関係な他の Task・UI からの認可判定まで巻き込む副作用が大きい。
- **B. role を Ceiling ではなく Working Set への membership だけの情報にとどめ、Write 可否は引き続き RBAC の `RepoAcl` だけに任せる。** 却下。これはまさに現状の穴であり、要件の「Write 範囲を Working Set として明示・制御する」を満たさない。
- **C. role Ceiling を Tool Broker ではなく Orchestrator（PAW-034）側だけで強制する。** 却下。Tool Broker は「Backend が最終判定する」層であり（Decision 0006）、Orchestrator を経由しない呼び出し経路が将来できたときにも穴を残さないよう、強制は Tool Broker 側に置く（Orchestrator 側で Plan の `repositories` / role を検査するのは追加の防御としてよい）。

### 5. Task 全体の完了条件

**推奨**: `begin_evaluation` / `complete` への遷移は、Working Set 内の Repo ごとの必要条件を集約して判定する。

- 各 `target` Repo: その試行の `task_attempt_repositories` 行の evaluation が PASS し、かつ有効な PR が存在すること（`pr_state` が成功側の終端にあること）。
- 各 `working` Repo（実際に変更が加わったもの）: evaluation が PASS していること。PR は要求しない（`target` ではないため）。
- `referenced` Repo: 完了条件に含めない（読み取り専用であり、evaluation も PR も発生しない）。
- 1 つでも必要条件を満たさない Repo があれば、Task 全体は `complete` に遷移しない。新しい状態は作らず、既存の `Failed` / `Waiting` の状態機械（PAW-032）をそのまま使う。
- `restore()` の `TaskSnapshot` は Repo ごとの evaluation / PR 状態を全て返し、呼び出し側（UI、Orchestrator）が「どの Repo が未完了か」を判別できるようにする。

根拠: REQUIREMENTS.md の「Task 全体の完了判定は、対象 Repo ごとの必要条件が満たされたかで判断する」をそのまま実装した形であり、Decision 0014「想定する形」がすでに Repo ごとの evaluation / PR 状態を持たせる設計を示している。

**検討した他の案**: 全 Repo 一律で evaluation の PASS だけを完了条件とし、PR の有無は Task の完了条件に含めない（PR 作成を完了後の別処理にする）。却下。REQUIREMENTS.md の `target` の定義そのものが「PR 作成まで行う対象」であり、PR 作成を `target` の完了要件から外すと要件を弱めることになる。

## 影響

- [Decision 0014](0014-task-working-set-persistence.md) の「提案」「想定する形」は変更しない。この Decision はその「決まっていないこと」1〜5 を埋めるだけである。
- PAW-034（PR #106、Decision 0021）は、この Decision が承認された後、「Working Set は呼び出し側が入力として渡す」という現状の暫定（Decision 0021 背景、Decision 0014 の見送り理由 4）を、「永続化された Working Set から解決する」形に置き換える必要がある。これは Decision 0021 自体の変更ではなく、#85 の実装が Orchestrator 側の組み立てを差し替えるだけである。
- `apps/backend/README.md`「Multi-Repo Task の Working Set … は PAW-032 に含みません」の記述は、#85 の実装時に「この Decision が示す形で持ちます」という記述へ更新する（この PR の対象外。#85 実装 PR で行う）。
- `authz/capabilities.py` に新しい Capability（`project.task.working_set.manage`）を追加し、`tests/test_authz_policy.py` の表を更新する必要がある（#85 実装 PR で行う）。
- 新しい `RepoRole` / `ROLE_WRITE_CEILING` は `tools/scope.py` と `tools/broker.py` に、`tools/capabilities.py` の `BrokerReason` に新しい値の追加が要る（#85 実装 PR で行う）。

## 承認後の扱い

承認されたら Status を Approved に改め、`Approval` に日付と承認の様子を記録する。#85 の実装 PR はこの Decision を参照する。
承認後に方針を変える場合は、この Decision を書き換えず、新しい Decision から `Supersedes` する。
[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。[Decision 0014](0014-task-working-set-persistence.md) も書き換えない。

## 推測した点（実装者が要件から解釈した点）

次は要件に明記がなく、実装者（この Decision の提案者）が既存コードの慣習（`REPO_PERMISSION_OF`、`WaitReason.APPROVAL`、PAW-034 の `ROLE_CEILING`）に合わせて解釈した細部である。5 節の主要な決定より優先度が低く、#85 の実装時に Backend README で確定させる程度でよいと考える。

- 新しい Capability の名前 `project.task.working_set.manage` と、新しい `BrokerReason` の名前 `repository_role_insufficient`。命名規則（小文字ドット区切り、既存の列挙との整合）は #85 実装時に合わせる。
- role を下げる操作（`target → working` など）は `SCOPED_AUTO` でよいとした点（Write 範囲を狭める方向のみであり、要件の「慎重に扱う」対象は増やす方向だけと読んだ）。
- `working` への昇格と `target` への昇格を同じ `APPROVAL` レベルにそろえた点（要件はこの 2 つを区別する記述をしていない）。

## 決めてほしいこと

1. **Working Set は Task 単位とし、Restart でも維持する**（1 節）でよいか。推奨: はい。
2. **Single-Repo Task も `task_repositories` に 1 行（role=target）を持つ統一モデルにし、`task_attempts` の worktree / review / PR 列を Repo ごとの新しい表（`task_attempt_repositories`）へ移す**（2 節）でよいか。推奨: はい。
3. **Repo の追加・役割変更を新しい Capability `project.task.working_set.manage` 経由にし、`referenced` 追加は `SCOPED_AUTO`、`working` / `target` への追加・昇格は `APPROVAL` とする**（3 節）でよいか。推奨: はい。`target` への昇格を `STRONG_APPROVAL`（Approval + Step-up 認証、Decision 0015）まで上げるべきかは、実運用の様子を見てから別途判断する。
4. **role（referenced / working / target）による Write 範囲の Ceiling を、RBAC（`RepoAcl`）とは別の軸として Tool Broker に追加し、両方を満たしたときだけ許可する（AND）。Approval があっても Ceiling は超えられない**（4 節）でよいか。推奨: はい（この Decision の核）。
5. **role が解決できないときは `referenced` として扱う（fail-closed）**（4 節の 5）でよいか。推奨: はい。
6. **Task 全体の完了条件を、`target` Repo は evaluation PASS + PR 成立、`working` Repo は evaluation PASS のみ、`referenced` Repo は対象外、とする**（5 節）でよいか。推奨: はい。
