# CLI の Session の Subscription の Token の File の持ち主と Mode（Decision 0087 の 4 の表の一部を置き換える）

- Status: Approved
- Approval: 2026-10-14、Humanが作業Session内で、判断点の説明（推奨つき）を受けたうえで直接回答して承認（approved as recommended, all 1 point: 下の「決めたこと」を推奨どおり承認）
- Date: 2026-10-14
- Scope: Issue [#208](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/208) の PR 4b の 3c（[#236](https://github.com/TomokiAkiyama06/personal-ai-workspace/pull/236)、`SystemdLauncher` と `deploy/cli-session/`）。
- Supersedes（一部）: [Decision 0087](0087-cli-secret-store-and-egress.md)（Approved）の 4 の表の Claude の Subscription の行の「Session ごとの Directory の File（0400、所有者 `paw-agent-cli`）」だけ。その行の他（起動の Wrapper が `CLAUDE_CODE_OAUTH_TOKEN` に入れる、Unit の `Environment=` や Command 行に書かない、`--bare` を使わない）と 0087 のその他は変えない。

## 背景

0087 の 4 は、Subscription の Token を Session ごとの Directory の File（0400、所有者 `paw-agent-cli`）に置くとした。File を書くのは Backend の Process（User `paw`）で、Backend は Capability を持たないので別の Account（`paw-agent-cli`）へ `chown` できない。所有者を `paw-agent-cli` にするには root の手助け（setuid の Helper や、systemd に File を作らせる仕組み）が要り、硬くした Backend の Unit の形と合わない。

## 決めたこと

Token の File（`credential.env`、`CLAUDE_CODE_OAUTH_TOKEN` の 1 行）は **Mode 0640、所有者 Backend（`paw`）、Group `paw-cli-bridge`** とする。

- Group は `/run/paw-cli` の setgid で Session の Directory と File に継ぐ。`paw-cli-bridge` の Member は `paw-agent-cli` だけとする。
- 読めるのは Backend と `paw-agent-cli` だけ。他の Account は読めない。
- File は Session の Directory ごと Session の終わりに消す（止められなかった Unit の Directory は残し、Unit を止めてから消す。#236 のとおり）。
- File の中身は許可した名前の 1 行だけ。改行を含む値は拒否する（#236 のとおり）。

## 理由

- 0087 の目的（平文を Backend と CLI の Account の外に出さない、Session の間だけ置く）は 0640 と Group でも満たせる。
- root の手助けを足すと、攻撃面と配備の手順が増える。

## 承認時の決定（2026-10-14）

Human は推奨どおり、この違い（0640・所有者 Backend・Group `paw-cli-bridge`）を承認した。
