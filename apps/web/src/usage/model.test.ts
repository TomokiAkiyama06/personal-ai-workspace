import { describe, expect, it } from "vitest";
import {
  formatCount,
  formatDuration,
  formatMetric,
  headlineQuota,
  purposeTotals,
  type QuotaUsage,
  quotaLevel,
  shortDay,
  UNLIMITED,
  worstLevel,
} from "./model";

function quota(overrides: Partial<QuotaUsage> = {}): QuotaUsage {
  return {
    kind: "codex",
    metric: "tokens",
    period: "month",
    limit: 10_000_000,
    used: 3_800_000,
    window_start: "2026-09-01T00:00:00+09:00",
    window_end: "2026-10-01T00:00:00+09:00",
    ...overrides,
  };
}

describe("usage formatting", () => {
  it("writes counts the way the design does", () => {
    expect(formatCount(157)).toBe("157");
    expect(formatCount(9_999)).toBe("9999");
    expect(formatCount(12_400)).toBe("12.4K");
    expect(formatCount(9_700_000)).toBe("9.7M");
    expect(formatCount(10_000_000)).toBe("10M");
  });

  it("writes durations as 45s / 4m 28s / 11h 42m / 40h", () => {
    expect(formatDuration(45)).toBe("45s");
    expect(formatDuration(268)).toBe("4m 28s");
    expect(formatDuration(120)).toBe("2m");
    expect(formatDuration(11 * 3600 + 42 * 60 + 5)).toBe("11h 42m");
    expect(formatDuration(40 * 3600)).toBe("40h");
    expect(formatMetric("runtime_seconds", 3600)).toBe("1h");
    expect(formatMetric("requests", 1200)).toBe("1200");
  });

  it("labels a day as month/day", () => {
    expect(shortDay("2026-09-07")).toBe("9/7");
  });
});

describe("quota levels (Decision 0016)", () => {
  it("is amber from 80% and reached at the limit", () => {
    expect(quotaLevel(quota({ used: 7_900_000 }))).toBe("ok");
    expect(quotaLevel(quota({ used: 8_000_000 }))).toBe("near");
    expect(quotaLevel(quota({ used: 10_000_000 }))).toBe("reached");
    expect(quotaLevel(quota({ used: 12_000_000 }))).toBe("reached");
  });

  it("treats a limit of 0 as reached (it blocks every new task)", () => {
    expect(quotaLevel(quota({ limit: 0, used: 0 }))).toBe("reached");
  });

  it("never meters an explicit Unlimited", () => {
    expect(quotaLevel(quota({ limit: UNLIMITED, used: 99_000_000 }))).toBe("unlimited");
  });

  it("treats a user without quotas as unlimited", () => {
    expect(worstLevel([])).toBe("unlimited");
    expect(headlineQuota([])).toBeNull();
  });

  it("summarizes a user by the fullest numeric quota", () => {
    const tokens = quota({ used: 1_000_000 });
    const tasks = quota({ metric: "tasks", limit: 10, used: 9 });
    const unlimited = quota({ kind: "claude", limit: UNLIMITED });
    expect(headlineQuota([tokens, tasks, unlimited])).toBe(tasks);
    expect(worstLevel([tokens, tasks, unlimited])).toBe("near");
    expect(headlineQuota([unlimited])).toBeNull();
  });
});

describe("purpose categories", () => {
  it("keeps the closed categories and folds anything else into その他", () => {
    const totals = purposeTotals([
      { purpose: "review", tasks: 31, tokens: 2_400_000 },
      { purpose: "coding", tasks: 84, tokens: 6_200_000 },
      { purpose: "Fix the login bug in auth.py", tasks: 1, tokens: 10 },
      { purpose: "other", tasks: 2, tokens: 5 },
    ]);
    expect(totals).toEqual([
      { purpose: "coding", tasks: 84, tokens: 6_200_000 },
      { purpose: "review", tasks: 31, tokens: 2_400_000 },
      { purpose: "other", tasks: 3, tokens: 15 },
    ]);
  });
});
