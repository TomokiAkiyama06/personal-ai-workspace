# Node の Attempt が閉じた後の GPU 時間を Fence なしに Task の Budget へ計上する（`TrackerLateGpuCharge`）

- Status: Approved
- Date: 2026-09-28
- Scope: PAW-036（[#32](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/32)、PR #131）の `HybridRuntime` と `TrackerLateGpuCharge`（`apps/backend/paw_backend/compute/runtimes.py`）。Cancel しても止まらなかった Local の呼び出しが、Node の Attempt が閉じた後に使い終えた GPU 時間の計上
- Supersedes: なし（[Decision 0037](0037-gpu-compute-scheduler.md) は書き換えない。0037 の「承認後の補足」が述べた Fence なしの計上について、Human の確認を別に記録して補う。0037 の 10 の「Local の Lease の時間を `GPU_SECONDS` に計上する」を狭めも広げもしない）
- Approval: 2026-09-28（21:00 JST ごろ）、Human（Owner）が作業 Session で推奨つきの確認に直接回答して承認。回答は「#131 late GPU charge: charge without the attempt fence (TrackerLateGpuCharge approved)」（Fence を通さずに計上する）

## 背景

Decision 0037 の 10 は、Local の Lease を持っていた秒数を Task の Budget の `GPU_SECONDS` に計上すると決めた。PR #131 の Codex Review を反映した変更で、Cancel しても止まらない Local の呼び出しは、止まるまで Lease を返さず、止まるまでの GPU 時間も計上するようにした。

ところが、そのような呼び出しが止まるころには、Orchestrator が Node の Attempt をすでに閉じており、Node の Budget は Attempt と Run の Fence によって計上を断る。そのままでは、実際に使った GPU 時間が Budget に現れず、Log に残るだけになる。PR #131 は、これを補うために注入できる `TrackerLateGpuCharge(budget_tracker)` を置き、Attempt と Run の Fence を通さずに Task の Budget へ直接計上するようにした。

Fence を通さない書き込みは、閉じた Attempt の後から Budget を変える扱いで、0037 の承認の範囲を越えるかどうか Human の確認が要る点だった（0037 の「承認後の補足」には記述があるが、承認そのものは 14 点に対するもの）。

## 決定

Node の Attempt が閉じた後に、Cancel しても止まらなかった Local の呼び出しが使い終えた GPU 時間は、`TrackerLateGpuCharge` で **Attempt と Run の Fence を通さずに** Task の Budget の `GPU_SECONDS` へ計上する。

- 計上するのは、実際に Lease を持って GPU を使った時間だけとする。見積もりや予約の時間は含めない。
- 本番の処理（`HybridRuntime` の組み立て）に繋ぐときは、`late_gpu_charge=TrackerLateGpuCharge(budget_tracker)` を渡す。渡さない構成では、その秒数は計上されず Log に残るだけになる。

## 理由

- Fence を通すと、実際に使った GPU 時間が計上されずに消える。止まらない呼び出しが Task の `GPU_SECONDS` を越えて GPU を使っても見えなくなる。
- 既に起きた消費の記録であり、Tool Broker の実行済みの Tool の呼び出しを Run の Fence なしに `tool_calls` へ計上する既存の扱い（`apps/backend/paw_backend/orchestrator/gateway.py` の Broker の Budget の `charge`）と同じになる。Budget の種類と上限は [Decision 0007](0007-task-queue-budget-and-loop-policy.md) のまま変えない。
- 計上は Budget を減らす方向にだけ働き、新しい実行を許すことはない。

## 影響

- コードの変更はない（PR #131 で実装済み）。この Decision は、その扱いが承認済みであることを記録する。
- 方針を変える場合は、この Decision を書き換えず、新しい Decision から `Supersedes` する。
