# Claude の CLI の Connection の Probe の固定の Prompt の文言を、Safeguard に止められにくい平易な文にし、Model の Fallback をした Probe を `connected` にしない（文言、Fallback の記録の場所、Fallback した Probe の outcome、Probe 専用の Model）

- Status: Approved
- Approval: 2026-11-05、Humanが作業Session（Claude Code）内で、PR [#330](https://github.com/TomokiAkiyama06/personal-ai-workspace/pull/330) の本文の「Human に確かめてほしいこと」の 4 つの質問（推奨つき）を受けたうえで、4 つすべてを推奨どおりと回答して承認（approved as recommended, all 4 points: 1 は文言を変える（新しい Decision で 0100 の 2 を Supersede）、2 は記録はまず Log だけ（Schema を変えない）、3 は Fallback した Probe は `connected` にせず、状態を変えない `unavailable`（新しい FailureCode `model_fallback`。`expired` にはしない）、4 は Probe 専用の Model を使わない。末尾の「承認時の決定」）
- Date: 2026-11-05（承認 2026-11-05）
- Scope: Issue [#325](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/325)（本番の Probe で、設定の Model（Opus 5.5）が Safeguard の `[cyber]` の拒否で止められ、CLI が `claude-opus-4-8` へ Fallback して答えた）。PR #330（main に 41fd59e で Merge 済み）は Fallback を読んで WARNING の 1 行を出すだけで、判定は変えていない。関係する既存の口: `apps/backend/paw_backend/agents/claude_probe.py`（`PROBE_SYSTEM_PROMPT`・`PROBE_PROMPT`・`PROBE_ANSWER`・`_read`・`_report`）、`agents/claude_stream.py`（`ClaudeStream.fallbacks`・`safety_stops`・`fell_back`・`fallback_note`）、`connections/domain.py`（`FailureCode`）、`connections/models.py`（`connection_usage` と `connection_probes` の `failure_code` の Check 制約。どちらも `FailureCode` の全部から作る）、`connections/probe.py`（状態を変えない結果）、`migrations/versions/0306_connection_probes.py`（`FAILURE_CODES` を書き出した Check 制約）。
- Supersedes（一部）:
  - [Decision 0100](0100-cli-connection-health-probe.md)（Approved）の 2 の「推奨（固定の Prompt）」の **System Prompt と標準入力の文そのもの** を、下の 1 の文に置き換える。0100 の 10 の「Codex も Claude と同じ文」は、下の 1 の新しい文を指す。0100 の 2 の他の部分（起動・Flag・受け入れる答え `PAW_PROBE_OK`・`expired`・Timeout）と「どちらにも Task・Project・User・Repository・時刻・Host 名・Connection の ID を入れない」は **変えない**。
  - 0100 の 2 の「推奨（受け入れる答え）」と 0100 の 5 の「推奨（状態を変えない結果）」に、下の 3 の **`model_fallback`** を足す（`connected` の条件に「Fallback がない」を足し、Fallback は状態を変えない `unavailable` にする）。
  - 次は書き換えず、その中で作る: 0100 の 3（`connection_probes`。下の 2 で列を足さない）・9（Audit に Model 名を書かない）、[Decision 0087](0087-cli-secret-store-and-egress.md) の 4（Credential の渡し方）。[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。

## 背景

- 本番の Probe（Issue #325）で、CLI の stream に `{"type":"system","subtype":"model_refusal_fallback","original_model":…,"fallback_model":…}`・`assistant` の `{"type":"fallback",…}` の block・`result` の `safety_stops: 2` と 2 つの Model の `modelUsage` が出た。設定の Model が Safeguard（`[cyber]`）で止められ、CLI が古い Model で答え直していた。
- 0100 の 2 の固定の文（`You are a connectivity check for a backend service. Do not use any tools. Reply with exactly the single line PAW_PROBE_OK and nothing else.` / `Reply with exactly: PAW_PROBE_OK`）は、"connectivity check"・"backend service"・"Do not use any tools" のように、Safeguard が攻撃の偵察と見やすい語を含む。
- Probe は「Node が使う設定の Model と Credential の組が通る」ことを確かめる物（0100 の 1・2）である。Fallback 先の Model が答えたことは、設定の Model が使えることを示さない。一方で Credential は有効なので、`expired` は当たらない。
- 0100 の 2 の文は Decision の本文の一部で（承認は「all 7 points approved as recommended」）、リスクの節も「固定の文が変わらないことを Test で固定する」としている。よって文言の変更は [AGENTS.md](../../AGENTS.md) の 14 に従い、この Decision で行う。

## 提案

### 1. 固定の Prompt の新しい文（0100 の 2 の文を置き換える）

- **System Prompt**: `Answer the user's message briefly.`
- **標準入力（User の Turn）**: `Please reply with the word PAW_PROBE_OK.`
- 受け入れる答えは今のまま `PAW_PROBE_OK`（部分文字列、大文字・小文字を区別。0100 の 2 の 4）。System Prompt は答えの語を含まない（標準入力だけが含む）。
- 文から除く語: "connectivity"・"check"・"backend"・"service"・"tool" とその言い換え（Probe の目的・内部の構成・Tool の有無を Model に告げない）。Tool がないことは Flag（`--tools ""`・空の MCP の設定・`--strict-mcp-config`）と `system/init` の検査（0100 の 2 の 1）で保たれ、文に頼らない。
- どちらも Backend の定数で、設定・User・Agent から変えられない。どちらにも Task・Project・User・Repository・時刻・Host 名・Connection の ID を入れない（0100 の 2 のまま）。Test は **文そのもの** を固定し（この Decision の文と同じであること）、除いた語がないこと・答えの語が標準入力にあることを確かめる。
- Codex の Probe（0100 の 10、#305 の後）も同じ 2 つの文を使う。

### 2. Fallback の記録の場所: 今は Log だけ

- PR #330 の WARNING の固定の 1 行（`Claude probe model fallback: session=<uuid> configured=<m> used=<m,...> fallbacks=<a>-><b> safety_stops=<n>`。Node は `Claude session model fallback: …`）だけにする。`connection_probes`・`connection_usage`・Audit の列と Event は足さない（Schema は変えない。0100 の 3・9 のまま）。`ProbeReport.model` と `connection_probes.model` は設定の Model のまま。
- まず Log で頻度を見る。続くようなら、新しい Decision で `connection_probes` に `fallback_model`（NULL 可）を足すかと、Node の側（`connection_usage`）の扱いを決める（未決）。

### 3. Fallback した Probe の outcome: 状態を変えない `unavailable`（`model_fallback`）

- **新しい `FailureCode`**: `model_fallback`（「設定の Model が止められ、別の Model が答えた・答えようとした」）。Probe だけが使う。Node の Session の結果と `on_usage`・失敗の分類は今のまま（2 のとおり Log だけ）。
- **何を Fallback とするか**: `ClaudeStream.fell_back`（PR #330）が真のとき。すなわち `system/model_refusal_fallback` か `assistant` の `fallback` の block が 1 つでもあるか、`result` の `safety_stops` が 1 以上。`modelUsage` に設定と違う名前があるだけでは Fallback としない（Alias と完全な ID の違いがあるため。#330 と同じ）。
- **判定の規則**（0100 の 2 の「受け入れる答え」の前に置く）:
  1. Probe の読みの間に Fallback の印（system の通知か assistant の block）を読んだら、**その時点で CLI を止め**、`unavailable`（`model_fallback`）にする。後の行（2 回目の Model の呼び出し・答え・`result`）は読まない（Fallback 先の Model に答えを書かせ切らない。Token も抑える）。
  2. `result` の `safety_stops` が 1 以上なら（通知も block もなくても）、他の条件（Model の呼び出しが 1 回・成功・`PAW_PROBE_OK`）を満たしていても `unavailable`（`model_fallback`）。
  3. 先に `expired`（認証の `api_retry`。CLI はその時点で止まる）になった Probe は `expired` のまま。Fallback の印の前に止まった Probe（Timeout・Stream の誤り・2 回目の Model の呼び出し）はその分類のまま。止めた後に読んでいない行は判定に使わない。
  4. 規則 1 で止めた Probe と規則 2 の Probe は、他の分類（`invalid_response`・`internal_error`・`rate_limited`）より `model_fallback` を先にする。
- **今の「ちょうど 1 回」の規則との関係**（PR #330 の時点の振る舞いを確かめた結果）:
  - 本番の形（Fallback の通知 → Model の呼び出しが 1 回 → `fallback` の block → `result` が成功で `safety_stops: 2`）では、呼び出しが 1 回で答えに `PAW_PROBE_OK` があるので **`connected`** になっていた（#330 の `FallbackTest` の `FELL_BACK`）。
  - CLI が止められた呼び出しと Fallback 先の呼び出しの **2 回** を報告すると、2 回目の `message_stop` で CLI を止めて `unavailable`（`invalid_response`）になっていた。`result` は読まれず、`safety_stops` の有無に依らない。通知が 2 回目の呼び出しの前か後かで Log の内容も変わる。
  - 新しい規則では、Fallback の通知は止められた呼び出しの後・Fallback 先の呼び出しの前に出る（Fallback 先の呼び出しは通知の後でしか始まらない）ので、2 回の形でも規則 1 で 2 回目の呼び出しの前に止まり、**`model_fallback`** に決まる。通知のない 2 回の形（CLI が Fallback を告げない版）は今のまま `invalid_response`。
  - 本番の 2026-11-05 09:32（JST）の Probe も `outcome = unavailable` で終わった（この Decision を書いた時点で `failure_code` は未確認）。#330 の後なので、Fallback の 2 回の形なら `invalid_response` のはずで、journal の `Claude probe model fallback` の行で確かめられる。3 の実装の後は、この形も `model_fallback` に決まる。
- **状態を変えない**: `model_fallback` の `unavailable` は、`rate_limited`・`busy`・`cancelled` と同じく `shared_connections.status`・`checked_at` を変えず、`connection_probes` の行の `outcome`（`unavailable`）と `failure_code`（`model_fallback`）にだけ残す（0100 の 5）。理由: Credential は有効で、設定の Model が今その文で止められたことは Credential について何も示さない。`connected` だった Connection を落とさず、`unavailable` だった Connection を `connected` にもしない（新しい・差し替えた Connection は確認まで `unavailable` のまま: 0016 の 8）。
- **繰り返しの抑え**（0100 の 8）: `model_fallback` は失敗として扱い、続けて失敗するたびに間隔を倍にする（`rate_limited` と同じ）。
- **Admin への見え方**: `GET /admin/connections` の `last_probe` の `failure_code` に `model_fallback` が出る。答えの文・Prompt・Model 名は応答に出さない（Model 名は Log の 1 行だけ）。
- **Schema**: `failure_code` は `connection_probes`（Migration 0306 の `ck_connection_probes_failure_code_valid`）と `connection_usage`（Migration 0030 の `failure_code_valid`）の Check 制約が `FailureCode` の全部の値を並べている（`connections/models.py` もそこから作る）。`model_fallback` を足すには Migration が要る。その形は下の「Human に確かめてほしいこと」1 で確かめ、それまで 3 の実装はしない。

### 4. Probe 専用の Model は使わない

- Probe は今のまま `[agents.claude].model` を使う（0100 の 2）。Probe の目的は Node が使う Model と Credential の組が通ることの確認で、別の（軽い）Model では確かめられない。費用は 1 回数百 Token で、0100 の承認時に問題なしと確認済み。

### 5. 採らなかった案

- 文言を変えず Fallback だけを `model_fallback` にする: 本番の形では毎回 `unavailable` になり、Probe が `connected` にできなくなる（#325 の元の問題が Admin に見えるだけで直らない）。
- Fallback した Probe を `connected` のままにする（#330 の振る舞い）: Fallback 先の Model の答えで、設定の Model が使えると誤って示す。
- `expired` にする: Credential は有効で、Admin に差し替えを促す誤った知らせになる。
- 状態を `unavailable` に書く（状態を変える `unavailable`）: Safeguard の判定は文と Model に依って揺れるので、`connected` の Connection を Fallback 1 回で落とすと、走れる Node の Session まで Admission で断る。
- `connection_probes` に `fallback_model` の列を今足す: Log の頻度を見る前の Schema の変更で、0100 の 9（Audit に Model 名を書かない）との関係の判断も要る（2 の未決）。

## Test の方針

- **文言（この Decision の 1）**: `PROBE_SYSTEM_PROMPT` と `PROBE_PROMPT` が 1 の文と **完全に一致** すること。除いた語（`connectivity`・`check`・`backend`・`service`・`tool`。大文字・小文字を問わない）がどちらにもないこと。`PAW_PROBE_OK` が標準入力にあること。今の「Task・User の値の形を含まない」の検査は残す。CLI に渡る `--system-prompt` と標準入力がその文であること（今の Launch の Test）。
- **Fallback（3。Migration の確認の後の PR）**: Fake の CLI で、本番の形（通知 → 1 回 → block → `safety_stops: 2` の成功）→ `unavailable`・`model_fallback`。通知の後に 2 回目の呼び出しがある形 → `model_fallback`（2 回目の呼び出しの前に CLI が止まり、答えと `result` を読まない）。通知も block もなく `safety_stops` だけ 1 以上 → `model_fallback`。`safety_stops: 0` と Fallback のない成功 → 今のまま `connected`。認証の `api_retry` の後の Fallback → `expired`。通知のない 2 回の呼び出し → 今のまま `invalid_response`。Service の Test で `model_fallback` が状態と `checked_at` を変えず、行の `outcome`・`failure_code` に残り、抑えの間隔が倍になること。Migration の Test（上げ下げと Model との一致）。
- 他の Test は弱めない（判定の Test の期待値はそのまま）。

## PR の分け方

1. この Decision と 1 の文言（定数・Test・README）。Refs #325。main（#330 の Merge の後）の上。
2. 3 の `model_fallback`（`FailureCode`・Migration・`_read` と `_report`・状態を変えない扱い・README と Secret Store の README の outcome の一覧・Test）。「Human に確かめてほしいこと」1 の回答の後。Refs #325。
3. 本番での確かめ（Human が新しい文言の Release を入れ、attach か再確認の Probe で `connected` と Fallback の Log の行がないことを見る）。

## 未決（この Decision では決めない）

- Fallback を DB に記録するか（`connection_probes.fallback_model` など）と、Node の Session の Fallback の扱い（2）。Log の頻度を見た後の新しい Decision。
- 新しい文言でも本番で Fallback が続くときの次の手（さらに文を変える・Provider に問い合わせる）。

## Human に確かめてほしいこと（Decision の外の前提）

1. **`model_fallback` の Migration の形**（3 の Schema）: `FailureCode` は `connection_probes` と `connection_usage` の両方の Check 制約の元なので、値を足すと両方の制約が変わる。
   - 推奨: A。新しい Migration（Revision は Issue の番号 `0325`、`down_revision` は今の head、`paw_compatibility = "expand"`）で、両方の表の `failure_code` の Check 制約を落として `model_fallback` を足した一覧で作り直す（値の列は変えない。`downgrade()` は `model_fallback` の行が残っていれば失敗する。開発と Test だけ）。`connection_usage` に `model_fallback` の行は作らない（Node は 2 のとおり Log だけ）が、`FailureCode` を 1 つに保つ。
   - B: Probe だけの別の Enum（`ProbeFailureCode`）を作り、`connection_probes` の制約だけを変える。悪い点: `ProbeReport.failure`・`connection_probes`・`last_probe` の型が `FailureCode` と分かれ、Service と Store の両方に変更が広がる。
2. 新しい文言の Release を本番に入れた後、Probe の journal に `Claude probe model fallback` の行が出なくなったかを見てほしい（3 の PR の前でも後でもよい）。

## リスク

- 新しい文でも Safeguard が止めることがある（Model と Safeguard の判定は変わる）。そのときは 3 の後なら `model_fallback` で Admin に見え、状態は変わらない。
- System Prompt から「1 行だけ」「Tool を使うな」の指示を除くので、Model が `PAW_PROBE_OK` の前後に文を足すことがある。判定は部分文字列なので通る（0100 の 2 の 4）。Tool は Flag と `system/init` の検査で使えない。
- 3 の規則 2 で、Fallback ではない理由の `safety_stops` も `model_fallback` になる。設定の Model が止められたことに変わりはないので、`connected` にしないのは保守的な向きである。
- 3 の実装までの間（1 の PR だけが入った状態）は、Fallback した Probe は今のまま `connected` になりうる（Log の 1 行で分かる）。

## 承認後の扱い

2026-11-05 に承認され、Status を Approved とし、承認の内容を Approval の行と「承認時の決定」に記録した。Supersede する 0100 の本文は書き換えず、この Decision が 0100 の 2 の文言と、2・5 の一部（`model_fallback`）を Supersede することを README の表に記した。
実装は「PR の分け方」の 1・2 の PR で行う（どちらも「Refs #325」）。本番の Release は Human が行う。
方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。
[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。

## 承認時の決定（2026-11-05）

Human は、Claude Code の作業 Session で PR #330 の 4 つの質問（推奨つき）を受け、4 つすべてを推奨どおりと回答して承認した。文言の最終の文（1）は、Human の示した例（System Prompt `Answer the user's message briefly.`、標準入力 `Please reply with the word PAW_PROBE_OK.`）をそのまま採った。

1. **Probe の文言**: 変える。0100 の 2 の文を 1 の文に置き換え、答え `PAW_PROBE_OK` の判定は変えない（1）。
2. **Fallback の記録の場所**: 今は Log だけ。Schema は変えない（2）。
3. **Fallback した Probe の outcome**: `connected` にせず、状態を変えない `unavailable`（新しい FailureCode `model_fallback`）。`expired` にはしない。2 回の Model の呼び出しの形も `model_fallback` に決まるようにする（3）。
4. **Probe 専用の Model**: 使わない（4）。
