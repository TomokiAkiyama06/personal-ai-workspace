# Seed Benchmark Dataset（paw-seed-v1）の構成

- Status: Proposed
- Date: 2026-09-28
- Scope: Issue [#13](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/13)（PAW-016: Seed Benchmark Dataset）。関連: PAW-010（#7、Task schema）、PAW-013（#10、Test / Hidden Acceptance Runner、[Decision 0001](0001-hidden-check-boundary.md)）、PAW-017（#14、Main Coding Model比較Run）
- Supersedes: なし（[BENCHMARK_EVALUATOR.md](../BENCHMARK_EVALUATOR.md)の「Deferred details」のうち、Seed task数・最初に使うRepo・Hidden testの保管方式を埋める）

## 背景

[MODEL_CANDIDATES.md](../MODEL_CANDIDATES.md) の 1 は、採用モデルを公開Benchmarkだけでは決めず、
Personal AI Workspace自身の実Repo / Issue / PR / Test / Agent workflowで評価すると定める。
[BENCHMARK_EVALUATOR.md](../BENCHMARK_EVALUATOR.md) は、Taskの出典（Historical real bug / issue、Spec-based feature task、Injected bug）と、
Historical taskでは「Buggy commitでtestがFAIL」「Known-good commitでtestがPASS」を重視することを定めるが、
Task数・最初に使うRepo・Hidden testの保管方式は未決（`[BENCHMARK]` Deferred details）である。
Issue #13 の受け入れ条件は、Historical / Spec / Injected Bugを含むこと、単純編集だけでなくmulti-file / repo exploration / test修正を含むこと、
難易度とカテゴリの記録、Golden behaviorをExecutable Testで確認すること。

この Decision はSeed setの中身の推奨を示す。PR（Issue #13）は推奨どおりに実装しており（下の「実装の状態」）、
承認されない点があれば別のPRで直す。承認されるまで、PAW-017の比較Runでこのsetを正式な判定には使わない。

## 提案

### 1. 出典: この公開Repository自身の3種類のTask

| 種類 | 作り方 | starting commit | known-good（Golden） | Hidden check |
| --- | --- | --- | --- | --- |
| Historical | Mergeされた PR（squash merge）1つを1 Task にする | merge commit の親（PRの直前の`main`） | merge commit（PR 全体の差分を Golden patch として保存） | PR が追加・変更した test module（test・fixture・support file）。FAIL_TO_PASS |
| Spec | Issue・REQUIREMENTS・docs に沿った、まだ無い小さな機能を作者が仕様として書く | Dataset の base commit（`eb236c7`） | 作者が書いた実装（非公開の Golden patch） | 作者が書いた test |
| Injected Bug | base commit に 1 つの bug（複数 file にまたがってもよい）を入れる。Test の期待値を壊す test 修正型を含む | bug を入れた tree の、親を持たない snapshot commit | bug を入れる前の base（逆 patch） | 既存の test（base の版で上書き）か、作者が書いた test |

- Historical は、starting commit で Hidden check が**失敗**し、merge commit で**成功**することを実行して確かめたPRだけを採る（下の 9）。
- Historical の `issue_text` は、元の Issue の本文、元の PR の説明のうち仕様にあたる節（概要・変更・内容・修正・受け入れ条件・テスト。検証結果・運用・Merge の節は除く）、
  Hidden test が import する module と名前の一覧、共通の作業条件で作る。PR の test は内部の名前や message まで確かめるため、
  名前の一覧がないと仕様を満たしても不合格になる。これは解法の手掛かりを増やすので、Historical の結果は Spec / Injected と分けて集計する（下の 6）。
- 各 Task には、候補が見られる visible check（`ruff check`、一部の Injected Bug では失敗している test module）と、見られない Hidden check（acceptance と regression。test 修正型では追加で「直した test が実装の誤りを検出するか」と「test 以外を変えていないか」）を置く。regression（PASS_TO_PASS）は、同じ領域の、PR が変えていない既存の test module。
- 他の Repository（ユーザーの別 Project、公開 Benchmark）は v1 では使わない。追加するときは Repository ごとに License と非公開性を確認する（下の 7）。

代替案: SWE-bench などの公開 Benchmark を Seed にする。自分の Repo・Agent workflow で測るという要件（MODEL_CANDIDATES.md 1）に合わず、学習データへの混入も大きいため採らない。

### 2. 規模: v1 は 24 Task

- Historical 11、Spec 6、Injected Bug 7（Issue の最低数 8 / 6 / 6 を満たす）。
- Benchmark 領域（`benchmarks/`、PostgreSQL 不要）が 18、Backend（`apps/backend`、PostgreSQL が要る Task を含む）が 6。
- 1 候補 1 回の Hidden check の実行時間は、Golden で数秒〜数分（PostgreSQL の Task は Database の作成を含めて 1 Task あたり数分）。24 Task × 候補数 × 試行回数（Resolved@N）が 1 晩で終わる規模にした。
- 統計的な差を見るには少ないので、PAW-017 の最初の Run の結果を見て v2（目安 50 Task）へ増やす。増やすときは `paw-seed-v2` として新しい版を作り、**比較 Run に一度使った版の Task は書き換えない**（結果の比較可能性を保つため）。

### 3. 難易度とカテゴリ

Task schema v1 は top level の追加 field を許さない（`additionalProperties: false`）。Schema は変えず、Dataset の索引 `benchmarks/seed-tasks/paw-seed-v1/manifest.json` に Task ごとに記録する。

- 難易度（`difficulty`）: `easy`（1 file・数行の修正で、症状から場所が分かる）/ `medium`（複数 file、または Repository の探索が要る）/ `hard`（新しい subsystem や大きな契約、Process・Database の並行性、数百行以上の Golden）。
- カテゴリ（`categories`、複数可）: `feature`、`bug_fix`、`security`、`multi_file`、`repo_exploration`、`test_fix`、`schema_or_validation`、`concurrency_or_process`、`database_migration`、`api_contract`、`metrics`。語彙は `benchmarks/seed_dataset.py` の `CATEGORIES` で固定し、索引の検査が未知の値を拒否する。
- v1 の内訳: easy 4 / medium 12 / hard 8。`multi_file` 10、`repo_exploration` 8、`test_fix` 1 など（`python -m benchmarks.seed_dataset check` で出る）。

### 4. Hidden check の保管: 公開 Repository の外の `/data/datasets/paw-seed-v1`

- 公開 Repository に置くのは Task JSON（Task schema v1）と索引だけ。Hidden check は opaque な `reference_id`（`seed-v1-` + 12 桁の 16 進）だけを持つ。
- Hidden check の中身（`hidden-checks.json`: `reference_id` → 実行方法と期待する結果、`overlays/`: Hidden test の file、`tasks/<task_id>/golden.patch`・`bug.patch`、`repos/<task_id>/`: 下の 5 の Repository）は、Evaluator の OS User だけが読める `/data/datasets/paw-seed-v1`（directory `0700`、file `0600`）に置き、commit しない。
- Hidden check は `benchmarks/seed_check.py`（Evaluator 側の checkout にある helper。候補の worktree の code ではない）が実行する。候補の worktree を一時 directory へ複製し（`.git` を除く）、その上に Hidden test を重ねて実行する。候補の worktree には何も書かない（Decision 0001 の「hidden の source を worktree へ複製しない」を保つ）。候補が同じ名前の test file を置いても上書きされる。
- skip された test は失敗として扱う（PostgreSQL の test が skip されて黙って合格にならないように）。PostgreSQL が要る check は、Evaluator が渡す管理用 URL の file から、check ごとに使い捨ての Database（`paw_seed_<乱数>`）を作って test に渡し、終わったら消す（消せなかったら check を失敗にする）。URL は log・出力に出さない。Test に渡す接続は管理用 URL と同じ Role を使う。Hidden test（Grant の test）が Role を作り、Database を作るので、`CREATEDB` / `CREATEROLE` のない Role には絞れない。そのため、この PostgreSQL は **Benchmark 専用の使い捨ての Cluster**（例: Run ごとに作って消す Container、Data は tmpfs）とし、他の用途の Database と共有しない。
- 退避（Backup）は、公開 Repository ではなく、非公開の場所（例: `/data` の定期 Backup、または非公開の Git Repository）にする。どこにするかは Admin が決める（この Decision では決めない）。
- 限界: Decision 0001 のとおり、Python の runner は防壁ではない。候補が同じ OS User で動く間は `/data/datasets/paw-seed-v1` を読めてしまう。本番の比較 Run は、候補を別の OS User か Container で動かし、この directory を見えなくする（PAW-017 の前提条件）。

### 5. 候補に見せる Repository

- Task JSON の `repository.locator` は `paw-dataset://paw-seed-v1/<task_id>` とし、Evaluator が `/data/datasets/paw-seed-v1/repos/<task_id>` へ解決する。`known_good_commit` は公開 JSON に書かない（書くと候補に解の commit を指すことになる）。
- Historical / Spec の Repository は starting commit とその祖先だけを持つ（`git log` / `blame` による探索は現実の作業と同じにできる。後の commit、つまり解は含まない）。
- Injected Bug の starting commit は、bug を入れた tree の**親を持たない** commit にする（親があると `git diff HEAD~1` で bug が分かる）。作者・日時・message を固定するので commit id は決定的で、`python -m benchmarks.seed_dataset build` が非公開の patch から作り直し、索引の commit id と一致することを確かめる。
- 候補の実行環境は Network に出られないこと（本番の Container）。出られると、公開 Repository から merge commit や `main` を取得できる。

### 6. 学習データへの混入（Contamination）

- この Repository は 2026-09-20 に公開され、Historical task の元になった commit は 2026-09-20〜09-27 のもの。候補モデルの学習データの締め切り（cutoff）と公開日がこれより前なら、元の PR・test はその学習データに含まれない。候補ごとに公開日と cutoff（分かる範囲）を記録し、cutoff が 2026-09-20 以降の候補では Historical の結果を別に扱う。
- Spec と Injected Bug の Golden・Hidden test は 2026-09-28 に作成し、公開していない。ただし Injected Bug の「正しい code」は公開 Repository にあるので、この Repository で学習したモデルは有利になり得る。
- 結果は種類（Historical / Spec / Injected Bug）ごとにも集計する。候補の patch が Golden とほぼ同じ（行単位の一致が極端に高い）場合は混入の疑いとして印を付け、人が確認する。
- 時間とともに混入は増えるので、v2 以降は新しい PR・新しい Spec を足し、古い Historical を外す。

### 7. License

- この Repository は Apache License 2.0（`LICENSE`）。Task は、この Repository の code・Issue・PR（所有者と、所有者のために動いた Agent が書いたもの）から作ったもので、第三者の code・data を含まない。公開する Task JSON と索引は Repository の一部として同じ License。
- 非公開の material（Hidden test・Golden）も、この Repository の code から作ったもので License 上の制約はない。非公開にするのは評価の公正さのためで、License のためではない。
- 他の Repository から Task を作るときは、その License が評価用の複製・改変を許すことと、非公開の code を公開 Task JSON に入れないことを確認する。

### 8. 人間の修正時間（`human_correction_ms`）の測り方

- 定義: Evaluator が未解決とした候補の結果（または解決したが Review で差し戻した結果）を、人が直して「Hidden check が全て成功し、人が受け入れた」状態にするまでの、人が作業した時間。
- 測り方: 人が明示的に開始・一時停止・終了する stopwatch（CLI）。開始は、人が候補の結果の worktree で修正を始めるとき、終了は、Evaluator の再実行が成功して人が完了を記録したとき。休憩は一時停止で除く。Evaluator の再実行の待ち時間は含める（実際に掛かる時間なので）。
- 記録: Result の `metrics.human_correction_ms`。候補がそのまま解決し修正が要らなかったら `0`、人が修正を試みなかったら**省略**（計測していないことと 0 を区別する。README の Result schema の方針）。60 分で打ち切り、打ち切ったものは未解決として記録する。
- 人の時間は高いので、全候補・全 Task では測らない。PAW-017 で上位 2 候補の未解決 Task から、人が選んで測る。
- 実装（stopwatch の CLI と Result への書き込み）はこの PR に含めず、PAW-017 の Issue で行う。

### 9. Golden behavior の確認

- `python -m benchmarks.seed_dataset verify` が、Task ごとに starting state と Golden state（starting commit に Golden patch を当てた状態）を `WorktreeRunner` で作り、Hidden check を `TestRunner.run_hidden` で、visible check を `TestRunner.run_visible` で実行する。
- 各 Hidden check は、期待する結果を `hidden-checks.json` に持つ: acceptance は starting で `failed`・Golden で `passed`、regression・forbidden changes は両方で `passed`、test 修正型の「実装の誤りを検出するか」は両方で `passed`。visible check は Golden で全て `passed`。1 つでも違えば、その Task は使えない。
- Evaluator・依存関係（`.github/requirements-ci.txt`）・Python を変えたら、比較 Run の前に `verify` をやり直す。v1 は Python 3.13、`.github/requirements-ci.txt` の現在の pin で確認した（Historical の古い starting commit でも、この pin で動くことを確認した）。

### 10. visible check の実行環境

visible check の command は `python` で始まる。Evaluator は、`PATH` の先頭を Benchmark 用の仮想環境（`.github/requirements-ci.txt` を入れたもの）にして実行する。候補にも同じ環境を渡す（公平比較の規則、BENCHMARK_EVALUATOR.md）。

## 実装の状態（この PR）

- `benchmarks/seed-tasks/paw-seed-v1/`: 索引（`manifest.json`）と 24 の Task JSON（全て `validate_task` で valid）。
- `benchmarks/seed_dataset.py`: 索引の検査（`check`、Credential らしい文字列の検出を含む）、非公開の Repository と Historical の Golden patch の作成（`build`）、Golden behavior の確認（`verify`）。
- `benchmarks/seed_check.py`: Hidden check の helper（複製と重ね合わせ、skip の拒否、使い捨ての Database、forbidden changes）。
- `benchmarks/tests/test_seed_dataset.py`: 公開 Dataset の検査と helper の test（CI で実行。非公開の material は不要）。
- `/data/datasets/paw-seed-v1`: 非公開の material（commit しない）。

## 決まっていないこと（人の判断が要る）

1. 上の 1〜10 の推奨の承認（特に、Historical の `issue_text` に Hidden test の import 名を入れること（1）、規模 24 と v2 の方針（2）、保管先と退避先（4）、Network を遮断した Container を PAW-017 の前提にすること（4・5））。
2. 非公開の material の退避先（4）。
3. 人の修正時間を測る対象の選び方（8）。

## 承認後の扱い

承認されたら Status を Approved にし、PAW-017 はこの版（`paw-seed-v1`）を使う。承認されない点は、別の PR で Dataset・索引・この提案を直す（承認前に比較 Run へ使った結果は、正式な判定に使わない）。
