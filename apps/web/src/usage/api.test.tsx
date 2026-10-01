import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { isApiError } from "../api/client";
import { apiError, mockApi, Providers, reply, session } from "../test/helpers";
import { apiUsageSource } from "./api";
import { UsageSourceProvider } from "./model";

afterEach(() => {
  vi.unstubAllGlobals();
});

// GET /api/v1/usage as the Backend answers it (issue #187): the screen's fields,
// plus some it does not read (scope, range, the period's instants, a user's status).
function backendReport(overrides: Record<string, unknown> = {}) {
  const day = (date: string, codex: number, claude: number) => ({
    date,
    local: 0,
    codex,
    claude,
  });
  return {
    scope: "workspace",
    range: "last14",
    window_start: "2026-09-03T15:00:00Z",
    window_end: "2026-09-17T15:00:00Z",
    tasks: 5,
    previous_tasks: 2,
    tokens: { local: null, external: 12_400 },
    gpu_seconds: null,
    escalations: null,
    daily: [
      ...Array.from({ length: 13 }, (_, index) =>
        day(`2026-09-${String(4 + index).padStart(2, "0")}`, 0, 0),
      ),
      day("2026-09-17", 3, 2),
    ],
    agents: [
      { agent: "codex", tasks: 3, tokens: 10_000 },
      { agent: "claude", tasks: 2, tokens: 2_400 },
    ],
    purposes: [{ purpose: "coding", tasks: 5, tokens: 12_400 }],
    quotas: [
      {
        kind: "codex",
        metric: "tasks",
        period: "month",
        limit: 20,
        used: 5,
        window_start: "2026-08-31T15:00:00Z",
        window_end: "2026-09-30T15:00:00Z",
      },
    ],
    users: [
      {
        user_id: "u-1",
        login_name: "tomoki",
        system_role: "owner",
        status: "active",
        tasks: 5,
        tokens: 12_400,
        quotas: [],
      },
    ],
    ...overrides,
  };
}

describe("apiUsageSource", () => {
  it("asks GET /usage for the scope and the period", async () => {
    const { calls } = mockApi({ "GET /usage": reply(200, backendReport()) });

    const report = await apiUsageSource.load("workspace", "month");

    expect(calls).toHaveLength(1);
    const url = new URL(String(vi.mocked(fetch).mock.calls[0]?.[0]), "http://localhost");
    expect(url.pathname).toBe("/api/v1/usage");
    expect(Object.fromEntries(url.searchParams)).toEqual({ scope: "workspace", range: "month" });
    expect(calls[0]?.init.credentials).toBe("same-origin");
    expect(report.tasks).toBe(5);
    expect(report.tokens).toEqual({ local: null, external: 12_400 });
    expect(report.daily).toHaveLength(14);
    expect(report.quotas[0]?.limit).toBe(20);
  });

  it("keeps only the fields the screen reads", async () => {
    mockApi({ "GET /usage": reply(200, backendReport()) });

    const report = await apiUsageSource.load("self", "last14");

    expect(report).not.toHaveProperty("scope");
    expect(report).not.toHaveProperty("window_start");
    expect(report.users[0]).not.toHaveProperty("status");
    expect(Object.keys(report).sort()).toEqual(
      [
        "agents",
        "daily",
        "escalations",
        "gpu_seconds",
        "previous_tasks",
        "purposes",
        "quotas",
        "tasks",
        "tokens",
        "users",
      ].sort(),
    );
  });

  it("passes the Backend's refusal on", async () => {
    mockApi({ "GET /usage": apiError(403, "forbidden") });

    const error = await apiUsageSource.load("workspace", "last14").catch((caught) => caught);

    expect(isApiError(error, "forbidden")).toBe(true);
  });

  it("shows the Backend's numbers on the usage screen", async () => {
    mockApi({
      "GET /auth/session": reply(200, session({ role: "owner" })),
      "GET /usage": reply(200, backendReport({ scope: "self", users: [] })),
    });
    window.history.replaceState(null, "", "/admin/usage");
    render(
      <Providers>
        <UsageSourceProvider source={apiUsageSource}>
          <App />
        </UsageSourceProvider>
      </Providers>,
    );

    expect(await screen.findByText("前の 14 日比 +3")).toBeVisible();
    expect(screen.getByText("Local — · 外部 12.4K")).toBeVisible();
    expect(screen.getAllByText("記録なし")).toHaveLength(2);
  });
});
