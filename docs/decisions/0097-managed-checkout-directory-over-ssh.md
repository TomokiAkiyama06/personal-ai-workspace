# `git: ssh` の配備で、Managed Checkout の Directory をその Linux User として作る（Wrapper の `paw-dir/v1`）

- Status: Approved
- Approval: 2026-10-22、Humanが作業Session内で、判断点ごとの説明（推奨つき）を受けたうえで承認（approved as recommended, all 6 points: 1 は A（Wrapper の `paw-dir/v1` の `mkdir` / `rmdir` で、その User として作る）、2 は Backend の Path の確認を Backend に残し、Home と `<home>/workspaces` を `paw` が辿れること（ACL の `u:paw:x`。`o+x` ではない）を `git: ssh` の配備の要件にする、3 は新しい Project の Directory に Root の Access ACL を `x` だけに削って写す（ACL がなければ Root の Group / Other の `x` だけ）・継いだ Default ACL は外す・Checkout は ACL なしの `0700`、4 は Root（`<home>/workspaces`）を Wrapper が作らない、5 は失敗した登録の後始末を空の Directory の `rmdir` だけにする、6 は Backend の `PAW_REPOSITORY_EXISTING_ROOTS` の既定 `{home}` を変えず、`git: ssh` の配備では `{home}/workspaces` にするよう別の小さな PR で手順書に書く）
- Date: 2026-10-22
- Scope: Issue [#285](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/285)。`SshGitRunner`（[Decision 0029](0029-per-user-git-runner-ssh.md)）で git を動かす配備（[Decision 0093](0093-production-runtime-config.md) の `[git] runner = "ssh"`）の、Managed Checkout（GitHub から Clone・新規（ローカル）・新規（GitHub）・他の人の Checkout の作成）の Directory の作成と、失敗したときの後始末だけ。`SubprocessGitRunner`（Backend の User が Account と同じ配備）は変えない。
- Supersedes:
  - [Decision 0017](0017-repository-registration-policy.md) の 1 の 3 点目（「Backend が作る Directory（`workspaces`、Project の Directory）は 0700 とする。作る前に、Home から 1 段ずつ `O_NOFOLLOW` で開き…最後の Directory は `mkdir` で作り…」）を、**`git: ssh` の配備に限って** 下の 2・3 で置き換える。`SubprocessGitRunner` の配備では 0017 のまま。
  - [Decision 0029](0029-per-user-git-runner-ssh.md) の 3（Wrapper が受け付けるのは git の副コマンドの表だけ）に、git ではない Protocol Tag `paw-dir/v1` を 1 つ足す（下の 2）。0029 の 3 の `clone` / `init` の `destination` の規則（呼び出し元の Linux User 自身の Checkout Root の配下）はそのまま。0029 は、その Directory を **誰が作るか** を決めていなかった。
  - 0017・0029 の本文は書き換えない。

## 背景

2026-10-22 の本番（Decision 0093 の段 3 の確認）で、Web の「リポジトリを登録」の **GitHub から Clone**・**新規（ローカル）**・**新規（GitHub）** が、必ず `422 path_rejected`（`not_found`）になった。

- git は `SshGitRunner` で、ログインした人の Linux User（例 `tomoki`）として Wrapper（`/usr/local/lib/paw/paw-git-wrapper`）の中で動く（Decision 0029）。
- しかし Managed Checkout の Directory（`<home>/workspaces/<project>/<repo>`）は、Backend の Process（Linux User `paw`）**自身** が作る: `paths.plan_checkout_path` が途中の Directory を `lstat` し、`paths.create_checkout_directory` が `os.mkdir` する（`repositories/service.py` の `_create_managed` と `create_checkout`）。
- `paw` は他の User の Home に書けない（辿る権限を足しても書けない）。作れたとしても持ち主は `paw` になり、「途中の Directory はすべて Account のもの」の検査（`NOT_OWNER`）に落ちる。
- 後始末（`paths.remove_directory`、`shutil.rmtree`）も `paw` として動き、Account の Directory を消せない。

**既存のディレクトリ** の登録は `mkdir` をしないので、Home と `workspaces` を `paw` が辿れ（2026-10-22 の確認は `o+x`。手順書は ACL の `setfacl -m u:paw:x` に改めた）、Repository が Wrapper の Root（`<home>/workspaces`）の下にあれば通る（2026-10-22 に確認。手順書は [PR #288](https://github.com/TomokiAkiyama06/personal-ai-workspace/pull/288)）。

一方、Backend の Process は git のほかにも、Checkout の Path を **自分で** 読む:

| どこで | 何をするか | `paw` に要るもの |
| --- | --- | --- |
| `plan_checkout_path` | `<home>/workspaces`・Project の Directory の `lstat`（ない Directory は飛ばす） | Home の `x` |
| `read_checkout_identity`（作った後・既存の登録） | Checkout の `realpath(strict)`・`lstat`（`st_dev`・`st_ino` を記録） | Home・`workspaces`・Project の Directory の `x` |
| `inspect_checkout_root`（Scope を作るたび。Decision 0017 の 11） | 同じ | 同じ |
| `check_existing_repository`（既存の登録） | Checkout と `.git` の中の `lstat` | Checkout の中まで `x` |
| `locate_directory_identity`（消えた Checkout を探す） | `scandir` | 読めない Directory は飛ばす（Best effort） |

Task の worktree（`<home>/workspaces/.paw-worktrees/<task>/<attempt>/<repo>/<node>`）は、`git worktree add` が足りない親の Directory も含めて **その User として** 作る（`integration/git.py` の `add_worktree` / `attach_worktree` は Wrapper を通る）。Backend は worktree の Directory を作らず・消さず・`lstat` もしない（Tool Broker の `realpath` は `strict` でなく、辿れない成分は Link でないものとして扱う）。そのため worktree は **この問題に当たらない**（`tests/test_ssh_git_wrapper.py` の `test_the_worktree_commands_of_decision_0036_run_pinned` が、`.paw-worktrees` のない Root で、Wrapper を通した `worktree add` が親の Directory ごと作ることを確かめている）。Mode は `sshd` の Session の umask（通常 `022` で `0755`）による。

## 提案

### 1. 誰が作るか

`git: ssh` の配備では、Managed Checkout の Directory（Project の Directory と、Checkout の Directory）を **その Linux User 自身が** 作る。Backend は Wrapper に頼み、自分では `mkdir` も `rmtree` もしない。Backend が Path を確かめる処理（上の表）は Backend の Process に残す（下の 4）。

### 2. Wrapper の `paw-dir/v1`（推奨の案 A）

Wrapper（`paw_git_wrapper.py`）に、git ではない Protocol Tag `paw-dir/v1` を足す（`paw-sandbox/v1` と同じく、先頭の語で分ける。Decision 0083 の 6）。Wire Format は Decision 0029 の 2 と同じ（語ごとに `shlex.quote`）で、ちょうど 3 語。

```text
paw-dir/v1 mkdir <path>
paw-dir/v1 rmdir <path>
```

`<path>` の規則（1 つでも外れれば 126 で拒否。git も何も起動しない）:

- 正規の絶対 Path（`//`・`.`・`..`・末尾の `/` がない）で、Root（`--root=`。既定 `<home>/workspaces`）の **ちょうど 2 段下**（`<root>/<project directory>/<repository name>`。Backend が作る形そのもの）。Root は設定の綴りか、Symbolic Link を解決した綴り（`paw-git-run/v1` の `Places.check` と同じ）。
- 2 つの成分は `[A-Za-z0-9][A-Za-z0-9._-]{0,254}`（`.` で始まらない。よって `.paw-worktrees` の中には作れない）。

`mkdir` の手順（Decision 0017 の 1 と同じ保証を、その User として）:

1. Root は既にある Directory で（ないなら `root_unavailable`。Wrapper は Root を作らない。下の 6 の 4）、Symbolic Link を解決した Root を `O_DIRECTORY | O_NOFOLLOW` で開き、その User の持ち物で `o+w` でないこと。
2. Project の Directory: `mkdirat`（なければ。あれば使う）→ `O_DIRECTORY | O_NOFOLLOW` で開く（Symbolic Link なら `symlink`、Directory でなければ `not_a_directory`）→ その User の持ち物で `o+w` でないこと（違えば `not_owner` / `world_writable`）。**この呼び出しで作ったときだけ**、Root と同じだけ辿れるようにし、それ以上は与えない（umask に依らない）: まず継いだ ACL（Access・Default）を外して `0700` にし、Root に Access ACL があれば（配備の `setfacl -m u:paw:x <home>/workspaces`）、その ACL の所有者以外の Entry（Named User・Group・Mask・Other）を `x` だけに削ったものを付ける（例: `drwx--x---+`、`user:paw:--x`）。Root に ACL がなければ、`0700` に Root の Group / Other の `x` だけを足す（Root が `0700` なら `0700`）。`paw` が中の Checkout を `lstat` できて、一覧は見えず、他の User には Root より多くを与えない。既にある Project の Directory は変えない。
3. Checkout の Directory: `mkdirat` で作る。既に何かあれば（何であれ）`exists`（再利用しない）。開き直して、継いだ ACL（Access・Default）を外し、Mode を `0700` にする（`fchmod`）。Default ACL を残さないので、git が Checkout の中に作る Directory に `paw` の Entry が広がらない。
4. 結果は終了コード 0（stdout に `created`）。2・3 の Path の問題は終了コード **1** と stdout の 1 語（`exists`・`symlink`・`not_owner`・`world_writable`・`not_a_directory`・`not_found`）。Backend はそれを `PathRejectedError` の同じ名前の `PathProblem` にする。

`rmdir` の手順（失敗した登録の後始末だけ）:

- 同じく Root から `O_NOFOLLOW` で Project の Directory まで開き、Checkout の Directory を `unlinkat(AT_REMOVEDIR)` で消す。**空の Directory だけ** を消す（中身があれば終了コード 1 の `not_empty`。消さない）。既にない（Project の Directory ごとない）ときは 0 の `removed`。Project の Directory は消さない（0017 と同じ）。

Log は 1 行（`accepted user=alice directory=mkdir` / `rejected user=alice reason=<理由>`）で、Path を書かない（Decision 0029 の Log の規則）。

### 3. Backend

- `paw_backend.repositories.directories` に継ぎ目 `CheckoutDirectories`（`create(path, account)`・`remove(path, account) -> bool`）を置く。`LocalCheckoutDirectories` は今の `paths.create_checkout_directory` / `paths.remove_directory`（0017 のまま）、`paw_backend.repositories.ssh.SshCheckoutDirectories` は上の `paw-dir/v1` を `SshGitRunner` と同じ鍵・同じ `ssh` の Option・同じ Timeout（`git_timeout_s`）で送る。
- `RepositoryService.from_policy` は、`runner` が `SshGitRunner` なら `SshCheckoutDirectories`、それ以外は `LocalCheckoutDirectories` を使う（設定は増やさない。`SshGitRunner` を使う配備は、Backend の User が Account と違う配備そのもの）。
- 終了コード 255 は今の `ssh_unavailable`、126（古い Wrapper の `bad_protocol` を含む）とそれ以外は `GitCommandError(mkdir|rmdir, nonzero_exit)` にする（Path の問題だと偽らない）。
- 失敗した登録の後始末で、`rmdir` が `not_empty` を返したら Directory を残し（Log は型だけ）、次の登録は `exists` で止まる（今の「死んだ試みの Directory は消さない」と同じ扱い。持ち主が消す）。`git clone` は失敗すると自分が書いた中身を消す（既にあった空の Directory は残す）ので、普通の失敗では空になり消える。

### 4. Backend が Path を確かめるための権限（要件にする）

上の表の確認は Backend の Process に残す。そのため `git: ssh` の配備では、**Home と `<home>/workspaces` を `paw` だけが辿れる（`x`）こと** を要件にする。推奨は ACL（`setfacl -m u:paw:x <home>` と `setfacl -m u:paw:x <home>/workspaces`。Mode は `0700` / `0750` のまま、Other の `x` は付けない）で、手順書は [PR #288](https://github.com/TomokiAkiyama06/personal-ai-workspace/pull/288)。`o+x`（`0711` など）は同じ Host の全 User に辿らせるので推奨しない（動きはする）。Project の Directory は Wrapper が Root と同じだけ辿れるように作る（上の 2）。

`paw` が Home に持つのは `x` だけで `r` はない。`git: ssh` の配備で Backend が Path に対して行うのは上の表の `realpath`・`lstat`（`x` で足りる）と、`locate_directory_identity` の `scandir`（読めない Directory は飛ばす）だけで、Home を `O_RDONLY` で開く `paths.create_checkout_directory` は呼ばない（`LocalCheckoutDirectories` だけが呼ぶ）。`SubprocessGitRunner` の配備でこの ACL の構成を使うと、`create_checkout_directory` の Home の `open` が `home_unsafe` になる（`not_found` ではない）が、その配備では Backend が Account 自身なので当たらない。Checkout 自身は `0700` のままでよい（`paw` は Checkout の `lstat` だけをする。中を読むのは既存の登録の `check_existing_repository` だけで、既存の Repository は持ち主が権限を決める）。

### 5. 採らなかった案

- **B: `git clone` / `git init` に最後の Directory を作らせる**（Backend は `mkdir` しない。Wrapper の `init` を「cwd は親、作る Directory はまだない」に変える）。git の `safe_create_leading_directories` は途中の Symbolic Link を辿り、Mode は umask に依り（`0700` なら `paw` が辿れない、`0755` なら他の User に見える）、`clone` は既にある空の Directory を再利用する（0017 の「既にある物は再利用しない」に反する）。`init` の前提（`init_existing`）の安全性の議論もやり直しになる。Wrapper のコードは小さくなるが、保証が弱い。
- **C: `paw` に書かせる**（ACL、`CAP_DAC_OVERRIDE`、`chown` のための `CAP_CHOWN`、`sudo -u`）: Directory の持ち主が `paw` になるか、Backend に権限を足す。Decision 0017・0029 が退けた権限の昇格と同じ。
- **D: Backend の Path の確認もすべて Wrapper に移す**（`paw` は Home を辿らない。ACL も `o+x` も要らない）: `inspect_checkout_root`（Scope を作るたび）・`read_checkout_identity`・`check_existing_repository`・`locate_directory_identity` と Tool Broker の Path の解決を、すべて SSH の往復にする大きな変更で、呼び出しごとに `ssh` を 1 つ起動する（Decision 0029 の 1）。将来、`paw` の `x` も外したくなったときの別の Decision とする。
- **後始末に `rm -r` を足す**: 乗っ取られた Backend が、その User の Root の中の任意の Checkout を消せるようになる（今の Wrapper の副コマンドに、User の File を消すものはない）。空の Directory の `rmdir` だけにした。

### 6. 移行

1. 全ての Account の Wrapper を先に入れ替える（`install` で置き換えるだけ。新しい Wrapper は今の `paw-git-run/v1`・`paw-sandbox/v1|v2` に同じく答える）。
2. 次に Backend を入れ替える。古い Wrapper のままだと、`paw-dir/v1` は `bad_protocol`（126）で、Managed Checkout は `git_failed` で失敗する（今の `path_rejected` と同じく使えないだけで、何も書かれない。Fail closed）。
3. Home と `<home>/workspaces` の `paw` の `x`（ACL）と `<home>/workspaces` の用意は、[PR #288](https://github.com/TomokiAkiyama06/personal-ai-workspace/pull/288)の手順のまま。
4. 確認（Human）: `git: ssh` の配備で、Web から **GitHub から Clone** と **新規（ローカル）** が登録でき、`<home>/workspaces/<project>/` が Account の持ち物で、`getfacl` に `user:paw:--x` があり Default ACL がなく Other に何もないこと（Mode `0710`）、Checkout が Account の持ち物の `0700` で Named User の Entry がないこと。`journalctl -t paw-git-wrapper` に `accepted user=<user> directory=mkdir` があり Path を含まないこと。

## Human の判断点（推奨つき）

1. Managed Checkout の Directory を、`git: ssh` の配備ではその User として Wrapper の `paw-dir/v1`（`mkdir` / `rmdir`）で作るか（案 A。B・C・D は 5）。推奨: **A**。0017 の 1 と同じ保証（Root から 1 段ずつ `O_NOFOLLOW`、途中は User の持ち物、最後は新しく作る）を、その User の権限で守れる。
2. Backend の Path の確認（4 の表）は Backend の Process に残し、Home と `<home>/workspaces` を `paw` が辿れること（推奨は ACL の `u:paw:x`。`o+x` ではない）を `git: ssh` の配備の要件にするか（D にしない）。推奨: **要件にする（ACL で）**。Scope を作るたびの再確認を SSH にすると重く、変更が大きい。D は `paw` の `x` も外したくなったときの別の Decision。
3. Wrapper が作る Directory の権限: Project の Directory は Root と同じだけ辿れる（Root の Access ACL を `x` だけに削って写す。ACL がなければ Root の Group / Other の `x` だけ）、継いだ Default ACL は外す、Checkout は ACL なしの `0700`。推奨: **そのとおり**。固定の `0711` は同じ Host の全 User に辿らせ、`0700` 固定では `paw` が Checkout を確かめられない。Wrapper が `paw` の名前を設定で持つ案（`--backend-user=`）は、Root の ACL と二重の正本になるので採らない。
4. Root（`<home>/workspaces`）は Wrapper が作らず、配備（Admin）が先に作るか。推奨: **作らない**。Root は Wrapper の検査の基準で、ないときに `root_unavailable` で全部を断る今の振る舞いを変えない。
5. 失敗した登録の後始末は、空の Directory の `rmdir` だけにするか（中身が残れば Directory を残し、次の登録は `exists` で止まる）。推奨: **`rmdir` だけ**。Wrapper に User の File を消す力を足さない。
6. 既存のディレクトリの Root の食い違い（Backend の `PAW_REPOSITORY_EXISTING_ROOTS` の既定 `{home}` と Wrapper の Root `<home>/workspaces`）。推奨: **この Decision では既定を変えず**、`git: ssh` の配備では `PAW_REPOSITORY_EXISTING_ROOTS={home}/workspaces` を設定するよう手順書に書く（別の小さな PR）。Backend の既定を配備の種類で変えると、`subprocess` の配備の今の登録を壊す。

## リスク

- ACL の構成では、辿れるのは `paw` だけ（他の User は Home にも入れない）。`paw`（乗っ取られた Backend を含む）は名前を知っている Path を `lstat` できるが、一覧は見えず、Checkout の中は `0700` で見えない。`o+x` の構成（推奨しない）では同じことが同じ Host の全 User にできる。
- ACL のない File System（`EOPNOTSUPP`）では、Root の Mode の `x` だけが写る。Root に Access ACL があるのに Project の Directory に付けられないときは、Wrapper は 126 で断る（作った空の Project の Directory は残り、持ち主が消す）。
- Wrapper の `mkdir` は、その User として Root の中に Directory を作る新しい力。形は「Root のちょうど 2 段下・`.` で始まらない名前・既にある物を再利用しない」に限り、File は作らない。乗っ取られた Backend ができることは、空の Directory を作ることと、空の Directory を消すことだけ増える。
- 検査と作成の間の差し替え（TOCTOU）は、`O_NOFOLLOW` と `*at` の呼び出しで閉じる（Path を文字列で辿り直さない）。差し替えられるのは、その User 自身と root だけ（Wrapper の「既知の限界」と同じ）。
- 古い Wrapper と新しい Backend の組み合わせは、Managed Checkout が `git_failed` で失敗する（6 の 2）。
- `mkdir` の答えが届かなかったとき（`ssh` の Timeout、接続の切断）は、Wrapper が作った空の Directory が残り得る（Backend は作ったかを知らないので消さない）。次の登録は `exists` で止まり、持ち主が `rmdir` する。今の「死んだ試みの Directory」と同じ扱い。

## 実装（この PR に含めるもの）

- `apps/backend/deploy/ssh-git-wrapper/paw_git_wrapper.py`: `paw-dir/v1` の `mkdir` / `rmdir`（`directory_plan`・`make_directory`・`remove_directory`）。
- `apps/backend/paw_backend/repositories/directories.py`: `CheckoutDirectories`・`LocalCheckoutDirectories`。
- `apps/backend/paw_backend/repositories/ssh.py`: `SshGitRunner.run_directory`・`SshCheckoutDirectories`。
- `apps/backend/paw_backend/repositories/service.py`: `directories` の継ぎ目と `from_policy` の選択。
- Test: `tests/test_ssh_git_wrapper_directories.py`（Wrapper 単体と、本物の Wrapper を偽の `sshd` の後ろに置いた往復）、`tests/test_repositories_service_ssh_checkout.py`（PostgreSQL。Backend の Process の `mkdir` を使えなくして、Wrapper を通して Clone・新規が登録できること、失敗したら空の Directory が消えること）。
- 手順: `apps/backend/deploy/ssh-git-wrapper/README.md`。
