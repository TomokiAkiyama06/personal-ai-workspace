# Memory の読み取り Capability `memory.read`（Retrieval の Audit の量）

- Status: Proposed
- Date: 2026-09-26
- Scope: PAW-043（Hybrid Retrieval）と、Long-term Memory を読む以降の Issue（Context の組み立て、Memory 画面の検索など）。書き込みや Shared Memory の管理の Capability は範囲外
- Supersedes: [Decision 0004](0004-rbac-capability-and-audit-policy.md) のうち、Memory の**読み取り**に関する次の 2 点だけ。「3. Audit の記録と Fail-closed」の 1 の `DENIED_ONLY` の許可リスト（`project.read`、`shared_memory.read`）に `memory.read` を足すこと、「2. Agent への委任」の 2 の委任可の一覧に `memory.read` を足すこと。0004 の他の点（`memory.use` が `REQUIRED` であること、Agent の判定が常に `REQUIRED` であることを含む）は変わらない
- Approval: 未承認

## 背景

[Decision 0019](0019-hybrid-retrieval-policy.md)（Approved）の 1 は、Hybrid Retrieval が User Scope の Memory を読むときの認可に、既存の Capability `memory.use` を使う。
`memory.use` の Audit Mode は Decision 0004 の 3 の 1 で `REQUIRED` である。そのため、**User Scope を含む Retrieval が 1 回ごとに、Audit の行を 1 つ書く**。
Retrieval は Chat の Turn ごとに呼ぶ（Context の組み立て）ため、量が多い。`audit_events` は追記専用で削除できず（Decision 0004 の 5）、行数は増え続ける。
`REQUIRED` は記録できないと許可を拒否に変える（Fail-closed）ので、Audit Table の障害が、Memory を使うすべての Chat の停止になる。

一方で、Shared Memory の読み取り（`shared_memory.read`）と Project の内容の読み取り（`project.read`）は、Decision 0004 が `DENIED_ONLY`
（許可した読み取りは記録せず、拒否だけを Best Effort で記録し、Audit の障害でも読み取りを止めない）としている。
自分自身の Memory の読み取りだけが `REQUIRED` なのは、読み取りの扱いとして一貫しない。
Human は 2026-09-25 に、Decision 0019 の 1 を「推奨の方向で承認。0024 で具体化する」と回答した。この Decision がその具体化である。

**この Decision は未承認（Proposed）である。** 承認されるまで、実装は `memory.use`（`REQUIRED`）のままにする（下の「承認後の扱い」）。

## 提案

### 1. Capability `memory.read` を足す

| 項目 | 値 | 理由 |
| --- | --- | --- |
| 名前 | `memory.read` | `memory.use`（書き込み・提案・利用）と区別する |
| Scope | `SELF`（自分のデータ。所有者本人だけが使える） | `memory.use` と同じ。Owner でも他の User の Private Memory は読めない（Decision 0004 の 1 の 3） |
| Role | User、Admin、Owner が持つ。`system` は持たない | `memory.use` と同じ |
| Audit | `DENIED_ONLY` | 読み取り専用の Capability の許可リストに足す |
| 委任 | 可（`memory.use` と同じ） | Agent の判定は Mode に関わらず常に `REQUIRED` のまま（Decision 0004 の 3 の 1）。委任しても Agent の読み取りの記録は減らない |

### 2. 何を `memory.read` にし、何を変えないか

- **`memory.read` にする**: Hybrid Retrieval の User Scope の認可（Decision 0019 の 1 の `memory.use` の代わり）。
- **変えない**:
  - `memory.use`（`REQUIRED`）は、書き込み・Candidate の提案（`propose_candidate`）など、読み取り以外の Memory の利用に使い続ける。
  - `shared_memory.read`、`project.read` の Mode と権限。Project / Repo Memory を `project.read` で読むこと（Decision 0019 の 2、Human が承認済み）。
  - Agent の判定は常に `REQUIRED`（Agent の Retrieval は、認可のたびに Audit の行を書く）。
  - 認証されていない Request の拒否は Database に書かず Log に出す。
  - Audit の項目、Table の保護、Break-glass の問い（Decision 0004 の 3 の 4、5）。

### 3. 記録されるもの・されないもの

- 拒否は記録する（`DENIED_ONLY` は拒否を Best Effort で書く）。Role が `system` の呼び出しなど、拒否される呼び出しは呼び出しごとに 1 行を書く（Decision 0004 の 3 の 3 の (b) と同じ。回数制限は PAW-022 で決める）。
- 許可した読み取りは記録しない。誰がいつ自分の Memory を検索したかは Audit から分からない。Audit の障害は読み取りを止めない。

## 選定理由

- 読み取りを許可リストの `DENIED_ONLY` に揃えるのは、Shared と Project の読み取りが既にそうであり、量の多い読み取りを Audit の書き込みの停止と結びつけないため。
- 新しい Capability を足すのは、`memory.use` の Mode を変えると、書き込み・提案の Audit（`REQUIRED`）まで外れるため。読み取りだけを分ける。
- 実装の変更は小さい: Capability 1 つ、Role の表 1 行、委任可の一覧 1 つ。Migration は要らない（Audit の `action` は文字列で、既知の Capability 名の制約は DB にない）。

## 代替案

- **`memory.use` を `REQUIRED` のままにする（現在の実装）**: Retrieval 1 回ごとに 1 行が増え、Audit Table の障害で Memory を使う Chat が止まる。行は削除できず、増え続ける。
- **Audit を間引く（サンプリング）**: 許可した読み取りの一部だけを書く。何が記録されるかが決まらず、「記録がない」ことの意味が曖昧になる。仕組み（乱数、割合の設定）も新しく要る。採らない。
- **`memory.use` を `DENIED_ONLY` にする**: 書き込み・提案の記録まで外れる。採らない。
- **Retrieval を別の種類の Event（Query の Hash など）として記録する**: 量は変わらず、Query の Hash は Query の内容を推測できる手がかりになる。採らない。
- **User Scope の読み取りを Authorizer なしで、本人だけに許す**: Decision 0019 の代替案のとおり、Agent への委譲を表せず、Decision 0004 の 1 の 3 に反する。

## リスク

- **許可した読み取りが記録されない**。誰が、いつ、自分の Memory をどれだけ読んだかは Audit から分からない。Decision 0004 が Shared と Project の読み取りで既に認めたリスクを、User Scope に広げる。
  読める範囲を誤る（漏れる）ことへの備えは Audit ではなく、Backend の ACL と Permission Leakage 0 の Test（`test_retrieval_leakage.py`、`test_retrieval_eligibility.py`）である。この Test が安全網であり続けることを、Retrieval を変える Issue の受け入れ条件に置く。
- 認証済みの User の拒否は 1 回ごとに 1 行を書く。読み取りの拒否（`system` の呼び出しなど）が多いと行が増える。
- `memory.read` を持つ Role の表を、`memory.use` と別に保守する必要がある（片方だけを変える誤りの余地）。Test で、2 つの Capability の Role の対応が同じであることを確かめる。

## 承認後の扱い

承認されたら、別の Issue（Decision 0019 の実装の後続）として、次を 1 つの PR で行う。

- `authz/capabilities.py`（Capability、`CAPABILITIES` の Scope・委任・`read_only=True`）、`authz/policy.py`（`_USER` の集合）を変える。Decision 0004 は書き換えない。
- `retrieval/resolver.py` の User Scope の認可を `memory.use` から `memory.read` にする。`memory.use` の呼び出し（Shared Memory の Candidate の提案など）は変えない。
- `test_authz_*` の Capability の一覧・委任の一覧・Audit Mode の許可リストの Test、Retrieval の Audit の Test（許可で 0 行、拒否で 1 行）を更新する。
- Backend README の「認可（RBAC / Capability）と Audit」の `DENIED_ONLY` の一覧、「Hybrid Retrieval」の記述を合わせる。

authz の Capability の表は他の Issue も変えるため、Merge の順序に依存して Conflict しやすい。そのため、この Issue（PAW-043）の PR では変えない。
承認されるまで、Retrieval は `memory.use`（`REQUIRED`）を使い続け、この Decision の値を前提にした実装はしない。

## 決めてほしいこと

1. Capability `memory.read`（`DENIED_ONLY`）を足し、Retrieval の User Scope の認可に使うこと（推奨: 承認）。
2. `memory.read` を委任可にすること。Agent の判定は常に `REQUIRED` のまま（推奨: 承認）。
3. `memory.read` の Role を `memory.use` と同じ（User、Admin、Owner。`system` は持たない）にすること（推奨: 承認）。
4. `memory.use`（`REQUIRED`）を、書き込み・提案などの読み取り以外に使い続けること（推奨: 承認）。
5. 許可した読み取りが Audit に残らないリスクを、Permission Leakage 0 の Test を安全網として受け入れること（推奨: 承認）。
6. 許可した読み取りの**回数の集計**（Audit ではなく Metric）を後で足すか（推奨: 今は決めない。必要になったら別の Issue）。
