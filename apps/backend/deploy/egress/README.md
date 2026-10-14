# Egress の Proxy（`paw-egress`）の配備

更新日: 2026-10-14

[Decision 0087](../../../../docs/decisions/0087-cli-secret-store-and-egress.md)（Approved）の 3。
実装は [`paw_egress.py`](paw_egress.py)（標準ライブラリだけの Python 1 File。Backend の仮想環境に依存しない）、
Test は [`tests/test_egress_proxy.py`](../../tests/test_egress_proxy.py)（一時 Directory の Unix Socket と Loopback だけ。DNS の答えと「Provider」への接続は Test の差し込み口）。

## 何をするか

```text
CLI の Container（--network none、User paw-agent-cli）
  └─ CLI（HTTPS_PROXY=http://127.0.0.1:<Port>）
       └─ paw_egress.py forward --session <uuid>（127.0.0.1:<Port> → Mount した /run/paw-egress/egress.sock。CONNECT に Session の Header を付ける）
Host
  └─ paw-egress.socket（/run/paw-egress/egress.sock、Group paw-cli-bridge、0660。systemd が作って Service に渡す）
  └─ paw-egress.service（User・Group paw-egress。Process は paw-cli-bridge に入らない）
       └─ CONNECT <許可した Host 名>:443 だけ → 自分で名前を引き、Global な Address だけに接続 → TLS をそのまま中継
  └─ nftables（paw-egress.nft）: paw-egress の外への通信は DNS と Global な Address の TCP 443 だけ
```

- 受けるのは `CONNECT host:443` だけ（Host 名は `egress.toml` の `allowed_hosts` と完全一致。Wildcard・別の Port・平文の HTTP の Request は拒否）。
- 名前は Proxy が引き、答えに 1 つでも Global でない Address（Private・Loopback・Link-local・CGNAT・Multicast・予約）があれば拒否する。
- TLS は開かない（中身を読まない・変えない・Credential を足さない）。
- 1 つの接続ごとに 1 行の Log: Session・Host・結果・Byte 数。中身・他の Header は書かない。Session は Container の中の転送が `--session <UUID>`（Backend が Container の Command に入れる）から `X-PAW-Session` に入れる。CLI が送った同じ名前の Header は転送が落とす。ただし Container の中の Process は Mount された Socket に直接つなぐこともできるので、Log の Session は目安（Session ごとの Socket で結び付けるのは後続）。宛先の制限（Allowlist・Global な Address・443）はどの経路でも同じに効く。
- 上限: CONNECT の Request は 8 KiB・10 秒、同時 64 接続、両方向とも 15 分 Idle なら閉じる。

## 配備（root）

`paw-egress.service` の先頭の注釈の手順で、Account（`paw-egress`）と Group（`paw-cli-bridge`。メンバーは `paw-agent-cli` だけ）を作り、
`paw_egress.py` と `egress.toml` を置いて Socket と Service を有効にする。
Socket は systemd が Group `paw-cli-bridge`・0660 で作って Proxy に渡す（Socket Activation）。Proxy の Process は `paw-cli-bridge` に入れない（その Group は Session の Directory の `credential.env` を読めるため。[Decision 0088](../../../../docs/decisions/0088-cli-token-file-mode.md)）。nftables は `paw-egress.nft` を既存の規則と一緒に読み込む（Resolver の Address は `/etc/resolv.conf` に合わせる）。この例は IPv6 の 2001::/23 を丸ごと落とす（その中のわずかな Global な範囲も。どれも Provider のものではなく、第 2 の守りは狭い側に倒す）。

`allowed_hosts` の初めの値は Claude Code の Subscription（`setup-token`）の `api.anthropic.com` だけ。
CLI の Smoke で拒否の Log（`outcome=host`）を見て、CLI が要る Host だけを足す（Decision 0087 の 4）。
