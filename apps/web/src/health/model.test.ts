import { describe, expect, it } from "vitest";
import { translate } from "../i18n";
import { abnormalReport, normalReport } from "../test/healthFixture";
import {
  abnormal,
  chartStepSeconds,
  groupOf,
  groupSeverity,
  initialGroup,
  percentPoints,
  queueOf,
  vramOf,
  worstSeverity,
} from "./model";
import { agoText, reasonText, statusText } from "./text";

const t = (key: Parameters<typeof translate>[1], params?: Record<string, string | number>) =>
  translate("ja", key, params);
const GB = 1024 ** 3;

describe("System Health model", () => {
  it("orders severities like the Notification Policy", () => {
    expect(worstSeverity([])).toBe("info");
    expect(worstSeverity(["warning", "critical", "error"])).toBe("critical");
    expect(worstSeverity(["info", "unknown"])).toBe("info");
  });

  it("groups the components into the board's areas", () => {
    expect(groupOf("compute")).toBe("gpu");
    expect(groupOf("memory_worker")).toBe("queue");
    expect(groupOf("audit_retention")).toBe("recovery");
    expect(groupOf("connection_reaper")).toBe("external");
    expect(groupOf("something_new")).toBeNull();
  });

  it("opens the worst area first, GPU / VRAM when everything is normal", () => {
    expect(initialGroup(normalReport())).toBe("gpu");
    const report = abnormalReport();
    expect(abnormal(report).map((entry) => entry.component)).toEqual([
      "recovery_backup",
      "compute",
    ]);
    expect(initialGroup(report)).toBe("recovery");
    expect(groupSeverity(report, "recovery")).toBe("error");
    expect(groupSeverity(report, "database")).toBe("info");
  });

  it("reads VRAM and the queue from the metrics, or nothing", () => {
    const compute = normalReport().components.find((entry) => entry.component === "compute");
    expect(vramOf(compute ?? null)).toEqual({
      total: 48 * GB,
      used: 18.2 * GB,
      reserved: 21 * GB,
      available: 22 * GB,
    });
    expect(
      vramOf({
        component: "compute",
        severity: "info",
        status: "not_configured",
        reasons: [],
        metrics: {},
        parts: [],
      }),
    ).toBeNull();
    const tasks = normalReport().components.find((entry) => entry.component === "task_queue");
    expect(queueOf(tasks ?? null)).toEqual({
      running: 2,
      queued: 1,
      waiting: 1,
      waitingResource: 1,
    });
  });

  it("joins the series into percentages, with gaps where nothing was recorded", () => {
    const point = (bucket_start: string, mean: number) => ({
      bucket_start,
      mean,
      min: mean,
      max: mean,
    });
    const points = percentPoints({
      gpu: [point("2026-10-07T04:00:00Z", 40), point("2026-10-07T05:00:00Z", 55)],
      used: [point("2026-10-07T04:00:00Z", 12 * GB), point("2026-10-07T06:00:00Z", 24 * GB)],
      reserved: [],
      // The total of the first bucket is used for later buckets without one.
      total: [point("2026-10-07T04:00:00Z", 48 * GB)],
    });
    expect(points).toEqual([
      { at: "2026-10-07T04:00:00.000Z", gpu: 40, vramUsed: 25, vramReserved: null },
      { at: "2026-10-07T05:00:00.000Z", gpu: 55, vramUsed: null, vramReserved: null },
      { at: "2026-10-07T06:00:00.000Z", gpu: null, vramUsed: 50, vramReserved: null },
    ]);
    expect(percentPoints({ gpu: [], used: [], reserved: [], total: [] })).toEqual([]);
  });

  it("asks for about 120 points per period, never finer than 10 seconds", () => {
    expect(chartStepSeconds("last1h")).toBe(30);
    expect(chartStepSeconds("last24h")).toBe(720);
    expect(chartStepSeconds("last7d")).toBe(5040);
  });
});

describe("System Health words", () => {
  it("words reason codes, with their argument", () => {
    expect(reasonText(t, "vram_pressure")).toBe("VRAM が逼迫しています");
    expect(reasonText(t, "expired:claude")).toBe("Claude の認証が期限切れです");
    expect(reasonText(t, "model_failed:memory_worker")).toBe(
      "Memory Worker のモデルの操作が失敗しました",
    );
    // A code this version does not know is shown as it is.
    expect(reasonText(t, "brand_new_reason")).toBe("brand_new_reason");
    expect(reasonText(t, "toString")).toBe("toString");
  });

  it("words statuses and ages", () => {
    expect(statusText(t, "stale")).toBe("更新なし");
    expect(statusText(t, "mystery")).toBe("mystery");
    expect(agoText(t, 45)).toBe("45 秒前");
    expect(agoText(t, 25_200)).toBe("7 時間前");
    expect(agoText(t, null)).toBe("記録なし");
  });
});
