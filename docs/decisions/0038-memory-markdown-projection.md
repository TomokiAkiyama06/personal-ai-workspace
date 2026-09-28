# Memory Markdown Projection の出力先・配置・形式・権限・失敗の通知

- Status: Approved
- Date: 2026-09-28
- Approval: 2026-09-28、Human が「決めてほしいこと」の 1〜9 を推奨どおりに承認した（末尾の「承認後の扱い」）
- Scope: Issue [#39](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/39)（PAW-045: Memory Markdown Projection）。`apps/backend/paw_backend/memory/projection/`、`apps/backend/paw_backend/cli/memory_projection.py`、`apps/backend/deploy/systemd/paw-memory-projection*`。関連: PAW-040（#34、Schema）、PAW-042（#36、[Decision 0034](0034-memory-versioning-freshness.md)）、PAW-047（#41、Recovery Repository）、[Decision 0031](0031-audit-retention-scheduler.md)（定期実行と失敗の通知の型）、[Decision 0009](0009-shared-memory-administration.md)、[Decision 0010](0010-research-privacy-filter-policy.md)、[Decision 0018](0018-memory-journal-consolidation-policy.md)、[Decision 0019](0019-hybrid-retrieval-policy.md)
- Supersedes: なし（既存の Decision を書き換えない。要件が決めていない点を埋める）

## 背景

[REQUIREMENTS.md](../../REQUIREMENTS.md)（「Storage / Markdown Projection」「Memory Markdown Backup / Backup Authority」「Storage placement: HDD Model Store / Memory Markdown」）と
[MEMORY_ARCHITECTURE.md](../MEMORY_ARCHITECTURE.md)（1・2・4・5・6・7・19 節）は次を `[FIXED]` として定める。

- PostgreSQL が正本。Markdown は人がすぐ読める「常時生成ビュー」で、Backup・Export・履歴比較・Recovery の Fallback に使う。
- Markdown は Personal AI Workspace 専用の領域（例 `/srv/personal-ai/memory/`、HDD 上）に置き、**Project Repository へは書かない・commit しない**。
- 保存イメージは `users/<user-id>/`、`projects/<project-id>/`、`repos/<repo-id>/`、`shared/`、`decisions/`。
- V1 の編集は Memory UI → PostgreSQL → Markdown の再生成。Markdown の直接編集は標準の経路にしない。
- HDD 上の Markdown の I/O を Chat の Latency の Critical Path に置かない。
- Markdown Projection を Dedicated Recovery Repository へ 30 分ごとに commit / push する（PAW-047）。Secret・DB dump・WAL は Git に入れない。
- Backup / Recovery の画面は「Last successful projection generation」「Backup failure / retry state」を示す（UI_DESIGN.md）。

Issue の受け入れ条件は「PostgreSQL を Source of Truth として HDD へ Projection を生成」「User / Project / Repo / Shared を分離」「direct repo injection なし」「diff-friendly Markdown」「projection failure 通知」。
要件が決めていないのは、**出力先の決め方と安全の条件**、**どの Version を投影するか**、**ファイルの形式**、**誰が読めるか（ファイルの権限）**、**Secret の扱い**、**いつ・どう実行し、失敗をどう通知するか**である。
この Decision はその推奨を示す。PR（Issue #39）は推奨どおりに実装しており、承認されない点があれば別の PR で直す。

**この Decision は 2026-09-28 に Human が承認した（Approved）。** 承認は「決めてほしいこと」の 1〜9 を推奨どおりとするもので、下の各選択は承認された方針である（10 の扱いは末尾の「承認後の扱い」）。

## 提案

### 1. 出力先: `PAW_MEMORY_PROJECTION_DIR`（既定なし）と、拒否する場所

- 出力先は環境変数 `PAW_MEMORY_PROJECTION_DIR` で与える。**既定値はない**（未設定なら Command は何もせず終了コード 2）。配備の例は `/srv/personal-ai/memory`（HDD）。
- 実行のたびに次を確かめ、満たさなければ何も書かずに失敗する（終了コード 3、Audit に `check_target:<理由>`）。
  - 絶対 Path で、正規の綴り（`..`・末尾の `/`・Symbolic Link を含まない）。`/` ではない。親 Directory が存在する。
  - **git の Work Tree の中ではない**: その Directory と上のすべての Directory に、git が Repository とみなす `.git`（`.git` File、`.git` の Symbolic Link、`HEAD` を持つ `.git` Directory）がない。空の `.git` Directory は git にとって Repository ではないので数えない。
  - **人の Home Directory の中でも上でもない**: `/home` と、`uid >= PAW_REPOSITORY_MIN_LINUX_UID` の Account の Home（Checkout はすべてそこにある。Decision 0017）。
  - 既存なら、この Process の User が所有する Directory。
  - **空か、Projection の Marker（`.paw-memory-projection`）を持つ**。初回は空の（または新しい）Directory に Marker を置く。Marker のない空でない Directory は拒否する（設定の誤りで、他の Directory に書いたり、そこのファイルを消したりしない）。
- systemd の例の Unit は、加えて `ProtectHome=true`・`ProtectSystem=strict`・`ReadWritePaths=/srv/personal-ai/memory` で、Home と他の Path へ書けなくする（多重の防御）。

### 2. 配置: 公開範囲ごとの Directory、名前は ID だけ

```text
<PAW_MEMORY_PROJECTION_DIR>/
├── .paw-memory-projection
├── users/<user-id>/<memory-id>.md, INDEX.md
├── projects/<project-id>/...
├── project-groups/<project-group-id>/...
├── repos/<repo-id>/...
└── shared/...
```

- Directory は現在の Version の Scope の列（`scope` と `owner_user_id` / `project_id` / `project_group_id` / `repo_id`）から決める。**Title や Login 名は Path に使わない**（Memory の文字列が書き先を選べない。Login 名を Path に出さない）。
- Memory 1 件を 1 File（`<memory-id>.md`）にする。Title が変わっても File 名は変わらない。各 Directory に `INDEX.md`（一覧の表）を置く。
- MEMORY_ARCHITECTURE.md の図にある **`decisions/` は作らない**。Decision 種別の Memory は、その Scope の Directory に置く（`memory_type` は Front Matter と `INDEX.md` に出る）。別の Directory へ写すと、公開範囲の違う Memory が 1 つの Directory に混ざるため。
- `project_group` は要件が図に書いていないが、Schema にある Scope なので別の Directory にする（どの User・Project にも混ぜない）。

### 3. 何を投影するか: 各 Memory の現在の Version

- 各 Memory の**現在の Version**（`version_number` が最大）を 1 つだけ投影する。Status（`active` / `deprecated` / `superseded` / `history`）は問わず、Front Matter と `INDEX.md` に示す（`INDEX.md` は `active` を先に並べ、Stale Candidate に `(stale)` を付ける）。
- **`session_only` は投影しない**（Long-term Memory ではない。REQUIREMENTS.md「Session / Task 終了後に保持しない」）。
- 過去の Version は投影しない（履歴は PostgreSQL と、PAW-047 の Recovery Repository の Git 履歴に残る）。そのため公開範囲を狭めた Memory（Decision 0034 の 2: `project → user`）は、新しい公開範囲の Directory にだけ現れ、古い Directory からは消える。
- Relation・Provenance（`memory_sources`）・Embedding は投影しない。Recovery に要る Machine-readable な Metadata は PAW-047（Recovery Projection）で扱う。
- 1 回の実行は 1 つの Snapshot（`REPEATABLE READ, READ ONLY` の 1 Transaction）から作る。

### 4. 形式: diff-friendly で決定的な Markdown

- 各 File: `---` で囲んだ Front Matter（Key の順は固定。値は JSON の文字列・数値・真偽値。JSON は YAML として読める。ただし JSON が Escape しないが YAML が拒む、または改行と読む文字（DEL・C1 制御文字 U+0080〜U+009F・U+2028・U+2029・U+FEFF・U+FFFE・U+FFFF）は `\uXXXX` で書く。日本語などの読める文字はそのまま）、生成の注記（`<!-- Generated from PostgreSQL ... Do not edit ... -->`）、`# <Title>`、本文。
- Front Matter の Key: `projection_format`（`1`）、`memory_id`、`version`、`scope`、Scope の ID、`title`、`memory_type`、`status`、`confirmation_state`、`importance`、`pinned`、`freshness_policy`、値があるときだけ `verified_at`・`revalidate_after_seconds`・`revalidate_triggers`（整列）・`expires_at`・`commit_sha`・`branch`・`stale_since`、`version_created_at`、置換があったときだけ `redactions`、本文を切ったときだけ `truncated: true`（5）。
- 改行は LF、UTF-8、末尾の改行は 1 つ。時刻は UTC（`...Z`）。**実行の時刻は File に書かない**（変わらない Memory は同じ bytes になる）。
- 変わらない File は書き直さない（更新時刻も変えない）。Git の差分には変わった Memory だけが出る。
- `INDEX.md` は Status・種類・Title・ID の順に並べ、表の区切り文字（`|`）と改行を Escape する。

### 5. Secret: 投影の段階で置換し、PostgreSQL はそのまま

- Title・本文・Branch 名（`repo_commit` の Memory の自由記述）の中の、認識できる Credential（`tools.credentials.redact_text`: GitHub・AWS・OpenAI などの Token、Private Key、`password = ...` の形など）を `[REDACTED]` に置き換えてから File に書く。件数を Front Matter の `redactions` と、Audit の `redacted=N` に残す。
- PostgreSQL の本文は変えない（Source of Truth）。投影は PAW-047 で Git に入り、要件は Git に Secret を入れないとするため。
- 検出は最善の努力であり、すべての Secret の形を見つけられるわけではない（`tools/credentials.py` と同じ限界）。
- **検査できない長さの本文**: `redact_text` は `MAX_TEXT_CHARS`（1,000,000 文字）を超える文字列を検査せず、先頭の `MAX_TEXT_CHARS` 文字に `[TRUNCATED]` を付けて返す（検査していない部分を Git へ出さないため）。`memory_versions.content` には長さの上限がないので、その Memory の File は本文の全体ではない。これを置換（`redactions`）とは数えず、Front Matter の `truncated: true`、Audit の ` truncated=N`（`completed` の `reason` の末尾、1 件以上のときだけ）、`run` の出力で示す。実行は成功のまま（1 件の長い Memory が 5 分ごとに `OnFailure=` を起こし続けないため）。本文の全体は PostgreSQL（正本）と、その Backup にある。

### 6. 実行と失敗の通知

- Server ローカルの Command `python -m paw_backend.cli memory-projection-run` を、systemd timer（`paw-memory-projection.timer`、`OnCalendar=*:0/5`、`Persistent=true`）が 5 分ごとに起動する（Decision 0031 と同じ型。cron でも同じ Command を使える）。
- 1 回の実行: 出力先を確かめて Lock（Marker の `flock`、非 Blocking）→ Snapshot を読む → Render → 書く → 結果を Audit へ。Lock は読み取りの前から結果を Audit に記録し終えるまで持つ（古い Snapshot が新しいものを上書きしない。Lock を取った読む側は、目の前の File に対応する結果を必ず読める。9）。同時の 2 つ目の実行は何もしない（終了コード 1、記録しない）。
- **Audit**: 実行ごとに `audit_events` に 1 行（別の Transaction）。`memory.projection.completed`（`reason = memories=N written=N removed=N redacted=N`）か `memory.projection.failed`（`reason = <step>:<code>`。`<step>` は `check_target` / `read_database` / `render` / `write_files`、`<code>` は閉じた語彙か例外の型の名前。**Path・例外の Message・Memory の文字列は書かない**）。`resource_kind = memory_projection_run`、`decision = allow`、Actor なし。列・制約・Migration は増やさない。
- **終了コード**: `0` 成功、`1` 拒否（使い方、同時実行）、`2` 環境（設定、URL・Directory が未設定、DB に届かない。`run` では「読み取りが失敗し、その失敗の記録もできなかった」を DB に届かないとみなす）、`3` 投影の失敗（出力先の拒否、読み・Render・書きの失敗、SIGTERM、結果を記録できない）。0 以外で `OnFailure=paw-memory-projection-failure.service` が起動し、`crit` の Journal と `wall` を出す（通知先は配備で差し替える）。
- **監視**: 読み取りだけの `memory-projection-check [--max-age-minutes N]`（既定 30）。「最後の実行」は Database の時計の `recorded_at` の順で決める（呼び出し側の `occurred_at` ではない。Host の時計が戻っても、新しい失敗が古い成功の陰に隠れない）。最後の実行が失敗、または N 分以内に成功がなければ終了コード 3。Backup / Recovery の画面（後続）は同じ関数（`projection_status`）で「Last successful projection generation」と最後の失敗を示せる。
- 出力先の確認・読み取り・Render で失敗した実行は、既存の File を書き換えも消しもしない（読み取りの失敗で投影が空になることはない）。書く前に、Plan が要る Directory と File の名前をすべて確かめる（Link・Directory の場所の File・File の場所の Directory・他の User の所有）ので、`unsafe_entry` の実行は何も書き換えず消しもしない。**それでも書き込みの途中で失敗した実行**（ENOSPC、`TimeoutStopSec` の後の SIGKILL など）は、File ごとには原子的（一時名に書いて `rename`）だが、Directory を順に処理するので、処理を終えた Directory（削除を含む）と、まだの Directory が混ざった状態を残し得る（例: `INDEX.md` が書かれていない File を指す）。この状態は Audit の `memory.projection.failed` で分かり、次に成功した実行が全体を直す。さらに、書く前に Root へ `.paw-memory-projection-incomplete` を置き、`memory.projection.completed` を記録できた後にだけ消す。途中で失敗した実行や、結果を記録できなかった実行（DB の一時的な停止など）は、この Flag を残すので、古い `completed` の行が混ざった Tree を保証することはない。Flag を消せなかった実行は失敗とし、2 行目の `memory.projection.failed`（`write_files:<code>`）を記録して終了コード 3 にする（写せない Tree を成功と示さない）。書く前の確認は、Plan が要る Directory に加えて、掃除のために開く既存の Directory（Top の Directory と `<uuid>` の Directory）も含む。読む側の条件は 9。
- SIGTERM は書いている File を書き終えてから取り消しになり、`<step>:CancelledError` を記録する。結果の記録の最中の SIGTERM は、記録を終えてから取り消しになる（実行ごとの 1 行を欠かさない）。出力先を開いている間の取り消しでも、取った Lock はすぐ放す（Runner を Backend の Process の中から呼んでも、Lock が残らない。8）。

### 7. 権限: Backend の OS User だけが読める、Audience ごとの Directory

- 投影は Principal の要求ではなく Backend 内部の Job で、**全 Scope を読む**（Decision 0034 の 5 の鮮度の Job と同じ、`memory/acl.py` の ACL 条件の例外）。公開範囲の分離は、**書く場所**（2）と **File の権限**で行う。
- Root と全 Directory は `0700`、全 File は `0600`（`fchmod`。umask によらない）。所有者は Job を動かす OS User。他の OS User（Linux の各 User を含む）は一覧も読み取りもできない。緩い Mode は次の実行で直す。Root の Mode を `0700` にするのは、出力先として受け入れた（空か、正しい Marker を持つ）後で、拒否した Directory の権限は変えない。
- **Job は Backend と同じ OS User（例 `paw`）で、`PAW_DATABASE_URL`（Application の Role）で動かす**。必要なのは `memory_versions` の SELECT と `audit_events` の INSERT / SELECT だけで、Backend と同じ Credential なので、別の OS User にしても守るものがない（Decision 0031 は Table の Owner の Credential を持つため専用 User にした。ここは違う）。
- **Workspace の User ごとに Linux の Owner を分ける（`chown`）ことはしない**（V1）。Root 権限が要り、Workspace の User と Linux の Account の対応（Decision 0017 / 0029）がない User もいるため。User が自分の Memory を読むのは Memory UI（後続）で、この Directory は Server の運用者（Owner）・Backup・Recovery のためのもの。
- Symbolic Link は辿らない（`O_NOFOLLOW` と `dir_fd`）。Directory の位置に Link があれば失敗（`unsafe_entry`）。File は一時名に書いて `rename` するので、Link や Hard Link の先へ書き込まない。Projection が作れる名前（`<uuid>.md`、`INDEX.md`、上の Directory、`<uuid>` の Directory、自分の一時 File）以外は読まず、消さず、`unmanaged` として数えるだけ。

### 8. 直接編集と、変更後の即時の再生成

- 投影の File は毎回上書きする（V1 は Memory UI → PostgreSQL → Markdown）。File の先頭に「編集しない」の注記を置く。File の直接編集の取り込み（Front Matter の Version と Optimistic Lock、Conflict の表示、Import の検証、Audit。MEMORY_ARCHITECTURE.md の 7）は扱わない。
- Memory の保存ごとの即時の再生成（Event 駆動）はこの Issue では作らない。最大 5 分の遅れを許す（要件: HDD の I/O を Chat の Critical Path に置かない）。Memory UI の Issue で、保存の後に同じ Runner を起こす配線を足せる（全体の再生成は変わらない File を書かないので、呼ぶ回数が増えても差分は増えない）。
- Owner / Admin の画面からの手動実行（REQUIREMENTS.md「Owner / Admin は管理画面から手動 Backup を実行可能」）は PAW-047 の Backup の画面で扱う。それまでは Server 上で同じ Command を実行する。

### 9. Recovery Repository（PAW-047）との関係

- Projection の Directory は git の Work Tree の中に置けない（1）。PAW-047 の Batch は、この Directory を Dedicated Recovery Repository の Checkout（別の場所）の `memory/` へ写して commit / push する（MEMORY_ARCHITECTURE.md 5 の図 `/srv/personal-ai/memory/ → Dedicated Recovery Repository / memory/` のとおり）。
- **PAW-047 が写すときの条件**: 写す間、Marker（`.paw-memory-projection`）の `flock` を取り（投影の実行と重ならない）、その Lock を持ったまま、`.paw-memory-projection-incomplete` がなく、かつ `projection_status` の**最後の実行が `memory.projection.completed`** のときだけ写す（書き込みの途中で失敗した、新旧が混ざった Tree を commit しない。6）。最後の実行が失敗なら、写さずに Backup の失敗として示し、次の回に再び試す。
- Projection の Directory 自体を Recovery Repository の Work Tree にする方式を PAW-047 が選ぶなら、そのときに 1 の「git の Work Tree の中ではない」を、設定した Recovery Repository だけを許す形に変える Decision を足す。

## 選定理由

- 出力先に既定値を置かず、Git の Work Tree・Home・Marker のない Directory を拒否するのは、「Project Repo へ Markdown を書かない」を設定の誤りでも破らないため（Fail-closed）。
- Directory を ID で分けて `0700` / `0600` にすると、ある User の Private Memory が、他の User の Directory にも、他の OS User が読める File にも現れない。
- 現在の Version だけを、実行時刻を含めずに、変わらない File を書き直さずに出すと、Git の差分が「変わった Memory」だけになる（diff-friendly）。
- Audit の 1 行・終了コード・`OnFailure=`・`check` は Decision 0031 で承認された型と同じで、運用者が 1 つの見方で失敗に気づける。

## 代替案

- **出力先に既定値（`/srv/personal-ai/memory`）を置く**: 設定を忘れても動くが、配備ごとの HDD の Mount 先と違うと Root File System に黙って書く。採らない。
- **Title を File 名にする**: 読みやすいが、Title の変更が Rename になり差分が大きく、文字列が Path を選べる。採らない。
- **全 Version を投影する**: 履歴が Markdown で読めるが、File 数が増え、狭めた Memory の古い（広い）公開範囲の Version が古い Directory に残る。採らない（履歴は DB と Git）。
- **`decisions/` を作る**: 図のとおりだが、公開範囲の違う Decision が 1 つの Directory に混ざる。採らない。
- **User ごとに Linux の Owner（`chown`）を分ける**: OS のレベルで User ごとに読めるが、Root が要り、Linux の Account のない User がいる。V1 では採らない。
- **Secret を置換しない**: File は PAW-047 で Git に入る。採らない。
- **Backend Process 内の定期 Task で実行する**: 複数 Worker・再起動の重複の扱いが要り、HDD の I/O が Backend の Process に入る。採らない（Decision 0031 と同じ理由）。
- **状態を専用の Table / Status File に持つ**: Migration が要る、または Projection の Directory（Git に入る）に運用の情報が混ざる。Audit の行で足りるので採らない。

## リスク

- **この Directory はすべての User の Private Memory を持つ。** Job の OS User と root は全部を読める。HDD の Backup・取り外しも同じ扱いが要る（Disk の暗号化は配備の作業）。
- Secret の検出は完全ではない。検出できない形の Secret は File（と PAW-047 の Git）に入り得る。
- 最大 5 分（Timer の間隔）、Markdown は DB より古い。変更の直後に File を読むと古い内容が見える。
- Home の判定は `/etc/passwd` の `uid >= PAW_REPOSITORY_MIN_LINUX_UID` と `/home` に基づく。それ以外の場所にある Checkout（`PAW_REPOSITORY_EXISTING_ROOTS` を Home の外に設定した場合）は、`.git` の判定だけで守る。
- 投影は 1 回ごとに全 Memory を読む（`memory_versions` の全件の Window 関数）。Memory の数が大きくなれば、変更のあった Memory だけを読む差分方式が要る（後続。形式は変わらない）。
- Timer が無効化されると失敗 Unit も動かない。`memory-projection-check` を別の監視から呼ぶことで補う。

## 決めてほしいこと

推奨の答えを添えて Human / Admin に問う。

1. **出力先**: `PAW_MEMORY_PROJECTION_DIR`（既定なし）。git の Work Tree の中・Home の中や上・Marker のない空でない Directory を拒否する。
   推奨: 提案どおり。
2. **配置**: `users/` `projects/` `project-groups/` `repos/` `shared/` の下を ID で分け、Memory ごとに `<memory-id>.md`、Directory ごとに `INDEX.md`。`decisions/` は作らない。
   推奨: 提案どおり。
3. **投影する Version**: 各 Memory の現在の Version だけ（Status を問わず示す）。`session_only` は除く。Relation・Provenance は PAW-047。
   推奨: 提案どおり。
4. **形式**: 固定の Front Matter（JSON の値）、生成の注記、見出し、本文。LF・UTC・実行時刻なし・変わらない File は書き直さない。
   推奨: 提案どおり。
5. **Secret**: 投影の段階で `redact_text` により置換し、件数を記録する。PostgreSQL は変えない。
   推奨: 提案どおり。
6. **実行と失敗の通知**: systemd timer（5 分ごと）+ `memory-projection-run`、実行ごとの Audit の 1 行（`memory.projection.completed` / `failed`）、終了コード 0〜3、`OnFailure=` の Unit、読み取りだけの `memory-projection-check`（既定 30 分）。
   推奨: 提案どおり。
7. **権限**: `0700` / `0600`、Backend と同じ OS User、`PAW_DATABASE_URL`。User ごとの `chown` はしない。全 Scope を読むことを ACL の例外として文書化する。
   推奨: 提案どおり。
8. **直接編集・即時の再生成・手動実行**: File は毎回上書き（直接編集は取り込まない）。保存ごとの即時の再生成と画面からの手動実行は後続（Memory UI / PAW-047）。
   推奨: 提案どおり（後続の課題）。
9. **Recovery Repository との関係**: Projection の Directory は Work Tree の外に置き、PAW-047 が Recovery Repository へ写す。写す間は Marker の Lock を取り、最後の実行が成功のときだけ写す。別の方式を選ぶなら新しい Decision で 1 を変える。
   推奨: 提案どおり。
10. **検査できない長さの本文**: 1,000,000 文字を超える本文は先頭だけを投影し、`truncated: true` と Audit の `truncated=N` で示す（置換の件数とは別。実行は成功のまま）。代わりに「分割して検査し全体を出す」（Private Key のように行を跨ぐ Secret を分割の境で見落とし得る）や「実行を失敗にする」（直すまで 5 分ごとに通知が続く）もある。
   推奨: 提案どおり（本文の長さの上限は Memory の書き込みの側の後続の課題）。

## 承認後の扱い

- 2026-09-28 に Human が「決めてほしいこと」の 1〜9 を推奨どおりに承認した。Status を Approved に改めた。
- 10（検査できない長さの本文の切り詰めと、その明示）は、独立 Review への対応（PR #138）で後から加えた点で、承認として伝えられたのは 1〜9 である。実装は 10 の推奨どおり（切ったことを `truncated: true` と Audit の `truncated=N` で示す）で、Human が別の答えを選ぶなら、背景の段落のとおり別の PR で直す。
- 9 の「最後の実行が成功のときだけ写す」は、独立 Review（Codex）の指摘を受け、実装で「Root に `.paw-memory-projection-incomplete` がないこと」も条件に加えて厳しくした（書き込みの途中の失敗や、結果を記録できなかった実行の後に、古い成功の行で写さないため）。条件を加えただけで、承認された方式（Marker の Lock、成功の後だけ写す）は変えていない。
- 承認前は、Command・Runner・Unit File はコードとして入るが、実運用の Server で Timer を有効化（`systemctl enable --now paw-memory-projection.timer`）しないとしていた。
承認されたので、運用者が出力先の Directory（`install -d -o paw -g paw -m 0700 /srv/personal-ai/memory` など）と環境 File を用意し、Unit File を配備して Timer を有効にする。
方針を変える場合は、この Decision を書き換えず、新しい Decision から `Supersedes` する。
