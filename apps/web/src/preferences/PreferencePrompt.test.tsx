import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { ApiError } from "../api/client";
import { MemorySourceProvider } from "../memory/source";
import { mockApi, Providers, reply, session } from "../test/helpers";
import { designMemories, FakeMemorySource, PROJECT_ID, REPO_ID } from "../test/memoryFake";
import { designCandidates, FakePreferenceSource } from "../test/preferenceFake";
import { type PreferenceSource, PreferenceSourceProvider } from "./source";

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderChat(source: PreferenceSource | null, path = "/") {
  const api = mockApi({ "GET /auth/session": reply(200, session()) });
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
  return api;
}

function phone() {
  vi.stubGlobal(
    "matchMedia",
    vi.fn((query: string) => ({
      matches: query === "(max-width: 767px)",
      media: query,
      addEventListener: () => {},
      removeEventListener: () => {},
    })),
  );
}

const writes = (source: FakePreferenceSource) =>
  source.calls.filter((call) => call.method !== "candidates");

describe("the chat's preference confirmation card", () => {
  it("shows nothing without a source", async () => {
    renderChat(null);
    expect(await screen.findByRole("heading", { name: /チャット/ })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "好みの確認" })).not.toBeInTheDocument();
  });

  it("asks about one candidate, the strongest ready one, with its evidence in words", async () => {
    renderChat(new FakePreferenceSource());
    const card = await screen.findByRole("region", { name: "好みの確認" });
    expect(screen.getAllByRole("region", { name: "好みの確認" })).toHaveLength(1);
    expect(within(card).getByText("この好みを覚えますか")).toBeInTheDocument();
    expect(
      within(card).getByRole("heading", { name: "pytest は -x を付けて実行する" }),
    ).toBeVisible();
    for (const chip of [
      "観測 4 回",
      "backend だけで観測",
      "「今後」などの発言あり",
      "食い違いなし",
      "リスク 低",
    ]) {
      expect(within(card).getByText(chip)).toBeInTheDocument();
    }
    // The buttons name the repository and the project; the recommended one is marked.
    const buttons = within(card).getAllByRole("button", {
      name: /このRepoだけ|このProject|すべてのProject/,
    });
    expect(buttons.map((button) => button.textContent)).toEqual([
      "このRepoだけ推奨backend",
      "このProjectExampleProject",
      "すべてのProject自分のメモリ",
    ]);
    // No API enum names on screen (the human's choice).
    expect(card.textContent).not.toMatch(/frequency|standing|consistent|default|required|low/);
  });

  it("saves at the chosen scope and folds into one line with the history link", async () => {
    const source = new FakePreferenceSource();
    renderChat(source);
    const user = userEvent.setup();
    const card = await screen.findByRole("region", { name: "好みの確認" });
    await user.click(within(card).getByRole("button", { name: /このRepoだけ/ }));
    expect(writes(source)).toEqual([
      {
        method: "confirm",
        args: [
          "m-pytest",
          { scope: "repo", project_id: PROJECT_ID, repo_id: REPO_ID, acknowledge_high_risk: false },
        ],
      },
    ]);
    const line = await screen.findByRole("status");
    expect(line).toHaveTextContent(/保存しました.*pytest は -x を付けて実行する.*backend · v2/);
    expect(within(line).getByRole("link", { name: "履歴" })).toHaveAttribute(
      "href",
      "/memory/m-pytest",
    );
    // At most one card per reply: the next ready candidate waits in Memory.
    expect(screen.queryByRole("region", { name: "好みの確認" })).not.toBeInTheDocument();
    // The list is read again after the answer.
    await waitFor(() =>
      expect(source.calls.filter((call) => call.method === "candidates").length).toBe(2),
    );
  });

  it("puts a candidate off with × without calling the API", async () => {
    const source = new FakePreferenceSource();
    renderChat(source);
    const user = userEvent.setup();
    const card = await screen.findByRole("region", { name: "好みの確認" });
    await user.click(
      within(card).getByRole("button", { name: "あとで聞く（候補はメモリに残ります）" }),
    );
    expect(screen.queryByRole("region", { name: "好みの確認" })).not.toBeInTheDocument();
    expect(writes(source)).toEqual([]);
  });

  it("[保存しない] rejects the candidate", async () => {
    const source = new FakePreferenceSource();
    renderChat(source);
    const user = userEvent.setup();
    const card = await screen.findByRole("region", { name: "好みの確認" });
    await user.click(within(card).getByRole("button", { name: "保存しない" }));
    expect(writes(source)).toEqual([{ method: "reject", args: ["m-pytest"] }]);
    expect(await screen.findByRole("status")).toHaveTextContent(
      /保存しないにしました.*同じ内容は今後聞きません/,
    );
  });

  it("needs the explicit acknowledgement for a high-risk candidate", async () => {
    const source = new FakePreferenceSource(
      designCandidates()
        .filter((item) => item.key === "merge.after_ci")
        .map((item) => ({ ...item, ready: true })),
    );
    renderChat(source);
    const user = userEvent.setup();
    const card = await screen.findByRole("region", { name: "好みの確認" });
    expect(within(card).getByText("高リスク")).toBeInTheDocument();
    expect(within(card).getByText(/推定だけでは保存せず/)).toBeInTheDocument();
    const save = within(card).getByRole("button", { name: "確認して保存" });
    expect(save).toBeDisabled();
    // A scope button only selects; the recommended one starts selected.
    await user.click(within(card).getByRole("button", { name: /すべてのProject/ }));
    expect(writes(source)).toEqual([]);
    expect(within(card).getByRole("button", { name: /すべてのProject/ })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await user.click(
      within(card).getByRole("checkbox", {
        name: "記録されるのは好みだけで、権限や Merge の承認は変わらないことを確認しました",
      }),
    );
    await user.click(save);
    expect(writes(source)).toEqual([
      {
        method: "confirm",
        args: [
          "e-merge.0",
          { scope: "user", project_id: null, repo_id: null, acknowledge_high_risk: true },
        ],
      },
    ]);
    expect(await screen.findByRole("status")).toHaveTextContent(/保存しました.*自分のメモリ/);
  });

  it("asks for the acknowledgement when the Backend judges the save high risk", async () => {
    const source = new FakePreferenceSource();
    source.failNext = new ApiError(409, "preference_high_risk_unacknowledged", "x");
    renderChat(source);
    const user = userEvent.setup();
    const card = await screen.findByRole("region", { name: "好みの確認" });
    await user.click(within(card).getByRole("button", { name: /このProject/ }));
    expect(within(card).getByRole("alert")).toHaveTextContent(/高リスクと判定されました/);
    const check = within(card).getByRole("checkbox");
    await user.click(check);
    await user.click(within(card).getByRole("button", { name: "確認して保存" }));
    expect(writes(source).at(-1)).toEqual({
      method: "confirm",
      args: [
        "m-pytest",
        { scope: "project", project_id: PROJECT_ID, repo_id: null, acknowledge_high_risk: true },
      ],
    });
  });

  it("offers to load the latest when the candidate changed elsewhere", async () => {
    const source = new FakePreferenceSource();
    source.failNext = new ApiError(409, "preference_candidate_changed", "x");
    renderChat(source);
    const user = userEvent.setup();
    const card = await screen.findByRole("region", { name: "好みの確認" });
    await user.click(within(card).getByRole("button", { name: /このRepoだけ/ }));
    expect(within(card).getByRole("alert")).toHaveTextContent(
      "この候補は別の画面で答えたか、新しい観測で内容が変わりました。",
    );
    const before = source.calls.filter((call) => call.method === "candidates").length;
    await user.click(within(card).getByRole("button", { name: "最新を読み込む" }));
    await waitFor(() =>
      expect(source.calls.filter((call) => call.method === "candidates").length).toBe(before + 1),
    );
  });

  it("その他: free text → the structured preview → saved as corrected", async () => {
    const source = new FakePreferenceSource();
    source.preview = {
      preference: {
        scope: "project_group",
        project_id: null,
        repo_id: null,
        apply_to: "開発系の Project",
        rule: "テストは pytest -x で回す。",
        exceptions: ["ドキュメントだけの Repo"],
        strength: "default",
        expires_at: null,
      },
      content: "x",
      risk_level: "low",
      requires_acknowledgement: false,
      interpreted_by: "rules",
    };
    renderChat(source);
    const user = userEvent.setup();
    const card = await screen.findByRole("region", { name: "好みの確認" });
    await user.click(within(card).getByRole("button", { name: "その他…" }));
    expect(within(card).getByText("その他 — 自分の言葉で決める")).toBeInTheDocument();
    await user.type(within(card).getByLabelText("どう覚えてほしいか"), "開発系だけ");
    await user.click(within(card).getByRole("button", { name: "構造にする" }));
    expect(writes(source)).toEqual([{ method: "interpret", args: ["m-pytest", "開発系だけ"] }]);
    expect(await within(card).findByText("プレビュー — まだ保存していません")).toBeVisible();
    expect(within(card).getByText("規則で解釈")).toBeInTheDocument();
    expect(within(card).getByText("リスク 低 · 確認不要")).toBeInTheDocument();
    expect(within(card).getByLabelText("範囲")).toHaveValue("project_group");
    expect(within(card).getByText(/自分のメモリに「適用対象: 開発系の Project」/)).toBeVisible();
    // Correct the preview: drop the exception, add another, change the rule.
    await user.click(
      within(card).getByRole("button", { name: "例外「ドキュメントだけの Repo」を外す" }),
    );
    await user.click(within(card).getByRole("button", { name: "+ 例外を追加" }));
    await user.type(within(card).getByLabelText("追加する例外"), "docs{Enter}");
    const rule = within(card).getByLabelText("内容");
    await user.clear(rule);
    await user.type(rule, "pytest は -x を付ける");
    await user.click(within(card).getByRole("button", { name: "この内容で保存" }));
    expect(writes(source).at(-1)).toEqual({
      method: "confirm",
      args: [
        "m-pytest",
        {
          preference: {
            scope: "project_group",
            project_id: null,
            repo_id: null,
            apply_to: "開発系の Project",
            rule: "pytest は -x を付ける",
            exceptions: ["docs"],
            strength: "default",
            expires_at: null,
          },
          acknowledge_high_risk: false,
        },
      ],
    });
    expect(await screen.findByRole("status")).toHaveTextContent(/保存しました/);
  });

  it("その他: the target belongs to a group of projects only", async () => {
    const source = new FakePreferenceSource();
    source.preview = {
      preference: {
        scope: "project_group",
        project_id: null,
        repo_id: null,
        apply_to: "開発系の Project",
        rule: "テストは pytest -x で回す。",
        exceptions: [],
        strength: "default",
        expires_at: null,
      },
      content: "x",
      risk_level: "low",
      requires_acknowledgement: false,
      interpreted_by: "rules",
    };
    renderChat(source);
    const user = userEvent.setup();
    const card = await screen.findByRole("region", { name: "好みの確認" });
    await user.click(within(card).getByRole("button", { name: "その他…" }));
    await user.type(within(card).getByLabelText("どう覚えてほしいか"), "開発系だけ");
    await user.click(within(card).getByRole("button", { name: "構造にする" }));
    await within(card).findByText("プレビュー — まだ保存していません");
    const save = within(card).getByRole("button", { name: "この内容で保存" });
    // A group needs its target.
    await user.clear(within(card).getByLabelText("適用対象"));
    expect(save).toBeDisabled();
    // Leaving the group drops the target (the Backend rejects it for any other scope).
    await user.type(within(card).getByLabelText("適用対象"), "開発系");
    await user.selectOptions(within(card).getByLabelText("範囲"), "user");
    expect(within(card).queryByLabelText("適用対象")).not.toBeInTheDocument();
    await user.click(save);
    expect(writes(source).at(-1)).toMatchObject({
      method: "confirm",
      args: ["m-pytest", { preference: { scope: "user", apply_to: null } }],
    });
  });

  it("その他: 必須 makes the preview high risk and needs the acknowledgement", async () => {
    const source = new FakePreferenceSource();
    renderChat(source);
    const user = userEvent.setup();
    const card = await screen.findByRole("region", { name: "好みの確認" });
    await user.click(within(card).getByRole("button", { name: "その他…" }));
    await user.type(within(card).getByLabelText("どう覚えてほしいか"), "必ず");
    await user.click(within(card).getByRole("button", { name: "構造にする" }));
    await within(card).findByText("プレビュー — まだ保存していません");
    await user.click(within(card).getByRole("button", { name: "必須" }));
    expect(within(card).getByText("リスク 高 · 確認が必要")).toBeInTheDocument();
    expect(within(card).getByText(/「必須」はメモリでは強制できない/)).toBeVisible();
    const save = within(card).getByRole("button", { name: "確認して保存" });
    expect(save).toBeDisabled();
    await user.click(within(card).getByRole("checkbox"));
    await user.click(save);
    expect(writes(source).at(-1)).toMatchObject({
      method: "confirm",
      args: ["m-pytest", { preference: { strength: "required" }, acknowledge_high_risk: true }],
    });
  });

  it("is a bottom sheet on a phone, and その他 a page of its own", async () => {
    phone();
    const source = new FakePreferenceSource();
    renderChat(source);
    const user = userEvent.setup();
    const sheet = await screen.findByRole("dialog", { name: "好みの確認" });
    expect(
      within(sheet).getByRole("heading", { name: "pytest は -x を付けて実行する" }),
    ).toBeVisible();
    await user.click(within(sheet).getByRole("button", { name: "その他…" }));
    const page = screen.getByRole("dialog", { name: "その他 — 自分の言葉で" });
    // The page edits the whole structure: the first exception and the expiry too.
    await user.type(within(page).getByLabelText("どう覚えてほしいか"), "今後も");
    await user.click(within(page).getByRole("button", { name: "構造にする" }));
    await within(page).findByText("プレビュー — 未保存");
    expect(within(page).getByRole("button", { name: "+ 例外を追加" })).toBeInTheDocument();
    expect(within(page).getByLabelText("期限")).toBeInTheDocument();
    await user.click(within(page).getByRole("button", { name: "候補のボタンに戻る" }));
    await user.click(
      within(screen.getByRole("dialog", { name: "好みの確認" })).getByRole("button", {
        name: "あとで聞く（候補はメモリに残ります）",
      }),
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(writes(source).filter((call) => call.method !== "interpret")).toEqual([]);
  });

  it("puts the phone's sheet off with Escape like ×", async () => {
    phone();
    const source = new FakePreferenceSource();
    renderChat(source);
    const user = userEvent.setup();
    await screen.findByRole("dialog", { name: "好みの確認" });
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(writes(source)).toEqual([]);
  });
});
