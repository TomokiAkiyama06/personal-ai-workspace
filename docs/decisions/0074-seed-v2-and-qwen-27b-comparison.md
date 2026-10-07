# paw-seed-v2 の構成と、Qwen3.6-27B-FP8 / Qwen3.8-27B-FP8 の比較の結果の扱い

- Status: Proposed
- Date: 2026-10-05
- Scope: Issue [#198](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/198)（PAW-017 の後続）。根拠は [paw-seed-v2 の比較 Run の報告](../benchmarks/paw-017-seed-v2-2026-10.md)。[Decision 0073](0073-main-coexistence-confirmation.md) で保留になった 1（Main の採用の確定と、共存の確認 Run の採用条件の読み方）もここで決める（下の 5）
- Supersedes: なし。[Decision 0041](0041-seed-benchmark-dataset.md) の 2（v2 は新しい版として作り、v1 の Task は書き換えない）に従う。[Decision 0040](0040-main-coding-model-selection.md) の 1（Main は Qwen3.6-27B-FP8）は、下の 4 の結果（Qwen3.6-27B-FP8 のまま）により変えない

## 背景

#180 の確認 Run で、Qwen3.6-27B-FP8 と Qwen3.8-27B-FP8 は 3 回とも paw-seed-v1 の 24 Task 中 14 Task を解き、解けた Task も同じだった。
v1 は Historical の 11 Task のうち 10 Task をどの Model も解けず、Injected Bug の 7 Task は 11 Model が全問を解いたため、上位の Model の差を測れない。

Human は 2026-10-05、作業 Session で次を決めた（この Decision の 3・4 はその記録で、提案ではない）。

1. 差が出る問題を足して、約 50 Task の `paw-seed-v2` を作る（v1 の 24 Task は書き換えずに含める）。
2. 精度の比較は、Coding Model だけを GPU に置き（Memory Worker・Embedding・Reranker を置かない）、2 つの Model を同じ設定で 3 回ずつ走らせる。
3. 選び方: Resolved@3 と 3 回の平均 Resolved の**両方**で Qwen3.8-27B-FP8 が Qwen3.6-27B-FP8 より **2 Task 以上**多ければ Qwen3.8-27B-FP8 にする。そうでなければ Qwen3.6-27B-FP8 のまま。時間は条件にせず、記録する。
4. 小さい Model は、測る結果に影響しない部分（予備の Run）だけ並列に動かしてよい。比較の 3 + 3 回は GPU を単独で使う。

## 提案

### 1. paw-seed-v2 の構成

| 出典 | 種類 | 数 | 難易度 |
| --- | --- | ---: | --- |
| paw-seed-v1（そのまま） | Historical 11 / Spec 6 / Injected Bug 7 | 24 | easy 4 / medium 12 / hard 8 |
| v2 で追加（base `3df6f80`） | Spec 20 / Injected Bug 5 | 25 | medium 10 / hard 15 |
| 計 | | **49** | easy 4 / medium 22 / hard 23 |

Human の目安（約 50）より 1 少ない。予備の Run で外した後に 3 回目の Task 作りはしなかった（下の 2）。

- v1 の 24 Task は、索引の entry に `"dataset": "paw-seed-v1"` を付けて**そのまま**載せる。Task file・Hidden check・非公開の Repository は v1 のもの（`benchmarks/seed-tasks/paw-seed-v1`、`/data/datasets/paw-seed-v1`）を使い、`python -m benchmarks.seed_dataset --dataset paw-seed-v2 check` が v1 の索引の entry と同じであることを確かめる。
- 新しい Task は Spec と Injected Bug だけ（Historical は v1 で 11 Task 中 10 Task がどの Model にも解けず、原因の多くが「元の PR の test が、Issue に書かれていない内部の名前・message を確かめる」ことだったため）。base commit は `3df6f80`（2026-10-01 の `main`。候補の学習データの締め切りより後）。
- 新しい Task の作り方の規則: Hidden test が確かめることはすべて Issue の本文か Repository に文書化された振る舞いから読み取れること（Golden patch が Issue の自然な読み方であること）、PostgreSQL・Network・時間に依存する test を使わないこと、acceptance の test case を多くして部分点が広がるようにすること。Injected Bug では、Bug をそのまま示す既存の visible test の case を starting state から外し、Hidden check で元に戻す。
- 非公開の material（Hidden test・Golden patch・bug patch・Repository）は `/data/datasets/paw-seed-v2`（Repository の外、`0700`）に置く。保管・退避・混入・License の扱いは Decision 0041 の 4〜7 と同じ。
- Golden の確認: 新しい Task はすべて `seed_dataset --dataset paw-seed-v2 verify` で、starting state で acceptance が失敗し、Golden で acceptance・regression・visible check が成功することを確かめた（新しい 25 Task すべて `ok`。v1 の 24 Task は v1 の確認（Decision 0041 の 9）のまま）。
- 番号は作った時のまま。予備の Run で外した Task は欠番にする（下の 2）。

### 2. 差が出る Task の選び方（予備の Run）

比べる 2 Model の結果を Task の選択に使わないため、2 Model 以外の 3 Model で予備の Run を行い、規則を先に決めてから当てはめた。

- 予備の Run の Model: Qwen3.6-35B-A3B-FP8、Qwen3.5-27B-FP8、Devstral-Small-2-24B（09-28 の Run で v1 を 12 / 13 / 12 Task 解いた Model。比較の Harness・設定と同じで、各 1 回、新しい Task だけ）。
- 規則: (a) 3 Model がすべて解いた Task は外す（差が出ない）。(b) 3 Model とも解けず、部分点もほぼ 0 の Task は、Trace を見て仕様の曖昧さが原因なら外す。(c) Qwen3.6-27B-FP8 / Qwen3.8-27B-FP8 の結果は、選択にも、Issue の本文の手直しにも使わない。
- 1 回目（30 Task: Spec 13、Injected Bug 17）: 3 Model の Resolved は 20 / 25 / 17。2 つの欠陥を入れた Injected Bug は 17 Task 中 15 Task を 3 Model がすべて解き、Spec は 13 Task 中 1 Task だけだった。(a) で 16 Task を外し、14 Task を残した（(b) に当たる Task はなかった）。v1 の Injected Bug と同じく、場所が 1〜2 か所の Bug は上位の Model の差を測れない。
- 2 回目（1 回目の結果から作った、より難しい 16 Task: 3 つ以上の module にまたがる Spec 8、3〜4 つの欠陥を 2 つ以上の module に入れた Injected Bug 8）: 3 Model の Resolved は 8 / 11 / 6（16 Task 中）。Injected Bug 8 Task のうち 5 Task を 3 Model がすべて解き、(a) で外した。Spec 8 Task はすべて残った（3 Model とも解けなかった 5 Task（spec-14〜17・21）も、どれかの Model の部分点が 0.8 以上で、(b) に当たらない）。11 Task を残した。
- 外した Task: 1 回目 16 Task（bug-01・03〜13・15〜17、spec-13）、2 回目 5 Task（bug-18・21・23〜25）。番号は欠番。外した Task の Hidden check・Golden は `/data/datasets/paw-seed-v2/authoring` に残し、比較には使わない。
- 結果として、新しい Task は Spec が多い（25 Task 中 20）。欠陥が多くても場所が読み取れる Bug は、予備の Run の 3 Model でも解けるため。

### 3. 比較 Run の条件（Human の決定、2026-10-05）

- Coding Model だけを GPU に置き、ほかの Process が GPU にないことを各 Run の開始前に確かめた（[Decision 0039](0039-compute-scheduler-calibration.md) の 4 の安全の設定: `MAX_JOBS=4`、`FLASHINFER_NVCC_THREADS=1`、開始前の `MemAvailable` 32 GiB 以上、8 GiB を下回ったら自分の Process Group だけを止める）。
- vLLM、`--max-model-len 131072 --max-num-seqs 8 --gpu-memory-utilization 0.90`、Reasoning parser `qwen3`、Tool parser `qwen3_coder`、Sampling は `generation_config` の既定。Harness・Prompt・Tool・Sandbox・Evaluator は 09-28 / 10-01 と同じ（4 Task 並列、60 step、Task ごとに 45 分）。
- 2 Model を 1 回ずつ交互に（3.6 → 3.8 → 3.6 → …）3 回ずつ、Run ごとに Server を起動し直して走らせる予定だった（時間帯・Host の負荷の偏りを 2 Model で揃えるため）。
- **予定から外れた点**: Run の Script（Repository の外）の Bug（Server を止める関数が global の loop の変数を使い、呼び出し側の Run 番号を上書きした）で、10-05 の 1 回目の通しでは Qwen3.8-27B-FP8 が run2 の 1 回だけ走った（16:55〜21:14）。変数を `local` にして再開し、Qwen3.8-27B-FP8 の run1 と run3 は 10-06 11:27〜20:02 に交互ではなく続けて走った。Dataset の固定した複製・設定・Harness は同じで、GPU にはこの Run の Process だけがあった。Qwen3.6-27B-FP8 の 3 回（10-05 15:22〜10-06 00:19）は影響を受けていない。詳細は報告の 2。

### 4. 結果と採用する Model

| Model | run1 / run2 / run3 | 平均 | Resolved@3 | 3 回とも解けた Task | Run の所要（平均） | 出力 token（Run の平均） | Context 不足 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Qwen3.6-27B-FP8 | 32 / 33 / 34 | 33.0 | 36 | 30 | 92 分 | 0.61 M | 0 |
| Qwen3.8-27B-FP8 | 37 / 37 / 37 | 37.0 | 37 | 37 | 257 分 | 1.70 M | 9 |

- 49 Task のうち解けた数。v1 の 24 Task の部分は 2 Model とも毎回 14 で、差はすべて新しい 25 Task で出た（Qwen3.6-27B-FP8 18 / 19 / 20、Qwen3.8-27B-FP8 23 / 23 / 23）。
- Resolved@3 の差は +1（Qwen3.8-27B-FP8 だけが解いた bug-14-health-outage-changes・spec-16-task-timeline-replay と、Qwen3.6-27B-FP8 だけが解いた spec-12-projection-readback）。平均の差は +4.0。
- **Human の選び方（3）を当てはめると、平均の差は 2 以上だが Resolved@3 の差が 1 で、両方ではない。したがって Main は Qwen3.6-27B-FP8 のまま**（Decision 0040 の 1 は変えない）。この Decision はこの規則の結果をそのまま記録し、規則を変える提案はしない。
- 事実として: Qwen3.8-27B-FP8 は 3 回とも同じ 37 Task を解き、Run の揺れがなかった（Qwen3.6-27B-FP8 は 32〜34）。一方で、Run の所要と出力 token はどちらも約 2.8 倍だった（生成速度は同じで、出す token が多い）。Context の上限は 2 Model で同じ（`--max-model-len 131072`、Harness の Prompt の上限 120,000 token、`max_tokens` 16,384）。Qwen3.8-27B-FP8 だけの Context 不足 9 回は、Reasoning を履歴に残す Harness で 1 step あたり約 3 倍（中央値 766 / 241 token）書く振る舞いから出たもので、8 回は 2 Model ともどの Run でも解けない v1 の Historical、1 回（spec-11）は解けていた。Context の上限はどの Resolved の数も変えていない。時間は規則の条件ではない。
- 部分点・難易度別・Task ごとの差・timeout は報告の 3。機械で読める集計は Server の `/data/results/paw-bench-2026-10-05/aggregate_198.json`。

### 5. Decision 0073 の 1（保留）の扱い

Decision 0073 の 1（共存の確認 Run の採用条件を「Resolved@3 と平均が単独と同じ範囲（1 Task 以内）」と読み、Qwen3.6-27B-FP8 の採用を確定する）は、この比較の結果で決めることになっていた。4 で Main は Qwen3.6-27B-FP8 のままとなったので、次を提案する。

- **Qwen3.6-27B-FP8 を Main Coding Model として採用を確定する**（Decision 0040 の 1。`MODEL_CANDIDATES.md` への追記は別の PR）。根拠: 10-01 の共存の Run（`gpu-memory-utilization` 0.61）は 14 / 13 / 14、Resolved@3 14 で、単独（14 / 14 / 14、Resolved@3 14）と 1 Task 以内。
- **採用条件の読み方は 0073 の 1 のとおり**「同じ Dataset の単独の Run と比べて、Resolved@3 と 3 回の平均がどちらも 1 Task 以内の低下」とする。1 回ごとの値では判定しない（Sampling は温度 0 でなく、単独でも 1 Run で 1〜2 Task 揺れる）。
- **0.51 の確認 Run（0073 の 2）は paw-seed-v1 の 24 Task で行い、10-01 の単独の Qwen3.6-27B-FP8（14 / 14 / 14、Resolved@3 14）を基準にする**。合格は Resolved@3 13 以上かつ平均 13.0 以上。v1 で Qwen3.6-27B-FP8 が解く 14 Task は単独では 6 回（10-01 の 3 回と今回の 3 回）すべてで解けており、揺れがないため、KV を減らしたことによる低下を見つけやすい。0.61 の結果とも直接比べられ、1 回が約 45 分で済む。

## 代替案

- **v1 の Historical を外した v2 にする**: 比較の可能性（Decision 0041 の 2）と、v1 の結果との比較のため、v1 の 24 Task はそのまま含める。v1 の部分と新しい部分の結果は報告で分けて示す。
- **候補の 2 Model の予備の Run で Task を選ぶ**: 差が大きく見える Task を選ぶことになり、結果が偏る。採らない（Human の指示）。
- **新しい Historical Task を足す**: v1 の Historical は仕様の曖昧さで解けないものが多かった。PR の test が Issue に書かれていない名前を確かめる限り同じことが起きるため、v2 では足さない。

## リスク

- 131k の設定では、Qwen3.8-27B-FP8 は長い Task で Context の余裕が小さい（Task ごとの Prompt の最大は p90 107k・最大 115k、Qwen3.6-27B-FP8 は 69k・85k）。Qwen3.8-27B-FP8 を将来採る場合は、古い Reasoning を履歴から落とす、または `max-model-len` を大きくすることを Context の大きさの設計で考える（この比較では試していない）。
- 新しい Task の作者は Agent（Claude）で、Golden・Hidden test も同じ作者が書いた。Issue の本文で読み取れない期待が残っている可能性がある（予備の Run で 3 Model が同じ test case だけを落とした Task は、本文に書かれていることを確かめて残した）。
- 予備の Run の 3 Model は候補より弱い。予備の Run で残した Task が、候補にとっても差が出るとは限らない。
- 1 回目の結果を見て 2 回目の Task を作ったため、Task の作り方自体は予備の Run の 3 Model に合わせている（候補の 2 Model には合わせていない）。

## 決めてほしいこと

1. **paw-seed-v2 の構成と Task の選び方（提案の 1・2）を承認する**か。v1 の 24 Task をそのまま含め、比べない 3 Model の予備の Run で 3 Model がすべて解いた Task を外した新しい 25 Task を足した 49 Task。推奨: はい（Golden の確認はすべて `ok`。比較の後は v2 の Task を書き換えない）。
2. **比較の結果に Human の選び方を当てはめ、Main を Qwen3.6-27B-FP8 のままにする**（提案の 4）か。Resolved@3 の差は +1（条件は 2 以上）、平均の差は +4.0 で、両方ではない。推奨: はい（規則どおり）。参考: Qwen3.8-27B-FP8 は 3 回とも 37 で揺れがなかったが、Run の所要と出力 token は約 2.8 倍。Run の順番が予定と違った（Qwen3.8-27B-FP8 の run1・run3 が 10-06 に続けて走った）ことは、結果として受け入れるか（推奨: はい。設定・Dataset・GPU の状態は同じで、3 回の値も同じ）。
3. **Decision 0073 の 1（保留）を提案の 5 のとおり決める**か: (a) Qwen3.6-27B-FP8 の Main への採用を確定する、(b) 共存の確認 Run の採用条件を「同じ Dataset の単独の Run と比べて Resolved@3 と平均がどちらも 1 Task 以内の低下」と読む、(c) 0.51 の確認 Run（0073 の 2）は paw-seed-v1 で行い、10-01 の単独（Resolved@3 14、平均 14.0）を基準にする（合格は Resolved@3 13 以上かつ平均 13.0 以上）。推奨: (a)〜(c) すべてはい。代わりに 0.51 の確認 Run を paw-seed-v2 で行う案もある（基準は今回の単独の Resolved@3 36・平均 33.0。差を測れる Task は多いが、単独でも 1 Run で 32〜34 と揺れるため 1 Task 以内の読み方では誤って不合格になりやすく、1 回が約 2 時間以上かかる）。

## 承認後の扱い

承認されたら `Approval` に記録し、Status を Approved に改める。3 が承認されたら、Decision 0073 の 1 はこの Decision の 5 で決まったものとして扱う（0073 は書き換えない）。0.51 の確認 Run と Deployment の設定は別の Issue で行う。
paw-seed-v2 を比較 Run に使った後は、v2 の Task を書き換えない（Decision 0041 の 2）。
