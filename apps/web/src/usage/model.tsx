// The usage / quota screen's data (PAW-064, the design's Usage board; Decision
// 0016 for the quota rules). The screen takes its data through `UsageSource`:
// the app plugs in `apiUsageSource` (GET /api/v1/usage, issue #187, `./api.ts`);
// without a source it shows that usage is not available.
//
// Privacy (docs/REQUIREMENTS.md, Decision 0016 §6): a report carries counts, closed
// purpose categories, IDs and login names only. It has no prompt, answer, chat or
// memory text, so the screen cannot show any.
import { createContext, type ReactNode, useContext } from "react";

/** 自分 (own usage, `agent.use`) or ワークスペース (every user, `admin.usage.view`). */
export type UsageScope = "self" | "workspace";
export const USAGE_SCOPES: readonly UsageScope[] = ["self", "workspace"];

/** The design's period menu: 直近 14 日 / 直近 30 日 / 今月 (calendar month, Asia/Tokyo). */
export type UsageRange = "last14" | "last30" | "month";
export const USAGE_RANGES: readonly UsageRange[] = ["last14", "last30", "month"];

/** The agents the screen compares; the order and colors are the design's. */
export type AgentKind = "local" | "codex" | "claude";
export const AGENT_KINDS: readonly AgentKind[] = ["local", "codex", "claude"];

/** The closed usage categories of Decision 0016 §6 (never free text). */
export type UsagePurpose = "chat" | "coding" | "review" | "research" | "evaluation" | "other";
export const USAGE_PURPOSES: readonly UsagePurpose[] = [
  "chat",
  "coding",
  "review",
  "research",
  "evaluation",
  "other",
];

// The Backend's quota enums (paw_backend.connections.domain).
export type QuotaKind = "codex" | "claude";
export type QuotaMetric = "requests" | "tasks" | "tokens" | "runtime_seconds";
export type QuotaPeriod = "rolling_5h" | "day" | "week" | "month";
/** The explicit "no limit" (Decision 0016 §2); a quota that is not set is unlimited too. */
export const UNLIMITED = "unlimited";

/** One quota with what its current window has used (the Backend's QuotaUsage). */
export interface QuotaUsage {
  kind: QuotaKind;
  metric: QuotaMetric;
  period: QuotaPeriod;
  limit: number | typeof UNLIMITED;
  /** In the metric's unit (runtime_seconds: whole seconds). */
  used: number;
  window_start: string;
  /** When the window resets; null for the rolling 5 hours. */
  window_end: string | null;
}

export interface DailyTasks {
  /** A calendar day (YYYY-MM-DD, Asia/Tokyo). */
  date: string;
  local: number;
  codex: number;
  claude: number;
}

export interface AgentTotal {
  agent: AgentKind;
  tasks: number;
  tokens: number;
}

export interface PurposeTotal {
  /** A category of Decision 0016; anything else is counted as その他. */
  purpose: string;
  tasks: number;
  tokens: number;
}

export interface UserUsage {
  user_id: string;
  login_name: string;
  system_role: string;
  tasks: number;
  tokens: number;
  /** The user's quotas; none set means unlimited (Decision 0016 §2). */
  quotas: QuotaUsage[];
}

export interface UsageReport {
  tasks: number;
  /** The tasks of the period of the same length just before, when known. */
  previous_tasks: number | null;
  tokens: { local: number | null; external: number };
  /** GPU time of the local agent in seconds; null when not recorded. */
  gpu_seconds: number | null;
  /** Escalated tasks by cause; null when not recorded. */
  escalations: { failed: number; loop_detected: number } | null;
  daily: DailyTasks[];
  agents: AgentTotal[];
  purposes: PurposeTotal[];
  /** The viewer's own quotas (scope 自分). */
  quotas: QuotaUsage[];
  /** Every user (scope ワークスペース). */
  users: UserUsage[];
}

export interface UsageSource {
  load(scope: UsageScope, range: UsageRange): Promise<UsageReport>;
}

const UsageSourceContext = createContext<UsageSource | null>(null);

export function UsageSourceProvider({
  source,
  children,
}: {
  source: UsageSource | null;
  children: ReactNode;
}) {
  return <UsageSourceContext.Provider value={source}>{children}</UsageSourceContext.Provider>;
}

/** The connected source, or null (none plugged in: usage is not available). */
export function useUsageSource(): UsageSource | null {
  return useContext(UsageSourceContext);
}

// ---------- Derived values ----------

/** How full a numeric quota is (0..1+), or null when it is unlimited. */
export function quotaRatio(quota: QuotaUsage): number | null {
  if (quota.limit === UNLIMITED) return null;
  if (quota.limit <= 0) return 1;
  return quota.used / quota.limit;
}

export type QuotaLevel = "ok" | "near" | "reached" | "unlimited";

/** The design's thresholds: amber from 80% (上限に接近), reached at the limit. */
export const NEAR_RATIO = 0.8;

export function quotaLevel(quota: QuotaUsage): QuotaLevel {
  const ratio = quotaRatio(quota);
  if (ratio === null) return "unlimited";
  if (ratio >= 1) return "reached";
  if (ratio >= NEAR_RATIO) return "near";
  return "ok";
}

const LEVEL_ORDER: Record<QuotaLevel, number> = { unlimited: 0, ok: 1, near: 2, reached: 3 };

/** The worst level of a user's quotas: none set is unlimited (Decision 0016 §2). */
export function worstLevel(quotas: readonly QuotaUsage[]): QuotaLevel {
  let worst: QuotaLevel = "unlimited";
  for (const quota of quotas) {
    const level = quotaLevel(quota);
    if (LEVEL_ORDER[level] > LEVEL_ORDER[worst]) worst = level;
  }
  return worst;
}

/** The quota a one-line summary shows: the fullest numeric one, or null (unlimited). */
export function headlineQuota(quotas: readonly QuotaUsage[]): QuotaUsage | null {
  let best: QuotaUsage | null = null;
  let bestRatio = -1;
  for (const quota of quotas) {
    const ratio = quotaRatio(quota);
    if (ratio !== null && ratio > bestRatio) {
      best = quota;
      bestRatio = ratio;
    }
  }
  return best;
}

/** Totals per category of Decision 0016, unknown values folded into その他. */
export function purposeTotals(purposes: readonly PurposeTotal[]): PurposeTotal[] {
  const totals = new Map<UsagePurpose, PurposeTotal>();
  for (const entry of purposes) {
    const purpose = (USAGE_PURPOSES as readonly string[]).includes(entry.purpose)
      ? (entry.purpose as UsagePurpose)
      : "other";
    const total = totals.get(purpose) ?? { purpose, tasks: 0, tokens: 0 };
    total.tasks += entry.tasks;
    total.tokens += entry.tokens;
    totals.set(purpose, total);
  }
  return USAGE_PURPOSES.flatMap((purpose) => {
    const total = totals.get(purpose);
    return total ? [total] : [];
  });
}

// ---------- Formatting (the design's compact figures) ----------

function trimmed(value: number): string {
  return value.toFixed(1).replace(/\.0$/, "");
}

/** 157 / 12.4K / 9.7M / 10M */
export function formatCount(value: number): string {
  const abs = Math.abs(value);
  if (abs >= 1_000_000_000) return `${trimmed(value / 1_000_000_000)}B`;
  if (abs >= 1_000_000) return `${trimmed(value / 1_000_000)}M`;
  if (abs >= 10_000) return `${trimmed(value / 1_000)}K`;
  return String(Math.round(value));
}

/** 45s / 4m 28s / 11h 42m / 40h */
export function formatDuration(totalSeconds: number): string {
  const seconds = Math.max(0, Math.floor(totalSeconds));
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) {
    const rest = seconds % 60;
    return rest === 0 ? `${Math.floor(seconds / 60)}m` : `${Math.floor(seconds / 60)}m ${rest}s`;
  }
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return minutes === 0 ? `${hours}h` : `${hours}h ${minutes}m`;
}

/** A quota amount in its metric's unit. */
export function formatMetric(metric: QuotaMetric, value: number): string {
  return metric === "runtime_seconds" ? formatDuration(value) : formatCount(value);
}

/** "9/17" of a YYYY-MM-DD day. */
export function shortDay(date: string): string {
  const [, month, day] = date.split("-");
  return month && day ? `${Number(month)}/${Number(day)}` : date;
}

/** "2026/09/17" of a YYYY-MM-DD day. */
export function longDay(date: string): string {
  return date.replaceAll("-", "/");
}
