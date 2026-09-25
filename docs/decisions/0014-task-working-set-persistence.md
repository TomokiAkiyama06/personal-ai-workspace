# Task の Working Set（Multi-Repo Task）の永続化

- Status: Approved
- Date: 2026-09-25
- Scope: PAW-032（Agent Task Lifecycle / Persistence）に Working Set を含めるか。含めない場合に、いつ、どこで持つか。関連: PAW-027 / 031 / 034 / 035 / 061 / 062
- Supersedes: なし
- Approval: 2026-09-25、Humanが作業Session内で、判断メモ（Artifact）の各点について「推奨どおり」と回答して承認（末尾の「承認時の決定」）

## 背景

[REQUIREMENTS.md](../../REQUIREMENTS.md) の「Multi-Repo Task / Working Set」（`[FIXED]`）は、Task ごとに Working Set（Repo の集合）を持ち、
Repo に `referenced`（調査・参照のみ）/ `working`（編集・テスト可）/ `target`（PR 作成まで）の役割を持たせ、
Write 範囲を Working Set として明示・制御し、Multi-Repo Task では Repo ごとに Git 状態（worktree / branch、test、review、PR）を分離し、
Task 全体の完了を対象 Repo ごとの必要条件で判定する、と定めている。
「Task State」の節は、再接続時に復元するものの 1 つに「Repo Working Set」を挙げている。

独立 Review（PAW-032 の PR #70、第 4 回）は、PAW-032 の実装が Working Set を保存せず、`restore()` も返さないことを指摘した。
指摘は事実として正しい。`task_attempts` は試行に 1 組の branch / worktree / Review / PR を持つだけで、どの Repo かも、複数の Repo も表さない。

一方で、次の証拠から、これを PAW-032 の完了条件として黙って実装することは見送る。この見送りは、Human が 2026-09-25 に承認した（末尾の「承認時の決定」）。

1. **PAW-032 の受け入れ条件に Working Set がない。**
   [Implementation Backlog](../IMPLEMENTATION_BACKLOG.md) の PAW-032 は「Queued / Running / … / Cancelled」「Client切断後もstate保持」「current step / logs / worktree / review / PR stateを復元」「Pause / Resume / Cancel / Retry / Restart / Stop Now」の 4 項目で、
   worktree / review / PR は Task に 1 組の書き方である。Depends on は `PAW-020` だけで、Repository の登録（PAW-027）には依存しない。
2. **Working Set が指す Repository がまだない。** Repository の登録と ID は PAW-027（Depends on: PAW-026）の責務で、
   PAW-032 の `project_id` も、Project の Table がまだないため外部キーのない UUID のままである（[Backend README](../../apps/backend/README.md)）。
3. **Repo ごとの Git 状態と統合は別の Issue の責務である。** Write Worker ごとの worktree / branch、同一 Repo の並列変更の統合、統合後の test / review は PAW-035（Depends on: PAW-034, PAW-027）、
   Working Set 候補の提案は Planner（PAW-034）、Repo ごとの表示は PAW-061 / 062 が扱う。
4. **Write 範囲の強制は Tool Broker（PAW-031）が行い、Working Set は呼び出し側が解決して渡す入力である。**
   Decision 0006（PAW-031 の PR に含まれる `docs/decisions/0006-tool-broker-policy.md`）は、Orchestrator が Task の作業対象を `TaskScope.repositories` として呼び出しごとに作ると選んでいる。
   このとき Repo の役割（`referenced` / `working` / `target`）は、まだ Broker の入力に含まれていない。役割をどう Write 範囲へ対応させるかは、Working Set の保存より先に決める必要がある。
5. **要件が決めていない設計判断がある**（下の「決まっていないこと」）。これらを PAW-032 の実装が暗黙に選ぶと、
   AGENTS.md「仕様にない重要判断を勝手に確定しない」に反する。

## 決まっていないこと

1〜5 は、Human が 2026-09-25 に、今は決めず、#85 の実装の前に別の Decision で決めると承認した（特に 4 は、保存より先に決める）。6 は同日に決めた。

1. **Working Set の単位。** Task に属するか（Restart でも維持）、試行に属するか（Restart で作り直す）。Restart は元の `starting_commit` と `input` から最初からやり直すが、対象 Repo まで初期化するかは要件にない。
2. **Single-Repo Task との関係。** 通常は Single-Repo（要件）。Single-Repo でも常に Working Set の 1 行（`target`）を持つのか、Multi-Repo のときだけ持つのか。前者なら、現在の `task_attempts` の worktree / Review / PR の列を Repo ごとの Table へ移すことになる。
3. **Repo の追加と役割の変更の権限と承認。** 要件は「Write 対象 Repo を増やす場合は、Read 対象追加より慎重に扱う」「曖昧な場合は User へ確認できるようにする」とだけ書く。誰が（Agent の提案を人間が承認するのか）、`Waiting for Approval` を経るのか、Audit にどう残すかは未決である。
4. **役割と Write 範囲の対応。** `working` は編集・テスト、`target` は PR 作成まで、と読めるが、Tool Broker の `SCOPED_AUTO` の範囲、Repo の ACL（PAW-025 の `RepoAcl`）との合成規則は決まっていない。
5. **Task 全体の完了条件。** 「対象 Repo ごとの必要条件が満たされたか」の具体（全 `target` の PR が必要か、全 `working` の Evaluator が PASS か、など）と、`complete` / `begin_evaluation` の遷移条件への反映。
6. **どの Issue が実装するか。** Backlog の PAW-035（Repo ごとの Git 状態）に含めるか、PAW-034 の前に新しい Issue を立てるか。
   Human が 2026-09-25 に、PAW-027 の後・PAW-034 の前に立てる新しい Issue [#85](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/85) と決めた（PAW-035 には含めない）。

## 提案

以下は、Human が 2026-09-25 に承認した方針である。

1. **Working Set の永続化は PAW-032 の完了条件に含めない。** PAW-032 は、Backlog の 4 項目どおり、Task に 1 組の worktree / review / PR 状態の永続化と復元までとする。
   Backend README に「PAW-032 は Working Set を含まない。この Decision が追跡する」と明記する（実装済み）。
2. **Repository の登録（PAW-027）ができた後、上の未決の項目が決まってから、専用の Migration で追加する。** 実装する Issue は、PAW-027 の後・PAW-034 の前に立てる新しい Issue [#85](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/85) とする（上の 6。PAW-035 には含めない）。
3. **想定する形**（承認された方針は、Working Set を PAW-032 に含めないことと、担当を #85 とすることである。この形に縛られることは決めておらず、形は #85 の実装の前の別の Decision で決める）:
   - `task_repositories`: `task_id`（`tasks` への外部キー）、`repository_id`（PAW-027 の Repository の UUID。Table ができるまでは外部キーなし。`project_id` と同じ扱い）、`role`（`referenced` / `working` / `target`、CHECK 制約）、追加した Actor、時刻。`(task_id, repository_id)` は一意。
   - Repo ごとの Git 状態: 試行と Repo ごとの行（branch、worktree の path、head commit、Review 状態、Evaluator 結果、PR の番号・URL・状態）。現在の `task_attempts` の列は、Single-Repo の状態としてそこへ移すか、Task 全体の状態として残す（上の 2）。
   - Repo の追加と役割の変更は、`task_events`（Append-only）へ Actor 付きの Event として残す。
   - `TaskService.restore()` は `TaskSnapshot` に Working Set（Repo、役割、Repo ごとの Git 状態）を含める。
   - Write 範囲の強制は Tool Broker（PAW-031）が、保存された Working Set の役割から作った `TaskScope.repositories` で行う。`TaskService` は保存と復元だけを担い、認可はしない（現在の方針のまま）。

## 検討した他の案

- **A. PAW-032 に最小の Working Set（Repo の UUID と役割だけの Table）を今入れる。** 見送る。要件が復元を求める中身は、Repo ごとの Git 状態と Write 範囲の境界であり、
  membership だけを保存しても要件は満たされない。かえって、単位（上の 1）、Single-Repo との関係（上の 2）、役割の意味（上の 4）を、Migration の形として暗黙に確定してしまう。
  今の時点で形を固めると、後で単位や役割の意味が変わったときに、Table を作り直す Migration が要る。
- **B. PAW-032 で Repo ごとの Git 状態まで実装する。** 見送る。Backlog の受け入れ条件を超え、PAW-027（Repository の登録）と PAW-035 の設計を先取りする。

## 影響

- PAW-032 の `restore()` は、Working Set を返さない。Multi-Repo Task を Backend が復元する必要が生じるまでは、Single-Repo の 1 組の状態だけが復元される。
  この間、Backend の他の部分が Task の対象 Repo を知るには、呼び出し側が渡す `TaskScope.repositories`（PAW-031）に頼る。
- 承認された内容を変える場合は、この Decision を書き換えず、新しい Decision から `Supersedes` する（AGENTS.md「仕様変更」）。

## 承認後の扱い

2026-09-25 に承認された。PAW-032 のPR（#70）は本Decisionを参照する。
承認後に方針を変える場合は、この Decision を書き換えず、新しい Decision から `Supersedes` する。
[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。

## 承認時の決定（2026-09-25）

- 本文の各点を、提案どおり承認した。
- Working Set の永続化は、PAW-032 の完了条件に含めない。
- 実装の担当は、PAW-027 の後・PAW-034 の前に立てる新しい Issue [#85](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/85) とする。PAW-035 には含めない。
- 「決まっていないこと」の 1〜5（Working Set の単位、Single-Repo Task との関係、Repo の追加・役割の変更の権限と承認、役割と Write 範囲の対応、Task 全体の完了条件）は、今は決めず、#85 の実装の前に別の Decision で決める。そのうち「役割と Write 範囲の対応」は、保存より先に決める。

