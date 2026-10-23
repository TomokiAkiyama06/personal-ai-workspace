// The 結果 panel of the task detail (issue #290, Decision 0098, 5 and its Test の方針).
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { ApiError } from "../api/client";
import type { Locale } from "../i18n";
import { mockApi, Providers, reply, session } from "../test/helpers";
import { fakeTaskSource, sampleTasks, TASK_201, TASK_203 } from "../test/tasks";
import type { TaskDetail, TaskResult } from "./model";
import { type TaskSource, TaskSourceProvider } from "./source";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

function renderTasks(path: string, source: TaskSource, locale?: Locale) {
  mockApi({ "GET /auth/session": reply(200, session()) });
  window.history.replaceState(null, "", path);
  return render(
    <Providers locale={locale}>
      <TaskSourceProvider source={source}>
        <App />
      </TaskSourceProvider>
    </Providers>,
  );
}

const RESULT: TaskResult = {
  completedAt: "2026-10-23T03:00:00Z",
  nodes: [
    {
      key: "survey",
      role: "researcher",
      title: "認証まわりの調査",
      summary: "セッションの失効は 2 か所で判定している。\n- auth/session.py\n- api/deps.py",
    },
    {
      key: "check",
      role: "reviewer",
      title: "調査の確認",
      summary:
        '<script>alert("x")</script> **太字** [リンク](https://example.com) https://example.com/a',
    },
  ],
};

/** The sample tasks with #201 (completed) given a result state. */
function withResult(state: TaskDetail["result"]): TaskDetail[] {
  const tasks = sampleTasks();
  const done = tasks.find((task) => task.id === TASK_201);
  if (done) done.result = state;
  return tasks;
}

describe("Task result panel", () => {
  it("shows each node's title, role and summary as plain text, in the plan's order", async () => {
    const { source, resultReads } = fakeTaskSource(withResult("available"), undefined, {
      [TASK_201]: RESULT,
    });
    renderTasks(`/agents/${TASK_201}`, source);
    const panel = await screen.findByRole("region", { name: "結果" });
    const items = await within(panel).findAllByRole("listitem");
    expect(items.map((item) => item.querySelector(".strong")?.textContent)).toEqual([
      "認証まわりの調査",
      "調査の確認",
    ]);
    expect(within(items[0] as HTMLElement).getByText("調査")).toHaveClass("tag");
    expect(within(items[1] as HTMLElement).getByText("レビュー")).toHaveClass("tag");
    // Line breaks are kept as text (white-space: pre-wrap), not turned into markup.
    const first = (items[0] as HTMLElement).querySelector(".result-summary");
    expect(first?.textContent).toBe(RESULT.nodes[0]?.summary);
    expect(first?.children).toHaveLength(0);
    // A script, Markdown and URLs stay plain text: no element, no link.
    const second = (items[1] as HTMLElement).querySelector(".result-summary");
    expect(second?.textContent).toBe(RESULT.nodes[1]?.summary);
    expect(second?.children).toHaveLength(0);
    expect(panel.querySelector("script, a, strong, b")).toBeNull();
    expect(within(panel).queryByRole("link")).not.toBeInTheDocument();
    // No redaction note (Decision 0098, 2 C).
    expect(panel.textContent).not.toMatch(/伏せ|REDACTED|Redact/i);
    expect(resultReads).toEqual([TASK_201]);
  });

  it("sits between the current work and the dependency graph, on every width", async () => {
    const { source } = fakeTaskSource(withResult("available"), undefined, {
      [TASK_201]: RESULT,
    });
    renderTasks(`/agents/${TASK_201}`, source);
    const panel = await screen.findByRole("region", { name: "結果" });
    const dag = screen.getByRole("region", { name: "依存グラフ" });
    expect(panel.compareDocumentPosition(dag) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(panel.closest(".wide-only, .phone-only")).toBeNull();
    expect(panel).toHaveClass("task-card-panel");
  });

  it("reads the result once and not again while the task stays the same", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    // A running task that completes while it is shown: the poll brings the
    // `available` state, the panel reads the result once, and no later read.
    const tasks = sampleTasks();
    const running = tasks.find((task) => task.id === TASK_203);
    const { source, resultReads } = fakeTaskSource(tasks, undefined, { [TASK_203]: RESULT });
    renderTasks(`/agents/${TASK_203}`, source);
    await screen.findByRole("article", { name: "認証セッションの修正" });
    expect(screen.queryByRole("region", { name: "結果" })).not.toBeInTheDocument();
    if (running) {
      running.state = "completed";
      running.currentStep = null;
      running.version = 4;
      running.result = "available";
    }
    await vi.advanceTimersByTimeAsync(5000);
    const panel = await screen.findByRole("region", { name: "結果" });
    await within(panel).findByText("認証まわりの調査");
    await vi.advanceTimersByTimeAsync(20_000);
    expect(resultReads).toEqual([TASK_203]);
  });

  it("says only that an unreadable repository hides the result", async () => {
    const tasks = withResult("hidden");
    const { source, resultReads } = fakeTaskSource(tasks);
    renderTasks(`/agents/${TASK_201}`, source);
    const panel = await screen.findByRole("region", { name: "結果" });
    expect(
      within(panel).getByText("読めないリポジトリを含むため、結果を表示できません"),
    ).toBeInTheDocument();
    expect(within(panel).queryByRole("listitem")).not.toBeInTheDocument();
    // The hidden state is the detail's: the result is not asked for.
    expect(resultReads).toEqual([]);
  });

  it("says the same when the result is refused after the detail said it was there", async () => {
    const { source } = fakeTaskSource(withResult("available"), undefined, {
      [TASK_201]: new ApiError(403, "task_result_forbidden", "forbidden"),
    });
    renderTasks(`/agents/${TASK_201}`, source);
    const line = await screen.findByText("読めないリポジトリを含むため、結果を表示できません");
    expect(line.closest("section")).toHaveAccessibleName("結果");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows nothing without a result, or when its read answers 404", async () => {
    const none = fakeTaskSource(withResult("none"));
    const first = renderTasks(`/agents/${TASK_201}`, none.source);
    await screen.findByRole("article", { name: "ログ出力の整理" });
    expect(screen.queryByRole("region", { name: "結果" })).not.toBeInTheDocument();
    expect(none.resultReads).toEqual([]);
    first.unmount();

    // `available`, but the result is gone (404 task_result_not_found).
    const gone = fakeTaskSource(withResult("available"));
    renderTasks(`/agents/${TASK_201}`, gone.source);
    await screen.findByRole("article", { name: "ログ出力の整理" });
    await waitFor(() => expect(gone.resultReads).toEqual([TASK_201]));
    await waitFor(() =>
      expect(screen.queryByRole("region", { name: "結果" })).not.toBeInTheDocument(),
    );
  });

  it("shows another failure with a retry that reads the result again", async () => {
    let fail = true;
    const { source, resultReads } = fakeTaskSource(withResult("available"), undefined, {
      [TASK_201]: RESULT,
    });
    const read = source.getTaskResult.bind(source);
    source.getTaskResult = (id) =>
      fail ? Promise.reject(new ApiError(503, "service_unavailable", "x")) : read(id);
    renderTasks(`/agents/${TASK_201}`, source);
    const panel = await screen.findByRole("region", { name: "結果" });
    expect(await within(panel).findByRole("alert")).toBeInTheDocument();
    fail = false;
    await userEvent.setup().click(within(panel).getByRole("button", { name: "再試行" }));
    expect(await within(panel).findByText("認証まわりの調査")).toBeInTheDocument();
    expect(resultReads).toEqual([TASK_201]);
  });

  it("speaks English", async () => {
    const { source } = fakeTaskSource(withResult("available"), undefined, {
      [TASK_201]: RESULT,
    });
    const first = renderTasks(`/agents/${TASK_201}`, source, "en");
    const panel = await screen.findByRole("region", { name: "Result" });
    expect(await within(panel).findByText("Research")).toBeInTheDocument();
    expect(within(panel).getByText("Review")).toBeInTheDocument();
    first.unmount();

    const hidden = fakeTaskSource(withResult("hidden"));
    renderTasks(`/agents/${TASK_201}`, hidden.source, "en");
    expect(
      await screen.findByText(
        "The result cannot be shown because the task includes a repository you cannot read",
      ),
    ).toBeInTheDocument();
  });
});
