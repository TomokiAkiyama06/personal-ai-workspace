# 本番の組み立てで Orchestrator に Parallel Worktree / Integration Node を配線する方針（無効にする設定を持たない、起動時に置き場所を確かめない、git の Runner と Account、worktree の要らない Task）

- Status: Proposed
- Date: 2026-09-29
- Scope: Issue [#155](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/155)。`paw_backend/orchestrator/composition.py`（`build_task_execution`、`build_worktrees`、`TaskExecution.worktrees`）、`paw_backend/app.py`（`create_app(git_runner=...)`）、`paw_backend/integration/coordinator.py`（worktree を受け取る Repository がないときの `prepare_node` / `integrate` / `targets`）。Migration はない
- Supersedes: なし。[Decision 0036](0036-parallel-worktree-integration.md)（Parallel Worktree / Integration Node）、[Decision 0051](0051-integration-worktree-ignored-files.md)、[Decision 0047](0047-task-execution-composition-and-task-end-effects.md)（本番の組み立て）、[Decision 0029](0029-per-user-git-runner-ssh.md)（Per-user の `GitRunner`）、[Decision 0017](0017-repository-registration-policy.md)（Linux Account の対応）はどれも書き換えない。この Decision はそれらの間の配線だけを扱う

## 背景

PR #151（#125、Decision 0047 Approved）の `build_task_execution` は、Agent の Runtime が渡されたときに `Orchestrator` を組み立てるが、`worktrees=` を渡していなかった。PR #130（PAW-035、Decision 0036 / 0051 Approved）は `Orchestrator(worktrees=...)` の継ぎ目と実装（`GitWorktreeCoordinator`）を入れたが、2 つの PR は並行して作られ、#151 が先に Merge されたため、本番の組み立てへの配線がどちらにも入らなかった。

配線がないと、本番で Orchestrator が動いたとき、書き込みのある Worker Node は専用の worktree / branch を受け取らず、利用者の Checkout そのものを Scope にする（[REQUIREMENTS.md](../../REQUIREMENTS.md) の「Write可能なSub-Agentは原則それぞれ専用のworktree / branchを使用する」[FIXED] を満たさない）。DAG の後の統合と Conflict の検知も行われない。

配線そのもの（Orchestrator を組み立てるときに `GitWorktreeCoordinator` を渡す）は、Decision 0036（`Orchestrator(worktrees=...)`）と Decision 0047 の 5（Runtime が必要とするものだけを組み立てる）から決まる。**次の選択は、どの Decision も決めていない。** 実装は各点で下の推奨を採っている。

## 提案（番号は「判断が必要な点」の番号）

### 1. 本番の Orchestrator は必ず worktree を持つ（無効にする設定を持たない）

- `build_task_execution` は、`Orchestrator` を組み立てるとき（`runtimes` と `orchestrator_config` が渡されたとき）は必ず `GitWorktreeCoordinator` を作って `worktrees=` に渡す。`PAW_*` の設定で外すことはできない。
- `Orchestrator` を組み立てないとき（Runtime がない、今の本番）は、Coordinator も作らない（Decision 0047 の 5）。`TaskExecution.worktrees` は `None`。
- `Orchestrator(worktrees=None)`（PAW-034 の動き）は、Orchestrator を直接組み立てる Test のために残る。本番の組み立てはこの形を作らない。
- **推奨: この形で承認する。** 要件は Write 可能な Sub-Agent に専用の worktree を [FIXED] で求めており、外せる設定は要件に反する配備を作れるだけになる。Checkout のない配備でも Task は動く（4）ので、外す理由がない。代替: `PAW_ORCHESTRATOR_WORKTREES`（既定 on）で外せるようにする。

### 2. worktree の置き場所の設定と、起動時の確認を持たない

- worktree の置き場所は Decision 0036 の 1 のとおり Account ごとに `<home>/<workspace_subdir>/.paw-worktrees` で、`workspace_subdir` は既存の `PAW_REPOSITORY_WORKSPACE_SUBDIR`（`RepositoryPolicy.from_settings`）である。**「worktree の Root」という別の設定は作らない。**
- 本番の起動は、置き場所がなくても、git が使えなくても拒否しない（起動時に確かめない）。置き場所は User ごとに違い、Backend は他の User の Home を自分で読まない（Decision 0017 / 0029、AGENTS.md）ので、起動時に確かめる方法がない。`git worktree add` が必要になったときに作る。
- 使えないときは、その Node の試行が `WorktreeUnavailable`（理由は `account_unavailable` / `git_failed` などの固定の値）で失敗し、統合は Task を `failed`（`Integration failed (<理由>)`）にする（Decision 0036 の 8）。どちらも Checkout に書かない（Fail closed）。
- 組み立ての引数の型（`git_runner` に `run` がない、`accounts` に `account_of` がない）は、Orchestrator を組み立てるかどうかにかかわらず起動時に `TypeError` で拒否する。
- **推奨: この形で承認する。** 代替: (a) `PAW_WORKTREE_ROOT` を足して 1 か所に置く（Decision 0036 の 1 の置き場所を変えることになり、User の分離（Linux User ごとの Home）と合わない）、(b) 起動時に Backend の Process の User の置き場所だけを確かめる（`SshGitRunner` の配備では他の User の置き場所を確かめられず、一部だけの確認になる）。

### 3. git の Runner と Linux Account

- git は配備の `GitRunner` で動かす。`create_app(git_runner=...)` / `build_task_execution(git_runner=...)` で渡し、既定は `SubprocessGitRunner()`。Decision 0029 の 6 の移行（`SubprocessGitRunner` を `SshGitRunner` に差し替えるだけ）は、この引数を差し替えることで行う。
- 既定の `SubprocessGitRunner` は Backend の Process の User とは違う Account の git を `identity_mismatch` で拒否する（Decision 0017 の 4）ので、Wrapper（[#134](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/134)）が配備されるまで、他の User の Task の Worker は `WorktreeUnavailable`（`git_failed`）で失敗する（Fail closed。利用者の Checkout には書かない）。
- Account は `LoginNameAccountDirectory`（Decision 0017 の対応）、最小 uid・`workspace_subdir`・git の Timeout は `RepositoryPolicy.from_settings(settings)` の 1 つの Policy から作る（`RepositoryService.from_policy` と同じ値）。
- 本番の `StoredTaskAuthority` が使う `RepositoryService`（`build_repository_scopes`）の Runner は、今のまま既定の `SubprocessGitRunner()` にする（その読み取りは git を実行しない。Decision 0047 の 5）。
- **推奨: この形で承認する。** 代替: 設定（例: `PAW_REPOSITORY_GIT_RUNNER=subprocess|ssh`）で Runner を選ぶ（`SshGitRunner` の鍵の Path のひな型が設定にまだない（Decision 0029 の 6）ので、今は選べる値が 1 つだけになる。鍵の配備の Issue で決める）。

### 4. worktree を受け取る Repository がない Task は、配線の前と同じく動く

- `GitWorktreeCoordinator` の `prepare_node`・`integrate`・`targets` は、Scope に対象の Repository（`prepare_node` / `integrate` は Checkout があり `working` / `target` の Repository、`targets` は Checkout のある Repository）が 1 つもなければ、**Linux Account を尋ねず、git も動かさずに**空を返す（`{}`、空の `IntegrationReport`（`clean`）、`()`）。
- これまでは Account を先に尋ねていたので、Linux Account のない User の Task（本番の `StoredTaskAuthority` は、そのような User の Repository を Scope から外す。Decision 0047 の 3）は、Checkout がなくても DAG の成功後の統合が `account_unavailable` で `failed` になった。配線するとこの Task が壊れるので、配線と同時に直す。
- 対象の Repository が 1 つでもあれば、今までどおり Account を尋ね、なければ `account_unavailable`（Fail closed）。
- **推奨: この形で承認する。** Decision 0036 の `workspaces.py` の「Checkout のない配備でも動き続ける」の趣旨どおりで、worktree を作るかどうかの規則（`gets_worktree`）は変えない。代替: Coordinator を変えず、Checkout のない配備は 1 の設定で worktree を外す（1 の代替と組になる）。

### 5. `IntegrationGate` は組み立てない

- `IntegrationGate`（Decision 0036 の 9）は、Test・Evaluator・Review の検査の実体（PAW-011 / PAW-013、別 Issue）がまだなく、3 種すべてに検査が要るので組み立てられない。この Decision では組み立てず、`TaskExecution.worktrees`（`targets` を持つ Coordinator）を、検査を入れる Issue が使えるように置く。
- 統合の後、Task は `evaluating` で止まる（今の本番の Orchestrator の動きと同じ。Complete は `TaskService` が判定する）。
- **推奨: この形で承認する。**

## 選定理由

- 配線を Orchestrator の組み立てと同じ条件にしたのは、Decision 0047 の 5（Runtime が必要とするものだけを組み立てる）のとおりで、Runtime のない今の本番に何も足さないため。
- 設定と起動時の確認を足さないのは、要件がすでに worktree を求め、置き場所も Decision 0036 が決めており、足すと要件や承認済みの Decision と食い違う配備を作れる余地だけが増えるため。使えないときは Node / Task の単位で Fail closed になる。

## リスク

1. `SubprocessGitRunner` の配備では、Backend の Process の User と違う User の Task の書き込みのある Worker Node は、Wrapper が配備されるまで `git_failed` で失敗する（Retry しても同じ）。Runtime が入った後、この失敗が繰り返されると Loop の検知（Decision 0007）に掛かる。
2. 起動時に確かめないので、git がない・`workspace_subdir` が書けない配備の誤りは、最初の Task の実行まで見えない（Node の失敗と Task Log の固定の理由で見える）。
3. SSH（他の Linux User）での統合は未検証（Decision 0036 のリスク 1 と同じ）。この PR の Test は、Test を実行する User 自身の一時 Repository と `SubprocessGitRunner` だけを使う。

## 判断が必要な点

1. 本番の Orchestrator は必ず worktree を持ち、無効にする設定を持たないこと（1）。推奨: 承認。
2. worktree の Root の設定を作らず、起動時に置き場所・git を確かめない（使えなければ Node / Task の単位で Fail closed）こと（2）。推奨: 承認。
3. git の Runner は `create_app(git_runner=...)` の引数（既定 `SubprocessGitRunner()`）、Account は `LoginNameAccountDirectory`、値は `RepositoryPolicy.from_settings` の 1 つの Policy から作ること。設定で Runner を選ぶ形は鍵の配備の Issue で決めること（3）。推奨: 承認。
4. worktree を受け取る Repository がない Task では、Coordinator が Account を尋ねず git も動かさずに空を返すこと（4）。推奨: 承認。
5. `IntegrationGate` は検査の実体の Issue まで組み立てないこと（5）。推奨: 承認。

## 承認後の扱い

判断が必要な点が承認されたら、`Approval` に記録し、Status を Approved に改める。承認されない点は実装を変え、新しい Decision を作らずにこの Decision を承認前に改める（承認後に方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する）。
[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。
