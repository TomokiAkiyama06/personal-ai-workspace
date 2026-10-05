# paw-seed-v2 — Seed Benchmark Dataset（第 2 版）

paw-seed-v1 では上位の Model の差を測れなかった（Historical の 11 Task のうち 10 Task をどの Model も解けず、Injected Bug の 7 Task は 11 Model が全問を解いた）ため、
差が出る中〜難の Task を足した第 2 版です（Issue #198、[Decision 0074](../../../docs/decisions/0074-seed-v2-and-qwen-27b-comparison.md)（Proposed）。版の規則は [Decision 0041](../../../docs/decisions/0041-seed-benchmark-dataset.md) の 2）。

- [`manifest.json`](manifest.json): 索引。`"dataset": "paw-seed-v1"` の entry は v1 の Task を**書き換えずに**載せたもので、Task file・Hidden check・非公開の material は v1 のもの（`../paw-seed-v1/tasks`、`/data/datasets/paw-seed-v1`）を使います。entry は v1 の索引と同じでなければなりません（`check` が確かめます）。
- `tasks/*.json`: v2 で足した Task（[Task schema v1](../../schemas/task-v1.schema.json)）。base commit は `3df6f80`（2026-10-01 の `main`）。

| 出典 | 種類 | 数 | 難易度 |
| --- | --- | ---: | --- |
| paw-seed-v1（そのまま） | Historical / Spec / Injected Bug | 11 / 6 / 7（計 24） | easy 4 / medium 12 / hard 8 |
| v2 で追加 | Spec | 20 | medium 9 / hard 11 |
| v2 で追加 | Injected Bug（2〜4 の欠陥、複数の module） | 5 | medium 1 / hard 4 |
| 計 | | 49 | easy 4 / medium 22 / hard 23 |

v2 で追加した 25 Task は、比べる 2 Model 以外の 3 Model の予備の Run で、3 Model がすべて解いた Task を外して残したものです（作った 46 Task のうち 21 Task を外しました。Decision 0074 の 2）。

Task の番号は作った時の番号のままです。予備の Run（Screening）で外した Task の番号は欠番にしています（理由は Decision 0074）。

Hidden test・Golden patch・bug patch・候補に渡す Repository は、この Repository には**含めません**。
Evaluator の OS User だけが読める `/data/datasets/paw-seed-v2` に置きます（Decision 0041 の 4・5）。

```bash
# 索引と Task の検査（CI でも実行。非公開の material は不要）
python -m benchmarks.seed_dataset --dataset paw-seed-v2 check
# v2 で足した Task の非公開の Repository を作る（v1 の Task は v1 の build が作ったもの）
python -m benchmarks.seed_dataset --dataset paw-seed-v2 build --source-repo .
# Golden behavior の確認（v1 の Task は /data/datasets/paw-seed-v1 を使う）
PATH="$VENV/bin:$PATH" python -m benchmarks.seed_dataset --dataset paw-seed-v2 verify \
  --work-dir "$WORK" --postgres-image pgvector/pgvector:pg18
```
