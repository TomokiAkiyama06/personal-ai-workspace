import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { ApiError } from "../api/client";
import { mockApi, Providers, reply, session } from "../test/helpers";
import { fakeTaskSource, sampleTasks, TASK_201, TASK_203, TASK_205 } from "../test/tasks";
import {
  ACCEPTED_CONTROLS,
  type DagNode,
  formatDuration,
  layoutDag,
  orderedNodes,
  type TaskDetail,
} from "./model";
import { type TaskSource, TaskSourceProvider } from "./source";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

function renderTasks(path: string, source?: TaskSource) {
  mockApi({ "GET /auth/session": reply(200, session()) });
  window.history.replaceState(null, "", path);
  return render(
    <Providers>
      <TaskSourceProvider source={source}>
        <App />
      </TaskSourceProvider>
    </Providers>,
  );
}

function node(key: string, dependsOn: string[] = []): DagNode {
  return {
    key,
    title: key,
    role: "worker",
    state: "pending",
    required: true,
    dependsOn,
    attempts: [],
  };
}

describe("task model", () => {
  it("shows only the controls the Backend's transition table accepts", () => {
    expect(ACCEPTED_CONTROLS.running).toEqual(["pause", "cancel", "stop_now"]);
    expect(ACCEPTED_CONTROLS.paused).toEqual(["resume", "cancel"]);
    expect(ACCEPTED_CONTROLS.failed).toEqual(["retry", "restart"]);
    expect(ACCEPTED_CONTROLS.cancelled).toEqual(["restart"]);
    expect(ACCEPTED_CONTROLS.completed).toEqual([]);
  });

  it("formats runtimes as the design does", () => {
    expect(formatDuration(42_000)).toBe("42s");
    expect(formatDuration(252_000)).toBe("4m 12s");
    expect(formatDuration(3_780_000)).toBe("1h 03m");
    expect(formatDuration(-5)).toBe("0s");
  });

  it("lays nodes out by dependency depth and survives unknown keys and cycles", () => {
    const layout = layoutDag([
      node("a"),
      node("b", ["a"]),
      node("c", ["a"]),
      node("d", ["b", "c", "missing"]),
    ]);
    expect(layout.map((entry) => [entry.node.key, entry.column, entry.row])).toEqual([
      ["a", 0, 0],
      ["b", 1, 0],
      ["c", 1, 1],
      ["d", 2, 0],
    ]);
    const cycle = layoutDag([node("x", ["y"]), node("y", ["x"])]);
    expect(cycle).toHaveLength(2);
    expect(orderedNodes([node("late", ["early"]), node("early")]).map((n) => n.key)).toEqual([
      "early",
      "late",
    ]);
  });
});

describe("Tasks page", () => {
  it("says the data is not available while no source is connected", async () => {
    renderTasks("/agents");
    expect(
      await screen.findByRole("heading", { name: "エージェント / タスク", level: 1 }),
    ).toBeInTheDocument();
    expect(screen.getByText("タスクの状態はまだ表示できません")).toBeInTheDocument();
    expect(screen.queryByText("この画面は後続の Issue で実装します。")).not.toBeInTheDocument();
  });

  it("lists the tasks with their state and opens the first one", async () => {
    const { source } = fakeTaskSource();
    renderTasks("/agents", source);
    const list = await screen.findByRole("navigation", { name: "タスクの一覧" });
    const cards = await within(list).findAllByRole("link");
    expect(cards.map((card) => card.textContent)).toEqual([
      expect.stringContaining("認証セッションの修正"),
      expect.stringContaining("PR #41 のレビュー対応"),
      expect.stringContaining("メモリ整理ジョブ"),
      expect.stringContaining("ログ出力の整理"),
    ]);
    expect(cards[0]).toHaveAttribute("aria-current", "page");
    expect(cards[1]).toHaveTextContent("承認待ち");
    expect(cards[2]).toHaveTextContent("リソース待ち");
    expect(cards[3]).toHaveTextContent("完了");
    // The queue note names the task that waits for a resource and its priority.
    expect(
      within(list).getByText(
        "メモリ整理ジョブ はリソースの空きを待っています（Waiting for Resource）。優先度 NORMAL。",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("並列上限 2 · VRAM 18.2 / 48 GB")).toBeInTheDocument();

    const detail = await screen.findByRole("article", { name: "認証セッションの修正" });
    expect(detail.querySelector(".task-title-block .state-pill")).toHaveTextContent("実行中");
    expect(within(detail).getByText("Codex · gpt-5-codex · 高")).toBeInTheDocument();
    expect(within(detail).getAllByText("wt/203-backend")).not.toHaveLength(0);
    expect(within(detail).getByText("標準 · 残 68%")).toBeInTheDocument();
    expect(within(detail).getByRole("link", { name: "PR #42 を開く" })).toHaveAttribute(
      "href",
      "/pulls/pr-42",
    );
  });

  it("filters by state with the counts of the design", async () => {
    const { source } = fakeTaskSource();
    renderTasks("/agents", source);
    const user = userEvent.setup();
    const approval = await screen.findByRole("button", { name: "承認待ち 1" });
    expect(screen.getByRole("button", { name: "実行中 1" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "失敗" })).toBeInTheDocument();
    await user.click(approval);
    const list = screen.getByRole("navigation", { name: "タスクの一覧" });
    expect(within(list).getAllByRole("link")).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "失敗" }));
    expect(within(list).getByText("条件に合うタスクはありません。")).toBeInTheDocument();
  });

  it("draws the dependency graph and opens a node's attempts and tool calls", async () => {
    const { source } = fakeTaskSource();
    renderTasks(`/agents/${TASK_203}`, source);
    const graph = await screen.findByRole("list", {
      name: "タスク 認証セッションの修正 の依存グラフ",
    });
    const nodes = within(graph).getAllByRole("button");
    expect(nodes.map((button) => button.textContent)).toEqual([
      expect.stringContaining("調査"),
      expect.stringContaining("実装"),
      expect.stringContaining("ios 参照の調査"),
      expect.stringContaining("テスト"),
      expect.stringContaining("レビュー"),
    ]);
    expect(nodes[2]).toHaveTextContent("任意");
    expect(nodes[3]).toHaveTextContent("Codex · 標準");
    expect(nodes[3]).toHaveTextContent("待機");
    // One arrow per dependency.
    expect(document.querySelectorAll(".dag-edges > path")).toHaveLength(4);

    const user = userEvent.setup();
    await user.click(nodes[1] as HTMLElement);
    expect(nodes[1]).toHaveAttribute("aria-pressed", "true");
    const detail = screen.getByRole("region", { name: "実装" });
    expect(within(detail).getByText("依存: 調査")).toBeInTheDocument();
    expect(within(detail).getByText("試行 1")).toBeInTheDocument();
    expect(within(detail).getByText("test_failure")).toBeInTheDocument();
    expect(within(detail).getByText(/Codex · 高 · Cloud/)).toBeInTheDocument();
    expect(within(detail).getByText("apply_patch")).toBeInTheDocument();
    await user.click(within(detail).getByRole("button", { name: "ノードの詳細を閉じる" }));
    expect(screen.queryByRole("region", { name: "実装" })).not.toBeInTheDocument();
  });

  it("shows each repository's role, branch, tests, review and pull request", async () => {
    const { source } = fakeTaskSource();
    renderTasks(`/agents/${TASK_203}`, source);
    const table = await screen.findByRole("region", { name: "リポジトリごとの状態" });
    const rows = within(table).getAllByRole("listitem");
    expect(rows[0]).toHaveTextContent("backend");
    expect(rows[0]).toHaveTextContent("Target");
    expect(rows[0]).toHaveTextContent("ai/auth-fix-203");
    expect(rows[0]).toHaveTextContent("未実行");
    expect(
      within(rows[0] as HTMLElement).getByRole("link", { name: "#42 draft" }),
    ).toBeInTheDocument();
    expect(rows[1]).toHaveTextContent("参照");
    expect(rows[1]).toHaveTextContent("main（読み取りのみ）");
  });

  it("sends a control and shows the state the Backend answers", async () => {
    const { source, calls } = fakeTaskSource();
    renderTasks(`/agents/${TASK_203}`, source);
    const user = userEvent.setup();
    const controls = await screen.findByRole("group", { name: "タスクの操作" });
    expect(
      within(controls)
        .getAllByRole("button")
        .map((button) => button.textContent),
    ).toEqual(["一時停止", "キャンセル", expect.stringContaining("今すぐ停止")]);
    await user.click(within(controls).getByRole("button", { name: "一時停止" }));
    expect(calls).toEqual([{ id: TASK_203, command: "pause", options: { expectedVersion: 3 } }]);
    const detail = await screen.findByRole("article", { name: "認証セッションの修正" });
    await waitFor(() =>
      expect(detail.querySelector(".task-title-block .state-pill")).toHaveTextContent("一時停止中"),
    );
    expect(within(detail).getByRole("button", { name: "再開" })).toBeInTheDocument();
  });

  it("asks for the reason before Stop Now and sends it (the Backend requires one)", async () => {
    const { source, calls } = fakeTaskSource();
    renderTasks(`/agents/${TASK_203}`, source);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /今すぐ停止/ }));
    expect(calls).toEqual([]);
    const form = screen.getByRole("form", { name: "今すぐ停止" });
    const submit = within(form).getByRole("button", { name: "今すぐ停止を実行" });
    expect(submit).toBeDisabled();
    await user.type(
      within(form).getByRole("textbox", { name: "停止する理由" }),
      "  誤った Repo を編集している  ",
    );
    await user.click(submit);
    expect(calls).toEqual([
      {
        id: TASK_203,
        command: "stop_now",
        options: { expectedVersion: 3, reason: "誤った Repo を編集している" },
      },
    ]);
    const detail = await screen.findByRole("article", { name: "認証セッションの修正" });
    await waitFor(() =>
      expect(detail.querySelector(".task-title-block .state-pill")).toHaveTextContent(
        "キャンセル済み",
      ),
    );
    expect(screen.queryByRole("form", { name: "今すぐ停止" })).not.toBeInTheDocument();
  });

  it("shows the Backend's refusal of a control", async () => {
    const { source } = fakeTaskSource();
    source.control = () => Promise.reject(new ApiError(403, "forbidden", "x"));
    renderTasks(`/agents/${TASK_203}`, source);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "一時停止" }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "一時停止" })).toBeEnabled();
  });

  it("lets Retry and Restart switch the agent or model", async () => {
    const tasks = sampleTasks();
    const failed = tasks.find((task) => task.id === TASK_205);
    if (failed) {
      failed.state = "failed";
      failed.waitReason = null;
    }
    const { source, calls } = fakeTaskSource(tasks);
    renderTasks(`/agents/${TASK_205}`, source);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "再試行" }));
    const form = screen.getByRole("form", { name: "再試行" });
    await user.type(within(form).getByRole("textbox", { name: "エージェント" }), "Claude");
    await user.click(within(form).getByRole("button", { name: "再試行を実行" }));
    expect(calls).toEqual([
      { id: TASK_205, command: "retry", options: { expectedVersion: 3, agent: "Claude" } },
    ]);
  });

  it("reads an unfinished task and the list again while they can change", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const { source } = fakeTaskSource();
      const getTask = vi.spyOn(source, "getTask");
      const listTasks = vi.spyOn(source, "listTasks");
      renderTasks(`/agents/${TASK_203}`, source);
      await screen.findByRole("article", { name: "認証セッションの修正" });
      const before = [getTask.mock.calls.length, listTasks.mock.calls.length];
      await vi.advanceTimersByTimeAsync(5000);
      expect(getTask.mock.calls.length).toBeGreaterThan(before[0] ?? 0);
      expect(listTasks.mock.calls.length).toBeGreaterThan(before[1] ?? 0);
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps the list when a background read fails", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const { source } = fakeTaskSource();
      renderTasks("/agents", source);
      const list = await screen.findByRole("navigation", { name: "タスクの一覧" });
      await within(list).findAllByRole("link");
      source.listTasks = () => Promise.reject(new ApiError(503, "service_unavailable", "x"));
      await vi.advanceTimersByTimeAsync(5000);
      expect(within(list).getAllByRole("link")).toHaveLength(4);
      expect(within(list).queryByRole("alert")).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("shows the task as it is now after a control is refused (a version conflict)", async () => {
    const tasks = sampleTasks();
    const failed = tasks.find((task) => task.id === TASK_205);
    if (failed) {
      failed.state = "failed";
      failed.waitReason = null;
    }
    const { source } = fakeTaskSource(tasks);
    renderTasks(`/agents/${TASK_205}`, source);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "最初からやり直す" }));
    // Someone else restarted the task meanwhile.
    if (failed) {
      failed.state = "queued";
      failed.version = 4;
    }
    source.control = () => Promise.reject(new ApiError(409, "conflict", "x"));
    await user.click(screen.getByRole("button", { name: "最初からやり直すを実行" }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    const detail = screen.getByRole("article", { name: "メモリ整理ジョブ" });
    await waitFor(() =>
      expect(detail.querySelector(".task-title-block .state-pill")).toHaveTextContent("キュー待ち"),
    );
  });

  it("keeps the list when the read after a control fails", async () => {
    const { source } = fakeTaskSource();
    renderTasks("/agents", source);
    const user = userEvent.setup();
    const list = await screen.findByRole("navigation", { name: "タスクの一覧" });
    await screen.findByRole("article", { name: "認証セッションの修正" });
    source.listTasks = () => Promise.reject(new ApiError(503, "service_unavailable", "x"));
    await user.click(screen.getByRole("button", { name: "一時停止" }));
    const detail = screen.getByRole("article", { name: "認証セッションの修正" });
    await waitFor(() =>
      expect(detail.querySelector(".task-title-block .state-pill")).toHaveTextContent("一時停止中"),
    );
    expect(within(list).getAllByRole("link")).toHaveLength(4);
    expect(within(list).queryByRole("alert")).not.toBeInTheDocument();
  });

  it("closes a control's form when the task no longer accepts it", async () => {
    const tasks = sampleTasks();
    const failed = tasks.find((task) => task.id === TASK_205);
    if (failed) {
      failed.state = "failed";
      failed.waitReason = null;
    }
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { source } = fakeTaskSource(tasks);
    renderTasks(`/agents/${TASK_205}`, source);
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    await user.click(await screen.findByRole("button", { name: "最初からやり直す" }));
    if (failed) {
      failed.state = "queued";
      failed.version = 4;
    }
    source.control = () => Promise.reject(new ApiError(409, "conflict", "x"));
    await user.click(screen.getByRole("button", { name: "最初からやり直すを実行" }));
    await waitFor(() =>
      expect(screen.queryByRole("form", { name: "最初からやり直す" })).not.toBeInTheDocument(),
    );
    // The task fails again: the old form does not come back by itself.
    try {
      if (failed) {
        failed.state = "failed";
        failed.version = 5;
      }
      await vi.advanceTimersByTimeAsync(5000);
      const detail = screen.getByRole("article", { name: "メモリ整理ジョブ" });
      await waitFor(() =>
        expect(detail.querySelector(".task-title-block .state-pill")).toHaveTextContent("失敗"),
      );
      expect(screen.queryByRole("form", { name: "最初からやり直す" })).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("updates the task's card from the control's answer when the list cannot be read", async () => {
    const tasks = sampleTasks();
    const failed = tasks.find((task) => task.id === TASK_205);
    if (failed) {
      failed.state = "failed";
      failed.waitReason = null;
    }
    const { source } = fakeTaskSource(tasks);
    renderTasks(`/agents/${TASK_205}`, source);
    const user = userEvent.setup();
    const list = await screen.findByRole("navigation", { name: "タスクの一覧" });
    await user.click(await screen.findByRole("button", { name: "再試行" }));
    source.listTasks = () => Promise.reject(new ApiError(503, "service_unavailable", "x"));
    await user.click(screen.getByRole("button", { name: "再試行を実行" }));
    const card = within(list).getByRole("link", { name: /メモリ整理ジョブ/ });
    await waitFor(() => expect(card).toHaveTextContent("実行中"));
  });

  it("ignores a poll that was in flight when a control answered", async () => {
    const { source } = fakeTaskSource();
    let resolveStale: (task: TaskDetail) => void = () => {};
    const stale = sampleTasks().find((task) => task.id === TASK_203) as TaskDetail;
    renderTasks(`/agents/${TASK_203}`, source);
    await screen.findByRole("article", { name: "認証セッションの修正" });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      source.getTask = () =>
        new Promise((resolve) => {
          resolveStale = resolve;
        });
      await vi.advanceTimersByTimeAsync(5000);
      const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
      await user.click(screen.getByRole("button", { name: "一時停止" }));
      const detail = screen.getByRole("article", { name: "認証セッションの修正" });
      await waitFor(() =>
        expect(detail.querySelector(".task-title-block .state-pill")).toHaveTextContent(
          "一時停止中",
        ),
      );
      await act(async () => resolveStale(stale));
      expect(detail.querySelector(".task-title-block .state-pill")).toHaveTextContent("一時停止中");
    } finally {
      vi.useRealTimers();
    }
  });

  it("offers Retry and Restart for a failed task and nothing for a completed one", async () => {
    const tasks = sampleTasks();
    const failed = tasks.find((task) => task.id === TASK_205);
    if (failed) {
      failed.state = "failed";
      failed.waitReason = null;
    }
    const { source } = fakeTaskSource(tasks);
    renderTasks(`/agents/${TASK_205}`, source);
    const controls = await screen.findByRole("group", { name: "タスクの操作" });
    expect(
      within(controls)
        .getAllByRole("button")
        .map((button) => button.textContent),
    ).toEqual(["再試行", "最初からやり直す"]);
    expect(
      screen.getByText("計画の作成前です。依存グラフは計画ができると表示されます。"),
    ).toBeInTheDocument();
  });

  it("explains why a waiting task waits and has no controls when completed", async () => {
    const { source } = fakeTaskSource();
    renderTasks(`/agents/${TASK_205}`, source);
    expect(
      await screen.findByText(
        "このタスクは GPU・Cloud の Quota・リポジトリのロックの空きを待っています。",
      ),
    ).toBeInTheDocument();
    const user = userEvent.setup();
    await user.click(screen.getByRole("link", { name: /ログ出力の整理/ }));
    expect(window.location.pathname).toBe(`/agents/${TASK_201}`);
    await screen.findByRole("article", { name: "ログ出力の整理" });
    expect(screen.queryByRole("group", { name: "タスクの操作" })).not.toBeInTheDocument();
  });

  it("says when a task does not exist", async () => {
    const { source } = fakeTaskSource();
    renderTasks("/agents/unknown", source);
    expect(await screen.findByText("このタスクは見つかりませんでした。")).toBeInTheDocument();
  });

  it("treats a malformed escape in the path as a task that does not exist", async () => {
    const { source } = fakeTaskSource();
    renderTasks("/agents/%E0%A4%A", source);
    expect(await screen.findByText("このタスクは見つかりませんでした。")).toBeInTheDocument();
  });
});
