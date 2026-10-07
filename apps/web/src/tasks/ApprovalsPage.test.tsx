import { render, screen, within } from "@testing-library/react";
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
    // The Backend grants one call: there is no "for this task" choice.
    expect(within(sheet).queryByRole("button", { name: "このタスクの間は許可" })).toBeNull();
    const user = userEvent.setup();
    await user.click(within(sheet).getByRole("button", { name: "今回だけ許可" }));
    expect(await screen.findByText("許可しました: package.add")).toBeInTheDocument();
    expect(decisions).toEqual([{ id: "approval-1", decision: "approve" }]);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(window.location.pathname).toBe("/approvals");
    expect(screen.getAllByRole("link", { name: /APPROVAL/ })).toHaveLength(1);
  });

  it("only rejects a strong approval here", async () => {
    const { source, decisions } = fakeTaskSource();
    renderAt("/approvals/approval-2", source);
    const sheet = await screen.findByRole("dialog", { name: "この操作を許可しますか" });
    expect(within(sheet).queryByRole("button", { name: "今回だけ許可" })).toBeNull();
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
});
