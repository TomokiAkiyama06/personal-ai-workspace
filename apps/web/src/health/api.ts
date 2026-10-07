// The production HealthSource: /api/v1/system/health* (PAW-066, Decision 0059).
// The Backend decides who may read what (`system_health.summary.read` for every
// human role, `admin.system_health.view` for Owner / Admin); this keeps only the
// fields the screen reads, so a field the Backend adds later is never shown by
// accident. A metric or part value that is not a number, a flag, a string or null
// is dropped.
import { apiRequest } from "../api/client";
import type {
  ComponentHealth,
  HealthEvent,
  HealthReport,
  HealthSource,
  HealthSummary,
  Metrics,
  MetricValue,
  SeriesPoint,
} from "./model";

function metricValue(value: unknown): MetricValue | undefined {
  if (value === null) return null;
  if (typeof value === "number" || typeof value === "boolean" || typeof value === "string") {
    return value;
  }
  return undefined;
}

function metrics(body: Record<string, unknown> | undefined): Metrics {
  const result: Metrics = {};
  for (const [name, value] of Object.entries(body ?? {})) {
    const kept = metricValue(value);
    if (kept !== undefined) result[name] = kept;
  }
  return result;
}

function component(body: ComponentHealth): ComponentHealth {
  return {
    component: body.component,
    severity: body.severity,
    status: body.status,
    reasons: [...body.reasons],
    metrics: metrics(body.metrics),
    parts: body.parts.map((part) => metrics(part)),
  };
}

export function toHealthReport(body: HealthReport): HealthReport {
  return {
    severity: body.severity,
    checked_at: body.checked_at,
    components: body.components.map(component),
  };
}

export function toHealthSummary(body: HealthSummary): HealthSummary {
  const connections: HealthSummary["connections"] = {};
  for (const [kind, state] of Object.entries(body.connections ?? {})) {
    connections[kind] = state === "available" ? "available" : "unavailable";
  }
  return { severity: body.severity, checked_at: body.checked_at, connections };
}

export const apiHealthSource: HealthSource = {
  async summary() {
    return toHealthSummary(await apiRequest<HealthSummary>("GET", "/system/health/summary"));
  },
  async report() {
    return toHealthReport(await apiRequest<HealthReport>("GET", "/system/health"));
  },
  async series(metric, since, until, stepSeconds) {
    const query = new URLSearchParams({
      since: since.toISOString(),
      until: until.toISOString(),
      step_seconds: String(stepSeconds),
    });
    const body = await apiRequest<{ points: SeriesPoint[] }>(
      "GET",
      `/system/health/metrics/${encodeURIComponent(metric)}?${query}`,
    );
    return body.points.map((point) => ({
      bucket_start: point.bucket_start,
      mean: point.mean,
      min: point.min,
      max: point.max,
    }));
  },
  async events(since) {
    const query = new URLSearchParams({ since: since.toISOString(), limit: "100" });
    const body = await apiRequest<{ events: HealthEvent[] }>(
      "GET",
      `/system/health/events?${query}`,
    );
    return body.events.map((event) => ({
      id: event.id,
      occurred_at: event.occurred_at,
      component: event.component,
      severity: event.severity,
      previous_severity: event.previous_severity,
      status: event.status,
      reasons: [...event.reasons],
    }));
  },
};
