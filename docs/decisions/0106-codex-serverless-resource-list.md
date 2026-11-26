# Codex の Session で、Server を指定しない MCP の Resource の一覧の呼び出し（`server = "codex"`）を通す（Decision 0101 の 3 の一部を変える。通す Tool、守りの条件、確かめ方）

- Status: Approved
- Approval: 2026-11-26、Human が作業 Session（Claude Code）内で、PR [#358](https://github.com/TomokiAkiyama06/personal-ai-workspace/pull/358) の 3 つの判断点（推奨つき）を受けたうえで、1〜3 をすべて A と回答して承認（approved as recommended, all 3 points: 末尾の「承認時の決定」）
- Date: 2026-11-23（承認 2026-11-26）
- Scope: Issue [#341](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/341)（[Decision 0105](0105-codex-no-interim-model.md) の PR の分け方の 4 で、Human が `gpt-6-sol` を本物で確かめて分かった）。この Decision は設計だけで、コードは承認の後の PR で書く。関係する既存の口: `apps/backend/paw_backend/agents/codex_app_server.py`（`CodexStream._item`・`MCP_RESOURCE_TOOLS`・`refused_item`）、`agents/bridge.py`（`resources/*` に `Method not found` を返し、1 行の Log を残す）、`agents/codex_cli.py`（`session_config`・`DISABLED_FEATURES`）、`deploy/cli-session/smoke_codex_recorder.py`・`live_check_codex.py`。
- Supersedes（一部）:
  - [Decision 0101](0101-codex-mcp-resource-tools-exception.md)（Approved）の 3 と、その「承認時の決定」の 1 のうち **「`server` を省いた（`"codex"`）呼び出しは止める」** の部分。下の 1 の 2 つの一覧の Tool に限って通す。
  - 次は書き換えず、その中で作る: 0101 の 1（許す Tool の集合）・2（`paw` だけが MCP の Server）・4・5、0101 の 3 の残り（`paw` を名指す呼び出しは通す、`paw` でも `codex` でもない宛先は止める、Relay と Bridge は Resource を出さない）。[Decision 0089](0089-codex-cli-runtime-route.md) の 2（Item の検出を二重の守りにする）、[Decision 0104](0104-codex-model-catalog-override.md)・0105（Catalog の上書き、検証した組）。[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。

## 背景

Human は、確かめ専用の ChatGPT の Login で `live_check_codex.py` を 3 回走らせた（`codex-cli 0.156.1`、`gpt-6-sol`、#355 の Catalog の上書きあり）。証拠は `/data/results/paw-codex-live-check-2026-11-17/` の `gpt-6-sol.json`・`gpt-6-sol-2.json`・`gpt-6-sol-3.json`。

- どの回も Probe は `connected` だった（約 920 / 9 tokens、3 秒前後）。Catalog の上書きのまま、`gpt-6-sol` と話せている。
- どの回も Review の Node は 3〜4 秒で `stream_refused` になった（`invalid_response`）。Review は送られず、後片付けは済み、Login の更新もなかった。
- #356・#357 で、断った Item の名前だけを残すようにした。3 回目の結果は `refused_items: ["mcpToolCall:codex:list_mcp_resources"]` だった。つまり Model は Review を始める前に、`list_mcp_resources` を **`server` の引数なしで** 呼んだ。
- 0101 の背景のとおり、この呼び出しの Item は `server = "codex"` になる（Codex の Resource の Handler が、引数を省いたときに付ける名前）。0101 の 3 では、これを止めると決めていた。
- 宛先: `server` を省いた一覧の呼び出しは、Session に設定した MCP の Server すべてに `resources/list`（または `resources/templates/list`）を送る。0101 の 2 の守り（`mcp_servers` は `paw` だけ、組み込みの Server を足す Feature は `DISABLED_FEATURES` で止める、Request の Tool の検査）で、その Server は `paw` だけになる。`paw` の Bridge は `resources` の Capability を出さず、`resources/*` には `Method not found` を返す。Bridge には 1 行の Log が残り、中身は書かない。

まとめると、今の規則では Code Mode を止めた `gpt-6-sol` は Review の最初の一手で必ず止まる。そして止めている呼び出しは、`paw` を名指した呼び出し（0101 の 3 で通すもの）と同じところにしか届かない。

## 判断点（Human に答えてほしいこと）

各点に案と推奨を置く。番号で「1 は A」のように答えてほしい。

### 1. どの呼び出しを通すか

- **A（推奨）: `mcpToolCall` で `server == "codex"` かつ `tool` が `list_mcp_resources` か `list_mcp_resource_templates` なら通す。** 0101 の 3 の `paw` を名指した呼び出しと同じく、PAW の Tool の呼び出しの順の数え方に入れ、Bridge の呼び出しの数には入れない。`read_mcp_resource` は `server` が必須の引数なので、`server = "codex"` の読み出しは Model が存在しない Server を名指したことになる。これは今のとおり止める。`paw` でも `codex` でもない宛先も、今のとおり止める。
- B: `list_mcp_resources` だけ通す（今回見えたもの）。悪い点: `list_mcp_resource_templates` は同じ形・同じ宛先で、Model が次にそちらを呼べばまた止まる。
- C: 今のまま止める。悪い点: Code Mode を止めた `gpt-6-sol` は Review ができない。0099 の 5 で Codex の Review は必須なので、Codex の組は作れず、書き込む Task は 0105 のとおり今の本番のまま待つ。

### 2. 通すときの守りの条件

- **A（推奨）: 1 の例外は、Session の設定で MCP の Server が `paw` だけのときに限る。** これは 0101 の 2 で今も起動の前に確かめている条件で、`CodexStream` に Session の Server の一覧を渡し、`["paw"]` でなければ `server = "codex"` の呼び出しを今のとおり止める（守りを 2 重に保つ）。PAW 以外の MCP の Server を足すことは、0101 の 2 のとおり新しい Decision が要る。そのときは、この例外も見直す。
- B: 条件を置かず、1 の呼び出しをいつも通す。悪い点: 将来の設定の誤りで Server が増えると、宛先が気付かれずに広がる。

### 3. 確かめ方

- **A（推奨）: 実装の PR で、記録用の Server の Smoke（`smoke_codex_recorder.py`、`127.0.0.1`、Dummy の Login）に、Model が `server` なしで `list_mcp_resources` を呼ぶ手順を足す。** そのとき次を確かめ、結果を `/data/results/` に残す。
  - Item が `mcpToolCall`・`server = "codex"` で来る。
  - Bridge の Log に `resources/list` が 1 行だけ出て、ほかの Server に何も届かない。
  - Model に Error が返り、続く `submit` が Node の結果まで通る。

  その後、Human が `live_check_codex.py` を `gpt-6-sol` でもう一度走らせる。次に別の Item で止まったら、同じように名前を見て、新しい Decision で扱う。合格したら 0105 の PR の分け方の 4（`(0.156.1, gpt-6-sol)` を検証した組に足す PR）に進む。

## PR の分け方

1. この Decision（docs だけ）。
2. 承認の後: `CodexStream` の Item の検出の変更（1・2）、Test、記録用の Server の Smoke の手順（3）。検証した組はまだ足さない。
3. Human が 2 の入った `live_check_codex.py` を `gpt-6-sol` で走らせ、合格したら 0105 の 4 の PR。

## Test の方針

- Item の検出の Test:
  - 次は通る: `server = "codex"` で `list_mcp_resources`、`server = "codex"` で `list_mcp_resource_templates`。
  - 次は止める:
    - `server = "codex"` で `read_mcp_resource`
    - `server = "codex"` で PAW の Tool の名前（`submit`）
    - `server = "other"`
    - Session の Server が `paw` だけでないときの `server = "codex"` の一覧
  - Probe は今のとおりどの呼び出しも止める（Probe は Tool を渡さない）。
- 通した呼び出しが Bridge の呼び出しの数（`calls_seen`）に入らず、Token の報告の順の数え方（`_last_call`）には入ることの Test。0101 の 3 の `paw` を名指した呼び出しと同じ扱い。

## リスク

- Model が一覧の呼び出しを何度も繰り返し、Token と時間を使う。Error が返るだけで中身はないので、Turn の上限と Node の時間の上限（今のまま）で止まる。Smoke と本番の Log（Bridge の 1 行）で頻度を見る。
- 将来の版が、`server` を省いた一覧の呼び出しの宛先を設定外に広げる（例: 組み込みの Server を Feature なしで足す）。0101 の 4 の版の確かめ（Feature の照合と Smoke）で、版ごとに見直す。
- 1 の A のあと、`gpt-6-sol` がほかの種類の Item（例: `plan`）で止まることがありうる。#356・#357 の記録で名前が分かるので、そのときは新しい Decision で扱う。

## 承認後の扱い

承認されたら、Status を Approved とし、承認の内容を Approval の行と末尾の「承認時の決定」に記録する。README の表に 1 行を足し、0101 の行は書き換えない。
方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。

## 承認時の決定（2026-11-26）

Human は、Claude Code の作業 Session で PR #358 の 3 つの判断点（推奨つき）を受け、すべてを推奨どおり A と回答して承認した。

1. **どの呼び出しを通すか**: A。`mcpToolCall` で `server == "codex"` かつ `tool` が `list_mcp_resources` か `list_mcp_resource_templates` なら通す。PAW の Tool の呼び出しの順の数え方に入れ、Bridge の呼び出しの数には入れない。`server = "codex"` の `read_mcp_resource`、`paw` でも `codex` でもない宛先は、今のとおり止める（1）。
2. **守りの条件**: A。1 の例外は、Session の設定で MCP の Server が `paw` だけ（`["paw"]`）のときに限る。`CodexStream` に Session の Server の一覧を渡し、そうでなければ今のとおり止める（2）。
3. **確かめ方**: A。実装の PR で記録用の Server の Smoke に `server` なしの `list_mcp_resources` の手順を足し、3 の 3 点を確かめて結果を `/data/results/` に残す。その後 Human が `live_check_codex.py` を `gpt-6-sol` で走らせ、合格したら 0105 の PR の分け方の 4 に進む（3）。

### 3 の 3 点目の確かめの結果と Human の選択（2026-11-26）

実装の PR（[#359](https://github.com/TomokiAkiyama06/personal-ai-workspace/pull/359)）で、記録用の Server の Smoke（`smoke_codex_recorder.py --serverless-list`、`codex-cli 0.156.1`、`gpt-6-sol`、`127.0.0.1`、Dummy の Login。OpenAI には何も送っていない）を走らせた。証拠: `/data/results/paw-codex-serverless-list-2026-11-26/STATUS.md`（1 回目は `run1/`、この選択の後の 2 回目は `run2/`）。

- 3 の 1 点目（Item が `mcpToolCall`・`server = "codex"` で来る）と 2 点目（Bridge の Log に `resources/list` が 1 行だけ、MCP の Server は `paw` だけ）は合格した。
- 3 点目のうち、続く `submit` は Node の結果まで通った。しかし Model に返ったのは Error ではなく、**中身のない空の一覧 `{"resources":[]}`** だった。Codex 0.156.1 は、`server` を省いた一覧の呼び出しでは、各 Server の失敗（Bridge の `Method not found`）を Model に返さず、空の一覧にまとめる。
- 2 の守りの目的はこれでも保たれている: 中身は出ない（Bridge は Resource を出さない）し、宛先は `paw` だけ（Bridge の Log の 1 行）。

Human に聞き、Human は **3 の 3 点目の「Model に Error が返り」を、「Model に Error か、中身のない空の一覧（`resources`・`resourceTemplates` が空）が返り」として合格とする** と答えた。中身のある一覧が返れば、今までどおり不合格とする。上の 3 の本文は書き換えず、この節で補う。新しい Decision は作らない（Human の指示）。
