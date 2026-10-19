# Secret Store（共有の Codex / Claude の Connection の Credential）の配備

更新日: 2026-10-20

[Decision 0087](../../../../docs/decisions/0087-cli-secret-store-and-egress.md)（Approved）の 1・2。
実装は [`paw_backend/connections/secret_store.py`](../../paw_backend/connections/secret_store.py)（`FileSecretStore`）と
[`paw_backend/cli/credentials.py`](../../paw_backend/cli/credentials.py)（`connection-credential put`）。

## 何を置くか

| もの | 場所 | 所有者・Mode |
| --- | --- | --- |
| Store の Directory | `PAW_SECRET_STORE_DIR`（例 `/var/lib/paw/secrets`） | Backend の User（`paw`）、0700 |
| 主鍵（32 Byte）を暗号化した systemd の Credential | `/etc/paw/credentials/paw-secret-key.cred` | root、0600 |
| 平文を入れる Wrapper | `/usr/local/sbin/paw-connection-credential-put`（この Directory の同名の File） | root、0755 |

Store の Directory は Release の Directory（`/opt/paw`）・Recovery Repository・Memory の Projection・Web の配布物・Restore Point の Directory（`/var/lib/paw/restore-points` と `/etc/paw/release.toml` の `restore_point_dir`）の中にも上にも置けない（起動時に拒否する）。
Backup にも Recovery にも入れない。Restore の後と、主鍵を失くしたときは、Credential を入れ直す（要件の「Credential 類のみ再登録」）。

## 初めの準備（root）

```sh
install -d -o paw -g paw -m 0700 /var/lib/paw/secrets
install -d -o root -g root -m 0700 /etc/paw/credentials
# Host の鍵と（あれば）TPM2 に結び付けて暗号化する。平文の鍵はどこにも残さない。
head -c 32 /dev/urandom | systemd-creds encrypt --name=paw-secret-key - /etc/paw/credentials/paw-secret-key.cred
chmod 0600 /etc/paw/credentials/paw-secret-key.cred
install -m 0755 paw-connection-credential-put /usr/local/sbin/
```

`/etc/paw/backend.env` に `PAW_SECRET_STORE_DIR=/var/lib/paw/secrets` を足し、
`paw-backend.service` の `LoadCredentialEncrypted=` の行（Unit の File に注釈で置いてある）を有効にして、`systemctl daemon-reload` の後に Backend を再起動する。
`PAW_SECRET_STORE_DIR` があるのに鍵を渡されていない・Directory の Mode が違うときは、Backend は起動しない（設定の誤りをそのまま動かさない）。

## Credential を入れる

```sh
sudo paw-connection-credential-put claude < token.txt   # claude setup-token の出力（1 年の Token）
```

Wrapper は `systemd-run` で一時の oneshot の Unit（Backend の User、同じ暗号化した鍵、`backend.env`）を作り、標準入力をそのまま渡す。
表示されるのは新しい Handle（`cred_` と 32 桁の 16 進）だけ。平文は引数・環境変数・履歴・Log に出ない。
Owner / Admin はその Handle を Connection に登録する（`ConnectionService.connect` / `replace_credential`。Handle の Lock の中で、Store がその種類の Credential として持っていることを確かめてから DB に書く。この操作の HTTP / 管理 Command の口はまだない）。

### Codex の ChatGPT の Login（Issue #239）

Codex の Credential は ChatGPT の Login の `auth.json`（JSON の Object）。同じ Host の使い捨ての `CODEX_HOME` で Device Code の Login をして作る（Codex の版は CLI の Image と同じもの。`podman run --rm <codex_image> codex login --help` で Flag を確かめる。`rust-v0.162.1` の Source とこの Host の `codex-cli 0.156.1` はどちらも `--device-auth`）:

```sh
umask 077
export CODEX_HOME="$(mktemp -d)"
printf 'cli_auth_credentials_store = "file"\nforced_login_method = "chatgpt"\n' > "$CODEX_HOME/config.toml"
codex login --device-auth                                     # 表示された URL と Code を Browser で入力する
codex login status                                            # "Logged in using ChatGPT"
sudo paw-connection-credential-put codex < "$CODEX_HOME/auth.json"   # 新しい Handle が表示される
shred -u "$CODEX_HOME/auth.json"; rm -rf "$CODEX_HOME"; unset CODEX_HOME
```

Owner / Admin はその Handle を Codex の Connection に登録（初めて）か置き換える。使い捨ての `CODEX_HOME` の Login は二度と使わない（使えば Refresh Token が回り、Store の版が失効する。だから入れたらすぐ消す）。
Session の間に Codex が Login を Refresh したら、Backend は Session の終わりにそれを同じ Handle の次の版として書き戻す（`rotate_credential`、Compare-and-swap、`connection.rotate` の Audit）。古い版は 1 日たつと Reaper が消す。書き戻せなかった（Process ごと落ちた）ときは、次の Health Check が `expired` を見つけたら、上の手順で入れ直す。
置き換え・切断で参照されなくなった Credential は、その場で（Lock の中で参照を確かめてから）消える。登録されなかった Handle と、置き換えから 1 日たった古い版は、Backend の Reaper（`connection_reaper.py`）の周期で消える。

## 鍵の入れ替え・Host の入れ替え

主鍵を入れ替える仕組みはまだない（入れ替えるときは、新しい鍵で Credential を入れ直し、古い Handle の File は Reaper に消させる）。
Host の入れ替えや TPM の Clear で鍵が読めなくなったときも同じ。
