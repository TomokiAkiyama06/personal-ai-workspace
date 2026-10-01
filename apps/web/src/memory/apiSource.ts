// The Memory screen's source in production: the Backend's /api/v1/memory routes
// (issue #186, Decision 0068). The answers already have the shapes of types.ts;
// this only builds the requests and unwraps the list and the sources. Every
// failure is the Backend's ApiError (404 `memory_not_found`, 409
// `memory_version_conflict`, ...), shown by the screen as it is.
import { apiRequest } from "../api/client";
import type { MemorySource } from "./source";
import type {
  MemoryEdit,
  MemoryHistory,
  MemorySourceRecord,
  MemoryVersion,
  ScopeKey,
  ScopeTree,
} from "./types";

const BASE = "/memory";

function memoryPath(memoryId: string): string {
  return `${BASE}/memories/${encodeURIComponent(memoryId)}`;
}

function scopeQuery(scope: ScopeKey, query: string): string {
  const params = new URLSearchParams({ scope: scope.kind });
  if (scope.kind === "project" || scope.kind === "repo") params.set("project_id", scope.project_id);
  if (scope.kind === "repo") params.set("repo_id", scope.repo_id);
  if (query.trim()) params.set("q", query);
  return params.toString();
}

export const apiMemorySource: MemorySource = {
  scopes: () => apiRequest<ScopeTree>("GET", `${BASE}/scopes`),

  list: async (scope, query) => {
    // `truncated` (more than the Backend lists at once) is not shown yet.
    const answer = await apiRequest<{ memories: MemoryVersion[]; truncated: boolean }>(
      "GET",
      `${BASE}/memories?${scopeQuery(scope, query)}`,
    );
    return answer.memories;
  },

  history: (memoryId) => apiRequest<MemoryHistory>("GET", `${memoryPath(memoryId)}/history`),

  sources: async (memoryId, versionNumber) => {
    const answer = await apiRequest<{ sources: MemorySourceRecord[] }>(
      "GET",
      `${memoryPath(memoryId)}/versions/${versionNumber}/sources`,
    );
    return answer.sources;
  },

  edit: (memoryId, expectedVersion, changes: MemoryEdit) =>
    apiRequest<MemoryVersion>("POST", `${memoryPath(memoryId)}/edit`, {
      expected_version: expectedVersion,
      ...changes,
    }),

  restore: (memoryId, expectedVersion, sourceVersion) =>
    apiRequest<MemoryVersion>("POST", `${memoryPath(memoryId)}/restore`, {
      expected_version: expectedVersion,
      source_version: sourceVersion,
    }),
};
