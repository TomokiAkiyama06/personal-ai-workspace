# Project / Repository / Member の HTTP API の方針（Route の Guard、自分の一覧の Guard と形、詳細の組み立てと表示名、Repository の登録を Request の中で行う、Error の対応、Step-up、この PR に入れない操作と招待のための User の検索）

- Status: Approved
- Approval: 2026-10-01、Humanが作業Session内で、判断点ごとの説明（推奨つき）を受けたうえで直接回答して承認（判断点1〜8、すべて推奨どおり。末尾の「承認時の決定」）
- Date: 2026-10-01
- Scope: Issue [#184](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/184)（UI #47・PR #176 の接続先）。`paw_backend/api/v1/projects.py`（`/api/v1/projects/*`、`/api/v1/admin/projects`）、`paw_backend/app.py`（`app.state.projects` / `app.state.repositories`、`create_app(gh_runner=...)`）、`ProjectService.list_members_named` / `list_invites_named`（`projects/store.login_names_of_members`）、`apps/web/src/projects/api.ts`。Migration はない
- Supersedes: なし。[Decision 0004](0004-rbac-capability-and-audit-policy.md)（RBAC と Audit）、[Decision 0008](0008-project-membership-and-lifecycle-policy.md) / [Decision 0022](0022-project-lifecycle-capabilities.md)（Project の Membership と Lifecycle）、[Decision 0017](0017-repository-registration-policy.md)（Repository の登録）、[Decision 0044](0044-web-app-serving-and-session.md)（Web App と Session、CSRF）はどれも書き換えない

## 背景

`ProjectService`（PAW-026）と `RepositoryService`（PAW-027）は Merge 済みで、どちらも自分で Authorizer に判定させ、Audit を残す。Web の プロジェクト の画面（PAW-061、PR #176）は `ProjectsSource` の形で先に Merge され、Route がないため本番では「まだ表示できません」を出していた。次のことはすでに決まっている。

- `/api/v1` のすべての Route は `require_capability` で守るか、公開の一覧に理由つきで載せる（`tests/test_authz_routes.py`）。Project の Route は「Path Parameter と保存済みの状態から Backend が Resource を作る」形（Backend README の「Endpoint への適用」）。
- Route の Guard と Service の両方が同じ Capability を判定し、1 回の操作で Audit の行が 2 つになる形は Decision 0058 の 3（Approved、Full GPU Mode）にある。
- 状態を変える Request は Same-Origin（Origin の検査、`SameSite=Strict` の Cookie）で守る（Decision 0044 の 3）。
- Member でない User には Project の存在を教えない（Decision 0008、`ProjectService` の docstring）。管理（Archive・削除・復元）と中身の読み取りは別（Decision 0004）。

**次の選択は、要件も上の Decision も決めていない。** 実装は各点で下の推奨を採っている。

## 提案（番号は「決めてほしいこと」の番号）

### 1. Project の Route は、Service と同じ Capability を、保存済みの Project から作った Resource で Guard する（Member でない・存在しない・Deleted はすべて 403）

- `GET /projects/{id}`・`/repositories`・`/members` は `project.read`、`archive` / `unarchive` / `begin-deletion` / `restore` は `project.lifecycle.manage`、役割の変更は `project.members.manage`、Repository の登録は `project.repo.add`。Resource は URL の ID と、Project の行の状態（Active / Archived / Pending deletion）から作る（`_project_of`）。Principal の Project の Role は Session が保存済みの Membership から読んだもの（`SessionPrincipalProvider`）。
- Service は自分の Transaction でもう一度判定する（Service は HTTP の経路に頼らない。0058 の 3 と同じ）。`REQUIRED` の Capability（Lifecycle・役割の変更・登録・作成・管理者の一覧）は、1 回の操作で Audit の行が 2 つになる。`project.read` は許可を書かない（`DENIED_ONLY`）ので、読み取りでは増えない。
- Member でない User、存在しない Project、Deleted の Project、形の正しくない ID は、Guard がすべて同じ **403 `forbidden`**（Body は固定）にする。Service の「見つからない」（404 `not_found`）は、Guard と Service の判定の間に変わった場合だけ出る。
- 理由: README の既定の形で、Route の一覧の Test がそのまま効く。403 と 404 を使い分けないので、存在を教えない性質は Service の 404 と同じに保たれる。
- 代わりに、Guard を置かず Service の判定だけにする案（`tests/test_authz_routes.py` の公開一覧に理由つきで載せる。Audit は 1 行、Member でない User には 404）もある。

### 2. 自分の Project の一覧（`GET /projects`）の Guard は `account.read`（新しい Capability は作らない）

- `ProjectService.list_projects` は自分の Membership を読むだけの Self Service で、Capability を持たない（Decision 0022）。Route には Guard が要るので、すべての人間の Role が持ち、Agent が持たず、読み取り専用（拒否だけを Audit）の `account.read` を使う。
- 理由: Policy の表を変えずに「Session のある人間だけ」を表せる。自分の Membership は自分の Account の情報に近い。
- 代わりに、`project.list` のような新しい Capability（`Scope.SYSTEM`、人間の Role、委任不可、読み取り専用）を足す案もある（Policy の表と `tests/test_authz_policy.py` の変更）。

### 3. 一覧の形: 3 つの状態を 1 回で返し、Paging はしない（状態ごとに最大 1000 件、超えたら `truncated: true`）

- `GET /projects` は `{projects: [...], truncated}`。各行は `id`・`name`・`status`・`my_role`・`repository_names`・`deletion_scheduled_at`（画面の `ProjectSummary` と同じ）。Active、Archived、Pending deletion（自分が Manager のものだけ。Service のとおり）の順で、各状態の中は Service の順（新しい順）。
- `repository_names` は、Project ごとに `RepositoryService.list_repositories`（`project.read`。ACL Override で `read` が閉じた Repository は入らない）を呼んで作る。Pending deletion の Project は読めないので空。Project の数だけ Transaction が増える（個人・小さなチームの規模を想定）。
- 理由: 画面の `list()` が 1 回で全体を受け取る形で、Archived を分けて表示するのは画面の役目（REQUIREMENTS.md「通常の Project 一覧では Archived を分離」）。
- 代わりに、状態ごとの Paging（`?status=&cursor=`）にする案、Repository の名前をまとめて読む Query を Service に足す案もある。

### 4. 詳細は 1 回の `GET` で Project・Repository・Member を返す。Member の表示名は `login_name`、招待は Manager にだけ

- `GET /projects/{id}` は `ProjectService.get_project`、`RepositoryService.list_repositories`、`ProjectService.list_members_named` を順に呼ぶ（別々の Transaction）。Repository は `acl`（`null` = 継承、配列 = Override。空は「アクセス不可」だが、その Repository は一覧に入らない）を持つ。
- Member の表示名のために、`ProjectService` に `list_members_named`（`project.read`）と `list_invites_named`（`project.members.manage`）を足した。認可と並びは `list_members` / `list_invites` と同じで、名前はその Project に Membership の行がある User のものだけを同じ Transaction で読む（`store.login_names_of_members`）。`creator` は `projects.created_by` と比べる。
- 招待（`status: "invited"`、`invite_expires_at`）は、Session の Role が Manager のときだけ `list_invites_named` を呼んで足す（`project.members.manage` は `REQUIRED` なので、**Manager が詳細を開くたびに Audit の行が 1 つ残る**）。その間に降格された Manager は招待なしで受け取る。
- 理由: 画面の `detail()` の形そのまま。Member の名前は REQUIREMENTS.md の Member の表示に必要で、Workspace の中の表示名であり Private Data ではない。
- 代わりに、招待を別の Route（`GET /projects/{id}/invitations`）に分けて、画面が必要なときだけ読む案もある。

### 5. Repository の登録は HTTP の Request の中で行う（Job にしない）。GitHub に作る経路は `gh_runner` を渡した Deployment だけ

- `POST /projects/{id}/repositories` の Body は `source` で 4 つに分かれる（`existing_path`: `path`・`name`、`github_clone`: `url`（`owner/repo` か `https` の URL）・`name`・`branch`、`new_local`: `name`・`default_branch`、`new_github`: `name`・`private`（既定 `true`）・`default_branch`）。Service の Method をそのまま呼び、`201` と登録した Repository を返す。
- Clone は Repository の Policy の Clone の Timeout（既定 900 秒）まで Request を開いたままにする。
- git は `create_app(git_runner=...)`（既定 `SubprocessGitRunner`。Backend 自身の Linux User でしか動かない。Decision 0029 の `SshGitRunner` で他の User に届く）で動かす。GitHub に新しく作る経路（`new_github`）と、Private の Clone の Credential Helper は、`create_app(gh_runner=SubprocessGhRunner(...))` を渡したときだけ有効で、渡さなければ `503 github_unavailable`。今の `server.py` は渡さない。
- 理由: 登録は Manager の明示の操作で、頻度が低く、結果（名前の重複、Path の拒否、git の失敗）をその場で見せたい。Job と進捗の仕組みを作ると、この Issue を大きく超える。
- 代わりに、`202` を返して Background で行い、状態を別の `GET` で読む案（Full GPU Mode と同じ形。0058 の 3）もある。`server.py` で `gh` を既定で使う案もある（Deployment の Issue で決める）。

### 6. Error の対応

| Service の Error | HTTP | `code` |
| --- | --- | --- |
| 権限の拒否（Audit が書けない） | 503 | `service_unavailable` |
| 権限の拒否（その他） | 403 | `forbidden` |
| 入力の不正（`InvalidProjectInputError` / `InvalidRepositoryInputError`、Pydantic） | 422 | `validation_error` |
| Project・Repository・Member が見つからない | 404 | `not_found` |
| Project の状態が許さない（Archived への登録など） | 409 | `project_state` |
| 確認の名前が一致しない | 422 | `confirmation_mismatch` |
| 復元の期限切れ / Manager がいない / 最後の Manager / Account が有効でない | 409 | `deletion_window_closed` / `no_manager` / `last_manager` / `account_not_active` |
| Lock 待ちの Timeout | 503 | `project_busy` / `repository_busy` |
| Repository の名前の重複・上限・Remote の重複・作業コピーの重複 / 作成中 / 消えた | 409 | `repository_name_taken` など、Error の `code` のまま |
| Path・Remote の拒否 | 422 | `path_rejected` / `remote_rejected` |
| Linux Account がない | 409 | `linux_account_unavailable` |
| git / gh の失敗 | 502 | `git_failed` / `gh_command_failed` |
| GitHub が使えない | 503 | `github_unavailable` |
| Database がない構成 | 503 | `projects_unavailable` |

Message は Service の固定の文（閉じた語彙だけで作られ、入力・Path・git の出力を含まない）。

### 7. Step-up は求めない（削除の開始・役割の変更とも）

- 削除の開始は、Project の名前の正確な入力（Decision 0008）と 30 日の保留（その間は復元できる）で守る。役割の変更は Manager の操作で、最後の Manager は残る。
- 理由: どちらも Decision 0025 の 6 の「Step-up が要る操作」の表になく、元に戻せる。Owner / Admin が Member でない Project を削除する場合も、Approved の Decision は Step-up を求めていない。
- 代わりに、Owner / Admin が Member でない Project の削除を始めるとき（または全員）に Passkey の Step-up を求める案もある（Decision 0025 の表に足す）。

### 8. この PR に入れない操作と、招待のための User の検索

この PR は、画面が使う読み取りと操作（一覧・詳細・作成・Lifecycle・役割の変更・Repository の登録）と、管理者の一覧（#84）までにした。次は入れていない（Issue #184 は閉じない）。

- 招待（`invite_member`）と、招待のための User の検索、自分宛ての招待の一覧・承諾・辞退（`list_my_invites` / `accept_invite` / `decline_invite`）、退出（`leave_project`）、Member の削除・招待の取り消し（`remove_member`）、名前と説明の変更、Repository の ACL の変更（`set_acl`）・削除・Remote、作業コピー（`create_checkout`）。
- **招待のための User の検索**は、Workspace の User の `login_name` を、その User と Project を共有していない Manager にも見せることになる（新しい読み取りの Model）。推奨は、**検索を作らず、招待する人の `login_name` を正確に入力して招待する**（`POST /projects/{id}/invitations {login_name, role}`。存在しない・有効でない User は区別しない `invitee_unavailable`）。代わりに、前方一致の検索（Manager だけ、最大 N 件、Rate Limit つき）を作る案もある。

## 代替案

- **Guard を置かない**: 1 のとおり。
- **新しい Capability で一覧を守る**: 2 のとおり。
- **一覧の Paging**: 3 のとおり。
- **登録を Job にする**: 5 のとおり。

## 決めてほしいこと

1. Project の Route を、Service と同じ Capability で、保存済みの Project から作った Resource で Guard し、Member でない・存在しない Project を同じ 403 にするか（推奨: はい。書き込みは Audit が 2 行）。
2. 自分の一覧の Guard に `account.read` を使うか、新しい Capability を足すか（推奨: `account.read`）。
3. 一覧を 3 つの状態まとめて 1 回で返し、Paging をしない（状態ごとに 1000 件まで、`truncated`）でよいか（推奨: はい）。
4. 詳細を 1 回の `GET` で返し、Member の表示名に `login_name` を使い、招待は Manager にだけ（開くたびに Audit 1 行）でよいか（推奨: はい）。
5. Repository の登録を Request の中で行い（Clone は最大 900 秒）、GitHub に作る経路は `gh_runner` を渡した Deployment だけにするか（推奨: はい。`server.py` での `gh` の既定は Deployment の Issue で）。
6. 6 の Error の対応でよいか（推奨: はい）。
7. 削除の開始と役割の変更に Step-up を求めないか（推奨: 求めない）。
8. 招待は User の検索を作らず、`login_name` の正確な入力で行うか（推奨: はい。8 の残りの操作は後続の PR）。

## 承認時の決定（2026-10-01）

Human は、作業 Session で判断が必要な点について推奨つきの説明を受け、直接回答して承認した（判断点1〜8、すべて推奨どおり）。
