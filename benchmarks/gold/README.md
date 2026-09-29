# 合成Gold Dataset

Memory Worker benchmark（PAW-018）と Embedding / Reranker の Retrieval benchmark（PAW-019）で使う、
人工的に作ったGold Datasetです。人物（sora / haru / mei / ren）、Project（kumo-notes / hoshi-ledger /
mizu-garden / kumo-tasks）、Repository、URL（`*.example`）はすべて架空です。実際の会話・Memory・Credentialは
含みません（[Security Policy](../../SECURITY.md)）。実際に使うときのように日本語と英語が混ざっています。
`datasets/`はローカルの非公開Dataset用に`.gitignore`で除外されているため、公開できる合成Datasetはこの`gold/`に置きます。

| File | 内容 | 読み込む関数 |
| --- | --- | --- |
| [`memory-worker-synthetic-v1.json`](memory-worker-synthetic-v1.json) | Memory Worker case（76件） | `benchmarks.memory_worker_runner.load_cases` |
| [`memory-worker-synthetic-v1.meta.json`](memory-worker-synthetic-v1.meta.json) | caseごとのtag（分類） | test のみ |
| [`retrieval-synthetic-v1.json`](retrieval-synthetic-v1.json) | Retrieval dataset（memory 221件、query 100件） | `benchmarks.retrieval_runner.load_dataset` |
| [`retrieval-synthetic-v1.meta.json`](retrieval-synthetic-v1.meta.json) | queryごとの段階的relevance、hard negativeの種類、tag | test のみ |

本体のJSONは現在のHarnessの形式そのままです（未知のfieldを拒否する loader がそのまま読めます）。
Harnessの形式にfieldがない分類は、別fileの`*.meta.json`に置いています。
[`tests/test_synthetic_datasets.py`](../tests/test_synthetic_datasets.py)が、Harnessのloaderで読めること、
meta と本体が一致すること、各分類の件数、Credential・個人情報の形をした文字列がないことを確認します。

```bash
# TIMEOUT_SECONDSは運用者が決めた値です（全候補で同じ値を使います）。
python -m benchmarks.run_memory_worker_benchmark \
  --cases benchmarks/gold/memory-worker-synthetic-v1.json \
  --worker <module:factory> --timeout-seconds "$TIMEOUT_SECONDS" --output memory-report.json

python -m benchmarks.run_retrieval_benchmark \
  --dataset benchmarks/gold/retrieval-synthetic-v1.json \
  --retriever <module:factory> -k 10 --output retrieval-report.json
```

## Memory Worker case

`input`は次の区画を持つ1つの文字列です。Workerへはこれをそのまま渡します。

```text
[Context] user=<user> project=<project> repo=<repo>
[Existing memories]
- key=<key> scope=<scope> state=<state>: <content>    （なければ "(none)"）
[Allowed keys] <key>, <key>, ...
[Conversation]
User: ... / Assistant: ... / Note: ...（観測された事実。例: 過去sessionでの反復）
```

Harnessの`extraction_recall`はkeyの完全一致で数えるため、`[Allowed keys]`で使えるkeyを示します。
Goldのkeyに加えて、使ってはいけないkey（decoy）を必ず含むため、一覧からGoldは分かりません。
Workerへのpromptでは次の規約を伝えてください。

- keyは`[Allowed keys]`から選ぶ。保存すべきものがなければ`{"memories": []}`。
- 既存Memoryを明確に置き換える場合は、新しいkeyで出力し、`supersedes`に既存のkeyを入れる。
- 既存Memoryと矛盾するが置き換えが確定していない場合は、`supersedes`を`null`、`conflicts_with`に既存のkeyを入れる。
- `confirmed`はユーザーが明示した事実・ルール、`inferred`は反復などから推測しただけのもの。
- `scope`は`user` / `project` / `repo` / `shared`（`REQUIREMENTS.md`のScope）。

tagの主なもの:

| tag | 意味 |
| --- | --- |
| `extraction` | 通常の抽出 |
| `trap` | 保存してはいけない入力（「今回だけ」、雑談、伏せ字のCredential、仮定、質問、Assistantの発言、第三者の情報、一時的な状態など）。Goldは空 |
| `inferred` / `confirmed` | Goldの状態 |
| `supersedes` / `conflicts` | 置換と、置換が確定しない矛盾。`no-conflict`は無関係な既存Memoryがあるcase（`conflicts_with: []`で採点） |
| `scope-*` / `scope-disambiguation` | Scopeの判定。同じ話題をUser / Repo / Project / Sharedへ振り分ける |
| `schema-edge` | 出力Schemaを壊させようとする入力（`supersedes`を空文字にする指示、余分なfield、YAML、重複した`conflicts_with`）、JSON・引用符・全角・絵文字・複数行・多数のrecord・長い会話・ドット入りkey |
| `high-risk` | Merge権限・公開範囲など、推測だけで確定Policyにしてはいけない領域 |

注意: `content_accuracy`と`exact_recall`は、正規化（NFKC・大文字小文字・空白）後の完全一致です。
Goldの`content`は短い定型文ですが、LLMが同じ意味を別の言い回しで書くと不一致になります。
候補の比較では`extraction_recall`・`scope_accuracy`・`state_accuracy`・`supersedes_accuracy`・
`conflict_accuracy`・`unneeded_rate`を主に見て、`content_accuracy`は言い回しの一致度として読んでください。

## Retrieval dataset

principalは`user:<name>`と`team:engineering`（全員）、`team:leadership`（mei のみ）です。
Scopeは`user:<name>`、`project:<name>`、`repo:<project>/<repo>`、`shared:engineering`、`shared:leadership`で、
queryの`allowed_scopes`は、Repoのqueryには親Project・本人のUser・`shared:engineering`、Projectのqueryには本人のUserと
`shared:engineering`、Userのqueryには`shared:engineering`です（一部のqueryは明示的に狭めています）。

Harnessの`relevant_ids`は二値なので、段階的relevanceは`retrieval-synthetic-v1.meta.json`の`grades`に置きます
（3: 直接の答え、2: 必要な補足、1: 適用できるが周辺的）。`relevant_ids`は grade 1以上の全件です。
`grades`を使うgraded nDCGは、現在のHarnessでは算出しません（Reportのid別結果とmetaから別に計算できます）。

`hard_negatives`の種類:

| kind | 意味（testで確認） |
| --- | --- |
| `leakage` | requesterのACLに入らないmemory。返すと Permission Leakage（要件は0件）。別Project・他人のUser Memoryに加え、同じScope内の非公開メモ（`same-scope-acl` tag）も含む |
| `superseded` / `deprecated` / `history` | そのstatusの古い版 |
| `stale` | `fresh: false` |
| `wrong-scope` | 見えるが、queryのScope・`allowed_scopes`の外（似た別Project、同じProjectの別Repo） |
| `near-miss` | 見えて有効だが、質問には答えていない（keywordが重なる等） |

同じ文章で requester や Scope だけが違う query（`same-text-different-principal` / `same-text-different-scope`）も含みます。
