# Tool の Sandbox で、`/workspace` の外の Repository には本物の git を使えるようにするか（#208 の GPU の Smoke の結果）

- Status: Proposed
- Date: 2026-10-15
- Scope: Issue [#208](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/208) の Tool の Sandbox の Image（`apps/backend/deploy/sandbox/`：`Containerfile`・`paw_git_shim.py`）。[Decision 0083](0083-agent-runtime-adapters.md)（Approved）の 2 の「Sandbox の中では本物の git を使わせず、Sandbox の `git` を小さな Shim にする」の適用の範囲。
- Supersedes（一部、承認されたら）: 0083 の 2 の上の 1 文を「**Node の Repository（`/workspace` の下）に対して** は本物の git を使わせず Shim にする」に狭める。Shim の許可の規則（副コマンド・Option・Revision・Pathspec・`.git` の File の読み取り専用の Mount）、6 の Mount と Mask、`--network none` は変えない。

## 背景

2026-10-15 の GPU の Smoke（報告は `docs/benchmarks/paw-208-local-agent-gpu-smoke-2026-10.md`。#208 の PR 5 の別の PR）で、`LocalAgentRuntime` を本番の Tool Broker と Sandbox で動かし、Benchmark の Harness と比べた。Tool 呼び出しの形は同じで、5 Task × 2 回はすべて解けたが、**Step 数が平均 1.3 倍、所要が 1.9 倍** になった。

原因は Sandbox に本物の git がないこと:

- 本番の Image は 0083 の 2 のとおり本物の git を消し、`/usr/local/bin/git` を Shim（`status`・`diff`・`log`・`show` を Host の Broker へ渡す）にしている。Shim は作業 Directory を見て Node の Repository として扱うので、`/tmp` に作った一時の Repository への `git init` なども Host へ送られ、拒まれる。
- Repository の Test には、Test の中で一時の Repository を本物の git で作るものがある。paw-seed の `benchmarks/tests/test_worktree_runner.py` の 89 件は、Benchmark の Sandbox では通るが、本番の Sandbox ではすべて `git -C /tmp/... init` の失敗で errors になる（System Prompt が勧める `python -m unittest discover -s benchmarks/tests` が failures 1・**errors 89**。Benchmark の Sandbox では failures 1 だけ）。この Repository の `apps/backend/tests` も多くが本物の git を使う。
- Model はこの失敗を自分の変更のせいか確かめようとして、Test を何度も走らせ（5 Task で 30 回。Harness は 14 回）、`git stash` で比べようとして拒まれた。
- 実験として Image に本物の git を残し、**作業の場所（`cwd` と `-C`）が `/workspace` の外で、`GIT_DIR` などの環境変数も `/workspace` を指す引数もないときだけ本物の git、それ以外は Shim** にしたところ、Test の実行は 14〜16 回、`git stash` は 0 回、Step は平均 13.9（Harness 12.5）、所要は Harness の 1.1 倍になった。

0083 の 2 が Shim にした理由は、(a) Linked Worktree の Git Directory は Mount の外にあり Sandbox からは使えない、(b) `.git` の File を書き換えれば別の Git Directory を指させられる、(c) 共有の Object Store から他の Task・Node・除外の Path の内容を読ませない、の 3 つで、どれも **Node の Repository** についてのものだった。`/tmp` の一時の Repository は Sandbox の中だけの物で、Host の Git Directory にも共有の Object Store にも届かない。

## 提案

### 1. Image に本物の git を残し、`/workspace` の外だけで使う（推奨）

- Image の本物の git を Path にない場所（例: `/usr/lib/paw/git`）に移し、`/usr/local/bin/git` を小さな前段にする。前段は次のどれかに当たれば Shim に渡し、どれにも当たらなければ本物の git を実行する:
  - 作業の場所（`cwd` に、`-C <path>` を順に当てたもの。`realpath` で解決）が `/workspace` か、その下。
  - 引数に `--git-dir`・`--work-tree`・`--namespace`・`--super-prefix`・`--exec-path` のどれかがある、または `/workspace` を含む引数がある。
  - 環境変数 `GIT_DIR`・`GIT_WORK_TREE`・`GIT_COMMON_DIR`・`GIT_INDEX_FILE`・`GIT_OBJECT_DIRECTORY`・`GIT_ALTERNATE_OBJECT_DIRECTORIES` のどれかがある。
- 前段は安全の境界ではない（Model は本物の git を直接実行できる）。**安全は今と同じく Mount と Mask が守る**: Sandbox から見えるのは Node の Repository の Mount（`.git` の File は読み取り専用、Git Directory と共有の Object Store は Mount されない、除外の Path は空の tmpfs）だけで、本物の git を `/workspace` に向けても、`cat` や `cp` でできる以上のことはできない（Git Directory がないので履歴も Object も読めない。書き込む Worker の Node は `bash` で Worktree に書けるのが今も同じ。読むだけの Node は Mount が読み取り専用）。`--network none` なので `clone` などの外への通信もできない。
- 前段の役目は使い勝手だけ: `/workspace` での `git status` / `git diff` を今どおり Shim（Host の正しい履歴と Scope の規則）に送ること。
- Test: 前段の振り分け（`cwd`・`-C` の組み合わせ・相対 Path・Symbolic Link・環境変数・`--git-dir`）、Image に本物の git が `/usr/local/bin/git` の他の名前で Path にないこと、`/workspace` の `.git` が読み取り専用のままであることを、今の Sandbox の Test（`test_sandbox_*`・`test_ssh_git_wrapper_sandbox.py`）に足す。GPU の再確認は要らない（上の実験で振る舞いを見た）。

### 2. Shim のまま、System Prompt で知らせる（代わりの案）

- System Prompt の「Environment」に「git は読み取り専用（`status`・`diff`・`log`・`show`）で、本物の git を使う Test は失敗する」と書く。Model の迷いは減る見込みだが、Repository の Test のうち git を使うものが走らなくなる（Model は自分の変更が壊したかどうかを確かめられない）。また 0083 の 2 が「Model から見える形は Benchmark と同じにする」とした System Prompt を変えることになる（Decision 0074 の数値と比べられなくなる）。

### 3. 変えない（代わりの案）

- 正解数には影響しなかった（10 / 10）。時間と token（出力 token の合計 51k → 67k）が多く使われ、Local の GPU の時間がその分長くなる。

## 決めてほしいこと

1. Tool の Sandbox の Image に本物の git を残し、作業の場所が `/workspace` の外で、Node の Repository を指す引数・環境変数がないときだけ本物の git を使う（1）か。推奨: **はい**（安全は Mount と Mask で今と同じ。Repository の Test が Benchmark と同じように走る）。代わりの案: System Prompt で知らせる（2）、変えない（3）。

## リスク

- Image の git の版の脆弱性が Sandbox に入る（Network なし・Capability なし・Account の権限だけなので、影響は Sandbox の中に限られる）。Image の更新で追う。
- 前段の振り分けの誤り（`/workspace` を指すのに本物の git に行く）は、Git Directory がないので `fatal: not a git repository` で失敗するだけで、範囲の外は見えない。

## 承認後の扱い

承認されたら `Approval` に記録して Status を Approved にし、`deploy/sandbox/` の Image と前段と Test を別の PR（Refs #208）で入れる。README の「Sandbox の中の `git`（Shim）」の節も直す。
