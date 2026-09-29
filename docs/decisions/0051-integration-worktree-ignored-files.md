# integration worktree の無視された File（`.gitignore`）を未 Commit の変更として扱う方針（Decision 0036 の 7・9・13 への追補）

- Status: Approved
- Date: 2026-09-29
- Scope: PAW-035（PR #130）の `WorktreeGit.is_exactly_committed` と、`GitWorktreeCoordinator` の integration worktree の確認（`integrate` の merge 前、`targets` の `clean`）。Migration はない
- Supersedes: [Decision 0036](0036-parallel-worktree-integration.md) の 13 の表の `status` の行（下の 3 の形を足す。今の形は残す）。0036 は書き換えない
- Approval: 2026-09-29、Human が作業 Session 内で、判断点ごとの説明（推奨つき）を受け、3 回に分けて回答して承認した（3 の `status` の形、4 の初期化済み Submodule の扱い（案 A）と `submodule status --cached`、5 の Wrapper の形。末尾の「承認時の決定」）

## 背景

Decision 0036 の 9 は「検査は integration worktree の Directory を読むので、検査の前に integration worktree に未 Commit・未追跡の変更や進行中の merge があれば検査せずに `failed`（`dirty`）、検査の間に worktree が書き換わったら `failed`（`changed`）」と定め、Merge Ready は検査した Commit を指す。
実装はこの確認に `git status --porcelain=v1 -z --untracked-files=all` を使っていた。この形は **`.gitignore` で無視された File を列挙しない**（`--ignored` が別に要る）。そのため integration worktree に Commit されていない `.env`・生成された Source・前の検査が残した成果物があっても「clean」となり、Merge Ready の Commit に含まれない内容を読んだ検査の結果で Task が完了し得た（PR #130 の Codex review、P1）。同じ確認は、Repository の `.gitmodules` が `ignore = all` とする Submodule の中の変更も見落としていた（2 回目の P1）。さらに、親の `status` は Submodule 自身の `.gitignore` で無視された File（`sub/.env` など）を、`--ignored` と `--ignore-submodules=none` を付けても列挙しない（3 回目の P1）。初期化されていない Submodule の空の Directory に書かれた File も、親の `status` には出ない（実装時に git 2.53 で確認）。

## 提案

1. **integration worktree では、無視された File も未 Commit の変更に数える。** 次の 3 か所で使う。
   - `targets`（Gate の検査の前と後。検査の前にあれば `dirty`、検査の間に作られれば `changed`）
   - `integrate` の merge の前（あれば `dirty` で `waiting`（理由 `user`）。Gate まで進んで毎回 `failed` になる Retry の繰り返しにしない）
2. **Worker の worktree では、無視された File を数えない（今のまま）。** 統合するのは Worker の branch の Commit だけで、Worker の Test が残す Cache（`__pycache__`・`.venv` など）は結果に入らない。数えると、Test を走らせた Worker はすべて `dirty` で止まる。
3. **git の形は `status --porcelain=v1 -z --untracked-files=normal --ignored=traditional --ignore-submodules=none`（この順の 6 語で固定）。** `--untracked-files=normal` は未追跡・無視された Directory を 1 行（`node_modules/` など）にまとめるので、大きな Directory があっても Runner の出力の上限に収まる（出力は空かどうかだけを見る）。`--untracked-files=all --ignored` は Directory の中の File を 1 つずつ並べ、上限を超えて `git_failed` になり得る。 `--ignore-submodules=none` は、Repository 自身の `.gitmodules`（Commit された File）や設定の `submodule.<name>.ignore = all` などで、初期化された Submodule の中の変更が `status` から隠れるのを防ぐ（隠れると、Commit にない Submodule の内容を読んだ検査で完了し得る。PR #130 の Codex review、2 回目の P1）。
4. **初期化された Submodule を持つ integration worktree は clean でない（fail closed。3 回目の P1 への Human の選択 A）。** 3 の `status` の前に、`submodule status --cached`（この順の 3 語で固定）を動かす。この形は Index の gitlink ごとに 1 行を出し、初期化されていない Submodule の行は `-` で始まる。`-` で始まらない行が 1 つでもあれば（初期化された Submodule。改行を含む Path で分かれた行も含む）、または失敗すれば（`.gitmodules` にない gitlink は `no submodule mapping` で失敗する）、その worktree は clean でない（`targets` は `clean=False`、`integrate` の merge の前は `dirty`）。すべての行が `-` で始まる（または出力が空の）ときだけ 3 に進む。`--cached` は Submodule の作業ツリーの差分を取らない（Submodule の中で filter 等を動かさない）。1 行は Submodule 1 つなので、出力は Runner の上限に収まる。この確認が先なので、3 の `status` が初期化された Submodule の中で動くことはない。3 の `--ignore-submodules=none` は多重の防御として残す。integration worktree は `worktree add` で作られ Submodule を初期化しないので、Submodule を持つ Repository も、誰かが初期化しない限り統合できる。
5. **Decision 0029 の Wrapper の許可リスト（Decision 0036 の 13 の表）に、3 の形を `status` の 2 つ目の形として、4 の `submodule status --cached` を新しい副コマンドの唯一の形として足す**（読み取りだけで、他の引数は取らない。`--git-dir` / `--work-tree` の条件は 0036 の 13 の承認時の決定のまま）。Wrapper（Issue #134、PR #150）はこの承認の後に、この形を受け付けるようにする。それまで `SshGitRunner` 経由の統合と Gate は Wrapper に拒否されて `git_failed` で止まる（fail closed）。
6. 検査（PAW-011 / PAW-013 の実際の Evaluator・Reviewer。別 Issue）は、integration worktree に書かない（依存の Install・Cache を含む）か、使い捨ての Copy で動く必要がある。書けば 1 により Task は `changed` で `failed` になる。これは 0036 の 9 がすでに課している「検査の間に worktree が書き換わったら `changed`」を、無視された File にも広げたものである。

- **推奨: この形で承認する。** 代替: (a) 検査を毎回、integration の Commit から作った使い捨ての worktree で動かす（無視された File は構造上入らないが、worktree の作成・削除の規則（0036 の 1・12）を変える）、(b) 無視された File を数えず、既知の制約として残す（Codex の指摘の穴が残る）。

## リスク

1. 検査の実装が integration worktree に依存を Install する形（`npm install` など）だと、2 回目以降の Gate は毎回 `dirty` になる。検査の実装の Issue で、使い捨ての Copy で動く形にする必要がある（6）。
3. integration worktree で Submodule を初期化すると、その Repository の統合は `dirty`（`waiting`）で止まり、Gate は `dirty` で `failed` になる（4）。Submodule の中まで確かめる方法（Submodule ごとの確認、使い捨ての worktree）は、必要になったときに別の Decision にする。
4. **残る穴:** 初期化されていない Submodule の空の Directory に書かれた File は、親の `status` にも `submodule status --cached` にも出ない（背景）。integration worktree に書けるのは Human だけ（Agent の Scope の外。Decision 0036）なので、Human が置いた `.env` と同じ扱いの範囲だが、4 はこれを検出しない。塞ぐには、gitlink が 1 つでもあれば clean でないとする（Submodule を持つ Repository は統合できなくなる）必要がある。
2. Human が integration worktree で Conflict を解くときに作った無視された File（Editor の一時 File など）も `dirty` になる。Human が消すまで統合は止まる（黙って消すより安全）。

## 承認時の決定（2026-09-29）

Human は作業 Session で、Codex の指摘ごとに推奨つきの説明を受け、3 回に分けて回答した。3 回とも推奨どおりで、個別の変更はない。

1. 無視された File を数える形（`status --porcelain=v1 -z --untracked-files=normal --ignored=traditional`）を integration worktree の確認に使い、0036 の 13 の許可する形に足す: 承認。
2. Codex の 2 回目の指摘（`.gitmodules` の `ignore = all`）を受けて、同じ `status` に `--ignore-submodules=none` を加える: 承認。
3. Codex の 3 回目の指摘（初期化済みの Submodule の中で、その Submodule の `.gitignore` が無視する File が親の `status` に出ない）には、案 A（初期化済みの Submodule があれば integration worktree を clean と扱わない。Fail closed）を選ぶ。案 B（Submodule ごとに再帰的に検査する）、C（既知の制約として残す）、D（使い捨ての worktree で検査する）は採らない。検出には読み取り専用の Git の形を 1 つだけ足す。その形は 4 の `submodule status --cached` とする: 承認。

PR #150 の SSH の Wrapper は、5 の 2 つの形（`submodule status --cached` と 3 の `status`）を許可する。
