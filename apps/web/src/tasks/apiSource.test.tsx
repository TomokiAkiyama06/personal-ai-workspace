import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { apiError, mockApi, Providers, reply, session } from "../test/helpers";
import { apiTaskSource, remainingPercent, type TaskDetailWire } from "./apiSource";
import { TaskSourceProvider } from "./source";

const TASK = "0b7c2e1a-1111-4222-8333-944455556666";

afterEach(() => {
  vi.unstubAllGlobals();
});

function wire(overrides: Partial<TaskDetailWire> = {}): TaskDetailWire {
  return {
    id: TASK,
    title: "Fix login",
    state: "running",
    wait_reason: null,
    priority: "high",
    project_id: "p-1",
    project_name: "Alpha",
    repository: "web-app",
    started_at: "2026-10-01T09:00:00Z",
    updated_at: "2026-10-01T09:05:00Z",
    version: 3,
    created_by: "u-1",
    agent: "codex",
    model: "gpt-5-codex",
    attempt: 1,
    retry_count: 0,
    current_step: {
      name: "implement",
      status: "running",
      started_at: "2026-10-01T09:01:00Z",
      finished_at: null,
      tool_calls: [
        {
          id: "c-1",
          name: "run_tests",
          status: "started",
          started_at: "2026-10-01T09:02:00Z",
          finished_at: null,
        },
      ],
    },
    budget: {
      preset: "standard",
      usage: [
        { kind: "steps", consumed: 10, limit: 50 },
        { kind: "tool_calls", consumed: 150, limit: 300 },
        { kind: "tokens", consumed: 0, limit: null },
      ],
    },
    repositories: [
      {
        repository_id: "r-1",
        name: "web-app",
        role: "target",
        branch: "paw/fix-login",
        worktree: "/srv/wt/1",
        review: "in_review",
        evaluation: "passed",
        pull_request: {
          id: "17",
          number: 7,
          url: "https://github.com/acme/web-app/pull/7",
          state: "open",
        },
      },
    ],
    dag: [
      {
        key: "plan",
        title: "Plan",
        role: "planner",
        state: "succeeded",
        required: true,
        depends_on: [],
        agent: "local",
        model: "qwen",
        attempts: [
          {
            number: 1,
            state: "succeeded",
            agent: "local",
            model: "qwen",
            placement: "local_gpu",
            error_class: null,
            started_at: "2026-10-01T09:00:00Z",
            finished_at: "2026-10-01T09:01:00Z",
          },
        ],
      },
    ],
    ...overrides,
  };
}

function renderApp(path: string) {
  window.history.replaceState(null, "", path);
  return render(
    <Providers>
      <TaskSourceProvider source={apiTaskSource}>
        <App />
      </TaskSourceProvider>
    </Providers>,
  );
}

describe("apiTaskSource", () => {
  it("reads the task list and renames its fields", async () => {
    const { calls } = mockApi({
      "GET /tasks": reply(200, { tasks: [wire({ priority: null, started_at: null })] }),
    });
    const list = await apiTaskSource.listTasks();
    expect(calls.map((call) => `${call.method} ${call.path}`)).toEqual(["GET /tasks"]);
    expect(list.tasks[0]).toEqual({
      id: TASK,
      title: "Fix login",
      state: "running",
      waitReason: null,
      priority: undefined,
      repository: "web-app",
      startedAt: undefined,
      updatedAt: "2026-10-01T09:05:00Z",
    });
    expect(list.capacity).toBeUndefined();
  });

  it("maps the detail: step tool calls, budget, repositories and DAG", async () => {
    mockApi({ [`GET /tasks/${TASK}`]: reply(200, wire()) });
    const task = await apiTaskSource.getTask(TASK);
    expect(task.version).toBe(3);
    expect(task.project).toBe("Alpha");
    expect(task.currentStep).toEqual({
      name: "implement",
      startedAt: "2026-10-01T09:01:00Z",
      toolCalls: [
        { id: "c-1", name: "run_tests", status: "started", startedAt: "2026-10-01T09:02:00Z" },
      ],
    });
    expect(task.budget).toEqual({ preset: "standard", remainingPercent: 50 });
    expect(task.repositories[0]).toEqual({
      name: "web-app",
      role: "target",
      branch: "paw/fix-login",
      worktree: "/srv/wt/1",
      review: "in_review",
      evaluation: "passed",
      pullRequest: {
        id: "17",
        number: 7,
        url: "https://github.com/acme/web-app/pull/7",
        state: "open",
      },
    });
    expect(task.dag?.[0]?.attempts[0]).toEqual({
      number: 1,
      state: "succeeded",
      agent: "local",
      model: "qwen",
      placement: "local_gpu",
      errorClass: null,
      startedAt: "2026-10-01T09:00:00Z",
      finishedAt: "2026-10-01T09:01:00Z",
    });
  });

  it("shows no current step once the step finished", async () => {
    const finished = wire();
    if (finished.current_step) finished.current_step.status = "succeeded";
    mockApi({ [`GET /tasks/${TASK}`]: reply(200, { ...finished, budget: null, dag: null }) });
    const task = await apiTaskSource.getTask(TASK);
    expect(task.currentStep).toBeNull();
    expect(task.budget).toBeUndefined();
    expect(task.dag).toBeNull();
  });

  it("sends a control with the expected version and only the given options", async () => {
    const { calls } = mockApi({
      [`POST /tasks/${TASK}/controls`]: reply(200, wire({ state: "cancelled", version: 4 })),
    });
    const task = await apiTaskSource.control(TASK, "stop_now", {
      expectedVersion: 3,
      reason: "looping",
    });
    expect(calls[0]?.body).toEqual({ command: "stop_now", expected_version: 3, reason: "looping" });
    expect(task.state).toBe("cancelled");
    await apiTaskSource.control(TASK, "retry", { expectedVersion: 4, agent: "codex" });
    expect(calls[1]?.body).toEqual({ command: "retry", expected_version: 4, agent: "codex" });
  });

  it("reads the pull requests with the Backend's Merge Ready", async () => {
    mockApi({
      "GET /pull-requests": reply(200, {
        pull_requests: [
          {
            id: "17",
            number: 7,
            url: "https://github.com/acme/web-app/pull/7",
            state: "open",
            title: "Fix login",
            task_id: TASK,
            task_title: "Fix login",
            repository: "web-app",
            branch: null,
            base: "main",
            review: "approved",
            evaluation: "passed",
            merge_ready: true,
            updated_at: "2026-10-01T09:05:00Z",
          },
        ],
      }),
    });
    const [pr] = await apiTaskSource.listPullRequests();
    expect(pr).toMatchObject({ id: "17", taskId: TASK, branch: "", mergeReady: true });
  });

  it("reads one pull request by its record id", async () => {
    const { calls } = mockApi({
      "GET /pull-requests/17": reply(200, {
        id: "17",
        number: 7,
        url: "https://github.com/acme/web-app/pull/7",
        state: "open",
        title: "Fix login",
        task_id: TASK,
        task_title: "Fix login",
        repository: "web-app",
        branch: "paw/x/1/_integration",
        base: "main",
        review: "approved",
        evaluation: "passed",
        merge_ready: true,
        updated_at: "2026-10-01T09:05:00Z",
      }),
    });
    const pr = await apiTaskSource.getPullRequest("17");
    expect(pr).toMatchObject({ id: "17", number: 7, mergeReady: true });
    expect(calls).toHaveLength(1);
  });

  it("reads the changed files, one diff, the review and the audit rows", async () => {
    const file = {
      index: 0,
      path: "src/app.py",
      previous_path: null,
      status: "modified",
      additions: 2,
      deletions: 1,
      has_patch: true,
      patch_truncated: false,
    };
    const { calls } = mockApi({
      "GET /pull-requests/17/files": reply(200, {
        recorded: true,
        head_commit: "a".repeat(40),
        truncated: false,
        additions: 2,
        deletions: 1,
        files: [file],
      }),
      "GET /pull-requests/17/files/0": reply(200, { ...file, count: 1, patch: "@@ -1 +1 @@\n" }),
      "GET /pull-requests/17/review": reply(200, {
        review: "approved",
        evaluation: "passed",
        reviewers: [
          {
            key: "r",
            title: "Review",
            state: "succeeded",
            agent: "claude",
            model: null,
            finished_at: "2026-10-01T09:05:00Z",
          },
        ],
      }),
      "GET /pull-requests/17/audit": reply(200, {
        rows: [
          {
            occurred_at: "2026-10-01T09:05:00Z",
            action: "tool.run_tests",
            decision: "deny",
            reason: "path_out_of_scope",
            actor: "agent",
          },
        ],
      }),
    });
    const changes = await apiTaskSource.getChangedFiles("17");
    expect(changes.files[0]).toEqual({
      index: 0,
      path: "src/app.py",
      previousPath: null,
      status: "modified",
      additions: 2,
      deletions: 1,
      hasPatch: true,
      patchTruncated: false,
    });
    const diff = await apiTaskSource.getFileDiff("17", 0);
    expect(diff).toMatchObject({ count: 1, patch: "@@ -1 +1 @@\n", path: "src/app.py" });
    const review = await apiTaskSource.getReview("17");
    expect(review.reviewers[0]).toMatchObject({
      agent: "claude",
      finishedAt: "2026-10-01T09:05:00Z",
    });
    const audit = await apiTaskSource.getAudit("17");
    expect(audit).toEqual([
      {
        occurredAt: "2026-10-01T09:05:00Z",
        action: "tool.run_tests",
        decision: "deny",
        reason: "path_out_of_scope",
        actor: "agent",
      },
    ]);
    expect(calls.map((call) => call.path)).toEqual([
      "/pull-requests/17/files",
      "/pull-requests/17/files/0",
      "/pull-requests/17/review",
      "/pull-requests/17/audit",
    ]);
  });

  it("lists the approvals of a task and sends a decision", async () => {
    const { calls, fetchMock } = mockApi({
      "GET /approvals": reply(200, {
        approvals: [
          {
            id: "a-1",
            task_id: TASK,
            task_title: "Fix login",
            agent: "codex",
            project_id: "p-1",
            tool: "package.add",
            level: "approval",
            summary: [{ name: "command", kind: "text", value: "uv add x" }],
            repositories: ["web-app"],
            created_at: "2026-10-01T09:00:00Z",
            expires_at: "2026-10-01T10:00:00Z",
          },
        ],
      }),
      "POST /approvals/a-1/decision": reply(200, { id: "a-1", outcome: "approved" }),
      "GET /approvals/a-1": reply(200, {
        id: "a-1",
        task_id: TASK,
        task_title: "Fix login",
        agent: null,
        project_id: "p-1",
        tool: "package.add",
        level: "strong_approval",
        summary: [{ name: "command", kind: "text", value: "uv add x" }],
        repositories: [],
        created_at: "2026-10-01T09:00:00Z",
        expires_at: "2026-10-01T10:00:00Z",
      }),
    });
    const [item] = await apiTaskSource.listApprovals(TASK);
    expect(item).toMatchObject({ id: "a-1", taskId: TASK, level: "approval", tool: "package.add" });
    expect(String(fetchMock.mock.calls[0]?.[0])).toContain(`/approvals?task_id=${TASK}`);
    await apiTaskSource.decideApproval("a-1", "approve");
    expect(await apiTaskSource.getApproval("a-1")).toMatchObject({ level: "strong_approval" });
    expect(calls[1]).toMatchObject({
      method: "POST",
      path: "/approvals/a-1/decision",
      body: { decision: "approve" },
    });
  });

  it("computes what is left of the budget", () => {
    expect(remainingPercent([{ consumed: 0, limit: null }])).toBeUndefined();
    expect(remainingPercent([{ consumed: 80, limit: 50 }])).toBe(0);
    expect(remainingPercent([{ consumed: 1, limit: 3 }])).toBe(66);
    expect(remainingPercent([{ consumed: 0, limit: 0 }])).toBe(0);
  });

  it("drives the task screen end to end and shows the Backend's refusal", async () => {
    mockApi({
      "GET /auth/session": reply(200, session()),
      "GET /tasks": reply(200, { tasks: [wire()] }),
      [`GET /tasks/${TASK}`]: reply(200, wire()),
      [`POST /tasks/${TASK}/controls`]: apiError(409, "task_conflict"),
    });
    renderApp(`/agents/${TASK}`);
    const tools = await screen.findByRole("region", { name: /ツール呼び出し · implement/ });
    expect(within(tools).getByText("run_tests")).toBeInTheDocument();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "一時停止" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("タスクが変わりました");
  });
});
