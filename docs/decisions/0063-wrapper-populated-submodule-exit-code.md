# SSH の Wrapper が中身のある Submodule を拒否したときの終了コードを 125 に分ける（Decision 0029 の 5・Decision 0051 の 5 の終了コードの約束の変更）

- Status: Proposed
- Date: 2026-09-30
- Scope: Issue [#90](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/90) の #130 `integration/git.py:305` の保留（PR #163 の説明と #90 のコメント）。SSH の Wrapper（`apps/backend/deploy/ssh-git-wrapper/paw_git_wrapper.py`）の終了コードと、`WorktreeGit.is_exactly_committed`（`apps/backend/paw_backend/integration/git.py`）の読み方だけを扱う。Migration はない
- Supersedes: [Decision 0029](0029-per-user-git-runner-ssh.md) の 5 と [Decision 0051](0051-integration-worktree-ignored-files.md) の 5 の**終了コードの約束だけ**（Wrapper の拒否は固定の 126 の 1 つ、という前提）。0029・0051 のそれ以外（`ssh_unavailable` の 255、許可する形、Fail closed）は変えない。0029・0051 は書き換えない

## 背景

Decision 0051 の 4 は、integration worktree の `submodule status --cached` が失敗すれば（`.gitmodules` にない gitlink の `no submodule mapping` など）その worktree を clean でない（`dirty`。Human が片付けて待つ）とした。Wrapper（PR #150）は、Decision 0051 の 5 の `status` / `submodule status --cached` を、中身のある Submodule があるとき `populated_submodule` で拒否する。拒否の終了コードは理由によらず固定の **126**（Decision 0029 の 5 は「Wrapper の終了コードは 0〜254」とだけ決め、Wrapper の README が 126 を書いた）。

そのため Backend は、終了コードだけでは「中身のある Submodule（Human が片付ける `dirty`）」と「それ以外の拒否（呼び出しの形・Wrapper・Server の設定の誤り。Human が worktree を片付けても直らない）」を区別できない。PR #163 で 126 を `git_failed` にした変更は、中身のある Submodule を Retry のたびに `git_failed` にしたため（Codex、P1）戻し、今は 126 を `dirty` として読んでいる。これでは Server の設定の誤りも「worktree を片付けてください」として Human を待たせる。stderr の理由コードは Log に出さず読まない方針（Decision 0029 の 5）なので、stderr で区別することもしない。

## 提案

1. **Wrapper は `populated_submodule` の拒否に、126 と別の固定の終了コード 125 を返す。** それ以外の拒否は今までどおり 126。125 は git の 0／1（答え）・128／129（git 自身の失敗）・`ssh` の 255 のどれとも重ならず、Decision 0029 の 5 の「0〜254」に収まる。Log と stderr の理由コード（`populated_submodule`）は変えない。
   - 推奨: 125。代替: 別の値（例 124 は `timeout(1)` の慣例と重なるので避ける）。
2. **Backend（`is_exactly_committed`）は `submodule status --cached` の終了コードを次のように読む。**
   - 125: clean でない（`dirty`。今と同じ）
   - 126: `GitCommandError(NONZERO_EXIT)`（`git_failed`）。Human が worktree を片付けても直らないので、待たせずに失敗させる
   - それ以外の 0 以外（git 自身の失敗。`no submodule mapping` の 128 など）: clean でない（`dirty`。Decision 0051 の 4 のまま）
   - 推奨: この読み方。代替: 125 以外の 0 以外をすべて `git_failed` にする（`no submodule mapping` は Human が `.gitmodules` を直すべき worktree の状態なので採らない）。
3. **値は Backend の `paw_backend.repositories.ssh` に `WRAPPER_POPULATED_SUBMODULE_CODE`（125）・`WRAPPER_REJECTED_CODE`（126）として置き、Wrapper は `POPULATED_SUBMODULE` / `REJECTED` として持つ。** Wrapper は Backend を import しない単独の Script なので、値は 2 か所にあり、Test がそれぞれの値を固定する。
   - 推奨: この形。代替: 共有の Module にする（Wrapper を Server に置く手順が増えるので採らない）。
4. **配備の順序。** Backend だけが新しく Wrapper が古い Server では、中身のある Submodule の拒否も 126 で届き `git_failed` になる（Fail closed。Merge Ready は出さない）。Wrapper を先に（または同時に）入れ替える。Wrapper だけが新しい Server では、125 は「126 以外の 0 以外」として今と同じ `dirty` になる。
   - 推奨: Wrapper を先に入れ替える手順を README に書く（`SshGitRunner` は本番の経路にまだ配線されていないので、今は影響がない）。

## リスク

1. 将来 Wrapper に「Human が片付ける」種類の拒否が増えたときは、同じ形で終了コードを足す必要がある（新しい Decision）。
2. 126 を `git_failed` にしたので、Wrapper の設定の誤り（`--root` の誤りなど）で統合が止まると、Human は Task の失敗（`git_failed`）として気づき、Server の Auth Log（`syslog` の理由コード）で原因を見る。`dirty` の待ちとしては見えない。
