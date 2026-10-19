# Task を作る正規の入口で決まっていないこと（読み取りだけの Task、二重送信、Priority と Budget の選び方）

- Status: Proposed
- Date: 2026-10-20
- Scope: Issue [#274](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/274)（#208 の段 3 の最後の確認の前提）の `POST /api/v1/projects/{project_id}/tasks`（`apps/backend/paw_backend/api/v1/tasks.py`）と Web の `エージェント / タスク` › `新しいタスク`（`apps/web/src/tasks/CreateTaskForm.tsx`）
- Supersedes: なし。[Decision 0093](0093-production-runtime-config.md)（Approved）の 5 の「小さな Task（読むだけの Repository の調べもの）」の確認の手順を、書き換えずにこの入口で行うことを記録する（下の「0093 の手順について」）

## 背景

#208 の段 3 は 2026-10-19 に本番へ入ったが、製品に Task を作る入口がなく、最後の確認（小さな Task を Local の Node で動かし、`agent_dag_node_attempts.placement = 'local_gpu'` を確かめる）ができなかった。人間は 2026-10-20 に、本番の最初の Task を正規の入口から入れると決めた（#274 の B 案）。

#274 の PR は、**既存の Decision が決めている範囲だけ** で入口を作った。

- 権限: Route の Guard が Project について `project.task.run`（監査する）。Working Set の各 Repository は、その Repository の ACL で、`referenced` は `project.read`、`working` / `target` は `project.repo.write`（[Decision 0035](0035-working-set-capability-grants-and-legacy-tasks.md) の 3）。Project の外の Repository と読めない Repository は同じ 404。
- Project の状態: Guard が Archived / Pending deletion を 403 で拒み、`TaskService.create_task` の Gate（[Decision 0020](0020-project-state-gate.md)）がその間に変わった Project を 409 `project_not_active` で拒む。
- `target` は必須（API が 422 `no_target_repository`）。0035 の 3 は `target` のない作成を認めるが、それは作成後に UI で `target` を足す経路のためで、Queue に入れる入口では `target` のない Task は始まらない（`NoTargetRepositoryError`）。
- Queue と Budget: `create_task` の同じ Transaction で、`Orchestrator.enqueue_task` と同じく Queue の Entry と Budget の Preset を書く（[Decision 0007](0007-task-queue-budget-and-loop-policy.md): Budget のない Task は走らせない）。
- 監査: 作成は他の作成の Route（`POST /projects` など）と同じく、Capability の判定の監査（`audit_events`）と、Task の `create` の Event（作成者と Working Set）で残る。
- CSRF: 他の POST と同じく、`SameSite=Strict` と Origin の検査（[Decision 0044](0044-web-app-serving-and-session.md)）。

次の 3 つは要件にも既存の Decision にもなく、この PR では決めずに今の振る舞いに留めた。

## 提案

### 1. 読み取りだけの Task（推奨: 別 Issue で A を実装する）

今は、読み取りだけを指示文で頼むことしかできない。`target` が必須なので、Planner が Worker の Node を作れば、その Node は自分の `paw/` の branch と worktree に書ける（default branch には書かない。[Decision 0036](0036-parallel-worktree-integration.md)）。読むだけの Plan でも、DAG が成功すると Task は `evaluating` に進み、`target` には PR が要る（[Decision 0030](0030-task-working-set-model.md) の 5）ので完了しない。

- **A（推奨）: Task に「読み取りだけ」の印を持たせ、Backend が保証する。** 作成の Body に `read_only: true` を足し、Task の行に保存する（Migration が要る）。その Task では、(a) Plan の受け入れで `worker` の Node を拒む（Planner の失敗として次の Planner へ。Planner・Researcher・Reviewer は今も書く Capability を持たない: `orchestrator/domain.py`）、(b) Working Set は `referenced` だけでよく、`target` がなくても始められる、(c) DAG が成功したら、PR・Evaluation の義務なしに `completed` にし、結果（各 Node の要約）を Task に残す。REQUIREMENTS の「Read-only Researcher / Reviewer は、Write が発生しないことを Backend が保証できる場合、同一の immutable snapshot / checkout を共有可能」とも合う。
- B: 指示文だけ（今のまま）。保証がなく、読むだけの Task が `evaluating` で止まる。
- C: 読み取りだけの Plan の形（Planner を使わず Researcher 1 つの固定の DAG）を作成時に選べるようにする。A より小さいが、完了の扱い（上の (c)）は A と同じく要る。

### 2. 二重送信（推奨: 今は A、Task の量が増えたら B）

`POST /projects/{id}/tasks` は、他の POST と同じく Idempotency Key を受けない。同じ Request を 2 回送ると Task が 2 つでき、どちらも Queue に入る（Test で固定した）。Web の画面は送信中にボタンを止める。

- **A（推奨、今）: 今のまま。** Browser は POST を自動で再送しない。誤って作った Task はキャンセルできる。
- B: `Idempotency-Key` Header を受け、User ごとに Key と Request の Hash を 24 時間保存し、同じ Key の再送には最初の Task を返す（違う Body なら 409）。Table と Migration が要る。CLI や Chat から作る経路が増え、再送が起きうるときに入れる。

### 3. Priority と Budget の Preset を誰が選べるか（推奨: 今のまま）

- Priority: REQUIREMENTS の「Task Queue / Priority / Preemption」は、通常の User の Task を `NORMAL` とし、「Owner / Admin は必要に応じて Task priority を引き上げ可能」とする。引き上げの Capability はまだない。この入口は Priority を受けず、常に `normal` で入れる。**推奨: 今のまま。** 引き上げは、Capability（`admin` の System の Capability など）を決める別の Decision で足す。
- Budget の Preset: [Decision 0007](0007-task-queue-budget-and-loop-policy.md) は Standard / Long / Unlimited の数値を決め、Unlimited に別の上限を置かないことを承認したが、誰がどれを選べるかは決めていない。この入口は `project.task.run` を持つ人に 3 つとも選ばせる（既定は Standard）。**推奨: 今のまま。** User / Admin の Quota（REQUIREMENTS の「User Quota / Unlimited」）は Task の Budget とは別に効く。Unlimited を Owner / Admin に限るなら、それを決める。

## 0093 の手順について

Decision 0093 の 5 の確認（段 3 の後の小さな Task）は、この入口で行う。手順は `docs/DEPLOYMENT_UPDATE.md` の段 3 の Checklist に書いた（`PAW_ORCHESTRATOR_WORKERS=1` にして、`新しいタスク` で Repository を `target` にし、読むだけを指示文で頼む。終わったら `evaluating` の Task をキャンセルする）。0093 の本文は変えない。1 の A が入ったら、手順を `read_only` に変える。

## 決めてほしいこと

1. 読み取りだけの Task を、A（`read_only` の印、Worker を拒む、`referenced` だけで始められる、DAG の成功で完了）で別 Issue にするか。推奨: はい。
2. 二重送信は今は A（Idempotency Key なし）でよいか。推奨: はい。
3. 作成の入口は Priority を受けずに常に `normal`、Budget の Preset は `project.task.run` を持つ人が 3 つとも選べる、でよいか。推奨: はい。

## リスク

- 1 を決めるまで、#208 の確認の Task は指示文だけで読み取りに留める。Worker の Node ができても書くのは `paw/` の branch と worktree だけで、PR は Integration Gate の後（[Decision 0052](0052-integration-push-and-pull-request.md)）。確認の後は Task をキャンセルする。
- 2 の A のまま、二重に作られた Task は両方とも GPU を使う。確認の時は Queue が空なことを先に見る。

## 承認後の扱い

- 1 が承認されたら、Issue を作って実装し、`docs/DEPLOYMENT_UPDATE.md` の手順を `read_only` に変える。
- 2・3 は今の実装のままで、`apps/backend/README.md` の記述を「承認済み」に変える。
