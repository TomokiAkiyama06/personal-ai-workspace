import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { mockApi, Providers, reply, session } from "../test/helpers";
import { designMemories, FakeMemorySource, REPO_ID } from "../test/memoryFake";
import { type MemorySource, MemorySourceProvider } from "./source";

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderMemory(path: string, source: MemorySource | null) {
  mockApi({ "GET /auth/session": reply(200, session()) });
  window.history.replaceState(null, "", path);
  return render(
    <Providers>
      <MemorySourceProvider source={source}>
        <App />
      </MemorySourceProvider>
    </Providers>,
  );
}

function designSource() {
  const data = designMemories();
  const source = new FakeMemorySource(data);
  return { source, mergeId: data.mergeId };
}

describe("Memory screen", () => {
  it("says the memories cannot be shown yet without a source (no Memory API)", async () => {
    renderMemory("/memory", null);
    expect(await screen.findByRole("heading", { name: "メモリ" })).toBeInTheDocument();
    expect(screen.getByText(/API が Backend にまだないため/)).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "スコープ" })).not.toBeInTheDocument();
  });

  it("shows the scope tree, the memory list and the detail (3 panes)", async () => {
    const { source } = designSource();
    renderMemory("/memory", source);
    const user = userEvent.setup();

    const scopes = await screen.findByRole("navigation", { name: "スコープ" });
    expect(within(scopes).getByRole("button", { name: /ユーザー\s*8/ })).toHaveAttribute(
      "aria-current",
      "true",
    );
    const project = within(scopes).getByRole("button", { name: /ExampleProject/ });
    expect(project).toHaveAttribute("aria-expanded", "true");
    await user.click(within(scopes).getByRole("button", { name: /backend\s*7/ }));
    await waitFor(() =>
      expect(source.calls.at(-1)).toEqual({
        method: "list",
        args: [{ kind: "repo", project_id: "p-example", repo_id: REPO_ID }, ""],
      }),
    );

    const list = await screen.findByRole("list", { name: "メモリ一覧" });
    const cards = within(list).getAllByRole("link");
    expect(cards.map((card) => card.textContent)).toEqual([
      expect.stringMatching(/Merge は必ず人が承認する.*確定.*project · 新しい/),
      expect.stringMatching(/Merge の前に人の確認を求めた.*観測.*project · 要確認/),
      expect.stringMatching(/ピン留め.*UI の表示言語は日本語.*確定.*user · 新しい/),
      expect.stringMatching(/レビューは Codex と Claude の両方.*推定.*project · 要確認/),
      expect.stringMatching(/Lint は旧設定を使う.*置き換え済み.*repo\/backend · 古い/),
    ]);
    expect(
      screen.getByText("メモリを選ぶと、本文・履歴・ソース・詳細が表示されます。"),
    ).toBeVisible();

    await user.click(cards[0] as HTMLElement);
    expect(window.location.pathname).toBe("/memory/m-merge");
    expect(await screen.findByRole("heading", { name: "Merge は必ず人が承認する" })).toBeVisible();
    expect(cards[0]).toHaveAttribute("aria-current", "page");
    const tabs = screen.getByRole("tablist", { name: "メモリの表示" });
    expect(
      within(tabs)
        .getAllByRole("tab")
        .map((tab) => tab.textContent),
    ).toEqual(["本文", "履歴", "ソース", "詳細"]);
    expect(within(tabs).getByRole("tab", { name: "本文" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(
      screen.getByText(/ユーザーがその Task \/ Session で明示的に許可した場合のみ/),
    ).toBeVisible();

    await user.click(within(scopes).getByRole("button", { name: "すべて閉じる" }));
    expect(project).toHaveAttribute("aria-expanded", "false");
    expect(within(scopes).queryByRole("button", { name: /backend/ })).not.toBeInTheDocument();
  });

  it("filters by status and by 要確認", async () => {
    const { source } = designSource();
    renderMemory("/memory", source);
    const user = userEvent.setup();
    const list = await screen.findByRole("list", { name: "メモリ一覧" });
    expect(within(list).getAllByRole("link")).toHaveLength(5);

    await user.click(screen.getByRole("button", { name: "要確認 2" }));
    expect(
      within(list)
        .getAllByRole("link")
        .map((card) => card.textContent),
    ).toEqual([
      expect.stringContaining("Merge の前に人の確認を求めた"),
      expect.stringContaining("レビューは Codex と Claude の両方"),
    ]);
    await user.click(screen.getByRole("button", { name: "要確認 2" }));

    await user.click(screen.getByRole("button", { name: "状態" }));
    await user.click(screen.getByRole("button", { name: "置き換え済み・廃止" }));
    expect(screen.getByRole("button", { name: "状態: 置き換え済み・廃止" })).toBeVisible();
    expect(
      within(list)
        .getAllByRole("link")
        .map((card) => card.textContent),
    ).toEqual([expect.stringContaining("Lint は旧設定を使う")]);
  });

  it("searches text through the source", async () => {
    const { source } = designSource();
    renderMemory("/memory", source);
    const user = userEvent.setup();
    await screen.findByRole("list", { name: "メモリ一覧" });
    await user.type(screen.getByRole("searchbox", { name: "メモリを検索" }), "flake8");
    await waitFor(() =>
      expect(source.calls.at(-1)).toEqual({ method: "list", args: [{ kind: "user" }, "flake8"] }),
    );
    const list = await screen.findByRole("list", { name: "メモリ一覧" });
    await waitFor(() => expect(within(list).getAllByRole("link")).toHaveLength(1));
  });

  it("draws the history graph and shows a past version when its node is chosen", async () => {
    const { source } = designSource();
    renderMemory("/memory/m-merge", source);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("tab", { name: "履歴" }));

    const graph = screen.getByRole("list", { name: "メモリの履歴グラフ" });
    const nodes = within(graph).getAllByRole("button");
    expect(nodes.map((node) => node.textContent)).toEqual([
      expect.stringMatching(/^現在 \/ 確定v3 · .* · Tomoki$/),
      expect.stringMatching(/^置き換え済み · 推定v2 · .* · Codex$/),
      expect.stringMatching(/^関連: Merge の前に人の確認を求めたextends · v1 · /),
      expect.stringMatching(/^置き換え済みv1 · .* · Tomoki$/),
    ]);
    expect(nodes[0]).toHaveAttribute("aria-pressed", "true");
    const card = screen.getByRole("region", { name: "選択中のバージョン" });
    expect(within(card).getByText("supersedes v2, confirmed_from v2")).toBeVisible();
    expect(within(card).getByText("ユーザー確認")).toBeVisible();
    // The current, active version cannot be "restored" (it would change nothing).
    expect(
      within(card).queryByRole("button", { name: "この内容で新しい版を作る" }),
    ).not.toBeInTheDocument();

    await user.click(nodes[1] as HTMLElement);
    const past = screen.getByRole("region", { name: "選択中のバージョン" });
    expect(
      within(past).getByText("main への Merge はエージェントが自動で実行しない。"),
    ).toBeVisible();
    expect(
      within(past).getByText(
        "supersedes v1, extends Merge の前に人の確認を求めた v1, v3 supersedes, v3 confirmed_from",
      ),
    ).toBeVisible();
    await user.click(within(past).getByRole("button", { name: "差分を並べて見る" }));
    expect(within(past).getByText("選択中（v2）")).toBeVisible();
    expect(within(past).getByText("現在（v3）")).toBeVisible();
    const added = past.querySelector("ins");
    expect(added?.textContent).toContain(
      "ユーザーがその Task / Session で明示的に許可した場合のみ",
    );

    // A related memory's version can be looked at, not restored into this one.
    await user.click(nodes[2] as HTMLElement);
    expect(
      within(screen.getByRole("region", { name: "選択中のバージョン" })).queryByRole("button", {
        name: "この内容で新しい版を作る",
      }),
    ).not.toBeInTheDocument();
  });

  it("restores a past version as a new active version, never the old one", async () => {
    const { source, mergeId } = designSource();
    renderMemory("/memory/m-merge", source);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("tab", { name: "履歴" }));
    const graph = screen.getByRole("list", { name: "メモリの履歴グラフ" });
    await user.click(within(graph).getAllByRole("button")[3] as HTMLElement);
    await user.click(screen.getByRole("button", { name: "この内容で新しい版を作る" }));

    expect(source.calls).toContainEqual({ method: "restore", args: [mergeId, 3, 1] });
    expect(await screen.findByText("v1 の内容で新しい版 v4 を作りました。")).toBeVisible();
    expect(await screen.findByRole("heading", { name: "Merge は Agent が判断する" })).toBeVisible();
    const nodes = within(screen.getByRole("list", { name: "メモリの履歴グラフ" })).getAllByRole(
      "button",
    );
    expect(nodes[0]?.textContent).toMatch(/^現在 \/ 確定v4 · /);
    expect(nodes[0]).toHaveAttribute("aria-pressed", "true");
    // v1 itself stays superseded.
    expect(nodes.at(-1)?.textContent).toMatch(/^置き換え済みv1 · /);
  });

  it("edits into a new version with the version it started from", async () => {
    const { source, mergeId } = designSource();
    renderMemory("/memory/m-merge", source);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "編集" }));
    const content = screen.getByRole("textbox", { name: "本文" });
    await user.clear(content);
    await user.type(content, "Merge は人だけが行う。");
    await user.type(screen.getByRole("textbox", { name: "変更理由（任意）" }), "簡潔にする");
    await user.click(screen.getByRole("button", { name: "保存" }));

    expect(source.calls).toContainEqual({
      method: "edit",
      args: [mergeId, 3, { content: "Merge は人だけが行う。", reason: "簡潔にする" }],
    });
    expect(await screen.findByText("新しい版 v4 を保存しました。")).toBeVisible();
    expect(await screen.findByText("Merge は人だけが行う。")).toBeVisible();
    expect(screen.queryByRole("textbox", { name: "本文" })).not.toBeInTheDocument();
  });

  it("shows the conflict notice and the difference when someone else saved first", async () => {
    const { source, mergeId } = designSource();
    renderMemory("/memory/m-merge", source);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "編集" }));
    const content = screen.getByRole("textbox", { name: "本文" });
    await user.clear(content);
    await user.type(content, "自分の案");

    // Codex writes v4 while the form is open.
    source.write(mergeId, { content: "Codex の更新" }, "codex", {
      actor_type: "agent",
      actor_user_id: null,
      actor_name: "Codex",
      created_at: "2026-09-18T05:29:00Z",
    });
    await user.click(screen.getByRole("button", { name: "保存" }));

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("編集中に別の更新がありました")).toBeVisible();
    expect(within(alert).getByText(/に Codex がこのメモリを更新しています。/)).toBeVisible();
    // The draft is kept.
    expect(screen.getByRole("textbox", { name: "本文" })).toHaveValue("自分の案");

    await user.click(within(alert).getByRole("button", { name: "差分を見る" }));
    expect(screen.getByText("最新の版（v4）")).toBeVisible();
    expect(screen.getByText("自分の変更")).toBeVisible();
    expect(document.querySelector(".memory-conflict-wrap del")?.textContent).toContain(
      "Codex の更新",
    );
    expect(document.querySelector(".memory-conflict-wrap ins")?.textContent).toContain("自分の案");

    // Saving again now goes on top of the version that won.
    await user.click(screen.getByRole("button", { name: "保存" }));
    expect(
      source.calls.filter((call) => call.method === "edit").map((call) => call.args[1]),
    ).toEqual([3, 4]);
    expect(await screen.findByText("新しい版 v5 を保存しました。")).toBeVisible();
    expect(screen.queryByText("編集中に別の更新がありました")).not.toBeInTheDocument();
  });

  it("keeps the other writer's change of a field the editor did not touch", async () => {
    const { source, mergeId } = designSource();
    renderMemory("/memory/m-merge", source);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "編集" }));
    const content = screen.getByRole("textbox", { name: "本文" });
    await user.clear(content);
    await user.type(content, "自分の本文");

    // Someone else renames the memory while the form is open.
    source.write(mergeId, { title: "Merge は Human だけ" }, null, { actor_user_id: "u-2" });
    await user.click(screen.getByRole("button", { name: "保存" }));
    await screen.findByRole("alert");
    await user.click(screen.getByRole("button", { name: "保存" }));

    const edits = source.calls.filter((call) => call.method === "edit").map((call) => call.args);
    expect(edits).toEqual([
      [mergeId, 3, { content: "自分の本文" }],
      [mergeId, 4, { content: "自分の本文" }],
    ]);
    expect(await screen.findByRole("heading", { name: "Merge は Human だけ" })).toBeVisible();
  });

  it("offers restore only where the Backend can restore", async () => {
    const { source, mergeId } = designSource();
    // v3 retired by another memory: the memory's current version is superseded.
    source.versions = source.versions.map((entry) =>
      entry.memory_id === mergeId && entry.version_number === 3
        ? { ...entry, status: "superseded" }
        : entry,
    );
    // v1 was a session-only memory (a person cannot write that freshness).
    source.versions = source.versions.map((entry) =>
      entry.memory_id === mergeId && entry.version_number === 1
        ? { ...entry, freshness_policy: "session_only" }
        : entry,
    );
    renderMemory("/memory/m-merge", source);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("tab", { name: "履歴" }));
    const nodes = within(screen.getByRole("list", { name: "メモリの履歴グラフ" })).getAllByRole(
      "button",
    );
    for (const index of [0, 1, 3]) {
      await user.click(nodes[index] as HTMLElement);
      expect(
        screen.queryByRole("button", { name: "この内容で新しい版を作る" }),
        `node ${index}`,
      ).not.toBeInTheDocument();
    }
  });

  it("does not restore an expired expiring version (the Backend needs a new freshness)", async () => {
    const { source, mergeId } = designSource();
    source.versions = source.versions.map((entry) =>
      entry.memory_id === mergeId && entry.version_number === 1
        ? { ...entry, freshness_policy: "expiring", expires_at: "2026-07-01T00:00:00Z" }
        : entry,
    );
    renderMemory("/memory/m-merge", source);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("tab", { name: "履歴" }));
    const nodes = within(screen.getByRole("list", { name: "メモリの履歴グラフ" })).getAllByRole(
      "button",
    );
    await user.click(nodes[3] as HTMLElement);
    expect(
      screen.queryByRole("button", { name: "この内容で新しい版を作る" }),
    ).not.toBeInTheDocument();
    await user.click(nodes[1] as HTMLElement);
    expect(screen.getByRole("button", { name: "この内容で新しい版を作る" })).toBeVisible();
  });

  it("shows the relations into the selected version too", async () => {
    const { source } = designSource();
    renderMemory("/memory/m-merge", source);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("tab", { name: "履歴" }));
    const nodes = within(screen.getByRole("list", { name: "メモリの履歴グラフ" })).getAllByRole(
      "button",
    );
    await user.click(nodes[2] as HTMLElement);
    expect(
      within(screen.getByRole("region", { name: "選択中のバージョン" })).getByText("v2 extends"),
    ).toBeVisible();
    await user.click(nodes[3] as HTMLElement);
    expect(
      within(screen.getByRole("region", { name: "選択中のバージョン" })).getByText("v2 supersedes"),
    ).toBeVisible();
  });

  it("discards the draft from the conflict notice", async () => {
    const { source, mergeId } = designSource();
    renderMemory("/memory/m-merge", source);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "編集" }));
    await user.type(screen.getByRole("textbox", { name: "本文" }), "追記");
    source.write(mergeId, { content: "他の人の更新" }, null, { actor_user_id: "u-2" });
    await user.click(screen.getByRole("button", { name: "保存" }));
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText(/に 別のユーザー がこのメモリを更新しています。/)).toBeVisible();
    await user.click(within(alert).getByRole("button", { name: "自分の変更を破棄" }));
    expect(screen.queryByRole("textbox", { name: "本文" })).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(await screen.findByText("他の人の更新")).toBeVisible();
  });

  it("does not offer 編集 for a memory that is not active", async () => {
    const { source } = designSource();
    renderMemory("/memory/m-lint", source);
    expect(await screen.findByRole("heading", { name: "Lint は旧設定を使う" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "編集" })).not.toBeInTheDocument();
  });

  it("lists the sources of the chosen version, deleted ones marked", async () => {
    const { source, mergeId } = designSource();
    renderMemory("/memory/m-merge", source);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("tab", { name: "ソース" }));
    expect(await screen.findByText("会話 1842")).toBeVisible();
    expect(screen.getByText("Task 188")).toBeVisible();
    expect(screen.getByText("ユーザーの確認")).toBeVisible();
    expect(source.calls).toContainEqual({ method: "sources", args: [mergeId, 3] });

    await user.click(screen.getByRole("tab", { name: "履歴" }));
    const graph = screen.getByRole("list", { name: "メモリの履歴グラフ" });
    await user.click(within(graph).getAllByRole("button")[1] as HTMLElement);
    await user.click(screen.getByRole("tab", { name: "ソース" }));
    expect(await screen.findByText(/^削除済み（/)).toBeVisible();
    expect(screen.getByRole("heading", { name: "v2 のソース" })).toBeVisible();
  });

  it("shows the details of the version", async () => {
    const { source } = designSource();
    renderMemory("/memory/m-merge", source);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("tab", { name: "詳細" }));
    const panel = screen.getByRole("tabpanel");
    expect(within(panel).getByText("Project スコープ")).toBeVisible();
    expect(within(panel).getByText("定期的に再確認")).toBeVisible();
    expect(within(panel).getByText("90 日")).toBeVisible();
    expect(within(panel).getByText("member_changed")).toBeVisible();
  });

  it("moves between tabs with the arrow keys", async () => {
    const { source } = designSource();
    renderMemory("/memory/m-merge", source);
    const user = userEvent.setup();
    const body = await screen.findByRole("tab", { name: "本文" });
    body.focus();
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: "履歴" })).toHaveFocus();
    expect(screen.getByRole("tab", { name: "履歴" })).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{ArrowLeft}{ArrowLeft}");
    expect(screen.getByRole("tab", { name: "詳細" })).toHaveFocus();
  });

  it("shows the Backend's error for a memory it does not find", async () => {
    const { source } = designSource();
    renderMemory("/memory/m-missing", source);
    expect(await screen.findByText("メモリが見つかりません。")).toBeVisible();
  });
});
