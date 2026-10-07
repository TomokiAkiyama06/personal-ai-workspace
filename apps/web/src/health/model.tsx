// System Health (PAW-067, the design's Monitoring board; the Backend is PAW-066,
// Decision 0059). The screen and the header chip take their data through
// `HealthSource`: the app plugs in `apiHealthSource` (`./api.ts`, GET
// /api/v1/system/health*); without a source (tests) nothing is read and the
// screen says System Health is not available.
//
// Everything here is codes and numbers (Decision 0059 §1): no message of a
// dependency, path, URL, user text or process id, so the screen cannot show one.
// The words come from the i18n catalog; a code this version does not know is
// shown as it is.
import { createContext, type ReactNode, useContext } from "react";

/** The Notification Policy's levels, in order. */
export type Severity = "info" | "warning" | "error" | "critical";
export const SEVERITIES: readonly Severity[] = ["info", "warning", "error", "critical"];

export function severityRank(severity: string): number {
  const rank = SEVERITIES.indexOf(severity as Severity);
  return rank < 0 ? 0 : rank;
}

/** The highest of `severities` (`info` for none or unknown values). */
export function worstSeverity(severities: Iterable<string>): Severity {
  let worst: Severity = "info";
  for (const severity of severities) {
    if (severityRank(severity) > severityRank(worst)) worst = severity as Severity;
  }
  return worst;
}

/** The Backend's components (paw_backend.health.domain.Component), in its order. */
export const COMPONENTS = [
  "database",
  "compute",
  "task_queue",
  "memory_worker",
  "connections",
  "connection_reaper",
  "recovery_backup",
  "memory_projection",
  "audit_retention",
] as const;
export type ComponentName = (typeof COMPONENTS)[number];

export function isComponent(value: string): value is ComponentName {
  return (COMPONENTS as readonly string[]).includes(value);
}

/** The Backend's closed status codes (paw_backend.health.domain.Status). */
export const STATUSES = [
  "ok",
  "degraded",
  "failing",
  "stale",
  "unavailable",
  "not_configured",
  "never_ran",
  "check_failed",
] as const;

/** A metric's value: a number, a flag, a closed code or unknown. */
export type MetricValue = number | boolean | string | null;
export type Metrics = Record<string, MetricValue>;

export interface ComponentHealth {
  component: string;
  severity: Severity;
  status: string;
  /** Closed codes that say why (`probe_unavailable`, `expired:claude`, ...). */
  reasons: string[];
  metrics: Metrics;
  /** The models of the scheduler, the GPUs of the probe, the two connections. */
  parts: Metrics[];
}

/** GET /system/health (Owner / Admin). */
export interface HealthReport {
  severity: Severity;
  checked_at: string;
  components: ComponentHealth[];
}

/** GET /system/health/summary (every human role): the compact state only. */
export interface HealthSummary {
  severity: Severity;
  checked_at: string;
  /** "Claude: Available / Unavailable" by connection kind. */
  connections: Record<string, "available" | "unavailable">;
}

export interface SeriesPoint {
  bucket_start: string;
  mean: number;
  min: number;
  max: number;
}

export interface HealthEvent {
  id: number;
  occurred_at: string;
  component: string;
  severity: Severity;
  previous_severity: Severity | null;
  status: string;
  reasons: string[];
}

/** The design's period menu: 直近 24 時間 / 直近 1 時間 / 直近 7 日. */
export type HealthRange = "last24h" | "last1h" | "last7d";
export const HEALTH_RANGES: readonly HealthRange[] = ["last24h", "last1h", "last7d"];
export const RANGE_SECONDS: Record<HealthRange, number> = {
  last1h: 3_600,
  last24h: 86_400,
  last7d: 7 * 86_400,
};

export interface HealthSource {
  /** The compact state (every signed-in user). */
  summary(): Promise<HealthSummary>;
  /** Every component (Owner / Admin). */
  report(): Promise<HealthReport>;
  /** One metric's series between `since` and `until`, one point per `stepSeconds`
   * (Owner / Admin). */
  series(metric: string, since: Date, until: Date, stepSeconds: number): Promise<SeriesPoint[]>;
  /** The severity changes since `since`, newest first (Owner / Admin). */
  events(since: Date): Promise<HealthEvent[]>;
}

const HealthSourceContext = createContext<HealthSource | null>(null);

export function HealthSourceProvider({
  source,
  children,
}: {
  source: HealthSource | null;
  children: ReactNode;
}) {
  return <HealthSourceContext.Provider value={source}>{children}</HealthSourceContext.Provider>;
}

/** The connected source, or null (none plugged in: System Health is not shown). */
export function useHealthSource(): HealthSource | null {
  return useContext(HealthSourceContext);
}

// ---------- The board's groups ----------

/**
 * The chips of the board (one per watched area of docs/UI_DESIGN.md "System
 * Health": GPU / VRAM, Task Queue, PostgreSQL, Recovery Repository, External
 * Agent / Provider). Each holds the components it summarises.
 */
export type GroupName = "gpu" | "queue" | "database" | "recovery" | "external";
export const GROUPS: readonly { name: GroupName; components: readonly ComponentName[] }[] = [
  { name: "gpu", components: ["compute"] },
  { name: "queue", components: ["task_queue", "memory_worker"] },
  { name: "database", components: ["database"] },
  { name: "recovery", components: ["recovery_backup", "memory_projection", "audit_retention"] },
  { name: "external", components: ["connections", "connection_reaper"] },
];

export function groupOf(component: string): GroupName | null {
  return (
    GROUPS.find((group) => (group.components as readonly string[]).includes(component))?.name ??
    null
  );
}

export function componentOf(report: HealthReport, name: ComponentName): ComponentHealth | null {
  return report.components.find((entry) => entry.component === name) ?? null;
}

export function groupComponents(report: HealthReport, group: GroupName): ComponentHealth[] {
  const names = GROUPS.find((entry) => entry.name === group)?.components ?? [];
  return names.flatMap((name) => {
    const found = componentOf(report, name);
    return found ? [found] : [];
  });
}

export function groupSeverity(report: HealthReport, group: GroupName): Severity {
  return worstSeverity(groupComponents(report, group).map((entry) => entry.severity));
}

/** The components that are not normal, worst first (the report's order within a level). */
export function abnormal(report: HealthReport): ComponentHealth[] {
  return report.components
    .filter((entry) => entry.severity !== "info")
    .sort((a, b) => severityRank(b.severity) - severityRank(a.severity));
}

/** The group to open first: the worst one, or GPU / VRAM when all are normal. */
export function initialGroup(report: HealthReport): GroupName {
  const worst = abnormal(report)[0];
  return (worst && groupOf(worst.component)) || "gpu";
}

// ---------- Reading metrics ----------

export function numberOf(metrics: Metrics, name: string): number | null {
  const value = metrics[name];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export interface Vram {
  total: number;
  used: number;
  reserved: number | null;
  available: number | null;
}

/** The VRAM of the scheduler (or the probe's sum over its GPUs), or null. */
export function vramOf(compute: ComponentHealth | null): Vram | null {
  if (!compute) return null;
  const total = numberOf(compute.metrics, "vram_total_bytes");
  const used = numberOf(compute.metrics, "vram_used_bytes");
  if (total === null || used === null || total <= 0) return null;
  return {
    total,
    used,
    reserved: numberOf(compute.metrics, "vram_reserved_bytes"),
    available: numberOf(compute.metrics, "vram_available_bytes"),
  };
}

export interface QueueCounts {
  running: number;
  queued: number;
  waiting: number;
  waitingResource: number;
}

export function queueOf(tasks: ComponentHealth | null): QueueCounts | null {
  if (!tasks) return null;
  const running = numberOf(tasks.metrics, "running");
  const queued = numberOf(tasks.metrics, "queued");
  if (running === null || queued === null) return null;
  return {
    running,
    queued,
    waiting: numberOf(tasks.metrics, "waiting") ?? 0,
    waitingResource: numberOf(tasks.metrics, "waiting_resource") ?? 0,
  };
}

// ---------- Formatting ----------

const GB = 1024 ** 3;

function trimmed(value: number, digits = 1): string {
  return value.toFixed(digits).replace(/\.0+$/, "");
}

/** 18.2 GB of a byte count. */
export function formatGb(bytes: number): string {
  return trimmed(bytes / GB);
}

/** 0.62 -> 62% */
export function formatPercent(ratio: number): string {
  return `${Math.round(ratio * 100)}%`;
}

/** 45 秒 / 12 分 / 3 時間 / 2 日 of an age in seconds (the compact lists). */
export function ageParts(seconds: number): { unit: "s" | "m" | "h" | "d"; value: number } {
  const value = Math.max(0, Math.floor(seconds));
  if (value < 60) return { unit: "s", value };
  if (value < 3_600) return { unit: "m", value: Math.floor(value / 60) };
  if (value < 86_400) return { unit: "h", value: Math.floor(value / 3_600) };
  return { unit: "d", value: Math.floor(value / 86_400) };
}

// ---------- The chart's series ----------

/** The metrics the chart reads (`<component>.<name>`, Decision 0059 §4). */
export const CHART_METRICS = {
  gpu: "compute.utilization_percent",
  used: "compute.vram_used_bytes",
  reserved: "compute.vram_reserved_bytes",
  total: "compute.vram_total_bytes",
} as const;

/** About this many points per period (the table stays readable). */
export const CHART_POINTS = 120;

export function chartStepSeconds(range: HealthRange): number {
  return Math.max(10, Math.ceil(RANGE_SECONDS[range] / CHART_POINTS));
}

export interface PercentPoint {
  at: string;
  gpu: number | null;
  vramUsed: number | null;
  vramReserved: number | null;
}

/**
 * The buckets of the four series as percentages: GPU utilization as it is, VRAM
 * used and reserved of the total of the same bucket (else the latest total
 * before it). A bucket with no value of a series leaves a gap in that line; with
 * `stepSeconds`, so does a bucket missing from every series.
 */
export function percentPoints(
  series: {
    gpu: readonly SeriesPoint[];
    used: readonly SeriesPoint[];
    reserved: readonly SeriesPoint[];
    total: readonly SeriesPoint[];
  },
  stepSeconds?: number,
): PercentPoint[] {
  const byTime = (points: readonly SeriesPoint[]) =>
    new Map(points.map((point) => [Date.parse(point.bucket_start), point.mean]));
  const gpu = byTime(series.gpu);
  const used = byTime(series.used);
  const reserved = byTime(series.reserved);
  const total = byTime(series.total);
  const recorded = [...new Set([...gpu.keys(), ...used.keys(), ...reserved.keys()])]
    .filter((time) => !Number.isNaN(time))
    .sort((a, b) => a - b);
  // A bucket where nothing was sampled has no point at all in any series: it is
  // put back (empty), so the lines break there and keep their time scale
  // (Codex P2, PR #203).
  const times: number[] = [];
  const step = (stepSeconds ?? 0) * 1000;
  for (const time of recorded) {
    const previous = times[times.length - 1];
    if (step > 0 && previous !== undefined) {
      for (let gap = previous + step; time - gap >= step / 2; gap += step) times.push(gap);
    }
    times.push(time);
  }
  const totals = [...total.entries()].sort((a, b) => a[0] - b[0]);
  const totalAt = (time: number): number | null => {
    let found: number | null = null;
    for (const [at, value] of totals) {
      if (at > time) break;
      found = value;
    }
    return found ?? totals[0]?.[1] ?? null;
  };
  const ratio = (value: number | undefined, whole: number | null) =>
    value === undefined || whole === null || whole <= 0 ? null : (value / whole) * 100;
  return times.map((time) => {
    const whole = totalAt(time);
    return {
      at: new Date(time).toISOString(),
      gpu: gpu.get(time) ?? null,
      vramUsed: ratio(used.get(time), whole),
      vramReserved: ratio(reserved.get(time), whole),
    };
  });
}
