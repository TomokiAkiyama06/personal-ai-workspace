# Task / Queue の Project 状態 Gate の適用範囲

- Status: Approved
- Date: 2026-09-25
- Scope: Issue [#83](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/83)（Task Lane と Queue Lane の Project 状態 Gate）と、Gate を組み立てる PAW-034（Orchestrator）。関連: PAW-026、PAW-032、PAW-033、PAW-025
- Supersedes: なし（[Decision 0008](0008-project-membership-and-lifecycle-policy.md) の 8 は承認済みで、そのまま有効である。この Decision は、その承認が決めていない点だけを扱う）
- Approval: 2026-09-26、Humanが作業Session内で、判断メモの各点に個別に回答し、残りは「推奨どおり」と回答して承認（末尾の「承認時の決定」）

## 背景

[Decision 0008](0008-project-membership-and-lifecycle-policy.md) の 8（2026-09-25 に Human が承認）は、`TaskService.create_task`・Retry・Restart と `TaskQueue.enqueue` が、Project の行を `SELECT ... FOR SHARE` で Lock し、Active 以外なら拒否する Gate を入れる方針を定めた。
Issue #83 で実装した（詳細は [Backend README](../../apps/backend/README.md) の「Project の状態 Gate」）。

実装は、その方針が決めていない次の点を、動かすために選んだ。ほとんどは実装の詳細だが、**製品の振る舞いに関わる 3 点**（下の A、B、C）は、Human の判断を求めた。
実装の詳細（Gate を Protocol にして注入すること、Cancel と Queue の Entry の Cancel を 1 つの Transaction にすること、条件つきの `TaskQueue.cancel`）は、Decision 0008 の 8 が承認した選択肢の範囲であり、この Decision では扱わない。

## 決定

1. **Gate が止めるのは、新しい作業を受け入れる、または始める操作である。** 最初の提案は `create_task`・Retry・Restart・`enqueue` の 4 つで、承認時の決定 C で **Start**（Queued の Task を実行中にする Command）が加わった。
   Cancel、Fail、Stop Now、Pause、Resume、Wait、Unblock、Begin evaluation、Complete は、どの状態の Project でも動く（削除待ちの Project の Task を止める Processor 自身が Cancel を発行する。すでに実行中の作業は止めない）。
   Lease の Heartbeat、返却、完了、取消も Project を見ない。
2. **Active 以外は全て拒否する。** Archived、Pending deletion、Deleted に加え、存在しない Project も同じ `ProjectNotActiveError` で拒否する（既定は拒否。理由は書かない）。
   要件は Archived を「新規 Agent Task、Repo 変更、Project Memory 更新は停止する」読み取り専用の状態と定めており、Authorizer（PAW-025）も `project.task.run` を Archived で拒否する。Gate はそれと同じ線を、競合の下でも守る。
3. **Gate は必須である**（承認時の決定 A）。`TaskService` と `TaskQueue` の Constructor は `project_gate` を必須の Keyword とし、省略と `None` は Construct 時の Error（`TypeError` / `InvalidQueueingArgumentError`）にする。Gate を静かに飛ばす組み立ては作れない。
   Project のない Test と Tool は、何でも通す Gate を明示して渡す。それは Test の Support Module にだけあり、`paw_backend` のどこにも無い（Test が保証する）。
4. **待ち時間に上限がある。** Gate の Lock は `lock_timeout_ms`（既定 3 秒）で待ち、超えると `ProjectBusyError`（再試行できる）。Task や Entry は書かれない。
5. **Queue は、Active でない Project の Entry を Claim しない**（承認時の決定 C）。`claim_next` は、Task の Project が Active でない（または存在しない）Entry を飛ばし、そのまま残す（`queued`。Lease の切れた `claimed` も同じ）。Project が Active に戻れば、元の順序で Claim される。
   実行中の Task は止めない。Claim の後に Archive が Commit されると、Start が拒否され、Worker は Entry を `release` で返す。

## 検討した他の案（決定 A、B、C の選択肢）

### A. Gate を必須にするか

Gate を省略すると、Gate は静かに働かなかった（省略が Error にならない）ことが問題だった。必須にすると、Task Lane と Tool の Test の組み立て（90 か所）を変える必要がある。

- 案 1: 省略可とし、PAW-034 の受け入れ条件に「Gate つきで組み立てる」ことを足す。
- **案 2（採用）: 必須にする。** 省略した組み立てを、Construct 時に防ぐ。Project のない Test には、何もしない Gate を明示して渡す。
- 案 3: 案 1 に加えて、起動時に Orchestrator が Gate の有無を検査する。

### B. Restore の後、Restart は Unarchive の後にだけできる

Decision 0008 の 8 の 3 は Cancel を選ぶ理由の 1 つに「復元後に Restart できる」を挙げた。Restore の復元先は Archived で、Archived は新規作業を受け入れないため、**停止された Task を Restart するには、Manager がさらに Unarchive（Active に戻す）する必要がある**。
これは要件（Archived は読み取り専用）と Authorizer に一致するが、復元してすぐ Restart できると読める文とはずれる。

- **案 1（採用）: そのままにする。** Restore は「アクセスを戻す」、Unarchive は「作業を再開できる状態に戻す」という 2 段の意味を保つ。
- 案 2: Archived でも Retry / Restart だけは許す。読み取り専用の意味が弱まり、Authorizer の規則と食い違う。

### C. Archive の時点ですでに受け入れた作業

最初の実装では、Gate が止めるのは **新しい** 作業だけで、Archive 前に queued だった Task は Archive の後も Claim され、Start され、実行された（`ProjectService.archive` は何も止めず、Start・Claim は Project を見なかった）。
要件は Archived を「新規 Agent Task、Repo 変更 … は停止する」と書き、すでに走っている Task を止めるか、queued の Task を走らせないかを書いていない。

- 案 1: 今は変えない。PAW-034 が Orchestrator の方針を決めるときに決める。
- 案 2: Archive も Delete 開始と同じ Outbox で Task を止める。Archive の Manager 操作が、実行中の Task を Cancel する副作用を持つ。
- **案 3（採用）: Claim と Start も Project の状態を確かめる。** `claim_next` は Active でない Project の Entry を飛ばし、Start は Gate を通る。走行中の Task は止まらない。

## その他の検討した案

- **Database の Trigger で拒否する。** `tasks` と `queue_entries` に Trigger を置き、Project が Active でなければ Insert を拒否する。Gate を省略できないが、別 Lane の Table を変えること、Task Lane の Test が存在しない Project の ID を使うことから、Decision 0008 の 4 で採らなかった。ここでも採らない（Gate の必須化で、省略できない点は満たす）。
- **Project の状態を Cache して確かめる。** Lock なしの読み取りは、Delete との競合を閉じない（読んだ後に状態が変わる）。
- **Gate なしで、Processor の再実行だけに頼る。** PAW-026 の状態。Processor は Delete 開始の後に作られた Task を止められるが、Restore と Restart の競合（Decision 0008 の 8 の 3 つの窓）は閉じない。
- **Claim の Filter を Join（`EXISTS`）で書く。** Planner が Active な Project から始めて、全 Entry を読んで並べ替える Plan を選ぶことがあり（Test で確認した）、Claim が Entry の数に比例して遅くなる。相関した Scalar の副問い合わせにして、Index の順に読んで最初に通る Entry で止まる Plan に固定した。

## リスク

- Gate は、Task の作成、Retry、Restart、Start、Enqueue のたびに Project の行を `FOR SHARE` で Lock する。Project の行を `FOR UPDATE` で Lock する操作（Archive、Delete、Restore、Member の変更）は短い Transaction だが、その間これらは待つ。
  待ち時間は `lock_timeout_ms`（既定 3 秒）で上限があり、超えると `ProjectBusyError` になる。1 つの Project への Task 作成が多い運用では、Member の変更と競合しうる。
- Claim の Filter は、Active でない Project の Entry のうち、最初に Claim できる Entry より前にあるものを、Claim のたびに読み直す。その数が、Claim の追加の費用である（Archived の Project の Entry は、Unarchive まで残る。Pending deletion の Project の Entry は Processor が Cancel する）。
  Index の順に読み、最初の Entry で止まることは Plan の Test が確認する。Active でない Project の Entry が大量に長く残る運用では、Archived の Project の Entry を Queue から外す仕組み（別の Issue）が要る。
- Archive の後に Start が拒否された Entry は、Worker が `release` するまで Lease を持つ（Lease の期限で自動的に戻る）。Orchestrator（PAW-034）は `ProjectNotActiveError` を受けたら `release` すること。
- Gate は `projects.status` だけを読み、書かない。Application Role の権限は変わらない（`SELECT` と、`FOR SHARE` に必要な列の `UPDATE` は Migration 0026 で付与済み）。

## 承認後の扱い

- A: Constructor を必須にし、Test の組み立てを全て変えた。PAW-034 の受け入れ条件へ「`ProjectStateGate` つきで組み立てる」ことを足す必要はなくなった（省略できないため）。
- B: 何も変えない（README に書いてある）。
- C: `claim_next` の Filter と Start の Gate を実装した。PAW-034 は、Start の拒否（`ProjectNotActiveError`）を受けたら Entry を `release` する。
- 承認後に方針を変える場合は、この Decision を書き換えず、新しい Decision から `Supersedes` する。

## 承認時の決定（2026-09-26）

- 本文の各点を承認した。A、C は推奨（案 1）と異なる案を選び、B は推奨どおりとした。
- **A. Gate は必須にする（案 2）。** `TaskService` と `TaskQueue` の Constructor は `ProjectStateGate`（の Protocol を満たす Object）を必須とし、省略は Construct 時の Error とする。Project のない Test と Tool は、明示した何もしない Gate（Test の Support Module の `AlwaysActiveGate`）を渡す。Production の Code はそれを使えない（Test が確認する）。README と Decision から、省略できるという記述を除く。
- **B. Restore の後の Restart は、Unarchive の後にだけ許す（案 1、推奨どおり）。** 変更なし。
- **C. Claim と Start も Project の状態を確かめる（案 3）。** Queue の Claim（`claim_next`）と Start は、Active でない Project の Entry を Claim せず、Task を Start しない。Entry は Queued のまま残り、Project が Active に戻れば再開できる（Restore の復元先は Archived なので、Unarchive の後）。実行中の Task は止めない。Claim の Filter が Claim を遅くしないことを、Plan の Test で確認する。
