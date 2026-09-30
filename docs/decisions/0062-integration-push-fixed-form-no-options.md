# Integration の Push の固定の形に `--no-follow-tags` / `--no-recurse-submodules` / `--no-signed` を含める（Decision 0052 の 3・9 の Supersede）

- Status: Approved
- Approval: 2026-09-30、Humanが作業Session内で、判断点ごとの説明（推奨つき）を受けたうえで直接回答して承認（判断が必要な点 1。末尾の「承認時の決定」）
- Date: 2026-09-30
- Scope: Issue [#90](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/90)（PR #166）。`paw_backend/integration/publish.py` の `push_arguments`、SSH の Wrapper（`apps/backend/deploy/ssh-git-wrapper/paw_git_wrapper.py`）の `PUSH_OPTIONS` と `_check_push`。Migration はない
- Supersedes: [Decision 0052](0052-integration-push-and-pull-request.md)（Approved）の **3 の「形は 1 つに固定する」の形** と **9 の表の `push` の引数の形** だけ。0052 のその他の点（宛先、`paw/` の branch、Force なし、`--push-host`、Credential Helper、設定の確認など）は変えない。0052 の本文は書き換えない

## 背景

Decision 0052 の 3 と 9 は、Integration Gate の Push の形を次の 1 つに固定した。

```text
-c credential.helper= -c credential.helper=!<gh> auth git-credential push --quiet -- <URL> <commit>:refs/heads/paw/...
```

しかし `git push` は、明示した Option のほかに Checkout（Repository）の設定も読む。次の 3 つは、この形のままだと 0052 の意図（検査した Commit を 1 つの `paw/` の branch にだけ、確実に Push する）を崩す。

1. `push.followTags=true`: 検査した Commit を指す Annotated Tag も一緒に Push される（`paw/` の branch 以外を書く）。
2. `push.recurseSubmodules`（`check` / `on-demand`）: Submodule の Commit を Submodule 自身の Remote へ Push しようとする、または Push を止める（登録した Remote 以外を書く・Push が失敗する）。
3. `push.gpgSign=true`（`if-asked` 以外）: 署名つきの Push を試み、鍵がない・Remote が署名つきの Push を受け付けないと Push が失敗し、Task が `evaluating` のまま残る。

1 と 2 を打ち消す `--no-follow-tags` と `--no-recurse-submodules` は、PR #159（#132、0052 の実装）の中で Codex review の P1 の指摘に応えて実装と Wrapper の許可リストに入ったが、0052 の 3・9 の本文の形には反映されていない。3 を打ち消す `--no-signed` は、PR #166（#90 の P2 の仕分け）で足した。

## 決定

### 1. Push の固定の形

Integration Gate の Push の形を次の 1 つに固定する（0052 の 3 の形を置き換える）。

```text
-c credential.helper= -c credential.helper=!<gh> auth git-credential push --quiet --no-follow-tags --no-recurse-submodules --no-signed -- <URL> <commit>:refs/heads/paw/...
```

- `--no-follow-tags` は Checkout の `push.followTags` を、`--no-recurse-submodules` は `push.recurseSubmodules` を、`--no-signed` は `push.gpgSign` を打ち消す。どれも Checkout の設定によらず、Push が書くのは 1 つの `paw/` の branch だけで、署名を求めない。
- 由来: `--no-follow-tags` と `--no-recurse-submodules` は PR #159（Codex review の P1）、`--no-signed` は PR #166。

### 2. SSH の Wrapper の許可リスト

0052 の 9 の表の `push` の行の引数の形を次に置き換える。

| 副コマンド | 引数の形 |
| --- | --- |
| `push` | `--quiet --no-follow-tags --no-recurse-submodules --no-signed -- https://<host>/<owner>/<repo>.git <commit id>:refs/heads/paw/...`（先頭に `-c credential.helper=` と `-c credential.helper=!<Wrapper の --gh> auth git-credential` の 2 つ） |

- Wrapper は、Option の列がこの順のこの 5 つ（`--quiet`、3 つの `--no-`、`--`）と完全に一致するときだけ受け付ける。3 つの `--no-` のどれかがない形（0052 の元の形を含む）、順序の違う形、`--follow-tags`・`--recurse-submodules=...`・`--signed`・`--tags` などを足した形は拒否する。
- その他の検査（`--push-host` の Host、URL の形、Commit ID、`refs/heads/paw/` の宛先、`+` / `--force` / `--mirror` / `--delete` の拒否、2 つの `-c` の扱い、設定の確認、pin なし）は 0052 の 9 のまま。

## 選定理由

- 0052 の 3・9 は「形は 1 つに固定する」ことで、Push が書くものを Backend と Wrapper の両方で狭めることを決めた。Checkout の設定で書くもの（Tag・Submodule）や成否（署名）が変わるなら、固定したことにならない。打ち消す Option を形に含めるのは、その決定を保つための最小の変更である。
- 実装（`push_arguments` と `PUSH_OPTIONS`）はすでにこの形で、Test もこの形を確かめている。本文の形を実装に合わせ、Approved の Decision と実装の食い違いをなくす。

## リスク

1. `--no-signed` により、署名つきの Push を必須にする Remote（GitHub では通常ない）では Push が拒否される。その場合は `push_failed` で Task が `evaluating` のまま残る（0052 の 8 と同じ）。署名つきの Push が要るなら、別の Decision で扱う。
2. 実装済みの Wrapper を配備した後にこの形を変えると、古い Backend と新しい Wrapper（またはその逆）の組み合わせで Push が拒否される。Wrapper と Backend は同じ版で配備する。

## 判断が必要な点

1. Push の固定の形を `push --quiet --no-follow-tags --no-recurse-submodules --no-signed -- <URL> <commit>:refs/heads/paw/...` にし（1）、SSH の Wrapper はこの形だけを受け付けること（2）。Decision 0052 の 3・9 の形だけを Supersede する。推奨: 承認。

## 承認後の扱い

承認されたら `Status` と `Approval` を改める（Human が行う。この Decision を Agent が Approved にしない）。Decision 0052 の本文は書き換えない。方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。

## 承認時の決定（2026-09-30）

Human は、作業 Session で判断が必要な点について推奨つきの説明を受け、直接回答して承認した（判断が必要な点 1）。Human は 2026-09-30、Push の形を承認済みの 0052 から変える扱いについて選択肢（新しい小さな Decision で Supersede する案、推奨）から直接この案を選んだ。
