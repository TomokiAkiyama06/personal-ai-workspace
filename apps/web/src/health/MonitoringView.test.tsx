import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { ApiError } from "../api/client";
import {
  abnormalReport,
  fakeHealthSource,
  healthEvents,
  normalReport,
} from "../test/healthFixture";
import { mockApi, Providers, reply, session } from "../test/helpers";
import { REFRESH_SECONDS } from "./MonitoringView";
import { type HealthSource, HealthSourceProvider } from "./model";

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function renderMonitoring(source: HealthSource | null, role = "owner") {
  mockApi({ "GET /auth/session": reply(200, session({ role })) });
  window.history.replaceState(null, "", "/admin/monitoring");
  return render(
    <Providers>
      <HealthSourceProvider source={source}>
        <App />
      </HealthSourceProvider>
    </Providers>,
  );
}

function panel() {
  return screen.getByRole("region", { name: /^(GPU \/ VRAM|Recovery Repository|PostgreSQL)$/ });
}

describe("サーバー監視", () => {
  it("says monitoring is not available without a source, under the admin tabs", async () => {
    renderMonitoring(null);
    expect(await screen.findByRole("heading", { name: "サーバー監視" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "サーバー監視はまだ表示できません" })).toBeVisible();
    const tabs = screen.getByRole("navigation", { name: "管理のタブ" });
    expect(within(tabs).getByRole("link", { name: "サーバー監視" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    // No header chip either: nothing is read.
    expect(screen.queryByRole("link", { name: /システムの状態/ })).not.toBeInTheDocument();
  });

  it("is compact when everything is normal", async () => {
    renderMonitoring(fakeHealthSource(normalReport()));
    expect(await screen.findByText("すべて正常です")).toBeVisible();
    expect(screen.getByText("9 項目を監視中 · 異常があると詳細を開きます")).toBeVisible();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    // The areas with their compact values.
    const areas = screen.getByRole("group", { name: "監視の対象" });
    expect(within(areas).getByRole("button", { name: /GPU \/ VRAM.*VRAM 38%/ })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(
      within(areas).getByRole("button", { name: /タスクキュー.*2 実行 \/ 1 待機/ }),
    ).toBeVisible();
    expect(within(areas).getByRole("button", { name: /PostgreSQL.*4 ms/ })).toBeVisible();
    expect(
      within(areas).getByRole("button", { name: /外部エージェント.*2 \/ 2 利用可/ }),
    ).toBeVisible();
    // The cards: VRAM, GPU, queue, reception.
    expect(screen.getByText("18.2 / 48 GB · 予約 21 GB")).toBeVisible();
    expect(screen.getByText("GPU 上のモデル 2")).toBeVisible();
    expect(screen.getByText("2 実行")).toBeVisible();
    expect(screen.getByText("1 待機 · Resource 待ち 1")).toBeVisible();
    expect(screen.getByText("15 秒間隔 · 失敗 0")).toBeVisible();
    // GPU / VRAM is open: meters, figures and the models; its row stays closed.
    const detail = panel();
    expect(within(detail).getByRole("meter", { name: "GPU 使用率" })).toHaveAttribute(
      "aria-valuenow",
      "38",
    );
    expect(within(detail).getByText("Memory Worker")).toBeVisible();
    expect(within(detail).getByText("qwen3.8-27b-fp8")).toBeVisible();
    expect(within(detail).getByRole("button", { name: "GPU / Model の詳細" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(screen.getByText("System Health は読み取りのみです。", { exact: false })).toBeVisible();
  });

  it("opens the details of what is abnormal", async () => {
    renderMonitoring(fakeHealthSource(abnormalReport()));
    const banner = await screen.findByRole("alert");
    expect(within(banner).getByText("ERROR")).toBeVisible();
    expect(within(banner).getByText("Backup: 続けて失敗しています")).toBeVisible();
    expect(within(banner).getByText("ほか 1 件")).toBeVisible();
    // The worst area is selected and the failing component's details are open.
    const areas = screen.getByRole("group", { name: "監視の対象" });
    expect(within(areas).getByRole("button", { name: /Recovery Repository/ })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    const detail = panel();
    expect(within(detail).getByText("異常", { selector: ".health-state" })).toBeVisible();
    expect(within(detail).getByRole("button", { name: "Backup の詳細" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    expect(within(detail).getByText("続けて失敗しています")).toBeVisible();
    expect(within(detail).getByText("最後の実行 5 分前 · 最後の成功 7 時間前")).toBeVisible();
    expect(within(detail).getByText("連続失敗 12 回")).toBeVisible();
    // Only the step of the failure is shown, not the error's type.
    expect(within(detail).getByText("失敗した段階 push")).toBeVisible();
    expect(within(detail).queryByText(/GitCommandError/)).not.toBeInTheDocument();
    // The normal components of the area stay compact.
    expect(
      within(detail).getByRole("button", { name: "Memory Projection の詳細" }),
    ).toHaveAttribute("aria-expanded", "false");
    expect(within(detail).getByText("未実行")).toBeVisible();

    // GPU / VRAM shows its warning when chosen.
    const user = userEvent.setup();
    await user.click(within(areas).getByRole("button", { name: /GPU \/ VRAM/ }));
    expect(within(panel()).getByText("VRAM が逼迫しています")).toBeVisible();
  });

  it("lists the severity changes as alerts", async () => {
    renderMonitoring(fakeHealthSource(normalReport()));
    const alerts = await screen.findByRole("region", { name: "直近のアラート" });
    expect(await within(alerts).findByText(/Backup が ERROR になりました/)).toBeVisible();
    expect(within(alerts).getByText("08:00")).toBeVisible();
    expect(within(alerts).getByText(/Codex \/ Claude の接続 は正常に戻りました/)).toBeVisible();
    expect(within(alerts).getByText("9/18")).toBeVisible();
  });

  it("says when there are no alerts or no history", async () => {
    renderMonitoring(
      fakeHealthSource(normalReport(), { events: async () => [], series: async () => [] }),
    );
    expect(await screen.findByText("この期間のアラートはありません。")).toBeVisible();
    expect(screen.getByText("この期間の記録はありません。")).toBeVisible();
  });

  it("draws GPU / VRAM and shows the same values as a table", async () => {
    renderMonitoring(fakeHealthSource(normalReport()));
    expect(
      await screen.findByRole("img", { name: /GPU \/ VRAM の推移（直近 24 時間/ }),
    ).toBeInTheDocument();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "表で見る" }));
    const table = screen.getByRole("table", { name: "GPU / VRAM の推移" });
    const rows = within(table).getAllByRole("row");
    // The header and two buckets: GPU, VRAM used and reserved of 48 GB.
    expect(rows).toHaveLength(3);
    expect(
      within(rows[1] as HTMLElement)
        .getAllByRole("cell")
        .map((cell) => cell.textContent),
    ).toEqual(["40%", "25%", "38%"]);
  });

  it("reads the history of the chosen period", async () => {
    const source = fakeHealthSource(normalReport());
    const series = vi.spyOn(source, "series");
    const events = vi.spyOn(source, "events");
    renderMonitoring(source);
    await screen.findByText("すべて正常です");
    await waitFor(() => expect(series).toHaveBeenCalledTimes(4));
    expect(series.mock.calls.map((call) => call[0])).toEqual([
      "compute.utilization_percent",
      "compute.vram_used_bytes",
      "compute.vram_reserved_bytes",
      "compute.vram_total_bytes",
    ]);
    const user = userEvent.setup();
    await user.selectOptions(screen.getByRole("combobox", { name: "期間" }), "last7d");
    await waitFor(() => expect(series).toHaveBeenCalledTimes(8));
    const [, since, until, step] = series.mock.calls[4] ?? [];
    expect((until as Date).getTime() - (since as Date).getTime()).toBe(7 * 86_400_000);
    expect(step).toBe(5040);
    expect(events).toHaveBeenCalledTimes(2);
  });

  it("shows the Backend's refusal and tries again", async () => {
    let fail = true;
    const source = fakeHealthSource(normalReport(), {
      report: async () => {
        if (fail) throw new ApiError(503, "service_unavailable", "x");
        return normalReport();
      },
    });
    renderMonitoring(source);
    expect(await screen.findByRole("alert")).toBeVisible();
    fail = false;
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "再試行" }));
    expect(await screen.findByText("すべて正常です")).toBeVisible();
  });

  it("keeps the last report when a refresh fails and says reception stopped", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let fail = false;
    const source = fakeHealthSource(normalReport(), {
      report: async () => {
        if (fail) throw new ApiError(503, "service_unavailable", "x");
        return normalReport();
      },
    });
    renderMonitoring(source);
    expect(await screen.findByText("すべて正常です")).toBeVisible();
    fail = true;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(REFRESH_SECONDS * 1000);
    });
    expect(await screen.findByText("途切れています")).toBeVisible();
    expect(screen.getByText("15 秒間隔 · 失敗 1")).toBeVisible();
    expect(screen.getByText("すべて正常です")).toBeVisible();
  });

  it("shows a reason code this version does not know as it is", async () => {
    const report = abnormalReport();
    const backup = report.components.find((entry) => entry.component === "recovery_backup");
    if (backup) backup.reasons = ["failing", "a_code_from_later"];
    renderMonitoring(fakeHealthSource(report, { events: async () => healthEvents().slice(0, 1) }));
    expect(await screen.findByText("a_code_from_later")).toBeVisible();
  });

  it("is not shown to a Member", async () => {
    renderMonitoring(fakeHealthSource(normalReport()), "user");
    await waitFor(() => expect(window.location.pathname).toBe("/"));
    expect(screen.queryByRole("heading", { name: "サーバー監視" })).not.toBeInTheDocument();
  });
});
