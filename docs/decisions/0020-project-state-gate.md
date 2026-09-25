# Task / Queue の Project 状態 Gate の適用範囲

- Status: Proposed
- Date: 2026-09-25
- Scope: Issue [#83](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/83)（Task Lane と Queue Lane の Project 状態 Gate）と、Gate を組み立てる PAW-034（Orchestrator）。関連: PAW-026、PAW-032、PAW-033、PAW-025
- Supersedes: なし（[Decision 0008](0008-project-membership-and-lifecycle-policy.md) の 8 は承認済みで、そのまま有効である。この Decision は、その承認が決めていない点だけを扱う）
- Approval: 未承認

## 背景

[Decision 0008](0008-project-membership-and-lifecycle-policy.md) の 8（2026-09-25 に Human が承認）は、`TaskService.create_task`・Retry・Restart と `TaskQueue.enqueue` が、Project の行を `SELECT ... FOR SHARE` で Lock し、Active 以外なら拒否する Gate を入れる方針を定めた。
Issue #83 で実装した（詳細は [Backend README](../../apps/backend/README.md) の「Project の状態 Gate」）。

実装は、その方針が決めていない次の点を、動かすために選んだ。ほとんどは実装の詳細だが、**製品の振る舞いに関わる 3 点**（下の A、B、C）は、Human の確認が要る。
実装の詳細（Gate を Protocol にして注入すること、Cancel と Queue の Entry の Cancel を 1 つの Transaction にすること、条件つきの `TaskQueue.cancel`）は、Decision 0008 の 8 が承認した選択肢の範囲であり、この Decision では扱わない。

## 提案（実装が選んだこと）

1. **Gate は 4 つの操作だけを止める。** 新しい作業を受け入れる `create_task`・Retry・Restart・`enqueue` だけである。
   Cancel、Fail、Stop Now、Pause は、どの状態の Project でも動く（削除待ちの Project の Task を止める Processor 自身が Cancel を発行する）。
   すでに受け入れた作業の進行（Start、Resume、Unblock、Claim、Heartbeat、Complete など）も止めない。
2. **Active 以外は全て拒否する。** Archived、Pending deletion、Deleted に加え、存在しない Project も同じ `ProjectNotActiveError` で拒否する（既定は拒否。理由は書かない）。
   要件は Archived を「新規 Agent Task、Repo 変更、Project Memory 更新は停止する」読み取り専用の状態と定めており、Authorizer（PAW-025）も `project.task.run` を Archived で拒否する。Gate はそれと同じ線を、競合の下でも守る。
3. **Gate は注入する。** `TaskService(database, project_gate=...)` と `TaskQueue(database, project_gate=...)`。省略すると Gate は働かない（Project のない Task Lane の Test と Tool のため）。
4. **待ち時間に上限がある。** Gate の Lock は `lock_timeout_ms`（既定 3 秒）で待ち、超えると `ProjectBusyError`（再試行できる）。Task や Entry は書かれない。

## 決めてほしいこと（推奨つき）

### A. Gate を必須にするか

現在は省略でき、省略すると **Gate は静かに働かない（fail-open）**。Task Lane の Test と Tool の Test は、存在しない Project の ID で Task を作るため、必須にすると 70 か所ほどの Test の組み立てを変える必要がある。
Service を組み立てる本番の経路は、まだ無い（PAW-034 が最初に作る）。

- 案 1（推奨）: 今は省略可とし、**PAW-034 の受け入れ条件に「`TaskService` と `TaskQueue` を `ProjectStateGate` つきで組み立てる」ことを足す**。組み立てる場所が 1 か所になるため、そこで確認できる。
- 案 2: 必須にする。省略した組み立てを型で防げるが、Task Lane と Queue Lane の Test の組み立てを広く変える。Project のない Test には、何もしない Gate を明示して渡す。
- 案 3: 案 1 に加えて、起動時に Gate が組み立てられていることを Orchestrator が検査する。

### B. Restore の後、Restart は Unarchive の後にだけできる

Decision 0008 の 8 の 3 は Cancel を選ぶ理由の 1 つに「復元後に Restart できる」を挙げた。Restore の復元先は Archived で、Archived は新規作業を受け入れないため、**停止された Task を Restart するには、Manager がさらに Unarchive（Active に戻す）する必要がある**。
これは要件（Archived は読み取り専用）と Authorizer に一致するが、復元してすぐ Restart できると読める文とはずれる。

- 案 1（推奨）: そのままにする。Restore は「アクセスを戻す」、Unarchive は「作業を再開できる状態に戻す」という 2 段の意味を保つ。
- 案 2: Archived でも Retry / Restart だけは許す。Restore の直後に再開できるが、Archived の「新規 Agent Task 停止」と Authorizer の規則（`project.task.run` を拒否）が食い違い、読み取り専用の意味が弱まる。

### C. Archive は、すでに受け入れた作業を止めない

Gate が止めるのは **新しい** 作業だけである。Archive 前に queued だった Task は、Archive の後も Claim され、Start され、実行される（`ProjectService.archive` は何も止めず、Start・Claim は Project を見ない）。
Delete 開始は Outbox で Task を Cancel するが、Archive にはそれが無い。要件は Archived を「新規 Agent Task、Repo 変更 … は停止する」と書き、すでに走っている Task を止めるか、queued の Task を走らせないかを書いていない。

- 案 1（推奨）: 今は変えない。Gate の目的（Delete との競合を閉じる）を超えるため、PAW-034 が Orchestrator の方針を決めるときに、「Non-Active の Project の Entry を Claim しない」「Archive で走行中の Task を Pause する」のどちらを採るかを合わせて決める。
- 案 2: Archive も Delete 開始と同じ Outbox で Task を止める。Archive の Manager 操作が、実行中の Task を Cancel する副作用を持つ。
- 案 3: Claim と Start も Project の状態を確かめる（`claim_next` に Project の Filter を足す）。走行中の Task は止まらない。

## 検討した他の案

- **Database の Trigger で拒否する。** `tasks` と `queue_entries` に Trigger を置き、Project が Active でなければ Insert を拒否する。Gate を省略できないが、別 Lane の Table を変えること、Task Lane の Test が存在しない Project の ID を使うことから、Decision 0008 の 4 で採らなかった。ここでも採らない。
- **Project の状態を Cache して確かめる。** Lock なしの読み取りは、Delete との競合を閉じない（読んだ後に状態が変わる）。
- **Gate なしで、Processor の再実行だけに頼る。** PAW-026 の状態。Processor は Delete 開始の後に作られた Task を止められるが、Restore と Restart の競合（Decision 0008 の 8 の 3 つの窓）は閉じない。

## リスク

- Gate は、Task の作成と Enqueue のたびに Project の行を `FOR SHARE` で Lock する。Project の行を `FOR UPDATE` で Lock する操作（Archive、Delete、Restore、Member の変更）は短い Transaction だが、その間 Task の作成は待つ。
  待ち時間は `lock_timeout_ms`（既定 3 秒）で上限があり、超えると `ProjectBusyError` になる。1 つの Project への Task 作成が多い運用では、Member の変更と競合しうる。
- Gate を省略した組み立て（A）は、Delete との競合を閉じない。Processor の再実行が、その Task を後から止める（PAW-026 の保証）。
- Gate は `projects.status` だけを読み、書かない。Application Role の権限は変わらない（`SELECT` と、`FOR SHARE` に必要な列の `UPDATE` は Migration 0026 で付与済み）。

## 承認後の扱い

- A: 案 1 なら、PAW-034（[#30](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/30)）の受け入れ条件へ 1 行足す（Issue の更新は Human または Orchestrator が行う）。案 2 なら、新しい Issue で Test の組み立てを変える。
- B: 案 1 なら、何も変えない（README に書いてある）。案 2 なら、Gate の対象から Retry と Restart を Archived だけ外す変更が要る。
- C: 案 1 なら、PAW-034 の受け入れ条件へ「Non-Active の Project の Task の扱い」を足す。案 2 と 3 は、新しい Issue で行う。
- 承認後に方針を変える場合は、この Decision を書き換えず、新しい Decision から `Supersedes` する。

## 決めてほしいこと（まとめ）

1. **A**: Gate を必須にするか（推奨: PAW-034 の受け入れ条件に足す。今は省略可）。
2. **B**: Restore の後の Restart を、Unarchive の後にだけ許すか（推奨: そのまま）。
3. **C**: Archive の時点ですでに受け入れた作業を、止めるか（推奨: 今は変えず、PAW-034 で決める）。
