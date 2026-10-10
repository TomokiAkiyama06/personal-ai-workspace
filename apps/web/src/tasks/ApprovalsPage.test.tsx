import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { ApiError } from "../api/client";
import { mockApi, Providers, reply, session } from "../test/helpers";
import { fakeTaskSource, TASK_204 } from "../test/tasks";
import { type TaskSource, TaskSourceProvider } from "./source";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function renderAt(path: string, source?: TaskSource) {
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

describe("Approvals page", () => {
  it("says the data is not available while no source is connected", async () => {
    renderAt("/approvals");
    expect(await screen.findByRole("heading", { name: "承認待ち", level: 1 })).toBeInTheDocument();
    expect(screen.getByText("タスクの状態はまだ表示できません")).toBeInTheDocument();
  });

  it("lists what the person is asked to approve", async () => {
    const { source } = fakeTaskSource();
    renderAt("/approvals", source);
    const cards = await screen.findAllByRole("link", { name: /APPROVAL/ });
    expect(cards).toHaveLength(2);
    expect(cards[0]).toHaveTextContent("package.add");
    expect(cards[0]).toHaveTextContent("Task 2040c0de · backend · Codex");
    expect(cards[0]).toHaveAttribute("href", "/approvals/approval-1");
    expect(cards[1]).toHaveTextContent("STRONG_APPROVAL");
    expect(cards[1]).toHaveTextContent("Passkey が必要");
  });

  it("allows one call once and takes it off the list", async () => {
    const { source, decisions } = fakeTaskSource();
    renderAt("/approvals/approval-1", source);
    const sheet = await screen.findByRole("dialog", { name: "この操作を許可しますか" });
    expect(within(sheet).getByText('uv add "pyjwt>=2.9"')).toBeInTheDocument();
    expect(within(sheet).getByText("backend")).toBeInTheDocument();
    // The Backend did not offer this call for the rest of the task
    // (`task_grant_allowed` is false): there is no "for this task" choice.
    expect(within(sheet).queryByRole("button", { name: "このタスクの間は許可" })).toBeNull();
    const user = userEvent.setup();
    await user.click(within(sheet).getByRole("button", { name: "今回だけ許可" }));
    expect(await screen.findByText("許可しました: package.add")).toBeInTheDocument();
    expect(decisions).toEqual([{ id: "approval-1", decision: "approve" }]);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(window.location.pathname).toBe("/approvals");
    expect(screen.getAllByRole("link", { name: /APPROVAL/ })).toHaveLength(1);
  });

  it("allows a call for the rest of its task when the Backend offers it", async () => {
    const { source, decisions } = fakeTaskSource();
    const [first, ...rest] = await source.listApprovals();
    if (!first) throw new Error("no sample");
    vi.spyOn(source, "listApprovals").mockResolvedValue([
      { ...first, taskGrantAllowed: true },
      ...rest,
    ]);
    renderAt(`/approvals/${first.id}`, source);
    const sheet = await screen.findByRole("dialog", { name: "この操作を許可しますか" });
    const forTask = within(sheet).getByRole("button", { name: "このタスクの間は許可" });
    // What it covers and how it ends is said next to the button.
    expect(forTask).toHaveAccessibleDescription(
      "このタスクが終わるまで、同じツールで同じか狭い対象の操作を聞かずに許可します。タスクの画面から取り消せます。",
    );
    const user = userEvent.setup();
    await user.click(forTask);
    expect(
      await screen.findByText("このタスクの間は許可しました: package.add"),
    ).toBeInTheDocument();
    expect(decisions).toEqual([{ id: "approval-1", decision: "approve_for_task" }]);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("explains why a call cannot be allowed for the task", async () => {
    const { source } = fakeTaskSource();
    const [first] = await source.listApprovals();
    if (!first) throw new Error("no sample");
    vi.spyOn(source, "listApprovals").mockResolvedValue([{ ...first, taskGrantAllowed: true }]);
    vi.spyOn(source, "decideApproval").mockRejectedValue(
      new ApiError(409, "task_grant_limit_reached", "x"),
    );
    renderAt(`/approvals/${first.id}`, source);
    const sheet = await screen.findByRole("dialog", { name: "この操作を許可しますか" });
    const user = userEvent.setup();
    await user.click(within(sheet).getByRole("button", { name: "このタスクの間は許可" }));
    expect(await within(sheet).findByRole("alert")).toHaveTextContent(
      "このタスクで許可中の操作が多すぎます",
    );
    expect(within(sheet).getByRole("button", { name: "今回だけ許可" })).toBeEnabled();
  });

  it("keeps the name of an argument that is not the command", async () => {
    const { source } = fakeTaskSource();
    const [first] = await source.listApprovals();
    if (!first) throw new Error("no sample");
    vi.spyOn(source, "listApprovals").mockResolvedValue([
      { ...first, summary: [{ name: "package", kind: "text", value: "pyjwt" }] },
    ]);
    renderAt(`/approvals/${first.id}`, source);
    const sheet = await screen.findByRole("dialog", { name: "この操作を許可しますか" });
    expect(within(sheet).getByText("package")).toBeInTheDocument();
    expect(within(sheet).getByText("pyjwt")).toBeInTheDocument();
  });

  it("only rejects a strong approval here", async () => {
    const { source, decisions } = fakeTaskSource();
    renderAt("/approvals/approval-2", source);
    const sheet = await screen.findByRole("dialog", { name: "この操作を許可しますか" });
    expect(within(sheet).queryByRole("button", { name: "今回だけ許可" })).toBeNull();
    expect(within(sheet).queryByRole("button", { name: "このタスクの間は許可" })).toBeNull();
    expect(within(sheet).getByText(/Passkey での再認証が必要です/)).toBeInTheDocument();
    const user = userEvent.setup();
    await user.click(within(sheet).getByRole("button", { name: "拒否する" }));
    expect(await screen.findByText("拒否しました: git.merge")).toBeInTheDocument();
    expect(decisions).toEqual([{ id: "approval-2", decision: "reject" }]);
  });

  it("shows the Backend's refusal and keeps the sheet open", async () => {
    const { source } = fakeTaskSource();
    vi.spyOn(source, "decideApproval").mockRejectedValue(
      new ApiError(409, "approval_not_pending", "x"),
    );
    renderAt("/approvals/approval-1", source);
    const sheet = await screen.findByRole("dialog", { name: "この操作を許可しますか" });
    const user = userEvent.setup();
    await user.click(within(sheet).getByRole("button", { name: "今回だけ許可" }));
    expect(await within(sheet).findByRole("alert")).toHaveTextContent(
      "この承認はすでに決定されたか",
    );
    expect(within(sheet).getByRole("button", { name: "今回だけ許可" })).toBeEnabled();
  });

  it("closes the sheet on Escape and on the backdrop", async () => {
    const { source } = fakeTaskSource();
    renderAt("/approvals/approval-1", source);
    await screen.findByRole("dialog");
    const user = userEvent.setup();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(window.location.pathname).toBe("/approvals");
    await user.click(await screen.findByRole("link", { name: /package\.add/ }));
    await screen.findByRole("dialog");
    await user.click(screen.getByRole("button", { name: "閉じる" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("opens an approval the bounded list does not hold", async () => {
    const { source } = fakeTaskSource();
    const [first, ...rest] = await source.listApprovals();
    if (!first) throw new Error("no sample");
    vi.spyOn(source, "listApprovals").mockResolvedValue(rest);
    const one = vi.spyOn(source, "getApproval");
    renderAt(`/approvals/${first.id}`, source);
    const sheet = await screen.findByRole("dialog", { name: "この操作を許可しますか" });
    expect(within(sheet).getByText(first.tool)).toBeInTheDocument();
    expect(one).toHaveBeenCalledWith(first.id);
  });

  it("retries an exact read that failed instead of calling it gone", async () => {
    const { source } = fakeTaskSource();
    const [first, ...rest] = await source.listApprovals();
    if (!first) throw new Error("no sample");
    vi.spyOn(source, "listApprovals").mockResolvedValue(rest);
    const one = vi
      .spyOn(source, "getApproval")
      .mockRejectedValueOnce(new ApiError(503, "approvals_unavailable", "x"))
      .mockResolvedValue(first);
    renderAt(`/approvals/${first.id}`, source);
    expect(await screen.findByRole("alert")).toHaveTextContent("いま承認を受け付けられません");
    expect(screen.queryByText(/この承認は見つかりませんでした/)).not.toBeInTheDocument();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "再試行" }));
    expect(
      await screen.findByRole("dialog", { name: "この操作を許可しますか" }),
    ).toBeInTheDocument();
    expect(one).toHaveBeenCalledTimes(2);
  });

  it("says an approval that is gone was not found", async () => {
    const { source } = fakeTaskSource();
    renderAt("/approvals/approval-9", source);
    expect(await screen.findByRole("alert")).toHaveTextContent("この承認は見つかりませんでした");
  });

  it("opens the approval from a task that waits for it", async () => {
    const { source } = fakeTaskSource();
    const list = vi.spyOn(source, "listApprovals");
    renderAt(`/agents/${TASK_204}`, source);
    const open = await screen.findByRole("link", { name: "確認" });
    expect(screen.getByText("package.add に承認が必要です")).toBeInTheDocument();
    expect(open).toHaveAttribute("href", "/approvals/approval-1");
    expect(list).toHaveBeenCalledWith(TASK_204);
  });

  it("keeps the plain notice when the person is not asked", async () => {
    const { source } = fakeTaskSource();
    vi.spyOn(source, "listApprovals").mockResolvedValue([]);
    renderAt(`/agents/${TASK_204}`, source);
    expect(await screen.findByText("このタスクは操作の承認を待っています。")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "確認" })).not.toBeInTheDocument();
  });

  it("lists the calls allowed for this task and revokes one", async () => {
    const { source, revoked } = fakeTaskSource();
    await source.decideApproval("approval-1", "approve_for_task");
    renderAt(`/agents/${TASK_204}`, source);
    const panel = await screen.findByRole("region", { name: "このタスクで許可中" });
    expect(within(panel).getByText("package.add")).toBeInTheDocument();
    expect(within(panel).getByText('uv add "pyjwt>=2.9"')).toBeInTheDocument();
    expect(within(panel).getByText(/0 回使用/)).toBeInTheDocument();
    const user = userEvent.setup();
    await user.click(within(panel).getByRole("button", { name: "取り消す: package.add" }));
    expect(await screen.findByText("取り消しました: package.add")).toBeInTheDocument();
    expect(revoked).toEqual(["grant-1"]);
    expect(screen.queryByText('uv add "pyjwt>=2.9"')).not.toBeInTheDocument();
  });

  it("reads the grants again while the task runs (their use counts change)", async () => {
    // Codex P2 on #216: a use does not change the task's version.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const { source } = fakeTaskSource();
      await source.decideApproval("approval-1", "approve_for_task");
      const [grant] = await source.listTaskGrants(TASK_204);
      if (!grant) throw new Error("no grant");
      renderAt(`/agents/${TASK_204}`, source);
      const panel = await screen.findByRole("region", { name: "このタスクで許可中" });
      expect(within(panel).getByText(/0 回使用/)).toBeInTheDocument();
      vi.spyOn(source, "listTaskGrants").mockResolvedValue([{ ...grant, uses: 3 }]);
      await vi.advanceTimersByTimeAsync(5000);
      expect(await within(panel).findByText(/3 回使用/)).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("never lets an older read bring a revoked grant back", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const { source } = fakeTaskSource();
      await source.decideApproval("approval-1", "approve_for_task");
      const before = await source.listTaskGrants(TASK_204);
      renderAt(`/agents/${TASK_204}`, source);
      const panel = await screen.findByRole("region", { name: "このタスクで許可中" });
      // A poll that is still in flight when the grant is revoked.
      let answer: (items: typeof before) => void = () => {};
      vi.spyOn(source, "listTaskGrants").mockImplementationOnce(
        () => new Promise((resolve) => (answer = resolve)),
      );
      await vi.advanceTimersByTimeAsync(5000);
      const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
      await user.click(within(panel).getByRole("button", { name: "取り消す: package.add" }));
      expect(await screen.findByText("取り消しました: package.add")).toBeInTheDocument();
      await act(async () => {
        answer(before);
        await vi.advanceTimersByTimeAsync(50);
      });
      expect(screen.queryByText('uv add "pyjwt>=2.9"')).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("shows no grants panel when nothing is allowed for the task", async () => {
    const { source } = fakeTaskSource();
    const grants = vi.spyOn(source, "listTaskGrants");
    renderAt(`/agents/${TASK_204}`, source);
    await screen.findByRole("link", { name: "確認" });
    expect(grants).toHaveBeenCalledWith(TASK_204);
    expect(screen.queryByRole("region", { name: "このタスクで許可中" })).toBeNull();
  });

  it("keeps the grant and says why when revoking fails", async () => {
    const { source } = fakeTaskSource();
    await source.decideApproval("approval-1", "approve_for_task");
    vi.spyOn(source, "revokeTaskGrant").mockRejectedValue(
      new ApiError(503, "approvals_unavailable", "x"),
    );
    renderAt(`/agents/${TASK_204}`, source);
    const panel = await screen.findByRole("region", { name: "このタスクで許可中" });
    const user = userEvent.setup();
    await user.click(within(panel).getByRole("button", { name: "取り消す: package.add" }));
    expect(await within(panel).findByRole("alert")).toHaveTextContent(
      "いま承認を受け付けられません",
    );
    expect(within(panel).getByText("package.add")).toBeInTheDocument();
  });
});
