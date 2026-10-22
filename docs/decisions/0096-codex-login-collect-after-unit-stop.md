# 止められなかった Codex の Session の Login（`auth.json`）を、Unit の停止を確かめた後に回収する

- Status: Approved
- Approval: 2026-10-22、Humanが作業Session内で、判断点ごとの説明（推奨つき）を受けたうえで承認（approved as recommended, all 7 points: 1 は D（枠を保つ Process の周期的な確認と、どの Backend の Process でも回る回収の Loop）、2 は回収が終わるまで再起動を越えて枠を保つ（上限なし、Fencing と Tombstone）、3 は Unit が inactive / failed・Job なし・cgroup が空、4 は Log・System Health・Audit、5 は一時の失敗は再試行・確かな失敗は Connection に印を残して枠を返す、6 はすぐの読み戻しを続ける、7 は Egress の残りの警告を Health が `paw-egress-session@*` の Unit から読む）
- Date: 2026-10-21
- Scope: Issue [#280](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/280)（[#239](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/239) の後続。PR [#276](https://github.com/TomokiAkiyama06/personal-ai-workspace/pull/276) の Codex の P2 の指摘）。この Decision は設計だけで、コードは承認の後の PR で書く。関係する既存の口（PR #276 の Branch `claude/codex-credentials-239`）: `apps/backend/paw_backend/agents/codex_cli.py`（`CodexSessionAdapter.run_session` / `_session` の `finally`、`CodexAttachment.unstopped`）、`agents/codex_credentials.py`（`CodexAuthFile.collect`）、`connections/session_slot.py`（`SessionSlot.keep` / `_hold`）、`connections/service.py`（`rotate_credential` の Compare-and-swap）、`agents/cli_session.py`（`SystemdLauncher.stop_unit` / `show`、`remove_session`）、Migration `0239_connection_session_leases`。
- 具体化: [Decision 0083](0083-agent-runtime-adapters.md)（Approved）の 6 の「Session が終わったとき … File の Credential が渡したものから変わっていれば … Compare-and-swap で書き戻す」と、[Decision 0088](0088-cli-token-file-mode.md)（Approved）・[Decision 0095](0095-codex-auth-file-owner-and-mode.md)（Approved）の 4 の「止められなかった Unit の Directory は残し、Unit を止めてから消す」の、**止められなかった Codex の Session** の場合の手順。
- Supersedes（一部、下の 2 が推奨どおりなら）: 0083 の 6 の「期限切れは他の Process が取り直せる」のうち、**Codex の Session の UUID が記された枠の行** だけ（期限が切れても普通の取り直しはせず、Unit の停止を確かめてから回収の Loop が扱う）。0083 の 6 の他（Connection ごとに 1 Session、60 秒の待ち、`connection_busy`、Compare-and-swap、`rotate_credential`）と 0091 は変えない。

## 背景

### 今の形（PR #276）

- Codex の ChatGPT の Login（`auth.json`。Refresh Token は使い捨て）は、Session の Directory の File（`/run/paw-cli/<uuid>/config/auth.json`、0660・所有者 `paw`・Group `paw-cli-bridge`、0095）。Codex は Refresh した Login をこの File の Truncate と書き直しで保存する。
- Session の終わりに、`_session` の `finally` が `CodexAuthFile.collect` で読み戻し、渡した値と違う正しい JSON の Object なら `write_back`（`rotate_credential`: Credential の Lock の中で、保存されている版が Session に渡した版のままのときだけ次の版を書く。置き換え・切断された Connection には書かない）に渡す。Session は Connection の枠（`connection_session_leases` の行、TTL 300 秒・Heartbeat 10 秒）を持ったまま書き戻す。
- CLI を止められなかったとき（`systemctl stop` が 3 回とも失敗、または起動の後始末で止められなかった: `attachment.unstopped`）:
  - 読み戻しは **すぐ** 行う（Human の判断で当面このまま）。
  - Login を置いた後なら `SessionSlot.keep()` で枠を保ち、`release()` は行を消さずに `_hold()`（更新か、期限切れの後の取り直しを Process の間ずっと続ける）に替える。
  - Session の Directory は消さずに残し、Log に `A CLI session could not be stopped; its files stay` を出す。Operator が Unit を止めてから `/run/paw-cli/<uuid>` を消す（`deploy/cli-session/README.md`）。
  - MCP の Bridge（Backend の中の Unix Socket の Server）は、同じ `finally` で閉じる（Backend の側の物なので、CLI が止まらなくても閉じられる）。

### 残る危険（古さ）

止まっていない CLI が、すぐの読み戻しの **後に** もう一度 Refresh すると、保存した版は使えなくなる（使い捨ての Refresh Token が回る）。その新しい Login は Session の Directory の `auth.json` にしか残らず、今は誰も回収しない。Operator が手順どおり Directory を消すと失われる。次の Session（枠が切れた後・Backend の再起動の後）は古い Refresh Token を受け取り、Connection は `expired` になって Admin の入れ直し（Device Code の Login）が要る。

- 書き戻しを飛ばすのはより悪い（既に Refresh していれば、保存した版は確実に使えない）。すぐの読み戻しは残す（下の 6）。
- `collect` は正しい JSON の Object だけを取り、書き戻しは Compare-and-swap なので、書きかけの File が保存されることはない。

### 今の形で足りない物

1. **Unit の停止を誰が・いつ確かめるか** がない（Operator の手作業だけ）。
2. Backend の再起動の後、**どの版を渡したか**（Compare-and-swap の期待の版）を知る方法がない（`write_back` は Process の中の Closure）。
3. 保たれた枠は Process の間だけで、再起動の後 300 秒で期限が切れ、**CLI がまだ動いているかもしれないのに** 次の Session が枠を取れる。
4. Operator への知らせは Log の 1 行だけ。

## 提案（推奨の全体）

```text
Login を置く前（どの Codex の Session でも。2）
  └─ 枠の行に Session の UUID・渡した Credential の Handle と版を書く（Unit を起動する前に Commit）
Session の終わり（CLI を止められなかった）
  ├─ すぐ読み戻して書き戻す（今のまま。6）
  ├─ 枠の行を「保たれた」にする（`kept`）。すぐの書き戻しの後なら行の版をその新しい版に改める（改める前に落ちても 2 の「版の突き合わせ」で困らない）
  ├─ Directory は残す。MCP の Bridge は閉じる（今のまま）
  └─ System Health の connections を WARNING、Audit connection.session_kept（4）
Unit の停止の確認（1）
  ├─ 枠を保つ Process が 60 秒ごとに systemctl show（読むだけ）
  └─ どの Backend の Process でも動く回収の Loop（60 秒ごと）: 期限の切れた Session つきの行を引き継ぐ
回収（2 の Fencing の 1〜5 の順）
  ├─ 起動を塞ぐ Fence を置く（launch.json を消し、Directory と config を 0550）
  ├─ 停止を確かめる（3 の条件）
  ├─ auth.json を読み戻し、変わっていれば行の Handle で rotate_credential
  │   （期待の版は、行の版以上の、その Handle の今の版。2 の「版の突き合わせ」）
  ├─ もう一度停止を確かめる
  ├─ Directory の中身を消し、空の Tombstone（0550）として残し、空なことと停止をもう一度確かめる
  └─ 枠を返す（行を消す）、Health を戻す、Audit connection.session_collected
回収できなかったら（5）
  └─ 一時の失敗は保ったまま次の確認で再試行、確かな失敗は記録して枠を返す
```

回収は、保たれた枠を持ったまま（同じ行の持ち主として）行い、その後で枠を返す。保存は今と同じ `rotate_credential` の Compare-and-swap（置き換え・切断された Connection には書かない）。

## 決めてほしいこと

### 1. Unit の停止をどう確かめ、回収を始めるか

- A: Operator の Command だけ（例 `paw-cli-session-collect <uuid>`。`paw-connection-credential-put` と同じく root の Wrapper が `systemd-run` で Backend の User・暗号化した主鍵の一時の Unit を作り、Unit を止め、停止を確かめ、回収し、Directory を消す）。良い点: 人が見て行う。悪い点: Operator が忘れる・Directory を手で消すと Login が失われる。Command と主鍵の扱いが 1 つ増える。
- B: Backend の起動の時の走査だけ。悪い点: 再起動するまで回収されない（再起動しない限り、Operator が Unit を止めても Connection は塞がったまま）。
- C: A と B の両方（#280 の素案）。
- **D（推奨）: 枠を保つ Process が Unit の状態を周期的に読み（60 秒ごと、暫定）、加えてどの Backend の Process でも、起動の時から周期的に（60 秒ごと、暫定）DB の保たれた行を走査する回収の Loop を動かす。** Operator は今の手順のとおり Unit を止める（`systemctl stop paw-cli-session@<uuid>.service`、root）だけで、Directory は消さない（手順を変える）。読むのは `systemctl show`（読むだけ。polkit の規則は変えない）。保つ Process は自分でも `stop` を試み続ける（polkit が許す `stop`。1 分ごと、暫定）。回収の Loop は `session` のある期限の切れた行（2）だけを見る（`/run/paw-cli` の Directory を総なめにしない: 他の Process の動いている Session の Directory を触らないため）。起動の時の 1 回だけにしないのは、再起動が前の持ち主の最後の Heartbeat の直後だと、その時点では行の期限がまだ切れておらず引き継げず、その後に見に来る者がいなくなるため（Codex review）。持ち主が生きている間は行の期限が切れないので、Loop は何もしない。A の Command は、D で足りないと分かったら後から足せる（D の回収の関数をそのまま呼ぶ）。

### 2. 保たれた枠がいつまで続くか（Backend の再起動を越えるか）

- A: 今のまま Process の間だけ。悪い点: 再起動の後 300 秒で期限が切れ、CLI がまだ動いているかもしれないのに次の Session が同じ Login で動く（0083 の 6 が防ごうとした「並列の Session が互いの Token を失効させる」そのもの）。
- **B（推奨）: 回収が終わるまで、Backend の再起動を越えて保つ。** `connection_session_leases` の行に `session`（Session の UUID）・`credential_handle`（Session に渡した Credential の Handle。値ではない不透明な ID）・`expected_version`（渡した版）・`kept`（止められなかった印）を足す（Migration。Credential の値は書かない）。**`session`・`credential_handle`・`expected_version` は、止められなかったときではなく、Login を `auth.json` に置く前（Unit を起動する前）に Commit する。** 止められなかったと分かった時に初めて書くと、その前に Backend が落ちた（OOM・Kill。すぐの書き戻しと印の書き込みの間も含む）とき、Unit は動いたまま行は普通の行で、300 秒後に次の Session が同じ Login で動けてしまう（Codex review）。すぐの書き戻しで版が進んだら `expected_version` を改めるが、Secret Store（File）と DB の行は 1 つの Transaction にできないので、改める前に落ちても困らないようにする（下の「版の突き合わせ」）。**`session` のある行は、期限が切れても `acquire_session_slot` が取り直さない**（ここが 0083 の 6 の一部の Supersede）。普通に終わった Session は今のとおり自分の行を消すので、`session` のある行の期限が切れているのは「持ち主の Process が Session の途中か終わりで消えた」ときだけで、そのときは止められなかったときと同じく回収の Loop が扱う（`kept` でなくても Unit がまだ動いているかもしれない）。持ち主の Process が生きている間は今の `_hold` が更新し、Process が終わると、1 の D の回収の Loop（どの Process でも 60 秒ごと）が、行の期限が切れた後の最初の周期で「`session` があり、期限が切れている」行を持ち主を替えて引き継ぎ（行の持ち主の Compare-and-swap。同時に 2 つの Process が試しても 1 つだけが取る）、引き継いだ Process が `_hold` と 1 の確認を続ける（Unit を止められていなければ `stop` も試みる。`kept` でない行は引き継いだ時に `kept` にする）。その間、同じ Connection の新しい Session は今と同じく 60 秒待って `connection_busy`（fail-closed）。上限の時間は置かない（自然に切れると A と同じ危険になる）。代わりに 4 の知らせで Operator に止めてもらう。
  - **Handle の突き合わせ**: 遅れての回収は行の `credential_handle` で `rotate_credential` を呼ぶ（今の Connection の Handle ではない）。`rotate_credential` は Handle の Lock の中で、Connection がまだその Handle を指していることを確かめる（`still_attached`）ので、Admin が置き換えた後は `detached` になり、新しい Credential を古い Session の Login で上書きしない（Codex review）。
  - **版の突き合わせ**: すぐの書き戻しが Secret Store に版 2 を書いた後、行の `expected_version` を 1 から 2 に改める前に Backend が落ちると、行は 1 のまま残る。遅れての回収が行の版で Compare-and-swap をすると `stale_version` になり、CLI がその後に Refresh した唯一の使える Login を捨ててしまう（Codex review）。そこで遅れての回収の期待の版は、**行の `expected_version` 以上の、その Handle の今の版** とする（Handle の Lock の中で読む）。理由: `session` のある行がある間、その Connection の枠は他の Session に取られず、同じ Handle に `rotate_credential` を呼ぶのはこの Session の書き戻しだけ（Admin の置き換えは別の Handle を作る）なので、行の版より進んだ版はこの Session 自身が書いたもの。比べる相手も今の版の値にする（同じなら `unchanged`）。行の `expected_version` は監査と、今の版がそれより小さいとき（起こらないはず）に ERROR として止めるためだけに使う。
  - **前の持ち主の Fencing**: 前の持ち主の Process は、死んだのではなく止まっていた（SIGSTOP・長い GC・Host の負荷）だけかもしれない。引き継いだ後に前の持ち主が再開して、同じ UUID の Login を置く・Unit を起動すると、2 つの CLI が同じ Login で動く（Codex review）。そこで (a) 持ち主は外への副作用（Login を置く・Unit を起動する・書き戻す・**Session の Directory を消す**（今の `CodexCliRuntime` の後始末の `remove_session` を含む））の直前に、自分の期限（今の `SessionSlot` の `ttl - margin`、単調時計）がまだ来ていないことと、行の `holder` がまだ自分であること（Renew の成功）を確かめ、どちらかが崩れたら Session を永久にやめる（その UUID を二度と起動せず、Directory にも触らない。後始末は引き継いだ者に任せる）。(b) 加えて回収の Loop は、**停止の確認より前に** 起動を塞ぐ印（Fence）を置き、回収の後にもう一度停止を確かめる（Codex review: 印を回収の後に置くと、最後の確認と `systemctl start` の間で止まっていた前の持ち主が、回収の Loop の確認の後に起動し、回収と同時に CLI が動きうる）。順は次のとおり:
    1. 引き継いだら、Session の Directory の `launch.json` を消し、Directory とその下の `config` を書けない Mode（0550）にする（Fence。`config` も塞ぐのは、`config/auth.json` を同じ User が `unlink` できないようにするため: Codex review）。0550 は Entry の作成・削除・名前の変更だけを止め、Group（`paw-cli-bridge` = `paw-agent-cli`）の辿る権限（`x`）と File そのものの書き込み（`auth.json` は 0660 のまま）は残すので、まだ動いている CLI は停止の確認までの間も Refresh を `auth.json` に保存できる（Codex は Truncate と書き直しで保存し、Rename しない: 0095 の 5）。0500 にすると Group の辿る権限も消え、その間の Refresh が保存できず使い捨ての Token を失いうる（Codex review）。Unit の Program は `launch.json` がなければ起動を拒み、Backend（同じ User でも Mode で拒まれる）は `launch.json`・Login を作り直せない（`SystemdLauncher` の `launch.json` の置き直しも `unlink` で失敗する）。`auth.json` は読み戻せる（File の Mode は 0660 のまま、Directory の `r-x`）。`create_session` の `mkdir` は既にある名前で失敗する。
    2. その後で 3 の停止の確認（`inactive` / `failed`・Job なし・cgroup が空）。Fence の前に Program が `launch.json` を読んで起動していれば、Unit は `active` か cgroup が空でないので、ここで待つ（回収しない）。
    3. 回収（読み戻しと書き戻し）。
    4. **もう一度 3 の停止の確認**。崩れていたら（起こらないはず）枠を返さず、ERROR と 4 の知らせで止める。
    5. Directory を一時だけ 0700 にして中身（`auth.json`・Token など）を消し、0550 に戻す。**戻した後に、Directory（と `config`）が空であることと、3 の停止の確認をもう一度満たすことを確かめてから** 枠を返す（Codex review: 0700 の一瞬に前の持ち主が `launch.json`・Login を作り直して `start` しうるため）。空でない・止まっていないときは枠を返さず、次の周期で 1 からやり直す（枠の行が残るので回収の Loop がまた見る）。
  枠を保っている持ち主の Process 自身が回収するとき（1 の D の周期的な確認）も、同じ 1〜5 の順にする（回収の手順を 1 つにする）。
  Tombstone は空の Directory で、`/run` の tmpfs なので Host の再起動で消える（それまで残しても数は回収した Session の数だけ）。代わりの案: Unit の Program が起動の時に変わらない Fencing の Token（行の世代）を確かめる（Program は DB を読めないので、Token の置き場所がまた Directory の File になり、上の Fence と同じことになる）。
- C: B と同じだが上限（例 24 時間）を置き、過ぎたら ERROR を残して枠を返す。悪い点: 上限が来たときに CLI がまだ動いていれば A と同じ危険。推奨しない。
- 期待の版を Session の Directory の File（`/run` の tmpfs）に書く案は採らず、DB の行にする。枠と同じ行なら、Session・Handle・期待の版・保たれていることが枠と一緒に引き継がれ、回収の Loop は行だけを見ればよい（1 の D）。Host の再起動で `/run` が消えても、行が残るので 5 の「`auth.json` が無い」として記録して終えられる。

### 3. 「止まった」と見なす条件（0091 の Session ごとの Egress・Bridge を含む）

- **推奨: 回収の条件は、(a) `paw-cli-session@<uuid>.service` の `ActiveState` が `inactive` または `failed` で Job がなく、かつ (b) その Unit の cgroup が空であること**（決まった Path `/sys/fs/cgroup/system.slice/system-paw\x2dcli\x2dsession.slice/paw-cli-session@<uuid>.service` が無いか、その `cgroup.events` が `populated 0`。`populated` は子孫の cgroup も含むので、`Delegate=yes` の下の Container の cgroup も数える。読むだけで、Backend の User に読める）。この Unit は `--cgroups=split` で Container（と `conmon`）の cgroup を Unit の cgroup の下に置く（0092 の 4）ので、cgroup が空なら `auth.json` を書ける Process（Container の中の CLI）は残っていない。(a) だけでは足りない: Unit は `Type=exec` で `ExitType=` を指定していない（既定の `main`）ので、systemd は主の Process（`paw_cli_session_run.py`）が終わった時点で Unit を止まったと見なし、`KillMode=mixed` の SIGKILL の後に残った Process の終わりを待たないことがある（Codex review。`systemd.service(5)` の `ExitType=`）。(b) が満たされないときは回収せず、次の周期で見直す。代わりの案: Unit に `ExitType=cgroup` を足して (a) だけにする（Unit の変更と配備の確認が要る。(b) と併せてもよい）。同じ UUID の Unit を Backend がもう一度 `start` することは、2 の Fencing（持ち主の確認と Tombstone）で防ぐ（UUID は Session ごとに新しく、polkit が許すのは Backend の `start` だけ）。
- **0091 の Egress（`paw-egress-session@<uuid>.socket` / `.service`）は、同時に読んで確かめるが、回収の条件にはしない。** 両方は `BindsTo=` で Session の Unit と一緒に止まるはず。Proxy の Account（`paw-egress`）は `paw-cli-bridge` に入らない（0088・0091）ので `auth.json` を書けず、Socket は止まった Container にしか Mount されていないので、残っても Login の古さの原因にならない。残っていたら ERROR の Log と 4 の知らせ（`egress_unit_left`）を出し、Operator に止めてもらう（Backend の polkit は Egress の Unit を止められない）。`/run/paw-egress/session/<uuid>.sock` が残っているかも同じく記録する（`RemoveOnStop=yes`）。
- **MCP の Bridge** は Backend の中の Server で、Session の `finally` で既に閉じている（再起動の後は存在しない）。回収の前に、その Session の Bridge が同じ Process で閉じていることを確かめる（閉じていなければ閉じる）だけにする。
- 代わりの案: Egress の Unit も止まっていることを回収の条件にする（より厳しいが、Backend からは止められないので、Egress だけが残ると Connection が塞がったままになる）。

### 4. Operator への知らせ方

- A: Log だけ（今）。悪い点: 見落とすと Connection がずっと `connection_busy`（2 の B）。
- **B（推奨）: Log（ERROR。Session の UUID と止める Command）＋ System Health の `connections` を `WARNING`（保たれた Session があるとき。回収の失敗で Login が古いかもしれないときは `ERROR`）＋ Audit。** Health の変化は既存の `system_health.component_changed` の通知で、Owner / Admin の Notification Center に出る（[Decision 0070](0070-stored-notifications-and-event-stream.md)。新しい通知の経路は作らない）。Health の `params` は Code と数だけ（`kept_sessions`・`reason`: `unit_running` / `egress_unit_left` / `collect_failed`）で、Session の UUID は Log にだけ書く。Audit は System の `connection.session_kept`・`connection.session_collected`（結果の Code: `written` / `unchanged` / `stale_version` / `detached` / `missing` / `invalid`）を足す（AGENTS.md の 13）。
- C: B に加えて外への通知（Mail・Slack）。今は外への通知の経路がない。推奨しない（必要なら別の Decision）。

### 5. 回収できなかったとき

- **推奨: 一時の失敗と確かな失敗を分ける。**
  - 一時の失敗（DB・Secret Store の I/O の誤り、Lock の Timeout）: 枠を保ち、Directory も残し、次の確認（1 の 60 秒ごとの周期）でやり直す。Health は `WARNING` のまま。
  - 確かな結果（もう一度やっても変わらない）: 記録して終える（Directory の中身を消して Tombstone にし、枠を返す）。
    - `auth.json` が無い（Host の再起動で `/run` が消えた・手で消された）、正しい JSON の Object でない: Login が古いかもしれない。**枠の行を消す前に、同じ Transaction で Connection の行に残る印（例 `credential_suspect_at` と理由の Code。Migration）を書く**（枠の行は消えるので、Health はそれだけでは `collect_failed` を知れない: Codex review）。System Health の `connections` はこの印がある間 `ERROR`（`collect_failed`）。印は、Provider への Health Check が `connected` を返したとき、または Admin が Credential を置き換えたときに消す。`expired` なら Admin が入れ直す（0083 の 6 の「リスク」のとおり）。代わりの案: Connection の `status` を `unavailable` にして次の Health Check に任せる（新しい列は要らないが、その間 Session も使えない）。
    - 値が変わっていない: 正常（`unchanged`）。
    - Compare-and-swap で書けない（版が進んでいた・Connection が置き換え・切断された）: 書かない（今と同じ）。`stale_version` / `detached` を Audit に残す。置き換え・切断なら古い Login はもう使われないので正常として終える。版が進んでいた（別の Session が書いた）は、2 の B では起こらないはずの状態なので ERROR の Log を残す。
  - Host の再起動で `/run/paw-cli/<uuid>` が無く、Unit も `inactive` のときは、上の「`auth.json` が無い」と同じに扱う。
- 代わりの案: どの失敗でも枠を返して Directory を消す（単純だが、DB の一時の誤りで回収できたはずの Login を失う）。どの失敗でも保ち続ける（確かな失敗で Connection がずっと塞がる）。どちらも推奨しない。

### 6. 止められなかった Session の終わりの、すぐの読み戻しを続けるか

- **推奨: 続ける（今のまま）。** CLI が止まらないまま Process が落ちても、それまでの Refresh は保存されている。遅れての回収は、すぐの書き戻しの後の版（2 の「版の突き合わせ」: 行の `expected_version` 以上の、その Handle の今の版）を期待の版にして、その後の Refresh だけを書き足す。
- 代わりの案: すぐの読み戻しをやめ、遅れての回収だけにする。悪い点: Unit が止まるまで（または Host の再起動で `/run` が消えれば永久に）Refresh が保存されない。推奨しない（#280 の背景のとおり）。

## Test（実装の PR）

Fake の Launcher（`stop` が失敗し、`show` が `active` → `inactive` と変わり、cgroup の `populated` を別に変えられる）と Fake の Store で:

1. Login を置く前に行に `session` と `expected_version = 1` → 止められなかった Session → すぐの読み戻し（版 1 → 2）→ 行が `kept` で `expected_version = 2` → 新しい Session は `connection_busy`。
2. その後に CLI が `auth.json` を書き換える → `show` が `active` の間は回収しない（Directory も消えない）→ `inactive` になると回収して版 3 を書く → Directory を Tombstone にする → 行を消す → 新しい Session が動く。
3. Login を置いた後・止める前に Backend が落ちる（`kept` の印が書かれない）→ 行は `session` つきで期限が切れても次の Session に取られず、回収の Loop が Unit の停止を確かめてから回収すること。
4. Backend の再起動（保つ Process を捨てる）→ 行の期限が切れても他の Session は取れない → 回収の Loop が引き継いで 2 と同じ結果。再起動が最後の Heartbeat の直後（起動の時にはまだ期限が切れていない）でも、期限が切れた後の周期で引き継ぐこと。2 つの Process の Loop が同時に試しても 1 つだけが取ること。
5. `ActiveState` が `inactive` でも cgroup が `populated 1` の間は回収しないこと。
6. 5 の各場合（一時の失敗の再試行、`auth.json` が無い、JSON でない、Compare-and-swap の失敗、Egress の Unit の残り）と、Health・Audit・Log に Credential の値が出ないこと。
7. 確かな失敗の後、枠の行が消えても Health が Connection の印から `collect_failed` の `ERROR` を出し続け、`connected` の Health Check で消えること。
8. 行の `expected_version` が 1 のまま Secret Store が版 2（すぐの書き戻しの後に落ちた）でも、遅れての回収が版 2 を期待の版にして版 3 を書くこと。Admin が Credential を置き換えた後は、行の Handle で `detached` になり、新しい Credential を書き換えないこと。
9. 前の持ち主が引き継がれた後に再開しても、Login を置かず Unit を起動しないこと。Fence の後に `start` が来ても Unit の Program が `launch.json` なしで拒むこと。Fence の前に起動していた Unit は 2 の確認で待たされ、回収の後の 4 の確認が崩れたら枠を返さないこと。
10. 回収の Loop が、`session` のない行・期限の切れていない行と他の Process の動いている Session の Directory を触らないこと。

## リスク

- 2 の B では、Operator が Unit を止めるまで Codex の Connection が使えない（fail-closed）。4 の知らせで気づける前提。Unit が `systemctl stop` でも止まらない状態（D 状態の Process など）は Host の root の対応になる。
- 停止の判定は Unit の cgroup が空であることに依る。`--cgroups=split` が効いていないと Container が Unit の外に残りうる（0092 の 7 と `deploy/cli-session/README.md` の確認の項目で先に確かめる）。
- 確認の周期（60 秒）と再試行の周期は暫定。

## 承認後の扱い

承認されたら `Approval` に記録し、Status を Approved に改める。実装の PR で Migration（`connection_session_leases` の 4 列と Connection の印）、回収の関数、回収の Loop、Health・Audit、`deploy/cli-session/README.md` の Operator の手順（Directory を手で消さない）を変える。
方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。
