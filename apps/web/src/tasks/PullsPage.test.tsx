import { getDefaultNormalizer, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { ApiError } from "../api/client";
import { mockApi, Providers, reply, session } from "../test/helpers";
import { fakeTaskSource, PR_42, samplePullRequests, TASK_203 } from "../test/tasks";
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
    // The implementation row comes with the changed files (issue #185 item 6).
    await within(conditions).findByText("実装");
    const rows = within(conditions).getAllByRole("listitem");
    expect(rows.map((row) => row.textContent)).toEqual([
      "実装3 ファイル",
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

  it("reads a selected pull request the list does not hold by its id", async () => {
    const [older, ...newer] = samplePullRequests();
    if (!older) throw new Error("no sample");
    const { source } = fakeTaskSource(undefined, newer);
    const getOne = vi.spyOn(source, "getPullRequest").mockResolvedValue(older);
    renderPulls(`/pulls/${older.id}`, source);
    expect(await screen.findByRole("heading", { name: older.title, level: 2 })).toBeInTheDocument();
    expect(getOne).toHaveBeenCalledWith(older.id);
    const list = screen.getByRole("navigation", { name: "プルリクエストの一覧" });
    expect(within(list).queryByText(older.title)).not.toBeInTheDocument();
  });

  it("retries an exact read that failed instead of hiding the pull request", async () => {
    // Codex P2 on PR #206: a transient failure left the routed PR blank for good.
    const [older, ...newer] = samplePullRequests();
    if (!older) throw new Error("no sample");
    const { source } = fakeTaskSource(undefined, newer);
    const getOne = vi
      .spyOn(source, "getPullRequest")
      .mockRejectedValueOnce(new ApiError(503, "service_unavailable", "x"))
      .mockResolvedValue(older);
    renderPulls(`/pulls/${older.id}`, source);
    expect(await screen.findByRole("alert")).toHaveTextContent("一時的に利用できません");
    expect(screen.queryByText(/このプルリクエストは見つかりませんでした/)).not.toBeInTheDocument();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "再試行" }));
    expect(await screen.findByRole("heading", { name: older.title, level: 2 })).toBeInTheDocument();
    expect(getOne).toHaveBeenCalledTimes(2);
  });

  it("says a pull request that does not exist was not found", async () => {
    const { source } = fakeTaskSource();
    renderPulls("/pulls/pr-missing", source);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "このプルリクエストは見つかりませんでした",
    );
    expect(screen.queryByRole("button", { name: "再試行" })).not.toBeInTheDocument();
  });

  it("does not crash on a malformed escape in the path", async () => {
    const { source } = fakeTaskSource();
    renderPulls("/pulls/%", source);
    expect(await screen.findByText("プルリクエストを選んでください。")).toBeInTheDocument();
  });

  it("shows the changed files, the reviewers and the audit rows of the record", async () => {
    const { source } = fakeTaskSource();
    renderPulls(`/pulls/${PR_42}`, source);
    const files = await screen.findByRole("region", { name: "変更されたファイル" });
    expect(await within(files).findByText("3 ファイル")).toBeInTheDocument();
    expect(within(files).getByText("+118")).toBeInTheDocument();
    expect(within(files).getByText("−54")).toBeInTheDocument();
    const session = within(files).getByRole("link", { name: "apps/backend/auth/session.py" });
    expect(session).toHaveAttribute("href", `/pulls/${PR_42}/files/0`);
    expect(within(files).getByRole("link", { name: "差分を開く" })).toHaveAttribute(
      "href",
      `/pulls/${PR_42}/files/0`,
    );

    const review = screen.getByRole("region", { name: "レビューの要点" });
    expect(await within(review).findByText("Codex — 完了")).toBeInTheDocument();
    expect(within(review).getByText("Claude — 完了")).toBeInTheDocument();

    const audit = screen.getByRole("region", { name: "監査" });
    const rows = await within(audit).findAllByRole("listitem");
    expect(rows).toHaveLength(4);
    expect(rows[2]).toHaveTextContent(/tool\.write_file DENY path_out_of_scope/);
    expect(rows[2]).toHaveClass("decision-deny");

    const merge = screen.getByRole("region", { name: "マージ" });
    expect(within(merge).getByRole("link", { name: "差分を見る" })).toHaveAttribute(
      "href",
      `/pulls/${PR_42}/files/0`,
    );
  });

  it("says when the changed files were not recorded", async () => {
    const { source } = fakeTaskSource();
    renderPulls("/pulls/pr-43", source);
    const files = await screen.findByRole("region", { name: "変更されたファイル" });
    expect(
      await within(files).findByText(/変更されたファイルは記録されていません/),
    ).toBeInTheDocument();
    const conditions = screen.getByRole("region", { name: "完了条件" });
    expect(within(conditions).queryByText("実装")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "差分を見る" })).not.toBeInTheDocument();
  });

  it("shows one file's diff and moves between the files", async () => {
    const { source } = fakeTaskSource();
    renderPulls(`/pulls/${PR_42}/files/0`, source);
    expect(
      await screen.findByRole("heading", { name: "apps/backend/auth/session.py", level: 2 }),
    ).toBeInTheDocument();
    expect(screen.getByText("1 / 3 ファイル · Task 2030c0de")).toBeInTheDocument();
    expect(screen.getByText("2 か所")).toBeInTheDocument();
    // The diff keeps its indentation.
    const exact = { normalizer: getDefaultNormalizer({ trim: false, collapseWhitespace: false }) };
    const added = screen.getByText("+    now = datetime.now(timezone.utc)", exact);
    expect(added).toHaveClass("diff-add");
    expect(screen.getByText("-        raise SessionExpired()", exact)).toHaveClass("diff-delete");
    expect(screen.getByText("@@ -168,4 +172,6 @@")).toHaveClass("diff-hunk");
    // The first file has no previous one.
    expect(screen.queryByRole("link", { name: "前のファイル" })).not.toBeInTheDocument();
    const user = userEvent.setup();
    await user.click(screen.getByRole("link", { name: "次のファイル" }));
    expect(
      await screen.findByRole("heading", { name: "apps/backend/auth/tokens.py", level: 2 }),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "前のファイル" })).toHaveAttribute(
      "href",
      `/pulls/${PR_42}/files/0`,
    );
    await user.click(screen.getByRole("link", { name: "ファイル一覧" }));
    const files = await screen.findByRole("region", { name: "変更されたファイル" });
    expect(within(files).getAllByRole("listitem")).toHaveLength(3);
    await user.click(screen.getByRole("link", { name: "プルリクエストへ戻る" }));
    expect(await screen.findByRole("region", { name: "完了条件" })).toBeInTheDocument();
  });

  it("says why a file has no diff", async () => {
    const { source } = fakeTaskSource();
    vi.spyOn(source, "getFileDiff").mockImplementation(async (_id, index) => ({
      index,
      path: "logo.png",
      previousPath: null,
      status: "added",
      additions: 0,
      deletions: 0,
      hasPatch: false,
      patchTruncated: false,
      count: 3,
      patch: null,
    }));
    renderPulls(`/pulls/${PR_42}/files/2`, source);
    expect(await screen.findByText(/このファイルの差分は記録されていません/)).toBeInTheDocument();
  });

  it("says when a file is not found", async () => {
    const { source } = fakeTaskSource();
    renderPulls(`/pulls/${PR_42}/files/9`, source);
    expect(
      await screen.findByText("このファイルの差分は見つかりませんでした。"),
    ).toBeInTheDocument();
  });
});
