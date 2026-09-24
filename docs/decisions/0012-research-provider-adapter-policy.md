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
2. **複数 Provider の結果の統合。** Provider（種類と名前の決定的な順）から交互に 1 件ずつ並べ、正規化した URL が同じ Hit は最初の 1 件だけを残し、件数を制限する。
   優先度や関連度による並べ替えは、要件に規則がないため行わない。
3. **URL の正規化。** scheme は `http` / `https` だけ（両者は別のものとして扱い、`www.` は除かない）。Host は小文字の ASCII、既定の Port と Fragment を除去、`%xx` の 16 進を大文字、非 ASCII は Percent-encode、Query は `(名前, 値)` の順に並べる。
   追跡用の Parameter（`utm_*`、`fbclid`、`gclid` など）を除く。空白・制御文字・バックスラッシュ・User info（`@`）・2,048 文字超の入力、IPv6 と非 ASCII の Host は拒否し、名前解決はしない。
4. **Credential の除去。** Query の Parameter のうち Credential 用の名前（`access_token`、`token`、`api_key`、`sig`、`x-amz-signature` など、`locator.py` の一覧）を除く。
   名前は Percent-decode（最大 4 回。Proxy が複数回 decode する場合に備える）してから、大文字小文字を区別せずに比べる。decode した名前に `&`、`;`、`=`、`#` が入るものは、先に decode する Parser では別の Parameter になるので、名前として成り立たないものとして丸ごと除く。**一覧は Best effort** で、Path に入った Credential や一覧にない名前は判別できない。
5. **License と `robots.txt`。** 要件に定義がないため `SourceMetadata` には含めない。必要になったときに追加する。
6. **責務の境界。** `network` Capability の確認、SSRF 対策、`robots.txt` の遵守、名前解決の後の接続先の検査は、呼び出し元（Tool Broker、PAW-031）と個々の Adapter の責任とする。この層は認可の判断も Network の Access もしない。
7. **公開日時は UTC で表せるもの。** `published_at` は Timezone つきで、UTC へ変換できる値だけを受け付ける（`datetime.max` を UTC-01:00 で表した値など、範囲を超える値は Adapter の不正な Response として扱い、`invalid_response` にする。Timezone のない値、Timezone の `utcoffset` が例外を出す値も同じ）。
8. **Timeout は協調的。** Adapter が Cancel を無視する、または Event Loop を止める同期処理をする場合、Broker は止められない。Adapter の実装規約として、Cancel に応じる非同期の実装を求める。

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
