# Research Privacy Filter と Query 最小化の方針

- Status: Proposed
- Date: 2026-09-24
- Scope: PAW-053 と、外部の Research Provider（Direct Web、Docs、GitHub、OpenCode）へ Query を送る以降の Issue
- Supersedes: なし
- Approval: 未承認（Humanの承認待ち）

## 背景

[REQUIREMENTS.md](../../REQUIREMENTS.md) の「Web Research / Knowledge Layer」の Privacy は、External Research へ
Private source code 全文、Secrets、Token / Credentials、Private Memory、個人 Chat 全文、不要な内部 Project 情報を**原則送信しない**こと、
Backend が必要に応じて検索 Query を**抽象化・最小化**して送ることを定める。
一方で、**何を「同じ文章の写し」とみなすか、どの程度まで抽象化するか、どの数値で切るか、判断できないときにどうするか**は定めていない。

PAW-053 の実装は、動かすためにこれらを仮の値と規則で置いた。
[AGENTS.md](../../AGENTS.md) の「仕様変更」に従い、仮に置いたものを一覧にして承認または変更を求める。

**この Decision は Proposed であり、Human の承認を得ていない。** 承認されるまで、次の値と規則は暫定である。
値は `paw_backend/research/privacy/contract.py` の定数と `rules.py` の規則で、変更しても Schema は変わらない（Migration は不要）。

実装は [Backend README](../../apps/backend/README.md) の「Research Privacy Filter」に書いている。

## 提案

### 1. 何を保証するか、しないか

- **保証する**: 呼び出し側が `PUBLIC` 以外のラベルを付けた Context（`PRIVATE_SOURCE`、`PRIVATE_MEMORY`、`RAW_CONVERSATION`、`SECRET`）の**文字列の写し**は、Query に残らない。
  写しは、大文字小文字（Unicode の**完全な Case folding**、`str.casefold`。`ß` と `SS`、`İ` と `i` + 結合文字の点、語末の `ς` と `σ` は同じ）、空白、Unicode の正規化形、ゼロ幅文字の違いを無視して検出する。認識できる Credential も残らない。
- **保証しない**: 言い換え、翻訳、符号化（Base64 以外の変換、文字の分割）、Context に含まれていない情報は検出できない。
  LLM 自身が悪意を持つ場合（Prompt Injection など）の完全な防御でもない。
  Context のラベルは、出所を知っている Backend のコードが付ける。ラベルの付け間違い・付け忘れは、この層では見つけられない。
- ラベルが付いていない Context（ラベル無しの文字列、`None`、Gate を設定していない Broker への入力）は**送信を拒否する**（Default deny）。
- **Gate を設定していない `ResearchBroker` も検索しない**（Fail closed）。`preflight` が無く `unfiltered=True` でもない Broker の `gather` は、どの Provider も呼ぶ前に `PreflightRequiredError` を出す。渡し忘れで、Query が最小化も Audit もされずに外へ出ることを防ぐため。Gate を通さずに Query を送る道は、`ResearchBroker(registry, unfiltered=True)` という**唯一の明示的な Opt-out** だけで、Test と、Private な情報を何も持たない呼び出し側のためにある。Query をそのまま送り、Audit しない。この Opt-out を Private な情報から作った Query に使ってはならない。

### 2. 写しの検出（数値は仮）

| 対象 | 窓の長さ | 意味 |
| --- | --- | --- |
| `PRIVATE_SOURCE` / `PRIVATE_MEMORY` / `RAW_CONVERSATION` | 16 文字 | Draft の中で、16 文字以上続けて Context にもある部分を、その長さの限り全部消す。Context が 16 文字より短ければ、Context の長さを窓とする |
| `SECRET` | 4 文字 | 4 文字以上続けて Secret にもある部分を消す。Secret が 4 文字より短ければ、その長さを窓とする |

- 窓の長さは Case folding 後の文字数で数える（`ß` は `ss` の 2 文字）。消す範囲は Draft の元の文字の位置で、展開される文字（`ß` など）は一部だけ一致しても、その文字全体を消す。
- 窓を短くするほど検出は厳しくなるが、普通の単語（`internal` の中の `nter` など）まで消えて Query が壊れる。
  Secret は「一部でも漏らさない」ことを優先して 4 文字にした。**この厳しさで良いかは人間が決める。**
- `PUBLIC` の Context は Query を書き換えない（Public な文書と同じ文章を検索することは正当）。
- Gate は、仕上がった Query に、Context の全文、または Secret の 4 文字以上の単語が残っていないかを、Rule とは別のコードで確かめ、残っていれば拒否する。

### 3. 抽象化の規則（数値は仮）

Draft に次の規則を、この順に適用する。各規則の正確な定義は `rules.py` の Docstring にある。

| 順 | 規則 | 内容 |
| --- | --- | --- |
| 1 | URL | Host だけを残す。Host が Private（IP、`localhost`、Dot を含まない名前、末尾の Label が `local` `internal` `lan` `home` `corp` `intranet` `localdomain` `private` `arpa` など）なら URL ごと消す。Path、Query、Fragment、User 情報は残さない |
| 2 | E-mail | 消す |
| 3 | File Path | `/`、`~/`、`./`、`../`、Windows のドライブ、UNC で始まる Token と、区切り文字（`/` `\`）を 2 つ以上含む Token を消す |
| 4 | Private Host | Host だけの Token が Private なら消す。Port、Path、User 情報（`admin@10.0.0.5`）、IPv6 の `[...]` と Zone ID（`[fe80::1%eth0]:8080`）、末尾の Dot の付いた絶対名（`db.internal.:5432`）、`[]` の無い IPv6（`fd00::` のように `::` で終わる圧縮形を含む）、`%` を含む名前（`db.internal%eth0`、`db.%69nternal`、`db%2einternal`）も対象で、判定の前にそれらを取り除く。Dot を含まない名前は単語と区別できないので残す |
| 5 | ID | UUID、Credential Handle、12 桁以上の 16 進数、5 桁以上の数字を消す（小数は残す） |
| 6 | 長い不透明な Token | 40 文字以上の `A-Za-z0-9+/_=-` の連続を消す |
| 7 | Version | 3 つ以上の部分がある Version（`3.13.15`）を先頭 2 つ（`3.13`）にする |

- Version を 2 つにするのは、正確な Patch Version が環境の情報になるため。検索には Minor Version で通常足りる。
- 日付（`2026/09/24`）や `key=value/1` の一部など、規則に当たる正当な語も消える。過剰に消す方向に倒している。
- **`%` を含む Host は Private（不明）とみなす。** `%` は IPv6 の Zone ID（`fe80::1%eth0`、URL では `%25eth0`）で、DNS の名前には現れない。`%` の前が IP でも名前でも、公開名とはみなさない（Link-local の Address を送らないため。`db.%69nternal` のような `%` 符号化で Private な Label を隠す書き方も同じ扱いになる）。公開名に `%` が付く正当な Host は無いという前提で、過剰に消す方向に倒している。URL の外の語でも同じで、Dot で区切った 2 つ以上の Label（`%` と 16 進数 2 桁の符号化を含んでもよい。`%2e` は Dot）か、その後に `%` と Zone ID が付いた形（`db.internal%eth0`）の語は Host として認識して消す。Zone ID の前の名前に英字が無い語（`3.5%`、`12.5%off`）、Dot の無い語（`100%`、`50%off`、`%d`、`%.2f`）、符号化として不正な語（`db.%zzinternal`）は割合や書式の文字列なので残す。Dot 付きの名前に `%` 符号化を含む語は、Host でなくても消える（`my%20file.txt`）。この過剰な除去を許すか、`%` 符号化をどこまで認識するか（3 回以上の符号化、Label の空、`%` の後が符号化として不正な Label は残る）は人間が決める。
- **`[]` の無い IPv6 は、有効な Address なら消す（`::` で終わる圧縮形を含む）。** 語末の `:` は句読点として先に取り除くので、`fd00::` は `fd00`、`2001:db8::` は `2001:db8` になって Address として認識されず、Private な Address が送られていた。そのため、取り除いた部分が `::` で始まり、取り除いた後の語が空でないときは、`::` を付け直した形（`fd00::`）を 1 回だけ IPv6 として調べる（`::` だけの語、`note:`、`fd00:`、`10:30:`、`std::` は残る）。Address の判定は `ipaddress` に任せるので、16 進数の字だけで作れる語（`Bad::`、`Face::`）も、`a::b` と同様に消える。この過剰な除去を許すかは人間が決める。
- URL の外の Host の判定は、空白で区切られた 1 語の全体が Host である場合に限る。`_` や ASCII 以外の文字を含む名前（`my_db.internal`）と、他の文字と続いた語（`host=db.internal`）は残る（README の「制限と未確認の点」）。これらも消すか（過剰に消す方向を強めるか）は人間が決める。

### 4. 長さと拒否

| 項目 | 値 | 動作 |
| --- | --- | --- |
| Draft の長さ | 2,000 文字まで | 超えたら拒否（`draft_too_long`）。Query ではなく貼り付けられた資料とみなす |
| 送る Query の長さ | 256 文字まで | 語の区切りで切る（拒否しない）。切ったことは Record の `truncated` に残す |
| Context | 32 個、合計 400,000 文字まで（1 個は 200,000 文字まで。超える Piece は作れない） | 個数か合計が超えたら拒否（`context_too_large`）。処理量の上限でもある |
| 何も残らない | 単語の文字が 1 つも残らない | 拒否（`empty_query`） |

拒否の理由は閉じた集合（`unclassified_context`、`draft_too_long`、`context_too_large`、`empty_query`、`credential_remains`、`private_text_remains`、`audit_failed`）で、Query、Context、例外の文言は含まない。

### 5. Audit

- 外部へ送る前に、`ExternalSendRecord` を `ExternalSendAudit`（Protocol）へ渡す。Record が受理されなければ送らない（`audit_failed`）。Sink の例外・時間切れ（既定 5 秒）も拒否になる。
- Record は、Query の SHA-256（塩なし。Query は Public な内容にしてから送るため）、Query の文字数、送る Provider の種類、Project ID、消した数（Credential、写しがあった Context の数、抽象化の数）、Label ごとの Context の数、切ったかどうか、時刻だけを持つ。**Query の本文、消した文字列、Context の本文は持たない。**
- Record は「送ってよいと判断した」記録で、「送り終えた」記録ではない。Provider の成否は Record に含まない。
- 永続化する Sink は持たない（メモリ上の Test 用 Sink だけ）。Audit Log への保存は、後続の Issue が `ExternalSendAudit` を実装して接続する。

### 6. 対象外

- `ResearchBroker.fetch`（以前の結果の URL を取得する）は Gate を通さない。URL は Provider が返したもので、Query ではない。そのため `fetch` は、Gate を設定していない Broker（`unfiltered=True` でもない Broker）でも使える。Query を送る `gather` と違い、`fetch` は `PreflightRequiredError` を出さない。Private な Source の URL を取得してよいかは、Tool Broker（PAW-031）と個々の Adapter の責任とする。
- Provider の応答（結果の本文）の検査、Research Scratch（PAW-050）への保存時の検査は行わない。

## 選定理由

- 文字列の写しの検出は、LLM の判断に頼らず、Backend が決定的に行える最小の方法で、Test で確かめやすい。意味の言い換えを検出するには別の仕組み（埋め込みの類似度など）が要り、誤検出の扱いも決める必要がある。
- 検出できないものがあることを隠さず、README と、この Decision に書いた。
- 判断できない入力（ラベル無し、Audit できない）は送らない。Research が止まることより、Private な情報が出ることを重く見た。

## 代替案

- LLM に「Query から Private な情報を除け」と頼む: 判断を LLM に委ねることになり、Prompt Injection と誤りに弱い。Backend の規則を主とする方針（[AGENTS.md](../../AGENTS.md)）に反する。
- 窓を単語の N-gram にする: 日本語のように空白で区切らない文字で写しを検出できない。
- Draft が長い場合に切って送る: 貼り付けられた資料の先頭が外へ出る。拒否にした。
- Audit に失敗しても送る: 「外部送信を Audit できる」要件に反する。

## 承認後の扱い

承認された値と規則を、この Decision の `Approval` に記録して Status を Approved に改める。
値が変わる場合は `contract.py` の定数、`rules.py`、Test の期待値を、承認された値に合わせ、新しい Decision から `Supersedes` する。
承認されるまで、この値を前提にした運用（Private な Repository の内容を Research の Context に自動で入れる設計など）をしない。
