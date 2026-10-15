# Tool の Sandbox（`paw-sandbox/v1`）の配備

更新日: 2026-10-16

Issue [#208](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/208) の段階 2。
[Decision 0083](../../../../docs/decisions/0083-agent-runtime-adapters.md)（Approved）の 2（ファイル・Shell・Git の Tool と git の Shim）と 6（Sandbox と Mask、SSH の Wrapper の拡張）を実装したもの。

- Backend: `paw_backend/sandbox/`（Tool の定義と Executor・Sandbox の計画・Pool・Runner・git の Shim の受け口）
- Server 側: [`../ssh-git-wrapper/paw_git_wrapper.py`](../ssh-git-wrapper/paw_git_wrapper.py) の `paw-sandbox/v1`（Decision 0029 の Forced Command の Wrapper を広げたもの。同じ鍵・同じ `authorized_keys` の行）
- Image に入れるもの: [`paw_git_front.py`](paw_git_front.py)（`/usr/local/bin/git`）、[`paw_git_shim.py`](paw_git_shim.py)（`/usr/local/lib/paw/paw-git-shim`）、Base の Image の本物の git（`/usr/lib/paw/git`。Path にない）、[`paw_file_tool.py`](paw_file_tool.py)（`/usr/local/lib/paw/paw-file-tool`）、[`Containerfile`](Containerfile)
- Test: `tests/test_sandbox_*.py`（`git` の前段は `test_sandbox_git_front.py`）、`tests/test_ssh_git_wrapper_sandbox.py`（実際の Container・Podman・他の Linux User は使わない。下の「Human がサーバーで行う確認」は手動）

**まだ本番の組み立てには配線していません。** これらの Tool を呼ぶ `LocalAgentRuntime` と、その組み立て（`ToolRunner(execution_timeout=300)` と `RepoToolExecutor` の配線、Sandbox の設定）は段階 3 の PR です。

## 何をするか

```text
Backend の Process（Linux User paw）
  ├─ Tool Broker（repo.shell.run など: 1 回ごとに判定・Audit・Budget・Fencing）
  └─ RepoToolExecutor ─ SandboxPool
       └─ ssh -i /etc/paw/ssh-keys/alice.key alice@127.0.0.1 'paw-sandbox/v1 start|exec|stop ...'
            └─ /usr/local/lib/paw/paw-git-wrapper（alice として。$SSH_ORIGINAL_COMMAND を検査）
                 └─ podman（rootless、alice の Storage）
                      └─ Container paw-sbx-<id>（--network none、CPU / Memory / PID の上限、認証情報なし）
                           ├─ /workspace      ← 1 つの Repository（書き込む Node は自分の worktree だけ読み書き）
                           ├─ Mask            ← Scope の除外の Path・Checkout の .git などに空の tmpfs / 空の File
                           └─ /run/paw-shim   ← git の Shim の Socket（読み取り専用）
```

- Sandbox は **Node（の試行）と Repository ごとに 1 つ**（Task・Run・Node の試行の Agent の ID・Repository・書き込みの有無が Key。並列の Node どうしも共有しない）。最初の呼び出しで起動し、同じ Node の次の呼び出しで使い回す（Benchmark と同じく `bash` の間で Container が続く）。Scope が変わって Mount・Mask が変われば止めて起動し直す。
- 止めるのは: Command が時間内に終わらない・取り消された（Decision 0083 の 8。`podman rm --force --time 5`: SIGTERM、5 秒で SIGKILL）、Node の終わり（`SandboxPool.release`）、使われないまま `idle_seconds`（既定 15 分。`reap_idle`）、`close`。Backend が落ちても Container の寿命（`--sandbox-lifetime`、既定 2 時間。`podman run --timeout`）で終わる。`--rm` なので止まれば消える。
- `exec` の Shell の Command は `timeout -k 5 300 bash -lc <command>`（Benchmark と同じ。Command は Remote Command の行ではなく **stdin** で渡す）。File の Tool は Image の中の `paw-file-tool`。出力は stdout / stderr それぞれ 4 MiB まで（超えた分は捨てて「切り詰めた」と返す）。

## Mount と Mask（Decision 0083 の 6）

Backend が Scope から作り（`paw_backend/sandbox/plan.py`）、Wrapper が全部をもう一度確かめる（Client を信用しない）。

| 何 | Backend が送るもの | Wrapper の確認 |
| --- | --- | --- |
| Repository | 呼び出しの Path（`workdir` / `path`）を含む Repository の Root を 1 つ。1 つの Repository の Node は `/workspace`、2 つ以上は `/workspace/<repository id>` | Root（既定 `<home>/workspaces`）の中、Symbolic Link を解決した結果が送られた Path と同じ（途中に Link がない）、Directory。**書き込みは `.paw-worktrees` の中の worktree（`.git` が通常の File）だけ**。利用者の Checkout は読み取り専用でしか Mount しない |
| Mask | Scope の `excluded_paths`（`NodeWorktree.protected` を含む）と除外の Repository・入れ子の他の Repository のうち、Mount した Root の中のもの。読むだけの Node では Checkout の `.git` と、それが指す git directory・common directory（Root の中なら）も | Mount の中、Link でない。Directory には空の tmpfs（`tmpfs-mode=0555`。Container の User は書けない）、File には `/dev/null` を読み取り専用で重ねる。無い・Link・その他の種類なら **起動しない**（`mask_unavailable` / `path_is_link` / `bad_path`。Node は `PermissionError`） |
| 読み取り専用 | 書き込む Node の worktree の `.git` の File | 書き込みの Mount の中の通常の File / Directory を、読み取り専用で重ね直す |
| Shim | `shim: true` | `<--sandbox-shim-base>/<id の hex>` が Link でない Directory。`/run/paw-shim` に読み取り専用 |

Path は `podman --mount` の CSV に入るので、`,`・`"`・制御文字を含む Path は使えない（`unmountable_path` / `bad_path`。起動しない）。

## Sandbox の中の `git`（前段と Shim）

Decision 0090（Approved 2026-10-16）の 1 で、`/usr/local/bin/git` は小さな前段 [`paw_git_front.py`](paw_git_front.py) になった。前段は呼び出しが Node の Repository（`/workspace`）に向いているときだけ Shim に渡し、それ以外（Test が `/tmp` に作る一時の Repository など）は Image の本物の git（`/usr/lib/paw/git`。Path にない）を実行する:

- `init`・`clone` は **作る場所** で決める: 行き先の Directory（省略なら作業の場所。`clone` で省略なら作業の場所の下の Repository の名前。`--bare` / `--mirror` は `<名前>.git`）と `--separate-git-dir` のどれかが `/workspace` か、その下なら Shim（今どおり拒否）、外なら本物の git。どこから呼んでも同じ（`/workspace` からの `git init /tmp/repo` は本物の git、`/tmp` からの `git init /workspace/x` は Shim）。
- それ以外は **作業の場所**（`cwd` に `-C` を順に当てたもの）で決める。`/workspace` か、その下なら Shim、外なら本物の git。
- どちらも、`--git-dir`・`--work-tree` の値、絶対 Path の `-c` / `--config-env` の値、環境変数 `GIT_DIR`・`GIT_WORK_TREE`・`GIT_COMMON_DIR`・`GIT_INDEX_FILE`・`GIT_OBJECT_DIRECTORY`・`GIT_ALTERNATE_OBJECT_DIRECTORIES` が `/workspace` の中を指せば Shim。Path は `realpath` で解決する。`init` / `clone` の Option は git と同じく読む（Long Option の一意な前方一致・まとめた Short Option）。読めない呼び出し（知らない Option、値の欠け、多すぎる Directory）も Shim。
- **前段は境界ではない**（Model は本物の git を直接実行できる）。安全は今と同じく Mount と Mask が守る: Sandbox から Node の Repository の Git Directory と共有の Object Store は見えず（`.git` の File は読み取り専用）、`--network none`。Image の Build は本物の git の写しが Path に残っていれば失敗する。
- 書き込める Worker は本物の git で `/workspace` の中に入れ子の Repository を作れるので、`submit` の突き合わせ（Decision 0083 の 10）は入れ子の Repository（`repo.git.changes` の `nested`。git は `sub/` と一覧する）があれば何も Commit せずに Model に消させる。`repo.git.commit` もその Path を拒否する（Gitlink として Commit しない）。

Shim（[`paw_git_shim.py`](paw_git_shim.py)、`/usr/local/lib/paw/paw-git-shim`）は、引数と作業 Directory を `/run/paw-shim/git.sock` に送り、Backend が Tool Broker の `repo.git.read` として判定してから、Host で git を動かす（`SshGitRunner` で Wrapper を通る。`--git-dir` / `--work-tree` を固定）。

- 許すのは `status`・`diff`・`log`・`show` と、決まった Option・Revision（`HEAD`・`HEAD~<n>`・起点 `refs/paw/base/<branch>`・40 / 64 桁の Object ID）・Scope の中の Path だけ（`paw_backend/sandbox/gitargs.py`）。`commit`・`checkout`・`--no-index`・`<rev>:<path>` などは `git: <理由> (<code>)` で終了コード 128。
- Revision は Node の起点から `HEAD` までの Commit だけ（`merge-base --is-ancestor` で 2 回確かめる）。Tree・Blob・Tag の ID は拒否。`log` は常に起点で止まる。**起点の Ref（`refs/paw/base/<branch>`）は段階 3 の Worktree の準備で作る**ので、それまでは書き込む Node も「今の状態だけ」（`show`・Revision つきの `diff`・内容の `log` は拒否）。
- Pathspec は Backend が作る: 指定された Path は `:(literal)<path>`、無ければ `.`。Mask した Path はすべて `:(exclude,literal)<path>` で常に外す。
- Socket は Sandbox ごと（`<shim base>/<id>/git.sock` と `token`）。Command の実行中だけ答え、Token（256 bit）を毎回確かめる。Sandbox と一緒に消える。

## 配備

前提: Podman 4 以降（`run --timeout`）を Rootless で使えること（各 User の `subuid` / `subgid`、`XDG_RUNTIME_DIR` = `/run/user/<uid>`。`loginctl enable-linger <user>` など）。Wrapper の配備（鍵・`authorized_keys`）は [`../ssh-git-wrapper/README.md`](../ssh-git-wrapper/README.md) のとおりで、Sandbox のために増える鍵はない。

1. Image を作る（Benchmark の Sandbox の Image を元に。[`Containerfile`](Containerfile) の先頭の注釈）。各 Workspace User の Rootless の Storage に入れ（`podman load` など）、Digest を控える。
2. Shim の Socket の置き場所を作る（Backend の User が作り、Workspace User の Group だけが通れる）:

   ```sh
   sudo groupadd --system paw-sandbox
   sudo usermod -aG paw-sandbox paw              # Backend（Socket の Group を変えるため）
   sudo usermod -aG paw-sandbox alice            # Workspace User ごと
   sudo install -d -o paw -g paw-sandbox -m 0711 /run/paw-sandbox   # 一覧は見せない（0711）
   ```

   再起動で消えるので `tmpfiles.d` に書く（`d /run/paw-sandbox 0711 paw paw-sandbox -`）。Backend は Sandbox ごとの Directory を `0750`、Socket を `0660`、Token を `0640`（Group `paw-sandbox`）で作る（`SandboxPool(shim_group=<gid>)`）。
3. `authorized_keys` の `command=` に Sandbox の Option を足す（無ければ `paw-sandbox/v1` はすべて `sandbox_not_configured` で拒否）:

   ```text
   command="/usr/local/lib/paw/paw-git-wrapper --sandbox-image=localhost/paw-sandbox@sha256:<digest> --sandbox-shim-base=/run/paw-sandbox --sandbox-keep-groups=yes"
   ```

   | Option | 既定 | 意味 |
   | --- | --- | --- |
   | `--sandbox-image=<name>@sha256:<64 hex>` | なし（必須） | Digest で固定した Image。Tag は拒否（`misconfigured`） |
   | `--sandbox-shim-base=<dir>` | なし | Shim の Socket の置き場所。無ければ Shim つきの Sandbox は起動しない |
   | `--podman=<path>` | `/usr/bin/podman` | |
   | `--sandbox-cpus=` / `--sandbox-memory=` / `--sandbox-pids=` | `4` / `8g` / `1024` | Benchmark と同じ |
   | `--sandbox-lifetime=<秒>` | `7200` | Container の寿命（Backend が止められなくても終わる） |
   | `--sandbox-command-timeout=<秒>` | `300` | 1 つの Command の上限（Decision 0083 の 8） |
   | `--sandbox-keep-groups=yes` | `no` | `--group-add keep-groups`（Container の中から Group `paw-sandbox` の Socket に届くため。crun が必要） |

## Human がサーバーで行う確認（チェックリスト）

Agent は他の Linux User・Podman を使えないため、次は Human がサーバーで行う（段階 3 の配線の前）。`paw_ssh` は [`../ssh-git-wrapper/README.md`](../ssh-git-wrapper/README.md) の確認と同じ関数。`ID=$(python3 -c 'import uuid; print(uuid.uuid4())')`。

- [ ] 1. `podman --version` が 4 以上。`alice` で `podman run --rm --network none <image@digest> true` が通る。
- [ ] 2. Start: `LocalWrapperSandboxRunner` / `SshSandboxRunner` で 1 つの worktree の Spec を `start` し、`podman ps --filter label=paw.sandbox=v1`（`alice` として）に出る。`podman inspect` で `NetworkMode=none`、`CapDrop` が全部、`--userns=keep-id`・`--user` が `alice` の uid:gid、Mount が Spec のとおり。Container の中の `id -u` が `alice` の uid。
- [ ] 3. Mask: Container の中で、Mask した Directory が空で書けない（`touch` が失敗）、Mask した File が空、worktree の `.git` が読めるが書けない、`/workspace` の外に Host の File が見えない。
- [ ] 4. Network: Container の中で `python3 -c 'import socket; socket.create_connection(("1.1.1.1", 53), 2)'` が失敗する。
- [ ] 5. 書き込みの拒否: Checkout を `writable: true` で `start` すると Wrapper が `path_outside_worktrees` で拒否する（Podman は動かない）。
- [ ] 6. Shim: Container の中の `git status` / `git diff` / `git log --oneline` が動き、`git commit` / `git show <起点より古い Commit>` / `git diff -- <除外の Path>` が 128 で拒否される。`/run/paw-shim` の Socket に Container の User が繋がる（`keep-groups`）。
- [ ] 6b. 本物の git（Decision 0090）: Container の中で `git init -q /tmp/r && git -C /tmp/r -c user.name=a -c user.email=a@b commit -q --allow-empty -m x && git -C /tmp/r log --oneline` が通り、`/workspace` での `git init`、`git init /workspace/x`（`/tmp` から）、`git init --separate-git-dir=/workspace/m /tmp/r2` が Shim で 128 になる。`which -a git` が `/usr/local/bin/git` だけ。
- [ ] 7. 時間と停止: `sleep 400` が 300 秒で 124 になる。`stop` で Container が消える。Backend を止めても `--sandbox-lifetime` で消える。
- [ ] 8. Log: `journalctl -t paw-git-wrapper` に `accepted user=alice sandbox=start` などがあり、Path・Command・出力を含まない。

1 つでも期待と違えば段階 3 で配線せず、結果を #208 に書く。

## 既知の限界

- **検査と起動の間の差し替え（TOCTOU）。** Wrapper が Mask の種類と Link でないことを確かめてから Podman が Mount するまでの間に、その Path を差し替えられる余地は残る。差し替えられるのはその Linux User（とその Agent。ただし Sandbox は Node ごとで、その Node の前の Sandbox は止めてから起動し直す）と root だけ。
- **Container の中は Benchmark と同じく書ける**（Root の File System は読み取り専用にしていない。`pip install` などは Container が消えるまで残る）。Host には `/workspace` の Mount を通してしか書けない。
- **Scope の除外の Path が Checkout の Path で書かれていると、worktree の同じ場所は除外にならない**（`derive_child_scope` は利用者の Checkout を除外にするが、Checkout の中の除外の Path を worktree の Path に写さない。PAW-035 からの挙動。Sandbox は Scope が除外する Path を Mask する）。
- 出力は stdout と stderr を分けて受け取り、`stdout + stderr` の順に返す（Benchmark と同じ。混ざった順ではない）。
