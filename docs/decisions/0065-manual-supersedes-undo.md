# 手動の `supersedes` を取り消すときは、古い記憶を有効に戻さず、その最後の内容で新しい版を作る

- Status: Approved
- Approval: 2026-10-01、Humanが作業Session内で、選択肢（推奨つき）の説明を受けたうえで直接回答して承認（案 B。末尾の「承認時の決定」）
- Date: 2026-10-01
- Scope: Issue [#49](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/49)（PR #178、Memory の History Graph）と、Memory の HTTP API の Issue [#186](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/186)。関連: [Decision 0034](0034-memory-versioning-freshness.md) の 8
- Supersedes: なし（Decision 0034 の 8 が History Graph の Issue に委ねた「手動の `supersedes` の取り消し方」を決める）

## 背景

Decision 0034 の 8 により、手動で付けた `supersedes` は今は取り消せず、取り消しの操作は History Graph（UI）の Issue で決めることになっていた。
PR #178（#49）の実装で、次の 2 つの案を Human に示した。

- 案 A: 関係を消し、置き換えられた古い記憶を有効（active）に戻す
- 案 B: 関係は履歴として残し、古い記憶の最後の内容で新しい版を作って有効にする

## 決定

案 B を採る。

1. 手動の `supersedes` を取り消すときは、古い版を有効に戻さない。置き換えられた記憶の最後の内容から新しい版を作り、それを有効にする。
2. 取り消しの前の `supersedes` の関係と版は、履歴として残す。
3. 復元（restore）と同じく、古い版を再び有効にしない原則に従う。
4. 実装は Memory の HTTP API（#186）と、その後の UI の Issue で行う。PR #178 では取り消しの操作を作らない。

## 選定理由

- 復元と同じ原則にそろい、履歴が一方向に進むため、History Graph と Audit で追いやすい。
- 案 A は、古い版の状態を書き換えるため、その版を前提にした関係や鮮度の判断と食い違うおそれがある。

## リスク

- 取り消すたびに版が 1 つ増える。
- 取り消しの操作そのもの（権限、Step-up の要否、理由の必須かどうか）は、実装の Issue で決める。

## 承認時の決定（2026-10-01）

Human は、作業 Session で案 A・B の説明（推奨: B）を受け、UI の確認と合わせて「推奨どおり」と直接回答した。案 B を承認した。
