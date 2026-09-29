# SSH の Forced Command の Wrapper（`paw-git-wrapper`）の配備

更新日: 2026-09-28

Issue [#134](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/134)。
[Decision 0029](../../../../docs/decisions/0029-per-user-git-runner-ssh.md)（Approved）の SSH の Wrapper と、
Decision 0036 の 13（PR #130 で Approved。Human の条件つき）で足した git の副コマンドを実装したもの。

- 実装: [`paw_git_wrapper.py`](paw_git_wrapper.py)（標準ライブラリだけの Python 1 File。Backend の仮想環境に依存しない）
- Test: [`tests/test_ssh_git_wrapper.py`](../../tests/test_ssh_git_wrapper.py)（現在の User と一時 Directory だけを使う。別の Linux User・`sshd`・SSH 鍵を使わない）
- Client 側: `paw_backend.repositories.ssh.SshGitRunner`（Wire Format は Decision 0029 の 2）

## 何をするか

```text
Backend の Process（Service 用 Linux User、例: paw）
  └─ ssh -i /etc/paw/ssh-keys/alice.key alice@127.0.0.1 '<paw-git-run/v1 ...>'
       └─ sshd（alice として）── authorized_keys の command= で固定
            └─ /usr/local/lib/paw/paw-git-wrapper（$SSH_ORIGINAL_COMMAND を検査）
                 └─ git（固定の -c・固定の環境。受け付けた形だけ）
```

Wrapper は Client（Backend）を信用しない。`$SSH_ORIGINAL_COMMAND` を POSIX の語の規則（`shlex.split`。`eval` しない）で分解し、次の全部を満たすときだけ git を `exec` する。1 つでも外れれば終了コード **126** で拒否する（fail closed。git を起動しない）。

1. 先頭の語が `paw-git-run/v1`、4 語目が `--`。
2. cwd は、その User の Root（既定 `<home>/workspaces`）の中の、既存の Directory。**Symbolic Link をすべて解決した後の Path** で判定する。
3. `GIT_CEILING_DIRECTORIES` は `-` か、cwd の親 Directory そのもの。Wrapper は Client の値に関係なく、**Root の親 Directory（Symbolic Link を解決した先）を常に Ceiling に足す**。Root の中の Repository でない Directory から、Home などの Root の外の Repository（Dotfiles の Checkout など。その設定を Wrapper は検査していない）を見つけさせない。
4. `-c` は**固定の一覧にある `key=value` の組そのもの**だけ（Human の条件）。
   - 常に受け付ける: `core.hooksPath=/dev/null`、`core.fsmonitor=false`、`submodule.recurse=false`、`protocol.allow=never`、`protocol.https.allow=always`（`--allow-protocol=` で足した Transport も同じ形）、`--config=` で配備が足した組。
   - `merge` の前だけ受け付ける: `user.name=Personal AI Workspace`、`user.email=integration@paw.invalid`、`commit.gpgSign=false`、`merge.verifySignatures=false`。
   - 受け付けた `-c` も git には渡さない。git には常に **Wrapper 自身の** Hardening（と `merge` なら Wrapper 自身の固定の作者）を付ける。
   - さらに Wrapper だけが付ける設定（Client は送らない）: `diff.ignoreSubmodules=all`（Agent が worktree に Commit した Gitlink と、その中に置いた入れ子の Repository の設定 ── Filter Driver は Command ── を、`status` が子の git で読んで実行しないため）、`maintenance.auto=false`（`merge` が呼び出しの後に切り離した `git maintenance` を残さないため）。そのため `status` は Submodule の中の変更を報告しない。
5. `--git-dir=` / `--work-tree=`（Decision 0036 の 13。Human の条件）は、両方そろっているときだけ、worktree の中で動く副コマンド（`status`・`merge`・`symbolic-ref`・`rev-parse`）にだけ受け付ける。
   - `--work-tree=`: `<Root>/.paw-worktrees` の中で、cwd と同じ Directory。
   - `--git-dir=`: Root の中、かつ `.paw-worktrees` の外にある `.../worktrees/<name>` の Directory（Checkout の `.git/worktrees/<name>`）。
     さらに、その中の `commondir` File（git が Object・Ref・設定を読む先）が、Symbolic Link を解決した結果、その Directory 自身が置かれた `.git` を指すこと（通常 File で、Link でないこと）。別の Repository を指せば `bad_git_dir` で拒否する。
     さらに、その中の `gitdir` File（git が記録した、その worktree の Directory が属する Work Tree の `.git`）が、Symbolic Link を解決した結果、`--work-tree=` の `.git` を指すこと（通常 File で、Link でないこと。相対 Path はその Directory から解決する）。別の Repository（または別の worktree）の `worktrees/<name>` をこの Work Tree と組み合わせれば `bad_git_dir` で拒否する（その Index・`HEAD` をこの Work Tree の File に当てさせない）。
   - どちらも正規化した Path（`..`・`//`・末尾の `/` を含まない絶対 Path）で、Symbolic Link を解決した先が外に出れば拒否する。
6. `.paw-worktrees` の中を cwd にするとき、`--git-dir=` / `--work-tree=` なしで動かせるのは `rev-parse` だけ（Agent が書き換えられる worktree の `.git` が、Filter Driver などの Command を持ち込むのを防ぐ）。
7. 副コマンドは許可リスト（`SUBCOMMANDS`）の中で、引数の並びも決まった形と完全に一致するときだけ。
8. `--git-dir=` / `--work-tree=` なしの呼び出し（`clone` を除く。`init` を含む）は、git を `exec` する前に、同じ cwd・固定の環境で `git rev-parse --path-format=absolute --git-dir --git-common-dir` を実行し、git が cwd から見つける git directory と common directory（`.git` の Directory・`gitdir:` File・Symbolic Link・`commondir` を git 自身が辿った先）が、Symbolic Link を解決した結果 Root の中、かつ `.paw-worktrees` の外にあることを確かめる。外なら `git_dir_outside_root` で拒否する（cwd の検査だけでは、Root の中の `.git` が Root の外の Repository を指せば、git はそこを読み書きする）。cwd が Repository でなければ git 自身に答えさせる（`rev-parse --is-bare-repository` の 128 を Backend が読む）。`init` は作る Directory を cwd にしたときだけ、かつその Directory に `.git` がまだないときだけ受け付ける（`init_existing`。既存の `.git` の再初期化は、git が Repository として読めない壊れた `.git` でも、そこに置かれた Link の先へ書き込むため）。
   さらに、見つけた git directory と common directory（pin した呼び出しでは `--git-dir=` の Directory とその common directory）の中に Symbolic Link か、ほかにも Hard Link を持つ通常 File が 1 つでもあれば `git_dir_link` で拒否する（`hooks/` の中は除く。git は書かず、Hook は動かない。Hard Link は `objects/` と Submodule の `modules/**/objects/` の中も除く。git は既存の Object File を書き換えない）。Directory 自体が Root の中でも、`refs/heads/paw` などを Root の外への Link にすれば git はその先に File を作り、`MERGE_MSG` を Root の外の File への Hard Link にすれば git はその中身を書き換えるため。`objects/info/alternates`・`objects/info/http-alternates`（Submodule のものを含む）があれば `git_dir_alternates` で拒否する（Root の外 ── 他の User の Repository を含む ── の Object を git に読ませ、その Commit を `worktree add` で取り出させないため）。
9. File の中身を読み書きする副コマンド（`status`・`merge`（`--abort` を含む）・`merge-tree`・`worktree add`）は、git を `exec` する前に、その呼び出しが読む設定を `git config --no-includes --list -z`（同じ `--git-dir=` / `--work-tree=`・cwd・固定の環境。これ自体は Command を起動しない）で一覧にし、**Command や別の Work Tree を名指しする設定が 1 つでもあれば拒否する**（`config_unsafe`。一覧にできなければ `config_unreadable`）。
   - 理由: Repository の設定（`.git/config`。`extensions.worktreeConfig` なら worktree ごとの `config.worktree` も）は Checkout とすべての worktree で共有され、worktree で作業する Agent が書ける。そこに置いた `filter.<x>.clean` を、Agent が Commit した `.gitattributes` が選べば、許可した `status` がその Command をこの User として実行してしまう。
   - `core.worktree` も拒否する: Checkout で `--git-dir=` / `--work-tree=` なしに動く `status`・`merge` が、検査した cwd の代わりにその Path（Root の外でも）の File を読み書きしてしまうため。
   - 拒否する設定: `core.worktree`・`protocol.*`（Repository の `protocol.<name>.allow` は Wrapper の `protocol.allow=never` より優先され、`ext::` は Command を動かす）・`extensions.partialClone`・`remote.<name>.promisor`（欠けた Object の遅延取得）・`gpg.*`（`gpg.ssh.defaultKeyCommand` など、署名の設定は Command を名指しする）・`branch.<name>.mergeOptions`（`merge` に `-S` などを足せる。Command Line の `commit.gpgSign=false` は明示の `-S` を打ち消さない）・`filter.*`・`include.*`・`includeIf.*`（Wrapper が一覧にしていない別の File を読ませるため）・`hook.*`・`pager.*`・`core.pager`・`core.editor`・`core.askPass`・`core.sshCommand`・`core.gitProxy`・`core.alternateRefsCommand`・`sequence.editor`・`diff.external`・`gpg.program`・`uploadPack.packObjectsHook`、および `<section>.<name>.<key>` の `<key>` が `textconv`・`command`・`driver`・`program`・`cmd`・`uploadpack`・`receivepack` のもの（`diff.<x>.textconv`・`merge.<x>.driver`・`gpg.ssh.program` など）。
   - 拒否しない設定: `core.hooksPath`・`core.fsmonitor`（Wrapper の `-c` が上書きする）、`credential.helper`（`clone -c` が Repository に残す。これらの副コマンドは Credential を使わない）、その他の通常の設定。
10. Decision 0051（PR #130）の `submodule status --cached`（integration worktree にある Submodule を調べる。`status` の前に呼ばれる）と `status` の形（integration worktree が Commit そのものか ── 無視された File と Submodule の中の変更を含めて ── を確かめる）は、pin したときだけ受け付ける。`--ignore-submodules=none` は Wrapper の `diff.ignoreSubmodules=all` を上書きし、git は中身のある Submodule ごとに子の git をその中で動かす（その Repository の設定 ── Filter Driver は Command ── を Wrapper は検査していない）。`submodule status --cached` も中身のある Submodule の中で `git describe` を動かす。そのため、どちらも `exec` の前に同じ pin で `git ls-files --stage -z`（Index を読むだけ）を実行し、Index の Gitlink（Mode `160000`）のどれかの Path に `.git` があれば `populated_submodule` で拒否する。

8〜10 の確認で動かす git（`rev-parse`・`config --list`・`ls-files`）にも、本体の呼び出しと同じ Wrapper 自身の `-c`（`core.fsmonitor=false`・`core.hooksPath=/dev/null`・`credential.helper=` など）と固定の環境を付ける（Repository の `core.fsmonitor` は Command で、確認そのものが実行してしまわないため）。`config --list` は `--show-scope` で Scope を付けて読み、Wrapper 自身の `-c`（Scope `command`）は検査しない。確認の git の出力は読みながら数え、64 MiB（`PROBE_OUTPUT_LIMIT`）を超えるか 30 秒を超えれば止めて拒否する（`probe_failed` / `config_unreadable`。Repository に置いた巨大な設定で Wrapper の Memory を使い切らせない）。

| 副コマンド | 受け付ける形（これ以外は拒否） |
| --- | --- |
| `rev-parse` | `--is-bare-repository`／`--show-toplevel --absolute-git-dir`／`--show-toplevel`／`--path-format=absolute --git-dir`／`--path-format=absolute --git-common-dir`／`--verify --quiet <rev>^{commit}`（`<rev>` は `HEAD`・`MERGE_HEAD`・`refs/heads/<branch>`・`refs/remotes/origin/<branch>`） |
| `symbolic-ref` | `--quiet --short HEAD`／`--quiet --short refs/remotes/origin/HEAD`／`--quiet HEAD` |
| `config` | `--local --get remote.origin.url` だけ（書き込み・`--global` は拒否） |
| `clone` | `--quiet [-c credential.helper=!<--gh> auth git-credential] [--branch <b>] -- <https URL> <Root の中・.paw-worktrees の外の Path>` |
| `init` | `--quiet --template= --initial-branch=<b> -- <Root の中・.paw-worktrees の外の Path>` |
| `remote` | `add -- origin <https URL>` だけ |
| `worktree` | `add --quiet -b paw/<...> -- <.paw-worktrees の中の Path> <commit id>`／`add --quiet -- <.paw-worktrees の中の Path> paw/<...>`／`list --porcelain -z`／`prune` |
| `merge` | `--no-ff --no-edit --quiet -m "Integrate paw/<b>" refs/heads/paw/<b>`／`--abort` |
| `merge-tree` | `--write-tree --name-only -z --no-messages refs/heads/paw/<a> refs/heads/paw/<b>` |
| `merge-base` | `--is-ancestor refs/heads/paw/<a> refs/heads/paw/<b>` |
| `submodule` | `status --cached` だけ（Decision 0051（PR #130）による。pin したときだけ。下の 10。`foreach`・`update`・`init` など他の副コマンド・Option はすべて拒否） |
| `status` | `--porcelain=v1 -z --untracked-files=all`／`--porcelain=v1 -z --untracked-files=normal --ignored=traditional --ignore-submodules=none`（Decision 0051（PR #130）による。`--git-dir=` / `--work-tree=` で pin したときだけ。下の 10） |

`push`・`fetch`・`pull`・`checkout`・`switch`・`reset`・`rebase`・`commit`・`branch`・`gc`・`submodule` など、表にないものはすべて拒否する。

git の環境は固定の許可リストだけ（`PATH`・`HOME`・`LC_ALL=C`・`GIT_CONFIG_GLOBAL=/dev/null`・`GIT_CONFIG_NOSYSTEM=1` 等。`git.py` の `git_environment` と同じ）。加えて `GIT_NO_LAZY_FETCH=1`（Partial Clone が `status`・`worktree add` の中で欠けた Object を Promisor Remote から取りに行かない）と、`clone` 以外では `-c credential.helper=`（Repository の設定の Credential Helper ── Command ── を空にする。Remote と通信するのは `clone` だけ）を付ける。`sshd` の環境（Client が `SendEnv` で送れる `LC_*` などを含む）は git に渡らない。

### 終了コード

- 受け付けた呼び出し: git 自身の終了コード（Wrapper は git を `exec` する）。
- 拒否: **126**。git の 0／1（`merge-base --is-ancestor`・`merge-tree` はこれを答えとして読む）、128／129（git 自身の失敗）、255（`ssh` 自身の失敗。`SshGitRunner` は `ssh_unavailable` として扱う。Decision 0029 の 5）のどれとも重ならない。Backend はこの値（`paw_backend.repositories.ssh.WRAPPER_REJECTED_CODE`）で拒否を git 自身の失敗と見分ける（`submodule status --cached` の拒否は、integration worktree が汚れている `dirty` ではなく git の失敗 `git_failed`。Decision 0051 の 5）。変えるときは両方を変える。

### Log（Secret を出さない）

呼び出しごとに `syslog`（Facility `LOG_AUTH`、Ident `paw-git-wrapper`）に 1 行だけ書く。

```text
paw-git-wrapper[1234]: accepted user=alice subcommand=status
paw-git-wrapper[1235]: rejected user=alice reason=config_not_allowed
```

書くのは、受け付けたか・固定の理由コード・許可リストにある副コマンド名・Linux User 名だけ。**引数・Path・URL・`-c` の値・git の出力は書かない**（URL や設定値は Credential を含み得る）。stderr にも `paw-git-wrapper: rejected (<理由コード>)` しか出さない（`SshGitRunner` は stderr を読まずに捨てる）。

理由コード: `no_command`・`too_long`・`bad_encoding`・`bad_protocol`・`root_unavailable`・`bad_worktrees`・`bad_path`・`path_unresolvable`・`path_outside_root`・`path_outside_worktrees`・`path_in_worktrees`・`bad_ceiling`・`bad_option`・`no_subcommand`・`subcommand_not_allowed`・`config_not_allowed`・`bad_git_dir`・`bad_work_tree`・`unpinned_worktree`・`bad_arguments`・`populated_submodule`・`git_dir_outside_root`・`git_dir_link`・`git_dir_alternates`・`init_existing`・`probe_failed`・`config_unsafe`・`config_unreadable`・`misconfigured`。

## 既知の限界

- **検査と実行の間の差し替え（TOCTOU）。** Wrapper は Path を解決して検査し、解決済みの cwd・`--git-dir=`・`--work-tree=` を git に渡すが、検査の後・git が Path を開く前に、Path の途中の Directory を Symbolic Link に差し替えられる余地は残る。差し替えられるのは、その Directory に書ける者（その Linux User 自身と root。Agent がその User として worktree に書く場合はその Agent も）だけで、その場合も git はその Linux User の権限でしか動かない（他の User の Home に書く権限は Unix の権限が拒否する）。
- **検査と実行の間の書き換え。** 8・9 の検査の後・git が設定を読む前に Repository の設定を書き換えられる余地は残る。書き換えられるのは、その設定 File に書ける者（その Linux User として動く者。worktree で作業する Agent を含む）だけ。この検査は前もって置かれた設定を防ぐもので、検査と同時に書き換え続ける者までは防がない。
- **`<Root>/.paw-worktrees` 自体を Symbolic Link にした配置は使えない**（`bad_worktrees` で全部拒否）。worktree の置き場所を別の Disk に置きたい場合は、Root ごと（`--root=`）移す。
- **Signal で終わった git。** git が Signal で終わると、`sshd` は終了コードでなく Signal を返し、`ssh` は 255 で終わる（`SshGitRunner` からは `ssh_unavailable` に見える）。Decision 0029 の 5 の「既知の限界」と同じ。
- **Decision 0036 の worktree の経路は PR #130 のマージ後に使われる。** Wrapper は PR #130 のコードに依存しない（単独で完結する）。`tests/test_ssh_git_wrapper.py` の `test_the_worktree_git_of_pr_130_runs_through_the_wrapper` は、`paw_backend.integration` が無い間は Skip し、PR #130 のマージ後は自動で動く。

## 前提

- OpenSSH の `sshd`（`authorized_keys` の `restrict` を使うため 7.2 以降）。
- git 2.45.1 以降（`merge-tree --write-tree` は 2.38 から。Decision 0036 の 14）。Wrapper が付ける `GIT_NO_LAZY_FETCH=1` は 2024-05 の Security Release（2.45.1、および 2.39.4・2.40.2・2.41.1・2.42.2・2.43.4・2.44.1 の各 Maintenance 版）で入ったもので、それより前の git は黙って無視する（Distribution の Backport の有無は配備の前に確かめる）。Wrapper は Partial Clone の設定（`extensions.partialClone`・`remote.<name>.promisor`）と Repository の `protocol.*` も拒否するので、古い git でも遅延取得・`ext::` は起きないが、版の要件は下げない。
- `/usr/bin/python3` が 3.10 以降（Wrapper は標準ライブラリだけを使う）。
- 対象の Linux User の Login Shell が `/bin/bash` などの普通の Shell であること。`sshd` は Forced Command を **その User の Login Shell の `-c`** で起動するため、`/usr/sbin/nologin` だと Wrapper が動かない（`ssh` は 1 などで終わり、`SshGitRunner` からは git の失敗に見える）。
- 対象の Linux User の Home と `workspaces` は、他の User が書けない権限（例: `0750` か `0700`）。Wrapper の Symlink の検査は「その User 自身か root しか Path を差し替えられない」ことを前提にしている。

## 配備（どの Linux User に・どの Path に置くか）

以下、Backend の Process の Linux User を `paw`、対象の Workspace User を `alice` とする。

### 1. Wrapper 本体（Server に 1 つ）

```sh
sudo install -d -o root -g root -m 0755 /usr/local/lib/paw
sudo install -o root -g root -m 0755 \
  apps/backend/deploy/ssh-git-wrapper/paw_git_wrapper.py \
  /usr/local/lib/paw/paw-git-wrapper
```

- 所有者は **root**、モードは `0755`（`paw` にも `alice` にも書けない）。Shebang は `#!/usr/bin/python3 -I`（`PYTHONPATH` などの環境変数と User の site-packages を読まない）。
- 更新も同じ `install` で置き換える。設定 File は無い（設定は下の `command=` の Option だけ）。

### 2. 鍵（Backend 側、User ごとに 1 つ）

```sh
sudo install -d -o paw -g paw -m 0700 /etc/paw/ssh-keys
sudo -u paw ssh-keygen -t ed25519 -N '' -C 'paw-backend-git:alice' \
  -f /etc/paw/ssh-keys/alice.key
```

- 秘密鍵 `/etc/paw/ssh-keys/alice.key` は `paw` の所有・`0600`（`TemplateSshKeyDirectory` が接続の前に毎回確かめる条件）。公開鍵 `alice.key.pub` は次の手順で使う。

### 3. Host Key の固定（Backend 側、Server に 1 つ）

Trust On First Use にしない（Decision 0029 の 1）。`ssh-keyscan` ではなく、Server 自身の Host Key の公開鍵 File から作る。

```sh
printf '127.0.0.1 %s\n' "$(cut -d' ' -f1,2 /etc/ssh/ssh_host_ed25519_key.pub)" \
  | sudo tee /etc/paw/ssh_known_hosts >/dev/null
sudo chown root:root /etc/paw/ssh_known_hosts
sudo chmod 0644 /etc/paw/ssh_known_hosts
```

### 4. `authorized_keys` の 1 行（`alice` の Home）

`/home/alice/.ssh/authorized_keys`（`alice` の所有、`0600`。`~/.ssh` は `0700`）に、次の 1 行を足す（改行しない。`AAAA...` は `alice.key.pub` の中身）。

```text
restrict,command="/usr/local/lib/paw/paw-git-wrapper",from="127.0.0.1",no-pty,no-agent-forwarding,no-port-forwarding,no-X11-forwarding,no-user-rc ssh-ed25519 AAAA... paw-backend-git:alice
```

- `restrict` はすべての転送・PTY・`~/.ssh/rc` を無効にする。Decision 0029 の 4 に書いた個別の Option も、古い `sshd` での解釈の違いに備えて重ねて書く。
- Decision 0029 の 4 の例にある `no-touch-required` は**付けない**。これは FIDO（`sk-*`）鍵の「触れる確認」を**省く**（緩める）Option で、`ed25519` の鍵には効果がない。緩める向きの Option は付けない方針にした（PR で Human に確認する）。
- Wrapper の Option（`command="..."` の中に空白区切りで足す。どれも省略可）:
  - `--root=/home/alice/<workspace_subdir>`: Backend の `workspace_subdir` が `workspaces` 以外のとき。Backend が送る Path と同じ綴り（Account Database の Home から作った Path）にする。
  - `--gh=/usr/bin/gh`: PAW-028 の `gh` の Credential Helper を `clone` に使うとき。`credential.helper=!/usr/bin/gh auth git-credential` という値そのものだけを受け付ける（Backend の `GitClient(gh_executable=...)` と同じ Path にする）。無ければ Credential Helper はすべて拒否する。
  - `--git=/usr/bin/git`（既定）、`--allow-protocol=https`（既定。足すと `protocol.<名前>.allow=always` を受け付ける）、`--config=key=value`（配備が固定する追加の設定）、`--home=`、`--path=`（git の `PATH`。既定 `/usr/local/bin:/usr/bin:/bin`）。
  - Option の綴りの誤りは、すべての呼び出しを `misconfigured` で拒否する（黙って緩い既定に戻らない）。

### 5. `sshd_config`

- `alice` が SSH で入れること（`AllowUsers` / `AllowGroups` を使っている場合は追加する）。`127.0.0.1` で Listen していること。
- `PermitUserEnvironment no`（既定）のまま。
- 任意の追加の締め付け（推奨）:

  ```text
  Match Address 127.0.0.1 User alice
      AllowTcpForwarding no
      X11Forwarding no
      PermitTTY no
      AllowAgentForwarding no
  ```

- 変更後は `sudo sshd -t && sudo systemctl reload ssh`。

### 6. Backend

- `PAW_REPOSITORY_SSH_HOST=127.0.0.1`・`PAW_REPOSITORY_SSH_PORT=22`・`PAW_REPOSITORY_SSH_KNOWN_HOSTS_PATH=/etc/paw/ssh_known_hosts`（`SshGitRunnerPolicy`）。
- `SshGitRunner` の本番の呼び出し経路への配線は、この Issue の範囲外（Decision 0029 の 6「移行」。下の確認がすべて済み、全 User の鍵がそろってから別の Issue で行う）。

## 失効・回転

Decision 0029 の 4 のとおり（Admin の手動運用）。

- 失効: `alice` の `authorized_keys` からその 1 行を消す（以後 `SshGitRunner` は `ssh_unavailable`）。
- 回転: 新しい鍵を作る → `authorized_keys` に新しい行を足す → `/etc/paw/ssh-keys/alice.key` を置き換える → 古い行を消す。

## Human がサーバーで行う確認（チェックリスト）

Agent は他の Linux User・SSH 鍵を使えないため、次の確認は Human がサーバーで行う。準備として、`paw` の Shell で次の関数を定義する（Backend と同じ Option で接続する）。

```sh
# sudo -u paw -s で paw になってから
paw_ssh() {
  ssh -i /etc/paw/ssh-keys/alice.key -F /dev/null -p 22 \
    -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile=/etc/paw/ssh_known_hosts -o GlobalKnownHostsFile=/dev/null \
    -o RequestTTY=no -o LogLevel=ERROR -- alice@127.0.0.1 "$@"
  echo "exit=$?"
}
# Backend と同じ形式の Remote Command を作る（語ごとに引用される）
paw_cmd() {  # paw_cmd <cwd> <ceiling または -> <git の引数...>
  /opt/paw/venv/bin/python - "$@" <<'EOF'
import shlex, sys
from paw_backend.repositories.git import git_config_arguments
cwd, ceiling, *args = sys.argv[1:]
words = ["paw-git-run/v1", cwd, ceiling, "--", *git_config_arguments(("https",)), *args]
print(" ".join(shlex.quote(w) for w in words))
EOF
}
W=/home/alice/workspaces
```

（`/opt/paw/venv` は Backend の仮想環境の Path に読み替える。確認用の Checkout として `alice` で `$W/check` に `git init` した Repository を 1 つ用意し、最初の Commit を作っておく。）

- [ ] 1. 版: `git --version` が 2.45.1 以上（または上の Maintenance 版以降）、`ssh -V` が OpenSSH 7.2 以上、`/usr/bin/python3 --version` が 3.10 以上。
- [ ] 2. 権限: `stat -c '%U %a' /usr/local/lib/paw/paw-git-wrapper` が `root 755`、`/etc/paw/ssh-keys` が `paw 700`、`/etc/paw/ssh-keys/alice.key` が `paw 600`、`/home/alice/.ssh/authorized_keys` が `alice 600`。
- [ ] 3. `getent passwd alice` の Login Shell が `nologin` / `false` ではない。
- [ ] 4. Shell に入れない: `paw_ssh`（Command なし）→ `paw-git-wrapper: rejected (no_command)`・`exit=126`。`ssh -tt ...`（PTY 要求）でも Shell は出ない。
- [ ] 5. 許可した呼び出しが動く: `paw_ssh "$(paw_cmd $W/check $W rev-parse --is-bare-repository)"` → `false`・`exit=0`。`paw_ssh "$(paw_cmd $W/check $W status --porcelain=v1 -z --untracked-files=all)"` は `.paw-worktrees` の外なので動く（`exit=0`）。
- [ ] 6. 副コマンドの拒否: `push origin HEAD`・`checkout -b x`・`fetch origin`・`reset --hard`・`config --global user.name x` のそれぞれで `paw_ssh "$(paw_cmd $W/check $W <引数>)"` → `exit=126`。`alice` の `git -C $W/check branch` に `x` が無く、HEAD が変わっていない。
- [ ] 7. `-c` の拒否: `paw_ssh "$(paw_cmd $W/check $W -c core.hooksPath=/tmp/x rev-parse --is-bare-repository)"` → `exit=126`（`reason=config_not_allowed`）。`-c user.name=x` を `rev-parse` の前に付けても `exit=126`。
- [ ] 8. 他の User の Home への cwd・`--git-dir`: 別の User `bob` がいれば、`paw_cmd /home/bob/workspaces/x /home/bob/workspaces rev-parse --show-toplevel` → `exit=126`。`paw_cmd $W/check $W --git-dir=/home/bob/workspaces/x/.git/worktrees/y --work-tree=$W/.paw-worktrees/y status --porcelain=v1 -z --untracked-files=all` → `exit=126`。`/etc`・`/tmp` でも同じ。
- [ ] 9. Symlink による脱出: `alice` で `ln -s /tmp $W/escape` を作り、`paw_cmd $W/escape $W rev-parse --is-bare-repository` → `exit=126`（`reason=path_outside_root`）。確認後 `rm $W/escape`。
- [ ] 10. 転送の拒否: `ssh ...（paw_ssh と同じ Option）-N -L 15999:127.0.0.1:22 alice@127.0.0.1` を別の端末で起動し、`nc -z 127.0.0.1 15999` が繋がらない（または `sshd` が `administratively prohibited` を返す）。
- [ ] 11. `from=` の制限: 別のホストから同じ鍵で `ssh alice@<server>` → `Permission denied`。
- [ ] 12. Host Key の固定: `UserKnownHostsFile=/dev/null` に変えて `paw_ssh` と同じ接続 → `Host key verification failed`・`exit=255`（Backend では `ssh_unavailable`）。
- [ ] 13. Log: `sudo journalctl -t paw-git-wrapper --since -10min`（または `/var/log/auth.log`）に `accepted` / `rejected` の行があり、URL・Path・`-c` の値・Token を含まない。
- [ ] 14. worktree の経路（PR #130 のマージ後）: Backend の `GitWorktreeCoordinator` を `SshGitRunner` で 1 Task 動かし、`$W/.paw-worktrees/...` に worktree と `paw/` の branch ができ、統合の Merge Commit の作者が `Personal AI Workspace <integration@paw.invalid>` で、`alice` の Checkout の HEAD と default branch が変わっていない。
- [ ] 15. 失効: `authorized_keys` の行を消す（またはコメントにする）→ `paw_ssh` が `Permission denied`・`exit=255`。行を戻して元に戻ることを確かめる。

1 つでも期待と違えば、`SshGitRunner` を配線せず（`SubprocessGitRunner` のまま）、結果を Issue #134 に書く。
