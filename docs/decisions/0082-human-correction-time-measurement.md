# Human correction time（人が直す時間）の測り方（PAW-017 の後続、#181）

- Status: Approved
- Approval: 2026-10-08、Humanが作業Session内で、判断点ごとの説明（推奨つき）を受けたうえで直接回答して承認（すべての判断点を推奨どおり承認。末尾の「承認時の決定」）
- Date: 2026-10-07
- Scope: Issue [#181](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/181)（[Decision 0040](0040-main-coding-model-selection.md) の 5 の後続）。測る対象の Run は [Decision 0074](0074-seed-v2-and-qwen-27b-comparison.md) の paw-seed-v2 の比較 Run（[報告](../benchmarks/paw-017-seed-v2-2026-10.md)）
- Supersedes: なし。[Decision 0041](0041-seed-benchmark-dataset.md) の 8（人間の修正時間の定義・stopwatch・60 分の打ち切り・上位 2 候補の未解決 Task から人が選ぶ）を変えず、その具体化（対象の Task、数えるもの、記録の形式）だけを提案する

## 背景

MODEL_CANDIDATES.md は Main Coding Model の評価で Resolved@1 と並んで Human correction time を重視するが、PAW-017 の比較 Run では測っていない。
Decision 0040 の 5 は「採用 Model の未解決の Spec / Historical の Patch について人が測る（別の Issue）」とし、PAW-017 を条件つきで閉じた。
Decision 0041 の 8（承認済み）は次を決めている。

- 定義: Evaluator が未解決とした候補の結果を、人が直して「Hidden check が全て成功し、人が受け入れた」状態にするまでの、人が作業した時間。
- 測り方: 人が明示的に開始・一時停止・終了する stopwatch（CLI）。休憩は一時停止で除き、Evaluator の再実行の待ち時間は含める。
- 記録: Result の `metrics.human_correction_ms`。修正が要らなければ `0`、試みなければ省略。60 分で打ち切り、打ち切ったものは未解決。
- 対象: 全候補・全 Task では測らず、上位 2 候補の未解決 Task から人が選ぶ。stopwatch の実装は PAW-017 の Issue で行う。

その後、Decision 0074 で Main は Qwen3.8-27B-FP8 に決まり（Qwen3.6-27B-FP8 は次点）、paw-seed-v2（49 Task）で 2 Model を 3 回ずつ走らせた結果がある。
この Decision は、0041 の 8 のうち決まっていない「どの Task のどの Patch を」「何を数え」「どう記録するか」を決める。**Benchmark は走らせない**（測るのは人の作業で、Model の Run は 10-05〜10-06 の結果をそのまま使う）。

## 提案

### 1. 目的: Main の結果を直す人の手間の基準値を得る（Model の選択には使わない）

- Main は Decision 0074 で決まっており、この測定の結果で Main を変える提案はしない。目的は、Main の「惜しい」結果を人が引き取るとどれだけかかるかの基準値を得て、PAW-017 の受け入れ条件 3 を満たすこと。
- そのため、Main と次点の 2 Model を同じ Task で比べることはしない（下の 2）。

### 2. 対象: Qwen3.8-27B-FP8 の paw-seed-v2 の run1 の、部分点 0.9 以上で未解決の 4 Task

Qwen3.8-27B-FP8 が 3 回とも解けなかった Task は 12（v1 の Historical 11 Task のうち hist-08 以外の 10 と、v2 の spec-03・spec-12。下の表。部分点・行数・終わり方は `aggregate_198.json` から）。

| Task | 種類 / 難易度 | 部分点（run1 / run2 / run3） | Patch の行数（run1） | run1 の終わり方 | 対象 |
| --- | --- | --- | ---: | --- | --- |
| paw-seed-v2-spec-03-unittest-report | Spec / medium | 0.93 / 0.87 / 0.93 | 493 | submitted | **はい** |
| paw-seed-v2-spec-12-projection-readback | Spec / hard | 0.92 / 0.92 / 0.92 | 799 | max_steps | **はい** |
| paw-seed-v1-hist-05-worktree-unreaped-child | Historical / hard | 0.96 / 0.96 / 0.96 | 238 | submitted | **はい** |
| paw-seed-v1-hist-11-memory-status-history | Historical / hard | 0.97 / 0.00 / 0.00 | 102 | max_steps | **はい** |
| paw-seed-v1-hist-07-memory-worker-benchmark | Historical / hard | 0.83 / 0.90 / 0.84 | 2001 | max_steps | いいえ |
| paw-seed-v1-hist-04-candidate-adapter | Historical / hard | 0.64 / 0.05 / 0.00 | 1316 | max_steps | いいえ |
| paw-seed-v1-hist-03-metrics-collector | Historical / medium | 0.52 / 0.52 / 0.52 | 806 | max_steps | いいえ |
| hist-01・02・06・09・10 | Historical | 0.00〜0.36 | 436〜1546 | | いいえ |

- 選び方: **run1 の部分点（acceptance の test case の通過率）が 0.9 以上**の Task。部分点が低い Task は、人が直すというより作り直すことになり、60 分の打ち切りに当たるだけで差を測れない。v1 の Historical の多くは、Hidden test が Issue に書かれていない内部の名前を確かめることが未解決の原因で（Decision 0074 の 1）、直す時間は「Hidden test の詳細を当てる時間」になる。
- hist-07 は部分点 0.83〜0.90 だが Patch が 2,001 行で、読むだけで打ち切りに近い。外す。
- **Patch は run1 に固定する**（Task ごとに一番良い Run を選ばない。選ぶと「惜しい」結果に偏る）。hist-11 は run2・run3 が空の Patch（0.00）で run1 だけが 0.97。run1 に固定した結果としてそのまま使う。
- 4 Task で、人の時間は最大 4 時間（打ち切り 60 分 × 4）。1 回に 1 Task とし、続けて測らない（疲れの影響を避ける）。
- **次点（Qwen3.6-27B-FP8）は測らない**。同じ Task を 2 回直すと 2 回目は答えを知っていて速くなる（学習の効果）。別の Task で測ると Task の差と Model の差が分けられない。0041 の 8 の「上位 2 候補の未解決 Task から」は、候補の集合を 2 Model に限る意味として読み、この Decision では Main だけから選ぶ（判断点 2）。
- Resolved の Task（解けたが Review で差し戻すもの。0041 の 8 の定義に含まれる）は、今回は測らない（判断点 5）。

### 3. 数えるもの・数えないもの

| | 数える | 数えない |
| --- | :---: | :---: |
| Issue の本文と候補の Patch を読む・理解する | ○ | |
| 調べる（コードを読む、visible check や手元の test を走らせる） | ○ | |
| 直す（編集・test の追加） | ○ | |
| Evaluator の再実行（下の 4）の待ち時間 | ○（0041 の 8） | |
| 作業の準備（clone・Patch の適用・この CLI の操作） | | ○（`start` の前に済ませる） |
| 休憩・中断（`pause` から `resume` まで） | | ○ |
| 結果の記録・報告を書く時間 | | ○（`finish` の後） |

- 開始（`start`）: 準備を終え、人が Issue と Patch を読み始めるとき。
- 終了（`finish`）: Evaluator の再実行で Hidden check（acceptance と regression）がすべて `passed` になり、人が「この変更を受け入れる（自分の Repository なら merge してよい）」と判断したとき。
- 断念（`abandon`）: 60 分より前に人が諦めたとき。打ち切り（60 分）と同じく未解決として扱う（下の 5）。
- 人が見てよいもの: Issue の本文（Task file の `issue_text`）、候補の Patch と Trace、visible check、Evaluator の再実行の結果（check ごとの `passed` / `failed`、acceptance の test case の通過率、落ちた test の名前と assertion の message）。**Hidden test の source と Golden patch は見ない**（実際の使い方では、人は CI の失敗を見るが「正解の Patch」は持っていない。Hidden test を読むと、直す時間ではなく写す時間になる）（判断点 3）。
- 人の道具: 人の手と普段の Editor / Shell。**Agent（Codex / Claude / Local）に直させない**（直す時間は「Model の結果を人が引き取る」手間を測るため。Agent を使うなら別の測定にする）。

### 4. 手順（Repository の外で行う）

1. 準備（時間に数えない）: 作業 Directory（例: `/data/results/paw-hct-<日付>/work/<task>`）に、Task の非公開の Repository（`/data/datasets/<dataset>/repos/<task>`。`<dataset>` は索引の entry の `dataset`: hist は `paw-seed-v1`、spec は `paw-seed-v2`）を clone し、`/data/results/paw-bench-2026-10-05/coding/Qwen3.8-27B-FP8/run1/<task>/candidate.patch` を `git apply` する。
2. `python -m benchmarks.human_correction --log /data/results/paw-hct-<日付>/events.jsonl start --task <task> --model Qwen3.8-27B-FP8 --run run1`
3. 直す。休むときは `pause`、戻ったら `resume`。経過は `status` で見る（60 分を超えると `cap_reached` が出る）。
4. Evaluator の再実行: 作業 Directory の `git diff`（starting commit から）を Patch にし、10-05 の比較 Run と同じ Evaluator（`/data/results/paw-bench-2026-10-05/_code/coding_harness.py eval-patch --task <task> --patch <patch> --out <dir>`）で評価する。Hidden check が `passed` でなければ 3 に戻る。
5. Hidden check がすべて `passed` で、人が受け入れるなら `finish`。諦めるなら `abandon`。
6. 4 Task が終わったら `summary --output /data/results/paw-hct-<日付>/summary.json`。結果は `docs/benchmarks/` の報告（別の PR）にまとめる。

Log・作業 Directory・Evaluator の結果は Repository の外（`/data/results`）に置く（Hidden check の結果を含むため。Decision 0041 の 4）。

### 5. 記録の形式と値の決め方（この PR で `benchmarks/human_correction.py` を実装）

- **Event log**（JSON Lines、追記のみ、`0600`）: 1 行 1 Event で、`v`・`event`（`start` / `pause` / `resume` / `finish` / `abandon` / `unchanged`）・`session`（`<model>/<task>/<run>`）・`at`（UTC、ミリ秒）と、`start` / `unchanged` だけ `task_id`・`model`・`run`。**Code・test の出力・メモは書かない**（Hidden check の内容を Log に漏らさないため）。
- CLI は Log に排他 lock を掛けたまま Log 全体と新しい Event を読み直して検査・追記し（既存の file も `0600` にし、symlink と他の User の file は拒否する）、あり得ない遷移（`running` でないのに `pause`、閉じた Session への Event、同じ Session の 2 回目の `start`、時刻の逆行）は書かずに終了 code 1 にする。
- **作業時間**: `start` から `finish` / `abandon` までのうち、`pause`〜`resume` を除いた時間。`pause` 中に `finish` したら、`pause` の時刻で終わる。
- **値**（`human_correction_ms`）:

| 結果（`outcome`） | 条件 | `human_correction_ms` | 人が解決した |
| --- | --- | --- | :---: |
| `accepted` | `finish`、作業時間 60 分以下 | 作業時間 | はい |
| `unchanged` | 直さずに受け入れた | `0` | はい |
| `capped` | 作業時間が 60 分を超えた（`finish` でも `abandon` でも） | 3,600,000（打ち切りの値） | いいえ |
| `abandoned` | 60 分より前に `abandon` | 3,600,000（打ち切りの値） | いいえ |
| （開いたまま） | `finish` / `abandon` していない | 省略 | |

- 未解決（`capped` / `abandoned`）を打ち切りの値で記録するのは、0041 の 8 の「60 分で打ち切り、打ち切ったものは未解決」を、値としては「60 分以内には直らなかった」に揃えるため（実際の作業時間は `active_ms` に残す）。集計では平均を出さず、Task ごとの値・中央値・解決した数を示す（打ち切りを含む平均は下限でしかない）（判断点 4）。
- **Result への書き込み**: `apply --session <id> --result <file>` は、Evaluator Result（schema v1）の `metrics.human_correction_ms` に値を入れ、`task_id` と `candidate.model` が Session と一致し、書いた後も schema v1 で valid であることを確かめる。ただし 10-05 の比較 Run の Harness（Repository の外）の `result.json` は schema v1 ではないため、今回の測定の記録は Event log と `summary.json` とし、`apply` は schema v1 の Result を出す Evaluator ができたときに使う（判断点 4）。
- 時刻は OS の時計（UTC）で、CLI の呼び出しの間に時計が戻ると次の Event を拒否する。

## 代替案

- **Main と次点の両方を同じ Task で測る**（0041 の 8 の文字どおりの読み方）: 学習の効果で 2 回目が速くなる。Task の半分ずつで順番を入れ替えても、4 Task では偏りを消せない。Main は決まっており、比較の意味も小さい。
- **未解決の 12 Task すべてを測る**: 人の時間が最大 12 時間で、部分点の低い 8 Task はほぼ打ち切りになると見込まれ、得る情報が少ない。
- **Task ごとに一番良い Run の Patch を選ぶ**: 「惜しい」結果に偏る。run1 に固定する。
- **Hidden test を人に見せる**: 速く確実に終わるが、正解の仕様を写す時間になり、実際の使い方と違う。
- **Git の commit 時刻や Editor の記録から自動で測る**: 休憩や考える時間を区別できない。0041 の 8 の明示的な stopwatch のままにする。
- **未解決を省略する、または実際の作業時間で記録する**: 省略は「試みなかった」と区別できない（0041 の 8）。実際の作業時間は「早く諦めたほど短い」値になり、解決した Task と並べると誤解を招く。

## リスク

- 4 Task・1 人・1 回で、値の揺れは大きい。基準値（目安）としてだけ使い、Model の比較には使わない。
- 測る人（Repository の Owner）は、Task の元になった機能を自分で作った（Historical）、または Issue を書いた。初めての人より速い可能性がある。
- paw-seed-v2 の新しい Task の Hidden test は Agent が書いた（Decision 0074 のリスク）。人が受け入れる変更でも Hidden check が落ちる場合は、`abandon` にせず、その理由を報告に書いて Human が扱いを決める（この Decision では決めない）。
- Evaluator の再実行は 10-05 の Harness（Repository の外、`_code`）に依存する。Repository の中に schema v1 の Result を出す Evaluator ができるまで、手順の 4 は Repository の外の Script を使う。

## 決めてほしいこと

1. **目的を Main の基準値を得ることとし、結果で Main を変えない**（提案の 1）か。推奨: はい。
2. **対象を Qwen3.8-27B-FP8 の run1 の、部分点 0.9 以上で未解決の 4 Task（spec-03・spec-12・hist-05・hist-11）とし、次点の Qwen3.6-27B-FP8 は測らない**（提案の 2）か。推奨: はい。0041 の 8 の「上位 2 候補の未解決 Task から人が選ぶ」は、候補を 2 Model に限る意味と読み、学習の効果を避けるため Main だけにする。別の案: hist-07 を加えた 5 Task（最大 5 時間）、または次点も同じ 4 Task で測る（Task ごとに順番を入れ替える）。
3. **人が見てよいもの**（提案の 3）: Issue・候補の Patch と Trace・visible check・Evaluator の再実行の結果（落ちた test の名前と assertion の message を含む）は見てよく、Hidden test の source と Golden patch は見ない。Agent に直させない。推奨: はい。
4. **記録の形式と値**（提案の 5）: Event log（JSON Lines）と `summary.json` を記録とし、未解決（打ち切り・断念）は `human_correction_ms` を 3,600,000 として「人が解決した: いいえ」を付け、集計は平均でなく Task ごとの値・中央値・解決した数で示す。schema v1 の Result への書き込み（`apply`）は、schema v1 の Result を出す Evaluator ができたときに使う。推奨: はい。
5. **Resolved の Task の Review（解けたが差し戻すもの）を今回は測らない**か。推奨: はい（今回は未解決の Patch を直す時間だけ。Review の時間は、PR の Review の流れ（Claude / Codex の Review）ができてから別に測る）。

## 承認後の扱い

承認されたら `Approval` に記録し、Status を Approved に改める。Human が 4 の手順で測り、結果を `docs/benchmarks/` の報告と MODEL_CANDIDATES.md の「残っていること」の更新（別の PR）にまとめて #181 を閉じる。
方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。

## 承認時の決定（2026-10-08）

Human は、作業 Session で判断が必要な点について推奨つきの説明を受け、直接回答して承認した（すべての判断点を推奨どおり承認）。
