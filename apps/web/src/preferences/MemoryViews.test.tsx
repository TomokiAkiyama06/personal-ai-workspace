import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { MemorySourceProvider } from "../memory/source";
import { mockApi, Providers, reply, session } from "../test/helpers";
import { designMemories, FakeMemorySource, PROJECT_ID } from "../test/memoryFake";
import { FakePreferenceSource } from "../test/preferenceFake";
import { type PreferenceSource, PreferenceSourceProvider } from "./source";

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderMemory(path: string, source: PreferenceSource | null = new FakePreferenceSource()) {
  mockApi({ "GET /auth/session": reply(200, session()) });
  window.history.replaceState(null, "", path);
  render(
    <Providers>
      <MemorySourceProvider source={new FakeMemorySource(designMemories())}>
        <PreferenceSourceProvider source={source}>
          <App />
        </PreferenceSourceProvider>
      </MemorySourceProvider>
    </Providers>,
  );
}

const navigation = () => screen.findByRole("navigation", { name: "メインナビゲーション" });

describe("メモリ › 推定の候補 / 保留中", () => {
  it("badges メモリ with the ready candidates and the held items", async () => {
    renderMemory("/memory");
    const memory = within(await navigation()).getByRole("link", { name: /メモリ/ });
    await waitFor(() =>
      expect(memory).toHaveTextContent(/メモリ5 件の好みの確認が答えを待っています/),
    );
  });

  it("has no badge, chips or section without a source", async () => {
    renderMemory("/memory", null);
    const scopes = await screen.findByRole("navigation", { name: "スコープ" });
    expect(within(scopes).queryByText("確認")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /推定の候補/ })).not.toBeInTheDocument();
    expect(within(await navigation()).getByRole("link", { name: "メモリ" })).toBeInTheDocument();
  });

  it("lists the candidates ready to ask first, with their state in words", async () => {
    renderMemory("/memory/candidates");
    const list = await screen.findByRole("list", { name: "候補の一覧" });
    const cards = within(list).getAllByRole("link");
    expect(cards.map((card) => card.textContent)).toEqual([
      expect.stringMatching(
        /pytest は -x を付けて実行する推定チャットで確認待ち観測 4 · 推奨 このRepoだけ/,
      ),
      expect.stringMatching(
        /レビューは Codex と Claude の両方推定チャットで確認待ち観測 3 · 推奨 このProject/,
      ),
      expect.stringMatching(
        /コミット前に ruff format を実行する推定まだ聞かない観測 2 · あと 1 回で確認/,
      ),
      expect.stringMatching(/lint を飛ばしてテストだけ回す推定聞かない「今回だけ」の発言のみ/),
      expect.stringMatching(/Lint は旧設定を使う推定食い違い/),
    ]);
    const scopes = screen.getByRole("navigation", { name: "スコープ" });
    expect(within(scopes).getByRole("link", { name: /推定の候補\s*5/ })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(within(scopes).getByRole("link", { name: /保留中\s*3/ })).toBeInTheDocument();
    expect(screen.getByText("候補を選ぶと、根拠と答えるボタンが表示されます。")).toBeVisible();
    expect(
      within(scopes).getByText(
        "候補はあなたにだけ見えます。確定するまで自分のメモリ（Private）に置かれます。",
      ),
    ).toBeInTheDocument();
  });

  it("shows a candidate's evidence and answers it from Memory", async () => {
    const source = new FakePreferenceSource();
    renderMemory("/memory/candidates/m-review", source);
    const user = userEvent.setup();
    expect(
      await screen.findByRole("heading", { name: "レビューは Codex と Claude の両方" }),
    ).toBeVisible();
    expect(screen.getByRole("link", { name: "メモリの履歴を開く" })).toHaveAttribute(
      "href",
      "/memory/m-review",
    );
    const panel = screen.getByRole("tabpanel");
    for (const text of ["3 回", "Project 1 · Repo 2", "続けてほしい", "一貫", "低"]) {
      expect(within(panel).getByText(text)).toBeInTheDocument();
    }
    expect(
      within(panel).getByText(/同じ Project の複数 Repo なので「このProject」を推奨。/),
    ).toBeInTheDocument();
    expect(panel.textContent).not.toMatch(/frequency|scope_diversity|standing|consistent/);
    // Seen in two repositories: the Repo button names the one seen most.
    expect(screen.getByRole("button", { name: /このRepoだけ/ })).toHaveTextContent(
      "backend · 最も多く観測",
    );
    await user.click(screen.getByRole("button", { name: /このProject/ }));
    expect(source.calls.find((call) => call.method === "confirm")?.args).toEqual([
      "m-review",
      { scope: "project", project_id: PROJECT_ID, repo_id: null, acknowledge_high_risk: false },
    ]);
    expect(await screen.findByRole("status")).toHaveTextContent(
      /保存しました.*ExampleProject · v2/,
    );
    // It leaves the list.
    await waitFor(() =>
      expect(
        within(screen.getByRole("list", { name: "候補の一覧" })).queryByText(
          "レビューは Codex と Claude の両方",
        ),
      ).not.toBeInTheDocument(),
    );
  });

  it("shows the held items with what each kind allows", async () => {
    const source = new FakePreferenceSource();
    renderMemory("/memory/held/e-merge.0", source);
    const user = userEvent.setup();
    const list = await screen.findByRole("list", { name: "候補の一覧" });
    expect(
      within(list)
        .getAllByRole("link")
        .map((card) => card.textContent),
    ).toEqual([
      expect.stringMatching(
        /CI が通った PR は確認なしで main にマージする高リスク保留観測 3 · 明示の確認が必要/,
      ),
      expect.stringMatching(/UI の表示言語は英語確定済みと違う確定済みのメモリと違う内容/),
      expect.stringMatching(/PR の説明は英語で書く広げたメモリ広げたメモリへの変更/),
    ]);
    expect(screen.getByText("保留の種類とできること")).toBeInTheDocument();
    expect(screen.getByText("高リスクで保留")).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/HELD_HIGH_RISK|held_high_risk/);
    const save = screen.getByRole("button", { name: "確認して保存" });
    expect(save).toBeDisabled();
    await user.click(screen.getByRole("checkbox"));
    await user.click(save);
    expect(source.calls.find((call) => call.method === "confirm")?.args).toEqual([
      "e-merge.0",
      { scope: "project", project_id: PROJECT_ID, repo_id: null, acknowledge_high_risk: true },
    ]);
  });

  it("offers only the same project or the person for a widened memory", async () => {
    renderMemory("/memory/held/e-pr.0");
    await screen.findByRole("heading", { name: "PR の説明は英語で書く" });
    expect(screen.getByText(/同じ Project に留めるか、自分だけに狭めて保存します/)).toBeVisible();
    expect(screen.queryByRole("button", { name: /このRepoだけ/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /このProject/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /すべてのProject/ })).toBeInTheDocument();
  });

  it("says when the selected candidate is gone", async () => {
    renderMemory("/memory/candidates/m-unknown");
    expect(
      await screen.findByText("この候補はもうありません。答え済みか、内容が変わりました。"),
    ).toBeVisible();
  });

  it("leaves the candidates for a scope from the scope tree", async () => {
    renderMemory("/memory/candidates");
    const user = userEvent.setup();
    const scopes = await screen.findByRole("navigation", { name: "スコープ" });
    await user.click(within(scopes).getByRole("button", { name: /ユーザー/ }));
    expect(window.location.pathname).toBe("/memory");
    expect(await screen.findByRole("list", { name: "メモリ一覧" })).toBeInTheDocument();
  });
});
