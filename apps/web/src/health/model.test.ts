import { describe, expect, it } from "vitest";
import { translate } from "../i18n";
import { abnormalReport, normalReport, withUnknownComponent } from "../test/healthFixture";
import {
  abnormal,
  areasOf,
  chartStepSeconds,
  groupComponents,
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

  it("puts components this version does not know under その他, shown only when there are some", () => {
    expect(areasOf(normalReport())).toEqual(["gpu", "queue", "database", "recovery", "external"]);
    expect(groupComponents(normalReport(), "other")).toEqual([]);

    const quiet = withUnknownComponent(normalReport(), "info");
    expect(areasOf(quiet)).toEqual(["gpu", "queue", "database", "recovery", "external", "other"]);
    expect(groupComponents(quiet, "other").map((entry) => entry.component)).toEqual([
      "inference_gateway",
    ]);
    expect(groupSeverity(quiet, "other")).toBe("info");
    // Normal: GPU / VRAM still opens first.
    expect(initialGroup(quiet)).toBe("gpu");

    // Abnormal: its area is selected like any other (Decision 0080 3), not GPU.
    const report = withUnknownComponent(normalReport(), "error");
    expect(groupSeverity(report, "other")).toBe("error");
    expect(initialGroup(report)).toBe("other");
    // The worst one still wins over an unknown one that is less bad.
    expect(initialGroup(withUnknownComponent(abnormalReport(), "warning"))).toBe("recovery");
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

  it("keeps a bucket where every series is missing as a gap (Codex P2)", () => {
    const point = (bucket_start: string, mean: number) => ({
      bucket_start,
      mean,
      min: mean,
      max: mean,
    });
    const points = percentPoints(
      {
        gpu: [point("2026-10-07T04:00:00Z", 40), point("2026-10-07T04:36:00Z", 60)],
        used: [],
        reserved: [],
        total: [],
      },
      720,
    );
    expect(points.map((entry) => [entry.at, entry.gpu])).toEqual([
      ["2026-10-07T04:00:00.000Z", 40],
      ["2026-10-07T04:12:00.000Z", null],
      ["2026-10-07T04:24:00.000Z", null],
      ["2026-10-07T04:36:00.000Z", 60],
    ]);
  });

  it("keeps the empty buckets before the first and after the last sample (Codex P2)", () => {
    // Monitoring that started late or an outage now must not stretch the samples
    // over the whole period (PR #203); the bucket still filling is no gap.
    const point = (bucket_start: string, mean: number) => ({
      bucket_start,
      mean,
      min: mean,
      max: mean,
    });
    const series = {
      gpu: [point("2026-10-07T04:12:00Z", 40), point("2026-10-07T04:24:00Z", 50)],
      used: [],
      reserved: [],
      total: [],
    };
    const range = {
      since: new Date("2026-10-07T03:50:00Z"),
      until: new Date("2026-10-07T05:05:00Z"),
    };
    expect(
      percentPoints(series, 720, range).map((entry) => [entry.at.slice(11, 16), entry.gpu]),
    ).toEqual([
      ["04:00", null],
      ["04:12", 40],
      ["04:24", 50],
      ["04:36", null],
      ["04:48", null],
    ]);
    // Nothing sampled stays "no records"; without a step nothing is added.
    expect(percentPoints({ gpu: [], used: [], reserved: [], total: [] }, 720, range)).toEqual([]);
    expect(percentPoints(series, undefined, range)).toHaveLength(2);
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
