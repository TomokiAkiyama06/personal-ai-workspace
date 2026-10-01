// The production UsageSource: GET /api/v1/usage (issue #187, Decision 0069). The
// Backend decides who may load which scope (`agent.use` for one's own,
// `admin.usage.view` for the workspace) and answers with the shape of
// `UsageReport`; this keeps only the fields the screen reads, so a field the
// Backend adds later (or one it already sends, like the period's instants) is
// never shown by accident.
import { apiRequest } from "../api/client";
import type {
  AgentTotal,
  DailyTasks,
  PurposeTotal,
  QuotaUsage,
  UsageRange,
  UsageReport,
  UsageScope,
  UsageSource,
  UserUsage,
} from "./model";

function quota(value: QuotaUsage): QuotaUsage {
  return {
    kind: value.kind,
    metric: value.metric,
    period: value.period,
    limit: value.limit,
    used: value.used,
    window_start: value.window_start,
    window_end: value.window_end,
  };
}

/** The Backend's report as the screen's `UsageReport`. */
export function toUsageReport(body: UsageReport): UsageReport {
  return {
    tasks: body.tasks,
    previous_tasks: body.previous_tasks,
    tokens: { local: body.tokens.local, external: body.tokens.external },
    gpu_seconds: body.gpu_seconds,
    escalations: body.escalations
      ? { failed: body.escalations.failed, loop_detected: body.escalations.loop_detected }
      : null,
    daily: body.daily.map(
      (day): DailyTasks => ({
        date: day.date,
        local: day.local,
        codex: day.codex,
        claude: day.claude,
      }),
    ),
    agents: body.agents.map(
      (entry): AgentTotal => ({ agent: entry.agent, tasks: entry.tasks, tokens: entry.tokens }),
    ),
    purposes: body.purposes.map(
      (entry): PurposeTotal => ({
        purpose: entry.purpose,
        tasks: entry.tasks,
        tokens: entry.tokens,
      }),
    ),
    quotas: body.quotas.map(quota),
    users: body.users.map(
      (user): UserUsage => ({
        user_id: user.user_id,
        login_name: user.login_name,
        system_role: user.system_role,
        tasks: user.tasks,
        tokens: user.tokens,
        quotas: user.quotas.map(quota),
      }),
    ),
  };
}

export const apiUsageSource: UsageSource = {
  async load(scope: UsageScope, range: UsageRange): Promise<UsageReport> {
    const query = new URLSearchParams({ scope, range });
    return toUsageReport(await apiRequest<UsageReport>("GET", `/usage?${query}`));
  },
};
