// System Health answers for tests and screenshots, in the Backend's shape
// (paw_backend.health, /api/v1/system/health*): codes and numbers only.
import type {
  ComponentHealth,
  HealthEvent,
  HealthReport,
  HealthSource,
  HealthSummary,
  SeriesPoint,
} from "../health/model";

const GB = 1024 ** 3;

function ok(component: string, metrics: ComponentHealth["metrics"] = {}): ComponentHealth {
  return { component, severity: "info", status: "ok", reasons: [], metrics, parts: [] };
}

/** Every component normal: GPU 38%, VRAM 18.2 / 48 GB, 2 running / 1 queued. */
export function normalReport(): HealthReport {
  return {
    severity: "info",
    checked_at: "2026-10-07T05:31:48Z",
    components: [
      ok("database", { up: 1, latency_ms: 3.6 }),
      {
        ...ok("compute", {
          mode: "normal",
          relief: 0,
          probe_ok: true,
          utilization_percent: 38,
          leases: 2,
          waiting: 1,
          waiting_for_vram: 0,
          vram_total_bytes: 48 * GB,
          vram_used_bytes: 18.2 * GB,
          vram_reserved_bytes: 21 * GB,
          vram_available_bytes: 22 * GB,
          models_on_gpu: 2,
        }),
        parts: [
          { name: "qwen3.8-27b-fp8", role: "main", state: "gpu", draining: false },
          { name: "memory-worker-4b", role: "memory_worker", state: "gpu", draining: false },
          { name: "bge-m3", role: "embedding", state: "cpu", draining: false },
        ],
      },
      ok("task_queue", {
        queued: 1,
        running: 2,
        waiting: 1,
        waiting_resource: 1,
        failed_last_hour: 0,
        failed_last_day: 0,
        retries_last_hour: 0,
        loops_last_hour: 0,
        oom_last_hour: 0,
        escalations_last_hour: 0,
      }),
      ok("memory_worker", {
        pending: 0,
        claimed: 0,
        waiting_for_worker: 0,
        expired_leases: 0,
        dead_last_day: 0,
      }),
      {
        ...ok("connections", { codex_available: 1, claude_available: 1 }),
        parts: [
          { kind: "codex", configured: true, enabled: true, status: "connected", available: true },
          { kind: "claude", configured: true, enabled: true, status: "connected", available: true },
        ],
      },
      ok("connection_reaper", { consecutive_failures: 0, total_reaped: 0 }),
      ok("recovery_backup", {
        last_run: "completed",
        last_run_seconds_ago: 600,
        last_success_seconds_ago: 600,
        consecutive_failures: 0,
        last_failure: null,
      }),
      ok("memory_projection", {
        last_run: "completed",
        last_run_seconds_ago: 120,
        last_success_seconds_ago: 120,
        consecutive_failures: 0,
        last_failure: null,
      }),
      {
        ...ok("audit_retention", { last_run: null, consecutive_failures: 0 }),
        status: "never_ran",
      },
    ],
  };
}

/** The Recovery push keeps failing (ERROR) and VRAM is under pressure (WARNING). */
export function abnormalReport(): HealthReport {
  const report = normalReport();
  report.severity = "error";
  report.components = report.components.map((entry) => {
    if (entry.component === "recovery_backup") {
      return {
        ...entry,
        severity: "error",
        status: "failing",
        reasons: ["failing"],
        metrics: {
          last_run: "failed",
          last_run_seconds_ago: 300,
          last_success_seconds_ago: 25_200,
          consecutive_failures: 12,
          last_failure: "push:GitCommandError",
        },
      };
    }
    if (entry.component === "compute") {
      return { ...entry, severity: "warning", status: "degraded", reasons: ["vram_pressure"] };
    }
    return entry;
  });
  return report;
}

/**
 * The report with a component this version does not know (a newer Backend's):
 * normal, or abnormal with a known and an unknown reason and its own numbers.
 */
export function withUnknownComponent(
  report: HealthReport,
  severity: ComponentHealth["severity"] = "warning",
): HealthReport {
  const problem = severity !== "info";
  return {
    ...report,
    severity: worstOf(report.severity, severity),
    components: [
      ...report.components,
      {
        component: "inference_gateway",
        severity,
        status: problem ? "degraded" : "ok",
        reasons: problem ? ["check_timeout", "gateway_backlog"] : [],
        metrics: problem ? { backlog: 7, mode: "drain", healthy: false, last_seen: null } : {},
        parts: [],
      },
    ],
  };
}

function worstOf(a: HealthReport["severity"], b: HealthReport["severity"]) {
  const order = ["info", "warning", "error", "critical"];
  return order.indexOf(a) >= order.indexOf(b) ? a : b;
}

export function healthSummary(overrides: Partial<HealthSummary> = {}): HealthSummary {
  return {
    severity: "info",
    checked_at: "2026-10-07T05:31:48Z",
    connections: { codex: "available", claude: "available" },
    ...overrides,
  };
}

export function healthEvents(): HealthEvent[] {
  const today = new Date();
  today.setHours(8, 0, 0, 0);
  return [
    {
      id: 3,
      occurred_at: today.toISOString(),
      component: "recovery_backup",
      severity: "error",
      previous_severity: "warning",
      status: "failing",
      reasons: ["failing"],
    },
    {
      id: 2,
      occurred_at: "2026-09-18T03:12:00Z",
      component: "connections",
      severity: "info",
      previous_severity: "warning",
      status: "ok",
      reasons: [],
    },
  ];
}

/** Two buckets of each chart metric: 40% / 50% GPU, VRAM from the total of 48 GB. */
export function healthSeries(metric: string): SeriesPoint[] {
  const at = ["2026-10-07T04:00:00Z", "2026-10-07T05:00:00Z"];
  const values: Record<string, [number, number]> = {
    "compute.utilization_percent": [40, 50],
    "compute.vram_used_bytes": [12 * GB, 24 * GB],
    "compute.vram_reserved_bytes": [18 * GB, 18 * GB],
    "compute.vram_total_bytes": [48 * GB, 48 * GB],
  };
  const pair = values[metric];
  if (!pair) return [];
  return at.map((bucket_start, index) => {
    const mean = pair[index] ?? 0;
    return { bucket_start, mean, min: mean, max: mean };
  });
}

/** A source that answers with the given report (and the fixture's history). */
export function fakeHealthSource(
  report: HealthReport = normalReport(),
  overrides: Partial<HealthSource> = {},
): HealthSource {
  return {
    summary: async () => healthSummary({ severity: report.severity }),
    report: async () => report,
    series: async (metric) => healthSeries(metric),
    events: async () => healthEvents(),
    ...overrides,
  };
}
