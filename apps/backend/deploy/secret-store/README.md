# Secret Store（共有の Codex / Claude の Connection の Credential）の配備

更新日: 2026-10-22

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
Owner / Admin はその Handle を Connection に結び付ける（下の「Handle を Connection に結び付ける」。`ConnectionService.connect` / `replace_credential`。Handle の Lock の中で、Store がその種類の Credential として持っていることを確かめてから DB に書く）。

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

Owner / Admin はその Handle を Codex の Connection に結び付ける（初めて）か差し替える（下の「Handle を Connection に結び付ける」の手順。種類は `codex`）。使い捨ての `CODEX_HOME` の Login は二度と使わない（使えば Refresh Token が回り、Store の版が失効する。だから入れたらすぐ消す）。
Session の間に Codex が Login を Refresh したら、Backend は Session の終わりにそれを同じ Handle の次の版として書き戻す（`rotate_credential`、Compare-and-swap、`connection.rotate` の Audit）。古い版は 1 日たつと Reaper が消す。書き戻せなかった（Process ごと落ちた）ときは、次の Health Check が `expired` を見つけたら、上の手順で入れ直す。
置き換え・切断で参照されなくなった Credential は、その場で（Lock の中で参照を確かめてから）消える。登録されなかった Handle と、置き換えから 1 日たった古い版は、Backend の Reaper（`connection_reaper.py`）の周期で消える。

## Handle を Connection に結び付ける（Owner / Admin、Issue #277）

入口は Backend の HTTP API（[`/api/v1/admin/connections`](../../README.md#connection-の-credential-の-http-apiissue-277)、`admin.config.manage`）。Web の画面はまだない。
変更（結び付け・差し替え・切断）には、その Session の 30 分以内（認証 Policy の時間）の **Passkey の Step-up** が要る。
Cookie の Session の API なので、Owner / Admin で Web の App に Sign in した Tab の開発者 Tool の Console から、同じ Origin の `fetch` で呼ぶ。Handle は秘密の値ではないが、Log や Chat に貼らない。

```js
// 0. Passkey の Step-up（PublicKeyCredential.parseRequestOptionsFromJSON に対応した Browser）
const {options} = await (await fetch("/api/v1/auth/passkeys/authenticate/begin", {method: "POST"})).json()
const passkey = await navigator.credentials.get({publicKey: PublicKeyCredential.parseRequestOptionsFromJSON(options)})
await fetch("/api/v1/auth/passkeys/authenticate/finish", {
  method: "POST", headers: {"Content-Type": "application/json"},
  body: JSON.stringify({credential: passkey.toJSON()}),
})

// 1. 状態を見る（種類・状態・版・revision。値も Handle も出ない）
await (await fetch("/api/v1/admin/connections")).json()

// 2a. 初めて: Handle を結び付ける（201。状態は unavailable から始まる）
await fetch("/api/v1/admin/connections/claude", {
  method: "POST", headers: {"Content-Type": "application/json"},
  body: JSON.stringify({secret_handle: "cred_..."}),
})

// 2b. 入れ替え: 1 で見た revision を添えて差し替える（他の人が先に変えていたら 409 stale_connection_credential。1 からやり直す）
await fetch("/api/v1/admin/connections/claude/credential", {
  method: "PUT", headers: {"Content-Type": "application/json"},
  body: JSON.stringify({secret_handle: "cred_...", expected_revision: "<revision>"}),
})

// 3. 外す（Connection を消す。参照されなくなった Credential はその場で消える）
await fetch("/api/v1/admin/connections/claude", {method: "DELETE"})
```

答えの読み方:

- `credential.stored` が `true`、`credential.version` が版（Session の中の Refresh の書き戻しで上がる）。`false` は Store から消えた・読めない（入れ直して差し替える）。`null` は Backend に Store が設定されていない。
- `status` は `unavailable`（まだ確かめていない・使えない）、`connected`、`expired`（Provider が Credential を拒んだ。入れ直して差し替える）。結び付けた直後と差し替えた直後は `unavailable` で、Health Check が `connected` にする。
- 422 `credential_not_stored`: その種類の Credential として Store にない Handle（打ち間違い、別の種類の Handle、Reaper が消した未登録の古い Handle）。`put` からやり直す。
- 503 `secret_store_not_configured`: Backend に `PAW_SECRET_STORE_DIR` がない（上の「初めの準備」）。
- 403 `step_up_required` / `step_up_method_insufficient`: Passkey の Step-up をしてから。

登録しなかった Handle は Reaper が 1 日で消す。差し替えの前の Credential は、参照されなくなればその場で消える。

## 鍵の入れ替え・Host の入れ替え

主鍵を入れ替える仕組みはまだない（入れ替えるときは、新しい鍵で Credential を入れ直し、古い Handle の File は Reaper に消させる）。
Host の入れ替えや TPM の Clear で鍵が読めなくなったときも同じ。
