// A usage report shaped like the design's Usage board, for tests (the Backend has
// no usage API yet). Only counts, categories and login names: no text.
import type { UsageReport } from "../usage/model";

const DAILY: [number, number, number][] = [
  [6, 3, 1],
  [8, 4, 1],
  [5, 2, 0],
  [9, 5, 1],
  [11, 6, 1],
  [4, 2, 1],
  [2, 1, 0],
  [10, 5, 1],
  [12, 7, 2],
  [9, 4, 1],
  [14, 8, 3],
  [11, 6, 1],
  [7, 3, 1],
  [5, 2, 1],
];

export function usageReport(overrides: Partial<UsageReport> = {}): UsageReport {
  return {
    tasks: 157,
    previous_tasks: 139,
    tokens: { local: 4_100_000, external: 5_600_000 },
    gpu_seconds: 11 * 3600 + 42 * 60,
    escalations: { failed: 1, loop_detected: 2 },
    daily: DAILY.map(([local, codex, claude], index) => ({
      date: `2026-09-${String(7 + index).padStart(2, "0")}`,
      local,
      codex,
      claude,
    })),
    agents: [
      { agent: "local", tasks: 113, tokens: 4_100_000 },
      { agent: "codex", tasks: 58, tokens: 3_800_000 },
      { agent: "claude", tasks: 22, tokens: 1_800_000 },
    ],
    purposes: [
      { purpose: "coding", tasks: 84, tokens: 6_200_000 },
      { purpose: "review", tasks: 31, tokens: 2_400_000 },
      { purpose: "research", tasks: 42, tokens: 1_100_000 },
    ],
    quotas: [
      {
        kind: "codex",
        metric: "tokens",
        period: "month",
        limit: 10_000_000,
        used: 3_800_000,
        window_start: "2026-08-31T15:00:00Z",
        window_end: "2026-09-30T15:00:00Z",
      },
      {
        kind: "claude",
        metric: "tokens",
        period: "month",
        limit: 6_000_000,
        used: 1_800_000,
        window_start: "2026-08-31T15:00:00Z",
        window_end: "2026-09-30T15:00:00Z",
      },
      {
        kind: "codex",
        metric: "tasks",
        period: "day",
        limit: 20,
        used: 20,
        window_start: "2026-09-19T15:00:00Z",
        window_end: "2026-09-20T15:00:00Z",
      },
      {
        kind: "claude",
        metric: "requests",
        period: "rolling_5h",
        limit: "unlimited",
        used: 48,
        window_start: "2026-09-20T01:00:00Z",
        window_end: null,
      },
    ],
    users: [
      {
        user_id: "u-1",
        login_name: "Tomoki",
        system_role: "owner",
        tasks: 157,
        tokens: 9_700_000,
        quotas: [],
      },
      {
        user_id: "u-2",
        login_name: "Reviewer A",
        system_role: "admin",
        tasks: 64,
        tokens: 3_100_000,
        quotas: [
          {
            kind: "codex",
            metric: "tokens",
            period: "month",
            limit: 10_000_000,
            used: 3_100_000,
            window_start: "2026-08-31T15:00:00Z",
            window_end: "2026-09-30T15:00:00Z",
          },
        ],
      },
      {
        user_id: "u-3",
        login_name: "Member B",
        system_role: "user",
        tasks: 38,
        tokens: 9_400_000,
        quotas: [
          {
            kind: "codex",
            metric: "tokens",
            period: "month",
            limit: 10_000_000,
            used: 9_400_000,
            window_start: "2026-08-31T15:00:00Z",
            window_end: "2026-09-30T15:00:00Z",
          },
        ],
      },
      {
        user_id: "u-4",
        login_name: "Member C",
        system_role: "user",
        tasks: 4,
        tokens: 200_000,
        quotas: [
          {
            kind: "claude",
            metric: "tokens",
            period: "month",
            limit: 5_000_000,
            used: 5_000_000,
            window_start: "2026-08-31T15:00:00Z",
            window_end: "2026-09-30T15:00:00Z",
          },
        ],
      },
    ],
    ...overrides,
  };
}
