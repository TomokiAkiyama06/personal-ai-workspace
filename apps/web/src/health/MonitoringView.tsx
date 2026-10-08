// 管理 › サーバー監視 (PAW-067, the design's Monitoring board) over System Health
// (PAW-066, Decision 0059). The board's layout with the Backend's data: the
// board's hosts and temperatures do not exist in the Backend, so its chips are
// the watched areas of docs/UI_DESIGN.md (GPU / VRAM, Task Queue, PostgreSQL,
// Recovery Repository, External Agent), its chart is GPU / VRAM and its alerts
// are the severity changes (Decision 0080).
//
// Normal is compact: one quiet line, every component one row. Something
// abnormal opens the details by itself: the banner, the worst area selected,
// and each abnormal component's reasons and numbers expanded.
// The screen only reads; the Backend decides who may (admin.system_health.view).
import { type ReactNode, useCallback, useEffect, useId, useRef, useState } from "react";
import { type MessageKey, useI18n } from "../i18n";
import { errorMessage } from "../i18n/errors";
import { Link } from "../router";
import { Icon } from "../shell/icons";
import { HealthChart, HealthTable } from "./HealthChart";
import {
  abnormal,
  CHART_METRICS,
  type ComponentHealth,
  chartStepSeconds,
  componentOf,
  formatGb,
  formatPercent,
  GROUPS,
  type GroupName,
  groupComponents,
  groupSeverity,
  HEALTH_RANGES,
  type HealthEvent,
  type HealthRange,
  type HealthReport,
  initialGroup,
  type Metrics,
  numberOf,
  type PercentPoint,
  percentPoints,
  queueOf,
  RANGE_SECONDS,
  type Severity,
  useHealthSource,
  vramOf,
} from "./model";
import {
  agoText,
  componentName,
  reasonText,
  severityLabel,
  severityName,
  statusText,
} from "./text";
import "./health.css";

/** The board's refresh: 15 秒ごとに更新. */
export const REFRESH_SECONDS = 15;
/** The chart and the alerts are read again this often (and on a new period). */
export const HISTORY_REFRESH_SECONDS = 60;

type Translate = ReturnType<typeof useI18n>["t"];

type Load =
  | { status: "unavailable" }
  | { status: "loading" }
  | { status: "error"; error: unknown }
  | { status: "ready"; report: HealthReport };

type History<T> = { status: "loading" } | { status: "error" } | { status: "ready"; value: T };

function visible(): boolean {
  return typeof document === "undefined" || document.visibilityState !== "hidden";
}

// ---------- Small pieces ----------

function SeverityPill({ severity }: { severity: Severity }) {
  const { t } = useI18n();
  return <span className={`health-pill sev-${severity}`}>{severityLabel(t, severity)}</span>;
}

function Dot({ severity }: { severity: Severity }) {
  return <span className={`health-dot-mark sev-${severity}`} aria-hidden="true" />;
}

function groupValue(t: Translate, report: HealthReport, group: GroupName): string {
  const components = groupComponents(report, group);
  const worst = components.reduce<ComponentHealth | null>(
    (found, entry) =>
      found === null || SEVERITY_ORDER[entry.severity] > SEVERITY_ORDER[found.severity]
        ? entry
        : found,
    null,
  );
  switch (group) {
    case "gpu": {
      const vram = vramOf(componentOf(report, "compute"));
      if (vram)
        return t("health.chipValue.vram", { percent: formatPercent(vram.used / vram.total) });
      break;
    }
    case "queue": {
      const queue = queueOf(componentOf(report, "task_queue"));
      if (queue) {
        return t("health.chipValue.queue", { running: queue.running, queued: queue.queued });
      }
      break;
    }
    case "database": {
      const database = componentOf(report, "database");
      const latency = database ? numberOf(database.metrics, "latency_ms") : null;
      if (database?.severity === "info" && latency !== null) {
        return t("health.chipValue.latency", { ms: Math.round(latency) });
      }
      break;
    }
    case "external": {
      const parts = componentOf(report, "connections")?.parts ?? [];
      if (parts.length > 0 && worst?.severity === "info") {
        return t("health.chipValue.connections", {
          available: parts.filter((part) => part.available === true).length,
          total: parts.length,
        });
      }
      break;
    }
    case "recovery":
      break;
  }
  return worst ? statusText(t, worst.status) : t("health.stat.none");
}

const SEVERITY_ORDER: Record<Severity, number> = { info: 0, warning: 1, error: 2, critical: 3 };

// ---------- Banner / normal line ----------

function StatusLine({ report }: { report: HealthReport }) {
  const { t } = useI18n();
  const problems = abnormal(report);
  const worst = problems[0];
  if (!worst) {
    return (
      <div className="health-normal" role="status">
        <Dot severity="info" />
        <strong>{t("health.normal")}</strong>
        <span className="muted">
          {t("health.normalDetail", { count: report.components.length })}
        </span>
      </div>
    );
  }
  const reason = worst.reasons[0];
  return (
    <div className={`health-banner sev-${worst.severity}`} role="alert">
      <SeverityPill severity={worst.severity} />
      <span className="health-banner-text">
        {t("health.banner.problem", {
          component: componentName(t, worst.component),
          reason: reason ? reasonText(t, reason) : statusText(t, worst.status),
        })}
      </span>
      {problems.length > 1 && (
        <span className="health-banner-more mono">
          {t("health.banner.more", { count: problems.length - 1 })}
        </span>
      )}
      {worst.component === "task_queue" && (
        <Link to="/agents" className="health-banner-link">
          {t("health.banner.tasks")}
        </Link>
      )}
    </div>
  );
}

// ---------- Chips and cards ----------

function GroupChips({
  report,
  selected,
  onSelect,
}: {
  report: HealthReport;
  selected: GroupName;
  onSelect: (group: GroupName) => void;
}) {
  const { t } = useI18n();
  return (
    // The scroll container is a div: a fieldset does not clip its overflow.
    <div className="health-chips-scroll">
      <fieldset className="health-chips">
        <legend className="visually-hidden">{t("health.groups")}</legend>
        {GROUPS.map(({ name }) => {
          const severity = groupSeverity(report, name);
          return (
            <button
              key={name}
              type="button"
              className="health-chip"
              aria-pressed={selected === name}
              onClick={() => onSelect(name)}
            >
              <Dot severity={severity} />
              <span className="health-chip-name">
                <span>{t(`health.group.${name}`)}</span>
                <span className="mono muted">{t(`health.groupKind.${name}`)}</span>
              </span>
              <span className={`health-chip-value mono sev-text-${severity}`}>
                {groupValue(t, report, name)}
              </span>
              <span className="visually-hidden">{severityName(t, severity)}</span>
            </button>
          );
        })}
      </fieldset>
    </div>
  );
}

function StatCard({
  label,
  value,
  detail,
  severity = "info",
}: {
  label: string;
  value: string;
  detail: string;
  severity?: Severity;
}) {
  return (
    <div className={`stat-card health-stat sev-border-${severity}`}>
      <span className="stat-label">{label}</span>
      <span className={`stat-value sev-text-${severity}`}>{value}</span>
      <span className="stat-detail mono">{detail}</span>
    </div>
  );
}

function StatCards({
  report,
  failures,
  receivedOk,
}: {
  report: HealthReport;
  failures: number;
  receivedOk: boolean;
}) {
  const { t } = useI18n();
  const compute = componentOf(report, "compute");
  const vram = vramOf(compute);
  const utilization = compute ? numberOf(compute.metrics, "utilization_percent") : null;
  const models = compute ? numberOf(compute.metrics, "models_on_gpu") : null;
  const tasks = componentOf(report, "task_queue");
  const queue = queueOf(tasks);
  const none = t("health.stat.none");
  const notConfigured = compute?.status === "not_configured";
  return (
    <div className="stat-cards">
      <StatCard
        label={t("health.stat.vram")}
        value={vram ? formatPercent(vram.used / vram.total) : none}
        detail={
          vram
            ? vram.reserved === null
              ? t("health.stat.vramDetail", {
                  used: formatGb(vram.used),
                  total: formatGb(vram.total),
                })
              : t("health.stat.vramReserved", {
                  used: formatGb(vram.used),
                  total: formatGb(vram.total),
                  reserved: formatGb(vram.reserved),
                })
            : notConfigured
              ? t("health.stat.notConfigured")
              : compute
                ? statusText(t, compute.status)
                : " "
        }
        severity={compute?.severity}
      />
      <StatCard
        label={t("health.stat.gpu")}
        value={utilization === null ? none : `${Math.round(utilization)}%`}
        detail={models === null ? " " : t("health.stat.gpuModels", { count: models })}
      />
      <StatCard
        label={t("health.stat.queue")}
        value={queue ? t("health.stat.queueValue", { count: queue.running }) : none}
        detail={
          queue
            ? t("health.stat.queueDetail", {
                queued: queue.queued,
                resource: queue.waitingResource,
              })
            : " "
        }
        severity={tasks?.severity}
      />
      <StatCard
        label={t("health.stat.receive")}
        value={receivedOk ? t("health.stat.receiveOk") : t("health.stat.receiveFailed")}
        detail={t("health.stat.receiveDetail", { seconds: REFRESH_SECONDS, count: failures })}
        severity={receivedOk ? "info" : "error"}
      />
    </div>
  );
}

// ---------- Chart and alerts ----------

function ChartCard({ range, history }: { range: HealthRange; history: History<PercentPoint[]> }) {
  const { t } = useI18n();
  const [asTable, setAsTable] = useState(false);
  const rangeText = t(`health.range.${range}`);
  const points = history.status === "ready" ? history.value : [];
  const long = range === "last7d";
  let body: ReactNode;
  if (history.status === "loading") {
    body = (
      <p className="muted small" role="status">
        {t("app.loading")}
      </p>
    );
  } else if (history.status === "error") {
    body = <p className="muted small">{t("health.chart.failed")}</p>;
  } else if (points.length === 0) {
    body = <p className="muted small health-empty">{t("health.chart.empty")}</p>;
  } else if (asTable) {
    body = <HealthTable points={points} long={long} />;
  } else {
    body = (
      <HealthChart
        points={points}
        long={long}
        label={t("health.chart.label", { range: rangeText })}
      />
    );
  }
  return (
    <section className="usage-card health-chart-card" aria-labelledby="health-chart-title">
      <div className="usage-card-head">
        <h2 id="health-chart-title">{t("health.chart.title")}</h2>
        <span className="mono muted small">{t("health.chart.unit", { range: rangeText })}</span>
        <ul className="chart-legend push-right">
          {(["gpu", "vramUsed", "vramReserved"] as const).map((series) => (
            <li key={series}>
              <span className={`legend-swatch swatch-${series}`} aria-hidden="true" />
              {t(`health.chart.${series}`)}
            </li>
          ))}
        </ul>
        {points.length > 0 && (
          <button
            type="button"
            className="link-button"
            aria-pressed={asTable}
            onClick={() => setAsTable((value) => !value)}
          >
            {asTable ? t("health.chart.showChart") : t("health.chart.showTable")}
          </button>
        )}
      </div>
      {body}
    </section>
  );
}

function eventTime(iso: string, formatTime: (iso: string) => string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const today = new Date();
  if (date.toDateString() === today.toDateString()) return formatTime(iso);
  return `${date.getMonth() + 1}/${date.getDate()}`;
}

function AlertsCard({ history }: { history: History<HealthEvent[]> }) {
  const { t, formatTime, formatDate } = useI18n();
  let body: ReactNode;
  if (history.status === "loading") {
    body = (
      <p className="muted small" role="status">
        {t("app.loading")}
      </p>
    );
  } else if (history.status === "error") {
    body = <p className="muted small">{t("health.alerts.failed")}</p>;
  } else if (history.value.length === 0) {
    body = <p className="muted small health-empty">{t("health.alerts.empty")}</p>;
  } else {
    body = (
      <ul className="health-alerts">
        {history.value.map((event) => {
          const name = componentName(t, event.component);
          return (
            <li key={event.id}>
              <time
                className="mono"
                dateTime={event.occurred_at}
                title={formatDate(event.occurred_at)}
              >
                {eventTime(event.occurred_at, formatTime)}
              </time>
              <span className={`health-tag sev-${event.severity}`}>
                {severityLabel(t, event.severity)}
              </span>
              <span className="health-alert-text">
                {event.severity === "info"
                  ? t("health.alerts.recovered", { component: name })
                  : t("health.alerts.changed", {
                      component: name,
                      severity: severityLabel(t, event.severity),
                    })}
                {event.reasons.length > 0 && (
                  <span className="muted">
                    {" "}
                    · {event.reasons.map((reason) => reasonText(t, reason)).join(" · ")}
                  </span>
                )}
              </span>
            </li>
          );
        })}
      </ul>
    );
  }
  return (
    <section className="usage-card health-alerts-card" aria-labelledby="health-alerts-title">
      <div className="usage-card-head">
        <h2 id="health-alerts-title">{t("health.alerts.title")}</h2>
        <span className="muted small">{t("health.alerts.note")}</span>
      </div>
      {body}
    </section>
  );
}

// ---------- The detail panel ----------

function Meter({
  label,
  value,
  ratio,
  tone,
}: {
  label: string;
  value: string;
  ratio: number;
  tone: string;
}) {
  return (
    <div className="health-meter">
      <div className="health-meter-head">
        <span>{label}</span>
        <span className="mono">{value}</span>
      </div>
      {/* biome-ignore lint/a11y/useSemanticElements: the design's meter (color per series) cannot be drawn with a native <meter> in every browser */}
      <div
        className="meter"
        role="meter"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(Math.min(1, Math.max(0, ratio)) * 100)}
        aria-valuetext={value}
      >
        <div
          className={`meter-fill ${tone}`}
          style={{ width: `${Math.min(100, Math.max(0, ratio * 100))}%` }}
        />
      </div>
    </div>
  );
}

function GpuSummary({ compute }: { compute: ComponentHealth | null }) {
  const { t } = useI18n();
  if (!compute) return null;
  const vram = vramOf(compute);
  const utilization = numberOf(compute.metrics, "utilization_percent");
  const figures: { label: string; value: number | null }[] = [
    { label: t("health.figure.leases"), value: numberOf(compute.metrics, "leases") },
    { label: t("health.figure.waiting"), value: numberOf(compute.metrics, "waiting") },
    { label: t("health.figure.vramWaiting"), value: numberOf(compute.metrics, "waiting_for_vram") },
    { label: t("health.figure.models"), value: numberOf(compute.metrics, "models_on_gpu") },
  ];
  const models = compute.parts.filter((part) => typeof part.role === "string");
  return (
    <>
      {utilization !== null && (
        <Meter
          label={t("health.stat.gpu")}
          value={`${Math.round(utilization)}%`}
          ratio={utilization / 100}
          tone="tone-gpu"
        />
      )}
      {vram && (
        <Meter
          label={t("health.chart.vramUsed")}
          value={t("health.stat.vramDetail", {
            used: formatGb(vram.used),
            total: formatGb(vram.total),
          })}
          ratio={vram.used / vram.total}
          tone="tone-used"
        />
      )}
      {vram && vram.reserved !== null && (
        <Meter
          label={t("health.chart.vramReserved")}
          value={t("health.stat.vramDetail", {
            used: formatGb(vram.reserved),
            total: formatGb(vram.total),
          })}
          ratio={vram.reserved / vram.total}
          tone="tone-reserved"
        />
      )}
      {figures.some((figure) => figure.value !== null) && (
        <dl className="health-figures">
          {figures.map((figure) =>
            figure.value === null ? null : (
              <div key={figure.label}>
                <dt>{figure.label}</dt>
                <dd className="mono">{figure.value}</dd>
              </div>
            ),
          )}
        </dl>
      )}
      {models.length > 0 && (
        <>
          <hr />
          <span className="health-section-title">{t("health.detail.models")}</span>
          <ul className="health-models">
            {models.map((part) => (
              <ModelRow key={`${part.role}-${part.name}`} part={part} />
            ))}
          </ul>
        </>
      )}
    </>
  );
}

function ModelRow({ part }: { part: Metrics }) {
  const { t } = useI18n();
  const role = String(part.role);
  const state = String(part.state);
  const roleKey = `health.role.${role}` as MessageKey;
  const stateKey = `health.modelState.${state}` as MessageKey;
  const severity: Severity = state === "failed" ? "error" : "info";
  return (
    <li>
      <Dot severity={severity} />
      <span className="health-row-name">
        {["main", "memory_worker", "embedding", "reranker"].includes(role) ? t(roleKey) : role}
        {typeof part.name === "string" && <span className="mono muted"> {part.name}</span>}
      </span>
      <span className="mono">
        {["gpu", "cpu", "unloaded", "failed"].includes(state) ? t(stateKey) : state}
        {part.draining === true && ` · ${t("health.modelDraining")}`}
      </span>
    </li>
  );
}

/** The numbers of a component, as short lines (codes and numbers only). */
function componentLines(t: Translate, health: ComponentHealth): string[] {
  const m = health.metrics;
  const n = (name: string) => numberOf(m, name);
  const lines: string[] = [];
  // A line only when every number in it was read: a check that failed or timed
  // out reports no metrics, and a missing count is not zero (Codex P2, PR #203).
  const metricLine = (key: MessageKey, names: Record<string, string>) => {
    const params: Record<string, number> = {};
    for (const [param, name] of Object.entries(names)) {
      const value = n(name);
      if (value === null) return;
      params[param] = value;
    }
    lines.push(t(key, params));
  };
  switch (health.component) {
    case "database": {
      const latency = n("latency_ms");
      if (latency !== null) lines.push(t("health.metric.latency", { ms: Math.round(latency) }));
      break;
    }
    case "compute": {
      const vram = vramOf(health);
      if (vram) {
        lines.push(
          t("health.metric.vram", {
            used: formatGb(vram.used),
            reserved: vram.reserved === null ? "—" : formatGb(vram.reserved),
            available: vram.available === null ? "—" : formatGb(vram.available),
            total: formatGb(vram.total),
          }),
        );
      }
      metricLine("health.metric.lease", {
        leases: "leases",
        waiting: "waiting",
        vram: "waiting_for_vram",
      });
      if (typeof m.mode === "string") lines.push(t("health.metric.mode", { mode: m.mode }));
      break;
    }
    case "task_queue":
      metricLine("health.metric.queue", {
        running: "running",
        queued: "queued",
        waiting: "waiting",
      });
      metricLine("health.metric.failures", { hour: "failed_last_hour", day: "failed_last_day" });
      metricLine("health.metric.retriesLoops", {
        retries: "retries_last_hour",
        loops: "loops_last_hour",
      });
      metricLine("health.metric.oom", {
        hour: "oom_last_hour",
        escalations: "escalations_last_hour",
      });
      break;
    case "memory_worker":
      metricLine("health.metric.memoryQueue", {
        pending: "pending",
        claimed: "claimed",
        deferred: "waiting_for_worker",
      });
      metricLine("health.metric.memoryDead", { expired: "expired_leases", dead: "dead_last_day" });
      break;
    case "connections":
      for (const part of health.parts) {
        const kind = String(part.kind);
        lines.push(
          t("health.metric.connection", {
            kind: kind === "codex" || kind === "claude" ? t(`health.connection.${kind}`) : kind,
            state:
              part.configured === false
                ? t("health.metric.notConfigured")
                : part.available === true
                  ? t("health.metric.connectionAvailable")
                  : t("health.metric.connectionUnavailable"),
          }),
        );
      }
      break;
    case "connection_reaper":
      metricLine("health.metric.reaper", {
        failures: "consecutive_failures",
        reaped: "total_reaped",
      });
      break;
    case "recovery_backup":
    case "memory_projection":
    case "audit_retention": {
      // Only times the check returned: a failed check returns none, and a missing
      // time is not "never" (null, sent by the Backend, is) (Codex P2, PR #203).
      if (health.status === "never_ran") break;
      if (!("last_run_seconds_ago" in m && "last_success_seconds_ago" in m)) break;
      lines.push(
        t("health.metric.job", {
          run: agoText(t, n("last_run_seconds_ago")),
          success: agoText(t, n("last_success_seconds_ago")),
        }),
      );
      const failures = n("consecutive_failures");
      if (failures) lines.push(t("health.metric.jobFailures", { count: failures }));
      if (typeof m.last_failure === "string") {
        // `<step>:<error type>`: the step is the closed code worth showing.
        lines.push(t("health.metric.lastFailure", { step: m.last_failure.split(":", 1)[0] ?? "" }));
      }
      break;
    }
  }
  return lines;
}

function ComponentRow({ health }: { health: ComponentHealth }) {
  const { t } = useI18n();
  const name = componentName(t, health.component);
  const lines = componentLines(t, health);
  const problem = health.severity !== "info";
  const [open, setOpen] = useState(problem);
  // Opens by itself when the component becomes abnormal (and closes on recovery).
  useEffect(() => setOpen(problem), [problem]);
  const hasDetail = lines.length > 0 || health.reasons.length > 0;
  return (
    <li className={problem ? "health-component problem" : "health-component"}>
      <div className="health-component-head">
        <Dot severity={health.severity} />
        <span className="health-row-name">{name}</span>
        <span className={`mono sev-text-${health.severity}`}>{statusText(t, health.status)}</span>
        {hasDetail && (
          <button
            type="button"
            className="icon-button health-toggle"
            aria-expanded={open}
            aria-label={t("health.detail.reasons", { component: name })}
            onClick={() => setOpen((value) => !value)}
          >
            <Icon name={open ? "chevronDown" : "chevronRight"} size={14} />
          </button>
        )}
      </div>
      {open && hasDetail && (
        <div className="health-component-detail">
          {health.reasons.length > 0 && (
            <ul className="health-reasons">
              {health.reasons.map((reason) => (
                <li key={reason}>{reasonText(t, reason)}</li>
              ))}
            </ul>
          )}
          {lines.map((line) => (
            <span key={line} className="mono">
              {line}
            </span>
          ))}
        </div>
      )}
    </li>
  );
}

function DetailPanel({ report, group }: { report: HealthReport; group: GroupName }) {
  const { t } = useI18n();
  const severity = groupSeverity(report, group);
  const components = groupComponents(report, group);
  return (
    <section className="usage-card health-panel" aria-labelledby="health-panel-title">
      <div className="health-panel-head">
        <h2 id="health-panel-title">{t(`health.group.${group}`)}</h2>
        <span className="mono muted small">{t(`health.groupKind.${group}`)}</span>
        <span className={`health-state sev-${severity}`}>
          <Dot severity={severity} />
          {severityName(t, severity)}
        </span>
      </div>
      {group === "gpu" && <GpuSummary compute={componentOf(report, "compute")} />}
      <hr />
      <span className="health-section-title">{t("health.detail.components")}</span>
      <ul className="health-components">
        {components.map((health) => (
          <ComponentRow key={health.component} health={health} />
        ))}
      </ul>
      <div className="info-box health-readonly">
        <Icon name="info" size={14} />
        <p>{t("health.readOnly")}</p>
      </div>
    </section>
  );
}

function Unavailable() {
  const { t } = useI18n();
  return (
    <section className="usage-card usage-unavailable" aria-live="polite">
      <Icon name="admin" size={22} />
      <h2>{t("health.unavailable.title")}</h2>
      <p className="muted small">{t("health.unavailable.body")}</p>
    </section>
  );
}

// ---------- The screen ----------

/** 14:31:48 (the board's 最終受信). */
function clockTime(locale: string, iso: string): string {
  return new Intl.DateTimeFormat(locale === "ja" ? "ja-JP" : "en-US", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(iso));
}

export function MonitoringView() {
  const { t, locale } = useI18n();
  const source = useHealthSource();
  const rangeId = useId();
  const [range, setRange] = useState<HealthRange>("last24h");
  const [load, setLoad] = useState<Load>(
    source ? { status: "loading" } : { status: "unavailable" },
  );
  const [receivedAt, setReceivedAt] = useState<string | null>(null);
  const [receivedOk, setReceivedOk] = useState(true);
  const [failures, setFailures] = useState(0);
  const [group, setGroup] = useState<GroupName | null>(null);
  const [chart, setChart] = useState<History<PercentPoint[]>>({ status: "loading" });
  const [events, setEvents] = useState<History<HealthEvent[]>>({ status: "loading" });
  const [attempt, setAttempt] = useState(0);
  const retry = useCallback(() => setAttempt((value) => value + 1), []);
  // The worst area is selected once, when the first report (or a new problem) arrives.
  const lastWorst = useRef<string | null>(null);

  useEffect(() => {
    if (!source) {
      setLoad({ status: "unavailable" });
      return;
    }
    void attempt;
    let cancelled = false;
    // The latest answer wins: an answer to an older request that arrives after a
    // newer one was shown is dropped, and a request that never answers does not
    // hold up the next ones (Codex P2, PR #203).
    let requested = 0;
    let shown = 0;
    const read = () => {
      requested += 1;
      const request = requested;
      source.report().then(
        (report) => {
          if (cancelled || request < shown) return;
          shown = request;
          setLoad({ status: "ready", report });
          setReceivedAt(new Date().toISOString());
          setReceivedOk(true);
          const worst = abnormal(report)[0]?.component ?? null;
          if (worst !== lastWorst.current) {
            lastWorst.current = worst;
            setGroup(initialGroup(report));
          }
        },
        (error: unknown) => {
          if (cancelled || request < shown) return;
          // A failure is an answer too: older answers arriving later are dropped.
          shown = request;
          setFailures((value) => value + 1);
          setReceivedOk(false);
          // A failed refresh keeps the last report on screen (受信 says it is stale).
          setLoad((previous) =>
            previous.status === "ready" ? previous : { status: "error", error },
          );
        },
      );
    };
    read();
    const timer = window.setInterval(() => {
      if (visible()) read();
    }, REFRESH_SECONDS * 1000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [source, attempt]);

  useEffect(() => {
    if (!source) return;
    let cancelled = false;
    // The latest answer wins, like the report.
    let requested = 0;
    let chartShown = 0;
    let eventsShown = 0;
    const read = () => {
      requested += 1;
      const request = requested;
      const until = new Date();
      const since = new Date(until.getTime() - RANGE_SECONDS[range] * 1000);
      const step = chartStepSeconds(range);
      Promise.all([
        source.series(CHART_METRICS.gpu, since, until, step),
        source.series(CHART_METRICS.used, since, until, step),
        source.series(CHART_METRICS.reserved, since, until, step),
        source.series(CHART_METRICS.total, since, until, step),
      ]).then(
        ([gpu, used, reserved, total]) => {
          if (!cancelled && request >= chartShown) {
            chartShown = request;
            setChart({
              status: "ready",
              value: percentPoints({ gpu, used, reserved, total }, step, { since, until }),
            });
          }
        },
        () => {
          if (cancelled || request < chartShown) return;
          chartShown = request;
          setChart((previous) => (previous.status === "ready" ? previous : { status: "error" }));
        },
      );
      source.events(since).then(
        (value) => {
          if (cancelled || request < eventsShown) return;
          eventsShown = request;
          setEvents({ status: "ready", value });
        },
        () => {
          if (cancelled || request < eventsShown) return;
          eventsShown = request;
          setEvents((previous) => (previous.status === "ready" ? previous : { status: "error" }));
        },
      );
    };
    setChart({ status: "loading" });
    setEvents({ status: "loading" });
    read();
    const timer = window.setInterval(() => {
      if (visible()) read();
    }, HISTORY_REFRESH_SECONDS * 1000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [source, range]);

  let body: ReactNode;
  switch (load.status) {
    case "unavailable":
      body = <Unavailable />;
      break;
    case "loading":
      body = (
        <p className="muted small" role="status">
          {t("app.loading")}
        </p>
      );
      break;
    case "error":
      body = (
        <div className="notice usage-error">
          <p role="alert">{errorMessage(t, load.error)}</p>
          <button type="button" className="secondary small-button" onClick={retry}>
            {t("app.retry")}
          </button>
        </div>
      );
      break;
    case "ready": {
      const selected = group ?? initialGroup(load.report);
      body = (
        <>
          <StatusLine report={load.report} />
          <GroupChips report={load.report} selected={selected} onSelect={setGroup} />
          <StatCards report={load.report} failures={failures} receivedOk={receivedOk} />
          <div className="health-columns">
            <div className="health-main">
              <ChartCard range={range} history={chart} />
              <AlertsCard history={events} />
            </div>
            <DetailPanel report={load.report} group={selected} />
          </div>
        </>
      );
      break;
    }
  }

  return (
    <div className="usage health">
      <div className="usage-toolbar">
        <h1>{t("health.title")}</h1>
        <span className="health-source">
          <Icon name="info" size={12} />
          {t("health.source")}
        </span>
        <label htmlFor={rangeId} className="visually-hidden">
          {t("health.range")}
        </label>
        <select
          id={rangeId}
          className="push-right"
          value={range}
          onChange={(event) => setRange(event.target.value as HealthRange)}
          disabled={!source}
        >
          {HEALTH_RANGES.map((entry) => (
            <option key={entry} value={entry}>
              {t(`health.range.${entry}`)}
            </option>
          ))}
        </select>
        <span className="health-refresh mono">
          {receivedAt
            ? t("health.refresh", { seconds: REFRESH_SECONDS, time: clockTime(locale, receivedAt) })
            : t("health.refreshNever", { seconds: REFRESH_SECONDS })}
        </span>
      </div>
      <div className="usage-body">{body}</div>
    </div>
  );
}
