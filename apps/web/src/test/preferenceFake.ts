// An in-memory PreferenceSource for tests (and the screenshots): the design's
// example candidates (the P1008Pref* boards) and the Backend's rules in
// miniature. A memory candidate is answered at its version (else 409
// `memory_version_conflict`); a high-risk one without `acknowledge_high_risk` is
// 409 `preference_high_risk_unacknowledged`; an answered candidate leaves the list.
import { ApiError } from "../api/client";
import type { MemoryVersion } from "../memory/types";
import { candidateId } from "../preferences/model";
import type { PreferenceSource } from "../preferences/source";
import type {
  Confirmation,
  Evidence,
  PreferenceCandidate,
  PreferencePreview,
} from "../preferences/types";
import { PROJECT_ID, REPO_ID, SELF_ID, version } from "./memoryFake";

export const IOS_REPO_ID = "r-ios";

function evidence(overrides: Partial<Evidence> = {}): Evidence {
  return {
    frequency: 4,
    project_count: 1,
    repo_count: 1,
    outside_projects: 0,
    last_observed_at: "2026-10-08T05:12:00Z",
    language_strength: "neutral",
    consistency: "consistent",
    risk_level: "low",
    ...overrides,
  };
}

const repoOptions = (recommended: "repo" | "project" | "user") => [
  {
    scope: "repo" as const,
    project_id: PROJECT_ID,
    repo_id: REPO_ID,
    recommended: recommended === "repo",
  },
  {
    scope: "project" as const,
    project_id: PROJECT_ID,
    repo_id: null,
    recommended: recommended === "project",
  },
  { scope: "user" as const, project_id: null, repo_id: null, recommended: recommended === "user" },
];

export function memoryCandidate(
  overrides: Partial<PreferenceCandidate> & { memory_id: string },
): PreferenceCandidate {
  return {
    kind: "memory",
    key: overrides.memory_id,
    title: "Preference",
    content: "Text",
    evidence: evidence(),
    recommendation: { scope: "repo", project_id: PROJECT_ID, repo_id: REPO_ID },
    options: repoOptions("repo"),
    ready: true,
    observed_at: "2026-10-08T05:12:00Z",
    version_number: 1,
    confirmation_state: "inferred",
    entry_id: null,
    item_index: null,
    held_reason: null,
    ...overrides,
  };
}

export function heldCandidate(
  overrides: Partial<PreferenceCandidate> & { entry_id: string },
): PreferenceCandidate {
  return {
    kind: "held",
    key: overrides.entry_id,
    title: overrides.entry_id,
    content: "Text",
    evidence: evidence(),
    recommendation: { scope: "project", project_id: PROJECT_ID, repo_id: null },
    options: repoOptions("project"),
    ready: true,
    observed_at: "2026-10-08T04:05:00Z",
    memory_id: null,
    version_number: null,
    confirmation_state: null,
    item_index: 0,
    held_reason: "held_high_risk",
    ...overrides,
  };
}

/** The boards' example: five candidates and three held items. */
export function designCandidates(): PreferenceCandidate[] {
  return [
    memoryCandidate({
      memory_id: "m-pytest",
      key: "pytest.flags",
      title: "pytest は -x を付けて実行する",
      content: "テストを実行するときは pytest に -x を付け、最初の失敗で止める。",
      evidence: evidence({ frequency: 4, language_strength: "standing" }),
    }),
    memoryCandidate({
      memory_id: "m-review",
      key: "review.agents",
      title: "レビューは Codex と Claude の両方",
      content: "コードレビューは Codex と Claude Code の両方に依頼し、両方の指摘を見てから直す。",
      evidence: evidence({
        frequency: 3,
        repo_count: 2,
        language_strength: "standing",
        last_observed_at: "2026-10-08T04:40:00Z",
      }),
      recommendation: { scope: "project", project_id: PROJECT_ID, repo_id: null },
      options: repoOptions("project"),
      observed_at: "2026-10-08T04:40:00Z",
    }),
    memoryCandidate({
      memory_id: "m-ruff",
      key: "ruff.format",
      title: "コミット前に ruff format を実行する",
      content: "コミットの前に ruff format を実行する。",
      evidence: evidence({ frequency: 2 }),
      ready: false,
    }),
    memoryCandidate({
      memory_id: "m-lint-skip",
      key: "lint.skip",
      title: "lint を飛ばしてテストだけ回す",
      content: "lint を飛ばしてテストだけ回す。",
      evidence: evidence({ frequency: 1, language_strength: "once" }),
      ready: false,
    }),
    memoryCandidate({
      memory_id: "m-lint-old",
      key: "lint.config",
      title: "Lint は旧設定を使う",
      content: "Lint は旧設定のまま使う。",
      evidence: evidence({ frequency: 3, consistency: "conflicting" }),
      ready: false,
    }),
    heldCandidate({
      entry_id: "e-merge",
      key: "merge.after_ci",
      // The Backend titles a held item by its key; the statement is the content.
      title: "merge.after_ci",
      content: "CI が通った PR は確認なしで main にマージする",
      evidence: evidence({
        frequency: 3,
        repo_count: 2,
        language_strength: "standing",
        risk_level: "high",
        last_observed_at: "2026-10-08T04:05:00Z",
      }),
      ready: false,
    }),
    heldCandidate({
      entry_id: "e-lang",
      key: "ui.language",
      title: "ui.language",
      content: "UI の表示言語は英語",
      evidence: evidence({ frequency: 2, consistency: "conflicting" }),
      held_reason: "held_confirmed",
      memory_id: "m-lang",
      ready: false,
      observed_at: "2026-10-07T04:05:00Z",
    }),
    heldCandidate({
      entry_id: "e-pr",
      key: "pr.language",
      title: "pr.language",
      content: "PR の説明は英語で書く",
      evidence: evidence({ frequency: 2 }),
      held_reason: "held_widened",
      memory_id: "m-pr",
      options: [
        { scope: "project", project_id: PROJECT_ID, repo_id: null, recommended: true },
        { scope: "user", project_id: null, repo_id: null, recommended: false },
      ],
      ready: false,
      observed_at: "2026-10-06T04:05:00Z",
    }),
  ];
}

export class FakePreferenceSource implements PreferenceSource {
  items: PreferenceCandidate[];
  calls: { method: string; args: unknown[] }[] = [];
  /** The next answer of `interpret` (else a rules preview of the text). */
  preview: PreferencePreview | null = null;
  /** Fail the next write with this error. */
  failNext: ApiError | null = null;

  constructor(items: PreferenceCandidate[] = designCandidates()) {
    this.items = [...items];
  }

  /** Fail the next read of the candidates with this error. */
  failRead: ApiError | null = null;

  async candidates(): Promise<PreferenceCandidate[]> {
    this.calls.push({ method: "candidates", args: [] });
    if (this.failRead) {
      const error = this.failRead;
      this.failRead = null;
      throw error;
    }
    return [...this.items];
  }

  private take(candidate: PreferenceCandidate): void {
    if (this.failNext) {
      const error = this.failNext;
      this.failNext = null;
      throw error;
    }
    const found = this.items.find((item) => candidateId(item) === candidateId(candidate));
    if (!found) throw new ApiError(409, "preference_candidate_changed", "changed");
    if (found.kind === "memory" && found.version_number !== candidate.version_number) {
      throw new ApiError(409, "memory_version_conflict", "conflict");
    }
  }

  async confirm(
    candidate: PreferenceCandidate,
    confirmation: Confirmation,
  ): Promise<MemoryVersion> {
    this.calls.push({ method: "confirm", args: [candidateId(candidate), confirmation] });
    this.take(candidate);
    const high =
      candidate.evidence.risk_level === "high" ||
      ("preference" in confirmation && confirmation.preference.strength === "required");
    if (high && !confirmation.acknowledge_high_risk) {
      throw new ApiError(409, "preference_high_risk_unacknowledged", "unacknowledged");
    }
    this.items = this.items.filter((item) => candidateId(item) !== candidateId(candidate));
    const scope = "preference" in confirmation ? confirmation.preference.scope : confirmation.scope;
    const where = scope === "project_group" ? "user" : scope;
    const ids = "preference" in confirmation ? confirmation.preference : confirmation;
    return version({
      memory_id: candidate.memory_id ?? `m-${candidate.key}`,
      version_number: (candidate.version_number ?? 0) + 1,
      scope: where,
      owner_user_id: where === "user" ? SELF_ID : null,
      project_id: where === "user" ? null : ids.project_id,
      repo_id: where === "repo" ? ids.repo_id : null,
      memory_type: "preference",
      title: candidate.title,
      content: candidate.content,
    });
  }

  async reject(candidate: PreferenceCandidate): Promise<void> {
    this.calls.push({ method: "reject", args: [candidateId(candidate)] });
    this.take(candidate);
    this.items = this.items.filter((item) => candidateId(item) !== candidateId(candidate));
  }

  async interpret(candidate: PreferenceCandidate, text: string): Promise<PreferencePreview> {
    this.calls.push({ method: "interpret", args: [candidateId(candidate), text] });
    if (this.preview) return this.preview;
    return {
      preference: {
        scope: "project",
        project_id: PROJECT_ID,
        repo_id: null,
        apply_to: null,
        rule: candidate.content,
        exceptions: [],
        strength: "default",
        expires_at: null,
      },
      content: candidate.content,
      risk_level: "low",
      requires_acknowledgement: false,
      interpreted_by: "rules",
    };
  }
}
