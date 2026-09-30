// Where the Task / PR screens get their data. The Backend has no HTTP route for
// tasks, DAGs or pull request records yet (only the Python services), so the
// app does not plug a source in: the screens then show that the data is not
// available. A later issue connects the Backend's API here without changing the
// screens (as the Notification Center's `NotificationSource`, Decision 0044's 11).
import { createContext, type ReactNode, useContext } from "react";
import type { ControlCommand, PullRequestRecord, TaskDetail, TaskList } from "./model";

export interface TaskSource {
  listTasks(): Promise<TaskList>;
  getTask(id: string): Promise<TaskDetail>;
  /** Send an operator control; resolves with the task as the Backend left it. */
  control(id: string, command: ControlCommand): Promise<TaskDetail>;
  listPullRequests(): Promise<readonly PullRequestRecord[]>;
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
