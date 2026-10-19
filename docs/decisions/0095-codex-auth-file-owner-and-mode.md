# Codex の ChatGPT の Login の `auth.json` の File の持ち主と Mode、Container の中の `CODEX_HOME`（Decision 0083 の 6 の一部を置き換える）

- Status: Proposed
- Date: 2026-10-20
- Scope: Issue [#239](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/239) の Codex の ChatGPT の Login の PR（[Decision 0087](0087-cli-secret-store-and-egress.md) の 5 の 4、[Decision 0089](0089-codex-cli-runtime-route.md) の 4 の 2）。関係する既存の口: `apps/backend/paw_backend/agents/codex_credentials.py`（`CodexAuthFile`）、`agents/codex_cli.py`（`CodexSessionAdapter`）、`deploy/cli-session/paw_cli_session_run.py`。
- Supersedes（一部）: [Decision 0083](0083-agent-runtime-adapters.md)（Approved）の 6 の「Credential の File を Session の間だけ CLI の Container の tmpfs（`CODEX_HOME` / `CLAUDE_CONFIG_DIR` に当たる場所、Mode 0700、所有者は上の専用の Account）に置く」のうち、**Codex の `auth.json` の File そのものの持ち主と Mode** だけ。`CODEX_HOME` を Container の tmpfs（0700、所有者は `paw-agent-cli`）にすること、Connection ごとに 1 Session の DB の Lease、Compare-and-swap の書き戻し、取り消しの時の書き戻しは変えない。[Decision 0088](0088-cli-token-file-mode.md)（Claude の `credential.env` の 0640）と同じ理由の、Codex の側の対になる決定。

## 背景

- 0083 の 6 は、Proxy で動かない CLI の Credential の File を「CLI の Container の tmpfs（Mode 0700、所有者は専用の Account）」に置くとした。0088 は、Backend（User `paw`、Capability なし）が別の Account（`paw-agent-cli`）へ `chown` できないため、Claude の Token の File を「0640・所有者 Backend・Group `paw-cli-bridge`」とした（0088 は Claude の行だけ）。
- Codex の ChatGPT の Login は、Session の途中で CLI 自身が `auth.json` を Refresh して書き換える（Refresh Token は使い捨て）。Backend は Session の終わりにその File を読み戻して Secret Store に書き戻す（0083 の 6）。そのため File は **CLI の Account が書けて、Backend が読める** 必要がある。
- Codex（`rust-v0.162.1`、`codex-rs/login/src/auth/storage.rs` の `FileAuthStorage::save`）は `auth.json` を Rename ではなく、既存の File を Truncate して書き直す（`OpenOptions::truncate(true).write(true).create(true)`、`mode(0o600)` は作るときだけ効く）。既存の File の持ち主と Mode はそのまま残る。
- Codex は `CODEX_HOME` に `auth.json` 以外も書く（Log・状態・`tmp/arg0`・`installation_id` など。同じ Source）。それらを Backend の Session の Directory（0750、所有者 Backend）に書かせると、`paw-agent-cli` の持ち物の Directory が残り、Backend が消せない。

## 提案（推奨）

1. **`CODEX_HOME` は Container の tmpfs**（`--mount type=tmpfs,destination=<Session の Directory>/config,tmpfs-mode=0700,chown=true`。所有者は Container の User = `paw-agent-cli`）。0083 の 6 のまま。Codex がそこに書く `auth.json` 以外の物は Container と一緒に消える。
2. その tmpfs の中に、Session の Directory の 2 つの File だけを Bind Mount する: `config.toml`（読み取り専用）と **`auth.json`（読み書き）**。
3. **`auth.json` の実体は Session の Directory（`/run/paw-cli/<uuid>/config/auth.json`、`/run` の tmpfs）の File で、Mode 0660・所有者 Backend（`paw`）・Group `paw-cli-bridge`**（Session の Directory の setgid で継ぐ）。Backend が Session の始めに新しい File として作り（umask に関わらず `fchmod` で 0660）、終わりに読み戻す。読み書きできるのは Backend と `paw-agent-cli` だけ（`paw-cli-bridge` の Member は `paw-agent-cli` だけ。Session の Directory は 0750）。0088 の `credential.env` との違いは Group の書き込み（CLI が Refresh を書くため）だけ。
4. File は Session の Directory ごと Session の終わりに消す（止められなかった Unit の Directory は残し、Unit を止めてから消す。#236 のとおり）。
5. 版を上げるとき（`VERIFIED_VERSIONS` に入れるとき）の Smoke で、その版の Codex が `auth.json` を同じ形（Truncate して書き直す。Rename しない）で保存することを確かめる。Rename で置き換える版では Bind Mount の上の Rename が失敗し（`EBUSY`）、Refresh が保存されないので、その版は有効にしない。

## 代替

- (a) 0083 の文のとおり File の所有者を `paw-agent-cli` にする: Backend は `chown` できず、root の手助け（setuid の Helper や systemd に File を作らせる仕組み）が要る（0088 と同じ理由で採らない）。
- (b) Container の中の入口（`start-codex`）が Session の Directory から tmpfs へ Copy し、CLI の終わりに Copy し戻す: 止める時（SIGTERM → 5 秒 → SIGKILL）に Copy し戻せないと、使い捨ての Refresh Token が失われ Connection が失効する。Copy し戻す先の File も結局 Backend が読める形が要る。推奨しない。
- (c) `CODEX_HOME` を Session の Directory の Group が書ける Directory（2770）にする: Codex が作る Directory（`paw-agent-cli` の持ち物）を Backend が消せず、内容（Log・状態）が `/run` に残る。Codex が `auth.json` を消して作り直すこともできる。推奨しない。

## 決めてほしいこと

1. **Codex の `auth.json` の実体を Session の Directory の File（0660・所有者 Backend・Group `paw-cli-bridge`）とし、Container の中の `CODEX_HOME` は tmpfs（0700、`paw-agent-cli`）でそこへ `config.toml`（読み取り専用）と `auth.json`（読み書き）だけを Bind Mount する。版ごとの Smoke で `auth.json` が Truncate で保存されることを確かめる**（上の 1〜5）でよいか。推奨: はい。

## Human に確かめてほしいこと（Decision の外の前提）

- 配備の確認（`deploy/cli-session/README.md` の「確かめること」）: Rootless の Podman が `--mount type=tmpfs,...,tmpfs-mode=0700,chown=true` と、その tmpfs の中への File の Bind Mount を、この硬くした Unit の下で受け付けること。

## リスク

- `paw-agent-cli` が Session の間 `auth.json` を書き換えられるのは 0083 の 6 の前提どおり（CLI が Refresh を書くため）。書き換えられた値は Secret Store に書き戻す前に JSON の Object かを確かめ、Compare-and-swap で古い版の上にだけ書く。
- Codex の版が保存の仕方を変えると Refresh が書き戻されない（5 の Smoke で防ぐ）。

## 承認後の扱い

承認されたら `Approval` に記録し、Status を Approved に改める。
方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。
