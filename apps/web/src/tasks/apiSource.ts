// The Backend's task API (/api/v1/tasks, /api/v1/pull-requests; issue #185,
// Decision 0067 Approved) as the screens' TaskSource. The Backend decides every
// state, permission and Merge Ready; this only renames the fields of its answers
// (snake_case) to the screens' types.
import { apiRequest } from "../api/client";
import type {
  ApprovalDecision,
  ApprovalLevel,
  AttemptState,
  AuditRow,
  BudgetPreset,
  ChangedFile,
  ChangedFiles,
  ControlCommand,
  ControlOptions,
  DagNode,
  EvaluationResult,
  FileDiff,
  NodeAttempt,
  NodeRole,
  NodeState,
  Placement,
  Priority,
  PullRequestRecord,
  PullRequestState,
  RepoRole,
  ReviewStatus,
  ReviewSummary,
  TaskCapacity,
  TaskDetail,
  TaskList,
  TaskRepository,
  TaskState,
  TaskSummary,
  ToolApproval,
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

interface TaskCapacityWire {
  parallel_limit: number;
  running: number;
  vram_used_bytes: number | null;
  vram_total_bytes: number | null;
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

const GB = 1024 ** 3;

/** 18.2 of a byte count: the GB the header shows, one decimal (as System Health). */
function gigabytes(bytes: number): number {
  return Math.round((bytes / GB) * 10) / 10;
}

// The scheduler's parallel limit (Decision 0084); the VRAM only comes
// to a person who may see System Health's detail, and only from a fresh reading.
function capacity(wire: TaskCapacityWire | null | undefined): TaskCapacity | undefined {
  if (!wire) return undefined;
  const vram =
    wire.vram_used_bytes !== null && wire.vram_total_bytes !== null && wire.vram_total_bytes > 0
      ? {
          vramUsedGb: gigabytes(wire.vram_used_bytes),
          vramTotalGb: gigabytes(wire.vram_total_bytes),
        }
      : {};
  return { parallelLimit: wire.parallel_limit, ...vram };
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

interface ChangedFileWire {
  index: number;
  path: string;
  previous_path: string | null;
  status: string;
  additions: number;
  deletions: number;
  has_patch: boolean;
  patch_truncated: boolean;
}

interface ChangesWire {
  recorded: boolean;
  head_commit: string | null;
  truncated: boolean;
  additions: number;
  deletions: number;
  files: ChangedFileWire[];
}

interface FileDiffWire extends ChangedFileWire {
  count: number;
  patch: string | null;
}

interface ReviewWire {
  review: ReviewStatus;
  evaluation: EvaluationResult;
  reviewers: {
    key: string;
    title: string;
    state: NodeState;
    agent: string | null;
    model: string | null;
    finished_at: string | null;
  }[];
}

interface AuditWire {
  rows: {
    occurred_at: string;
    action: string;
    decision: "allow" | "deny";
    reason: string;
    actor: AuditRow["actor"];
  }[];
}

interface ApprovalWire {
  id: string;
  task_id: string;
  task_title: string;
  agent: string | null;
  project_id: string;
  tool: string;
  level: ApprovalLevel;
  summary: { name: string; kind: string; value: string }[];
  repositories: string[];
  created_at: string;
  expires_at: string;
}

function changedFile(wire: ChangedFileWire): ChangedFile {
  return {
    index: wire.index,
    path: wire.path,
    previousPath: wire.previous_path,
    status: wire.status,
    additions: wire.additions,
    deletions: wire.deletions,
    hasPatch: wire.has_patch,
    patchTruncated: wire.patch_truncated,
  };
}

function approval(wire: ApprovalWire): ToolApproval {
  return {
    id: wire.id,
    taskId: wire.task_id,
    taskTitle: wire.task_title,
    agent: wire.agent,
    tool: wire.tool,
    level: wire.level,
    summary: wire.summary,
    repositories: wire.repositories,
    createdAt: wire.created_at,
    expiresAt: wire.expires_at,
  };
}

const taskPath = (id: string) => `/tasks/${encodeURIComponent(id)}`;
const pullPath = (id: string) => `/pull-requests/${encodeURIComponent(id)}`;

/** The production source: the Backend's /api/v1 task routes. */
export const apiTaskSource: TaskSource = {
  async listTasks(): Promise<TaskList> {
    const body = await apiRequest<{ tasks: TaskSummaryWire[]; capacity?: TaskCapacityWire | null }>(
      "GET",
      "/tasks",
    );
    const limits = capacity(body.capacity);
    return limits
      ? { tasks: body.tasks.map(summary), capacity: limits }
      : { tasks: body.tasks.map(summary) };
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
  async getPullRequest(id: string): Promise<PullRequestRecord> {
    return pullRequest(await apiRequest<PullRequestWire>("GET", pullPath(id)));
  },
  async getChangedFiles(id: string): Promise<ChangedFiles> {
    const body = await apiRequest<ChangesWire>("GET", `${pullPath(id)}/files`);
    return {
      recorded: body.recorded,
      truncated: body.truncated,
      additions: body.additions,
      deletions: body.deletions,
      files: body.files.map(changedFile),
    };
  },
  async getFileDiff(id: string, index: number): Promise<FileDiff> {
    const body = await apiRequest<FileDiffWire>("GET", `${pullPath(id)}/files/${index}`);
    return { ...changedFile(body), count: body.count, patch: body.patch };
  },
  async getReview(id: string): Promise<ReviewSummary> {
    const body = await apiRequest<ReviewWire>("GET", `${pullPath(id)}/review`);
    return {
      review: body.review,
      evaluation: body.evaluation,
      reviewers: body.reviewers.map((reviewer) => ({
        key: reviewer.key,
        title: reviewer.title,
        state: reviewer.state,
        agent: reviewer.agent,
        model: reviewer.model,
        finishedAt: reviewer.finished_at,
      })),
    };
  },
  async getAudit(id: string): Promise<readonly AuditRow[]> {
    const body = await apiRequest<AuditWire>("GET", `${pullPath(id)}/audit`);
    return body.rows.map((row) => ({
      occurredAt: row.occurred_at,
      action: row.action,
      decision: row.decision,
      reason: row.reason,
      actor: row.actor,
    }));
  },
  async listApprovals(taskId?: string): Promise<readonly ToolApproval[]> {
    const query = taskId === undefined ? "" : `?task_id=${encodeURIComponent(taskId)}`;
    const body = await apiRequest<{ approvals: ApprovalWire[] }>("GET", `/approvals${query}`);
    return body.approvals.map(approval);
  },
  async getApproval(id: string): Promise<ToolApproval> {
    return approval(await apiRequest<ApprovalWire>("GET", `/approvals/${encodeURIComponent(id)}`));
  },
  async decideApproval(id: string, decision: ApprovalDecision): Promise<void> {
    await apiRequest("POST", `/approvals/${encodeURIComponent(id)}/decision`, { decision });
  },
};
