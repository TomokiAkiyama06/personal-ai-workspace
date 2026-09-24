# Tool Broker の Policy・承認・Credential の方針

- Status: Proposed
- Date: 2026-09-24
- Scope: PAW-031（Tool Broker / Capability Policy）と、Tool を使う以降の Issue（PAW-023 / 030 / 033 / 034 など）
- Supersedes: なし
- Approval: 未承認（Humanの承認待ち）

## 背景

[Security / Tool Permissions](../SECURITY_TOOL_PERMISSIONS.md) と [REQUIREMENTS.md](../../REQUIREMENTS.md) の
「Tool Broker / Capability Policy / Secret Isolation」「Tool approval boundary」は、Capability の 6 つの class、5 つの Approval level、
Credential の Plaintext を Agent に渡さないこと、Backend が最終判定することを定める。
一方、次のことは文書だけでは決まらず、PAW-031 の実装が選んだ。独立 Review もこれらを問題として指摘した。
承認前の Product Policy を実装が暗黙に確定させないよう、選択を一覧にして Human が承認または変更できるようにする。

実装は [Backend README](../../apps/backend/README.md) の「Tool Broker / Capability Policy」にある。
**この Decision は Proposed であり、Human の承認を得ていない。** 承認されるまで、実装の選択は暫定である。
[Decision 0004](0004-rbac-capability-and-audit-policy.md)（Proposed）が PAW-031 に残した「Tool Broker の Approval で委任不可の操作を許す仕組み」もここで決める。

## 提案

### 1. Approval は Level を上げるだけで、権限を広げない

1. Tool の呼び出しは、まず PAW-025 の認可（委任元 User の権限と Agent の Grant の積集合）を通る。Broker の Level はその上に足すだけで、置き換えも拡張もしない。
2. Approval で、委任できない Capability（`admin.*`、`owner.*`、Project の設定・Member・Agent Policy・Lifecycle など）の操作を Agent に許す仕組みは作らない。承認があっても `authz_denied` のまま。
3. Approval は認可・Scope・Budget を使うときにもう一度確認する。承認の後に権限が減れば、使えない（承認は消費されない）。

### 2. Policy の表（Capability class × Environment × Scope）

`DEFAULT_TOOL_POLICY` は 36 マス。表にない組み合わせは `DENY`（既定拒否）。Tool の Level は class ごとの Level のうち最も厳しいもの。
`ToolSpec.min_level` は Level を上げるだけ。Task Scope の範囲外（Path / Project / Credential）は Policy で許可できない。

| Capability | 範囲内 | Task の Host の外 | 範囲外（Path 等） | Host 全体の環境（範囲内） |
| --- | --- | --- | --- | --- |
| `read` | `AUTO` | `APPROVAL` | `DENY` | `AUTO` |
| `write` | `SCOPED_AUTO` | `APPROVAL` | `DENY` | `APPROVAL` |
| `execute` | `SCOPED_AUTO` | `DENY` | `DENY` | `APPROVAL` |
| `network` | `SCOPED_AUTO` | `APPROVAL` | `DENY` | `APPROVAL` |
| `credential-use` | `SCOPED_AUTO` | `DENY` | `DENY` | `STRONG_APPROVAL` |
| `destructive` | `APPROVAL` | `DENY` | `DENY` | `STRONG_APPROVAL` |

要件の表との違い（Human に判断を求める）:

- **厳しい方向**: build / test / lint（`execute`）は `AUTO` でなく `SCOPED_AUTO`（挙動は同じ: 人の確認なし）。Task Scope 内の一時ファイルの削除は `destructive` なので `APPROVAL`
  （要件は `SCOPED_AUTO`。「一時ファイルだけを消す」Tool を別に宣言する方法が要る）。Task の Host の外への Web の読み取りは `APPROVAL`（要件は Web / Docs の read-only 取得を `AUTO`）。
- **緩い方向（Tool の宣言が前提）**: Task Scope 内の `credential-use` は `SCOPED_AUTO`。要件の `STRONG_APPROVAL` は Credential の登録・更新・削除で、使用は分類されていない
  （AI 専用 Branch への push と PR 作成は `SCOPED_AUTO`）。Credential を管理する Tool は `min_level=STRONG_APPROVAL` を宣言する。
  Host 全体の `sudo` / 特権操作は要件では `STRONG_APPROVAL` だが、表は `HOST` 環境の `write` / `execute` を `APPROVAL` にしている。特権の Tool は `min_level=STRONG_APPROVAL` を宣言する必要がある。

### 3. 外部の読み取り・書き込み

1. 外部 write の許可は `TaskScope.hosts` で表す。Issue 作成や PR 作成といった「目的」の単位ではない。
2. Task の Host の外への読み取りは、行き止まりの `DENY` でなく `APPROVAL`（承認者は正確な URL を見て 1 回だけ許す）。ワイルドカードや「任意の公開 Host」の許可はない。
3. Credential は、その Credential の使える Host にだけ使える（`TaskScope.credential_handles` は Host の集合を持つ）。Task が触れられる Host でも、その Credential の使える Host でなければ `DENY`（承認でも許可しない）。

### 4. 承認者と取り消し

1. 承認・却下できるのは、Agent が働いている **User 本人だけ**（人間の `Principal`）。Agent 自身の ID は拒否する。Admin / Owner が他の User の Task を承認する仕組みは作らない。
2. 第三者には、他の User の承認の存在を教えない（`not_found`。Audit には `not_authorised`）。
3. 取り消しは、委任元 User と Admin / Owner ができる（権利を減らす方向だけなので、Admin / Owner が代われる）。Task の終了（cancelled / failed / completed）で、その Task の Open な承認を取り消す。
4. `STRONG_APPROVAL` は Step-up（PAW-023）の確認が要る。確認できない（Verifier がない、失敗、Timeout）ときは承認できない。Store も `step_up_verified` を受け取り、DB は Step-up なしの強い承認を保存しない。

### 5. 承認の表示・件数・却下

1. 承認者に、呼び出しの全引数を名前つきで見せる（Redact・Escape・256 文字で切り、全長と Hash 先頭つき）。見せるものがない承認は開かない。
2. (Task, User) ごとの Open な承認は 10 まで（1〜100 で設定可）。却下した呼び出しは 5 分（1 分〜24 時間で設定可）は再要求できない。

### 6. Credential

1. Agent の引数・結果に Credential の平文を入れない。使用は不透明な handle だけ。Tool の引数に平文があれば `DENY`、結果は Redact する（形式・代入の形・Key 名・Dict の Key）。
2. 検出は Best Effort であることを認める（本来の防御は handle のみの構造）。

### 7. 永続化と DB の保証

1. 承認は PostgreSQL に保存する（Migration `0031`）。状態は DB の Trigger と CHECK 制約でも守る（作成は pending のみ、合法な遷移のみ、識別する列は不変、DELETE / TRUNCATE は拒否、すべて `ENABLE ALWAYS`）。
2. Application の Role には最小権限（`tool_approvals` は SELECT・INSERT と状態の列の UPDATE、履歴は SELECT・INSERT）だけを与える。
3. **承認と消費の Role の分離は、この Decision の範囲では実装しない。** Application の Role が 1 つの間は、Application の Process が侵害されれば、その User の名前で承認を書ける（Database では防げない）。
   Agent の Runtime へ Application の Role の接続を渡さないことが前提。承認の Endpoint（PAW-022 / 023）ができる時に、別 Role または `SECURITY DEFINER` 関数で分離する。

## 既知の制限と後続の課題

- Symlink の確認と使用の間の競合（TOCTOU）、DNS Rebinding、Redirect は Executor の責務（[README](../../apps/backend/README.md) の「Executor の契約」）。
- Approval の期限は Application の時計で比較する（Database の時計ではない）。
- 却下の Cooldown は Hash 単位で、引数を変えた別の呼び出しは止めない（件数の上限が量を抑える）。
- 引数のない Tool は承認を開けない。承認が要る Tool は、何をするかを表す引数を必須にする。
- Credential の検出は形のわかる Format と代入の形だけ。
- Task の Scope・Grant・Project の状態は、Orchestrator が呼び出しごとに現在の値から作る（Broker は渡された Context を信頼する）。

## リスク

- 承認の Role を分離するまで、Application の Process の侵害は承認の偽造につながる（Database の保証は、書き換え・Replay・削除・自己承認以外の Application の誤りに効く）。
- 要件より緩い点（`credential-use`、特権の Tool）は Tool の宣言に頼る。宣言を誤った Tool は、要件より少ない確認で走る。Tool の登録時のレビューが要る。
- 厳しい点（Host の外の読み取りが `APPROVAL`）は、調査型の Task で承認が増える。運用で `TaskScope.hosts` の作り方を見直す。

## 承認後の扱い

承認された場合、PAW-031 の PR は本 Decision を参照する。
Human が変更を指示した項目は、この Decision を更新してから実装を合わせる。
承認後に方針を変える場合は、この Decision を書き換えず、新しい Decision から `Supersedes` する。
[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。
