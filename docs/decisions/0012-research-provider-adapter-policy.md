# Research Provider Adapter の方針

- Status: Proposed
- Date: 2026-09-24
- Scope: PAW-051 と、Research の Provider を使う以降の Issue（PAW-052 Evidence / Claim Provenance、PAW-053 Privacy Filter、各 Provider の Adapter）
- Supersedes: なし
- Approval: 未承認（Humanの承認待ち）

## 背景

[REQUIREMENTS.md](../../REQUIREMENTS.md) の「Web Research / Knowledge Layer」は、Provider の抽象化（Direct Web、Docs、GitHub、OpenCode）と Privacy を定める。
一方で、複数 Provider の結果の統合規則、不正な Response の扱い、URL の正規化の細部、Credential の除去の範囲は定めていない。
PAW-051 の実装は、動かすためにこれらを選んだ。Review（Codex）は、これらが `docs/decisions/` の提案と承認を経ずに実装の契約になっていることを指摘した。
[AGENTS.md](../../AGENTS.md) の「仕様変更」に従い、選択を一覧にして承認または変更を求める。

**この Decision は Proposed であり、Human の承認を得ていない。** 承認されるまで、次の選択は暫定である。
実装は [Backend README](../../apps/backend/README.md) の「Research Provider Adapter」に書いている。

## 提案

1. **不正な Hit は Provider の Response 全体を無効にする。** 1 つでも検証に通らない Hit があると、その Provider の Response 全体を `invalid_response` とする（一部だけを採用すると、Adapter の不具合が見えなくなるため）。
   他の Provider の結果には影響しない（失敗の隔離）。
   Constructor を通らずに作られた `ProviderHit` / `ProviderDocument`（Slot が未設定、型や長さが不正、UTC で表せない公開日時）も同じで、Class が正しくても Field を全て読み直して検証する（`isinstance` は Field の値を保証しない）。
   Field は `ProviderHit` / `ProviderDocument` 自身の Slot から 1 度だけ読み、Subclass の Property や `__getattribute__` は呼ばない。Property だけで Field を返す Subclass は不正な Response とする（任意の例外と、読むたびに変わる値を防ぐため）。`fetch` も同じ規則を使う。
   **Response の Container は `list` か `tuple` そのものだけ**とする（`type(x) is list` / `tuple`）。Subclass と、`__class__` で名乗る Object は、`__len__`、`__iter__`、`__getitem__` を呼ばずに `invalid_response` とする（Adapter の Code が Broker の中で例外を出したり、長さを偽って `limit` を超えさせたりするのを防ぐため）。Subclass を許して基底 Class の Method で読む案もあるが、正当な Subclass の使い道がなく、拒否のほうが単純で確実なので採らない。
   **失敗の分類も Adapter の Code を動かさない。** `ProviderFailure.code` は `ProviderFailure` 自身の Slot から 1 度だけ読み、Class は `type()` と `issubclass` で確かめる。Subclass の Property、`__getattribute__`、`__class__`、Metaclass の `__name__` は呼ばない。`ResearchErrorCode` そのものでない値と、未設定の Slot は `internal_error` とする（例外が `gather` から漏れて他の Provider の結果が失われるのを防ぐため）。
   **Log に出す例外の型は固定の分類にする。** Adapter が上げた例外の Class 名は Adapter が決められる（Credential や、Log 行を偽造する改行を入れられる）ので、Log の `exception_type` は、`type(error)` が Python 組み込みの例外またはこの Package の例外の**一覧（`LOGGED_EXCEPTION_TYPES`）にある Class そのもの**のときだけ、その名前とし、それ以外（Adapter が定義した Class、一覧にある Class の Subclass、名前だけ似せた Class）は固定の `adapter_error` とする。名前を切り詰めたり無害化したりして出す案は、何が残るかを Adapter が決められるので採らない。照合は `id` で行い、Class の属性は 1 つも読まない（Metaclass の Hook を動かさない）。代償として、組み込み以外の Library の例外（`httpx.ConnectError` など）は型では区別できず、Adapter が `ProviderFailure` へ変える必要がある。一覧に Class を足すことは、この Decision の変更ではなく Code の Review で行う。
2. **複数 Provider の結果の統合。** Provider（種類と名前の決定的な順）から交互に 1 件ずつ並べ、正規化した URL が同じ Hit は最初の 1 件だけを残し、件数を制限する。
   優先度や関連度による並べ替えは、要件に規則がないため行わない。
3. **URL の正規化。** scheme は `http` / `https` だけ（両者は別のものとして扱い、`www.` は除かない）。Host は小文字の ASCII、既定の Port と Fragment を除去、`%xx` の 16 進を大文字、非 ASCII は Percent-encode、Query は `(名前, 値)` の順に並べる。
   追跡用の Parameter（`utm_*`、`fbclid`、`gclid` など）を除く。空白・制御文字・バックスラッシュ・User info（`@`）・2,048 文字超の入力、IPv6 と非 ASCII の Host は拒否し、名前解決はしない。
4. **Credential の除去。** Query の Parameter のうち Credential 用の名前（`access_token`、`token`、`api_key`、`sig`、`x-amz-signature` など、`locator.py` の一覧）を除く。
   名前は Percent-decode（最大 4 回。Proxy が複数回 decode する場合に備える）してから、大文字小文字を区別せずに比べる。decode した名前に `&`、`;`、`=`、`#` が入るものは、先に decode する Parser では別の Parameter になるので、名前として成り立たないものとして丸ごと除く。**一覧は Best effort** で、Path に入った Credential や一覧にない名前は判別できない。
5. **License と `robots.txt`。** 要件に定義がないため `SourceMetadata` には含めない。必要になったときに追加する。
6. **責務の境界。** `network` Capability の確認、SSRF 対策、`robots.txt` の遵守、名前解決の後の接続先の検査は、呼び出し元（Tool Broker、PAW-031）と個々の Adapter の責任とする。この層は認可の判断も Network の Access もしない。
7. **公開日時は UTC で表せるもの。** `published_at` は Timezone つきで、UTC へ変換できる値だけを受け付ける（`datetime.max` を UTC-01:00 で表した値など、範囲を超える値は Adapter の不正な Response として扱い、`invalid_response` にする。Timezone のない値、Timezone の `utcoffset` が例外を出す値も同じ。例外の種類は 10 のとおり）。変換の前に、標準の `datetime` の Method で Field を通常の `datetime` へ複製する（`datetime.astimezone` は途中の値を Subclass 自身の Constructor で作るため、複製しないと Adapter の Code が Broker の中で動く）。
8. **Timeout は協調的。** Adapter が Cancel を無視する、または Event Loop を止める同期処理をする場合、Broker は止められない。Adapter の実装規約として、Cancel に応じる非同期の実装を求める。
9. **Provider の名前は正規化せず、厳密な `str` の写しだけを保持する。** `name` は受け取った文字列そのものを `fullmatch` で `[a-z0-9][a-z0-9_-]{0,63}`（ASCII だけ、`$` と `IGNORECASE` なし）に照合する。全角・NFKC で同じになる文字・大文字・Zero-width 文字・空白・末尾の改行は拒否し、NFKC、小文字化、`strip` は行わない（別の名前と同じにしたり、見た目が同じ名前を別に登録させたりしないため）。
   `str` の Subclass は中身が合えば受け付けるが、Registry が持つのは C の `str.encode` で作った**厳密な `str` の写し**で、Adapter の Object は持たない（`__hash__` の例外で `register` が失敗する、`__eq__` で一意性をすり抜ける、`__lt__` で `select()` / `gather()` が失敗する、`__str__` で Log に Credential 相当の文字が入る、のを防ぐため）。型は `type()` で読み、`__class__` で `str` / `ProviderKind` を名乗るだけの Object は `ProviderInterfaceError` とする。`kind` は `ProviderKind` の要素そのものだけを受け付ける。
   Subclass を拒否する案もあるが、`StrEnum` の要素を名前に使う正当な使い道があり、写しなら同じ安全性が得られるので採らない。
10. **同期で動く Adapter の Code が出した `BaseException` は、Adapter 自身の失敗とする。** `asyncio.CancelledError` は `BaseException` で、`except Exception` では捕まらない。`Task.cancel()` は `await` の地点でしか届かないので、`await` を挟まずに動く Adapter の Code（`published_at` の `tzinfo.utcoffset`、`provider.search` / `provider.fetch` を読んで呼ぶ部分。Property や `__getattribute__` を含む）が `CancelledError` を出しても、それは Broker の Task の Cancel ではない。それを通すと `gather()` / `fetch()` 全体が Cancel され、成功した他の Provider の結果が失われる（1 つの Provider が全体を止められる）。
   そのため、この同期の部分だけ `BaseException` を捕まえる。`utcoffset` の例外は `invalid_response`、`provider.search` / `fetch` を読んで呼ぶ部分の例外は `internal_error`（Log の `exception_type` は固定の `adapter_error`）にする。他の Provider の結果は残す。`await` の最中に届く Cancel と Timeout は、これまでどおり握りつぶさず伝える。
   **`KeyboardInterrupt` と `SystemExit` も同じに扱う。** 同期の部分では、Adapter が出したものと Signal の本物の `KeyboardInterrupt` を区別できず、後者もこの窓（数マイクロ秒）の間だけは `invalid_response` になって握りつぶされる。これを通す案（`BaseException` のうち `KeyboardInterrupt` / `SystemExit` だけ再送出）もあるが、Adapter が `sys.exit()` を呼ぶ Library を使うだけで Process が止まり、失敗の隔離という目的に反するので採らない。`asyncio.run` は最初の Ctrl-C を Task の Cancel（`await` の地点）に変えるので、通常の起動では、本物の `KeyboardInterrupt` が同期の窓に届くのは 2 回目の Ctrl-C だけである。
   **例外を出さずに Task の Cancel を要求する同期の Code も、Adapter の失敗とする。** 同期の Code は `asyncio.current_task().cancel()` を呼んだあと、普通に値を返せる（`__getattribute__` が Method を返す、`tzinfo.utcoffset` が Offset を返す）。何も出ないが、要求は Task に残り、次の `await`（または Task の終わり）で届いて、`gather()` 全体を Cancel し、成功した他の Provider の結果を失わせる。
   そのため、同期の窓（`provider.search` / `provider.fetch` を読んで呼ぶ部分と、1 つの Provider の Response の検証。`published_at` の `tzinfo.utcoffset` を含む）の前後で `Task.cancelling()` を記録し、増えていれば、増えた数だけ `Task.uncancel()` で取り消して、その Provider を Adapter の失敗にする（前者は `internal_error`、後者は `invalid_response`。Log の `exception_type` は固定の `adapter_error`）。呼び出して得た Coroutine は `await` せずに閉じる（Provider の Code は動かない）。窓の前からあった Cancel の要求（呼び出し元自身の `cancel()`）は数が減らないので残り、`await` の地点でこれまでどおり届く。`asyncio.current_task()` が `None`（Task の外）なら何もしない。
   **限界:** `Task.uncancel()` は「次の `await` で Cancel する」印を、数が 0 になったときにしか消さない。窓の前に数が 1 以上あって、その Cancel が届いていない Task（`CancelledError` を握りつぶして `uncancel()` を呼ばなかった Task）では、Adapter が付けた印が残り、次の `await` で届く。窓の中で Adapter 自身が `Task.uncancel()` を呼んで数を減らしても検出しない。`ProviderRegistry.register` が Adapter の属性を読む部分は Broker の窓ではなく、同じ保護は付けていない（登録は運用者の配線で、失敗は `ProviderInterfaceError` として呼び出し元へ出る）。
   **この Decision が決めないこと:** Adapter の非同期の Code（`await` の最中）が自分で出した `CancelledError`、または自分で呼んだ `Task.cancel()`（内部で使った Task が別の場所で Cancel された場合など）は、Task の Cancel と区別せず、これまでどおり `gather` へ伝わる。`Task.cancelling()` は同期の窓の前後を比べるだけに使う。`await` の最中は、Adapter 自身の要求と本物の Cancel が同じ `CancelledError` として届き、数の増加では区別できないので、区別する案は別の判断として承認を求める。

## 選定理由

- Provider ごとに Response の質が違うため、不正な Hit を黙って捨てるより、Provider 全体の不具合として見えるほうが Adapter の修正につながる。
- 交互配置と URL による重複除去は、Provider の順序に依存せず、決定的で、説明しやすい。
- Credential の除去は、Provider が返した Credential つきの URL が、表示や保存に残らないための最後の防波堤で、完全ではない。

## 代替案

- 不正な Hit だけを捨てる: 部分的に使えるが、Adapter の不具合を隠す。
- 関連度で並べ替える: Provider ごとの Score が比べられない。統合の規則は PAW-043 / PAW-052 で決める余地がある。
- Credential を allowlist 方式（許す Parameter だけを残す）にする: 安全だが、正当な Parameter を失い、Provider ごとの調整が要る。

## 承認後の扱い

承認された内容をこの Decision の `Approval` に記録して Status を Approved に改める。変更が必要な場合は、新しい Decision から `Supersedes` する。
