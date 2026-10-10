// The Inferred Preference confirmation flow's data (issue #38, PAW-044). The
// shapes are the Backend's answers of /api/v1/memory/preferences/*
// (paw_backend/api/v1/memory_preferences.py, Decision 0081) field for field, in
// the API's JSON spelling: ids are strings, times ISO 8601.

export type CandidateKind = "memory" | "held";
export type LanguageStrength = "standing" | "neutral" | "once";
export type Consistency = "consistent" | "changed" | "conflicting";
export type RiskLevel = "low" | "high";
/** The buttons [このRepoだけ] [このProject] [すべてのProject]. */
export type TargetScope = "repo" | "project" | "user";
/** What a free-text answer (その他) may name; `project_group` is kept as a condition. */
export type InterpretedScope = TargetScope | "project_group";
export type Strength = "default" | "required";
export type HeldReason = "held_high_risk" | "held_confirmed" | "held_widened";
export type InterpretedBy = "model" | "rules";

/** The five factors of Decision 0081 point 3, as numbers and categories. */
export interface Evidence {
  frequency: number;
  project_count: number;
  repo_count: number;
  outside_projects: number;
  last_observed_at: string | null;
  language_strength: LanguageStrength;
  consistency: Consistency;
  risk_level: RiskLevel;
}

export interface ScopeOption {
  scope: TargetScope;
  project_id: string | null;
  repo_id: string | null;
  recommended: boolean;
}

export interface Recommendation {
  scope: TargetScope;
  project_id: string | null;
  repo_id: string | null;
}

/**
 * One candidate: an unconfirmed private memory (`kind = memory`: `memory_id` and
 * `version_number`), or one the consolidator held for the person (`kind = held`:
 * `entry_id`, `item_index` and `held_reason`; `memory_id` is the key's memory,
 * if any). `options` are the only buttons to offer; an empty list leaves
 * [保存しない] alone.
 */
export interface PreferenceCandidate {
  kind: CandidateKind;
  key: string;
  title: string;
  content: string;
  evidence: Evidence;
  recommendation: Recommendation;
  options: ScopeOption[];
  /** Whether the chat asks now (Decision 0081 point 3). */
  ready: boolean;
  observed_at: string;
  memory_id: string | null;
  version_number: number | null;
  confirmation_state: string | null;
  entry_id: string | null;
  item_index: number | null;
  held_reason: HeldReason | null;
}

/** The structure of a free-text answer, as previewed and as confirmed. */
export interface StructuredPreference {
  scope: InterpretedScope;
  project_id: string | null;
  repo_id: string | null;
  apply_to: string | null;
  rule: string;
  exceptions: string[];
  strength: Strength;
  expires_at: string | null;
}

/** The answer of `interpret`: nothing is written yet. */
export interface PreferencePreview {
  preference: StructuredPreference;
  /** The text the confirmation would write. */
  content: string;
  risk_level: RiskLevel;
  requires_acknowledgement: boolean;
  interpreted_by: InterpretedBy;
}

/** A confirmation: a button, or the (possibly corrected) その他 preview. */
export type Confirmation =
  | {
      scope: TargetScope;
      project_id: string | null;
      repo_id: string | null;
      acknowledge_high_risk: boolean;
    }
  | { preference: StructuredPreference; acknowledge_high_risk: boolean };
