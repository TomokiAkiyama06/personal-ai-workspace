import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { mockApi, Providers, reply, session } from "../test/helpers";
import { fakeTaskSource, samplePullRequests, TASK_203 } from "../test/tasks";
import { type TaskSource, TaskSourceProvider } from "./source";

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderPulls(path: string, source?: TaskSource) {
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

describe("Pull requests page", () => {
  it("says the data is not available while no source is connected", async () => {
    renderPulls("/pulls");
    expect(
      await screen.findByRole("heading", { name: "プルリクエスト", level: 1 }),
    ).toBeInTheDocument();
    expect(screen.getByText("タスクの状態はまだ表示できません")).toBeInTheDocument();
  });

  it("shows the completion conditions and keeps merging to the human", async () => {
    const { source } = fakeTaskSource();
    renderPulls("/pulls", source);
    expect(
      await screen.findByRole("heading", { name: "認証セッションの失効判定を一本化する" }),
    ).toBeInTheDocument();
    const conditions = screen.getByRole("region", { name: "完了条件" });
    const rows = within(conditions).getAllByRole("listitem");
    expect(rows.map((row) => row.textContent)).toEqual([
      "プルリクエストopen",
      "テスト / EvaluatorPASS",
      "レビュー承認",
      "人によるマージ承認未実施",
    ]);
    const merge = screen.getByRole("region", { name: "マージ" });
    // No merge API: the button is there but disabled, and says why.
    expect(within(merge).getByRole("button", { name: "マージする" })).toBeDisabled();
    expect(within(merge).getByRole("button", { name: "マージする" })).toHaveAccessibleDescription(
      /GitHub の PR でマージしてください/,
    );
    const github = within(merge).getByRole("link", { name: "GitHub で開く" });
    expect(github).toHaveAttribute("href", "https://github.com/example/backend/pull/42");
    expect(github).toHaveAttribute("rel", "noopener noreferrer");
    expect(within(merge).getByRole("link", { name: "タスクを開く" })).toHaveAttribute(
      "href",
      `/agents/${TASK_203}`,
    );
    expect(screen.getByText(/それは Merge の許可にはなりません/)).toBeInTheDocument();
  });

  it("filters Merge Ready / in review / draft and never links a non-https URL", async () => {
    const pullRequests = samplePullRequests();
    const draft = pullRequests[1];
    if (draft) draft.url = "javascript:alert(1)";
    const { source } = fakeTaskSource(undefined, pullRequests);
    renderPulls("/pulls/pr-43", source);
    const user = userEvent.setup();
    await screen.findByRole("heading", { name: "ログ出力の整理", level: 2 });
    expect(screen.queryByRole("link", { name: "GitHub で開く" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Merge Ready 1" }));
    const list = screen.getByRole("navigation", { name: "プルリクエストの一覧" });
    expect(within(list).getAllByRole("link")).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "下書き 1" }));
    expect(within(list).getByRole("link")).toHaveTextContent("ログ出力の整理");
  });

  it("reads open pull requests again while the screen is open", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const { source } = fakeTaskSource();
      const list = vi.spyOn(source, "listPullRequests");
      renderPulls("/pulls", source);
      await screen.findByRole("heading", { name: "認証セッションの失効判定を一本化する" });
      const before = list.mock.calls.length;
      await vi.advanceTimersByTimeAsync(5000);
      expect(list.mock.calls.length).toBeGreaterThan(before);
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps reading the list while it is empty, so the first PR appears", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const pullRequests: ReturnType<typeof samplePullRequests> = [];
      const { source } = fakeTaskSource(undefined, pullRequests);
      renderPulls("/pulls", source);
      expect(
        await screen.findByText("エージェントが作ったプルリクエストはまだありません。"),
      ).toBeInTheDocument();
      pullRequests.push(...samplePullRequests());
      await vi.advanceTimersByTimeAsync(5000);
      expect(
        await screen.findByRole("heading", { name: "認証セッションの失効判定を一本化する" }),
      ).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("does not crash on a malformed escape in the path", async () => {
    const { source } = fakeTaskSource();
    renderPulls("/pulls/%", source);
    expect(await screen.findByText("プルリクエストを選んでください。")).toBeInTheDocument();
  });
});
