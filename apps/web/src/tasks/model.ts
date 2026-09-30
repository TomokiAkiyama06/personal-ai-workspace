// What the Task / Agent DAG screens show (PAW-062). The values are the Backend's
// own (apps/backend: tasks/domain.py, tasks/records.py, orchestrator/domain.py);
// the Backend owns every state and decides every command. The Web App only
// shows them and sends the operator's control; it never derives a state itself.
//
// The Backend has no HTTP route for tasks yet: the screens receive their data
// through `TaskSource` (source.tsx), and without one they show the unavailable
// state.

/** tasks/domain.py `TaskState`. */
export type TaskState =
  | "queued"
  | "running"
  | "waiting"
  | "paused"
  | "evaluating"
  | "completed"
  | "failed"
  | "cancelled";

/** tasks/domain.py `WaitReason`: set if and only if the state is `waiting`. */
export type WaitReason = "user" | "approval" | "resource";

/** tasks/domain.py `CONTROL_COMMANDS`: the operator controls. */
export type ControlCommand = "pause" | "resume" | "cancel" | "retry" | "restart" | "stop_now";

/**
 * The controls each state accepts (the transition table of tasks/domain.py),
 * in the order the buttons are shown. Only which buttons to SHOW: the Backend
 * decides whether a command is allowed and answers with the task's new state.
 */
export const ACCEPTED_CONTROLS: Record<TaskState, readonly ControlCommand[]> = {
  queued: ["cancel"],
  running: ["pause", "cancel", "stop_now"],
  waiting: ["cancel", "stop_now"],
  paused: ["resume", "cancel"],
  evaluating: ["cancel", "stop_now"],
  completed: [],
  failed: ["retry", "restart"],
  cancelled: ["restart"],
};

/** orchestrator/domain.py `NodeRole`. */
export type NodeRole = "planner" | "worker" | "researcher" | "reviewer";

/** orchestrator/domain.py `NodeState`. */
export type NodeState =
  | "pending"
  | "ready"
  | "running"
  | "succeeded"
  | "failed"
  | "blocked"
  | "cancelled";

/** orchestrator/domain.py `AttemptState`. */
export type AttemptState = "running" | "succeeded" | "failed" | "interrupted";

/** orchestrator/domain.py `ExecutionPlacement`: where an attempt ran. */
export type Placement = "local_gpu" | "local_cpu" | "cloud";

/** tasks/domain.py `RepoRole` (Decision 0030). */
export type RepoRole = "referenced" | "working" | "target";

/** tasks/records.py `ReviewStatus`. */
export type ReviewStatus = "not_started" | "in_review" | "approved" | "changes_requested";

/** tasks/records.py `EvaluationResult`: tests and the Evaluator. */
export type EvaluationResult = "not_run" | "passed" | "failed";

/** tasks/records.py `PullRequestState`. */
export type PullRequestState = "draft" | "open" | "merged" | "closed";

/**
 * The arguments of a control (TaskService.execute): Stop Now needs the
 * operator's reason (it is kept in the audit); Retry and Restart may switch the
 * agent or model; `expectedVersion` refuses a control on a task that changed.
 */
export interface ControlOptions {
  expectedVersion: number;
  reason?: string;
  agent?: string;
  model?: string;
}

/** The Backend's limit of a control's reason (tasks/service.py MAX_REASON_LENGTH). */
export const MAX_REASON_LENGTH = 500;

/** How often an unfinished task and the list are read again (no push channel yet). */
export const REFRESH_MS = 5000;

/** States that still change by themselves (polled while shown). */
export function isUnsettled(state: TaskState): boolean {
  return state !== "completed" && state !== "failed" && state !== "cancelled";
}

/** Queue priority (tasks/queueing). */
export type Priority = "high" | "normal" | "low";

/** Task budget preset (tasks/queueing/budget.py). */
export type BudgetPreset = "standard" | "long" | "unlimited";

export interface PullRequestRef {
  /** The id of the pull request's record (the PR screen's path). */
  id: string;
  number: number;
  url: string;
  state: PullRequestState;
}

/** One list entry of the task list. */
export interface TaskSummary {
  id: string;
  title: string;
  state: TaskState;
  waitReason: WaitReason | null;
  priority?: Priority;
  /** The repository shown on the card (the target, else the first one). */
  repository?: string;
  /** When the current run started (the runtime of a running task). */
  startedAt?: string;
  updatedAt: string;
}

/** A repository of the task's Working Set with its state in the current attempt. */
export interface TaskRepository {
  name: string;
  role: RepoRole;
  branch: string | null;
  worktree: string | null;
  review: ReviewStatus;
  evaluation: EvaluationResult;
  pullRequest: PullRequestRef | null;
}

export interface NodeAttempt {
  number: number;
  state: AttemptState;
  agent?: string;
  model?: string;
  placement?: Placement;
  /** A closed error class (never the error's text). */
  errorClass?: string | null;
  startedAt: string;
  finishedAt?: string | null;
}

export interface ToolCall {
  id: string;
  name: string;
  status: "started" | "succeeded" | "failed" | "interrupted";
  startedAt: string;
}

export interface DagNode {
  key: string;
  title: string;
  role: NodeRole;
  state: NodeState;
  required: boolean;
  dependsOn: readonly string[];
  agent?: string;
  model?: string;
  /** Oldest first. */
  attempts: readonly NodeAttempt[];
  /** The tool calls of the node's running step, oldest first. */
  toolCalls?: readonly ToolCall[];
}

export interface TaskDetail extends TaskSummary {
  /** The task's version: sent back with a control as the expected version. */
  version: number;
  project?: string;
  agent: string | null;
  model: string | null;
  attempt: number;
  retryCount: number;
  currentStep: { name: string; startedAt: string } | null;
  budget?: { preset: BudgetPreset; remainingPercent?: number };
  repositories: readonly TaskRepository[];
  /** The nodes of the current DAG in the orchestrator's order; null before planning. */
  dag: readonly DagNode[] | null;
}

export interface TaskCapacity {
  parallelLimit: number;
  vramUsedGb?: number;
  vramTotalGb?: number;
}

export interface TaskList {
  tasks: readonly TaskSummary[];
  capacity?: TaskCapacity;
}

/** A pull request a task delivered (integration/publish.py records it on the attempt). */
export interface PullRequestRecord extends PullRequestRef {
  title: string;
  taskId: string;
  taskTitle: string;
  repository: string;
  branch: string;
  base: string;
  review: ReviewStatus;
  evaluation: EvaluationResult;
  /** Merge Ready as the Backend judges it (the Web App does not derive it). */
  mergeReady: boolean;
}

/** Runtime as the design writes it: "42s", "4m 12s", "1h 03m". */
export function formatDuration(milliseconds: number): string {
  const total = Math.max(0, Math.floor(milliseconds / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (hours > 0) return `${hours}h ${String(minutes).padStart(2, "0")}m`;
  if (minutes > 0) return `${minutes}m ${String(seconds).padStart(2, "0")}s`;
  return `${seconds}s`;
}

/** The time between two instants (`end` defaults to `now`); null if unknown. */
export function elapsed(start: string | undefined, end: string | null | undefined, now: number) {
  if (!start) return null;
  const from = Date.parse(start);
  const to = end ? Date.parse(end) : now;
  if (Number.isNaN(from) || Number.isNaN(to)) return null;
  return formatDuration(to - from);
}

/** A task id is a UUID; the screens show its first 8 characters. */
export function shortId(id: string): string {
  return id.replace(/-/g, "").slice(0, 8);
}

export interface NodePosition {
  node: DagNode;
  column: number;
  row: number;
}

/**
 * Columns by dependency depth (the longest path from a node without
 * dependencies), rows in the orchestrator's order within a column. A dependency
 * on an unknown key is ignored; a cycle (the Backend refuses one) cannot hang it.
 */
export function layoutDag(nodes: readonly DagNode[]): NodePosition[] {
  const byKey = new Map(nodes.map((node) => [node.key, node]));
  const depth = new Map<string, number>();
  const visiting = new Set<string>();
  const depthOf = (node: DagNode): number => {
    const known = depth.get(node.key);
    if (known !== undefined) return known;
    if (visiting.has(node.key)) return 0;
    visiting.add(node.key);
    let value = 0;
    for (const key of node.dependsOn) {
      const parent = byKey.get(key);
      if (parent) value = Math.max(value, depthOf(parent) + 1);
    }
    visiting.delete(node.key);
    depth.set(node.key, value);
    return value;
  };
  const rows = new Map<number, number>();
  return nodes.map((node) => {
    const column = depthOf(node);
    const row = rows.get(column) ?? 0;
    rows.set(column, row + 1);
    return { node, column, row };
  });
}

/** The nodes in dependency order (the phone's step list). */
export function orderedNodes(nodes: readonly DagNode[]): DagNode[] {
  return layoutDag(nodes)
    .map((position, index) => ({ ...position, index }))
    .sort((a, b) => a.column - b.column || a.row - b.row || a.index - b.index)
    .map((position) => position.node);
}
