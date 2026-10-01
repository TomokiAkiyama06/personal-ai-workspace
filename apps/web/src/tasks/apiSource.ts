// The Backend's task API (/api/v1/tasks, /api/v1/pull-requests; issue #185,
// Decision 0067 Proposed) as the screens' TaskSource. The Backend decides every
// state, permission and Merge Ready; this only renames the fields of its answers
// (snake_case) to the screens' types.
import { apiRequest } from "../api/client";
import type {
  AttemptState,
  BudgetPreset,
  ControlCommand,
  ControlOptions,
  DagNode,
  EvaluationResult,
  NodeAttempt,
  NodeRole,
  NodeState,
  Placement,
  Priority,
  PullRequestRecord,
  PullRequestState,
  RepoRole,
  ReviewStatus,
  TaskDetail,
  TaskList,
  TaskRepository,
  TaskState,
  TaskSummary,
  ToolCall,
  WaitReason,
} from "./model";
import type { TaskSource } from "./source";

interface TaskSummaryWire {
  id: string;
  title: string;
  state: TaskState;
  wait_reason: WaitReason | null;
  priority: Priority | null;
  project_id: string;
  project_name: string;
  repository: string | null;
  started_at: string | null;
  updated_at: string;
}

interface NodeAttemptWire {
  number: number;
  state: AttemptState;
  agent: string | null;
  model: string | null;
  placement: Placement | null;
  error_class: string | null;
  started_at: string;
  finished_at: string | null;
}

interface DagNodeWire {
  key: string;
  title: string;
  role: NodeRole;
  state: NodeState;
  required: boolean;
  depends_on: string[];
  agent: string | null;
  model: string | null;
  attempts: NodeAttemptWire[];
}

interface RepositoryWire {
  repository_id: string;
  name: string;
  role: RepoRole;
  branch: string | null;
  worktree: string | null;
  review: ReviewStatus;
  evaluation: EvaluationResult;
  pull_request: { id: string; number: number; url: string; state: PullRequestState } | null;
}

export interface TaskDetailWire extends TaskSummaryWire {
  version: number;
  created_by: string;
  agent: string | null;
  model: string | null;
  attempt: number;
  retry_count: number;
  current_step: {
    name: string;
    status: "running" | "succeeded" | "failed" | "interrupted";
    started_at: string;
    finished_at: string | null;
    tool_calls: {
      id: string;
      name: string;
      status: ToolCall["status"];
      started_at: string;
      finished_at: string | null;
    }[];
  } | null;
  budget: {
    preset: BudgetPreset;
    usage: { kind: string; consumed: number; limit: number | null }[];
  } | null;
  repositories: RepositoryWire[];
  dag: DagNodeWire[] | null;
}

interface PullRequestWire {
  id: string;
  number: number;
  url: string;
  state: PullRequestState;
  title: string;
  task_id: string;
  task_title: string;
  repository: string;
  branch: string | null;
  base: string;
  review: ReviewStatus;
  evaluation: EvaluationResult;
  merge_ready: boolean;
  updated_at: string;
}

function summary(wire: TaskSummaryWire): TaskSummary {
  return {
    id: wire.id,
    title: wire.title,
    state: wire.state,
    waitReason: wire.wait_reason,
    priority: wire.priority ?? undefined,
    repository: wire.repository ?? undefined,
    startedAt: wire.started_at ?? undefined,
    updatedAt: wire.updated_at,
  };
}

/**
 * What is left of the budget, in percent: the smallest share left of the items
 * that have a limit (undefined when none has one: the Unlimited preset).
 */
export function remainingPercent(
  usage: readonly { consumed: number; limit: number | null }[],
): number | undefined {
  let lowest: number | undefined;
  for (const item of usage) {
    if (item.limit === null) continue;
    const left = item.limit === 0 ? 0 : Math.max(0, item.limit - item.consumed) / item.limit;
    const percent = Math.floor(left * 100);
    lowest = lowest === undefined ? percent : Math.min(lowest, percent);
  }
  return lowest;
}

function attempt(wire: NodeAttemptWire): NodeAttempt {
  return {
    number: wire.number,
    state: wire.state,
    agent: wire.agent ?? undefined,
    model: wire.model ?? undefined,
    placement: wire.placement ?? undefined,
    errorClass: wire.error_class,
    startedAt: wire.started_at,
    finishedAt: wire.finished_at,
  };
}

function node(wire: DagNodeWire): DagNode {
  return {
    key: wire.key,
    title: wire.title,
    role: wire.role,
    state: wire.state,
    required: wire.required,
    dependsOn: wire.depends_on,
    agent: wire.agent ?? undefined,
    model: wire.model ?? undefined,
    attempts: wire.attempts.map(attempt),
  };
}

function repository(wire: RepositoryWire): TaskRepository {
  return {
    name: wire.name,
    role: wire.role,
    branch: wire.branch,
    worktree: wire.worktree,
    review: wire.review,
    evaluation: wire.evaluation,
    pullRequest: wire.pull_request,
  };
}

export function taskDetail(wire: TaskDetailWire): TaskDetail {
  const step = wire.current_step;
  return {
    ...summary(wire),
    version: wire.version,
    project: wire.project_name,
    agent: wire.agent,
    model: wire.model,
    attempt: wire.attempt,
    retryCount: wire.retry_count,
    // Only a step that runs is "the current step" (a finished one has no runtime).
    currentStep:
      step && step.status === "running"
        ? {
            name: step.name,
            startedAt: step.started_at,
            toolCalls: step.tool_calls.map((call) => ({
              id: call.id,
              name: call.name,
              status: call.status,
              startedAt: call.started_at,
            })),
          }
        : null,
    budget: wire.budget
      ? { preset: wire.budget.preset, remainingPercent: remainingPercent(wire.budget.usage) }
      : undefined,
    repositories: wire.repositories.map(repository),
    dag: wire.dag ? wire.dag.map(node) : null,
  };
}

function pullRequest(wire: PullRequestWire): PullRequestRecord {
  return {
    id: wire.id,
    number: wire.number,
    url: wire.url,
    state: wire.state,
    title: wire.title,
    taskId: wire.task_id,
    taskTitle: wire.task_title,
    repository: wire.repository,
    branch: wire.branch ?? "",
    base: wire.base,
    review: wire.review,
    evaluation: wire.evaluation,
    mergeReady: wire.merge_ready,
  };
}

const taskPath = (id: string) => `/tasks/${encodeURIComponent(id)}`;

/** The production source: the Backend's /api/v1 task routes. */
export const apiTaskSource: TaskSource = {
  async listTasks(): Promise<TaskList> {
    const body = await apiRequest<{ tasks: TaskSummaryWire[] }>("GET", "/tasks");
    return { tasks: body.tasks.map(summary) };
  },
  async getTask(id: string): Promise<TaskDetail> {
    return taskDetail(await apiRequest<TaskDetailWire>("GET", taskPath(id)));
  },
  async control(id: string, command: ControlCommand, options: ControlOptions): Promise<TaskDetail> {
    const body = {
      command,
      expected_version: options.expectedVersion,
      ...(options.reason !== undefined && { reason: options.reason }),
      ...(options.agent !== undefined && { agent: options.agent }),
      ...(options.model !== undefined && { model: options.model }),
    };
    return taskDetail(await apiRequest<TaskDetailWire>("POST", `${taskPath(id)}/controls`, body));
  },
  async listPullRequests(): Promise<readonly PullRequestRecord[]> {
    const body = await apiRequest<{ pull_requests: PullRequestWire[] }>("GET", "/pull-requests");
    return body.pull_requests.map(pullRequest);
  },
};
