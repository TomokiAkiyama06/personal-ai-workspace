// What the Task / Agent DAG screens show (PAW-062). The values are the Backend's
// own (apps/backend: tasks/domain.py, tasks/records.py, orchestrator/domain.py);
// the Backend owns every state and decides every command. The Web App only
// shows them and sends the operator's control; it never derives a state itself.
//
// The screens receive their data through `TaskSource` (source.tsx); the app
// plugs in the Backend's /api/v1 task routes (apiSource.ts, issue #185), and
// without a source they show the unavailable state.

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

/** The Backend's limit of an agent / model name (tasks/service.py MAX_NAME_LENGTH). */
export const MAX_NAME_LENGTH = 100;

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
}

export interface TaskDetail extends TaskSummary {
  /** The task's version: sent back with a control as the expected version. */
  version: number;
  project?: string;
  agent: string | null;
  model: string | null;
  attempt: number;
  retryCount: number;
  /**
   * The step that runs now with its tool calls (oldest first). A tool call
   * belongs to the task's step, not to a DAG node (the Backend records no link).
   */
  currentStep: { name: string; startedAt: string; toolCalls?: readonly ToolCall[] } | null;
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

/** A route segment decoded, or null when it is not valid percent-encoding. */
export function decodeSegment(segment: string): string | null {
  try {
    return decodeURIComponent(segment);
  } catch {
    return null;
  }
}

// -- The PR screen's panels and the mobile boards (issue #185 item 6, Decision
// 0078): the Backend's /api/v1/pull-requests/{id}/files|review|audit and
// /api/v1/approvals.

/** A file the pull request changes, as recorded when it was delivered. */
export interface ChangedFile {
  index: number;
  path: string;
  /** The path before a rename or copy. */
  previousPath: string | null;
  /** GitHub's: added, removed, modified, renamed, copied, changed, unchanged. */
  status: string;
  additions: number;
  deletions: number;
  /** Whether its diff was kept (none for a binary or very large file). */
  hasPatch: boolean;
  patchTruncated: boolean;
}

export interface ChangedFiles {
  /** False: the files were not read when the pull request was delivered. */
  recorded: boolean;
  /** GitHub listed more files than were kept. */
  truncated: boolean;
  additions: number;
  deletions: number;
  files: readonly ChangedFile[];
}

/** One changed file with its unified diff (the MobileDiff board). */
export interface FileDiff extends ChangedFile {
  /** How many files were kept ("1 / 4"). */
  count: number;
  patch: string | null;
}

/** A reviewer node of the DAG of the pull request's attempt. */
export interface Reviewer {
  key: string;
  title: string;
  state: NodeState;
  agent: string | null;
  model: string | null;
  finishedAt: string | null;
}

export interface ReviewSummary {
  review: ReviewStatus;
  evaluation: EvaluationResult;
  reviewers: readonly Reviewer[];
}

/** One audit row of the pull request's task (a closed projection). */
export interface AuditRow {
  occurredAt: string;
  action: string;
  decision: "allow" | "deny";
  reason: string;
  actor: "agent" | "person" | "system";
}

/** tools/approval_types.py: the levels a human approves. */
export type ApprovalLevel = "approval" | "strong_approval";

/** A pending tool approval the person is asked for (the MobileApproval board). */
export interface ToolApproval {
  id: string;
  taskId: string;
  taskTitle: string;
  agent: string | null;
  tool: string;
  level: ApprovalLevel;
  /** Every argument of the call as the approver sees it (bounded, redacted). */
  summary: readonly { name: string; kind: string; value: string }[];
  /** The repositories the call names that the person may read. */
  repositories: readonly string[];
  createdAt: string;
  expiresAt: string;
}

export type ApprovalDecision = "approve" | "reject";

export type DiffLineKind = "hunk" | "add" | "delete" | "context" | "note";

/** A unified diff's lines, by kind (the MobileDiff board's colours). */
export function diffLines(patch: string): { kind: DiffLineKind; text: string }[] {
  const lines = patch.endsWith("\n") ? patch.slice(0, -1).split("\n") : patch.split("\n");
  return lines.map((text) => {
    if (text.startsWith("@@")) return { kind: "hunk", text };
    if (text.startsWith("+")) return { kind: "add", text };
    if (text.startsWith("-")) return { kind: "delete", text };
    if (text.startsWith("\\")) return { kind: "note", text };
    return { kind: "context", text };
  });
}

/** The approvals screen (MobileApproval) and one approval's sheet on it. */
export const APPROVALS_PATH = "/approvals";

export function approvalPath(id: string): string {
  return `${APPROVALS_PATH}/${encodeURIComponent(id)}`;
}
