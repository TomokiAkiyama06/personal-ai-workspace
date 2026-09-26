# User ごとの Linux User で git を実行する SshGitRunner（SSH 経由）

- Status: Proposed
- Date: 2026-09-27
- Scope: Issue [#105](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/105)（PAW-027 / [Decision 0017](0017-repository-registration-policy.md) の追補）。`GitRunner`（`apps/backend/paw_backend/repositories/git.py`）の**もう1つの実装**（`paw_backend.repositories.ssh.SshGitRunner`）と、その周辺（鍵、`SshGitRunnerPolicy`、Wrapper の契約）だけを扱う。PAW-028（`gh auth` の User ごとの実行）が同じ仕組みを必要とすることは Decision 0017 の 4 で述べたとおりだが、この Decision は git 以外の実行（`gh` コマンドなど）を承認しない。
- Supersedes: なし。Decision 0017 の 4・11 点目・「承認後の扱い」・「リスク」に「別 Issue #105」として残した宿題への回答であり、0017 の本文は書き換えない。
- Approval: 未承認

## 背景

[Decision 0017](0017-repository-registration-policy.md) の 4（承認済み）は、git を **Backend の Process の Linux User として**だけ動かし、Checkout の持ち主の Account と一致しなければ `identity_mismatch` で拒否すると定めた。
Backend を 1 つの Service 用 Linux User で動かす配備では、これにより **User ごとの Checkout（PAW-027 の目的そのもの）が実質的に動かない**（0017 の「リスク」1 点目）。

人間の回答（2026-09-25、Issue #105 で確認済み）:

> ユーザーごとに割り振られたSSHで、そのUser自身のLinux Userとしてgitを実行する方針でよい（このServerはすでにLinux Userを分けている）。

Issue #105 はこの方針の実装を、次の制約つきで求めている。

- `GitRunner` の実装を 1 つ追加する: Backend → `ssh <linux user>@localhost`（User ごとの専用鍵）→ git。既存の `LoginNameAccountDirectory`（`login_name` = Linux User）の対応をそのまま使う。
- Backend が**すべての User の Shell を持つ形にしない**。鍵は `command=` で決まった Wrapper（git の許可した副コマンドと、その User の workspace 配下だけ）に固定し、`from="127.0.0.1"`・`no-pty`・`no-agent-forwarding`・`no-port-forwarding`（Issue 本文にはないが同じ理由で）を付ける。

この Decision は、その実装が置いた選択を一覧にし、Human が承認または変更できるようにする（[AGENTS.md](../../AGENTS.md) の「仕様変更」）。**この PR はコード側の `GitRunner` 抽象化・呼び出し側の差し替え・エラー処理だけを実装し、本番の SSH 鍵配布・Linux Account の作成・Wrapper Script の実配置は行わない**（Issue #105 の指示どおり、範囲外）。したがって `SshGitRunner` は今回、本番の呼び出し経路には配線されない（Decision 0017 の docstring が言うとおり、`RepositoryService` はまだ HTTP Endpoint を持たない）。配線は、Wrapper が実際に配備された後の別の Issue で行う。

## 提案

### 1. 接続の形

- `SshGitRunner.run` は、既存の `SubprocessGitRunner.run` と同じ `GitRunner` Protocol を実装する。呼び出し側（`GitClient`、ひいては `RepositoryService.from_policy` の `runner` 引数）は、どちらの実装を渡されたかを知らない。**移行は `runner=SubprocessGitRunner(...)` を `runner=SshGitRunner(...)` に差し替えるだけ**で、`RepositoryPolicy`・`AccountDirectory`・DB の行は何も変えない。
- 1 回の呼び出しごとに、新しい `ssh` Process を 1 つ起動する（`ControlMaster` は使わない。複雑さより単純さを優先する暫定案。将来、性能上の理由で持続接続に変える場合は新しい Decision）。
- 接続先は既定で `127.0.0.1`（`ssh <linux user>@127.0.0.1`）。`localhost` という名前ではなく IP literal にしたのは、`localhost` が環境によって `::1` を先に解決し得るため（鍵の `from="127.0.0.1"` 制限と食い違うと、その環境でだけ全呼び出しが失敗する）。Host は `SshGitRunnerPolicy.host` として設定でき、`RepositoryPolicy.clone_hosts` と違って IP literal を許す（後者は攻撃者が指定する Clone 先の SSRF 対策、前者は配備が固定する接続先で、信頼できる値である点が異なる）。
- 鍵は `SshKeyDirectory`（継ぎ目。`TemplateSshKeyDirectory` が既定の実装）が Linux User 名から解決する。既定のひな型は `/etc/paw/ssh-keys/{user}.key`。鍵 File は「この Backend の Process だけが読める」こと（`stat` で確認: 実 Path が Symbolic Link を含まない、通常 File、group/other の権限ビットが 0、所有者が Backend の Process の実効 User）を **接続の前に毎回**確認し、満たさなければ `GitFailure.SSH_KEY_UNAVAILABLE` で拒否する（`ssh` を起動しない。鍵が無い・壊れている状態を、接続を試みてから知るより安全）。
- ローカルの `ssh` Process の環境は `PATH` だけ（`git_environment` と同じ「許可リストだけ、Backend 自身の環境は渡さない」方針を SSH にも適用する）。`-F <ssh_config_path>`（既定 `/dev/null`）で Backend の Process の User 自身の `~/.ssh/config` を無視し、`-o UserKnownHostsFile=<固定 Path>`・`-o GlobalKnownHostsFile=/dev/null`・`-o StrictHostKeyChecking=yes`・`-o BatchMode=yes`・`-o IdentitiesOnly=yes`・`-o RequestTTY=no`・`-o ForwardAgent=no`・`-o ForwardX11=no`・`-o ClearAllForwardings=yes`・`-o PermitLocalCommand=no` を毎回付ける。**Host Key は固定**（`known_hosts_path` に登録された鍵しか受け付けない。Trust On First Use にしない: 初回接続時に何者かが割り込む余地を残さない）。

### 2. 送る内容（Wire Format）と、Wrapper が読むもの

`ssh` は、宛先の後に渡した引数を**自分では引用符を付けずに空白 1 個で連結し**、Forced Command 側には `$SSH_ORIGINAL_COMMAND` という 1 本の文字列として渡す。そこで `SshGitRunner` は、宛先の後に渡す引数を**最初から 1 個の文字列**として組み立てる（`build_remote_command`。`ssh` 自身の連結に依存しない）。各語は `shlex.quote` で個別に引用してから空白で連結するため、Wrapper 側は POSIX の Word 分割規則で `eval` を使わずに `read -a` 等で分解すれば、**元の語の並びをそのまま**復元できる（空白・改行・引用符を含む語も壊れない。`tests/test_repositories_ssh.py` の `BuildRemoteCommandTest` が敵対的な文字列で確認する）。

語の並びは次のとおり（先頭から）。

1. `paw-git-run/v1`（Protocol Tag）。知らない Tag を見た Wrapper は、推測せず拒否する。
2. cwd（絶対 Path。設定しない場合は `.`）。
3. `GIT_CEILING_DIRECTORIES` の値、または `-`（未設定）。
4. `--`（固定の区切り。5 語目からが本来の引数列であっても、境界を Parser に推測させない）。
5. 以降: `git_config_arguments` の `-c key=value` の列（**参考情報。Wrapper はこれを信用せず、自分の固定の Hardening を独立に適用してよい・すべきである**）、続けて git の副コマンドとその引数。

この形式そのものは安全境界ではない。安全境界は、鍵の制限（`command=`。Client 側は Server が何を実行するかを選べない）と、Wrapper 自身の再検証（副コマンドの許可リスト、cwd の確認。Client の申告を信用しない）である。この PR が保証するのは「Wrapper が正しく実装されていれば曖昧さなく解釈できる、1 つの決まったエンコード」だけである。

### 3. Wrapper が許可する git の副コマンド（提案。実装はしない）

`GitClient`（`apps/backend/paw_backend/repositories/git.py`）が実際に発行する副コマンドは、次の 6 つに限られる（Decision 0017 の実装がそうなっている。新しい操作を足すときは、この一覧と Wrapper の両方を更新する必要がある）。

| 副コマンド | 呼び出し元 | 引数の形 |
| --- | --- | --- |
| `rev-parse` | `inspect`（`--is-bare-repository`、`--show-toplevel --absolute-git-dir`、`--verify --quiet HEAD^{commit}`） | 固定オプションの組み合わせのみ |
| `symbolic-ref` | `inspect`（`--quiet --short HEAD` / `refs/remotes/origin/HEAD`） | 同上 |
| `config` | `inspect`（`--local --get remote.origin.url`。読み取りだけ） | `--local --get` 以外の `config` 呼び出し（`--global`、`--system`、書き込みの `--add` 等）は Wrapper が拒否する |
| `clone` | `clone`（`--quiet [--branch <b>] -- <url> <destination>`） | `destination` は呼び出し元の Linux User 自身の Checkout Root（`<home>/workspaces/...`）配下であること |
| `init` | `init`（`--quiet --template= --initial-branch=<b> -- <destination>`） | 同上。`--template=` 以外の値は拒否（空でない Template Directory を指せない） |
| `remote` | `add_origin`（`add -- origin <url>`。書き込みはこれだけ） | `add` 以外（`remove`、`set-url` 等）は不要なので拒否 |

Wrapper は、5 語目以降の**最初の非 `-c` 語**（`-c key=value` の列を読み飛ばした後の最初の語）が上表のいずれでもなければ拒否する。**新しい副コマンドは、この表と Wrapper のコードを両方変更しないと使えない**（許可リストは足すことはあっても、暗黙には広がらない）。

さらに Wrapper は、`git_config_arguments` が送ってくる `-c` の値を信用せず、**自分自身の固定の Hardening**（`core.hooksPath=/dev/null`、`core.fsmonitor=false`、`protocol.allow=never` に許可した Transport だけ `always`、`submodule.recurse=false`、`GIT_CONFIG_GLOBAL=/dev/null`、`GIT_CONFIG_NOSYSTEM=1` 等、`git.py` の module docstring と同じ規則）を独立に適用してから git を起動する。cwd は、要求された Linux User 自身の `workspaces` 配下（または `register_existing` が確認済みの既存 Repository の Root 配下）であることを確認する。これらの検証は Backend 側の Path 検証（`paths.py`）とは独立した、Wrapper 自身の防衛線である。

**この節は Decision の提案であり、実装ではない。** 実際の Wrapper Script、`authorized_keys` の `command=` 行、Linux Account・鍵ファイルの配備は、この PR の範囲外（Issue #105 の指示どおり）。次の Issue が実装する。

### 4. 鍵の発行・失効・回転

- **発行**: Admin 操作（`ssh-keygen -t ed25519 -f /etc/paw/ssh-keys/<login_name>.key -N ''` 相当）。公開鍵を、対象 Linux User の `~/.ssh/authorized_keys` に **1 行**追加する（`command="<wrapper のパス>",from="127.0.0.1",no-pty,no-agent-forwarding,no-port-forwarding,no-X11-forwarding,no-user-rc,no-touch-required <公開鍵>`。`no-user-rc` は `authorized_keys` 側の Local Command 相当を無効にする追加の締め付け、Issue 本文にはないが同じ理由で推奨する)。秘密鍵は Backend の Process の User だけが読めるモードで `/etc/paw/ssh-keys/` に置く（`TemplateSshKeyDirectory` が起動のたびに確認する条件と同じ: 通常 File、group/other 権限 0、所有者が Backend の実効 User）。
- **失効**: 対象 User の `authorized_keys` からその 1 行を削除する（対象の Linux Account が削除・無効化されたとき、または鍵の漏洩が疑われるとき）。Backend 側は何もしなくてよい（鍵 File が消えれば `SSH_KEY_UNAVAILABLE`、Server 側で `authorized_keys` の行が消えれば `SSH_UNAVAILABLE`。どちらも fail closed）。
- **回転**: 新しい鍵を発行し、`authorized_keys` に新しい行を追加し、Backend 側の鍵 File を新しい鍵に置き換えてから、古い行を消す（順序を守れば無停止。同時に古い鍵で動いている呼び出しがあっても、鍵 File の置き換えは次の呼び出しから効く）。回転の周期は運用文書（Admin 向け）が定める。この Decision は周期の数値を決めない。
- **人間の判断点 1**: 鍵の発行・失効・回転を**誰が・どの経路で**行うか（Admin の手動操作か、`LoginNameAccountDirectory` に連動する自動化か）。推奨: この PR の範囲では手動運用文書とし、自動化は実際に配備した後の別 Issue とする（自動化には「User の作成と鍵の発行を同じ Transaction にする」保証が要り、今は無い）。

### 5. エラー処理

`GitFailure` に 2 つの値を追加する（`errors.py`。既存の値は変えない）。

| 値 | 状況 | Backend からの見え方 |
| --- | --- | --- |
| `ssh_unavailable` | `ssh` 自身が接続・認証を完了できなかった（Host が unreachable、鍵を拒否された、Host Key が一致しない、対象の Linux User が存在しない・無効化されている、`authorized_keys` の行が無い）。`ssh` 自身の慣例で終了コード **255**（0〜254 は Wrapper 経由の git 自身の終了コード） | `SshGitRunner.run` が `ssh` の終了コードを見て、255 のときだけこの値にする。0〜254 はこれまでどおり `GitResult` として返り、`NONZERO_EXIT` の判定は呼び出し元（`GitClient._checked`）に任せる |
| `ssh_key_unavailable` | この Process 側で、対象 User の鍵 File が無い・条件を満たさない | `ssh` を起動する前に `SshKeyDirectory.key_path_of` が `OSError` を返し、`ssh` は 1 度も起動しない |

- **SSH 接続失敗**（Host unreachable、Host Key 不一致、鍵拒否）と、**Linux User が未作成**は、Client 側からは区別できない（どちらも `ssh` の終了コード 255 になる。`ssh` 自身がこれらを区別する詳細な理由は、`BatchMode=yes` のもとでは対話的な診断を伴わず、stderr は Log に残さない方針（`git.py` の module docstring と同じ: 出力を Log に出さない）のため読み取らない）。**この区別が要る場合は、Log とは別の経路（`ssh -v` を使う手動診断、または `sshd` 側の Auth Log）に頼る**。この Decision はその経路を作らない（運用手順に書く）。
- **Timeout**・**出力の超過**・**UTF-8 でない出力**は、`SubprocessGitRunner` と同じ判定（共通化した `run_subprocess`。`git.py`）をそのまま使う。`timeout_s` は SSH の Handshake から Wrapper が git を終えるまでの**呼び出し全体**を覆う。Handshake だけの上限は別に `SshGitRunnerPolicy.connect_timeout_s`（既定 10 秒、`ssh -o ConnectTimeout=`）が持つ。
- **既知の限界**: git 自身が終了コード 255 を使うことは無い(1 = 一般的な失敗、128 = fatal、129 = 使用法。255 を使うとは考えにくい)ため、この判定は実務上安全だが、`ssh` の側の規約であって git や Wrapper Script が守る契約ではない。将来 Wrapper が 255 を返す実装になれば、この判定は誤ってそれを `ssh_unavailable` として扱う。Wrapper の終了コードは 0〜254 に収める運用規則として、次の Issue の実装指示に明記する。

### 6. 移行

- **コード**: `RepositoryService.from_policy(..., runner=SshGitRunner(...))` に差し替えるだけ。`RepositoryPolicy`・`AccountDirectory`・`repositories` / `repository_remotes` / `repository_checkouts` の行は何も変わらない。DB の Migration は不要（この PR にも含めない）。
- **設定**: `PAW_REPOSITORY_SSH_HOST` / `_PORT` / `_CONNECT_TIMEOUT_SECONDS` / `_KNOWN_HOSTS_PATH`（`config.py`）を追加した。`SshGitRunnerPolicy.from_settings` が検証する。鍵のひな型（`TemplateSshKeyDirectory` の Template）は今回、設定に足していない（本番の配備 Path が決まってから、実配備の Issue で追加する: 今どのような Path になるか未定なものを設定の既定値にすると、その既定値が実質の決定になってしまうため)。
- **手順（配備側。次の Issue が実施する）**: (1) 対象の Linux User ごとに鍵を発行し `authorized_keys` に 1 行加える、(2) Wrapper Script を配備し、`sshd_config` の `AllowUsers` に対象 User を追加する、(3) `known_hosts` に固定の Host Key を登録する、(4) 1 User で `clone_from_github` 等を試し、別の Linux User の Home へ書けないこと・許可した副コマンド以外が拒否されることを確認する（Decision 0017 の「検証」の項目）、(5) 全 User が揃ってから Backend の `runner` を差し替える（Blue-Green: 差し替え前は `SubprocessGitRunner` のまま、Backend の Process の User 自身の Checkout だけ動く状態を維持できる）。
- **Backend が停止したときの扱い**（Issue #105 の未決点）: `SshGitRunner` は状態を持たない（`ssh` は呼び出しごとに起動し、接続を保持しない）。Backend の再起動は、進行中の Clone を『途中で終わった予約』として扱う既存の仕組み（Decision 0017 の 12・`pending` の Timeout）がそのまま適用される。SSH 特有の後始末は無い。

### 7. Windows / 他 OS

- 対象外（Issue #105 の未決点への回答）。`SshGitRunner` は POSIX の Linux Account・OpenSSH の `authorized_keys` の `command=` を前提にしており、Windows の Server では成立しない。将来の Windows 対応は、別の `GitRunner` 実装（別の Decision）とする。

## 採らなかった案

- `sudo -u` で User を切り替える: Decision 0017 の「採らなかった案」がすでに拒否している（Backend への権限昇格）。人間の回答も SSH 経由に決めている。
- `ssh` の持続接続（`ControlMaster`/`ControlPersist`）: 実装が複雑になる割に、この規模（1 Checkout 操作あたり高々数秒）では性能上の理由が乏しい。将来必要になれば新しい Decision。
- `$SSH_ORIGINAL_COMMAND` を Wrapper が `eval` で解釈する: 引用の実装を誤ると Shell Injection になる。この Decision は POSIX の Word 分割（`shlex.split` 相当。`eval` を伴わない）を明示的に要求する。
- Wrapper が Client の送る `-c` 設定をそのまま使う: Client 側のコードにバグがあっても Wrapper が独立に守る、という多層防御を失う。5 語目以降の `-c` は参考情報とし、Wrapper 自身が固定の Hardening を適用する前提にした。
- Host Key を Trust On First Use（初回接続時に学習）にする: 初回接続に割り込まれると鍵を差し替えられる。固定の `known_hosts_path` を配備時に用意する前提にした。
- SSH 接続失敗と Linux User 未作成を区別する専用の終了コードを Wrapper に持たせる: Wrapper 自体が動いていない・鍵が拒否された状況では、Wrapper のどんな取り決めも届かない（そもそも実行されない）。区別が要る運用は Log 側（`sshd` の Auth Log）に委ねる。

## Human の判断点（推奨つき）

1. 鍵の発行・失効・回転を誰が・どの経路で行うか（4）。推奨: 当面は Admin の手動運用（運用文書）、自動化は配備後の別 Issue。
2. Wrapper が許可する副コマンドの一覧（3 の表）。推奨: 承認。新しい副コマンドを足すときは、この一覧と `GitClient` の変更を同じ PR で行う運用にする。
3. `SshGitRunnerPolicy` の既定値（Host `127.0.0.1`、Port 22、`ConnectTimeout` 10 秒）。推奨: 暫定値として承認。設定で変えられる。
4. SSH 接続失敗と Linux User 未作成を区別しない（5）。推奨: 承認。区別が要る場合は `sshd` の Auth Log に委ねる（この Decision の範囲に含めない）。
5. この PR は `SshGitRunner` を本番の呼び出し経路に配線しない（実配備は別 Issue）。推奨: 承認。Wrapper Script・鍵配布・`sshd_config` の変更が伴う配線は、実際に検証できる Issue で行う。
6. Windows / 他 OS は対象外（7）。推奨: 承認。

## リスク

- **Wrapper が実装されるまで、この PR は何も配備上の効果を持たない**（`SubprocessGitRunner` は今までどおり Backend の Process の User でだけ動く）。`SshGitRunner` はコードとしては完成しているが、実際に per-user Clone が動くようになるのは、Wrapper・鍵・`sshd_config` が揃った後（次の Issue）である。
- Wrapper の実装を誤ると（cwd の検証漏れ、副コマンドの許可漏れ、`-c` の値を信用してしまう等）、`command=` の制限があっても Linux User の Home 内で任意の git 操作ができてしまう。この Decision は契約を定めるが、契約どおりに実装されているかはこの PR では検証できない（Wrapper が無いため）。次の Issue で、Wrapper 自身の Test（敵対的な cwd・副コマンドでの拒否確認）が要る。
- SSH の Host Key を固定運用にすると、Server 側で Host Key を再生成したとき（OS の再インストール等)、`known_hosts_path` を更新するまで全呼び出しが `ssh_unavailable` になる（fail closed。安全側だが、運用手順に明記が要る）。
- 終了コード 255 による判定は、`ssh` 自身の慣例に依存する（5 の「既知の限界」）。

## 実装（この PR に含めるもの）

- `paw_backend/repositories/ssh.py`: `SshKeyDirectory`（継ぎ目）、`TemplateSshKeyDirectory`、`SshGitRunnerPolicy`、`build_remote_command`、`SshGitRunner`（`GitRunner` Protocol の実装）。
- `paw_backend/repositories/git.py`: `SubprocessGitRunner` と `SshGitRunner` が共有する Process 実行（Timeout・出力上限・Process Group ごとの Kill）を `run_subprocess` として切り出した（挙動は変えていない。既存の `tests/test_repositories_git.py` がそのまま通ることで確認する）。
- `paw_backend/repositories/errors.py`: `GitFailure.SSH_UNAVAILABLE` / `SSH_KEY_UNAVAILABLE` を追加。
- `paw_backend/config.py`: `PAW_REPOSITORY_SSH_*` の設定。
- `tests/test_repositories_ssh.py`: 実 SSH・実 Linux User に依存しない Test（`ssh_executable` を Fake の実行 File に差し替える。既存の `tests/test_repositories_git.py` の「罠を仕掛けて確かめる」流儀に合わせた）。
