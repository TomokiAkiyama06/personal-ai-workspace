# Local Agent の Runtime の GPU の Smoke の報告（#208 の PR 5、2026-10-15）

- Issue: [#208](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/208)（[Decision 0083](../decisions/0083-agent-runtime-adapters.md) の 9 の「GPU を使う Smoke Test」と 11 の 5）
- 実施: 2026-10-15 16:48〜17:50（Human の許可: 2026-10-15「GPU 使ってよい」）
- 見ること（0083 の 9）: 本番の `paw-llm-main.service`（12 の Flag を足したもの）で paw-seed-v1 の数 Task を `LocalAgentRuntime` に解かせ、Benchmark の Harness と **Tool 呼び出しの形・Step 数が大きく違わない** こと。合否の規則はない（Smoke）。
- 生の結果（Trace・Patch・Server の Log）は Repository に入れない（Hidden check の内容を含むため。Decision 0041 の 4）。Server の `/data/results/paw-smoke-2026-10-15`（`coding/`、集計は `aggregate_smoke.json`）にある。

## 1. 条件

### Model の Server

- vLLM 0.30.0、Qwen3.8-27B-FP8（これまでの Benchmark と同じ NVMe に Stage した Weight）。
- 引数は `apps/backend/deploy/systemd/paw-llm-main.service` の `ExecStart` と同じ: `--served-model-name main --host 127.0.0.1 --port 18080 --reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser qwen3_coder`。`--max-model-len` と `--gpu-memory-utilization` は Unit のとおり付けていない（0083 の 12: 配備で決める）。
- 安全の設定（Decision 0039 の 4・0072）: `MAX_JOBS=4`、`FLASHINFER_NVCC_THREADS=1`、起動の前に `MemAvailable` 40 GiB 以上、8 GiB を下回ったら自分の Process group だけを止める Watchdog（止めたことはない）、起動の前に GPU に他の Process がないことの確認（空き 97,231 MiB）。GPU にはこの Smoke の Process だけがいた。

### `LocalAgentRuntime` の側（本番の部品をそのまま使い、DB だけ In-memory）

`main` の `7a67984`（#242 の後）の Backend を、本番の組み立て（`orchestrator/composition.py`）と同じ順に繋いだ:

```text
LocalAgentRuntime（既定の LocalAgentLimits・MAIN_REASONING_POLICY）
  → NodeToolkit → NodeTools.call → ToolRunner(ToolBroker, RepoToolExecutor, 300 秒)
  → SandboxPool → LocalWrapperSandboxRunner → 本物の paw-git-wrapper（paw-sandbox/v1）
  → Container（deploy/sandbox/Containerfile で Benchmark の Image から作った Image）
  Sandbox の中の git = 本物の Shim → Shim の Socket → Broker の repo.git.read → Host の git
Worktree: 本物の GitWorktreeCoordinator.prepare_node（Branch paw/<task>/<run>/impl、refs/paw/base/*）
```

- Tool Broker の Audit・承認・Budget・Lease・Task の状態は Test 用の In-memory の実装（`tests/tools_support.Harness`）。Account はこの Linux User（`FakeAccounts`）。
- **Podman の代わりに Docker を使った**: この Host にはまだ Podman がない（CLI の Smoke の前提として Human が入れる予定）。Wrapper の `--podman=` に、Podman の引数を Docker の引数に直すだけの小さな Script を渡した。違いは Podman だけの Option（`--userns keep-id`、`run --timeout`、`rm --time / --ignore`）を落としたことだけで、Mount・Mask・`--network none`・`--cap-drop all`・`--user <uid>:<gid>`・上限・Shim の Mount は Wrapper が組み立てたとおり。
- Task の文: Benchmark の Harness の User の文（`issue_text` と「## Visible checks」）を、先頭の `# ` の行を `title`、残りを `goal` にして `NodeAssignment` に入れた。Model に届く User の文は Harness と同じになる。
- 評価: Benchmark の Harness（2026-10-08 の版）の `eval-patch` で、起点の Commit から Node の最後の状態（Runtime の Commit と未 Commit の変更）までの差分を Hidden check にかけた。

### 比べた Run

同じ Server（2 回起動。1 回目に Harness、2 回目に `LocalAgentRuntime` の再実行と実験）で、paw-seed-v1 の 5 Task（bug-01・bug-03・bug-06・spec-02・spec-03。これまでの Run で 7〜30 Step で解ける Task）を 4 Task 並列で:

| 名前 | 何 | 回数 |
| --- | --- | ---: |
| harness | Benchmark の Harness（10-08 の版）。Reasoning の方針は `DropOlderReasoning(keep_last=2, min_prompt_tokens=32768)`（Runtime の既定と同じにした） | 2 |
| runtime | `LocalAgentRuntime`（上の構成。本番の Sandbox の Image） | 2 |
| runtime（実験） | 同じ。ただし Sandbox の Image に本物の git を残し、`/workspace` の外の Repository の git だけ本物で動かす（下の 3。**製品の形ではない**） | 2 |

## 2. 結果

### 解けた数・終わり方・Tool 呼び出しの形

| | harness | runtime | runtime（実験） |
| --- | ---: | ---: | ---: |
| 解けた（5 Task × 2 回） | **10 / 10** | **10 / 10** | 10 / 10 |
| `submit` で終わった | 10 | 10 | 10 |
| Step の合計（平均） | 125（12.5） | **161（16.1）** | 139（13.9） |
| Tool 呼び出しの合計 | 184 | 222 | 193 |
| 1 Step あたりの Tool 呼び出し（最大） | 1.47（3） | 1.38（2） | 1.39（3） |
| 引数が JSON の Object でない呼び出し | 0 | 0 | 0 |
| Tool を呼ばない応答（催促） | 0 | 0 | 0 |
| 1 Step の出力 token の中央値（p90） | 200（804） | 262（913） | 248（886） |
| Prompt の最大 | 31,872 | 31,356 | 30,749 |
| 所要の合計（10 Task、秒） | 2,136 | **3,957** | 2,405 |

Task ごとの Step 数（run1 / run2）:

| Task | harness | runtime | runtime（実験） | 参考: 10-07 の同時に動かす Run（3 回） |
| --- | --- | --- | --- | --- |
| bug-01 | 7 / 8 | 12 / 12 | 9 / 8 | 8 / 8 / 7 |
| bug-03 | 14 / 10 | 14 / 15 | 14 / 11 | 10 / 11 / 11 |
| bug-06 | 8 / 7 | 9 / 11 | 8 / 9 | 10 / 9 / 8 |
| spec-02 | 19 / 29 | 27 / 27 | 25 / 29 | 21 / 22 / 17 |
| spec-03 | 12 / 11 | 16 / 18 | 14 / 12 | 14 / 17 / 11 |

- **Tool 呼び出しの形は Harness と同じ**: 4 つの Tool（`bash`・`str_replace`・`write_file`・`submit`）、`tool_choice: auto`、`max_tokens` 16,384 が毎回送られ、Model は Parser を通った構造化された `tool_calls` で答えた（壊れた引数・Tool なしの応答は 0）。1 つの応答に複数の呼び出しを並べる割合も同じくらい。Reasoning は毎回 `reasoning_content` として返り、履歴に残った。`DropOlderReasoning` が外した Reasoning は、runtime では 0（送った履歴の Assistant の Message がすべて Reasoning を持っていたことで確認。外す前の見積もり〔直前の `prompt_tokens + completion_tokens` + その後の Tool の結果 ÷ 3〕の最大は約 31,300 で閾値 32,768 を超えなかった）、Harness では 1 回（harness_run2 の spec-02、見積もりが閾値を超えた Step で 25 件を外した）。
- **Backend の側の処理は期待どおり**: すべての Tool が Broker で `allow`（`scoped_auto` / `auto`）になり、Executor の失敗は 0。20 回の `submit` はすべて Branch の差分と突き合わされて 1 つの Commit になり（未 Commit の変更は 0、`refs/paw/base/<branch>` は起点のまま）、spec-02 では申告の `changed_files` が差分と合わず 1 回直させた（2 回とも）。
- **Step 数は runtime が Harness より多い**（平均 12.5 → 16.1。2 回の合計では 5 Task すべてで多い）。**所要は約 1.9 倍**で、その大半は Tool の実行の時間（下）。原因は次の 3 の Sandbox の git で、それを変えた実験では Step 数・所要とも Harness に近づいた（13.9、1.1 倍）。

## 3. 大きな違い: Sandbox に本物の git がないため、Repository の Test が失敗して見える

- 本番の Sandbox の Image は、Decision 0083 の 2 と 6 のとおり本物の git を消し、`git` を Shim（`status`・`diff`・`log`・`show` を Host へ渡す）にしている。
- paw-seed の Repository の `benchmarks/tests/test_worktree_runner.py`（89 件）は、Test の中で `/tmp` に一時の Repository を `git -C /tmp/... init` で作る。Shim はこれを拒む（作業 Directory も `-C` も Shim の外）ので、**System Prompt が勧める `python -m unittest discover -s benchmarks/tests` は、Benchmark の Sandbox では 411 件中 failures 1（Task が直すもの）なのに、本番の Sandbox では failures 1・errors 89 になる**（bug-06 の起点の `80b50b1` と spec-02 / spec-03 の起点の `eb236c7` で、どちらも 411 件。Benchmark の Image では OK または failures 1、本番の Sandbox の Image では errors 89 が足される。1 回 70〜90 秒）。
- Model はこの 89 件を自分の変更のせいか確かめようとして、Test を何度も走らせ（Command を記録した runtime の 1 回〔5 Task〕で 30 回。Harness は 1 回あたり 14 回）、`git stash` で変更前に戻して比べようとして拒まれ（同じ 1 回で 4 回）、`tar` で元の File を取り出す回り道をした。Step と所要の増加はほぼこれで説明できる:
  - 実験（Image に本物の git を `/usr/lib/paw-exp/git` として残し、作業の場所と `-C` が `/workspace` の外で、`GIT_DIR` などの環境変数も `/workspace` を指す引数もないときだけ本物の git を、それ以外は Shim を使う）では、Test の実行は 1 回あたり 14〜16 回、`git stash` は 0 回、Step は平均 13.9、所要は 2,405 秒（Harness の 1.1 倍）になった。
- 製品の Repository でも同じことが起きる見込み（`apps/backend/tests` の多くは本物の git で一時の Repository を作る）。
- 正解数には影響しなかった（10 / 10）。ただし誤った失敗の表示は Model を迷わせ、時間と token（出力 token の合計 51k → 67k）を使う。直し方は Sandbox の安全の設計（0083 の 2・6）に関わるので、Decision 0090（Proposed、[#245](https://github.com/TomokiAkiyama06/personal-ai-workspace/pull/245)）で Human の判断を求める。

## 4. 小さな違い（Decision 0083 の設計どおり）

- `git log` は起点から `HEAD` までに限られる（0083 の 2）。起点の直後は空になり、Harness のように Repository の履歴は見えない。Model が `git log` を呼んだのは少なく（Command を記録した runtime の 1 回で 3 回）、影響は見えなかった。
- `git stash`・`git checkout` などは Shim が拒む（`subcommand_not_allowed`。2 回の runtime で 11 回。Tool の出力の先頭で数えた）。拒んだときの文は何が使えるかを示すので、Model は別の方法に切り替えた。`HEAD:<path>` の形は `revision_not_allowed`（1 Task で 2 回）。
- `submit` の `changed_files` の突き合わせ（0083 の 10）で 1 回直させる Task があった（spec-02。直した後は通った）。

## 5. Server の設定について Human に知らせること

- `paw-llm-main.service` の今の引数では、vLLM 0.30.0 の既定で `--max-model-len` は Model の最大の **262,144**、`--gpu-memory-utilization` は **0.92** になり、KV の Pool は 54.6 GiB・876,375 token、GPU の使用の Peak は 90,834 MiB だった。単独で動かすならこれで動くが、共存（Decision 0073 / 0075 / 0076: 0.51・131,072）の値ではない。0083 の 12 のとおり配備で付ける必要がある。
- `--language-model-only` を付けないので、Vision の部分も Load される（Model の Load は 28.51 GiB。付けた Benchmark は 27.64 GiB）。Coding の Agent には使わないので、配備で付けるか決めてよい（付けるなら Footprint は Decision 0075 の値のまま）。

## 6. まとめ

- `LocalAgentRuntime` は本番の Tool Broker・Sandbox・Worktree・`submit` の Commit を通って、5 Task × 2 回をすべて解いた。**Tool 呼び出しの形は Benchmark の Harness と変わらない。**
- **Step 数は平均 1.3 倍、所要は 1.9 倍**に増えた。原因は Sandbox に本物の git がないことで Repository の Test が 89 件失敗して見えることで、`/workspace` の外だけ本物の git を使う実験では Harness に近づいた（1.1 倍）。扱いは Decision 0090（Proposed、#245）。
- Code の Bug は見つからなかった。
