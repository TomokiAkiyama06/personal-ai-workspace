// Where the Task / PR screens get their data. The app plugs in the Backend's
// /api/v1 task routes (apiSource.ts, issue #185); tests plug in a fake. Without a
// source the screens show that the data is not available (as the Notification
// Center's `NotificationSource`, Decision 0044's 11).
import { createContext, type ReactNode, useContext } from "react";
import type {
  ApprovalDecision,
  AuditRow,
  ChangedFiles,
  ControlCommand,
  ControlOptions,
  FileDiff,
  PullRequestRecord,
  ReviewSummary,
  TaskDetail,
  TaskList,
  ToolApproval,
} from "./model";

export interface TaskSource {
  listTasks(): Promise<TaskList>;
  getTask(id: string): Promise<TaskDetail>;
  /** Send an operator control; resolves with the task as the Backend left it. */
  control(id: string, command: ControlCommand, options: ControlOptions): Promise<TaskDetail>;
  listPullRequests(): Promise<readonly PullRequestRecord[]>;
  /** One record by its id: the PR screen opens one the bounded list does not hold. */
  getPullRequest(id: string): Promise<PullRequestRecord>;
  /** The files the pull request changes, as recorded when it was delivered. */
  getChangedFiles(id: string): Promise<ChangedFiles>;
  /** One of them with its diff. */
  getFileDiff(id: string, index: number): Promise<FileDiff>;
  getReview(id: string): Promise<ReviewSummary>;
  /** The newest audit rows of the pull request's task. */
  getAudit(id: string): Promise<readonly AuditRow[]>;
  /** The pending tool approvals the person is asked for (of one task, if given). */
  listApprovals(taskId?: string): Promise<readonly ToolApproval[]>;
  /** One of them by its id: the sheet opens one the bounded list does not hold. */
  getApproval(id: string): Promise<ToolApproval>;
  /** Approve or reject one; resolves when the Backend stored the decision. */
  decideApproval(id: string, decision: ApprovalDecision): Promise<void>;
}

const TaskSourceContext = createContext<TaskSource | null>(null);

export function TaskSourceProvider({
  source,
  children,
}: {
  source?: TaskSource;
  children: ReactNode;
}) {
  return <TaskSourceContext.Provider value={source ?? null}>{children}</TaskSourceContext.Provider>;
}

/** The connected source, or null (the screens show the unavailable state). */
export function useTaskSource(): TaskSource | null {
  return useContext(TaskSourceContext);
}
