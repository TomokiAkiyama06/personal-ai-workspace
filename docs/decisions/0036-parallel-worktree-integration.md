# Parallel Worktree / Integration Node の方針（Worker ごとの worktree・branch、統合の方法、Conflict の扱い、統合後の Test / Evaluator / Review、default branch の保護）

- Status: Proposed
- Date: 2026-09-28
- Scope: PAW-035（[#31](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/31)）の実装（`paw_backend/integration/`、`paw_backend/orchestrator/workspaces.py`、`Orchestrator(worktrees=...)`、`TaskScope.excluded_paths` とその切り出しの規則）。Migration はない
- Supersedes: なし（[Decision 0021](0021-dag-orchestrator-policy.md)・[Decision 0017](0017-repository-registration-policy.md)・[Decision 0029](0029-per-user-git-runner-ssh.md) は書き換えない。0029 の 3 の表への追加は下の 13 で提案し、承認されたらその部分だけを Supersede する）
- Approval: なし（Human の判断待ち）

## 背景

[REQUIREMENTS.md](../../REQUIREMENTS.md) の「Agent Orchestration / Parallel-first execution」の「Repository isolation」は次を定める（[FIXED]）。

- Write 可能な Sub-Agent は原則それぞれ専用の worktree / branch を使う。
- 同一 Repo 内で複数 Worker が並列実装した場合は、default branch へ直接統合せず、Task 専用の integration branch / worktree へ変更を集約する。
- Integration で、commit 取り込み、conflict 検出 / 解消、build / test、Evaluator、Review を行う。Project / default branch への最終 Merge は Human-controlled。
- Multi-Repo Task では Repo ごとに独立した integration state を持つ。
- Read-only の Researcher / Reviewer は、Write が発生しないことを Backend が保証できる場合、同一の checkout を共有してよい。

PAW-035 の受け入れ条件は「Write Worker ごとに専用 worktree / branch」「同一 Repo の並列変更を integration worktree へ集約」「conflict 検知」「integration 後に test / evaluator / review」「default branch へ直接統合しない」の 5 つである。
**worktree の置き場所と名前、Worker の branch の起点、統合の順序と方法、Conflict が起きたときの Task の行き先、未 commit の変更の扱い、統合後の検査の順序と完了の条件、後片付け、永続化の要否は、要件も Backlog も決めていない。** [AGENTS.md](../../AGENTS.md) の「仕様変更」に従い、実装が置いた選択をここに一覧にする。実装は各点で下の推奨を採っている。

すでに承認された Decision が課す条件は、選択ではなく守るべき条件として扱った。

- Decision 0021: Plan は 1 回だけで Node は Node を足せない（Conflict を解く Node を自動で足さない理由）。DAG が成功したら Task は `evaluating` まで、完了は Evaluator（統合は `evaluating` の前に置いた）。失敗の文字列は保存しない（git の出力も Conflict の File 名も Task Log に書かない）。
- Decision 0017 / 0029: git はその User の Linux User として、`GitRunner`（`SubprocessGitRunner` / `SshGitRunner`）だけを通して動かす。Backend は他の User の Home を自分で読まない（worktree の確認は git に尋ねる）。
- AGENTS.md: 他の User の SSH 鍵・Home・Credential を読まない（Test は現在の User の一時 Repository と `SubprocessGitRunner` だけを使う）。Merge は Human の権限。

## 提案（番号は「判断が必要な点」の番号）

### 1. 置き場所と名前

```text
<home>/<workspace_subdir>/.paw-worktrees/<task id>/<attempt>/<repository id>/<node key>
branch: paw/<task id>/<attempt>/<node key>

<home>/<workspace_subdir>/.paw-worktrees/<task id>/<attempt>/<repository id>/_integration
branch: paw/<task id>/<attempt>/_integration
```

- 名前は Backend が持つ ID（Task、試行、Repository、Node の key）だけから導く。Model も User も名前を選ばない。同じ試行の同じ Node は同じ worktree を受け取る（Node の Retry は途中の成果を引き継ぐ）。
- 試行（`TaskRun.attempt`）ごとに分ける。Retry は同じ試行なので同じ worktree、Restart は新しい試行なので新しい worktree（古いものは残る）。
- `_integration` は Node の key になれない（key は英小文字で始まる）ので、Node と衝突しない。
- worktree は利用者の Checkout の**外**（`workspaces` 直下の隠し Directory）に置く。Checkout と worktree が入れ子になる配置は拒否する（`overlaps_checkout`）。
- **推奨: この形で承認する。** 代替: Checkout の `.git/` の下に置く（利用者の Checkout の中の Path になり、Scope で Checkout と区別できない）。

### 2. worktree を与える Node

- Role が `worker` で、Grant に `project.repo.write` があり、Scope に Checkout（`ScopedRepository.root`）を持つ Repository があるときだけ、その Repository ごとに 1 つ。Planner / Researcher / Reviewer と、Plan が読み取りだけを求めた Worker は、既存の Checkout を読む（Grant に書き込みの Capability がないことで Write しないことを保証する。Decision 0021 の 2）。
- Node の Scope は worktree を指す（`derive_child_scope(worktrees=...)`）。Repository の `root` を worktree にし、worktree を先頭の `path_roots` にし（相対 Path は worktree の中に解決される）、**利用者の Checkout、Task の integration worktree、Account の worktree の置き場所全体（`<home>/<workspace_subdir>/.paw-worktrees`）、自分の worktree の `.git` を `TaskScope.excluded_paths`（新しい Field）にする**。自分の worktree は、置き場所の中にある Path Root として除外から切り出す（**切り出しの規則**: `excluded_paths` の中に厳密に含まれる Path Root の下の Path は、その除外に当たらない。除外された Repository には適用しない）。Worker は利用者の Checkout にも、統合先にも、同じ DAG の兄弟の Node や他の Task の worktree にも、自分の `.git` にも書けない（`.git` を書き換えられると、Backend がそこで動かす git が Agent の設定（Filter Driver などの Command）を読む）。`scope_within` はこの広げ方だけを受け入れ、親が除外した Path の中に Backend が用意した worktree 以外の Root を足すことを拒む。
- 除外の数の上限は `MAX_EXCLUDED_PATHS`（親の 32 + Repository ごとに 4 × 32）。
- **読み取り専用の Node は上流の Worker の変更を見ない。** 読み取り専用の Node は利用者の Checkout を読むので、DAG で Worker の後に置いた Reviewer / Researcher（Decision 0021 の DAG の中の Review や Test の Node）は、上流の `NodeResult` は受け取るが、その Worker の変更のない Tree を読む。統合された結果の Review は、DAG の後の `IntegrationGate`（9）が integration worktree で行う。
- **worktree を受け取らない Worker**（Checkout のない Repository だけを扱う Node、`project.repo.write` のない Worker）の Scope は、親の Scope のまま（worktree の置き場所を除外しない）。
- **推奨: この形で承認する。** Issue #85（PR #124）が Merge された後は、Working Set の Role が `working` / `target` の Repository だけに worktree を与える（`referenced` には与えない）変更を、統合時に行う。読み取り専用の Node の制限（判断点 15）と、worktree を受け取らない Worker の Scope（判断点 16）は下に別の判断点として挙げる。

### 3. Worker の branch の起点

- Worker の branch は integration branch（まだ統合前なので base と同じ）から作り、**直接依存している Worker Node の branch を Node の順に merge する**（依存先の成果の上で作業できるように）。すでに含まれている branch は merge しない（冪等）。
- この merge が Conflict したら、その Node は再試行なしで失敗する（`WorktreeConflict`。同じ merge は同じ Conflict になる）。
- **推奨: この形で承認する。** 代替: 依存先の branch を 1 つだけ起点にする（依存先が 2 つ以上のときに決められない）、依存先の成果を取り込まない（依存の意味がない）。

### 4. Integration の base

- 試行で最初に integration branch を作るとき、Checkout の default branch（`origin/HEAD` の指す branch、なければ Checkout で checkout されている branch）の、local の branch の先端（なければ `origin/<default>`）を base にし、以後その試行では動かさない。
- **fetch しない。** Remote の最新は取り込まない（Credential を使う操作を増やさない。PAW-028 の範囲）。
- **推奨: この形で承認する。** 代替: `tasks.starting_commit`（Task に 1 つだけで Multi-Repo に合わない）や、PR #124 の Repository ごとの `starting_commit` を base にする（#124 の Merge 後に検討する）。

### 5. Integration の時期と方法

- DAG が成功した直後、Task を `evaluating` にする前に 1 回行う（`Orchestrator._integrate`）。Evaluator と Review は統合された結果を見る。
- 成功した Worker Node の branch を、Node の順（`ordinal`）に、integration worktree の中で `git merge --no-ff` する（Worker ごとに Merge Commit が 1 つ残る）。merge の前に `git merge-tree --write-tree` で Conflict を判定し、Conflict する merge は始めない（worktree が途中の状態で残らない）。
- Merge Commit の作者は固定の `Personal AI Workspace <integration@paw.invalid>`、署名しない（`commit.gpgSign=false`。Repository の設定で鍵が要る状態にされても統合が止まらない）。
- 冪等: integration branch に含まれている branch は merge しない。
- **integration worktree に進行中の merge（`MERGE_HEAD`）があれば中断しない**。Backend 自身の merge は `merge-tree` で先に判定し、失敗したらその場で `merge --abort` するので、残っている進行中の merge は Human の（6 の解消の途中）ものとみなし、その Repository を `dirty`（7）として止める（Crash で Backend の merge が残った場合も、Human が `merge --abort` するまで止まる。黙って捨てるより安全）。
- `merge-tree` が Conflict なしと判定した merge を git が拒んだら（`index.lock` の競合、Timeout、設定の誤りなど）、**Conflict ではなく git の失敗**（8。`failed` で Retry できる）にする。Worker の branch に依存先を取り込む merge（3）も同じで、Conflict の判定は `merge-tree` だけが行う。
- **worktree の中の `.git` は信用しない。** worktree の中で動かす git の Command（`status`、`merge`、`symbolic-ref`、`rev-parse`）は、Checkout の `.git/worktrees/<name>` を `--git-dir=` / `--work-tree=` で明示して動かす。その Directory は、worktree の `.git` が指すもの（`rev-parse --git-dir`。何も実行しない）を手掛かりに求め、Checkout の共通の git Directory（`rev-parse --git-common-dir`）の `worktrees/` の直下であること、その Directory を通して期待する branch が checkout されていること（1 つの branch は 1 つの worktree にしか checkout できない）を確かめたときだけ使う。違えば `not_the_worktree`。`merge-tree` と `merge-base` は Checkout で動かす。
- Repository ごとに独立した状態（`nothing` / `merged` / `conflict` / `dirty`）を持つ（要件の Multi-Repo の独立した integration state）。
- 同じ Process の中で並列に走る Node が同じ integration branch を同時に作らないよう、(Task, 試行, Repository) ごとに Lock する。別の Worker Process の間は、Queue の Lease と DAG の epoch が 1 つの DAG を 1 人にする（git の操作そのものは Fence しない。リスク 2）。
- **推奨: この形で承認する。** 代替: Rebase で直線にする（Worker の Commit を書き換える）、Squash する（Worker ごとの履歴が消える）、Octopus Merge（どの Worker が Conflict したか分からない）。

### 6. Conflict の扱い

- その Repository は**最初に Conflict した branch で止まり**、以降の branch は merge しない。Conflict した Node の key と、git が示した File（最大 100）を `RepositoryIntegration` で返す。Task Log には Repository ID・Node の key・File の件数だけを固定の形式で書く（File 名は書かない）。
- Task は `waiting`（理由 `user`）になる。Human が integration worktree で Conflict を解消して Commit し、Task を Unblock して Queue に戻すと、統合は解消済みの branch を含まれているものとして通り過ぎ、次の branch へ進む（Node は再実行しない）。
- **推奨: この形で承認する。** 代替: (a) Task を `failed` にする（Retry でも同じ Conflict になる。Human が解消する経路を明示するため `waiting`）、(b) Conflict を解く Agent の Node を自動で足す（Decision 0021 の「Plan は 1 回、Node は Node を足せない」に反する。新しい Decision が要る）、(c) Conflict する branch を飛ばして残りを merge する（どこまで統合されたかが分かりにくい）。

### 7. 未 Commit の変更

- Worker の worktree（または integration worktree）に未 Commit の変更（未追跡の File を含む）があれば、その Repository は merge の前に止まり（`dirty`）、Task は `waiting`（理由 `user`）になる。**自動で Commit も破棄もしない。**
- **推奨: この形で承認する。** Worker は成果を Commit する（`NodeResult.commit`）ことを Runtime の契約にする。代替: Node の成功時に Backend が自動で Commit する（誰の変更かが曖昧になり、意図しない File を含み得る）。

### 8. git が使えないときの Task

- 統合で git が失敗したら（Timeout、終了コード、Linux Account がない等）、Task は `failed` になる（`Integration failed (<固定の理由>)` を Log に書く）。Retry は同じ DAG の結果のまま統合をやり直す（Decision 0021 の 6 の「成功した DAG の Retry」と同じ経路）。
- Retry / Unblock で成功済みの DAG を引き継いだ Worker が統合するときも、Heartbeat で Lease を保ち、Lease を失ったらその Run は Task に何も命令しない。
- **推奨: この形で承認する。**

### 9. 統合後の Test / Evaluator / Review

- `IntegrationGate`（`paw_backend/integration/gate.py`）が、`evaluating` の Task の integration worktree（Repository ごとの Path・branch・Commit）を検査に渡す。順序は固定で **Test → Evaluator → Review**、最初に通らなかった検査で止まる。
- 3 種類すべてに少なくとも 1 つの検査が要る（1 つでも欠けた Gate は作れない）。
- 全部通れば Task を `completed`（**Merge Ready**。Human の Merge の判断を待つ。何も Merge / Push しない）、どれかが通らなければ `failed`。検査は integration worktree の Directory を読むので、**検査の前に integration worktree に未 Commit・未追跡の変更や進行中の merge があれば検査せずに `failed`**（`dirty`）。検査の間に integration branch が動いたら（誰かが Commit した）、または worktree が書き換わったら、検査していない Commit を完了にしないため `failed`（`changed`）。完了するときは、Repository ごとに検査した Commit を Task Log に書く（Merge Ready なのはその Commit。Multi-Repo でも残る）。Task の Command は、読んだ Run と Version に Fence する。Cancel / Retry が間に入ったら次の種類の検査を始めない。
- 試行の `ReviewState` に記録する: Test が失敗したとき、または Evaluator の後に `evaluation_result`、Review の前後に `review_status`（`in_review` → `approved` / `changes_requested`）。検査が返した文章は保存しない。
- 実際の検査（PAW-011 / PAW-013 の Evaluator、Codex / Claude の Reviewer）は別の Issue で、この PR は Protocol（`async check(request) -> CheckVerdict`）だけを定める。Reviewer が実装した Agent と別であること（Review の独立性）は構成の責任で、Gate は強制しない。
- **推奨: この形で承認する。**

### 10. default branch へ直接統合しない保証

- Backend が書く branch はすべて `paw/` の下にある。default branch が `paw/` の下にある Repository は拒否する（`default_branch_in_namespace`）。
- この機能の git の操作に `push` / `fetch` / `pull` / `checkout` / `switch` / `reset` / `rebase` はない。branch を動かすのは、新しい `paw/` branch を作る `worktree add -b` と、`paw/` branch の worktree の中での `merge` だけ（merge の前に worktree の branch を git に確かめる）。利用者の Checkout の HEAD・作業ツリー・default branch は変わらない（Test が毎回確かめる）。
- **PR の作成と integration branch の Push はこの PR では行わない**（Decision 0021 は `project.pr.create` を統合の PAW-035 に送ったが、Push には Remote の Credential が要り、PAW-028 の `gh` と SSH 配備の検証がこの環境でできない）。別の Issue にする。
- **推奨: この形で承認し、PR の作成・Push を別 Issue にする。**

### 11. 永続化

- 新しい Table も Migration も作らない。名前が決定的なので、git（branch と worktree）が状態の正本で、統合はいつでもやり直せる（冪等）。
- Repository が 1 つの Task は、試行の worktree の状態（`TaskService.update_attempt`）に integration branch・Path・Commit を記録する。Multi-Repo の Task は、今の試行の行が 1 つの worktree しか持てないため Task Log だけに書く。**PR #124（Issue #85）が Merge された後は、Repository ごとの試行の状態（`update_attempt(repository_id=...)`）に全 Repository を記録する変更を、統合時に行う。**
- **推奨: この形で承認する。** 代替: `task_worktrees` / `repository_integrations` の Table を作る（UI（PAW-062）が一覧を要するときに、#124 の Repository ごとの状態と合わせて検討する）。

### 12. 後片付け

- worktree と branch は Task の終了後も残す（要件の「Taskを止める操作と、成果物・branch・worktreeを削除する操作を分離する」）。削除は別の操作として後の Issue で作る。
- 手で消された worktree は、branch が残っていれば同じ branch で作り直す（その前に `git worktree prune` で消えた worktree の登録を片付ける。利用者自身の、Directory が消えた worktree の登録も消える）。
- **推奨: この形で承認する。**

### 13. SSH の Wrapper が許可する副コマンドの追加（Decision 0029 の 3 の表）

`SshGitRunner` で他の Linux User として動かすには、Wrapper の許可リストに次を足す必要がある（今の Wrapper は未配備で、この PR は Wrapper を実装しない）。

| 副コマンド | 引数の形 |
| --- | --- |
| `worktree` | `add --quiet [-b paw/...] -- <.paw-worktrees 配下の Path> <commit id / paw/... branch>`、`list --porcelain -z`、`prune` |
| `merge` | `--no-ff --no-edit --quiet -m "Integrate paw/..." refs/heads/paw/...`（先頭に `-c user.name=... -c user.email=... -c commit.gpgSign=false -c merge.verifySignatures=false`）、`--abort` |
| `merge-tree` | `--write-tree --name-only -z --no-messages refs/heads/paw/... refs/heads/paw/...` |
| `merge-base` | `--is-ancestor refs/heads/paw/... refs/heads/paw/...` |
| `status` | `--porcelain=v1 -z --untracked-files=all` |
| `rev-parse`（追加の形） | `--verify --quiet <ref>^{commit}`、`--show-toplevel`、`--path-format=absolute --git-dir`、`--path-format=absolute --git-common-dir` |
| `symbolic-ref`（追加の形） | `--quiet HEAD` |

worktree の中で動かす `status`・`merge`・`symbolic-ref`・`rev-parse --verify` は、副コマンドの前に `--git-dir=<Checkout の .git>/worktrees/<name>` と `--work-tree=<.paw-worktrees 配下の Path>` の 2 つの大域 Option を付ける（Wrapper はこの 2 つを、その User の `workspaces` の下の Path に限って許す）。Wrapper の cwd の確認は、その User の `workspaces` の下（`.paw-worktrees` を含む）を許す。`merge` と `worktree add -b` の対象は `paw/` の branch だけにする（Wrapper 自身の防衛線）。git の Log 名（`GitCommandError` の名前）は、先頭の `-c` と `--git-dir=` / `--work-tree=` を読み飛ばした最初の語にした（`repositories.git.command_name`。Wrapper の「最初の非 `-c` 語」と同じ規則）。

- **推奨: この表を承認し、承認されたら Decision 0029 の 3 の表のこの部分を Supersede する。**

### 14. 必要な git の版

- `merge-tree --write-tree` は git 2.38 以降。Server の git がそれより古いと統合は `git_failed` で止まる（fail closed）。
- **推奨: git 2.38 以降を Server の要件にする。**

## 選定理由

- 名前を ID から導くのは、DB に何も足さずに冪等にでき（Worker が死んでも、次の Lease 保持者が同じ worktree を見つける）、Model が Path を選ぶ余地をなくせるため。
- `--no-ff` と `merge-tree` の事前判定は、Worker ごとの成果を履歴で追え、Conflict のときに worktree を途中の状態で残さないため。
- Conflict と未 Commit を `waiting` にするのは、どちらも「人が決めること」で、Retry では解決しないため。git の失敗を `failed` にするのは、既存の Retry の経路で直せるため。
- 検査の順序を固定し 3 種を必須にするのは、要件の Flow（Integration → Executable Evaluator → Reviewer → Merge Ready）をそのまま構造にし、構成の誤りで検査が抜けないようにするため。

## リスク

1. **SSH（他の Linux User）での動作は未検証。** Test は現在の User の一時 Repository と `SubprocessGitRunner` だけで行った（他の User の SSH 鍵・Home を読まないため）。Wrapper（13）が配備されるまで、`SshGitRunner` 経由の統合は Wrapper に拒否される。
2. git の操作は Fence されない。Lease を失った Worker の git が走っている間に次の Worker が同じ worktree を操作すると、git の Lock（`index.lock`）で片方が失敗し得る（その Run は `git_failed` で Task を `failed` にし得るが、Lease を失った Run は Task に命令しないので、失敗するのは新しい Run の側）。
3. Home に Symbolic Link を含む Account では、git が返す `--show-toplevel`（解決済み）と Backend が作る Path が一致せず、worktree が `not_the_worktree` で拒否される（fail closed）。
4. Worker が Commit しないと統合できない（7）。Runtime（別 Issue）が Commit する契約を守る必要がある。
5. Multi-Repo の Task の統合の状態は、#124 の Merge までは Task Log にしか残らない（11）。
6. `worktree prune` は利用者自身の、Directory が消えた worktree の登録も片付ける（12）。
7. Worker の Tool は Path の Scope で worktree の置き場所と `.git` から締め出されるが、Worker が Shell などの任意の実行を持てば Scope の外も書ける（今の Worker の Role に実行の Capability はない）。その場合は Sandbox（別 Issue）が要る。
8. 読み取り専用の Node が worktree の置き場所（他の Task の worktree を含む、同じ User の File）を読むことは妨げない（書けない）。

## 判断が必要な点

1. 置き場所と名前（1）。推奨: 承認。
2. worktree を与える Node と Node の Scope（2。`TaskScope.excluded_paths` の追加と切り出しの規則、worktree の置き場所全体と `.git` の除外を含む）。推奨: 承認。#124 の後は `working` / `target` に限る。
3. Worker の branch の起点と、依存先との Conflict で Node を再試行なしに失敗させること（3）。推奨: 承認。
4. Integration の base（4。fetch しない）。推奨: 承認。
5. Integration の時期・順序・`--no-ff`・固定の作者・署名しない、進行中の merge を中断しないこと、Conflict でない merge の失敗を git の失敗にすること、worktree の git を `--git-dir` で固定すること（5）。推奨: 承認。
6. Conflict で Repository を止め、Task を `waiting`（`user`）にし、Human の解消後に再統合すること（6）。推奨: 承認。
7. 未 Commit の変更で止め、自動で Commit しないこと（7）。推奨: 承認。
8. git の失敗で Task を `failed` にすること（8）。推奨: 承認。
9. 統合後の Test → Evaluator → Review の Gate と、全部通ったら `completed`（Merge Ready）にすること、未 Commit の integration worktree を検査しないこと（9）。推奨: 承認。
10. PR の作成と Push を別 Issue にすること（10）。推奨: 承認。
11. 新しい Table を作らないこと（11）。推奨: 承認。#124 の後に Repository ごとに記録する。
12. worktree / branch を残し、削除を別の操作にすること（12）。推奨: 承認。
13. Wrapper の許可リストへの追加（13。Decision 0029 の 3 の部分的な Supersede）。推奨: 承認。
14. git 2.38 以降を要件にすること（14）。推奨: 承認。
15. DAG の中で Worker の後に置いた読み取り専用の Node（Reviewer / Researcher）が上流の Worker の変更を見ないこと（2）。推奨: この PR では制限として明記して承認し、統合された結果の Review は `IntegrationGate`（9）が行う。上流の branch を読み取り専用で見せる（読み取り専用の worktree を作る）ことは、DAG の中の Review が必要になったときに別の Issue で行う。代替: 読み取り専用の Node にも、上流の Worker の branch を merge した読み取り専用の worktree を与える（Write しないことを Backend が保証する必要があり、Scope の変更が大きい）。
16. worktree を受け取らない Worker の Scope に worktree の置き場所を含めたままにすること（2）。推奨: 承認（その Node は `project.repo.write` がないか、Checkout のある Repository を持たない）。代替: `NodeWorkspaces` が置き場所を返し、Orchestrator がすべての Node の Scope から除外する。

## 承認後の扱い

承認されたら `Status` と `Approval` を改める（Human が行う。この Decision を Agent が Approved にしない）。方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。
