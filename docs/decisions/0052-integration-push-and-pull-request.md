# Integration Gate を通った integration branch の Push と PR の作成（Decision 0036 の 10 の後続）

- Status: Proposed
- Date: 2026-09-29
- Scope: Issue [#132](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/132)（`paw_backend/integration/publish.py` の `GitHubPullRequestPublisher`、`IntegrationGate(publisher=...)`、SSH の Wrapper の許可リストへの `push` の追加）。Migration はない
- Supersedes: [Decision 0029](0029-per-user-git-runner-ssh.md) の 3 の表（と、それを Supersede した [Decision 0036](0036-parallel-worktree-integration.md) の 13・[Decision 0051](0051-integration-worktree-ignored-files.md) の 3）に `push` の 1 つの形を足す部分だけ（下の 9）。0029・0036・0051 の本文は書き換えない

## 背景

[Decision 0036](0036-parallel-worktree-integration.md)（Approved）の 10 は、PR の作成と integration branch の Push を PAW-035 に含めず、別の Issue（#132）にした。[Decision 0030](0030-task-working-set-model.md)（Approved）の 5 節は、Working Set の Role が `target` の Repository に「届いた PR」（`open` か `merged`）がなければ Task を完了にしない。そのため PAW-035 の `IntegrationGate` は、Test → Evaluator → Review がすべて通っても、PR が記録されるまで Task を `evaluating` に残す（`REQUIREMENTS_NOT_MET`）。この Issue は、その PR を作って記録する経路である。

要件と承認済みの Decision が決めていること（選択ではなく守る条件として扱った）:

- [REQUIREMENTS.md](../../REQUIREMENTS.md)「Tool approval boundary」: 「AI専用branchへの通常push」と「Task目的に含まれる通常PR作成」は `SCOPED_AUTO`、「protected branchへの直接push」「force push」「Merge」は `STRONG_APPROVAL`。「Git boundary」: AI 専用 branch では `edit → test → commit → push → PR create` を自動化でき、最終 Merge は Human。
- REQUIREMENTS.md「Git / GitHub」（FIXED）: 各 User が自分の Linux User で `gh auth login` し、Token と SSH 秘密鍵は誰も閲覧できない。GitHub App は使わない。
- Decision 0030: `target` は「PR作成まで行う対象」。`project.pr.create` は `target` でだけ許す（Role の上限）。
- Decision 0029（Approved）: git は `GitRunner` を通して、その User の Linux User として動かす。他の Linux User へは SSH の Wrapper を通し、Wrapper は許可リストにない副コマンドを拒否する。**この Decision は git 以外の実行（`gh`）を承認していない。**
- Decision 0036 の 9: Merge Ready なのは検査した Commit。

**要件と承認済みの Decision が決めていないこと**: Push と PR の時期、どの Repository に作るか、Push の宛先と形、Credential の経路、必要な Capability と承認と Audit、PR の本文、既存の PR の扱い、作れなかったときの Task の行き先。[AGENTS.md](../../AGENTS.md) の「仕様変更」に従い、実装が置いた選択を一覧にする。実装は各点で下の推奨を採っている。

## 提案（番号は「判断が必要な点」の番号）

### 1. 時期と、Push する Commit

- `IntegrationGate` が Test → Evaluator → Review をすべて通し、検査の後に integration branch と worktree が変わっていないこと（`CHANGED` の確認）を確かめた**後**、Task を完了にする**前**に、Repository ごとに Push と PR の作成を行う。どれかの検査が通らなかった・`DIRTY`・`CHANGED` のときは何も Push しない。
- Push するのは **検査した Commit そのもの**（`<commit id>:refs/heads/paw/<task>/<attempt>/_integration`）で、branch の名前ではない。検査の後に integration branch に Commit が足されても、その Commit は Push されない。
- **推奨: この形で承認する。** 代替: Review の前に Push する（Review が通らなかった変更が GitHub に残る）、Draft の PR を先に作って Review 後に Ready にする（GitHub の状態が 2 段階になり、途中で止まった Task の PR が残る）。

### 2. PR を作る Repository

- 検査した Repository のうち、**Repository ごとに Push の直前に読み直した Task の Scope で Working Set の Role が `target`** のものだけ（検査の前に読んだ Scope は使わない。検査の間に Project が Archived になった、ACL が狭められた、Remote や Role が変わった場合は、今の状態で判定する）。`working` の Repository は Push も PR もしない（Decision 0030 の 5 節は `working` に PR を求めない）。Scope から外された Repository（Checkout がない、Project が削除された）にも作らない。
- `target` から降格された Repository は、Decision 0030 の義務（PR）を負い続けるが、Role の上限で `project.pr.create` がないので作らない。Task は `evaluating` のまま（Human が Role を戻すか、変更を破棄して降格を確かめる）。
- **推奨: この形で承認する。**

### 3. Push の宛先と形

- 宛先は、Backend がその Repository に登録した Remote（`ScopedRepository.remotes`。許可された Host（`RepositoryPolicy.clone_hosts`）の `https://<host>/<owner>/<repo>`）の URL を明示して渡す。Checkout の設定の `origin` などの Remote 名は使わない。
- branch は integration branch と同じ名前 `paw/<task>/<attempt>/_integration`。`paw/` の外の branch には Push しない（Default branch に直接 Push しない）。
- **Force Push しない。** GitHub 側の同じ名前の branch が別の Commit を指していれば（Fast-forward でない）、Push は拒否され、PR は作らない（4 の Credential の失敗と同じく、Task は `evaluating` のまま）。
- 形は 1 つに固定する（`-c credential.helper= -c credential.helper=!<gh> auth git-credential push --quiet -- <URL> <commit>:refs/heads/paw/...`）。
- **推奨: この形で承認する。** 代替: Checkout の `origin` に Push する（Checkout の設定に従うので、登録した Repository と違う宛先になり得る）。

### 4. Credential（PAW-028）

- git の Push は、Task を作った User の Linux User として、配備の `GitRunner`（別の Linux User には `SshGitRunner` と Wrapper）で動かし、Credential は **その User 自身の `gh auth login`**（gh の Credential Helper。PAW-028 で Clone が使うものと同じ文字列）から得る。空の `credential.helper=` を先に付け、Repository の設定の Credential Helper を使わない。Backend は Token を見ない。
- PR は `gh api`（GitHub の REST API。`repos/<owner>/<repo>/pulls`）で、同じ User の Linux User として `GhRunner` で作る。
- **制限（判断点）: `gh` を他の Linux User として動かす経路はない。** Decision 0029 は git 以外を承認しておらず、`SubprocessGhRunner` は Backend の Process と同じ Linux User のときだけ動く（違えば `IDENTITY_MISMATCH` で拒否。fail closed）。したがって、Backend と別の Linux User の Task では、Push は Wrapper で通っても PR は作れず、Task は `evaluating` に残る（`not_published`、理由 `github_failed`）。
- **推奨: この形で承認し、`gh`（`gh api` の固定の形だけ）を他の Linux User として動かす経路は、Wrapper と同じ考え方の別の Issue（新しい Decision）にする。** 代替: (a) Wrapper に `gh` を許す（Decision 0029 の範囲を越え、`gh api` の引数の検査の設計が要る）、(b) `git credential fill` で Token を Backend が受け取り GitHub API を直接呼ぶ（Backend が Token を持つことになり、要件に反する）。

### 5. Capability、承認、Audit

- **Capability**: Repository ごとに `project.pr.create`（Decision 0004 で `write` の Repository 権限に写る、委任可）を、**Task を作った User の Agent の行為**として Authorizer で判定する（`authorize_agent_action`。その User の今の権限と、この Capability だけを持つ Grant の積。Grant の Agent ID は Task と Run から導く固定の値）。Repository の ACL（Override が `agent` を許さなければ拒否）、Project の状態（Archived なら拒否）も今のものを使う。`github.use` は別に判定しない（`project.pr.create` が「この Project の Repository に PR を作る」ことそのものを表し、Push は PR の一部として同じ判定に含める）。
- **承認**: 要件の「AI専用branchへの通常push」「Task目的に含まれる通常PR作成」（`SCOPED_AUTO`）に当たるので、Human の承認は求めない。`target` の Role が「Task目的に含まれる」ことを表す（Working Set を `target` にする操作は、Decision 0030 で `STRONG_APPROVAL`）。
- **Audit**: 判定は `REQUIRED`（Audit の書き込みに失敗したら拒否になり、何も Push しない）。`audit_events` の行は、Actor が Task の作成者、Agent が上の Grant の ID、Action が `project.pr.create`、Resource が Repository。結果は Task Log に固定の形で書く（`Pull request #<number> of repository <id> (<state>) at <commit>: <url>`、または `... not made (<理由>)`）、PR は試行の Repository ごとの状態（`task_attempt_repositories` の `pr_number` / `pr_url` / `pr_state`。`update_attempt(pull_request=...)`）に記録する。git と gh の出力は保存も Log もしない。
- **推奨: この形で承認する。** 代替: `github.use` も判定する（Audit の行が 2 つになる）、System の Identity で判定する（誰の権限で PR を作ったかが Audit に残らない）。

### 6. PR の本文

- Title: `[PAW] <Task の title>`（空白と改行を 1 つの空白にし、GitHub の上限の 256 文字で切る）。
- Body: Task の ID と試行、検査した Commit、検査の種類ごとの結果（`test` / `evaluator` / `review` が `passed` と、その種類の検査の数）、「Merge は Human が判断する」の 1 文。**検査が返した文章（`CheckVerdict.summary`）と Task の入力（`input`）は入れない**（Gate は検査の文章を保存しない。外へ出すのはもっと広く見えることになる）。
- Base は Checkout の default branch（Decision 0036 の 4 と同じ規則: `origin/HEAD` の branch、なければ Checkout の branch。`paw/` の下なら拒否）。
- **推奨: この形で承認する。** 代替: 検査の文章を本文に入れる（Reviewer の指摘が PR で見える代わりに、検査が読んだものが GitHub に出る）。

### 7. 既存の PR の扱い（冪等）

- 作る前に、その branch の PR を GitHub に尋ねる（`state=all`）。**Base が default branch の PR だけ**を対象にし（別の branch に向けた PR は、検査した変更を default branch に提案していない）、あればそれを記録し、新しく作らない。作った PR の Base が default branch でなければ `invalid_response`。複数あれば `open` → `merged` → `draft` → `closed` の順に選ぶ。
- `merged` の PR は、その Head が**検査した Commit** のときだけ使う。Merge された後に integration branch が進んだ（Human が Conflict を解消して Gate をもう一度動かした等）場合、古い `merged` の PR に今の Commit は入っていないので、新しい PR を作る（PR #159 の Codex review）。
- `open` / `draft` の PR の Head が検査した Commit でなければ（Push の後に別の書き手が GitHub の `paw/` の branch を進めた）、その PR は検査していない変更を提案しているので記録せず、`branch_moved` で止まる（Task は `evaluating` のまま。8）。PR を作った直後の応答の Head も同じく確かめる（PR #159 の Codex review）。
- `draft` はそのまま `draft` として記録し（届いていないので Task は `evaluating`）、Ready にはしない。**`closed`（Merge されずに閉じられた）の PR があれば、新しい PR を作らない**（Human が閉じた判断を上書きしない）。`closed` を記録し、Task は `evaluating` のまま（`requirements_not_met`）。Human は Task を Cancel するか、GitHub で PR を開き直してから Gate をもう一度動かす。
- 作る要求が拒まれたら（同時に作られた場合を含む）、もう一度尋ね、あればそれを使う。
- 新しい試行（Restart）は branch の名前が変わるので、新しい PR になる。
- **推奨: この形で承認する。** 代替: `closed` のときに新しい PR を作る（Human が閉じた PR がまた開かれる）。

### 8. 作れなかったときの Task

- Push や PR の作成が失敗したら（`not_authorized`、`no_github_remote`、`account_unavailable`、`base_unknown`、`push_failed`、`github_failed`、`branch_moved`、`invalid_response`）、Task は **`evaluating` のまま**（`GateOutcome.NOT_PUBLISHED`）。検査の結果は記録済みで、Gate をもう一度動かすと、検査をやり直してから Push と PR をやり直す（冪等）。`failed` にはしない。
- Multi-Repo では、1 つが失敗しても他の `target` の PR は作って記録する（どれも冪等）。
- **推奨: この形で承認する。** 代替: `failed` にする（Retry は DAG の結果から統合をやり直すが、原因の多くは認証や権限で、Retry では直らない）、`waiting`（`user`）にする（Human の操作が要る原因と要らない原因（GitHub の一時的な失敗）が混ざる）。

### 9. SSH の Wrapper の許可リストへの `push` の追加（Decision 0029 の 3 の表）

`SshGitRunner` で他の Linux User として Push するには、Wrapper（[#134](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/134)、PR #150）の許可リストに次の 1 つの形を足す必要がある。

| 副コマンド | 引数の形 |
| --- | --- |
| `push` | `--quiet -- https://<host>/<owner>/<repo>.git <commit id>:refs/heads/paw/...`（先頭に `-c credential.helper=` と `-c credential.helper=!<Wrapper の --gh> auth git-credential` の 2 つ） |

- **宛先の Host は、Wrapper の `command=` 行に Admin が書いた `--push-host=`（例 `github.com`）だけ**。無ければ `push` はすべて拒否する。Wrapper は Client（Backend）を信用しないので、宛先を Client に任せると、乗っ取られた Backend が Root の中のどの Repository の Commit でも好きな Server へ送れてしまう（PR #159 の Codex review）。Host の中のどの Repository に Push できるかは、その User の `gh auth login` の権限が決める（書き込める Repository だけ）。代替: Repository の単位の許可リストを Admin が持つ（Repository を登録するたびに Wrapper の設定を変える運用が要る）。
- URL は `https://<host>/<owner>/<repo>.git` の形だけ（User 情報・Port・余分な Path・`..` は拒否）。`<commit id>` は 40 桁または 64 桁の 16 進、宛先は `refs/heads/paw/` の branch だけ。`+`（Force）、`--force`、`--mirror`、`--all`、`--tags`、`--delete`、他の Option、2 つ以上の Refspec は拒否する。
- 2 つの `-c` は `push` の前だけ受け入れ（`clone` の Credential Helper と同じく、Wrapper の `--gh` が設定されているときだけ）、受け入れた値は使わず Wrapper 自身が同じ 2 つを付ける。
- Push は Repository の File の内容を読まない（Filter Driver を起動しない）が、Repository の設定の `url.<base>.pushInsteadOf` などは読む（下の確認で拒否する）。
- Wrapper は、`push` の前に、その呼び出しが読む設定を一覧にし（`status` などと同じ確認）、Command を名指しする設定（`core.askPass` など。Credential が得られなければ git が動かしうる）と、宛先を書き換える `url.<base>.insteadOf` / `pushInsteadOf` があれば拒否する。
- `push` は pin（`--git-dir=` / `--work-tree=`）を受け付けず、worktree の中の cwd では動かない（Checkout で動かす）。
- **推奨: この形を承認し、承認されたら Decision 0029 の 3 の表のこの部分を Supersede する。** Wrapper（`apps/backend/deploy/ssh-git-wrapper/paw_git_wrapper.py`、PR #150 でマージ済み）への実装と Test は、この Issue の PR に含めた。

### 10. 永続化

- 新しい Table も Migration も作らない。PR は既存の `task_attempt_repositories` の `pr_number` / `pr_url` / `pr_state`（#85）に記録する。PR の状態のその後の変化（Merge された、閉じられた）を GitHub から読み直す仕組み（Webhook や定期の確認）は作らない（GitHub App を使わない V1 の範囲。別の Issue）。
- **推奨: この形で承認する。**

## 選定理由

- 検査した Commit を Push するのは、Decision 0036 の 9 の「Merge Ready なのは検査した Commit」を GitHub 上でも保つため。
- Capability を Task の作成者の Agent の行為として判定するのは、PR が作成者の GitHub の Identity で作られるのに合わせ、作成者の今の権限（外された、Viewer に降格された、Project が Archived）を Push のたびに反映し、誰のために作ったかを Audit に残すため。
- 作れなかったときに `evaluating` に残すのは、PAW-035 の Gate の扱い（`REQUIREMENTS_NOT_MET` で `evaluating` のまま）と同じにし、Retry では直らない原因で Task を `failed` にしないため。

## リスク

1. **他の Linux User の Task では PR を作れない**（4）。Push は Wrapper（9）で通っても、`gh` の Identity を切り替える経路がないため `github_failed` になる。
2. **SSH（他の Linux User）と実際の GitHub での動作は未検証。** Test は、Test を実行する User の一時 Directory の bare Repository（GitHub の代わり）と、`gh api` のように答える Fake の `GhRunner` だけを使う（本物の Credential・Network・他の User の Home は使わない）。
3. `gh api` の出力の形は GitHub の REST API の形に依存する。違う形は `invalid_response` で拒否する（fail closed）。
4. Push の後、PR を作る前に失敗すると、GitHub に `paw/` の branch だけが残る。次の Gate の実行で PR が作られる（冪等）。削除はしない（Decision 0036 の 12 と同じく、branch の削除は別の操作）。
5. PR の状態は作った時点のもの。GitHub で後から閉じられても、記録は `open` のまま（10）。

## 判断が必要な点

1. 時期（Review の後、`CHANGED` の確認の後、完了の前）と、検査した Commit を Push すること（1）。推奨: 承認。
2. 今の Scope で `target` の Repository だけに作ること（2）。推奨: 承認。
3. 登録した Remote の URL へ、`paw/` の同じ名前の branch に、Force なしで Push すること（3）。推奨: 承認。
4. Credential は作成者の `gh auth login`（git の Credential Helper と `gh api`）を使い、`gh` を他の Linux User として動かす経路は別の Issue にすること（4）。推奨: 承認（別の Issue）。
5. `project.pr.create` を作成者の Agent の行為として判定し（`github.use` は判定しない）、Human の承認は求めず（`SCOPED_AUTO`）、`REQUIRED` の Audit と Task Log と試行の状態に残すこと（5）。推奨: 承認。
6. PR の Title と本文（検査の文章と Task の入力を入れない）、Base は default branch（6）。推奨: 承認。
7. 既存の PR を再利用し、`closed` のときは新しく作らないこと（7）。推奨: 承認。
8. 作れなかったときは `evaluating` のまま（`not_published`）にすること（8）。推奨: 承認。
9. Wrapper の許可リストへの `push` の 1 つの形の追加（9。Decision 0029 の 3 の部分的な Supersede）。推奨: 承認。
10. 新しい Table を作らず、PR の状態を GitHub から読み直さないこと（10）。推奨: 承認。

## 承認後の扱い

承認されたら `Status` と `Approval` を改める（Human が行う。この Decision を Agent が Approved にしない）。方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。
