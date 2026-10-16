# Egress の Proxy（`paw-egress`）の配備

更新日: 2026-10-16

[Decision 0087](../../../../docs/decisions/0087-cli-secret-store-and-egress.md)（Approved）の 3 と、Session ごとの Socket と Proxy にした [Decision 0091](../../../../docs/decisions/0091-egress-per-session-socket.md)（Approved）。
実装は [`paw_egress.py`](paw_egress.py)（標準ライブラリだけの Python 1 File。Backend の仮想環境に依存しない）、
Test は [`tests/test_egress_proxy.py`](../../tests/test_egress_proxy.py)（一時 Directory の Unix Socket と Loopback だけ。DNS の答えと「Provider」への接続は Test の差し込み口。Unit の File は読んで形を確かめるだけで、本物の systemd は使わない）。

## 何をするか

```text
paw-cli-session@<uuid>.service（Backend が start。polkit が許すのはこの名前だけ）
  Requires= / After= paw-egress-session@<uuid>.socket
paw-egress-session@<uuid>.socket（root の systemd が作る。BindsTo=paw-cli-session@<uuid>.service）
  /run/paw-egress/session/<uuid>.sock（User paw-egress、Group paw-cli-bridge、0660。Session の Unit と一緒に止まり、File も消える）
paw-egress-session@<uuid>.service（最初の接続で起動。User・Group paw-egress、Slice paw-egress.slice）
  paw_egress.py serve --config /etc/paw/egress.toml --session <uuid>
  └─ CONNECT <許可した Host 名>:443 だけ → 自分で名前を引き、Global な Address だけに接続 → TLS をそのまま中継
CLI の Container（--network none、User paw-agent-cli）
  ├─ /run/paw-egress.sock ← 自分の Session の /run/paw-egress/session/<uuid>.sock だけを Mount
  ├─ paw_egress.py forward（127.0.0.1:3128 → 上の Socket。何も足さない）
  └─ CLI（HTTPS_PROXY=http://127.0.0.1:3128）
nftables（paw-egress.nft）: paw-egress の外への通信は DNS と Global な Address の TCP 443 だけ
```

- **Session は「どの Socket で受けたか」で決まる**。Socket は Session の Container にだけ Mount され、Proxy の Instance は Session の UUID を Command の引数（`%i`）で知る。Container が送る Header（`X-PAW-Session` など）は読まない。journald の記録にも Unit 名（`paw-egress-session@<uuid>.service`）が付く。
- 受けるのは `CONNECT host:443` だけ（Host 名は `egress.toml` の `allowed_hosts` と完全一致。Wildcard・別の Port・平文の HTTP の Request は拒否）。
- 名前は Proxy が引き、答えに 1 つでも Global でない Address（Private・Loopback・Link-local・CGNAT・Multicast・予約）があれば拒否する。
- TLS は開かない（中身を読まない・変えない・Credential を足さない）。
- 上限（Decision 0091 の 3、暫定）: CONNECT の Request は 8 KiB・10 秒、両方向とも 15 分 Idle なら閉じる。**Session ごとに同時 16 接続**（超えると `503`、`outcome=busy`）、**新しい接続は Session ごとに 1 秒に 20**（超えた接続は 1 つずつ Log に書かずに閉じ、10 秒に 1 行 `outcome=rate count=<数>` で数える。journald の Unit ごとの Rate limit に当たらないため。限度は Unit の設定で上げも外しもしない）。**Host 全体で生きている接続は同時 64**: 接続ごとに `/run/paw-egress/slots/` の 64 個の File のどれかに `flock` を取り（読み取りだけで開く）、取れなければ `503`（`busy`）。Process が殺されても Kernel が Lock を放す。Byte 数の上限は置かない。
- Proxy の Process の資源: 各 `paw-egress-session@<uuid>.service` は `MemoryMax=128M`・`TasksMax=8`（Main と名前を引く 4 Thread）、すべて `paw-egress.slice`（`TasksMax=256`: Proxy 32 個分、`MemoryMax=5G`: 普段は効かない最後の守り）に入る。Slice の `TasksMax=` に当たると新しい Session の Proxy が起動できず、その Session の通信は拒否になる（外へは出ない）。

## Log（journald）

1 接続ごとの行（`conn=` は Instance の中で一意な番号）:

```text
session=<uuid> conn=3 host=api.anthropic.com outcome=open sent=0 received=0
session=<uuid> conn=3 host=api.anthropic.com outcome=progress sent=5120 received=81234   （開いている間 60 秒ごと）
session=<uuid> conn=3 host=api.anthropic.com outcome=ok sent=812 received=4096          （終わり: ok / error / stopped）
session=<uuid> conn=4 host=statsig.anthropic.com outcome=host sent=0 received=0         （拒否は 1 行: host / port / address / busy など）
session=<uuid> outcome=rate count=7                                                      （1 秒に 20 を超えて閉じた数。10 秒に 1 行）
session=<uuid> outcome=summary connections=12 refused=busy:1,host:2 rate=7 sent=... received=...   （Instance が普通に止まるとき）
```

- `sent` / `received` は **前の行からの差分**（累計ではない）。Session の量は、`session=` が同じ行の `sent` / `received` をそのまま足せば重複なく得られる（**正はこの 1 接続ごとの行**）。Proxy が OOM などで殺されても、開いていた接続は始まりの行と最後の途中の行が残る（数え落としは接続ごとに最後の 60 秒まで。終わりの行がない接続が殺された接続）。
- `outcome=summary` は読みやすさのための目安（殺された Instance は書かず、次の接続で起動した Instance は自分の分だけを書く）。Codex は 1 つの Session の Unit を 2 回起動するので、集計の行も 2 つ出る。
- 中身・Header・Path は書かない。どの Task・Node・Connection の Session かは、Backend の Log の 1 行 `CLI session started: session=<uuid> task=<id> node=<key> connection=<id> cli=<claude|codex>` で結び付ける。
- Journal の保存の量と期間（`journald.conf` の `SystemMaxUse=` など）は、Egress の行を調べる期間より長くする。

## 配備（root）

`paw-egress-session@.service` の先頭の注釈の手順で、Account（`paw-egress`）と Group（`paw-cli-bridge`。メンバーは `paw-agent-cli` だけ）を作り、
`paw_egress.py` と `egress.toml`（`egress.example.toml`）・`tmpfiles.d` の設定（`paw-egress.tmpfiles.conf` を `/etc/tmpfiles.d/paw-egress.conf` に置いて `systemd-tmpfiles --create`）・3 つの Unit（`paw-egress.slice`・`paw-egress-session@.socket`・`paw-egress-session@.service`）を置いて `systemctl daemon-reload` する。
**どれも enable しない**（CLI の Session の Unit が自分の Socket を引き込む）。
Socket は systemd が Group `paw-cli-bridge`・0660 で作って Proxy に渡す（Socket Activation）。Proxy の Process は `paw-cli-bridge` に入れない（その Group は Session の Directory の `credential.env` を読めるため。[Decision 0088](../../../../docs/decisions/0088-cli-token-file-mode.md)）。`serve --session` は渡された Socket だけを使い、なければ起動しない（自分で作らない）。
nftables は `paw-egress.nft` を既存の規則と一緒に読み込む（Resolver の Address は `/etc/resolv.conf` に合わせる）。この例は IPv6 の 2001::/23 を丸ごと落とす（その中のわずかな Global な範囲も。どれも Provider のものではなく、第 2 の守りは狭い側に倒す）。

`allowed_hosts` の初めの値は Claude Code の Subscription（`setup-token`）の `api.anthropic.com` だけ。
CLI の Smoke で拒否の Log（`outcome=host`）を見て、CLI が要る Host だけを足す（Decision 0087 の 4）。

### 共有の Socket からの移行（`paw-egress.socket` / `paw-egress.service` を配備した Host）

Decision 0091 の 2: 共有の Socket は残さない（誤った設定で Container に Mount されると Session ごとの守りが崩れるため）。

```bash
systemctl disable --now paw-egress.socket paw-egress.service
rm /etc/systemd/system/paw-egress.socket /etc/systemd/system/paw-egress.service
systemctl daemon-reload
test ! -e /run/paw-egress/egress.sock && echo "the shared socket is gone"
# /etc/paw/egress.toml: socket = "/run/paw-egress/egress.sock" の行を消し、slots = "/run/paw-egress/slots" を足す
# /etc/paw/cli-session.toml: egress_socket の行を消し、egress_session_dir = "/run/paw-egress/session" を足す
#   （egress_socket が残っていると CLI の Session は起動を拒む）
```

その後で上の「配備」の `tmpfiles.d` と 3 つの Unit を置き、新しい `paw_egress.py`・`paw_cli_session_run.py`・`paw-cli-session@.service` と、CLI の Image（`start-claude` / `start-codex` の転送は `--session` を渡さない。Build し直して Digest を `cli-session.toml` に入れる）を入れ直す。

## 確かめること（CLI を有効にする前。Human）

- Backend の `systemctl start paw-cli-session@<uuid>.service` だけで `/run/paw-egress/session/<uuid>.sock` が作られること（polkit の規則の追加なし。依存で引き込まれる Socket の Unit に polkit が別の許可を求めたら、`50-paw-cli-session.rules` に `paw-egress-session@<uuid>.socket` の start / stop だけを同じ UUID の形で足す: Decision 0091 のリスク）。
- Session の Unit の終わり（CLI が自分で終わったときも、`systemctl stop` のときも）で Socket と Proxy が止まり、Socket の File が消えること。残った Unit は `systemctl list-units 'paw-egress-session@*'` で見つけ、`systemctl stop` する。
- Container の中から見える Egress の Socket が自分の 1 つだけであること（`podman exec` は root で。`ls /run/paw-egress*`）。
- Log の `session=` が Session の UUID であること、Header を偽っても変わらないこと、終わりに `outcome=summary` の 1 行が出ること。
- `systemctl show -p Slice,MemoryMax,TasksMax paw-egress-session@<uuid>.service` が `paw-egress.slice`・128M・8 であること。
