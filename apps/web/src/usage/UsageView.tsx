// The usage / quota screen (PAW-064, the design's Usage board and the ユーザーと
// 上限 table of the Admin board). The same view is 管理 › 使用状況 (Owner / Admin,
// with 自分 / ワークスペース) and 設定 › 自分の使用状況 (everyone, 自分 only).
// Which scope a viewer may load is decided by the Backend (`agent.use` for one's
// own, `admin.usage.view` for everyone's); the role only chooses what to show.
import { type ReactNode, useCallback, useEffect, useId, useState } from "react";
import { type MessageKey, useI18n } from "../i18n";
import { errorMessage } from "../i18n/errors";
import { Avatar, RoleBadge } from "../shell/common";
import { Icon } from "../shell/icons";
import {
  AGENT_KINDS,
  type AgentTotal,
  formatCount,
  formatDuration,
  formatMetric,
  headlineQuota,
  purposeTotals,
  type QuotaLevel,
  type QuotaUsage,
  quotaLevel,
  quotaRatio,
  UNLIMITED,
  USAGE_RANGES,
  USAGE_SCOPES,
  type UsageRange,
  type UsageReport,
  type UsageScope,
  type UserUsage,
  useUsageSource,
  worstLevel,
} from "./model";
import { UsageChart, UsageTable } from "./UsageChart";

type Load =
  | { status: "unavailable" }
  | { status: "loading" }
  | { status: "error"; error: unknown }
  | { status: "ready"; report: UsageReport };

function signed(value: number): string {
  return value > 0 ? `+${value}` : String(value);
}

function StatCard({
  label,
  value,
  detail,
  tone,
}: {
  label: string;
  value: string;
  detail: string;
  tone?: "warning";
}) {
  return (
    <div className="stat-card">
      <span className="stat-label">{label}</span>
      <span className={tone ? `stat-value ${tone}` : "stat-value"}>{value}</span>
      <span className="stat-detail mono">{detail}</span>
    </div>
  );
}

function StatCards({ report, range }: { report: UsageReport; range: UsageRange }) {
  const { t } = useI18n();
  const notRecorded = t("usage.notRecorded");
  const tokens = (report.tokens.local ?? 0) + report.tokens.external;
  const gpu = report.gpu_seconds;
  const escalations = report.escalations;
  return (
    <div className="stat-cards">
      <StatCard
        label={t("usage.card.tasks")}
        value={formatCount(report.tasks)}
        detail={
          report.previous_tasks === null
            ? " "
            : t(`usage.delta.${range}`, { delta: signed(report.tasks - report.previous_tasks) })
        }
      />
      <StatCard
        label={t("usage.card.tokens")}
        value={formatCount(tokens)}
        detail={t("usage.card.tokensSplit", {
          local: report.tokens.local === null ? "—" : formatCount(report.tokens.local),
          external: formatCount(report.tokens.external),
        })}
      />
      <StatCard
        label={t("usage.card.gpu")}
        value={gpu === null ? "—" : formatDuration(gpu)}
        detail={
          gpu === null
            ? notRecorded
            : t("usage.card.gpuAverage", {
                average: formatDuration(report.tasks > 0 ? gpu / report.tasks : 0),
              })
        }
      />
      <StatCard
        label={t("usage.card.escalations")}
        value={escalations === null ? "—" : String(escalations.failed + escalations.loop_detected)}
        detail={
          escalations === null
            ? notRecorded
            : t("usage.card.escalationsSplit", {
                failed: escalations.failed,
                loop: escalations.loop_detected,
              })
        }
        tone={
          escalations && escalations.failed + escalations.loop_detected > 0 ? "warning" : undefined
        }
      />
    </div>
  );
}

function DailyCard({ report }: { report: UsageReport }) {
  const { t } = useI18n();
  const [asTable, setAsTable] = useState(false);
  const empty = report.daily.every((day) => day.local + day.codex + day.claude === 0);
  return (
    <section className="usage-card" aria-labelledby="usage-daily-title">
      <div className="usage-card-head">
        <h2 id="usage-daily-title">{t("usage.chart.title")}</h2>
        <ul className="chart-legend">
          {AGENT_KINDS.map((agent) => (
            <li key={agent}>
              <span className={`legend-swatch series-${agent}`} aria-hidden="true" />
              {t(`usage.agent.${agent}`)}
            </li>
          ))}
        </ul>
        {!empty && (
          <button
            type="button"
            className="link-button push-right"
            aria-pressed={asTable}
            onClick={() => setAsTable((value) => !value)}
          >
            {asTable ? t("usage.chart.showChart") : t("usage.chart.showTable")}
          </button>
        )}
      </div>
      {empty ? (
        <p className="muted small usage-empty">{t("usage.chart.empty")}</p>
      ) : asTable ? (
        <UsageTable days={report.daily} />
      ) : (
        <UsageChart days={report.daily} />
      )}
    </section>
  );
}

function AgentBars({ agents }: { agents: readonly AgentTotal[] }) {
  const { t } = useI18n();
  const byAgent = new Map(agents.map((entry) => [entry.agent, entry]));
  const max = Math.max(1, ...agents.map((entry) => entry.tasks));
  return (
    <ul className="agent-bars">
      {AGENT_KINDS.map((agent) => {
        const entry = byAgent.get(agent) ?? { agent, tasks: 0, tokens: 0 };
        return (
          <li key={agent}>
            <span className="agent-name">{t(`usage.agent.${agent}`)}</span>
            <span className="agent-bar-line">
              <span
                className={`agent-bar series-${agent}`}
                style={{ width: `${(entry.tasks / max) * 60}%` }}
                aria-hidden="true"
              />
              <span className="mono agent-value">
                {t("usage.agents.value", {
                  tasks: formatCount(entry.tasks),
                  tokens: formatCount(entry.tokens),
                })}
              </span>
            </span>
          </li>
        );
      })}
    </ul>
  );
}

function BreakdownCard({ report, range }: { report: UsageReport; range: UsageRange }) {
  const { t } = useI18n();
  const purposes = purposeTotals(report.purposes);
  return (
    <section className="usage-card usage-breakdown">
      <h2>{t("usage.agents.title", { range: t(`usage.rangeShort.${range}`) })}</h2>
      <AgentBars agents={report.agents} />
      <hr />
      <h2>{t("usage.purposes.title")}</h2>
      {purposes.length === 0 ? (
        <p className="muted small">{t("usage.purposes.empty")}</p>
      ) : (
        <table className="purpose-table" aria-label={t("usage.purposes.title")}>
          <thead>
            <tr>
              <th scope="col">{t("usage.purposes.category")}</th>
              <th scope="col">{t("usage.purposes.tasks")}</th>
              <th scope="col">{t("usage.purposes.tokens")}</th>
            </tr>
          </thead>
          <tbody>
            {purposes.map((entry) => (
              <tr key={entry.purpose}>
                <th scope="row">{t(`usage.purpose.${entry.purpose}` as MessageKey)}</th>
                <td className="mono">{formatCount(entry.tasks)}</td>
                <td className="mono">{formatCount(entry.tokens)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function useQuotaLabel(): (quota: QuotaUsage) => string {
  const { t } = useI18n();
  return (quota) => {
    const kind = t(`usage.agent.${quota.kind}`);
    const period = t(`usage.quota.period.${quota.period}`);
    return quota.metric === "tokens"
      ? t("usage.quota.label", { kind, period })
      : t("usage.quota.labelMetric", {
          kind,
          period,
          metric: t(`usage.quota.metric.${quota.metric}`),
        });
  };
}

function QuotaMeter({ quota }: { quota: QuotaUsage }) {
  const { t, formatDate } = useI18n();
  const label = useQuotaLabel()(quota);
  const level = quotaLevel(quota);
  const ratio = quotaRatio(quota);
  return (
    <div className={`quota-meter level-${level}`}>
      <div className="quota-meter-head">
        <span className="quota-name">{label}</span>
        {quota.limit === UNLIMITED ? (
          <span className="quota-used mono">{t("usage.quota.unlimited")}</span>
        ) : (
          <>
            <span className="quota-used mono">{formatMetric(quota.metric, quota.used)}</span>
            <span className="quota-limit mono">/ {formatMetric(quota.metric, quota.limit)}</span>
          </>
        )}
      </div>
      {ratio !== null && (
        // biome-ignore lint/a11y/useSemanticElements: the design's meter (color by level) cannot be drawn with a native <meter> in every browser
        <div
          className="meter"
          role="meter"
          aria-label={label}
          aria-valuemin={0}
          aria-valuemax={typeof quota.limit === "number" ? quota.limit : 0}
          aria-valuenow={quota.used}
          aria-valuetext={`${formatMetric(quota.metric, quota.used)} / ${formatMetric(quota.metric, quota.limit as number)}`}
        >
          <div className="meter-fill" style={{ width: `${Math.min(100, ratio * 100)}%` }} />
        </div>
      )}
      {(level === "near" || level === "reached") && (
        <span className="quota-state small">
          {t(`usage.quota.level.${level}`)}
          {level === "reached" &&
            quota.window_end &&
            ` · ${t("usage.quota.resets", { date: formatDate(quota.window_end) })}`}
        </span>
      )}
    </div>
  );
}

function QuotaCard({ quotas }: { quotas: readonly QuotaUsage[] }) {
  const { t } = useI18n();
  return (
    <section className="usage-card usage-quota">
      <h2>{t("usage.quota.title")}</h2>
      {quotas.length === 0 ? (
        <p className="muted small">{t("usage.quota.none")}</p>
      ) : (
        quotas.map((quota) => (
          <QuotaMeter key={`${quota.kind}-${quota.metric}-${quota.period}`} quota={quota} />
        ))
      )}
      <div className="info-box quota-note">
        <Icon name="info" size={15} />
        <p>{t("usage.quota.note")}</p>
      </div>
    </section>
  );
}

const LEVEL_CLASS: Record<QuotaLevel, string> = {
  ok: "ok",
  unlimited: "ok",
  near: "warning",
  reached: "error",
};

function UsersCard({ users }: { users: readonly UserUsage[] }) {
  const { t } = useI18n();
  return (
    <section className="usage-card usage-users">
      <div className="usage-card-head">
        <h2>{t("usage.users.title")}</h2>
        <span className="muted small">{t("usage.users.privacy")}</span>
      </div>
      {users.length === 0 ? (
        <p className="muted small">{t("usage.users.empty")}</p>
      ) : (
        <div className="usage-table-wrap">
          <table className="users-table" aria-label={t("usage.users.title")}>
            <thead>
              <tr>
                <th scope="col">{t("usage.users.user")}</th>
                <th scope="col">{t("usage.users.role")}</th>
                <th scope="col">{t("usage.users.tasks")}</th>
                <th scope="col">{t("usage.users.tokens")}</th>
                <th scope="col">{t("usage.users.quota")}</th>
                <th scope="col">{t("usage.users.status")}</th>
              </tr>
            </thead>
            <tbody>
              {users.map((row) => {
                const headline = headlineQuota(row.quotas);
                const level = worstLevel(row.quotas);
                return (
                  <tr key={row.user_id}>
                    <th scope="row">
                      <span className="user-cell">
                        <Avatar name={row.login_name} />
                        {row.login_name}
                      </span>
                    </th>
                    <td>
                      <RoleBadge role={row.system_role} />
                    </td>
                    <td className="mono">{formatCount(row.tasks)}</td>
                    <td className="mono">{formatCount(row.tokens)}</td>
                    <td className="mono">
                      {headline === null || headline.limit === UNLIMITED
                        ? t("usage.quota.unlimited")
                        : t("usage.quota.value", {
                            limit: formatMetric(headline.metric, headline.limit),
                            per: t(`usage.quota.per.${headline.period}`),
                          })}
                    </td>
                    <td className={`user-level ${LEVEL_CLASS[level]}`}>
                      {t(`usage.quota.level.${level === "unlimited" ? "ok" : level}`)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function Unavailable() {
  const { t } = useI18n();
  return (
    <section className="usage-card usage-unavailable" aria-live="polite">
      <Icon name="usage" size={22} />
      <h2>{t("usage.unavailable.title")}</h2>
      <p className="muted small">{t("usage.unavailable.body")}</p>
    </section>
  );
}

/**
 * The toolbar (scope, period, the privacy note) and the report.
 * `scopes`: what the toggle offers (Owner / Admin: 自分 and ワークスペース).
 */
export function UsageView({
  scopes = ["self"],
  title,
}: {
  scopes?: readonly UsageScope[];
  title: MessageKey;
}) {
  const { t } = useI18n();
  const source = useUsageSource();
  const rangeId = useId();
  const [scope, setScope] = useState<UsageScope>("self");
  const [range, setRange] = useState<UsageRange>("last14");
  const [load, setLoad] = useState<Load>(
    source ? { status: "loading" } : { status: "unavailable" },
  );
  const [attempt, setAttempt] = useState(0);
  const retry = useCallback(() => setAttempt((value) => value + 1), []);

  useEffect(() => {
    if (!source) {
      setLoad({ status: "unavailable" });
      return;
    }
    // `attempt` re-runs the load after an error (再試行).
    void attempt;
    let cancelled = false;
    setLoad({ status: "loading" });
    source.load(scope, range).then(
      (report) => {
        if (!cancelled) setLoad({ status: "ready", report });
      },
      (error: unknown) => {
        if (!cancelled) setLoad({ status: "error", error });
      },
    );
    return () => {
      cancelled = true;
    };
  }, [source, scope, range, attempt]);

  const offered = USAGE_SCOPES.filter((entry) => scopes.includes(entry));
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
    case "ready":
      body = (
        <>
          <StatCards report={load.report} range={range} />
          <DailyCard report={load.report} />
          <div className="usage-columns">
            <BreakdownCard report={load.report} range={range} />
            {scope === "self" ? (
              <QuotaCard quotas={load.report.quotas} />
            ) : (
              <UsersCard users={load.report.users} />
            )}
          </div>
        </>
      );
      break;
  }

  return (
    <div className="usage">
      <div className="usage-toolbar">
        <h1>{t(title)}</h1>
        {offered.length > 1 && (
          <fieldset className="segments usage-scope">
            <legend className="visually-hidden">{t("usage.scope")}</legend>
            {offered.map((entry) => (
              <button
                key={entry}
                type="button"
                aria-pressed={scope === entry}
                onClick={() => setScope(entry)}
              >
                {t(`usage.scope.${entry}`)}
              </button>
            ))}
          </fieldset>
        )}
        <label htmlFor={rangeId} className="visually-hidden">
          {t("usage.range")}
        </label>
        <select
          id={rangeId}
          value={range}
          onChange={(event) => setRange(event.target.value as UsageRange)}
        >
          {USAGE_RANGES.map((entry) => (
            <option key={entry} value={entry}>
              {t(`usage.range.${entry}`)}
            </option>
          ))}
        </select>
        <span className="usage-privacy">{t("usage.privacy")}</span>
      </div>
      <div className="usage-body">{body}</div>
    </div>
  );
}
