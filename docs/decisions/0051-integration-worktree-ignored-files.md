# integration worktree の無視された File（`.gitignore`）を未 Commit の変更として扱う方針（Decision 0036 の 7・9・13 への追補）

- Status: Proposed
- Date: 2026-09-29
- Scope: PAW-035（PR #130）の `WorktreeGit.is_exactly_committed` と、`GitWorktreeCoordinator` の integration worktree の確認（`integrate` の merge 前、`targets` の `clean`）。Migration はない
- Supersedes: [Decision 0036](0036-parallel-worktree-integration.md) の 13 の表の `status` の行（下の 3 の形を足す。今の形は残す）。0036 は書き換えない
- Approval: 未承認

## 背景

Decision 0036 の 9 は「検査は integration worktree の Directory を読むので、検査の前に integration worktree に未 Commit・未追跡の変更や進行中の merge があれば検査せずに `failed`（`dirty`）、検査の間に worktree が書き換わったら `failed`（`changed`）」と定め、Merge Ready は検査した Commit を指す。
実装はこの確認に `git status --porcelain=v1 -z --untracked-files=all` を使っていた。この形は **`.gitignore` で無視された File を列挙しない**（`--ignored` が別に要る）。そのため integration worktree に Commit されていない `.env`・生成された Source・前の検査が残した成果物があっても「clean」となり、Merge Ready の Commit に含まれない内容を読んだ検査の結果で Task が完了し得た（PR #130 の Codex review、P1）。

## 提案

1. **integration worktree では、無視された File も未 Commit の変更に数える。** 次の 3 か所で使う。
   - `targets`（Gate の検査の前と後。検査の前にあれば `dirty`、検査の間に作られれば `changed`）
   - `integrate` の merge の前（あれば `dirty` で `waiting`（理由 `user`）。Gate まで進んで毎回 `failed` になる Retry の繰り返しにしない）
2. **Worker の worktree では、無視された File を数えない（今のまま）。** 統合するのは Worker の branch の Commit だけで、Worker の Test が残す Cache（`__pycache__`・`.venv` など）は結果に入らない。数えると、Test を走らせた Worker はすべて `dirty` で止まる。
3. **git の形は `status --porcelain=v1 -z --untracked-files=normal --ignored=traditional`。** `--untracked-files=normal` は未追跡・無視された Directory を 1 行（`node_modules/` など）にまとめるので、大きな Directory があっても Runner の出力の上限に収まる（出力は空かどうかだけを見る）。`--untracked-files=all --ignored` は Directory の中の File を 1 つずつ並べ、上限を超えて `git_failed` になり得る。
4. **Decision 0029 の Wrapper の許可リスト（Decision 0036 の 13 の表）に、3 の形を `status` の 2 つ目の形として足す**（読み取りだけで、他の引数は取らない。`--git-dir` / `--work-tree` の条件は 0036 の 13 の承認時の決定のまま）。Wrapper（Issue #134、PR #150）はこの承認の後に、この形を受け付けるようにする。それまで `SshGitRunner` 経由の統合と Gate は Wrapper に拒否されて `git_failed` で止まる（fail closed）。
5. 検査（PAW-011 / PAW-013 の実際の Evaluator・Reviewer。別 Issue）は、integration worktree に書かない（依存の Install・Cache を含む）か、使い捨ての Copy で動く必要がある。書けば 1 により Task は `changed` で `failed` になる。これは 0036 の 9 がすでに課している「検査の間に worktree が書き換わったら `changed`」を、無視された File にも広げたものである。

- **推奨: この形で承認する。** 代替: (a) 検査を毎回、integration の Commit から作った使い捨ての worktree で動かす（無視された File は構造上入らないが、worktree の作成・削除の規則（0036 の 1・12）を変える）、(b) 無視された File を数えず、既知の制約として残す（Codex の指摘の穴が残る）。

## リスク

1. 検査の実装が integration worktree に依存を Install する形（`npm install` など）だと、2 回目以降の Gate は毎回 `dirty` になる。検査の実装の Issue で、使い捨ての Copy で動く形にする必要がある（5）。
2. Human が integration worktree で Conflict を解くときに作った無視された File（Editor の一時 File など）も `dirty` になる。Human が消すまで統合は止まる（黙って消すより安全）。
