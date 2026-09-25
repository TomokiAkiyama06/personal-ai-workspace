# Repository の登録と Checkout の方針

- Status: Proposed
- Date: 2026-09-25
- Scope: PAW-027（Repository Registration / Per-user Checkout）と、Repository・Checkout を使う以降の Issue（PAW-028 GitHub 接続、PAW-031 / PAW-034 の Working Set、PAW-035 Worktree、PAW-061 Project / Repo UI）
- Supersedes: なし
- Approval: 未承認（Human の承認待ち）

## 背景

[REQUIREMENTS.md](../../REQUIREMENTS.md) の「Project / Repository registration and per-user checkout」は、次を定めている。

- Project は Workspace DB 上の論理的な単位で、User / Agent が編集する Git checkout は **Linux User ごとに分離**する（例: `/home/<user-a>/workspaces/<project>/<repo>`）。
- 追加の経路は 3 つ（GitHub から clone、Ubuntu 上の既存 Repository、新規作成）。
- Project Repository へ `AGENTS.md`、`MEMORY.md`、`.personal-ai/` などの管理ファイルを**自動追加しない**。
- Project の削除は、GitHub Repository、Issue / PR、Remote branch、Ubuntu の local checkout を**自動で削除しない**。
- Repository の追加は Manager の権限、Repo ACL の override は Project の Role を**狭める**だけ（[Decision 0004](0004-rbac-capability-and-audit-policy.md)、承認済み）。
- Tool Broker は、Backend が登録した Remote で URL を Repository に結び付け、Path で入れ子の Repository の両方の ACL を効かせる（[Decision 0006](0006-tool-broker-policy.md) の 8、承認済み）。

一方で、次は要件も Backlog も決めていない。PAW-027 の実装は、動かすために下の選択を置いた。
[AGENTS.md](../../AGENTS.md) は、仕様にない重要判断を勝手に確定しないと定める。そこで、実装が置いた選択を一覧にし、Human が承認または変更できるようにする。
**この Decision は未承認である。** 承認前の実装は、下の各点を前提にしている（承認されなければ、この Decision を書き換えず、新しい Decision から `Supersedes` する）。

実装は [Backend README](../../apps/backend/README.md) の「Repository Registration / Per-user Checkout」に書いている。

## 提案

### 1. Checkout の置き場所

- Backend が作る Checkout は `<home>/workspaces/<project directory>/<repository name>` に置く。`<home>` は Linux Account の Home を解決した実際の Path、`workspaces` は設定（`PAW_REPOSITORY_WORKSPACE_SUBDIR`）。
- `<project directory>` は `<slug>-<Project ID の先頭 8 桁の 16 進>`。slug は Project 名の ASCII の英数字（小文字、それ以外の連続は `-`、40 文字まで、空なら `project`）。Project を改名しても、保存済みの Path は動かない。
- Backend が作る Directory（`workspaces`、Project の Directory）は 0700 とする。作る前に、Home から 1 段ずつ `O_NOFOLLOW` で開き、Symbolic Link と他人の Directory を拒否する。最後の Directory は `mkdir` で作り、既にある物（何であれ）は再利用しない。
- 要件の `<project>` を、名前だけにせず ID を足したのは、同じ名前の Project が 2 つあっても衝突しないため（名前は一意ではない）。

### 2. Repository の名前

- Project の中の正規名は `A-Z a-z 0-9 . _ -` の 100 文字まで、先頭は英数字、`.git` で終わらない。**大文字小文字を区別せず一意**とする（GitHub と同じ。名前は Directory 名になる）。
- 名前の変更は実装しない（変えると、既に作られた Directory と食い違う）。

### 3. Workspace の User と Linux Account の対応

- 要件は「Linux User ごと」とだけ言い、Workspace の User と Linux の User の対応を定めていない。
- 提案: **`users.login_name` を Linux の User 名とする**（`LoginNameAccountDirectory`）。置き換えられる継ぎ目（`AccountDirectory`）を用意した。
- System の Account は Checkout の持ち主にしない。uid が最小値（既定 1000。`PAW_REPOSITORY_MIN_LINUX_UID`）未満、`nobody`（65534 以上）、Shell が `nologin` / `false` の Account は拒否する。Login 名の規則は `root` や `www-data` を許すため、これがないと、そのような名前の User が System の Directory に書ける。最小の uid の値は**設定（`RepositoryPolicy.min_uid`）だけ**とし、`LoginNameAccountDirectory` は Policy から受け取る（別の既定値を持たない）。本番の組み立て（`RepositoryService.from_policy`）は同じ Policy から Directory と Service を作り、食い違う組み合わせは Service の構築時に拒否する。

### 4. git を動かす User

- git は **Backend の Process の Linux User として**動かす。Process の User が Checkout の持ち主の Account でなければ、実行を拒否する（`identity_mismatch`）。権限の昇格（`sudo`、`setuid` など）は実装していない。
- したがって、Backend が Service 用の 1 つの User で動く配備では、Per-user の Clone は、Account の User に切り替える実行の仕組み（User ごとの Worker、特権を分けた Helper など）を配備側が `GitRunner` として渡すまで動かない。PAW-028（`gh auth` は Linux User ごと）も同じ仕組みを必要とする。
- git の環境は許可リストだけ（`PATH` 固定、`HOME`、Locale、`GIT_*`）。`GIT_CONFIG_GLOBAL=/dev/null`、`GIT_CONFIG_NOSYSTEM=1`、Hook 無効（`core.hooksPath=/dev/null`）、`core.fsmonitor=false`、`protocol.allow=never`（許可した Transport だけ `always`。既定は `https`）、Submodule の再帰なし。Credential は渡さない・Log に出さない。Timeout（通常 30 秒、Clone 900 秒）と出力の上限（64 KiB）がある。

### 5. 「既存 Repository の登録」で確かめること

- Path は絶対で、正規の綴り（`//`、`/./`、末尾の `/` がない）、解決済み（どの成分も Symbolic Link でない）。
- 許可 Root（設定 `PAW_REPOSITORY_EXISTING_ROOTS`。既定は `{home}`、つまり**自分の Home の中**）の**内側**（Root 自身は不可）。Root は必ず `{home}` か `{user}` を含む（全員で共有する Directory は Per-user の分離に反するため、設定として拒否する）。他人の Home は Root の外になる。
- Root より下に、名前が `.` で始まる成分がない（`~/.config/gh` のような場所を Repository にしない）。
- Checkout Root（`workspaces`）自身、またはそれを含む Directory ではない。
- Directory は Account の Linux User の物で、誰でも書ける（`o+w`）のではない。`.git` は実際の Directory（`.git` の File・Symbolic Link は不可。Linked Worktree と Submodule は登録できない）で、同じ持ち主、`HEAD` / `config` が通常の File、`objects` / `refs` が実際の Directory、`objects/info/alternates` と `commondir` がない。
- git 自身に Work Tree と Git Directory を尋ね、`path` と `path/.git` に一致する（`core.worktree` などの細工を拒否）。Bare Repository は不可。
- 既定の Branch は `origin/HEAD` が指す Branch、なければ現在の Branch。どちらも決まらなければ（Detached HEAD など）拒否。Branch 名は ASCII の限定した形だけ（それ以外は拒否）。
- `remote.origin.url` は、そのまま保存しない。`https` の URL（User 情報なし）と、許可 Host の GitHub の `ssh` 形式（`git@github.com:o/r`）だけを `https` の綴り 2 つ（`.../r` と `.../r.git`）として登録する。**User 情報（Token など）を含む `https` URL は登録を拒否する**（DB に Secret を保存しないため。値は Error にも出さない）。それ以外（ローカルの Path、`file://`、`http://`）は Remote を登録しない。
- 検証は、確認した瞬間の事実である。持ち主は後でも Directory を変えられるため、Tool Broker は呼び出しごとに Path を解決し直す（登録は File のアクセス制御ではない）。

### 6. 誰が何をできるか（Capability。新しい Capability は足さない）

| 操作 | Capability | 対象 | 備考 |
| --- | --- | --- | --- |
| 登録（clone / 既存 / 新規）、削除、Remote の追加と削除 | `project.repo.add` | Project の Manager | 監査は `REQUIRED` |
| ACL override の設定 | `project.settings.manage` | Project の Manager | 監査は `REQUIRED` |
| 自分の Checkout の作成 | `project.read`（Repository の `read`）+ `workspace.use`（自分の Workspace） | Project の Member | `workspace.use` の判定が `REQUIRED` の記録になる |
| 自分の Checkout の登録解除 | `workspace.use` | 本人 | Project の状態を問わない |
| 参照 | `project.read` | Member | ACL が `read` を許さない Repository は見えない |

- Owner / Admin は Member でなければ、Repository を登録も参照もできない（Decision 0004 の 1 のとおり）。
- **Viewer も自分の Checkout を作れる**（`project.read` を持つ。Checkout は読み取りの複製）。Repository の ACL override が `read` を許さなければ、その Repository は存在しないように扱う。
- Repository の追加・削除・Remote・ACL・Checkout の作成は、Project が **Active** のときだけ。Archived の Project は読み取りだけ（参照は可、登録解除は可）。
- 判定の Audit は、Authorizer が Decision として記録する（結果ではない）。登録の Audit は `project.repo.add` の Decision で、`resource_id` が Repository の ID（新規なら新しい ID）を指す。

### 7. 削除の意味

- Repository の削除は**登録の削除**である。Repository、Remote、**全員の** Checkout の行を消す。Directory も GitHub の Repository も消さない（要件）。持ち主の Directory はそのまま残り、登録されなくなる。
- **Clone の実行中に登録解除（`remove_checkout` / `remove_repository`）があったとき、実行中の呼び出しは Directory を消さない**（`CheckoutGoneError` で終わる）。呼び出し自身の失敗・Cancel の後始末は、自分の `pending` の行がまだあるときだけ行う（行を先に消せたときだけ Directory と、他に Checkout のない新規の Repository を消す）。登録解除は File に触れない約束であり、解除の後に持ち主が足した作業を守るため。Repository が消えた後の GitHub の Remote の登録は、行が消えたことを確かめ（`FOR SHARE`）、`CheckoutGoneError` として返す。
- Checkout の登録解除も Directory を消さない。残った Directory が同じ Path にあると、次の Checkout の作成は「既にある」として拒否する（上書きも再利用もしない）。
- Project の削除（30 日後の Deleted）に伴い、`purge_projects` が、**Deleted になっている** Project の Repository の登録を消す（`ProjectService.purge_expired` が返した ID を渡す。Decision 0008 の 2）。Directory と GitHub の Repository は残る。

### 8. Remote のない Repository

- 「新規 Local」の Repository は Remote を持たない。**作った User だけが Checkout を持つ**。他の User の Clone 元になる登録済みの Remote がないため、`create_checkout` は拒否する（他の User の Home を Clone 元にしない）。
- 後から Remote を足す（`add_remote`）と、他の Member が Clone できる。

### 9. Clone できる Host

- `https://<host>/<owner>/<repo>` の Host は許可リスト（設定 `PAW_REPOSITORY_CLONE_HOSTS`、既定 `github.com`）に完全一致するものだけ。任意の URL を許すと、Backend の Process が内部 Network の Host へ接続する（SSRF）。IP Address は許可リストに入れられない。
- `owner/repo` の短い綴りは、許可リストの最初の Host とみなす。

### 10. GitHub の Credential は PAW-028

- Backend は GitHub の Token を持たず、読まず、渡さない。Clone は User 自身の環境（`gh auth`、PAW-028）に任せる。今の実行環境は Global の git 設定を読まないため、Private Repository の Clone は PAW-028 が `extra_config`（Credential Helper）を足すまで通らない。
- 「GitHub にも新規作成」は `GitHubGateway`（継ぎ目）を呼ぶ。既定の実装は拒否する。PAW-028 が、User 自身の GitHub の権限で作成する実装を渡す。作成後に登録が失敗しても、GitHub の Repository は削除しない（削除の権限を Backend に持たせない）。ログに 1 行残す。

### 11. 入れ子の Repository

- 入れ子は禁止しない。同じ User の Checkout の Path の包含関係から、都度求める（Table は持たない。移動・削除でずれる複製を作らない）。`scope_entries` が、要求した Repository の Entry に続けて、包含される・包含する**他の** Checkout（`ready` のもの、それぞれの Project の ACL つき）を返し、Tool Broker が両方の ACL を効かせる（Decision 0006 の 8(b)）。Working Set に入れる責務は Orchestrator にある。

### 12. 上限

- 1 つの Project の Repository は 100 まで、1 つの Repository の Remote は 8 まで（Tool Broker の `MAX_REMOTES`）。いずれも暫定値。
- 途中で終わった Clone の予約（`pending`）は、Clone の Timeout の 2 倍を過ぎると古いとみなし、同じ User の次の作成が置き換える。**その Process が残した Directory は自動では消さない**（後から中身が変わっているかもしれないため）。次の作成は「既にある」で止まり、持ち主が消してから再実行する。

### 13. 管理 Markdown を入れない

- Backend が Repository に書くのは、`git clone` / `git init` が作るものだけ。Working Tree に File を足さず、Commit せず、`.git` へ書くのは `git remote add origin`（GitHub に新規作成した場合だけ）だけ。`git init` は Template を使わない（`--template=`）。

## 採らなかった案

- Checkout を Project で 1 つ共有する: 要件が禁じる（複数 User が 1 つの Working Tree を編集しない）。
- Directory を `<project 名>` だけにする: 同名の Project が衝突し、名前に使えない文字が入る。
- `.git` の File（Linked Worktree・Submodule）も登録する: Git Directory が別の場所を指し、他の User の Repository を指す細工と区別できない。V1 は拒否する。
- Home の外の共有 Directory（`/srv/repos`）を登録する: 複数 User が同じ Working Tree を編集することになる。
- 既存の Directory を、登録済みの Repository の自分の Checkout として取り込む: Remote の照合が要る（V1 では作らない）。
- Repository の `origin` の URL をそのまま保存する: Token を含む URL を DB に残す。
- Backend が `sudo -u` で User を切り替える: 権限の昇格を Backend に持たせる。配備の判断と別 Issue に分ける。
- Table で入れ子を保存する: 移動・削除で古くなる複製になる。
- ACL の設定に新しい Capability を足す: Decision 0004（承認済み）の Capability 一覧を変える。既存の `project.settings.manage`（Manager）を使う。

## Human の判断点（推奨つき）

1. Workspace の User と Linux の User の対応は `login_name`（3）。推奨: 承認。Mapping Table や LDAP が要るなら、`AccountDirectory` を置き換える。
2. Checkout の置き場所と Project Directory の名前（1）。推奨: 承認。
3. git を Backend の Process の User で動かし、別の User なら拒否する（4）。推奨: 承認したうえで、User を切り替える実行の仕組みを配備の Issue にする。
4. 既存 Repository の検証の範囲（5）、特に隠し Directory・`.git` の File・Linked Worktree の拒否。推奨: 承認。
5. Viewer にも Checkout を許す（6）。推奨: 承認（読み取りの複製）。書き込みの前提にはしない。
6. ACL の設定を `project.settings.manage`（Manager）にする（6）。推奨: 承認。
7. 削除は登録だけ（7）。推奨: 承認（要件のとおり）。
8. Remote のない Repository は他の User が Clone できない（8）。推奨: 承認。
9. Clone の Host は `github.com` だけを既定にする（9）。推奨: 承認。GitHub Enterprise は設定で足す。
10. 上限（Repository 100、Timeout）は暫定値（12）。推奨: 暫定値として承認。

## リスク

- Backend が Service 用の 1 つの User で動く配備では、Clone・既存 Repository の検証が動かない（4）。Human が、User を切り替える仕組みを決めるまで、Per-user の Checkout は実運用に使えない。
- Path の検証は、確認した瞬間の事実で、持ち主は後から Directory を差し替えられる（5）。Backend が Agent に渡す Path は、Tool Broker が呼び出しごとに解決し直すことに依存する。
- Private Repository の Clone と GitHub への作成は PAW-028 まで動かない（10）。
- `users.login_name` と Linux の User 名が一致しない環境では、Account を引けない（3）。

## 承認後の扱い

承認されたら、この Decision の Status を Approved にし、承認時の決定を末尾に追記する（数値は暫定値として扱い、変更は設定・定数と Test の期待値で行う。Repository 名・Branch・URL・Path の長さと形式は DB の CHECK 制約にも書かれているため、変えるには新しい Migration が要る）。
承認されない点があれば、この Decision を書き換えず、新しい Decision から `Supersedes` する。

## 決めてほしいこと

1. Workspace の User と Linux の User の対応（`login_name`）でよいか。
2. Checkout の置き場所（`<home>/workspaces/<slug>-<id8>/<name>`）でよいか。
3. git を Backend の Process の User で動かし、別の User では動かさない方針でよいか。User を切り替える仕組み（誰が、どの権限で）は別の Issue にするか。
4. 既存 Repository の検証の範囲（隠し Directory、`.git` の File、Linked Worktree の拒否）でよいか。
5. Viewer に Checkout を許してよいか。
6. ACL の設定を Manager（`project.settings.manage`）に限ってよいか。
7. 削除を登録の削除だけにする方針でよいか。
8. Remote のない Repository を、他の User が Clone できないままにしてよいか。
9. Clone の Host の既定を `github.com` だけにしてよいか。
10. 上限（Project あたり 100 Repository、Remote 8、Timeout）を暫定値にしてよいか。
