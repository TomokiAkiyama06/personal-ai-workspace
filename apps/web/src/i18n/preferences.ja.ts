// 推定された好みの確認（Issue #38、PAW-044）の文言。ja.ts に展開する（キーの正本は ja.ts の型）。
// 文言は Design canvas の「F. 推定された好みの確認」（P1008Pref*）に合わせる。
// API の Enum の名前（frequency・standing・default など）は画面に出さない（Human の決定）。
export const preferencesJa = {
  "pref.card.label": "好みの確認",
  "pref.card.question": "この好みを覚えますか",
  "pref.card.meta": "観測 {count} 回 · 最終 {time}",
  "pref.card.later": "あとで聞く（候補はメモリに残ります）",
  "pref.card.laterHint": "あとで答える場合は × — メモリ › 推定の候補に残ります",
  "pref.card.footer":
    "保存すると確定した新しい版になり、メモリの履歴から戻せます。メモリは権限・承認・Merge の設定を変えません。",
  "pref.card.saved": "保存しました",
  "pref.card.rejected": "保存しないにしました",
  "pref.card.rejectedHint": "同じ内容は今後聞きません",
  "pref.card.history": "履歴",
  "pref.card.version": "v{number}",

  "pref.pill.inferred": "推定",
  "pref.pill.highRisk": "高リスク",
  "pref.pill.held": "保留",

  "pref.chip.frequency": "観測 {count} 回",
  "pref.chip.onlyRepo": "{repo} だけで観測",
  "pref.chip.onlyProject": "{project} だけで観測",
  "pref.chip.projectRepos": "{project} の {count} Repo",
  "pref.chip.projects": "{count} Project で観測",
  "pref.chip.outside": "Project の外で観測",
  "pref.chip.standing": "「今後」などの発言あり",
  "pref.chip.once": "「今回だけ」の発言のみ",
  "pref.chip.consistent": "食い違いなし",
  "pref.chip.changed": "内容が改められた",
  "pref.chip.conflicting": "食い違い",
  "pref.chip.riskLow": "リスク 低",
  "pref.chip.riskHigh": "リスク 高",
  "pref.chip.held.held_high_risk": "保留: 高リスク",
  "pref.chip.held.held_confirmed": "保留: 確定済みと違う",
  "pref.chip.held.held_widened": "保留: 広げたメモリ",

  "pref.scope.question": "どこで使いますか",
  "pref.scope.repo": "このRepoだけ",
  "pref.scope.project": "このProject",
  "pref.scope.user": "すべてのProject",
  "pref.scope.userTarget": "自分のメモリ",
  "pref.scope.unknownRepo": "Repo",
  "pref.scope.unknownProject": "Project",
  "pref.scope.recommended": "推奨",
  "pref.scope.mostObserved": "{name} · 最も多く観測",

  "pref.action.reject": "保存しない",
  "pref.action.other": "その他…",
  "pref.action.confirm": "確認して保存",
  "pref.action.saving": "保存しています…",
  "pref.action.reload": "最新を読み込む",

  "pref.risk.notice":
    "Merge・削除・公開範囲・権限（ACL / Role）・Credential・外部送信にかかわる内容は、推定だけでは保存せず、ここで明示的に確認したときだけ記録します。",
  "pref.risk.saves": "保存すると",
  "pref.risk.savesText": "あなたの好みとしてメモリに記録され、エージェントが参考にします。",
  "pref.risk.keeps": "変わらないこと",
  "pref.risk.keepsText":
    "main への Merge は引き続きあなたが承認します。権限・ACL・承認・Merge Policy は変わりません。",
  "pref.risk.acknowledge":
    "記録されるのは好みだけで、権限や Merge の承認は変わらないことを確認しました",
  "pref.risk.hint": "チェックするまで保存できません · Passkey は不要",

  "pref.error.highRisk":
    "保存する内容が高リスクと判定されました。内容を確かめ、チェックしてから保存してください。",
  "pref.error.changed": "この候補は別の画面で答えたか、新しい観測で内容が変わりました。",
  "pref.error.forbidden":
    "この Project / Repo にはメモリを書けません。「すべてのProject」（自分のメモリ）なら保存できます。",
  "pref.error.widened":
    "広げたメモリは別の Project に移せません。同じ Project に留めるか、自分だけに狭めてください。",
  "pref.error.notFound": "この候補は見つかりません（消えたか、Repo のメモリです）。",

  "pref.other.title": "その他 — 自分の言葉で決める",
  "pref.other.titleShort": "その他 — 自分の言葉で",
  "pref.other.back": "候補のボタンに戻る",
  "pref.other.textLabel": "どう覚えてほしいか",
  "pref.other.interpret": "構造にする",
  "pref.other.reinterpret": "もう一度構造にする",
  "pref.other.interpreting": "構造にしています…",
  "pref.other.preview": "プレビュー — まだ保存していません",
  "pref.other.previewShort": "プレビュー — 未保存",
  "pref.other.riskLow": "リスク 低 · 確認不要",
  "pref.other.riskHigh": "リスク 高 · 確認が必要",
  "pref.other.byRules": "規則で解釈",
  "pref.other.byModel": "モデルで解釈",
  "pref.other.scope": "範囲",
  "pref.other.scope.project_group": "Project のグループ",
  "pref.other.scope.repo": "このRepoだけ（{name}）",
  "pref.other.scope.project": "このProject（{name}）",
  "pref.other.scope.user": "すべてのProject",
  "pref.other.applyTo": "適用対象",
  "pref.other.rule": "内容",
  "pref.other.exceptions": "例外",
  "pref.other.addException": "+ 例外を追加",
  "pref.other.newException": "追加する例外",
  "pref.other.removeException": "例外「{text}」を外す",
  "pref.other.strength": "強さ",
  "pref.other.strength.default": "基本",
  "pref.other.strength.required": "必須",
  "pref.other.expires": "期限",
  "pref.other.noExpiry": "空なら期限なし",
  "pref.other.groupNote":
    "Project のグループはまだ登録できないため、自分のメモリに「適用対象: {target}」と条件を書いて保存します。",
  "pref.other.requiredNote":
    "「必須」はメモリでは強制できないため、本文に「強さ: 必須（メモリは実行や権限を強制しない）」と書いて保存します。",
  "pref.other.riskNote":
    "Merge・削除・権限にかかわる内容や「必須」は高リスクです。保存しても main への Merge は引き続きあなたが承認し、権限・承認の設定は変わりません。",
  "pref.other.save": "この内容で保存",
  "pref.other.revalidate": "保存のときに Backend がもう一度検証し、リスクを計り直します",

  "pref.memory.candidates": "推定の候補",
  "pref.memory.candidatesCount": "推定の候補 {count}",
  "pref.memory.held": "保留中",
  "pref.memory.heldCount": "保留中 {count}",
  "pref.memory.heldShort": "保留 {count}",
  "pref.memory.all": "すべて",
  "pref.memory.views": "表示する候補",
  "pref.memory.section": "確認",
  "pref.memory.private":
    "候補はあなたにだけ見えます。確定するまで自分のメモリ（Private）に置かれます。",
  "pref.memory.synced": "同期済み {time}",
  "pref.memory.readyOrder": "聞く準備ができた順",
  "pref.memory.readyOrderPhone": "聞く準備ができた順 · あなたにだけ見えます",
  "pref.memory.newest": "新しい順",
  "pref.memory.listLabel": "候補の一覧",
  "pref.memory.empty": "答えを待っている候補はありません。",
  "pref.memory.heldEmpty": "保留中の候補はありません。",
  "pref.memory.none": "候補を選ぶと、根拠と答えるボタンが表示されます。",
  "pref.memory.unavailable": "候補を読めませんでした。",
  "pref.memory.answer": "答える",
  "pref.memory.notFound": "この候補はもうありません。答え済みか、内容が変わりました。",

  "pref.ask.ready": "チャットで確認待ち",
  "pref.ask.notYet": "まだ聞かない",
  "pref.ask.once": "聞かない",
  "pref.ask.conflicting": "食い違い",
  "pref.meta.ready": "観測 {count} · 推奨 {scope}",
  "pref.meta.notYet": "観測 {count} · あと {left} 回で確認",
  "pref.meta.once": "「今回だけ」の発言のみ",
  "pref.meta.conflicting": "確定済みのメモリや他の観測と食い違うため聞かない",
  "pref.meta.held.held_high_risk": "観測 {count} · 明示の確認が必要",
  "pref.meta.held.held_confirmed": "確定済みのメモリと違う内容",
  "pref.meta.held.held_widened": "広げたメモリへの変更",

  "pref.held.tag.held_high_risk": "高リスクで保留",
  "pref.held.tag.held_confirmed": "確定済みと違う",
  "pref.held.tag.held_widened": "広げたメモリ",
  "pref.held.note.held_high_risk":
    "この候補は会話から推定されましたが、Merge・削除・権限などにかかわるためメモリに書かれず保留されています。推定のまま AI に渡ることもありません。",
  "pref.held.note.held_confirmed":
    "確定済みのメモリと違う内容のため保留されています。保存すると確定済みのメモリの次の版になります。",
  "pref.held.note.held_widened":
    "あなたが広げたメモリへの変更のため保留されています。同じ Project に留めるか、自分だけに狭めて保存します。",
  "pref.held.retired": "このメモリはすでに置き換えられているため、「保存しない」だけを選べます。",
  "pref.held.rejectHint":
    "保存しないを選ぶと、同じ保留には答え済みになります。新しい観測で保留されたら、また聞きます",
  "pref.held.kinds": "保留の種類とできること",
  "pref.held.kind.held_high_risk": "高リスク",
  "pref.held.kind.held_high_riskText": " — チェックで明示確認した後だけ保存",
  "pref.held.kind.held_confirmed": "確定済みと違う",
  "pref.held.kind.held_confirmedText": " — 保存すると確定済みの次の版になります",
  "pref.held.kind.held_widened": "広げたメモリ",
  "pref.held.kind.held_widenedText": " — 同じ Project に留めるか、自分だけに狭めるだけ",
  "pref.held.kind.retired": "置き換え済みのメモリへの保留 — 「保存しない」だけ",
  "pref.held.lastObserved": "最後の観測 {time}",

  "pref.detail.scopeUser": "自分だけ",
  "pref.detail.unconfirmed": "未確認 · v{number}",
  "pref.detail.openHistory": "メモリの履歴を開く",
  "pref.detail.answerHint": "ここで答えると、チャットの確認カードは出なくなります",
  "pref.detail.rejectNote":
    "「保存しない」を選ぶと、同じ内容は今後聞きません（保存しないと判断した版として履歴に残り、履歴から復元できます）。",
  "pref.detail.tabs": "候補の表示",
  "pref.detail.tab.evidence": "根拠",
  "pref.detail.tab.body": "本文",
  "pref.detail.tab.history": "履歴",
  "pref.detail.tab.sources": "ソース",
  "pref.detail.historyText":
    "この候補はまだ確定していない版です。版の履歴と関係は、メモリの画面の履歴で見られます。",

  "pref.evidence.frequency": "回数",
  "pref.evidence.frequencyValue": "{count} 回",
  "pref.evidence.frequencyText": "この候補を名指した会話の数。{min} 回以上で確認を出します。",
  "pref.evidence.scope": "範囲",
  "pref.evidence.scopeValue": "Project {projects} · Repo {repos}",
  "pref.evidence.scopeText": "Project の外の観測 {outside}。{why}",
  "pref.evidence.why.repo": "同じ Repo だけで観測したので「このRepoだけ」を推奨。",
  "pref.evidence.why.project": "同じ Project の複数 Repo なので「このProject」を推奨。",
  "pref.evidence.why.user":
    "複数の Project か Project の外で観測したので「すべてのProject」を推奨。",
  "pref.evidence.language": "言葉の強さ",
  "pref.evidence.language.standing": "続けてほしい",
  "pref.evidence.language.neutral": "特になし",
  "pref.evidence.language.once": "今回だけ",
  "pref.evidence.languageText":
    "「今後」「毎回」「今回だけ」などの語を数えます。発言の本文はここに出しません。",
  "pref.evidence.consistency": "一貫性",
  "pref.evidence.consistency.consistent": "一貫",
  "pref.evidence.consistency.changed": "改められた",
  "pref.evidence.consistency.conflicting": "食い違い",
  "pref.evidence.consistencyText.consistent":
    "内容が改められていない。確定済みのメモリと食い違わない。",
  "pref.evidence.consistencyText.changed":
    "観測のたびに内容が改められています。食い違いではありません。",
  "pref.evidence.consistencyText.conflicting":
    "確定済みのメモリか他の候補と食い違います。食い違いがある間はチャットで聞きません。",
  "pref.evidence.risk": "リスク",
  "pref.evidence.risk.low": "低",
  "pref.evidence.risk.high": "高",
  "pref.evidence.riskText.low": "Merge・削除・公開・権限・Credential・外部送信の語がない。",
  "pref.evidence.riskText.high":
    "Merge・削除・公開・権限・Credential・外部送信にかかわります。保存には明示の確認が必要です。",
  "pref.evidence.last": "最後の観測",
  "pref.evidence.lastText": "この候補を名指した最後の会話の時刻。",
  "pref.evidence.none": "—",
  "pref.evidence.ask": "確認を出す条件",
  "pref.evidence.stop": "聞かない理由",

  "pref.nav.waiting": " 件の好みの確認が答えを待っています",

  "pref.chat.unavailable":
    "チャットの画面は後続の Issue で作ります。推定された好みの確認は、ここと メモリ › 推定の候補 で答えられます。",

  "error.preference_candidate_changed":
    "この候補は別の画面で答えたか、新しい観測で内容が変わりました。最新を読み込んでください。",
  "error.preference_high_risk_unacknowledged":
    "高リスクの好みは、明示的に確認してから保存してください。",
} as const;
