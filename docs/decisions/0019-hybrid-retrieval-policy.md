# Hybrid Retrieval の方針（暫定値と選択）

- Status: Proposed
- Date: 2026-09-25
- Scope: PAW-043（Hybrid Retrieval Pipeline）と、Retrieval を使う以降の Issue（Memory の保存・整理 PAW-041 / 042、Context の組み立て）
- Supersedes: なし
- Approval: 未承認

## 背景

[REQUIREMENTS.md](../../REQUIREMENTS.md) の「Memory Retrieval Pipeline」と [Memory Architecture](../../docs/MEMORY_ARCHITECTURE.md) の 12 節は、
Retrieval の**順序**を定めている（Permission / ACL、Metadata、Keyword + Vector、Rerank、重複・矛盾の整理、Top-N）。
「Vector Search だけで採用を決めない」「最終採用前に Scope、status、freshness、confirmed / inferred、importance を評価する」「Embedding と Reranker は Benchmark で決める」も定めている。
一方で、**具体的な数値、Score の組み立て、日本語の Keyword 検索、Audit の量、部品が失敗したときの扱い**は定めていない（Context Budget は `[BENCHMARK]`）。

PAW-043 の実装は、動かすためにこれらを暫定値と選択で置いた。
[AGENTS.md](../../AGENTS.md) の「仕様変更」は、重要判断を記録して人間 / Admin の承認を得ると定める。
そこで、実装が置いた値と選択を一覧にし、Human が承認または変更できるようにした。
要件が決めていること（順序、`active` だけ、ACL を先に、`conflicts_with` は選ばない、Permission Leakage 0）はここでは判断しない。

実装は [Backend README](../../apps/backend/README.md) の「Hybrid Retrieval」に書いている。
数値は `paw_backend/memory/retrieval/ranking.py` の `RankingPolicy` と `limits.py` に集めてあり、変えても Schema は変わらない（Migration は不要）。

## 提案

### 1. 権限を先に決める（順序と Capability）

Backend が、Memory を 1 行も読む前に、`Authorizer` で Scope ごとに決める。結果（読める Scope と ID）だけから SQL を作る。

| Scope | 決め方 | Audit（[Decision 0004](0004-rbac-capability-and-audit-policy.md)） |
| --- | --- | --- |
| `user` | `memory.use`（自分の Resource） | `REQUIRED`: **Retrieval 1 回に 1 行**。書けなければ拒否（Fail-closed） |
| `shared` | `shared_memory.read` | `DENIED_ONLY`: 許可した読み取りは記録しない |
| `project` | DB から**受諾済みの Membership を読み直し**（`Principal` の `project_roles` は信用しない）、Project ごとに `project.read` | `DENIED_ONLY` |
| `repo` | `RepoAclSource` が返す Repository の ACL を、`project.read` の Repository Resource で判定（Override が `read` を外していれば拒否） | `DENIED_ONLY` |
| `project_group` | `ProjectGroupSource` が返す ID をそのまま信用（Capability なし） | なし |

- **Viewer も Project Memory を読める**。要件は Viewer を「Project / Repository / Task / Memory の閲覧のみ」とする。`project.memory.use`（Contributor 以上）ではなく `project.read` を使う。Archived は読める。Pending deletion / Deleted は**読めず、Authorizer にも尋ねない**（尋ねると呼び出しごとに拒否の Audit が増える）。招待中は Membership ではない。
- 拒否は Scope が何も返さないだけで、エラーにも応答にも出さない（存在を明かさないため）。エラーにするのは「決定を記録できない」（`audit_unavailable`）だけ。
- **Audit の量（要判断）。** `memory.use` は `REQUIRED` のため、User Scope を含む Retrieval が 1 回ごとに Audit の行を 1 つ書く。Chat の Turn ごとに呼ぶと量が多い。この実装は Decision 0004 を変えずに、そのまま使う。
- Project Group は、要件に実体・Member・権限がない（`memory/acl.py`）。呼び出し側が決めた ID を信用する。Source がなければ Group の Memory は読めない。Repository も、Table（PAW-027）がないため Source が要る。Source がなければ Repo Memory は読めない。
- Agent（`AgentGrant`）経由の Retrieval はこの Issue に含めない。Backend の Context 組み立てが Task の User の `Principal` で呼ぶことを想定する。

### 2. 権限・状態・鮮度の Filter は、順位付けの Query の WHERE に入れる

- Keyword の Query と Vector の Query は、それぞれ自分の WHERE に、権限の条件（`memory.acl`）、`status = 'active'`、鮮度の条件を持つ。結果を後から絞らない。距離の Sort・Rank は、読める行だけが入る。
- Relation（`conflicts_with`、`supersedes`）は**両端が読める**ときだけ読む。
- 1 回の Retrieval は、読み取り専用・`REPEATABLE READ` の 1 Transaction（期限で接続を切る `Database.run_abortable`）。
- **結果には件数・合計・「他に n 件」を含めない。** 読めない Memory の存在・数・Score が、応答から分からないことを Test で証明する（読めない Memory を無効にした DB と全 Field が等しい）。

### 3. Keyword 検索（日本語）

PostgreSQL の全文検索（`simple` 設定）を使う。`simple` は日本語の文を 1 つの Token にするため、そのままでは Query が文全体にしか一致しない。
形態素解析の拡張（MeCab 系、pg_bigm、PGroonga）はこの環境に無い。そこで、

- 文書側: NFKC で正規化し、ひらがな・カタカナ・漢字の**1 文字ごとに空白**を入れる（Migration 0043 の GIN Index。`status = 'active'` だけを対象にする）。**先頭 100,000 文字まで**を対象にする（tsvector は 1 MB を超えられず、超える Memory の書き込みが Index で失敗するため。Shared Memory の上限は 20,000 文字）。
- Query 側: 隣り合う 2 文字を**句**（`'検' <-> '索'`）として OR で並べる。英語は語ごと。
- 英語の機能語（`the`、`of`、`we` など）と、ひらがな 2 文字だけの組（`する`、`です`）は Query から除く（残る語がなければ全部使う）。

「同じ文字が同じ順で並ぶ」文を見つけ、一致する組の数で順位を付ける近似であり、形態素解析ではない。意味の近さは Vector が担う。
採らなかった案: `pg_trgm`（DB の Locale に依存し、非 ASCII が無視され得る）、Python 側で Token 化して別の列に保存する（`memory_versions` は不変で、列と Migration が増える）。

### 4. Vector 検索と ANN Index

- 距離は Cosine（`<=>`）。1 つの `embedding_model_id` だけを比べる。Embedder が使う Model が `embedding_models` に無ければ Vector の候補は出ない。
- **ANN Index（HNSW / IVFFlat）は作らない。** HNSW は次元を固定した Model ごとの Index が要り、Model は PAW-019 の Benchmark で決まる。権限で絞った正確な走査は、どの件数でも正しい。
- **将来 ANN Index を足すときの条件**（新しい Decision で承認する）: 権限の条件は今と同じく同じ WHERE に置く（Python で後から絞らない）。HNSW は Index を走査した**後**に Filter するため、絞り込みが強いと Recall が落ちる（漏れではなく取りこぼし）。対策は Scope ごとの部分 Index か `hnsw.iterative_scan`。Permission Leakage 0 の Test と Plan の Test を、そのまま通すこと。
- 類似度の下限（`min_vector_similarity`）は既定で `None`。類似度は Model ごとに尺度が違うため、Model の決定後に決める。下限がなければ、Memory が少ないとき Top-N に無関係な Memory が入る。

### 5. 融合と Score（暫定値）

| 項目 | 値 | 意味 |
| --- | --- | --- |
| 融合 | Reciprocal Rank Fusion、`k = 60`、Keyword と Vector の重みは 1 : 1 | 順位だけを使う（生の Score は尺度が違う）。最良の場合で割って 0〜1 |
| 候補数 | Keyword 50、Vector 50、Rerank へ 50（上限 200 / 200 / 100） | |
| 返す件数 | 既定 10、上限 50（Context Budget の Token 数は `[BENCHMARK]` のまま決めない） | |
| Rerank の混合 | `0.3 × 融合 + 0.7 × Reranker の Score` | Reranker がなければ融合のまま |
| Confirmation | confirmed × 1.0、inferred × 0.85、observed × 0.7 | 要件の「Confirmed > Inferred」 |
| Stale | × 0.5（`on_stale: lower_priority`） | |
| Importance | 0〜100 を × 0.8〜1.2（50 が 1.0） | |
| Pin | × 1.1 | 要件の「Retrieval 優先度の補助」 |
| Scope の具体性 | shared 0 < user 1 < project_group 2 < project 3 < repo 4 の各段で +2% | 要件の「Repo > Project > User > Shared」。`project_group` の位置は**推測**（要件は Group を定めない） |

最終 Score は「関連度 × 上の係数」で、関連度が 0 の候補は Importance や Pin が高くても 0 のまま（Vector の類似だけで採用を確定せず、構造化した情報で評価する）。

### 6. 鮮度

- `session_only` は Long-term Memory ではないため返さない。`expiring` は `expires_at` の瞬間から返さない（日付が無ければ返さない）。時計は呼び出しごとに 1 回、注入した Clock から読む。
- `revalidate` は `verified_at + revalidate_after` の瞬間から **Stale Candidate**（返す。印を付けて Score を下げる）。`stale_since` が付いた Memory も Stale。呼び出し側は `stale_policy = exclude` で除ける。
- `repo_commit` は、呼び出し側が Repository の現在の Head を渡したとき、Memory の `commit_sha` と違えば Stale。Head を渡さなければ判定しない（Fresh）。要件の「差分・変更の重要度」の判定はしない。

### 7. 重複と矛盾

- **重複**: 単語と日本語の 2 文字の組の集合の Jaccard が 0.9 以上（特徴が 4 未満なら完全一致のみ）を同じ Memory とみなし、優先順位（Confirmed、Fresh、より具体的な Scope、Score）の高い方に統合する（統合された Version の ID は `duplicates` に載る）。`conflicts_with` で結ばれた組は統合しない。
- **矛盾**: `conflicts_with` で結ばれた Memory は **Conflict Group** として返し、**どちらも選ばない**（要件: 曖昧な矛盾はユーザーに確認）。Query に一致しない側も、相手として最大 20 件まで取り込む。Group は分けずに Top-N に入れる（入らなければ Group ごと落とし、`dropped_conflict_groups` に数える）。
- `superseded` などは返さない。`active` なのに、読める `active` な Version に `supersedes` されている行は（Status が移っていない不整合として）返さない。

### 8. System Policy と Shared Memory

[Decision 0009](0009-shared-memory-administration.md) のとおり、System Security Policy が覆う `shared` の Memory は返さない。Policy を読めなければ Retrieval は失敗する（Shared Memory があるときだけ読む）。宣言（`policy_subjects`）が壊れた Shared Memory は判定できないため返さない。

### 9. 部品の失敗

- **Embedder / Reranker の失敗**（例外、時間切れ、形の違う答え）は、Retrieval を失敗させず、`degraded`（`vector` / `rerank`）として返す。Keyword の結果、または融合の順で答える。権限は影響を受けない。
- **Policy の Source、Repository / Project Group の Source の失敗**は Retrieval を失敗させる（安全な答えが作れない）。
- 全体は `timeout_seconds`（既定 10 秒）、各部品は `stage_timeout_seconds`（既定 3 秒）。例外の Message・Query・Memory の本文は Log にも Error にも出さない。

## 選定理由

- 権限を最初に、SQL の WHERE に入れるのは、順位付けや件数・Score に読めない行が影響しないため（後から絞る案は、Vector の Top-K を読めない行が占めて Recall が落ち、件数・Score に漏れる）。
- `project.read` を Project / Repo Memory に使うのは、要件が Viewer に Memory の閲覧を認め、既存の Capability で足りるため。新しい Capability は Decision 0004 の変更になる。
- 日本語を文字の組で扱うのは、追加の拡張なしで、多くの検索を Keyword でも拾えるため。誤って多く拾う分は Reranker と Vector に任せる。
- ANN を先延ばしにするのは、Model が未定で、権限の Filter と両立させる方法（部分 Index、Iterative Scan）を実測で選ぶべきため。
- 数値を暫定にするのは、実データも Benchmark もまだ無いため。`benchmarks/retrieval_runner.py` の Recall@K、MRR、Permission Leakage、Stale / Superseded の誤採用率で調整する。

## 代替案

- 権限を結果に後から適用する: 漏れる。採らない。
- User Scope の読み取りを Authorizer なしで（本人だけ）許す: Decision 0004 の 3 節（自分のデータの Capability は本人だけ）に反し、Agent への委譲を表せない。
- `memory.use` の Audit を避けるため `DENIED_ONLY` にする: Decision 0004 の変更（新しい Decision が要る）。
- Embedder の失敗で Retrieval を失敗させる: GPU の障害で Chat の Memory が全部無くなる。Degrade は答えの質が落ちることを `degraded` で示す。
- 矛盾の一方を自動で選ぶ（新しい方、Confirmed の方）: 要件が「曖昧な矛盾はユーザー確認」と定める。選ばない。
- 重複を Vector の類似度で判定する: Model 依存で、Permission の Test が難しくなる。単語・文字の集合で決める。

## リスク

- 日本語の Keyword は近似で、無関係な語の一致を拾う（Reranker と Vector で補う前提）。
- Membership の読み取りから Memory の読み取りまでの間（1 回の呼び出しの間）に、Membership が外れても、その 1 回は読める。
- 時間の側面（応答が遅いか速いか）から、読めない Memory の存在が推測されることは防いでいない（Permission Leakage の対象は、応答の内容）。
- `memory.use` の Audit は Retrieval ごとに 1 行増える（上の「Audit の量」）。
- Embedder が Model を切り替えると、旧 Model の Embedding は使われない（再生成が要る）。

## 承認後の扱い

承認された値・選択は、`ranking.py` / `limits.py` の設定と Backend README の記述に合わせる。値だけの変更は Migration が要らない（新しい Decision から `Supersedes`）。
ANN Index、`memory.read`（下の 1）、Agent 経由の Retrieval、HTTP の Endpoint は、それぞれ別の Issue / Decision で扱う。

## 決めてほしいこと

1. **User Memory の Retrieval の Audit**（推奨: 新しい Capability `memory.read`（`DENIED_ONLY`）を、Decision 0004 を `Supersedes` する新しい Decision で足す）。今の実装は `memory.use`（`REQUIRED`、Retrieval 1 回に Audit 1 行）を使う。
2. Viewer が `project.read` で Project / Repo Memory を読めること（推奨: 承認）。
3. 日本語の Keyword を「文字の組 + 全文検索」で近似すること（推奨: 承認。形態素解析の拡張を入れられる環境になったら見直す）。
4. ANN Index を作らず、正確な走査にすること（推奨: 承認。Model 決定後に条件付きで足す）。
5. 5 節の暫定値（融合、Confirmation、Stale、Importance、Pin、Scope の段差、`project_group` の位置）と 7 節の重複の基準（推奨: 暫定で承認し、Benchmark で調整）。
6. Embedder / Reranker の失敗を `degraded` で返すこと（推奨: 承認）。
7. Policy の Source が失敗したら Retrieval を失敗させること（推奨: 承認）。
8. Project Group と Repository を、呼び出し側の Source の答えとして扱うこと（推奨: 承認。実体は PAW-027 と Group の定義を待つ）。
9. 矛盾の Conflict Group を Group ごと Top-N に入れる（入らなければ落として数える）こと（推奨: 承認）。
10. `session_only` を返さず、`repo_commit` を Head を渡されたときだけ判定すること（推奨: 承認）。
