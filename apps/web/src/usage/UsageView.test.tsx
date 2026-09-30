import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { ApiError } from "../api/client";
import { mockApi, Providers, reply, session } from "../test/helpers";
import { usageReport } from "../test/usageFixture";
import { type UsageReport, type UsageSource, UsageSourceProvider } from "./model";

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderUsage(path: string, role: string, source: UsageSource | null) {
  mockApi({ "GET /auth/session": reply(200, session({ role })) });
  window.history.replaceState(null, "", path);
  return render(
    <Providers>
      <UsageSourceProvider source={source}>
        <App />
      </UsageSourceProvider>
    </Providers>,
  );
}

function fakeSource(report: UsageReport = usageReport()) {
  const load = vi.fn(async () => report);
  return { source: { load } satisfies UsageSource, load };
}

describe("使用状況", () => {
  it("says usage is not available while the Backend has no usage API", async () => {
    renderUsage("/admin/usage", "owner", null);
    expect(await screen.findByRole("heading", { name: "使用状況" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "使用状況はまだ表示できません" })).toBeVisible();
    // The toolbar is the design's: 自分 / ワークスペース, the period and the privacy note.
    expect(screen.getByRole("button", { name: "自分" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "ワークスペース" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "期間" })).toHaveValue("last14");
    expect(screen.getByText("会話の本文と Private Memory は集計に含めません")).toBeVisible();
  });

  it("sits under the admin tabs with 使用状況 current", async () => {
    renderUsage("/admin/usage", "admin", null);
    const tabs = await screen.findByRole("navigation", { name: "管理のタブ" });
    expect(within(tabs).getByRole("link", { name: "使用状況" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(
      within(tabs)
        .getAllByRole("link")
        .map((link) => link.textContent),
    ).toEqual([
      "概要",
      "ユーザー",
      "使用状況",
      "サーバー監視",
      "上限と課金",
      "モデルとルーター",
      "バックアップ",
      "監査ログ",
    ]);
  });

  it("shows the viewer's own usage, agents, purposes and quotas", async () => {
    const { source, load } = fakeSource();
    renderUsage("/admin/usage", "owner", source);
    expect(await screen.findByText("157")).toBeVisible();
    expect(load).toHaveBeenCalledWith("self", "last14");
    expect(screen.getByText("前の 14 日比 +18")).toBeVisible();
    expect(screen.getByText("9.7M")).toBeVisible();
    expect(screen.getByText("Local 4.1M · 外部 5.6M")).toBeVisible();
    expect(screen.getByText("11h 42m")).toBeVisible();
    expect(screen.getByText("平均 4m 28s / タスク")).toBeVisible();
    expect(screen.getByText("失敗 1 · ループ検知 2")).toBeVisible();
    expect(screen.getByRole("group", { name: /日別のタスク実行数/ })).toBeInTheDocument();
    expect(screen.getByText("113 タスク · 4.1M トークン")).toBeVisible();
    expect(screen.getByRole("rowheader", { name: "実装 / リファクタ" })).toBeVisible();
    expect(screen.getByRole("rowheader", { name: "調査 / 要約" })).toBeVisible();

    const quota = screen.getByRole("heading", { name: "上限（Quota）" }).closest("section");
    if (!quota) throw new Error("no quota card");
    const codex = within(quota).getByRole("meter", { name: "Codex（今月）" });
    expect(codex).toHaveAttribute("aria-valuetext", "3.8M / 10M");
    // A reached quota says so in words (not by color alone).
    expect(within(quota).getByRole("meter", { name: "Codex · タスク（今日）" })).toBeVisible();
    expect(within(quota).getByText(/上限に到達/)).toBeVisible();
    // Unlimited has no meter, only 「無制限」.
    expect(within(quota).getByText("Claude · 呼び出し（直近 5 時間）")).toBeVisible();
    expect(within(quota).getByText("無制限")).toBeVisible();
    expect(within(quota).getAllByRole("meter")).toHaveLength(3);
  });

  it("says a user without quotas is unlimited", async () => {
    const { source } = fakeSource(usageReport({ quotas: [] }));
    renderUsage("/settings/usage", "user", source);
    expect(
      await screen.findByText("上限は設定されていません。設定されていない項目は無制限です。"),
    ).toBeVisible();
  });

  it("loads the workspace scope and the period the viewer chooses", async () => {
    const { source, load } = fakeSource();
    renderUsage("/admin/usage", "admin", source);
    await screen.findByText("157");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "ワークスペース" }));
    await waitFor(() => expect(load).toHaveBeenLastCalledWith("workspace", "last14"));
    await user.selectOptions(screen.getByRole("combobox", { name: "期間" }), "month");
    await waitFor(() => expect(load).toHaveBeenLastCalledWith("workspace", "month"));

    const table = await screen.findByRole("table", { name: "ユーザーと上限" });
    const rows = within(table).getAllByRole("row").slice(1);
    const cells = rows.map((row) =>
      within(row)
        .getAllByRole("cell")
        .map((cell) => cell.textContent),
    );
    expect(cells).toEqual([
      ["Owner", "157", "9.7M", "無制限", "正常"],
      ["Admin", "64", "3.1M", "10M / 月", "正常"],
      ["Member", "38", "9.4M", "10M / 月", "上限に接近"],
      ["Member", "4", "200K", "5M / 月", "上限に到達"],
    ]);
    expect(screen.getByText("会話の本文・Private Memory は表示しません")).toBeVisible();
  });

  it("does not show the admin screen to a Member who opens it directly", async () => {
    const { source } = fakeSource();
    renderUsage("/admin/usage", "user", source);
    expect(await screen.findByRole("heading", { name: "自分の使用状況" })).toBeVisible();
    expect(window.location.pathname).toBe("/settings/usage");
    expect(screen.queryByRole("navigation", { name: "管理のタブ" })).not.toBeInTheDocument();
  });

  it("offers a Member only their own usage, in 設定 › 自分の使用状況", async () => {
    const { source, load } = fakeSource();
    renderUsage("/settings/usage", "user", source);
    expect(await screen.findByRole("heading", { name: "自分の使用状況" })).toBeVisible();
    await screen.findByText("157");
    expect(screen.queryByRole("button", { name: "ワークスペース" })).not.toBeInTheDocument();
    expect(load).toHaveBeenCalledWith("self", "last14");
  });

  it("shows the same numbers as a table (表で見る)", async () => {
    const { source } = fakeSource();
    renderUsage("/admin/usage", "owner", source);
    await screen.findByText("157");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "表で見る" }));
    const table = screen.getByRole("table", { name: "日別のタスク実行数" });
    const first = within(table).getAllByRole("row")[1];
    if (!first) throw new Error("no row");
    expect(within(first).getByRole("rowheader")).toHaveTextContent("2026/09/07");
    expect(
      within(first)
        .getAllByRole("cell")
        .map((cell) => cell.textContent),
    ).toEqual(["6", "3", "1", "10"]);
    expect(screen.queryByRole("group", { name: /日別のタスク実行数/ })).not.toBeInTheDocument();
  });

  it("shows a day's numbers when its bar gets keyboard focus", async () => {
    const { source } = fakeSource();
    renderUsage("/admin/usage", "owner", source);
    await screen.findByText("157");
    const chart0 = screen.getByRole("group", { name: /日別のタスク実行数/ });
    // The days are controls of the chart's group (not hidden inside an image).
    expect(within(chart0).getAllByRole("button")).toHaveLength(14);
    const day = within(chart0).getByRole("button", { name: /^2026\/09\/17 / });
    act(() => day.focus());
    const chart = screen.getByRole("group", { name: /日別のタスク実行数/ });
    expect(within(chart).getByText("2026/09/17")).toBeInTheDocument();
    expect(day).toHaveAccessibleName("2026/09/17 Local 14, Codex 8, Claude 3");
  });

  it("shows the Backend's refusal and retries", async () => {
    const load = vi
      .fn<UsageSource["load"]>()
      .mockRejectedValueOnce(new ApiError(403, "forbidden", "forbidden"))
      .mockResolvedValue(usageReport());
    renderUsage("/admin/usage", "admin", { load });
    expect(await screen.findByRole("alert")).toBeVisible();
    await userEvent.setup().click(screen.getByRole("button", { name: "再試行" }));
    expect(await screen.findByText("157")).toBeVisible();
    expect(load).toHaveBeenCalledTimes(2);
  });

  it("shows no free text a report might carry, only the closed categories", async () => {
    const { source } = fakeSource(
      usageReport({
        purposes: [{ purpose: "Summarize my private diary", tasks: 1, tokens: 900 }],
      }),
    );
    renderUsage("/admin/usage", "owner", source);
    expect(await screen.findByRole("rowheader", { name: "その他" })).toBeVisible();
    expect(screen.queryByText(/private diary/)).not.toBeInTheDocument();
  });

  it("shows 記録なし where the Backend records no GPU time or escalations", async () => {
    const { source } = fakeSource(
      usageReport({ gpu_seconds: null, escalations: null, tokens: { local: null, external: 10 } }),
    );
    renderUsage("/admin/usage", "owner", source);
    expect(await screen.findAllByText("記録なし")).toHaveLength(2);
    expect(screen.getByText("Local — · 外部 10")).toBeVisible();
  });
});
