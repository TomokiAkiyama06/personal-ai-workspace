// Where the Task / PR screens get their data. The app plugs in the Backend's
// /api/v1 task routes (apiSource.ts, issue #185); tests plug in a fake. Without a
// source the screens show that the data is not available (as the Notification
// Center's `NotificationSource`, Decision 0044's 11).
import { createContext, type ReactNode, useContext } from "react";
import type {
  ControlCommand,
  ControlOptions,
  PullRequestRecord,
  TaskDetail,
  TaskList,
} from "./model";

export interface TaskSource {
  listTasks(): Promise<TaskList>;
  getTask(id: string): Promise<TaskDetail>;
  /** Send an operator control; resolves with the task as the Backend left it. */
  control(id: string, command: ControlCommand, options: ControlOptions): Promise<TaskDetail>;
  listPullRequests(): Promise<readonly PullRequestRecord[]>;
  /** One record by its id: the PR screen opens one the bounded list does not hold. */
  getPullRequest(id: string): Promise<PullRequestRecord>;
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
