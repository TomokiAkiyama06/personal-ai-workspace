# Shared Connection の呼び出しを Queue の Lease で Fencing し、Node の計上は Lease で Fencing しない（Decision 0046 の 6 の追補）

- Status: Approved
- Date: 2026-09-29
- Scope: Issue [#153](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/153)（[Decision 0046](0046-tool-call-lease-fencing.md) の「判断が必要な点」6 で別の Issue に回したもの）。Shared Connection（PAW-030、`paw_backend/connections/`）の `ConnectionService.execute`、Orchestrator（PAW-034）の `NodeBudgetHandle.charge`、GPU 時間の遅い計上（PAW-036、`TrackerLateGpuCharge`）
- Supersedes: なし。[Decision 0046](0046-tool-call-lease-fencing.md)（Approved）の 6 への**追補**（Amends）で、0046 は書き換えない。[Decision 0016](0016-shared-connection-adapter-policy.md)（`execute` の手順）、[Decision 0021](0021-dag-orchestrator-policy.md)（7 節の予算、8 節の Fencing）、[Decision 0037](0037-gpu-compute-scheduler.md) の 10（GPU 時間の計上）と [Decision 0050](0050-late-gpu-charge-without-fence.md)（Fence なしの遅い計上）は変えない
- Approval: 2026-09-29、Humanが作業Session内で、判断点ごとの説明（推奨つき）を受けたうえで「推奨どおり」と回答して承認（1〜8 の全点。末尾の「承認時の決定」）

## 背景

Decision 0046 は、Tool Broker を通る全ての Tool 呼び出しを Queue の Lease（`QueueLease` の `claim_count`）で Fencing した（`TaskContext.lease`、`LeaseVerifier` / `QueueLeaseVerifier`、Broker の 5a、`lease_lost` で Run を止め、`lease_unavailable` でその呼び出しだけを拒否する）。確認は判定の時点の読み取りで、確認の後の実行は Fencing しない（0046 の 3、承認済み）。並列の Node の一方が Lease の喪失を見つけた後に他方が書く結果は、Human が残リスクとして受け入れた（0046 の「承認時の決定」）。

0046 の 6 は、次の 2 つを範囲外とし、この Issue で決めるとした。

1. **`ConnectionService.execute`**: `TaskContext`（`lease` を含む）を受け取るが、Tool Broker を通らないため Lease を確かめない。Codex / Claude の Provider を呼ぶ、費用のかかる外部の呼び出しである。
2. **`NodeBudgetHandle.charge`**: Node が報告する `tokens` / `gpu_seconds` の計上で、Run でだけ Fencing し、Lease は確かめない。

現在、Orchestrator の Node から `ConnectionService.execute` を呼ぶ経路はなく（本番の組み立てにも `ConnectionService` はない）、1 は実際の穴ではない。

## 決定

### 1. `ConnectionService.execute` は Lease を確かめる（Fail closed）

- **`ConnectionService(lease=LeaseVerifier)`** を足した（Keyword、任意）。既定は Broker と同じ `FailClosedLeaseVerifier`（`UNKNOWN` を返す）で、Verifier を渡さない Service は全ての呼び出しを拒否する。本番は Broker と同じ `orchestrator.QueueLeaseVerifier(queue)`（`TaskQueue.holds_lease`）を渡す。`check(task_id, lease)` の非同期の Method を持たないものは構築時に `InvalidConnectionInputError`。
- **全ての呼び出しで**、`context.lease` を `context.task_id` について尋ねる。検査は 0046 と同じ規則: Lock しない 1 つの読み取りで、Lease を延長しない。
- **位置（手順の 4a）**: Task の Budget（4）の後、Admission（5。Task・Connection・Quota の判定と `in_flight` の使用量の行の INSERT）の前。単独で拒否できる検査（引数、`agent.use`、Adapter の有無、Task の Budget）は Lease を尋ねずに拒否する。Lease を失った Worker の呼び出しは、Credential を解決せず、Provider を呼ばず、使用量の行を書かず、Quota も使わない（0046 で Lease の検査を Repository の予約・変更の記録の前に置いたのと同じ）。
- **拒否**: `LOST` は `RefusalReason.LEASE_LOST`（`lease_lost`）。`UNKNOWN`、例外、期限（`database_timeout_seconds`。1 つの Queue の読み取りのため Store の文の期限と同じ）、`LeaseStatus` でない答えは `RefusalReason.LEASE_UNAVAILABLE`（`lease_unavailable`）。どちらも既存の拒否と同じく `connection.use`（`deny`、reason = 上の値）を Audit に書き、`TaskNotUsableError(reason)` を投げる。例外の文言は Log に出さず、型名（`log_type_name`）だけを出す。
- **Run を止めるのは呼び出し元**: `ConnectionService` は Run の Guard を持たない。Orchestrator の Node からこの Service を呼ぶ経路を作るときは、その経路が `NodeToolGateway` と同じく、`TaskNotUsableError(LEASE_LOST)` で Run の Guard を `StopReason.LEASE_LOST` で止め、`lease_unavailable` ではその呼び出しだけを失敗させる（0046 の 4 と同じ）。今は経路がないため、その反応は実装していない。

### 2. 確認を通った呼び出しは止めず、精算と計上は Lease を尋ねない

- 0046 の 3 と同じく、確認の後の実行は Fencing しない。Provider を呼んでいる間に Lease が失われても、その呼び出しは止めない（Heartbeat が Lease の期限の前に Run を失ったとして Node を Cancel すれば、`execute` の既存の Cancel の処理で `cancelled` として精算される）。
- **精算（使用量の行、Quota、Task の Budget への Token の加算）は、Lease を尋ねずに必ず行う。** Provider は Token を消費済みで、費用はかかっている。Lease は「Worker が新しく始めること」を Fencing するもので、「行われた仕事の記録」を Fencing するものではない（Decision 0050 の理由と同じ: 記録は Budget を減らす方向にだけ働き、新しい実行を許さない）。
- 精算の Token の Budget への加算は、今と同じく Run でも Fencing しない（`BudgetTracker.record(task, TOKENS, n)`、`run` なし）。この Issue では変えない（下の「判断が必要な点」5）。

### 3. `NodeBudgetHandle.charge` は Lease を確かめない（Run の Fencing のまま）

- `NodeBudgetHandle.charge` は今と同じく、Attempt の Fence、Run の Guard（止まっていれば `NodeStopped`）、`BudgetTracker.record(..., run=)` の Run の Fencing だけを行い、Queue の Lease は尋ねない。コードの変更はない（Docstring に理由を記した）。
- 理由:
  - **記録であって行動ではない。** 計上は既に行われた仕事（Model が使った Token、GPU の時間）の記録で、何も始めない。Lease が守るべき「古い Worker の新しい行動」（Tool の呼び出し、Shared Connection の呼び出し、Node の起動、DAG の書き込み）は、それぞれ Broker・この Decision の 1・Heartbeat・`epoch` が Fencing している。
  - **Lease を確かめると、実際の消費だけが消える。** 引き継ぎ（別の Worker の Claim、同じ Worker ID の再 Claim）では Run が同じままなので、Lease を失ったことにまだ気付いていない Worker の仕事も同じ Run の消費である。これを落とすと、引き継いだ Worker が同じ Run の Budget をその分多く使える（Budget を越える方向に働く）。
  - 計上ごとに Queue の読み取りが 1 つ増える。
- Worker が Lease の喪失を**知った後**（Heartbeat または Broker が `lease_lost` を読み、Run の Guard が `LEASE_LOST` で止まった後）に報告された計上は、今と同じく `NodeStopped(LEASE_LOST)` で拒否され、記録されない（Decision 0021 の Guard の規則、0037 の 9「計上が `NodeStopped` などで失敗しても、伝えるのは Runtime の例外」）。この Issue では変えない（下の「判断が必要な点」6）。

### 4. GPU 時間の遅い計上（`TrackerLateGpuCharge`）は Lease を確かめない

- `TrackerLateGpuCharge` は Decision 0050 のとおり Attempt と Run の Fence を通さずに計上する。Queue の Lease も Fence の一種であり、同じ理由（止まらなかった Local の呼び出しが実際に使った GPU の時間を隠さない）で Lease も確かめない。コードの変更はない（Docstring に記した）。
- `HybridRuntime` が遅い計上へ回すのは、Node の Budget の計上が `NodeStopped(ABANDONED)`（Attempt が閉じた）で拒否されたとき、または Node の終わりの後まで GPU を持っていた呼び出し（`late=True`）だけである。Run の Guard が `LEASE_LOST` で止まった後、Attempt が閉じる前に終わった呼び出しの計上は `NodeStopped(LEASE_LOST)` で拒否され、計上されず Log に残る（今の動作。Decision 0050 の範囲を広げない。下の「判断が必要な点」7）。

## 選定理由

- **`ConnectionService.execute` を確かめる**のは、Shared Connection の呼び出しが Tool の呼び出しと同じく「Worker が Task のために新しく始める、取り消せない外部の行動」で、しかも費用がかかるため。Lease を失った Worker の呼び出しは、引き継いだ Worker の呼び出しと重なって同じ仕事に二重に費用をかけうる。0046 の Human の決定（Lease を失った Worker の行動を Fail closed で拒否する）を、Broker を通らないもう 1 つの行動の経路に及ぼす。
- **Service の中で確かめる**のは、Broker の場合と同じく、呼び出し元（今後の Orchestrator の経路、PAW-022 以降の経路）が確かめ忘れても効くようにするため。`TaskContext.lease` は既に必須で、全ての呼び出しが Lease を持つ。
- **既定を Fail closed にした**のは、Broker の `LeaseVerifier`・`BudgetProvider`・`TaskActivityProvider` と同じく、渡し忘れた構成が保護されない形を作らないため。本番の組み立てに `ConnectionService` はまだなく、影響は Test の Fixture だけである。
- **Admission の前に置いた**のは、Lease を失った Worker の呼び出しが `in_flight` の行（Request の Quota の 1 回）を残さないため。確認から Provider の呼び出しまでの隙間は、Admission の 1 Transaction と Credential の解決の分だけ広がるが、どちらも Lease を延長せず、0046 の 3 の残リスクと同じ種類である。
- **`TaskNotUsableError` の `reason` で返した**のは、既存の「Task をこの呼び出し元が使えない」拒否（`task_superseded` など）と同じ形で、Audit の reason と型付きの Error の reason が同じ閉じた語彙（`RefusalReason`）になるため。新しい Error の型を足すと、呼び出し元の分岐が増えるだけで情報は増えない。
- **計上を Lease で Fencing しない**のは、上の 3・4 のとおり、Fencing が守るのは行動で、記録を落とすと Budget を越える方向にしか働かないため（Decision 0050 と同じ考え方）。

## 代替案

- **`ConnectionService.execute` も確かめない**（0046 の 6 の現状）: 今は Orchestrator からの経路がないので実害はないが、経路を作った時点で Lease を失った Worker が費用のかかる外部の呼び出しをできる。経路を作る人が確かめ忘れると穴になる。
- **Admission の Transaction の中で Queue の行を読む**: 確認と `in_flight` の行の INSERT が 1 つの Transaction になり、読み取りが 1 往復減るが、`connections` の Store が Queue の Table を直接読むことになり、Broker と別の規則の実装が 2 つになる。Lock しない読み取りである限り、確認の後の隙間の性質は変わらない。
- **Lease の喪失で Provider の呼び出しを Cancel する**: Heartbeat の Cancel が既にこれを行う。`execute` の中で Lease を監視し続けると、呼び出しごとに定期的な読み取りが要る。
- **精算・計上を Lease で Fencing する**: Lease を失った Worker の Token・GPU 時間を記録しない。実際の費用が Quota と Budget に現れず、同じ Run を引き継いだ Worker が Budget を越えて使える。
- **`NodeBudgetHandle.charge` で Lease を確かめ、`LOST` なら Run を止める**（記録はする）: Lease の喪失に Heartbeat より早く気付けるが、計上は Node の終わり（または GPU の呼び出しの終わり）にしか来ないので早さの利点は小さく、計上ごとに読み取りが増える。Tool の呼び出しは Broker が確かめている。

## リスク

- **確認の後の実行は Fencing されない**（0046 の 3 と同じ）。Provider の呼び出しの間に Lease が失われても、その呼び出しは止まらず、答えは呼び出し元に返る。呼び出し元（今後の Orchestrator の経路）がその答えを DAG に書くかは、0046 の `_settle` の規則（Run の Guard が止まっていれば書かない）と `epoch` の Fencing に従う。
- 呼び出しごとに Queue の 1 行の読み取りが増える（0046 と同じ Index）。
- Lease の喪失を知った後に報告された Node の Token・GPU 時間は記録されない（3・4 の最後の段落。今の動作）。数え落としは、その時点で実行中だった仕事の分までで、GPU 時間の遅い計上に回るかどうかは、計上の時点で Attempt が閉じているか（`ABANDONED`）どうかの順序で決まる。
- `ConnectionService` を Queue の Lease を持たない呼び出し元（例えば PAW-022 以降の対話の Session が Task を介さずに呼ぶ経路）から使うと、全て拒否される。そのような経路を作るときは、別の Decision で Lease の代わりの Fencing を決める。

## 判断が必要な点（2026-09-29 に推奨どおり承認）

1. **`ConnectionService.execute` で Lease を確かめるか**: 確かめる（1 節）。`ConnectionService(lease=LeaseVerifier)` を足し、既定は Fail closed（`FailClosedLeaseVerifier`）、本番は `QueueLeaseVerifier(queue)`。推奨: 確かめる。代わりに「今は経路がないので確かめない」とする場合は、経路を作るときに決め直す必要がある。
2. **検査の位置**: Task の Budget の後、Admission の前（Lease を失った Worker は使用量の行を残さず、Quota を使わない）。推奨: この位置。Admission の Transaction の中で読む案は、Broker と別の実装になるため採らない。
3. **拒否の形**: `lease_lost` / `lease_unavailable` を `RefusalReason` に足し、`connection.use` の拒否として Audit に書き、`TaskNotUsableError(reason)` を投げる。検査の期限は `database_timeout_seconds`。推奨: この形。代わりに専用の Error（`LeaseLostError` など）を足す案もある。
4. **Run を止める責務**: `ConnectionService` は Run を止めず、Orchestrator の Node からの経路を作るときに、その経路が `NodeToolGateway` と同じ反応（`lease_lost` で `LEASE_LOST`、`lease_unavailable` はその呼び出しだけ）をする。推奨: この分担（今は経路がないため、その反応は実装しない）。
5. **実行中に Lease を失った呼び出しの精算と計上**: 呼び出しは止めず、使用量の行・Quota・Task の Budget への Token の加算は Lease を尋ねずに必ず行う。精算の Budget への加算は、今と同じく Run でも Fencing しない。推奨: この形（費用は発生済みで、隠すと Quota と Budget を越える）。代わりに Budget への加算を Run で Fencing する（0046 の 5 の `tool_calls` にそろえる）案は、置き換えられた Run の Token が引き継いだ Run の Budget を使わない利点があるが、実際の費用が Budget に現れなくなる。変えるなら別の Issue にする。
6. **`NodeBudgetHandle.charge` で Lease を確かめるか**: 確かめない（Run の Fencing のまま、3 節）。Lease の喪失を知った後（Guard が `LEASE_LOST`）の計上は、今と同じく拒否され記録されない。推奨: 確かめない、かつ Guard の規則はこの Issue では変えない。代わりに「`LEASE_LOST` で止まった後も、同じ Run の計上は記録してから `NodeStopped` を投げる」案は数え落としを無くすが、Decision 0021 の Guard の規則と 0037 の 9 を変えるので、必要なら別の Decision にする。
7. **GPU 時間の遅い計上との関係**: `TrackerLateGpuCharge` は Lease も確かめない（Decision 0050 のとおり Fence なし）。遅い計上へ回すのは今と同じく `ABANDONED` と `late=True` だけで、`LEASE_LOST` で拒否された計上には広げない。推奨: この形（Decision 0050 の範囲を変えない）。代わりに `LEASE_LOST` の拒否も遅い計上へ回す案は、GPU 時間の数え落としを無くすが、Run の Fence なしで計上するため、Run が置き換えられた後の分も新しい Run の Budget に入る。6 と合わせて別の Decision で扱うのがよい。
8. **Queue の Lease を持たない呼び出し元**: `TaskContext.lease` は必須のため、Queue の Claim を持たない呼び出し元は `ConnectionService.execute` を使えない（全て `lease_lost` / `lease_unavailable`）。推奨: 受け入れ、そのような経路を作るときに別の Decision で決める。

## 承認後の扱い

判断が必要な点が承認されたら、`Approval` に記録し、Status を Approved に改める。承認されない点は実装を変え、新しい Decision を作らずにこの Decision を承認前に改める（承認後に方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する）。
[REQUIREMENTS.md](../../REQUIREMENTS.md) と Decision 0046 の原文は書き換えない。

## 実装と Test

- `paw_backend/connections/service.py`（`ConnectionService(lease=)`、`_check_lease`、手順の 4a）、`domain.py`（`RefusalReason.LEASE_LOST` / `LEASE_UNAVAILABLE`）、`errors.py`（`TaskNotUsableError` の説明）。Migration も権限の変更もない（Application の Role は `queue_entries` の `SELECT` を既に持つ。Decision 0046）。
- `paw_backend/orchestrator/gateway.py`（`NodeBudgetHandle.charge`）と `paw_backend/compute/runtimes.py`（`TrackerLateGpuCharge`）は Docstring だけ。
- Test: `tests/test_connections_lease.py`（Lease を失った Worker の拒否と Audit、何も始まらないこと、全ての呼び出しで尋ねること、Quota より先に拒否すること、`UNKNOWN`・例外・期限・答えでない値・Verifier なしの `lease_unavailable`、検査の順序、実行中に Lease を失った呼び出しの精算と Budget への加算、本番の `QueueLeaseVerifier` と実際の Queue での引き継ぎ・同じ Worker ID の再 Claim・期限切れ・別の Task の Lease）。既存の Connection の Test は Lease を持つ Verifier を既定にした（`tests/connections_support.py`）。

## 承認時の決定（2026-09-29）

Human は、作業 Session で判断が必要な点について推奨つきの説明を受け、「推奨どおり」と回答して承認した（1〜8 の全点）。**すべて推奨どおり**で、個別の変更はない。
