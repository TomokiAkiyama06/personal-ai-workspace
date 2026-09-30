// A TaskSource for tests (and local screenshots) with the design's example data:
// the Tasks board's #203 (running, backend target + ios referenced), #204
// (waiting for approval), #205 (waiting for a resource) and #201 (completed).
import { ApiError } from "../api/client";
import type {
  ControlCommand,
  ControlOptions,
  PullRequestRecord,
  TaskDetail,
  TaskList,
  TaskState,
} from "../tasks/model";
import type { TaskSource } from "../tasks/source";

export const TASK_203 = "2030c0de-0000-4000-8000-000000000203";
export const TASK_204 = "2040c0de-0000-4000-8000-000000000204";
export const TASK_205 = "2050c0de-0000-4000-8000-000000000205";
export const TASK_201 = "2010c0de-0000-4000-8000-000000000201";
export const PR_42 = "pr-42";

/** Minutes before `now` as an ISO time. */
function ago(now: number, seconds: number): string {
  return new Date(now - seconds * 1000).toISOString();
}

export function sampleTasks(now = Date.now()): TaskDetail[] {
  const base = {
    version: 3,
    agent: null,
    model: null,
    attempt: 1,
    retryCount: 0,
    currentStep: null,
    dag: null,
    project: "ExampleProject",
  };
  return [
    {
      ...base,
      id: TASK_203,
      title: "認証セッションの修正",
      state: "running",
      waitReason: null,
      priority: "normal",
      repository: "backend",
      startedAt: ago(now, 252),
      updatedAt: ago(now, 5),
      agent: "Codex",
      model: "gpt-5-codex · 高",
      currentStep: { name: "実装", startedAt: ago(now, 64) },
      budget: { preset: "standard", remainingPercent: 68 },
      repositories: [
        {
          name: "backend",
          role: "target",
          branch: "ai/auth-fix-203",
          worktree: "wt/203-backend",
          review: "not_started",
          evaluation: "not_run",
          pullRequest: {
            id: PR_42,
            number: 42,
            url: "https://github.com/example/backend/pull/42",
            state: "draft",
          },
        },
        {
          name: "ios",
          role: "referenced",
          branch: "main",
          worktree: null,
          review: "not_started",
          evaluation: "not_run",
          pullRequest: null,
        },
      ],
      dag: [
        {
          key: "research",
          title: "調査",
          role: "researcher",
          state: "succeeded",
          required: true,
          dependsOn: [],
          attempts: [
            {
              number: 1,
              state: "succeeded",
              agent: "Local",
              model: "qwen3",
              placement: "local_gpu",
              startedAt: ago(now, 250),
              finishedAt: ago(now, 208),
            },
          ],
        },
        {
          key: "implement",
          title: "実装",
          role: "worker",
          state: "running",
          required: true,
          dependsOn: ["research"],
          attempts: [
            {
              number: 1,
              state: "failed",
              agent: "Local",
              model: "qwen3",
              placement: "local_gpu",
              errorClass: "test_failure",
              startedAt: ago(now, 180),
              finishedAt: ago(now, 70),
            },
            {
              number: 2,
              state: "running",
              agent: "Codex",
              model: "高",
              placement: "cloud",
              startedAt: ago(now, 64),
            },
          ],
          toolCalls: [
            { id: "tc-1", name: "read_file", status: "succeeded", startedAt: ago(now, 60) },
            { id: "tc-2", name: "apply_patch", status: "started", startedAt: ago(now, 12) },
          ],
        },
        {
          key: "ios-research",
          title: "ios 参照の調査",
          role: "researcher",
          state: "succeeded",
          required: false,
          dependsOn: ["research"],
          attempts: [
            {
              number: 1,
              state: "succeeded",
              agent: "Local",
              model: "qwen3",
              placement: "local_gpu",
              startedAt: ago(now, 200),
              finishedAt: ago(now, 182),
            },
          ],
        },
        {
          key: "test",
          title: "テスト",
          role: "worker",
          state: "pending",
          required: true,
          dependsOn: ["implement"],
          agent: "Codex",
          model: "標準",
          attempts: [],
        },
        {
          key: "review",
          title: "レビュー",
          role: "reviewer",
          state: "pending",
          required: true,
          dependsOn: ["test"],
          agent: "Claude",
          model: "標準",
          attempts: [],
        },
      ],
    },
    {
      ...base,
      id: TASK_204,
      title: "PR #41 のレビュー対応",
      state: "waiting",
      waitReason: "approval",
      repository: "ios",
      updatedAt: ago(now, 120),
      repositories: [],
    },
    {
      ...base,
      id: TASK_205,
      title: "メモリ整理ジョブ",
      state: "waiting",
      waitReason: "resource",
      priority: "normal",
      repository: "workspace",
      updatedAt: ago(now, 300),
      repositories: [],
    },
    {
      ...base,
      id: TASK_201,
      title: "ログ出力の整理",
      state: "completed",
      waitReason: null,
      repository: "backend",
      updatedAt: ago(now, 720),
      repositories: [],
    },
  ];
}

export function samplePullRequests(): PullRequestRecord[] {
  return [
    {
      id: PR_42,
      number: 42,
      url: "https://github.com/example/backend/pull/42",
      state: "open",
      title: "認証セッションの失効判定を一本化する",
      taskId: TASK_203,
      taskTitle: "認証セッションの修正",
      repository: "backend",
      branch: "paw/2030c0de/1/_integration",
      base: "main",
      review: "approved",
      evaluation: "passed",
      mergeReady: true,
    },
    {
      id: "pr-43",
      number: 43,
      url: "https://github.com/example/ios/pull/43",
      state: "draft",
      title: "ログ出力の整理",
      taskId: TASK_201,
      taskTitle: "ログ出力の整理",
      repository: "ios",
      branch: "paw/2010c0de/1/_integration",
      base: "main",
      review: "in_review",
      evaluation: "not_run",
      mergeReady: false,
    },
  ];
}

const AFTER: Partial<Record<ControlCommand, TaskState>> = {
  pause: "paused",
  resume: "running",
  cancel: "cancelled",
  stop_now: "cancelled",
  retry: "running",
  restart: "queued",
};

/** An in-memory source; `calls` records the controls it was sent. */
export function fakeTaskSource(
  tasks: TaskDetail[] = sampleTasks(),
  pullRequests: PullRequestRecord[] = samplePullRequests(),
) {
  const calls: { id: string; command: ControlCommand; options: ControlOptions }[] = [];
  const state = [...tasks];
  const find = (id: string) => {
    const task = state.find((entry) => entry.id === id);
    if (!task) throw new ApiError(404, "not_found", "not found");
    return task;
  };
  const source: TaskSource = {
    async listTasks(): Promise<TaskList> {
      return { tasks: state, capacity: { parallelLimit: 2, vramUsedGb: 18.2, vramTotalGb: 48 } };
    },
    async getTask(id) {
      // A copy, as a response would be: a test changing the stored task later
      // does not change what the screen already holds.
      return { ...find(id) };
    },
    async control(id, command, options) {
      calls.push({ id, command, options });
      const task = find(id);
      const updated: TaskDetail = {
        ...task,
        state: AFTER[command] ?? task.state,
        waitReason: null,
        version: task.version + 1,
      };
      state.splice(state.indexOf(task), 1, updated);
      return updated;
    },
    async listPullRequests() {
      return pullRequests;
    },
  };
  return { source, calls };
}
