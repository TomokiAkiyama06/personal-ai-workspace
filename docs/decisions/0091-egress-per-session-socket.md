# Egress の Proxy への接続を Session に結び付ける方法（Session ごとの Socket を systemd の Template の Socket で作り、Session ごとに Proxy を 1 つ動かす）

- Status: Proposed
- Date: 2026-10-16
- Scope: Issue [#239](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/239) の残りの「Egress: Session ごとの Socket で Session を結び付ける（設計が要る）」。Issue [#208](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/208) の CLI の段を組み立てで有効にする前の前提。この Decision は設計だけで、コードは承認の後の PR で書く。関係する既存の口: `apps/backend/deploy/egress/`（`paw_egress.py`・`paw-egress.socket`・`paw-egress.service`・`paw-egress.nft`。#233・#240）、`apps/backend/deploy/cli-session/`（`paw-cli-session@.service`・`paw_cli_session_run.py`・`start-claude`・`start-codex`。#236・#244）、`apps/backend/paw_backend/agents/cli_session.py`（`create_session`・`SystemdLauncher`）。
- 具体化: [Decision 0087](0087-cli-secret-store-and-egress.md)（Approved）の 3 の「1 つの接続ごとに Session の ID（Socket の前に置く小さな Header か、Session ごとの Socket）…を Log に残す」の 2 つの候補のうち、**Session ごとの Socket** を選ぶ。
- Supersedes（一部）: 0087 の 3 の「受けるのは Unix Socket だけ（`/run/paw-egress/egress.sock`、Group は `paw-cli-bridge`、Mode 0660）」のうち **共有の 1 つの Socket** の部分を、下の 1 の Session ごとの Socket（Group・Mode は同じ）に置き換える。0087 の 3 の他（`CONNECT` だけ、Host 名の完全一致の Allowlist と 443、Global な Address だけ、TLS を開かない、nftables の `meta skuid paw-egress` の宛先の規則、Proxy の Process を `paw-cli-bridge` に入れないこと（0088））と 4 の表は変えない。nftables には Host 全体の同時接続の上限の規則だけを足す（下の 3）。

## 背景

### 今の形（#233・#236・#240 で Merge 済み）

```text
CLI の Container（--network none、User paw-agent-cli、--userns keep-id）
  ├─ /run/paw-egress.sock ← Host の /run/paw-egress/egress.sock（全 Session で共有の 1 つ）を Mount
  ├─ paw_egress.py forward --session "$PAW_SESSION_ID"（127.0.0.1:3128 → 上の Socket。CONNECT に X-PAW-Session を付ける）
  └─ claude / codex（HTTPS_PROXY=http://127.0.0.1:3128）
Host
  └─ paw-egress.socket → paw-egress.service（User paw-egress、1 つの Process が全 Session の接続を受ける。同時 64 接続）
```

Session の ID は、Container の中の転送（`forward`）が環境変数 `PAW_SESSION_ID`（`paw_cli_session_run.py` が `--env` で入れる）から `X-PAW-Session` の Header に入れ、Proxy はそれを Log に書く。

### なぜ共有の Socket では Session を結び付けられないか

Codex の Review（#233 の `paw_egress.py` の「Bind session identity at the Unix-socket boundary」）の指摘のとおり:

- 共有の Socket は **Container の中に Mount されている**。Container の中の Process（CLI 本体・その依存・MCP の Relay）は、転送を通らずに `/run/paw-egress.sock` へ直接つなぎ、`X-PAW-Session` を **省く・別の Session の UUID を書く** ことができる。`PAW_SESSION_ID` も同じ Container の環境変数なので CLI から読める。Header は Container の側が決める値で、Proxy から見て信頼できない。
- Socket の相手の資格情報（`SO_PEERCRED`）でも区別できない: すべての Session の Container は同じ Host の Account `paw-agent-cli` で動く（`--userns keep-id` は「今の User の UID:GID を Container の中で同じ値に Map する」: Podman の公式の `podman run` の説明）。UID は全 Session で同じ。
- Proxy は 1 つの Process で、同時接続の上限（64）も Byte 数も全 Session の合計でしか持てない。

0087 の 3 は「宛先の制限（Allowlist・Global な Address・443）はどの経路でも同じに効く」ので、**宛先の守りは今のままでも崩れない**。崩れるのは次の 3 つ:

1. **記録（Audit）の信頼性**: Egress の Log の `session=` が偽れる・空にできる。どの Session（どの Task・User・Connection）がどこへ何 Byte 送ったかを、事故の調べで確かに言えない（[AGENTS.md](../../AGENTS.md) の 13 の Audit の項目、0087 の 3 の「1 つの接続ごとに Session の ID」）。侵された Session が別の Session（別の User の Task）の名で通信を残すこともできる。
2. **Session ごとの制限ができない（可用性）**: 1 つの Session が 64 の接続を埋めれば、他の User の Session の通信が `503`（`busy`）で落ちる。Session ごとの上限を作ろうにも、Session を信頼できる形で数えられない。
3. **Session ごとの量が分からない（計上・検知）**: Session ごとの Byte 数を確かに集められないので、送り出しの量の異常（持ち出しの兆し）を Session に結び付けて見られない。Session が終わった後に残る接続を、その Session のものとして閉じることもできない（今は Container の終わりで閉じる）。

なお、TLS は Proxy を素通りするので、ある Session が共有の Socket を通して **別の Session の通信の中身を読む・変える** ことはできない。影響は記録・可用性・計上で、Credential や中身の漏れではない。

#239 の他の項目は #240・#243・#244 で直り、残りはこの項目と `CODEX_HOME` の書き込み（Codex の Login の PR）だけ。CLI を組み立てで有効にする前に直すと #239 で約束している。

## 選択肢

### (A) systemd の Template の Socket で Session ごとの Socket を作り、Session ごとに Proxy を 1 つ動かす（推奨）

```text
paw-cli-session@<uuid>.service（Backend が start。polkit が許すのはこの名前だけ。今のまま）
  Requires= / After= paw-egress-session@<uuid>.socket
paw-egress-session@<uuid>.socket（root の systemd が作る）
  ListenStream=/run/paw-egress/session/<uuid>.sock、SocketUser=paw-egress、SocketGroup=paw-cli-bridge、SocketMode=0660、RemoveOnStop=yes
  BindsTo=paw-cli-session@<uuid>.service（Session の Unit が止まる・終わると Socket も止まる）
paw-egress-session@<uuid>.service（最初の接続で起動。User paw-egress、今の paw-egress.service と同じ硬さ）
  paw_egress.py serve --config /etc/paw/egress.toml --session <uuid>
  BindsTo= / After= paw-egress-session@<uuid>.socket（Socket と一緒に止まる。その Session の接続はすべて閉じる）
Container
  /run/paw-egress.sock ← Host の /run/paw-egress/session/<uuid>.sock（自分の Session の Socket だけ）
```

- **Session は「どの Socket で受けたか」で決まる**。Socket は Session の Container にだけ Mount され、Container は他の Session の Socket に届かない（Mount していない。`--network none` なので Abstract の Unix Socket も Host のものに届かない）。Header は使わない（来ても無視する）。Proxy の Instance は自分の Session の UUID を Command の引数（`%i`）で知り、Log の `session=` はそれを書く。journald の記録にも Unit 名（`paw-egress-session@<uuid>.service`）が付く。
- **systemd で確かめたこと**（systemd 259 の `systemd.socket(5)`・`systemd.service(5)`・`systemd.unit(5)`、Host の版で確認）:
  - Template の Socket の `ListenStream=` に `%i` を使える（systemd 自身の `systemd-journald@.socket` が `ListenStream=/run/systemd/journal.%i/stdout` の形で使っている）。`Accept=no` の Socket は、同じ名前の Service（Template なら同じ Instance）を起動し、Listen している Socket を渡す。
  - Socket の File Descriptor は **Service が起動するときに** 渡される（`Sockets=` の説明）。動いている 1 つの Proxy に、後から Session ごとの新しい Socket を渡すことはできない。だから Session ごとの Socket を systemd に作らせるなら、Session ごとに Proxy の Process が 1 つになる。
  - `BindsTo=` は、相手が明示的に止められたときだけでなく、**相手が自分で終わった・失敗したときも** 自分を止める（`Requires=` より強い）。Session の Unit（`Restart=no`）の Program が終わると、Socket とその Proxy も止まる。
- Socket と Proxy の Unit は、Session の Unit の `Requires=` で systemd が同じ Job の中で起動する。Backend の polkit の規則（`50-paw-cli-session.rules`: `paw-cli-session@<uuid>.service` の start / stop だけ）は変えない見込み（polkit が見るのは Backend が頼んだ Unit で、依存で引き込まれた Unit ではない）。**配備の確認で確かめる**（下の「リスク」）。違えば、規則に `paw-egress-session@<uuid>.socket` の start / stop を足す（同じ UUID の形だけ）。
- 置き場所は `/run/paw-egress/session/`（親は `DirectoryMode=0755` で systemd が作る）。Session の Directory（`/run/paw-cli/<uuid>`、Backend の持ち物）には置かない（Backend が消す File と systemd が消す File を混ぜない）。
- 費用: CLI の Session 1 つにつき Python の小さな Process が 1 つ（標準ライブラリだけ。数十 MB の見込み）。**CLI の Session の数は Host 全体では今は何にも抑えられていない**（Decision 0084 の容量の計算は Cloud の Agent を除き、`max_parallel_nodes` は 1 つの Run の中の値で、Connection の Lease も Claude の Subscription の Session を全体で抑えない）。そのため Session ごとの上限とは別に、Host 全体の上限を下の 3 で持つ。

### (B) 共有の Proxy のまま、Container の外（`paw_cli_session_run.py`）で Header を付けて中継する

- Session の Unit の Program（Host の側、`paw-agent-cli`、`%i` を systemd から得る）が自分の `RuntimeDirectory` に Session ごとの Socket を作って Container に Mount し、受けた接続を共有の `egress.sock` へ `X-PAW-Session` を付けて中継する。共有の `egress.sock` は Container に Mount しない。
- 良い点: Unit が増えない、Proxy は 1 つのまま（全体の上限を 1 か所で持てる）。
- 悪い点: Session は結局 Header で、Proxy が信じる根拠は「`paw-cli-bridge` の Member の Host の Process は Program だけ」という配備の前提になる（Socket の境界ではない）。今は `subprocess.run` だけの Program が、Session の間ずっと動く非同期の中継になる。Session ごとの上限は共有の Proxy の中に Header を鍵として書く。推奨しない（A より守りが弱く、作る量は大差ない）。

### (C) Header に Session ごとの秘密（HMAC）を付ける

- Backend が `egress.token = HMAC(鍵, UUID)` を Session の Directory に書き、転送が Header に付け、Proxy が同じ鍵で確かめる。Container は自分の Session の Token しか読めないので、他の Session の名は偽れない。
- 悪い点: Backend と `paw-egress` が共有する新しい鍵（Secret Store の外の File、Rotation、Backup の扱い）が要る。Header の Parse と照合が Proxy の攻撃面に残る。Session ごとの上限は共有の Proxy の中に作る。推奨しない。

### (D) `SO_PEERCRED` の PID から cgroup を引いて Session を決める

- 相手の PID の `/proc/<pid>/cgroup` から `paw-cli-session@<uuid>.service` を読む。
- 悪い点: Rootless の Podman の Container の Process がどの cgroup に入るかは、Podman の cgroup の管理（既定は systemd で、User の systemd に Scope を作らせる。Rootless の cgroupfs では Container の cgroup を使わない: Podman の公式の `podman` の説明）と版で変わり、この Unit には User の systemd がない（README の `Delegate=yes` の確認の項）。PID の再利用の競合（`SO_PEERPIDFD` が要る）、`paw-egress` が他の Account の `/proc` を読む必要もある。UID は全 Session で同じ（上の背景）なので UID では決められない。Session ごとに別の UID にするのは 0088 の File の形を作り直すことになる。推奨しない。

### (E) 変えない（Log の Session は目安のまま）

- 宛先の守りは今のままでも効く。記録・Session ごとの上限・計上は上の背景の 3 つのとおり信頼できないまま。#239 で「CLI を有効にする前に直す」とした約束と、0087 の 3 の「1 つの接続ごとに Session の ID」を満たさない。推奨しない。

## 提案

### 1. Session ごとの Socket（A）

上の (A) の 3 つの Unit で、Session ごとの Socket と Proxy にする。

- `paw-cli-session@.service` に `Requires=` と `After=` の `paw-egress-session@%i.socket` を足す（今の `After=paw-egress.service` は外す）。
- `paw_cli_session_run.py` は `egress_socket`（共有）の代わりに `egress_session_dir`（既定 `/run/paw-egress/session`）を設定で受け、`<dir>/<uuid>.sock` を Container の `/run/paw-egress.sock` に Mount する。その Socket がなければ起動を拒む（共有の Socket に戻らない）。`PAW_SESSION_ID` の `--env` は外す。
- `start-claude` / `start-codex` の転送は `--session` を渡さない（`forward` は Header を付けない、今の `--session` なしの形）。
- `paw_egress.py serve` に `--session <uuid>` を足す。この形では Socket は systemd から渡されたものだけを使い（自分で作らない）、`X-PAW-Session` は読まず、Log の `session=` は引数の値にする。

### 2. 共有の Socket を本番から外す

- `paw-egress.socket` / `paw-egress.service`（共有）は本番の配備から外す（Repository の File は消し、README の手順を 1 の 3 つの Unit に替える）。共有の Socket が Host に残っていると、誤った設定で Container に Mount されたときに 1 の守りが崩れるため。
- 開発で Socket Activation なしに動かす形（`serve --config` が自分で Socket を作る）は Test のために残す。`X-PAW-Session` の読み取りと `forward --session` は消す（偽れる値を Log に書く経路を残さない）。

### 3. Session ごとの上限と、Session の終わりの集計の 1 行

- Session ごとの同時接続は **16**（暫定。今の全体の 64 を Session ごとの値に替える）。超えた接続は `503`（`busy`）で、Log に残す。CONNECT の Request の 8 KiB・10 秒、両方向 15 分の Idle は変えない。
- Byte 数の上限は置かない（正しい長い Session を落とさないため）。代わりに、Proxy の Instance が止まるとき（Session の終わり）に **集計の 1 行**（`session=`・接続の数・拒否の数（理由ごと）・送った / 受けた Byte の合計）を書く。値・中身は書かない（0087 の 3 のまま）。
- **Host 全体の上限も残す**（Session ごとの上限だけでは、Session の数だけ上限が増える）:
  - 外への接続の数: nftables で、`paw-egress` の Account の新しい TCP 443 の接続を **Host 全体で同時 64**（今の全体の上限と同じ値）に抑え、超えたものは TCP の Reset で拒む（`paw-egress.nft` に、`nft(8)` の例の形の `ct count over` の規則を足す。正確な規則は実装の PR で `nft -c` で確かめる）。Proxy はその失敗を `connect` の拒否として Log に残す。Kernel が数えるので、Session ごとの Proxy が何個あっても全体で効く。
  - Proxy の Process の資源: 各 `paw-egress-session@<uuid>.service` に自分の `MemoryMax=`・`TasksMax=`（値は実装の PR で。大きくなった Proxy は自分だけが OOM で止まり、他の Session の Proxy を巻き込まない）を付け、すべてを 1 つの Slice（`paw-egress.slice`）に入れる。Host 全体の数は Slice の `TasksMax=`（暫定の目安は Proxy 32 個分の Thread の数）で抑え、超えると新しい Session の Proxy が起動できずその Session の通信は拒否になる（外へは出ない）。Slice の `MemoryMax=` は「1 つの上限 × 個数」より大きくし、普段は効かない最後の守りとする（Slice の上限で OOM が起きると Slice の中のどの Process が止まるかは選べないため: Kernel の cgroup v2 の説明）。
  - CLI の Session の数そのものを Host 全体で抑える上限（Backend の側）は、この Decision では決めない（Egress だけの話ではないので、CLI を有効にする前の別の Issue にする）。

### 4. Backend の Log に Session の UUID と Node を残す

- Egress の Log の `session=<uuid>` を Task・Node・Connection・User に結び付けられるよう、Backend は CLI の Session を起動するときに **1 行の Log**（Session の UUID・Task の ID・Node の ID・Connection の ID・CLI の種類。Credential・Prompt は書かない）を書く。今は UUID を `cli_session.py` の中でしか持たず、どこにも書いていない。
- DB の Usage の行や Audit の Table に Session の UUID の列を足すことは、この Decision では決めない（後続の Issue。Egress の集計を DB に入れるときに一緒に決める）。

### 5. CLI ごとの Allowlist はまだ分けない

- Session ごとの Proxy にすれば、Session の CLI の種類（Claude / Codex）ごとに Allowlist を分けられる（Claude の Session は `api.anthropic.com` だけ、など）。ただし今の Allowlist は `api.anthropic.com` だけで、Codex の宛先は ChatGPT の Login の Smoke の後に決まる（0087 の 4）。CLI の種類を Proxy に渡す形（Unit の名前に入れる、など）もその時に決める。この Decision では分けない。

### 6. 進め方（1 つの PR）

承認の後、1 つの PR（Refs #239、Refs #208）で:

- `deploy/egress/`: `paw-egress-session@.socket`・`paw-egress-session@.service`（今の Service と同じ硬さ、`Slice=paw-egress.slice`）・`paw-egress.slice`（3 の上限）、`paw-egress.nft` の Host 全体の同時接続の規則、`paw_egress.py` の `serve --session`・Session ごとの上限・集計の 1 行・Header の読み取りと `forward --session` の削除、共有の Unit の削除、README。
- `deploy/cli-session/`: Unit の依存、`paw_cli_session_run.py` の Mount と `PAW_SESSION_ID` の削除、`start-*` の転送、`cli-session.example.toml`、README の配備と「確かめること」。
- `paw_backend/agents/`: 4 の Log の 1 行。
- Test: `tests/test_egress_proxy.py`（`--session` の形で Log の `session=` が引数の値であること・Header を偽っても変わらないこと・17 本目の接続の `busy`・Unit が Slice に入り nftables の例に全体の上限の規則があること・集計の 1 行・渡された Socket がないと起動しないこと）、`tests/test_cli_session_units.py`（Unit の依存・Mount が Session の Socket だけで共有の Socket を Mount しないこと・Socket がないと拒否すること・`PAW_SESSION_ID` がないこと）。CI の中では本物の systemd は動かさない（Unit の File を読んで形を確かめる今の Test と同じ）。
- 配備の確認（Human、CLI を有効にする前の README の「確かめること」に足す）: Backend の `systemctl start paw-cli-session@<uuid>.service` だけで Socket が作られること（polkit の追加なし）、Session の Unit の終わりで Socket と Proxy が止まり Socket の File が消えること、Container の中から見える Egress の Socket が自分の 1 つだけであること、Log の `session=` と集計の 1 行。

## 決めてほしいこと

1. **Egress の Proxy への接続は、systemd の Template の Socket で作る Session ごとの Socket で Session に結び付け、Session ごとに Proxy を 1 つ動かす（A）。Header（`X-PAW-Session`）は使わない**（1）でよいか。推奨: はい。代わりの案: 共有の Proxy のまま Container の外で Header を付ける（B）、HMAC の Header（C）、`SO_PEERCRED` と cgroup（D）、変えない（E）。
2. **共有の Socket（`paw-egress.socket` / `paw-egress.service`）を本番の配備から外し、Header の読み取りと `forward --session` を消す**（2）でよいか。推奨: はい。
3. **Session ごとの同時接続の上限を 16（暫定）と Proxy ごとの Memory の上限にし、Host 全体の上限（nftables で外への同時接続 64、Proxy の Slice の `TasksMax=`）も残す。Byte 数の上限は置かず、Session の終わりに集計の 1 行を Log に書く**（3）でよいか。推奨: はい。
4. **Backend は CLI の Session を起動するとき、Session の UUID と Task・Node・Connection・CLI の種類を 1 行の Log に書く。DB の列は後続**（4）でよいか。推奨: はい。
5. **CLI の種類ごとの Allowlist は今は分けず、Codex の宛先が決まるときに決める**（5）でよいか。推奨: はい。
6. **6 のとおり 1 つの PR で入れ、systemd と polkit の振る舞いは CLI を有効にする前の配備の確認で確かめる**（6）でよいか。推奨: はい。

## リスク

- **polkit**: 依存で引き込まれる Socket の Unit の起動に、polkit が別の許可を求めないことは配備の確認で確かめる。求められた場合は、規則に `paw-egress-session@<uuid>.socket` の start / stop だけを足す（同じ UUID の形の Pattern。Backend に許す範囲は Session の Unit と同じ Session の Socket だけで、広げない）。
- **Process の数**: Session ごとに Proxy の Process が 1 つ増える。CLI の Session の数は Host 全体では今は抑えられていないので、3 の Slice の上限で抑え、Backend の側の上限は別の Issue にする。Slice の上限に当たると新しい Session の通信が拒否で失敗する（Log で分かる）。
- **起動の順と止め方の誤り**: `BindsTo=` / `Requires=` の組み合わせを誤ると、Socket が Session より先に止まる（CLI の通信が拒否で失敗するだけで、外へは出ない）か、Session の後に残る（使う Container がないので無害だが、File が残る）。配備の確認と、残った Socket の Unit を見つける手順を README に書く。
- **Codex の 2 回の起動**: Codex は同じ Session の Unit を `--version` と `app-server` で 2 回起動する（README）。1 回目の終わりで Socket が止まり、2 回目の起動で `Requires=` により作り直される。集計の 1 行は起動ごとに出る（Log を読む側は Session の UUID で合わせる）。
- **IPv6 / DNS など宛先の判定**: この Decision では変えない（0087 の 3 のまま）。

## 承認後の扱い

承認されたら `Approval` に記録し、Status を Approved に改め、6 の PR を作る。
方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。
[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。

## 出典（2026-10-16 に確認）

- systemd 259（`259.5-0ubuntu3.4`）の `systemd.socket(5)`（Socket と Service の名前の対応、`Accept=`、`Service=`、`RemoveOnStop=`）、`systemd.service(5)`（`Sockets=`: Socket の File Descriptor は Service の起動のときに渡す）、`systemd.unit(5)`（`BindsTo=`・`PartOf=`・`StopWhenUnneeded=`）、`/usr/lib/systemd/system/systemd-journald@.socket`（`ListenStream=` の `%i`）
- Kernel: [Control Group v2](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html#memory-interface-files)（`memory.max` を超えて縮められないと、その cgroup の中で OOM killer が動く）
- Podman: [`podman run`](https://docs.podman.io/en/stable/markdown/podman-run.1.html)（`--userns=keep-id`: 今の User の UID:GID を Container の中で同じ値に Map する）、[`podman`](https://docs.podman.io/en/stable/markdown/podman.1.html)（`--cgroup-manager` の既定は systemd、Rootless の cgroupfs では Container の cgroup を使わない）
- Codex の Review: #233 の `apps/backend/deploy/egress/paw_egress.py` への指摘（「Bind session identity in the forwarder」「Bind session identity at the Unix-socket boundary」）
