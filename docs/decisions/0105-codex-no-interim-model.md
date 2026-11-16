# Codex の当面の既定の Model（Decision 0104 の 4）を置かず、Catalog の上書きを先に入れて `gpt-6-sol` を確かめる（ChatGPT の Login では `gpt-5.5` が使えなかった。当面の本番、PR の順、Catalog の出どころ）

- Status: Proposed
- Date: 2026-11-16
- Scope: Issue [#341](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/341)（[Decision 0104](0104-codex-model-catalog-override.md) の PR の分け方の 2 の Script を Human が本物で走らせて分かった）。この Decision は設計だけで、コードは承認の後の下の「PR の分け方」の PR で書く。関係する既存の口: `apps/backend/deploy/cli-session/live_check_codex.py`（#353）、`agent_models/verified.py`（`VERIFIED_MODELS`。Codex の組は空）、`agents/codex_cli.py`（`session_config`・`probe_config`）。
- Supersedes（一部。下の推奨どおりに承認されたとき）:
  - [Decision 0104](0104-codex-model-catalog-override.md)（Approved）の 4（当面の既定の Model を `gpt-5.5` にする）と、PR の分け方の 3（`(0.156.1, gpt-5.5)` を足す PR）。
  - 次は書き換えず、その中で作る: 0104 の 1・2・3・5（上書きの 5 つの値、Catalog の置き場所、本番の外での確かめ、Probe にも当てる）と PR の分け方の 4・5。[Decision 0101](0101-codex-mcp-resource-tools-exception.md)（許す Tool は広げない）、[Decision 0103](0103-selectable-cli-agent-models.md)（検証した組の Gate）。[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。

## 背景

2026-11-16 に Human が、確かめ専用の ChatGPT の Login（本番の Login・`~/.codex` とは別）で `live_check_codex.py`（`codex-cli 0.156.1`、`--model gpt-5.5`）を走らせた。証拠: `/data/results/paw-codex-live-check-2026-11-16/STATUS.md` と `gpt-5.5.json`。

- Probe は `unavailable`（`internal_error`、Turn の失敗、2.4 秒）で、Node は走らなかった（`probe_not_connected`）。Login の更新はなく、後片付けは済んだ。
- 同じ Login で Human が `codex exec -m gpt-5.5` を走らせると、OpenAI は 400 `model_not_found`「The 'gpt-5.5' model is not supported when using Codex with a ChatGPT account.」を返した。
- Login した状態の `codex debug models`（Server から取った Catalog）は `gpt-6-astra`・`gpt-6-sol`・`gpt-6-luna`・`gpt-5.6-sol`・`gpt-5.6-terra`・`gpt-5.6-luna`（一覧に出る）と `gpt-reserve`・`codex-auto-review`（隠れる）だけで、**すべて `tool_mode = code_mode_only`**。Login なしの Catalog（CLI に組み込み）にある `gpt-5.5`・`gpt-5.4` は無い。

つまり、この Account で使える Model に Code Mode の無いものは無く、0104 の 4 の A（上書きなしで合格する `gpt-5.5` を当面の既定にする）は実現できない。0104 の 4 の前提（`gpt-5.5` は上書きなしで合格する）は記録用の Server の Smoke（Dummy の Login）で確かめたもので、本物の Account で使えるかは確かめていなかった。

今の本番: Release は #350 の前（20261108-67e2fecae8d2）、`[agents.codex].model = gpt-6-sol`、`[orchestrator]` は step 5（Codex を割り当てる）。Human の判断（2026-11-12）で #350 を含む Release は Codex の検証した組ができるまで入れない。本番の Codex の Session は `additional_tools` で `exec` などを渡しており、0101 の検査はそれを見ていない（0104 の背景）。

## 判断点（Human に答えてほしいこと）

各点に案と推奨を置く。番号で「1 は A」のように答えてほしい。

### 1. 当面の既定の Model と本番

- **A（推奨）: 当面の既定の Model は置かない。本番は今のまま（#350 の前の Release、`gpt-6-sol`、step 5）で、0104 の PR の分け方の 4（上書きの実装）と 5（`gpt-6-sol` の組）を先に進め、組が入った Release を入れる。** 本番の Codex の Session が 0101 の外の Tool を渡されている状態は、上書きの Release まで続く（2026-11-12 の Human の判断のまま）。
- B: 今すぐ本番で Codex の割り当てを止める（`[orchestrator]` を Codex なしにする）。悪い点: 0099 の 5 で Codex の Review は必須なので、`runtime-config-check` が通る形（2026-11-12 の step 3: すべて Local）にすると Claude も使わなくなる。
- C: #350 を含む Release を今入れ、Codex は送る前に止まる（Fail closed）。悪い点: 書き込む Task はすべて失敗する。

### 2. Catalog の出どころ（0104 の 2 の補い）

Login した Codex は Catalog を Server から取り、その中身は組み込みの Catalog と違う（`gpt-5.5` が無く `gpt-reserve` がある）。`gpt-6-sol` の項目の他の値（`base_instructions`・`context_window` など）も違うかもしれない。

- **A（推奨）: Repository に置く Catalog は、0104 の 2 のとおり Login なしの `codex debug models` の出力とする。ただし実装の PR（0104 の 4）の前に、Human が確かめ専用の Login で `codex debug models` を 1 回取り（Model は呼ばない。Login の更新が起きても同じ File に残る）、`gpt-6-sol` の項目を組み込みのものと比べる。上書きの 5 つ以外に違いがあれば、その違いを PR に書き、Server の値を使うかを Human に聞く。** 比べた結果は `/data/results/` に置く。
- B: Login した状態の Catalog をそのまま Repository に置く。悪い点: Account によって中身が変わりうる。Login なしでは作り直せない。

### 3. 確かめの Script の既定

- **A（推奨）: `live_check_codex.py` は今のまま（上書きは 0104 の 4 の PR で Backend と同じものを使うように変える）。** README の「まず `gpt-5.5` で」の記述を、0104 の 4 の PR で「`gpt-6-sol` で」に直す。

## PR の分け方

1. この Decision（docs だけ）。
2. （2 が A のとき）Human が Login した状態の Catalog を取り、`gpt-6-sol` の項目を比べる（PR ではない。結果を `/data/results/` に置く）。
3. 0104 の 4: Backend の上書きの実装、版ごとの Catalog の File、Smoke と `live_check_codex.py` の対応、README の直し、Test。検証した組はまだ足さない。
4. Human が 3 の入った Script を `gpt-6-sol` で走らせ、合格した後: `(0.156.1, gpt-6-sol)` を検証した組に足す PR（0104 の 5）。Release の後、本番の Probe で事後に確かめ、Admin が Policy で許す。

## リスク

- 上書きの Release まで、本番の Codex は 0101 の外の Tool（`exec`・`spawn_agent` など）を渡されたまま動く。Codex の Session は Container と Egress（0091）の中で動くが、その中で `exec` などが何をできるかは測っていない。心配なら 1 の B を選ぶ。
- Code Mode を止めた `gpt-6-sol` の質は、0104 の 3 の確かめまで分からない。駄目なら Codex の Model が無くなる（そのときは新しい Decision で、Code Mode を安全に使う形か API Key の Login を考える）。

## 承認後の扱い

承認されたら、Status を Approved とし、承認の内容を Approval の行と末尾の「承認時の決定」に記録する。README の表に 1 行を足し、0104 の行は書き換えない。
方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。
