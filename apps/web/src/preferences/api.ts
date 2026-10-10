// The preference screens' source in production: the Backend's
// /api/v1/memory/preferences routes (issue #38, Decision 0081). A memory
// candidate is answered at its version (`expected_version`, else 409
// `memory_version_conflict`); a held one by its journal entry and item.
import { apiRequest } from "../api/client";
import type { MemoryVersion } from "../memory/types";
import type { PreferenceSource } from "./source";
import type { PreferenceCandidate, PreferencePreview } from "./types";

const BASE = "/memory/preferences";

/** The candidate's routes and the fields that pin it (its version, for a memory). */
function target(candidate: PreferenceCandidate): { path: string; pin: Record<string, number> } {
  if (candidate.kind === "memory") {
    return {
      path: `${BASE}/memories/${encodeURIComponent(candidate.memory_id ?? "")}`,
      pin: { expected_version: candidate.version_number ?? 0 },
    };
  }
  return {
    path: `${BASE}/held/${encodeURIComponent(candidate.entry_id ?? "")}/${candidate.item_index ?? 0}`,
    pin: {},
  };
}

export const apiPreferenceSource: PreferenceSource = {
  candidates: async () => {
    const answer = await apiRequest<{ candidates: PreferenceCandidate[] }>(
      "GET",
      `${BASE}/candidates`,
    );
    return answer.candidates;
  },

  confirm: (candidate, confirmation) => {
    const { path, pin } = target(candidate);
    return apiRequest<MemoryVersion>("POST", `${path}/confirm`, { ...pin, ...confirmation });
  },

  reject: async (candidate) => {
    const { path, pin } = target(candidate);
    await apiRequest<unknown>("POST", `${path}/reject`, pin);
  },

  interpret: (candidate, text) => {
    const { path, pin } = target(candidate);
    return apiRequest<PreferencePreview>("POST", `${path}/interpret`, { ...pin, text });
  },
};
