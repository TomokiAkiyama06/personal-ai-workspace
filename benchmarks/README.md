# Benchmark の準備領域

Benchmark Task の仕様、候補 Agent の比較、公開可能な評価用データを整備するためのディレクトリです。
[Task schema](schemas/task-v1.schema.json) とvalidatorはTaskの形式だけを検証し、Repository取得、
check実行、Hidden Test参照の解決、隔離実行は行いません。

実装順序は [Benchmark / Evaluator 設計](../docs/BENCHMARK_EVALUATOR.md) と
[Implementation Backlog](../docs/IMPLEMENTATION_BACKLOG.md) に従います。

- [PAW-010 — Benchmark Task Schema](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/7)：Task の機械可読な形式を定義する。PAW-001 に依存する。
- [PAW-011 — Evaluator Result Schema](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/8)：Evaluator の判定結果と計測値の形式を定義する。
- [PAW-012 — 隔離 Worktree Runner](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/9)：同一の開始状態から候補 Agent を実行する。
- [PAW-013 — Test / Hidden Acceptance Runner](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/10)：Visible TestとHidden Testを分離して実行する。
- [PAW-015 — Candidate Adapter Interface](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/12)：モデルや Runtime の違いを共通 Interface で扱う。
- [PAW-016 — Seed Benchmark Dataset](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/13)：仕様と検証可能な挙動に基づく初期評価セットを作る。

機械的な判定と結果の扱いについては [Evaluator](../evaluator/README.md) を参照してください。

実データを追加する際は [Security Policy](../SECURITY.md) に従います。
Private Dataset、実際の会話・Memory、Credential、Model Weights はこの Public Repository に保存しません。

## Task schema v1

Task JSONの必須fieldは次のとおりです。

| Field | 内容 | Candidateへの可視性 |
| --- | --- | --- |
| `schema_version` | Schema version。v1は`1.0` | 可視 |
| `task_id` | Taskを識別するID | 可視 |
| `kind` | `historical`、`spec`、`injected_bug`のいずれか | 可視 |
| `repository` | Credentialを含まないRepository locatorとstarting commit。known-good commitは任意 | 可視 |
| `issue_text` | Candidateへ与える課題 | 可視 |
| `visible_checks` | Candidateへ公開するcheckのID、種別、argv形式のcommand | 可視 |
| `hidden_checks` | Hidden checkのID、種別、opaqueな`reference_id`だけ | Evaluator metadata |

`visible_checks`と`hidden_checks`は空配列を許容します。Hidden Test本文、command、path、Credentialを
`hidden_checks`へ保存してはいけません。`reference_id`の保存先や解決方法、Candidateからの隔離方法は
PAW-013で定義します。Schema validationはlocatorやcommitの存在確認、credentialの検出を行いません。
Task authorは[Security Policy](../SECURITY.md)に従い、credentialをlocatorへ保存してはいけません。

## Isolated worktree runner

`benchmarks.worktree_runner.WorktreeRunner` is evaluator infrastructure for starting a
candidate process from a specified commit.  Each `create()` call resolves the commit,
creates a detached worktree below an evaluator-owned runs directory, and retains a
JSONL lifecycle log outside the worktree.  `execute()` removes that worktree after a
normal exit, timeout, or cancellation; its log remains available for audit.

The runner does not execute visible or hidden checks and does not select a model.  It
does not persist command text, stdout, or stderr because those fields can contain
credentials.  It records only lifecycle events, exit status, duration, and byte counts.
PAW-013 owns check execution and hidden-test isolation.

## Validator

standalone CLIはprojectの安定したvirtual environmentへCI依存を導入して実行します。
pre-commitのhook環境は別環境であり、standalone CLIからは利用しません。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r .github/requirements-ci.txt
.venv/bin/python -m benchmarks.validate_task benchmarks/tests/fixtures/task-schema/valid/spec.json
```

複数fileを一度に指定できます。すべてvalidなら終了code 0、入力が不正なら1、bundled schemaを
利用できない場合は2を返します。エラーはJSON pathと理由を表示し、拒否した入力値は表示しません。

## Retrieval benchmark

`benchmarks.retrieval_runner`はACL付きのDatasetに対してRetrieverを実行し、次の指標を算出します。
Recall@K、MRR、nDCG@K、Permission Leakage（必須要件は合計0）、
stale / superseded / Scope誤選択率、latency（mean / p50 / p95、nearest-rank）です。
順位指標の定義は`benchmarks.retrieval_metrics`にあり、Retrieverの返すidの重複は2件目以降を捨て、
Datasetにないidは権限外として数えます。Retrieverが例外を出したqueryは指標0の失敗queryとして続行します
（記録するのは例外の型だけです）。latencyは`retrieve`の呼び出しだけを計測します。
CPU時間は、`retrieve`の呼び出し中にこのprocessが使ったCPU時間（`cpu_ms`、平均は`cpu_ms_mean`、合計は`cpu_ms_total`）です。
Retrieverがin-processで動く場合だけ含まれ、外部のServiceが使うCPU時間は見えません。GPU・VRAMは、`--collect-resources`で
`MetricsCollector`を使うと、wall clockとVRAM・GPU utilizationのpeakが`resources`に入ります（付けない場合は含みません）。
`retrieve`は文字列のsequence（list・tuple）を返す必要があり、単一のid文字列（`"mem1"`）などは不正な戻り値として終了code 2にします。
返されたidは、重複を除いた上位`k`件だけを採点します（`k`を超える分を末尾に足しても、どの指標も上がりません）。
Datasetのrelevantなmemoryは、active・fresh・queryと同じScopeである必要があります。そうでないと、正解をそのまま返しても
stale / superseded / Scope誤選択率が0にならず、指標が矛盾するためDataset不備として拒否します。
失敗したqueryの数は`failed_queries`に出ます。Retrieverが`retrieve(query_text, principals, k)`を
呼べない場合（メソッドがない、引数が合わない）は、全queryが失敗した報告にせず、実行前にエラーにします。
stale / superseded / Scope誤選択率の分母は、`k`ではなく、実際に返した上位k件以内の件数です
（返却数が少ないRetrieverは、少ない件数の中での混入率になります。候補間で返却数が違う場合は`k`を揃えて比較してください）。
Datasetの未知のfield（綴り誤り）は、黙って無視せず不備（終了code 1）として拒否します。`retrieve`が返すidは、`k`で切る前に全件が文字列であることを検証します。
memoryの`status`は、要件で定義された`active`・`superseded`・`deprecated`・`history`です（`active`以外は、返すと誤選択として数えます）。
Datasetの`fresh`は真偽値、`acl`・`requester_principals`・`relevant_ids`は文字列のlistで、
違う型は暗黙に変換せずDataset不備として扱います。

```bash
python -m benchmarks.run_retrieval_benchmark \
  --dataset benchmarks/tests/fixtures/retrieval/valid_dataset.json \
  --retriever benchmarks.tests.fixture_retrievers:make_retriever \
  -k 5 --output report.json
```

`--retriever`は`module:factory`で、引数なしのfactoryが
`retrieve(query_text, requester_principals, k) -> ids`を持つobjectを返します。
importしたmoduleは呼び出し元の権限で実行されるため、信頼できるcodeだけを指定してください。
Reportには文章、ACL、requester principal、例外messageを含めません。終了codeは、成功が0、Datasetの不備が1、
Retrieverの指定・戻り値やReport出力の不備、`-k`の指定誤りが2です。
Datasetの正式な形式はSeed Benchmark Dataset（PAW-016）で確定するため、現在の形式は暫定です。

## Evaluator Result schema v1

Result JSONは、Evaluator version、Task ID、Candidateのmodel/runtime/quantization、`FAIL_TO_PASS`と
`PASS_TO_PASS`、各Evaluator checkの結果、計測値を保存します。checkの種別は`build`、`syntax`、`unit`、
`integration`、`lint`、`type`、`regression`、`acceptance`、`security`、`forbidden_changes`です。statusは
`passed`、`failed`、`not_run`、`error`で表します。`check_results[].id`は対応するTaskの
`visible_checks`または`hidden_checks`の`id`を使い、Result内で重複してはいけません。

`metrics`では、取得できた場合に次を保存できます。durationの単位はミリ秒、VRAMはbytesです。

| Field | 内容 |
| --- | --- |
| `wall_clock_ms` | 実行時間 |
| `token_count` | token数 |
| `context_tokens` | Runtimeが報告したcontext token数 |
| `agent_steps` | Agent step数 |
| `retries` | Retry回数 |
| `tool_calls` | Tool呼び出し回数 |
| `diff_size` | 変更行数 |
| `tool_failures` | Tool失敗回数 |
| `peak_vram_bytes` | Peak VRAM |
| `peak_gpu_utilization_percent` | Peak GPU utilization（%） |
| `human_correction_ms` | 人間による修正時間 |

Runtimeで取得できないmetricは、`metrics`から省略できます。これは計測不能と0を区別するためです。
Result schema validatorもTask schema validatorと同じ終了code・値を出力しないエラー方針を使います。
量子化をしないCandidateは`quantization`へ`none`を、Runtime側で詳細を公開しないCandidateは
`provider-managed`を記録します。

## Metrics collector

`benchmarks.metrics_collector.MetricsCollector` はCandidate adapterからstep、retry、tool call、
Runtimeが報告する累積token/context usageを受け取り、Result schemaにそのまま入れられる
`metrics` objectへ正規化します。CollectorはProvider接続やCredentialを扱いません。

GPU telemetryが必要な場合は `NvidiaSmiGpuSampler` を注入します。これは`nvidia-smi`を使って
全GPUの使用VRAM合計と各GPU utilizationの最大値を周期的に観測します。GPUがない、または
`nvidia-smi`が利用できない環境では、GPU metricは省略されます。

```bash
.venv/bin/python -m benchmarks.validate_result benchmarks/tests/fixtures/result-schema/valid/complete.json
```
