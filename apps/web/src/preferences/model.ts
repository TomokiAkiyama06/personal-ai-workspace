// The rules the preference screens apply to the Backend's answers (issue #38).
// Pure functions: what to ask in the chat, the order of the lists, the Memory
// badge, and where each candidate lives on screen. Nothing here decides whether
// a candidate may be asked or saved: `ready`, the options and the risk come from
// the Backend (Decision 0081); this only orders and labels them.
import type { ScopeTree } from "../memory/types";
import type { PreferenceCandidate, ScopeOption, TargetScope } from "./types";

/** Observations that make a candidate ready (the Backend's MIN_REPEATS, shown only). */
export const MIN_REPEATS = 3;

export const CANDIDATES_PATH = "/memory/candidates";
export const HELD_PATH = "/memory/held";

export type PreferenceView = "candidates" | "held";

/** A candidate's id on screen: the memory, or the held item of a journal entry. */
export function candidateId(candidate: PreferenceCandidate): string {
  return candidate.kind === "memory"
    ? (candidate.memory_id ?? candidate.key)
    : `${candidate.entry_id ?? ""}.${candidate.item_index ?? 0}`;
}

export function candidatePath(candidate: PreferenceCandidate): string {
  const base = candidate.kind === "memory" ? CANDIDATES_PATH : HELD_PATH;
  return `${base}/${encodeURIComponent(candidateId(candidate))}`;
}

/** /memory/candidates[/<id>] and /memory/held[/<id>]; null for any other path. */
export function preferencePath(path: string): { view: PreferenceView; id: string | null } | null {
  for (const [view, base] of [
    ["candidates", CANDIDATES_PATH],
    ["held", HELD_PATH],
  ] as const) {
    if (path === base || path === `${base}/`) return { view, id: null };
    if (path.startsWith(`${base}/`)) {
      const rest = path.slice(base.length + 1).replace(/\/$/, "");
      try {
        return { view, id: decodeURIComponent(rest) || null };
      } catch {
        return { view, id: null };
      }
    }
  }
  return null;
}

/**
 * Whether saving needs the explicit acknowledgement (Decision 0081 point 10):
 * a high-risk candidate, or one held for being high risk. The Backend checks it
 * again on every confirmation (409 `preference_high_risk_unacknowledged`).
 */
export function needsAcknowledgement(candidate: PreferenceCandidate): boolean {
  return candidate.evidence.risk_level === "high" || candidate.held_reason === "held_high_risk";
}

function observedTime(candidate: PreferenceCandidate): number {
  const time = Date.parse(candidate.observed_at);
  return Number.isNaN(time) ? 0 : time;
}

/**
 * Strongest first: the person's standing words ("今後は…"), then the most
 * observations, then the newest observation.
 */
export function byStrength(a: PreferenceCandidate, b: PreferenceCandidate): number {
  const standing =
    Number(b.evidence.language_strength === "standing") -
    Number(a.evidence.language_strength === "standing");
  if (standing !== 0) return standing;
  if (a.evidence.frequency !== b.evidence.frequency) {
    return b.evidence.frequency - a.evidence.frequency;
  }
  return observedTime(b) - observedTime(a);
}

/**
 * The candidate the chat asks about after a reply (the human's choice: at most
 * one card per assistant reply, the strongest ready candidate first; the rest
 * wait in Memory). `skip`: the ones dismissed (×) in this conversation.
 */
export function promptCandidate(
  candidates: readonly PreferenceCandidate[],
  skip: ReadonlySet<string>,
): PreferenceCandidate | null {
  const ready = candidates.filter((item) => item.ready && !skip.has(candidateId(item)));
  return [...ready].sort(byStrength)[0] ?? null;
}

/** The Memory badge: the ready candidates and every held item (the human's choice). */
export function badgeCount(candidates: readonly PreferenceCandidate[]): number {
  return candidates.filter((item) => item.kind === "held" || item.ready).length;
}

/** Why a candidate is (not yet) asked about, for the list's chip. */
export type AskState = "ready" | "notYet" | "once" | "conflicting";

export function askState(candidate: PreferenceCandidate): AskState {
  if (candidate.ready) return "ready";
  if (candidate.evidence.consistency === "conflicting") return "conflicting";
  if (candidate.evidence.language_strength === "once") return "once";
  return "notYet";
}

const ASK_ORDER: readonly AskState[] = ["ready", "notYet", "once", "conflicting"];

/**
 * 推定の候補, "聞く準備ができた順": the ready ones (strongest first), then the ones
 * that will be asked with more observations, then the ones that are not asked.
 */
export function memoryCandidates(
  candidates: readonly PreferenceCandidate[],
): PreferenceCandidate[] {
  return candidates
    .filter((item) => item.kind === "memory")
    .sort(
      (a, b) => ASK_ORDER.indexOf(askState(a)) - ASK_ORDER.indexOf(askState(b)) || byStrength(a, b),
    );
}

/** 保留中: newest observation first (the Backend's order). */
export function heldCandidates(candidates: readonly PreferenceCandidate[]): PreferenceCandidate[] {
  return candidates
    .filter((item) => item.kind === "held")
    .sort((a, b) => observedTime(b) - observedTime(a));
}

/** Project and repository names, from the Memory scope tree (null: unknown). */
export interface Names {
  project(id: string | null): string | null;
  repo(id: string | null): string | null;
}

export function namesFrom(tree: ScopeTree | null): Names {
  const projects = new Map<string, string>();
  const repos = new Map<string, string>();
  for (const project of tree?.projects ?? []) {
    projects.set(project.project_id, project.name);
    for (const repo of project.repos) repos.set(repo.repo_id, repo.name);
  }
  return {
    project: (id) => (id ? (projects.get(id) ?? null) : null),
    repo: (id) => (id ? (repos.get(id) ?? null) : null),
  };
}

/** The option the person most likely means: the recommended one, else the first. */
export function defaultOption(candidate: PreferenceCandidate): ScopeOption | null {
  return candidate.options.find((option) => option.recommended) ?? candidate.options[0] ?? null;
}

/** The option of `scope`, if the Backend offers it. */
export function optionOf(candidate: PreferenceCandidate, scope: TargetScope): ScopeOption | null {
  return candidate.options.find((option) => option.scope === scope) ?? null;
}
