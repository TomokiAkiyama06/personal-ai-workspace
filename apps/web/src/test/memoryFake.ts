// An in-memory MemorySource for tests (and the screenshots of the Memory screen):
// the Backend's versioning rules in miniature. An edit or a restore needs the
// current version number (else 409 `memory_version_conflict`), writes version
// n + 1 as active and supersedes n; a restore copies the old content.
import { ApiError } from "../api/client";
import type { MemorySource } from "../memory/source";
import type {
  MemoryHistory,
  MemoryRelation,
  MemorySourceRecord,
  MemoryVersion,
  ScopeKey,
  ScopeTree,
} from "../memory/types";

export const SELF_ID = "u-1";
export const PROJECT_ID = "p-example";
export const REPO_ID = "r-backend";

let counter = 0;
const nextId = (prefix: string) => `${prefix}-${++counter}`;

export function version(overrides: Partial<MemoryVersion> & { memory_id: string }): MemoryVersion {
  return {
    version_id: nextId("v"),
    version_number: 1,
    scope: "user",
    owner_user_id: SELF_ID,
    project_id: null,
    project_group_id: null,
    repo_id: null,
    memory_type: "policy",
    title: "Memory",
    content: "Text",
    importance: 50,
    pinned: false,
    status: "active",
    confirmation_state: "confirmed",
    freshness_policy: "permanent",
    verified_at: null,
    revalidate_after: null,
    revalidate_triggers: [],
    expires_at: null,
    commit_sha: null,
    branch: null,
    stale_since: null,
    actor_type: "user",
    actor_user_id: SELF_ID,
    actor_name: null,
    change_reason: null,
    created_at: "2026-09-18T02:20:00Z",
    ...overrides,
  };
}

const projectScope = { scope: "project" as const, owner_user_id: null, project_id: PROJECT_ID };

/** The design's example: "Merge は必ず人が承認する" and its history. */
export function designMemories() {
  const merge = "m-merge";
  const observation = "m-observation";
  const v1 = version({
    memory_id: merge,
    ...projectScope,
    version_number: 1,
    title: "Merge は Agent が判断する",
    content: "Review が APPROVE なら、エージェントが main へ Merge してよい。",
    status: "superseded",
    actor_name: "Tomoki",
    created_at: "2026-06-02T01:00:00Z",
  });
  const v2 = version({
    memory_id: merge,
    ...projectScope,
    version_number: 2,
    title: "Merge は必ず人が承認する",
    content: "main への Merge はエージェントが自動で実行しない。",
    status: "superseded",
    confirmation_state: "inferred",
    actor_type: "agent",
    actor_user_id: null,
    actor_name: "Codex",
    change_reason: "3 回の観測",
    created_at: "2026-09-04T03:00:00Z",
  });
  const v3 = version({
    memory_id: merge,
    ...projectScope,
    version_number: 3,
    title: "Merge は必ず人が承認する",
    content:
      "main への Merge はエージェントが自動で実行しない。\nユーザーがその Task / Session で明示的に許可した場合のみ Merge まで進む。",
    freshness_policy: "revalidate",
    verified_at: "2026-09-18T02:20:00Z",
    revalidate_after: 90 * 86_400,
    revalidate_triggers: ["member_changed"],
    actor_name: "Tomoki",
    change_reason: "ユーザー確認",
    created_at: "2026-09-18T02:20:00Z",
  });
  const obs = version({
    memory_id: observation,
    ...projectScope,
    version_number: 1,
    title: "Merge の前に人の確認を求めた",
    content: "Task #188 で、エージェントが Merge の前に人の確認を求めた。",
    confirmation_state: "observed",
    actor_type: "agent",
    actor_user_id: null,
    actor_name: "Codex",
    created_at: "2026-08-21T05:00:00Z",
  });
  const relations: MemoryRelation[] = [
    {
      from_version_id: v2.version_id,
      to_version_id: v1.version_id,
      relation: "supersedes",
      reason: "content",
    },
    {
      from_version_id: v2.version_id,
      to_version_id: obs.version_id,
      relation: "extends",
      reason: null,
    },
    {
      from_version_id: v3.version_id,
      to_version_id: v2.version_id,
      relation: "supersedes",
      reason: "content",
    },
    {
      from_version_id: v3.version_id,
      to_version_id: v2.version_id,
      relation: "confirmed_from",
      reason: null,
    },
  ];
  const others: MemoryVersion[] = [
    version({
      memory_id: "m-language",
      title: "UI の表示言語は日本語",
      content: "Web UI とエージェントの説明は日本語で表示する。",
      pinned: true,
      actor_name: "Tomoki",
      created_at: "2026-09-10T01:00:00Z",
    }),
    version({
      memory_id: "m-review",
      ...projectScope,
      title: "レビューは Codex と Claude の両方",
      content: "PR のレビューは Codex と Claude の両方に依頼する。",
      confirmation_state: "inferred",
      actor_type: "agent",
      actor_user_id: null,
      actor_name: "Claude",
      created_at: "2026-09-20T01:00:00Z",
    }),
    version({
      memory_id: "m-lint",
      scope: "repo",
      owner_user_id: null,
      project_id: PROJECT_ID,
      repo_id: REPO_ID,
      title: "Lint は旧設定を使う",
      content: "backend の Lint は旧設定（flake8）を使う。",
      status: "superseded",
      freshness_policy: "repo_commit",
      commit_sha: "4f2a9c1d0b7e6a5f4e3d2c1b0a9f8e7d6c5b4a39",
      branch: "main",
      actor_type: "system",
      actor_user_id: null,
      created_at: "2026-05-11T01:00:00Z",
    }),
  ];
  const sources: Record<string, MemorySourceRecord[]> = {
    [v3.version_id]: [
      {
        source_type: "conversation",
        conversation_id: "1842",
        message_id: null,
        source_ref: null,
        source_deleted_at: null,
        created_at: "2026-09-02T01:00:00Z",
      },
      {
        source_type: "task",
        conversation_id: null,
        message_id: null,
        source_ref: "188",
        source_deleted_at: null,
        created_at: "2026-08-21T05:00:00Z",
      },
      {
        source_type: "user_confirmation",
        conversation_id: null,
        message_id: null,
        source_ref: `memory_confirmed_by:${SELF_ID}`,
        source_deleted_at: null,
        created_at: "2026-09-18T02:20:00Z",
      },
    ],
    [v2.version_id]: [
      {
        source_type: "conversation",
        conversation_id: null,
        message_id: null,
        source_ref: null,
        source_deleted_at: "2026-09-12T01:00:00Z",
        created_at: "2026-09-02T01:00:00Z",
      },
    ],
  };
  return { versions: [v1, v2, v3, obs, ...others], relations, sources, mergeId: merge };
}

export const DESIGN_TREE: ScopeTree = {
  user: 8,
  projects: [
    {
      project_id: PROJECT_ID,
      name: "ExampleProject",
      count: 12,
      project_count: 2,
      repos: [
        { repo_id: REPO_ID, name: "backend", count: 7 },
        { repo_id: "r-ios", name: "ios", count: 3 },
      ],
    },
    { project_id: "p-internal", name: "InternalTools", count: 5, project_count: 5, repos: [] },
    { project_id: "p-research", name: "ResearchNotes", count: 3, project_count: 3, repos: [] },
  ],
  shared: 4,
};

export class FakeMemorySource implements MemorySource {
  versions: MemoryVersion[];
  relations: MemoryRelation[];
  sourceRows: Record<string, MemorySourceRecord[]>;
  tree: ScopeTree;
  /** Which memories each scope lists (by default: every memory's current version). */
  listed: ((scope: ScopeKey) => string[]) | null = null;
  calls: { method: string; args: unknown[] }[] = [];

  constructor(
    data: {
      versions: MemoryVersion[];
      relations?: MemoryRelation[];
      sources?: Record<string, MemorySourceRecord[]>;
    },
    tree: ScopeTree = DESIGN_TREE,
  ) {
    this.versions = [...data.versions];
    this.relations = [...(data.relations ?? [])];
    this.sourceRows = { ...(data.sources ?? {}) };
    this.tree = tree;
  }

  private current(memoryId: string): MemoryVersion {
    const own = this.versions.filter((entry) => entry.memory_id === memoryId);
    if (own.length === 0) throw new ApiError(404, "memory_not_found", "Memory not found");
    return own.reduce((max, entry) => (entry.version_number > max.version_number ? entry : max));
  }

  async scopes(): Promise<ScopeTree> {
    this.calls.push({ method: "scopes", args: [] });
    return this.tree;
  }

  async list(scope: ScopeKey, query: string): Promise<MemoryVersion[]> {
    this.calls.push({ method: "list", args: [scope, query] });
    const ids = [...new Set(this.versions.map((entry) => entry.memory_id))];
    const chosen = this.listed ? this.listed(scope) : ids;
    return chosen
      .filter((id) => ids.includes(id))
      .map((id) => this.current(id))
      .filter((entry) => !query || `${entry.title}\n${entry.content}`.includes(query));
  }

  async history(memoryId: string): Promise<MemoryHistory> {
    this.calls.push({ method: "history", args: [memoryId] });
    this.current(memoryId);
    const versions = this.versions
      .filter((entry) => entry.memory_id === memoryId)
      .sort((a, b) => a.version_number - b.version_number);
    const ids = new Set(versions.map((entry) => entry.version_id));
    const relations = this.relations.filter(
      (edge) => ids.has(edge.from_version_id) || ids.has(edge.to_version_id),
    );
    const far = new Set(
      relations
        .flatMap((edge) => [edge.from_version_id, edge.to_version_id])
        .filter((id) => !ids.has(id)),
    );
    const related = this.versions.filter((entry) => far.has(entry.version_id));
    return { versions, relations, related };
  }

  async sources(memoryId: string, versionNumber: number): Promise<MemorySourceRecord[]> {
    this.calls.push({ method: "sources", args: [memoryId, versionNumber] });
    const found = this.versions.find(
      (entry) => entry.memory_id === memoryId && entry.version_number === versionNumber,
    );
    return found ? (this.sourceRows[found.version_id] ?? []) : [];
  }

  /** Write n + 1 over the current version, as edit / restore / another writer do. */
  write(
    memoryId: string,
    changes: Partial<MemoryVersion>,
    reason: string | null,
    by: Partial<MemoryVersion> = {},
  ): MemoryVersion {
    const current = this.current(memoryId);
    const next: MemoryVersion = {
      ...current,
      ...changes,
      version_id: nextId("v"),
      version_number: current.version_number + 1,
      status: "active",
      confirmation_state: "confirmed",
      stale_since: null,
      actor_type: "user",
      actor_user_id: SELF_ID,
      actor_name: null,
      change_reason: reason,
      created_at: new Date().toISOString(),
      ...by,
    };
    if (current.status === "active") {
      this.versions = this.versions.map((entry) =>
        entry === current ? { ...entry, status: "superseded" } : entry,
      );
    }
    this.versions.push(next);
    this.relations.push({
      from_version_id: next.version_id,
      to_version_id: current.version_id,
      relation: "supersedes",
      reason,
    });
    return next;
  }

  private check(memoryId: string, expected: number) {
    const current = this.current(memoryId);
    if (current.version_number !== expected) {
      throw new ApiError(409, "memory_version_conflict", "Version conflict");
    }
    return current;
  }

  async edit(
    memoryId: string,
    expectedVersion: number,
    changes: { title?: string; content?: string; reason?: string },
  ): Promise<MemoryVersion> {
    this.calls.push({ method: "edit", args: [memoryId, expectedVersion, changes] });
    this.check(memoryId, expectedVersion);
    const { reason, ...fields } = changes;
    return this.write(memoryId, fields, reason ?? null);
  }

  async restore(
    memoryId: string,
    expectedVersion: number,
    sourceVersion: number,
  ): Promise<MemoryVersion> {
    this.calls.push({ method: "restore", args: [memoryId, expectedVersion, sourceVersion] });
    this.check(memoryId, expectedVersion);
    const from = this.versions.find(
      (entry) => entry.memory_id === memoryId && entry.version_number === sourceVersion,
    );
    if (!from) throw new ApiError(409, "memory_state_conflict", "Unknown version");
    return this.write(memoryId, { title: from.title, content: from.content }, "restore");
  }
}
