// English for the Inferred Preference confirmation (issue #38). Every key of
// preferences.ja.ts (the type enforces it).
import type { preferencesJa } from "./preferences.ja";

export const preferencesEn: Record<keyof typeof preferencesJa, string> = {
  "pref.card.label": "Preference confirmation",
  "pref.card.question": "Remember this preference?",
  "pref.card.meta": "Seen {count} times · last {time}",
  "pref.card.later": "Ask later (the candidate stays in Memory)",
  "pref.card.laterHint": "To answer later press × — it stays in Memory › Inferred",
  "pref.card.footer":
    "Saving writes a new confirmed version you can roll back in the memory history. Memory never changes permissions, approvals or merge settings.",
  "pref.card.saved": "Saved",
  "pref.card.rejected": "Not kept",
  "pref.card.rejectedHint": "You will not be asked about it again",
  "pref.card.history": "History",
  "pref.card.version": "v{number}",

  "pref.pill.inferred": "Inferred",
  "pref.pill.highRisk": "High risk",
  "pref.pill.held": "Held",

  "pref.chip.frequency": "Seen {count} times",
  "pref.chip.onlyRepo": "Only in {repo}",
  "pref.chip.onlyProject": "Only in {project}",
  "pref.chip.projectRepos": "{count} repos of {project}",
  "pref.chip.projects": "In {count} projects",
  "pref.chip.outside": "Outside any project",
  "pref.chip.standing": "Said “from now on”",
  "pref.chip.once": "Only “this time”",
  "pref.chip.consistent": "No conflict",
  "pref.chip.changed": "Revised",
  "pref.chip.conflicting": "Conflict",
  "pref.chip.riskLow": "Low risk",
  "pref.chip.riskHigh": "High risk",
  "pref.chip.held.held_high_risk": "Held: high risk",
  "pref.chip.held.held_confirmed": "Held: differs from confirmed",
  "pref.chip.held.held_widened": "Held: widened memory",

  "pref.scope.question": "Where should it apply?",
  "pref.scope.repo": "This repo only",
  "pref.scope.project": "This project",
  "pref.scope.user": "All projects",
  "pref.scope.userTarget": "My memory",
  "pref.scope.unknownRepo": "Repo",
  "pref.scope.unknownProject": "Project",
  "pref.scope.recommended": "Recommended",
  "pref.scope.mostObserved": "{name} · seen most",

  "pref.action.reject": "Don't keep",
  "pref.action.other": "Other…",
  "pref.action.confirm": "Confirm and save",
  "pref.action.saving": "Saving…",
  "pref.action.reload": "Load the latest",

  "pref.risk.notice":
    "Anything about merging, deleting, visibility, permissions (ACL / role), credentials or external sending is never saved from an inference; it is recorded only when you confirm it here.",
  "pref.risk.saves": "Saving",
  "pref.risk.savesText": "records it in Memory as your preference; agents take it into account.",
  "pref.risk.keeps": "What does not change",
  "pref.risk.keepsText":
    "You still approve merges to main. Permissions, ACLs, approvals and the merge policy stay as they are.",
  "pref.risk.acknowledge":
    "I understand only a preference is recorded; permissions and merge approvals do not change",
  "pref.risk.hint": "Check the box to save · no passkey needed",

  "pref.error.highRisk": "What would be saved was judged high risk. Review it and check the box.",
  "pref.error.changed": "This candidate was answered elsewhere or changed with a new observation.",
  "pref.error.forbidden":
    "You cannot write memory in this project / repo. “All projects” (your memory) works.",
  "pref.error.widened":
    "A widened memory cannot move to another project. Keep it in the same project or narrow it to you.",
  "pref.error.notFound": "This candidate is gone (removed, or a repository memory).",

  "pref.other.title": "Other — say it in your own words",
  "pref.other.titleShort": "Other — your own words",
  "pref.other.back": "Back to the buttons",
  "pref.other.textLabel": "How should it be remembered?",
  "pref.other.interpret": "Structure it",
  "pref.other.reinterpret": "Structure again",
  "pref.other.interpreting": "Structuring…",
  "pref.other.preview": "Preview — not saved yet",
  "pref.other.previewShort": "Preview — not saved",
  "pref.other.riskLow": "Low risk · no confirmation",
  "pref.other.riskHigh": "High risk · confirmation needed",
  "pref.other.byRules": "Read by rules",
  "pref.other.byModel": "Read by the model",
  "pref.other.scope": "Scope",
  "pref.other.scope.project_group": "A group of projects",
  "pref.other.scope.repo": "This repo only ({name})",
  "pref.other.scope.project": "This project ({name})",
  "pref.other.scope.user": "All projects",
  "pref.other.applyTo": "Applies to",
  "pref.other.rule": "Content",
  "pref.other.exceptions": "Exceptions",
  "pref.other.addException": "+ Add exception",
  "pref.other.newException": "Exception to add",
  "pref.other.removeException": "Remove exception “{text}”",
  "pref.other.strength": "Strength",
  "pref.other.strength.default": "Default",
  "pref.other.strength.required": "Required",
  "pref.other.expires": "Expires",
  "pref.other.noExpiry": "Empty: no expiry",
  "pref.other.groupNote":
    "Project groups cannot be registered yet, so this is saved in your memory with the condition “applies to: {target}”.",
  "pref.other.requiredNote":
    "Memory cannot enforce “required”, so the text says “strength: required (memory does not enforce execution or permissions)”.",
  "pref.other.riskNote":
    "Merging, deleting, permissions and “required” are high risk. After saving you still approve merges to main, and permissions and approvals do not change.",
  "pref.other.save": "Save this",
  "pref.other.revalidate": "The backend validates it again and re-measures the risk when saving",

  "pref.memory.candidates": "Inferred",
  "pref.memory.candidatesCount": "Inferred {count}",
  "pref.memory.held": "Held",
  "pref.memory.heldCount": "Held {count}",
  "pref.memory.heldShort": "Held {count}",
  "pref.memory.all": "All",
  "pref.memory.views": "Candidates to show",
  "pref.memory.section": "To confirm",
  "pref.memory.private":
    "Only you see candidates. Until confirmed they stay in your private memory.",
  "pref.memory.synced": "Synced {time}",
  "pref.memory.readyOrder": "Ready to ask first",
  "pref.memory.readyOrderPhone": "Ready to ask first · only you see these",
  "pref.memory.newest": "Newest first",
  "pref.memory.listLabel": "Candidates",
  "pref.memory.empty": "No candidates are waiting for an answer.",
  "pref.memory.heldEmpty": "No held candidates.",
  "pref.memory.none": "Select a candidate to see its evidence and answer it.",
  "pref.memory.unavailable": "Could not read the candidates.",
  "pref.memory.answer": "Answer",
  "pref.memory.notFound": "This candidate is gone: answered, or it changed.",

  "pref.ask.ready": "Waiting in chat",
  "pref.ask.notYet": "Not asked yet",
  "pref.ask.once": "Not asked",
  "pref.ask.conflicting": "Conflict",
  "pref.meta.ready": "Seen {count} · recommended {scope}",
  "pref.meta.notYet": "Seen {count} · asked after {left} more",
  "pref.meta.once": "Only “this time”",
  "pref.meta.conflicting": "Not asked: conflicts with a confirmed memory or other observations",
  "pref.meta.held.held_high_risk": "Seen {count} · explicit confirmation needed",
  "pref.meta.held.held_confirmed": "Differs from a confirmed memory",
  "pref.meta.held.held_widened": "A change to a widened memory",

  "pref.held.tag.held_high_risk": "Held: high risk",
  "pref.held.tag.held_confirmed": "Differs from confirmed",
  "pref.held.tag.held_widened": "Widened memory",
  "pref.held.note.held_high_risk":
    "This was inferred from a conversation but is about merging, deleting or permissions, so it was held instead of written to memory. It is not passed to the AI as an inference either.",
  "pref.held.note.held_confirmed":
    "Held because it differs from a confirmed memory. Saving writes the next version of that memory.",
  "pref.held.note.held_widened":
    "Held because it changes a memory you widened. Keep it in the same project or narrow it to you.",
  "pref.held.retired": "This memory was already replaced; only “Don't keep” is left.",
  "pref.held.rejectHint":
    "“Don't keep” answers this held item. A new observation that is held is asked again",
  "pref.held.kinds": "Kinds of held items",
  "pref.held.kind.held_high_risk": "High risk",
  "pref.held.kind.held_high_riskText": " — saved only after the explicit check",
  "pref.held.kind.held_confirmed": "Differs from confirmed",
  "pref.held.kind.held_confirmedText": " — saving writes the confirmed memory's next version",
  "pref.held.kind.held_widened": "Widened memory",
  "pref.held.kind.held_widenedText": " — keep it in the same project or narrow it to you",
  "pref.held.kind.retired": "Held for a replaced memory — “Don't keep” only",
  "pref.held.lastObserved": "Last seen {time}",

  "pref.detail.scopeUser": "Only me",
  "pref.detail.unconfirmed": "Unconfirmed · v{number}",
  "pref.detail.openHistory": "Open the memory history",
  "pref.detail.answerHint": "Answering here removes the card from the chat",
  "pref.detail.rejectNote":
    "“Don't keep” means you are not asked about it again (a rejected version stays in the history and can be restored).",
  "pref.detail.tabs": "Candidate views",
  "pref.detail.tab.evidence": "Evidence",
  "pref.detail.tab.body": "Text",
  "pref.detail.tab.history": "History",
  "pref.detail.tab.sources": "Sources",
  "pref.detail.historyText":
    "This candidate is an unconfirmed version. Its versions and relations are in the memory history.",

  "pref.evidence.frequency": "Count",
  "pref.evidence.frequencyValue": "{count} times",
  "pref.evidence.frequencyText": "Conversations that named it. Asked from {min} times.",
  "pref.evidence.scope": "Spread",
  "pref.evidence.scopeValue": "Project {projects} · Repo {repos}",
  "pref.evidence.scopeText": "Outside projects: {outside}. {why}",
  "pref.evidence.why.repo": "Seen in one repo only, so “This repo only” is recommended.",
  "pref.evidence.why.project": "Several repos of one project, so “This project” is recommended.",
  "pref.evidence.why.user":
    "Several projects or outside any project, so “All projects” is recommended.",
  "pref.evidence.language": "Wording",
  "pref.evidence.language.standing": "Keep doing it",
  "pref.evidence.language.neutral": "Nothing special",
  "pref.evidence.language.once": "This time only",
  "pref.evidence.languageText":
    "Counts words like “from now on” or “this time”. The messages are not shown here.",
  "pref.evidence.consistency": "Consistency",
  "pref.evidence.consistency.consistent": "Consistent",
  "pref.evidence.consistency.changed": "Revised",
  "pref.evidence.consistency.conflicting": "Conflict",
  "pref.evidence.consistencyText.consistent":
    "Never revised and no conflict with a confirmed memory.",
  "pref.evidence.consistencyText.changed": "Revised with new observations; not a conflict.",
  "pref.evidence.consistencyText.conflicting":
    "Conflicts with a confirmed memory or another candidate; not asked in the chat meanwhile.",
  "pref.evidence.risk": "Risk",
  "pref.evidence.risk.low": "Low",
  "pref.evidence.risk.high": "High",
  "pref.evidence.riskText.low":
    "No words about merging, deleting, visibility, permissions, credentials or external sending.",
  "pref.evidence.riskText.high":
    "About merging, deleting, visibility, permissions, credentials or external sending. Saving needs an explicit confirmation.",
  "pref.evidence.last": "Last seen",
  "pref.evidence.lastText": "When a conversation last named it.",
  "pref.evidence.none": "—",
  "pref.evidence.ask": "Reason to ask",
  "pref.evidence.stop": "Reason not to ask",

  "pref.nav.waiting": " preference confirmations waiting",

  "pref.chat.unavailable":
    "The chat screen comes in a later issue. Inferred preferences can be answered here and in Memory › Inferred.",

  "error.preference_candidate_changed":
    "This candidate was answered elsewhere or changed. Load the latest.",
  "error.preference_high_risk_unacknowledged":
    "Confirm a high-risk preference explicitly before saving it.",
};
