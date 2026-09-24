# Task Queue・Budget・Loop検知の方針

- Status: Proposed
- Date: 2026-09-24
- Scope: PAW-033 と、Budget・Loop検知・Escalationを使う以降の Issue（PAW-034 Orchestrator、PAW-036 GPU Scheduler など）
- Supersedes: なし
- Approval: 未承認（Humanの承認待ち）

## 背景

[REQUIREMENTS.md](../../REQUIREMENTS.md) は、Task の Budget の Preset の名前（Standard / Long / Unlimited）、
6 種類の上限（max runtime、steps、retries、tool calls、tokens、GPU time）、Loop 検知の必要性、Priority（HIGH / NORMAL / LOW）を定めている。
一方で、**具体的な数値、Loop の閾値、Budget を使い切ったときの遷移は定めていない**
（要件は「実装時の選択」としている）。

PAW-033 の実装は、動かすためにこれらを仮の値で置いた。Review（Codex）は、
承認前の Product Policy を既定として実装してよいのかを指摘した。
[AGENTS.md](../../AGENTS.md) の「仕様変更」は、重要判断を `docs/decisions/` に提案して人間 / Admin の承認を得ると定める。
そこで、仮に置いた値と選択を一覧にし、承認または変更を求める。

**この Decision は Proposed であり、Human の承認を得ていない。** 承認されるまで、次の値と選択は暫定である。
値は `domain.PRESET_LIMITS` と `LoopPolicy` のデータで、変更しても Schema は変わらない（Migration は不要）。
承認された値が変わる場合は、新しい Decision から `Supersedes` する。

実装は [Backend README](../../apps/backend/README.md) の「Task Queue / Budget / Loop 検知」に書いている。

## 提案

### 1. Preset の数値

| 種類 | Standard | Long | Unlimited |
| --- | --- | --- | --- |
| `runtime_seconds` | 3,600（1 時間） | 14,400（4 時間） | 上限なし |
| `steps` | 50 | 200 | 上限なし |
| `retries` | 10 | 20 | 上限なし |
| `tool_calls` | 300 | 1,200 | 上限なし |
| `tokens` | 1,000,000 | 4,000,000 | 上限なし |
| `gpu_seconds` | 3,600 | 14,400 | 上限なし |

- 上限ちょうどまで使うのは超過ではない（`消費量 + 予定 > 上限` が超過）。
- Preset を設定していない Task は、無制限とみなさず、エラーにする。
- 警告の閾値は要件にないため置かない（`WARN` はない）。

### 2. Unlimited

- 6 つの数値の上限を無くすだけとする。Loop 検知、Stop Now、Critical safety / resource protection による停止は Preset と無関係で、Unlimited の Task にも効く。
- 要件が Unlimited に別の数値の上限（たとえば絶対の Runtime）を定めていないため、設けていない。**設けるかは人間が決める。**

### 3. Loop 検知の閾値

- 直近 10 件（`window_size`）のうち、最後の失敗と Signature も試行番号（`approach`）も同じ件数が 3 回（`repeat_threshold`）以上で Loop とする（連続でなくてよい）。
- Loop のとき、`approach` が 1（`max_alternatives`）未満なら代替を試し（`TRY_ALTERNATIVE`）、そうでなければ上位の Agent へ渡す（`ESCALATE`）。
- Signature は、Error class・Step・正規化した Message（先頭 2,000 文字、NFKC、小文字化、数字と ID の置換）の Hash とする。Message の原文は保存しない。
- Loop の検知だけでは Task を `failed` にしない。

### 4. Budget を使い切ったときの遷移

`decide_next_action` は、次の順で最初に当てはまる規則を使う。

1. Budget が `EXCEEDED`: `retries` が超過した種類に含まれれば `FAIL`、それ以外は `WAIT_FOR_USER`（安全な区切りで停止し、人間が上限を上げるか終了する）。**Loop の判定より優先する**（使い切った予算をさらに使う Escalation はしない）。
2. Loop が `ESCALATE`: 上位の Agent を使えれば `ESCALATE_AGENT`、なければ `WAIT_FOR_USER`。
3. Loop が `TRY_ALTERNATIVE`: `TRY_ALTERNATIVE`。
4. それ以外: `CONTINUE`。

Command（PAW-032 の `wait` / `fail`）を発行するのは Orchestrator（PAW-034）で、PAW-033 は発行しない。

### 5. Queue の飢餓

- `HIGH` > `NORMAL` > `LOW`、同じ優先度では先着順とする。要件に Aging / 飢餓防止の規則がないため**置いていない**。`HIGH` / `NORMAL` が続く間、`LOW` は待ち続ける。
- 優先度は開始の順序にだけ影響し、実行中の Entry は中断しない（Preemption は Queue の責務ではない）。

## 選定理由

- 数値は、1 台の GPU Server で個人〜小規模チームが使うことを想定した、桁を合わせるための仮の値であり、実測に基づかない。
  Benchmark（PAW-016 / 017）と実運用の記録で見直す前提で、データとして 1 か所に置いた。
- Budget 超過を Loop より優先するのは、Escalation が予算を追加で消費するため。

## 代替案

- 数値を Preset に固定せず、Admin が設定する: Preset の名前を要件が定める以上、まず既定の値が要る。設定 UI と認可は別の Issue。
- 超過時にすべて `FAIL` にする: 人間が上限を上げて続けられなくなる。`retries` だけを `FAIL` にした。
- Aging を入れる: 要件に規則がなく、`LOW` の待ち時間の上限を決める必要がある。

## 承認後の扱い

承認された値と選択を、この Decision の `Approval` に記録して Status を Approved に改める。
値が変わる場合は `domain.PRESET_LIMITS` / `LoopPolicy` のデータと Test の期待値を、承認された値に合わせる。
承認されるまで、この値を前提にした運用（Budget の上限に頼る自動停止の設計など）をしない。
