// The Memory screen's data (PAW-063). The shapes follow the Backend's Memory
// domain (paw_backend/memory: models.py, versioning/records.py) field for field,
// in the API's JSON spelling: ids are strings, times ISO 8601, a duration seconds.
// The Backend has no Memory HTTP API yet; these are what a MemorySource returns.

export type MemoryScope = "user" | "project" | "project_group" | "repo" | "shared";
export type MemoryStatus = "active" | "superseded" | "deprecated" | "history";
export type ConfirmationState = "observed" | "inferred" | "confirmed" | "rejected";
export type FreshnessPolicy =
  | "permanent"
  | "revalidate"
  | "repo_commit"
  | "expiring"
  | "session_only";
export type ActorType = "user" | "agent" | "system";
export type RelationType =
  | "supersedes"
  | "extends"
  | "conflicts_with"
  | "confirmed_from"
  | "revalidated_from"
  | "merged_from";
export type SourceType =
  | "conversation"
  | "task"
  | "repo_analysis"
  | "user_confirmation"
  | "project_decision";

/** One stored version (the Backend's MemoryVersionView). */
export interface MemoryVersion {
  memory_id: string;
  version_id: string;
  version_number: number;
  scope: MemoryScope;
  owner_user_id: string | null;
  project_id: string | null;
  project_group_id: string | null;
  repo_id: string | null;
  memory_type: string;
  title: string;
  content: string;
  importance: number;
  pinned: boolean;
  status: MemoryStatus;
  confirmation_state: ConfirmationState;
  freshness_policy: FreshnessPolicy;
  verified_at: string | null;
  /** Seconds. */
  revalidate_after: number | null;
  revalidate_triggers: string[];
  expires_at: string | null;
  commit_sha: string | null;
  branch: string | null;
  stale_since: string | null;
  actor_type: ActorType;
  actor_user_id: string | null;
  /** The person's display name, when the API resolves it (not a column). */
  actor_name?: string | null;
  change_reason: string | null;
  created_at: string;
}

/** An edge of the history graph, from the newer version to the older one. */
export interface MemoryRelation {
  from_version_id: string;
  to_version_id: string;
  relation: RelationType;
  reason: string | null;
}

/**
 * The History Graph of one memory: its versions the reader may see (oldest first,
 * as `MemoryVersioningService.history`), the relations touching them, and the
 * versions of other memories at the far end of those relations (readable ones
 * only; an edge to anything else is left out).
 */
export interface MemoryHistory {
  versions: MemoryVersion[];
  relations: MemoryRelation[];
  related: MemoryVersion[];
  /**
   * Whether the reader may change the memory (edit / restore), as the Backend
   * decides it: a project Viewer reads the history (`project.read`) but cannot
   * edit (`project.memory.use`). Absent: not known, the controls are shown and
   * the Backend's answer decides.
   */
  can_write?: boolean;
}

/** A `memory_sources` row: why a version exists (UI_DESIGN.md §8). */
export interface MemorySourceRecord {
  source_type: SourceType;
  conversation_id: string | null;
  message_id: string | null;
  source_ref: string | null;
  /** Set when the conversation / message it pointed at was deleted. */
  source_deleted_at: string | null;
  created_at: string;
}

export interface RepoScope {
  repo_id: string;
  name: string;
  count: number;
}

export interface ProjectScope {
  project_id: string;
  name: string;
  /** Every memory of the project, its repos included. */
  count: number;
  /** The project-wide memories (scope `project`). */
  project_count: number;
  repos: RepoScope[];
}

/** The Scope pane (UI_DESIGN.md §6.1): what the reader may see, with counts. */
export interface ScopeTree {
  user: number;
  projects: ProjectScope[];
  shared: number;
}

export type ScopeKey =
  | { kind: "user" }
  | { kind: "project"; project_id: string }
  | { kind: "repo"; project_id: string; repo_id: string }
  | { kind: "shared" };

/** A manual edit (the Backend's MemoryChanges; only what this screen edits). */
export interface MemoryEdit {
  title?: string;
  content?: string;
  reason?: string;
}
