import { afterEach, describe, expect, it, vi } from "vitest";
import { mockApi, reply } from "../test/helpers";
import { apiHealthSource } from "./api";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("apiHealthSource", () => {
  it("reads the report and keeps only the fields the screen shows", async () => {
    const { calls } = mockApi({
      "GET /system/health": reply(200, {
        severity: "warning",
        checked_at: "2026-10-07T05:31:48Z",
        extra: "not shown",
        components: [
          {
            component: "compute",
            severity: "warning",
            status: "degraded",
            reasons: ["vram_pressure"],
            metrics: { utilization_percent: 38, nested: { no: 1 }, list: [1], mode: "normal" },
            parts: [{ name: "main", role: "main", state: "gpu", pid: { no: 1 } }],
            message: "not shown",
          },
        ],
      }),
    });
    const report = await apiHealthSource.report();
    expect(calls[0]).toMatchObject({ method: "GET", path: "/system/health" });
    expect(report).toEqual({
      severity: "warning",
      checked_at: "2026-10-07T05:31:48Z",
      components: [
        {
          component: "compute",
          severity: "warning",
          status: "degraded",
          reasons: ["vram_pressure"],
          metrics: { utilization_percent: 38, mode: "normal" },
          parts: [{ name: "main", role: "main", state: "gpu" }],
        },
      ],
    });
  });

  it("reads the summary for every user", async () => {
    mockApi({
      "GET /system/health/summary": reply(200, {
        severity: "info",
        checked_at: "2026-10-07T05:31:48Z",
        connections: { codex: "available", claude: "unavailable" },
      }),
    });
    expect(await apiHealthSource.summary()).toEqual({
      severity: "info",
      checked_at: "2026-10-07T05:31:48Z",
      connections: { codex: "available", claude: "unavailable" },
    });
  });

  it("asks for a series with its period and step, and the events since a time", async () => {
    const { fetchMock } = mockApi({
      "GET /system/health/metrics/compute.utilization_percent": reply(200, {
        metric: "compute.utilization_percent",
        points: [{ bucket_start: "2026-10-07T04:00:00Z", count: 3, mean: 40, min: 35, max: 45 }],
      }),
      "GET /system/health/events": reply(200, {
        events: [
          {
            id: 1,
            occurred_at: "2026-10-07T04:00:00Z",
            component: "database",
            severity: "critical",
            previous_severity: "info",
            status: "unavailable",
            reasons: ["database_unavailable"],
          },
        ],
      }),
    });
    const since = new Date("2026-10-06T05:00:00Z");
    const until = new Date("2026-10-07T05:00:00Z");
    expect(await apiHealthSource.series("compute.utilization_percent", since, until, 720)).toEqual([
      { bucket_start: "2026-10-07T04:00:00Z", mean: 40, min: 35, max: 45 },
    ]);
    const seriesUrl = new URL(String(fetchMock.mock.calls[0]?.[0]), "http://localhost");
    expect(Object.fromEntries(seriesUrl.searchParams)).toEqual({
      since: "2026-10-06T05:00:00.000Z",
      until: "2026-10-07T05:00:00.000Z",
      step_seconds: "720",
    });
    const events = await apiHealthSource.events(since);
    expect(events[0]?.reasons).toEqual(["database_unavailable"]);
    const eventsUrl = new URL(String(fetchMock.mock.calls[1]?.[0]), "http://localhost");
    expect(eventsUrl.searchParams.get("since")).toBe("2026-10-06T05:00:00.000Z");
  });
});
