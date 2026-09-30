import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { I18nProvider } from "../i18n";
import { RouterProvider } from "../router";
import { NotificationBanners, NotificationBell } from "./NotificationCenter";
import {
  type IncomingNotification,
  mergeNotification,
  NotificationProvider,
  type NotificationSource,
} from "./store";

function manualSource() {
  let listener: ((notification: IncomingNotification) => void) | null = null;
  const source: NotificationSource = {
    subscribe(onNotification) {
      listener = onNotification;
      return () => {
        listener = null;
      };
    },
  };
  return {
    source,
    emit: (notification: IncomingNotification) => act(() => listener?.(notification)),
  };
}

function renderCenter(source?: NotificationSource) {
  return render(
    <I18nProvider>
      <RouterProvider>
        <NotificationProvider source={source}>
          <NotificationBell />
          <NotificationBanners />
        </NotificationProvider>
      </RouterProvider>
    </I18nProvider>,
  );
}

const at = "2026-09-28T02:00:00Z";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Notification Center", () => {
  it("is an empty bell without a source", async () => {
    renderCenter();
    const user = userEvent.setup();
    const bell = screen.getByRole("button", { name: "通知" });
    await user.click(bell);
    expect(bell).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("通知はありません。")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(bell).toHaveAttribute("aria-expanded", "false");
  });

  it("counts unread notifications, groups the same kind and marks all read", async () => {
    const { source, emit } = manualSource();
    renderCenter(source);
    emit({ key: "repo", severity: "info", title: "リポジトリの解析が完了しました", at });
    emit({ key: "backup", severity: "warning", title: "Memory Backup が一時的に失敗しました", at });
    emit({ key: "backup", severity: "warning", title: "Memory Backup が一時的に失敗しました", at });
    const user = userEvent.setup();
    const bell = screen.getByRole("button", { name: "通知 2 件未読" });
    await user.click(bell);
    const panel = screen.getByRole("complementary", { name: "通知" });
    expect(within(panel).getByText("未読 2")).toBeInTheDocument();
    expect(within(panel).getAllByRole("listitem")).toHaveLength(2);
    expect(within(panel).getByText("2 件をまとめて表示")).toBeInTheDocument();
    expect(within(panel).getByText("WARNING")).toBeInTheDocument();
    expect(within(panel).getByRole("link", { name: "すべての通知を見る" })).toHaveAttribute(
      "href",
      "/notifications",
    );
    expect(within(panel).getByRole("link", { name: "通知ルール" })).toBeInTheDocument();
    await user.click(within(panel).getByRole("button", { name: "すべて既読" }));
    expect(screen.getByRole("button", { name: "通知" })).toBeInTheDocument();
    expect(within(panel).getAllByText(/· 既読/)).toHaveLength(2);
  });

  it("filters by すべて / 未読 / 重要 / タスク", async () => {
    const { source, emit } = manualSource();
    renderCenter(source);
    emit({ key: "a", severity: "info", title: "メモリを 2 件整理しました", at });
    emit({
      key: "b",
      severity: "error",
      title: "エージェントのジョブが停止しました",
      source: "ExampleProject · Task #203",
      category: "task",
      at,
    });
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "通知 2 件未読" }));
    const panel = screen.getByRole("complementary", { name: "通知" });
    const filters = within(panel).getByRole("group", { name: "通知の絞り込み" });
    const titles = () =>
      within(panel)
        .queryAllByRole("listitem")
        .map((item) => item.querySelector(".notification-title")?.textContent);
    expect(within(filters).getByRole("button", { name: "すべて" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(titles()).toHaveLength(2);
    await user.click(within(filters).getByRole("button", { name: "重要" }));
    expect(titles()).toEqual(["エージェントのジョブが停止しました"]);
    await user.click(within(filters).getByRole("button", { name: "タスク" }));
    expect(titles()).toEqual(["エージェントのジョブが停止しました"]);
    expect(within(panel).getByText("ExampleProject · Task #203")).toBeInTheDocument();
    await user.click(within(panel).getByRole("button", { name: "すべて既読" }));
    await user.click(within(filters).getByRole("button", { name: "未読" }));
    expect(titles()).toEqual([]);
    expect(within(panel).getByText("該当する通知はありません。")).toBeInTheDocument();
  });

  it("opens the full-screen list on a phone instead of the dropdown", async () => {
    vi.stubGlobal(
      "matchMedia",
      vi.fn((query: string) => ({
        matches: query === "(max-width: 767px)",
        media: query,
        addEventListener: () => {},
        removeEventListener: () => {},
      })),
    );
    renderCenter();
    const user = userEvent.setup();
    const bell = screen.getByRole("button", { name: "通知" });
    expect(bell).not.toHaveAttribute("aria-expanded");
    await user.click(bell);
    expect(window.location.pathname).toBe("/notifications");
    expect(screen.queryByRole("complementary")).not.toBeInTheDocument();
  });

  it("shows ERROR and CRITICAL as non-modal banners that can be dismissed", async () => {
    const { source, emit } = manualSource();
    renderCenter(source);
    emit({ key: "info", severity: "info", title: "普通の通知", at });
    emit({ key: "gpu", severity: "critical", title: "GPUが応答しません", at });
    emit({ key: "db", severity: "error", title: "DB接続エラー", at });
    expect(screen.getByRole("alert")).toHaveTextContent("GPUが応答しません");
    expect(screen.getByRole("status")).toHaveTextContent("DB接続エラー");
    expect(screen.queryByText("普通の通知")).not.toBeInTheDocument();
    // Non-modal: the rest of the page stays usable.
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    const user = userEvent.setup();
    expect(within(screen.getByRole("alert")).getByText("CRITICAL")).toBeInTheDocument();
    await user.click(
      within(screen.getByRole("alert")).getByRole("button", { name: "バナーを閉じる" }),
    );
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("keeps at most 100 entries, newest first", () => {
    let items = [] as ReturnType<typeof mergeNotification>;
    for (let i = 0; i < 120; i++) {
      items = mergeNotification(items, { key: `k${i}`, severity: "info", title: `n${i}`, at });
    }
    expect(items).toHaveLength(100);
    expect(items[0]?.key).toBe("k119");
  });
});
