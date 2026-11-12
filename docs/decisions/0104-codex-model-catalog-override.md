# Codex の Catalog の Model（`gpt-6-sol` など）を、PAW が書く Model の Catalog（`model_catalog_json`）で Code Mode と Multi-agent を止めて使う（上書きする項目、Catalog の置き場所、本物での確かめ、当面の既定の Model、PR の分け方）

- Status: Proposed
- Date: 2026-11-12
- Scope: Issue [#341](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/341)（[Decision 0103](0103-selectable-cli-agent-models.md) の PR の分け方の 5「本番での確かめ」で見つかった）。この Decision は設計だけで、コードは承認の後の下の「PR の分け方」の PR で書く。関係する既存の口: `apps/backend/paw_backend/agents/codex_cli.py`（`session_config`・`DISABLED_FEATURES`）、`agents/codex_probe.py`（`probe_config`）、`agent_models/verified.py`（`VERIFIED_MODELS`。#350 で入り、Codex の組は今は空）、`deploy/cli-session/smoke_codex_recorder.py`（`additional_tools` を記録して不合格にする）、`deploy/cli-session/Containerfile.codex`。
- Supersedes: なし。次は書き換えず、その中で作る: [Decision 0101](0101-codex-mcp-resource-tools-exception.md)（Tool の検査の規則。**許す Tool は広げない**）、[Decision 0103](0103-selectable-cli-agent-models.md) の 3（検証した組を足す手順）と 10（本番の暫定の既定 `gpt-6-sol`。下の 4 で当面の既定を提案する）、[Decision 0100](0100-cli-connection-health-probe.md)（Probe は Tool を何も渡さない）、[Decision 0099](0099-claude-planner-and-node-assignment.md) の 5（Codex の Review は必須）。[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。

## 背景

2026-11-08〜11-12 の記録用の Server の Smoke（`127.0.0.1` の Provider と Dummy の `auth.json`。OpenAI には何も送っていない。証拠: `/data/results/paw-codex-catalog-2026-11-12/STATUS.md` と各 Run の JSON）で、次が分かった。

- `codex-cli 0.156.1` の Model の Catalog（`codex debug models`）では、一覧に出る Model のうち `gpt-6-astra`・`gpt-6-sol`・`gpt-6-luna`・`gpt-5.6-*` がすべて `tool_mode = "code_mode_only"`、`multi_agent_version = "v2"`（`gpt-5.6-luna` は `v1`）、`use_responses_lite = true`。Code Mode が無いのは古い `gpt-5.5`（と隠れた `gpt-5.4`）だけ。
- `gpt-6-sol` の Node の Session では、Codex は Tool を `tools` ではなく `additional_tools` の Input の Item で渡し、その中身は `functions` の namespace の **`exec`（V8 で JavaScript を走らせ、他の Tool をまとめて呼ぶ）・`wait`・`request_user_input_async`**、`collaboration` の namespace の **`spawn_agent`・`send_message`・`followup_task`・`wait_agent`・`list_agents`・`interrupt_agent`**、そして `mcp__paw` の `submit`。0101 の許す集合の外で、`DISABLED_FEATURES` の `multi_agent=false`・`code_mode_host=false` でも消えない（Catalog の値が Feature より優先される）。
- **本番の Probe（0100）も同じ**: `gpt-6-sol` の Probe の Request は `exec` と `collaboration` の Tool を渡している。0100 の「Tool を何も渡さない」を満たしていない（本番の Probe の判定は答えの文だけを見るので `connected` になっていた）。
- Codex には Catalog を File で置き換える設定 `model_catalog_json`（`config.toml` の Top-level の Key）がある。`codex debug models` の出力を元に `gpt-6-sol` の項目の **5 つの値** を変えた Catalog を渡すと、Node の Session の `tools` は `list_mcp_resources`・`list_mcp_resource_templates`・`read_mcp_resource`・`mcp__paw__submit` だけになり（0101 の検査に合格。`submit` は Node の結果まで通る）、Probe の Request は Tool なしになった（0100 に合格）。5 つのうち 1 つでも残すと不合格（下の 1 の表）。
- `gpt-5.5` は Catalog を変えずに Node も Probe も合格する。
- Codex は、Code Mode を持つ Model の Code Mode を止めると「Model の性能が落ちることがある」と警告している。**本物の Backend での品質（Review の質・失敗の率）はまだ測っていない。** 記録用の Server は Tool の一覧しか確かめられない。

このままでは Catalog の Model は 0101 の検査を通らず、#350 の検証した組（Codex は空）で Codex の Session はすべて送る前に止まる。0099 の 5 で Codex の Review は必須なので、#350 を含む Release を入れると書き込む Task はすべて失敗する（Human の回答 2026-11-12: Codex の扱いが決まるまで #350 は Deploy しない）。

## 判断点（Human に答えてほしいこと）

各点に案と推奨を置く。番号で「1 は A」のように答えてほしい。

### 1. Catalog の Model をどう通すか

- **A（推奨）: PAW が書く Catalog で、Code Mode と Multi-agent と余分な Tool を止める（許す Tool は広げない）。** Session（Node と Probe）の `config.toml` に `model_catalog_json` を書き、その Catalog の対象の Model の項目だけ次の 5 つを上書きする。他の項目（`base_instructions`・`context_window`・Reasoning の段など）は CLI の Catalog のまま。

  | 項目 | 元（`gpt-6-sol`） | 上書き | 残すと出る Tool（Smoke） |
  |---|---|---|---|
  | `tool_mode` | `code_mode_only` | `direct` | `exec`・`wait`（`additional_tools`） |
  | `multi_agent_version` | `v2` | なし（`null`） | `collaboration__*`（v2）、`multi_agent_v1__*`（v1） |
  | `experimental_supported_tools` | `["send_user_message_async", "clock"]` | `[]` | `request_user_input_async`・`clock__curr_time` |
  | `use_responses_lite` | `true` | `false` | Tool が `additional_tools` の形で来る |
  | `supports_search_tool` | `true` | `false` | `tool_search` |

  - 0101 の検査（許す集合）・Smoke の `additional_tools` の不合格・Item の検出は **そのまま**。上書きは「余分な Tool を出させない」手段で、検査を緩めるものではない。
- B: Code Mode をそのまま使い、0101 の許す集合に `exec`・`wait` などを足す。悪い点: `exec` は任意の JavaScript を走らせ他の Tool をまとめて呼ぶので、Tool の呼び出しの 1 回ごとの検査（0083 の Tool Broker、0089 の 2 の Item の検出）の外に出る。`spawn_agent` は PAW の外で Agent を増やす。許す範囲が大きく広がり、0101 を Supersede する新しい議論が要る。
- C: Catalog を変えずに通る Model だけ使う（今は `gpt-5.5` だけ）。悪い点: 古い Model に固定され、0103 の「選べる」の意味がほぼ無くなる。新しい版で `gpt-5.5` が消えれば Codex が使えなくなる。

### 2. Catalog の置き場所

- **A（推奨）: CLI の版ごとの Catalog（`codex debug models` の出力、Login なし）を Repository に置き（例: `apps/backend/deploy/cli-session/codex-models-0.156.1.json`）、Backend が Session ごとに上書きを当てた File を `CODEX_HOME` に書いて `model_catalog_json` で指す。** 版を上げる PR で、その版の Catalog を足す（Review で差分が見える）。上書きする 5 つの項目は Code の定数で、単体の Test を付ける。上書きした結果に Code Mode の値が残っていれば Session を始めない（Fail closed）。
- B: Image の Build の時に `codex debug models` を Image の中に書き、Session の始めに Container の中で上書きする。悪い点: 中身が Review に出ず、Build の時の版の差がそのまま入る。
- C: Model の項目を Code に手で書く（Catalog の一部だけ）。悪い点: `base_instructions` などの長い値を写し間違えやすい。

### 3. 検証した組に足す前の本物での確かめ

- **A（推奨）: 記録用の Server の Smoke（Node と Probe）に加え、本物で 2 つ確かめてから組を足す。** (1) 本番の Probe（Human が Release を入れた後、上書きの Catalog つき）が `connected`。(2) Human の了承のうえで、小さな書き込む Task を 1 つ流し、Codex の必須の Review が Tool の検査の失敗なしに終わり、結果が読める（品質は Human が目で見る）。どちらかが駄目なら組に入れない。
- B: 記録用の Server の Smoke と本物の Probe だけで足す。悪い点: Code Mode を止めた時の質の劣化を見ないまま本番の Review を任せる。

### 4. 当面の既定の Model（上書きの実装が入るまで）

- **A（推奨）: `(0.156.1, gpt-5.5)` を先に検証した組に足し、本番の `[agents.codex].model` を `gpt-5.5` にする。** Catalog を変えずに Smoke（Node と Probe）が通ったので、上書きの実装を待たずに #350 を含む Release を入れられ、本物の Probe と小さな Task での確かめ（3 の A）もこの組で先にできる。上書きが入り `gpt-6-sol` が 3 を通ったら、Admin の Policy（0103 の 2）で既定を `gpt-6-sol` に変える（再起動なし）。
- B: 上書きの実装と `gpt-6-sol` の確かめが終わるまで #350 を含む Release を入れない（本番の Codex は今の `gpt-6-sol` のまま、`additional_tools` は検査されないまま）。
- C: 上書きが入るまで Codex を本番で使わない。悪い点: 0099 の 5 で必須の Codex の Review のため、assignment がある限り外せない（2026-11-12 に確かめた: Reviewer の `allowed` は claude と codex の両方が必須）。外すには ladder をすべて Local に戻すしかない。

### 5. Probe（0100）にも同じ上書きを当てるか

- **A（推奨）: 当てる。** Probe は「Node が使う既定の Model と Credential の組」を確かめる（0103 の 5）ので、Node と同じ Catalog で走らせる。上書きなしでは Probe の Request に `exec` などが乗り、0100 の「Tool を渡さない」を満たさない。
- B: Probe は上書きしない。悪い点: 上のとおり 0100 に合わない。

## Test の方針（実装の PR）

- 上書きの単体の Test: 元の Catalog の `gpt-6-sol` から 5 つの値が上書きされ、他の項目は変わらない。Catalog にない Model は Session を始めない。上書きの後に `code_mode*`・`multi_agent_version` が残れば Fail closed。Catalog の File が読めない・形が違うときも Fail closed。
- `session_config` と `probe_config` の Test: `model_catalog_json` が Session の `CODEX_HOME` の中の File を指し、その File が Session の終わりに消える。
- 記録用の Server の Smoke（`smoke_codex_recorder.py`）: Backend と同じ上書きで走る（Smoke 専用の別の上書きを持たない）。`gpt-6-sol` の Node と Probe、`gpt-5.5` の Node と Probe が合格し、上書きを外すと `gpt-6-sol` が不合格になる。

## PR の分け方

1. この Decision（docs だけ）。
2. 4 が A のとき: `(0.156.1, gpt-5.5)` を `VERIFIED_MODELS` に足す（Smoke の JSON の Path を PR に書く）。本番の `model` を `gpt-5.5` にする Human の手順（Script）。
3. Backend: 版ごとの Catalog の File、上書きの定数と Session・Probe への `model_catalog_json`、Smoke の Script の対応、Test。検証した組はまだ足さない。
4. 3 が入った Release で本物の確かめ（3 の A）をした後: `(0.156.1, gpt-6-sol)` を検証した組に足す PR。Admin が Policy で許し、既定を変える。

## 未決（この Decision では決めない）

- `gpt-6-astra`・`gpt-6-luna` の組（同じ上書きで、それぞれ 3 の確かめをしてから足す）。
- Codex の版を上げたときに Catalog の項目の意味が変わる場合（版を上げる PR で、その版の Smoke が上書きの後も 0101 を通ることを確かめる。通らなければ新しい Decision）。
- Code Mode を安全に使う形（`exec` の中の Tool の呼び出しを 1 回ずつ検査できるなら）。必要になれば新しい Decision。

## リスク

- Code Mode を止めると Model の質が落ちることがある（Codex の警告）。3 の A の本物での確かめで見る。`gpt-5.5` を当面の既定にする 4 の A は、この危険を避ける。
- 本物の Backend が Catalog の上書きと違う形の Request を受け付けない、または Login のとき Codex が Catalog を Server から取り直して上書きを無視する。Probe の Request の Tool は本番では見えないので、記録用の Server の Smoke・本物の Probe の結果・Item の検出（0089 の 2）で見る。
- 版を上げると Catalog の項目が増え、新しい項目で Tool が足される。版を上げる PR で Smoke をやり直す（0101 の 5 のとおり）。

## 承認後の扱い

承認されたら、Status を Approved とし、承認の内容を Approval の行と末尾の「承認時の決定」に記録する。README の表に 1 行を足す。
実装は「PR の分け方」の 2〜4 の PR で行う（どれも「Refs #341」）。本番の `runtime.toml` の変更と Release は Human が行う。
方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。
