# paw-seed-v1 — Seed Benchmark Dataset

Personal AI Workspace 自身の Repository から作った、Coding Agent 比較用の初期 Task set です（PAW-016、[Decision 0041](../../../docs/decisions/0041-seed-benchmark-dataset.md)（Proposed））。

- [`manifest.json`](manifest.json): 索引。Task ごとの種類・難易度・カテゴリ・出典（元の PR / Issue / base commit）を記録します。
- `tasks/*.json`: [Task schema v1](../../schemas/task-v1.schema.json) の Task。Hidden check は opaque な `reference_id` だけを持ちます。

| 種類 | 数 | 出典 |
| --- | --- | --- |
| Historical | 11 | Merge 済みの PR（starting commit は merge commit の親、Golden test は PR の test） |
| Spec | 6 | Issue・docs に沿った未実装の小機能（base commit `eb236c7`） |
| Injected Bug | 7 | base commit に入れた bug（test 修正型 1 を含む） |

難易度は easy 4 / medium 12 / hard 8 です。カテゴリの内訳は `python -m benchmarks.seed_dataset check` が出します。

Hidden test・Golden patch・bug patch・候補に渡す Repository は、この Repository には**含めません**。
Evaluator の OS User だけが読める `/data/datasets/paw-seed-v1` に置きます（Decision 0041 の 4・5）。

```bash
# 索引と Task の検査（CI でも実行。非公開の material は不要）
python -m benchmarks.seed_dataset check
# 非公開の Repository と Historical の Golden patch を作る
python -m benchmarks.seed_dataset build --source-repo . --private-root /data/datasets/paw-seed-v1
# Golden behavior の確認（starting で失敗・Golden で成功）。PATH の先頭は Benchmark 用の仮想環境
PATH="$VENV/bin:$PATH" python -m benchmarks.seed_dataset verify \
  --private-root /data/datasets/paw-seed-v1 --work-dir "$WORK" \
  --postgres-image pgvector/pgvector:pg18
```

`--postgres-image` は pgvector 入りの PostgreSQL の Image です（Backend の Task で、check ごとに使い捨ての Container を起こして test にその Superuser の URL を渡し、終わったら Container ごと消します。Docker が要ります）。URL は出力しません。
`--work-dir` は Repository の外の、Evaluator だけが使う directory にします。
