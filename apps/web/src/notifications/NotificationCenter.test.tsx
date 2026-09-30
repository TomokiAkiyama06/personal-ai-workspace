import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { I18nProvider } from "../i18n";
import { RouterProvider } from "../router";
import { NotificationBanners, NotificationBell, NotificationsPage } from "./NotificationCenter";
import {
  type IncomingNotification,
  mergeNotification,
  NotificationProvider,
  type NotificationSource,
} from "./store";

function manualSource() {
  let listener: ((notification: IncomingNotification) => void) | null = null;
  let resolver: ((key: string) => void) | null = null;
  const source: NotificationSource = {
    subscribe(onNotification, onResolve) {
      listener = onNotification;
      resolver = onResolve ?? null;
      return () => {
        listener = null;
        resolver = null;
      };
    },
  };
  return {
    source,
    emit: (notification: IncomingNotification) => act(() => listener?.(notification)),
    resolve: (key: string) => act(() => resolver?.(key)),
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

  it("counts the same event only once (dedup) and aggregates different ones", async () => {
    const { source, emit } = manualSource();
    renderCenter(source);
    const backup = (id: string, time: string) =>
      emit({
        key: "backup",
        id,
        severity: "warning",
        title: "Memory Backup が一時的に失敗しました",
        at: time,
      });
    backup("e1", "2026-09-28T01:30:00Z");
    backup("e1", "2026-09-28T01:30:00Z");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "通知 1 件未読" }));
    const panel = screen.getByRole("complementary", { name: "通知" });
    expect(within(panel).queryByText(/件をまとめて表示/)).not.toBeInTheDocument();
    backup("e2", "2026-09-28T03:00:00Z");
    backup("e3", "2026-09-28T02:00:00Z");
    backup("e2", "2026-09-28T03:00:00Z");
    expect(within(panel).getAllByRole("listitem")).toHaveLength(1);
    expect(within(panel).getByText("3 件をまとめて表示")).toBeInTheDocument();
    // The first and the latest event of the entry (NOTIFICATION_POLICY §4).
    const first = new Intl.DateTimeFormat("ja-JP", { hour: "2-digit", minute: "2-digit" });
    expect(
      within(panel).getByText(
        `最初 ${first.format(new Date("2026-09-28T01:30:00Z"))} · 最新 ${first.format(new Date("2026-09-28T03:00:00Z"))}`,
      ),
    ).toBeInTheDocument();
    // A duplicate does not make a read entry unread again.
    await user.click(within(panel).getByRole("button", { name: "すべて既読" }));
    backup("e3", "2026-09-28T02:00:00Z");
    expect(screen.getByRole("button", { name: "通知" })).toBeInTheDocument();
    // A new event does.
    backup("e4", "2026-09-28T04:00:00Z");
    expect(screen.getByRole("button", { name: "通知 1 件未読" })).toBeInTheDocument();
    expect(within(panel).getByText("4 件をまとめて表示")).toBeInTheDocument();
  });

  it("escalates an entry to the newest severity and shows the banner", () => {
    const { source, emit } = manualSource();
    renderCenter(source);
    emit({ key: "backup", id: "1", severity: "warning", title: "Backup が失敗しました", at });
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    emit({
      key: "backup",
      id: "2",
      severity: "critical",
      title: "Memory Backup が 6 時間成功していません",
      detail: "最終成功 07:30 · 連続失敗 12 回",
      at,
    });
    const banner = screen.getByRole("alert");
    expect(banner).toHaveTextContent("Memory Backup が 6 時間成功していません");
    expect(banner).toHaveTextContent("最終成功 07:30 · 連続失敗 12 回");
  });

  it("removes an entry its source resolved", async () => {
    const { source, emit, resolve } = manualSource();
    renderCenter(source);
    emit({ key: "a", severity: "warning", title: "承認待ち", at });
    expect(screen.getByRole("button", { name: "通知 1 件未読" })).toBeInTheDocument();
    resolve("a");
    expect(screen.getByRole("button", { name: "通知" })).toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: "通知" }));
    expect(screen.getByText("通知はありません。")).toBeInTheDocument();
  });

  it("offers the entry's actions as links that mark it read and close the panel", async () => {
    const { source, emit } = manualSource();
    renderCenter(source);
    emit({
      key: "task",
      severity: "error",
      title: "エージェントのジョブが停止しました",
      body: "同じ修正を 3 回繰り返したためエスカレーションしました。",
      category: "task",
      at,
      actions: [{ label: "タスクを開く", to: "/agents", primary: true }],
    });
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "通知 1 件未読" }));
    const panel = screen.getByRole("complementary", { name: "通知" });
    expect(within(panel).getByRole("link", { name: "通知設定を開く" })).toHaveAttribute(
      "href",
      "/settings/notifications",
    );
    const open = within(panel).getByRole("link", { name: "タスクを開く" });
    expect(open).toHaveAttribute("href", "/agents");
    await user.click(open);
    expect(window.location.pathname).toBe("/agents");
    expect(screen.queryByRole("complementary")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "通知" })).toBeInTheDocument();
    // The banner offers the same action.
    expect(
      within(screen.getByRole("status")).getByRole("link", { name: "タスクを開く" }),
    ).toBeInTheDocument();
  });

  it("shows the phone layout: the unread count on the 未読 chip and short group counts", () => {
    vi.stubGlobal(
      "matchMedia",
      vi.fn((query: string) => ({
        matches: query === "(max-width: 767px)",
        media: query,
        addEventListener: () => {},
        removeEventListener: () => {},
      })),
    );
    const { source, emit } = manualSource();
    render(
      <I18nProvider>
        <RouterProvider>
          <NotificationProvider source={source}>
            <NotificationsPage />
          </NotificationProvider>
        </RouterProvider>
      </I18nProvider>,
    );
    emit({ key: "w", id: "1", severity: "warning", title: "一時的に失敗しました", at });
    emit({ key: "w", id: "2", severity: "warning", title: "一時的に失敗しました", at });
    expect(screen.getByRole("heading", { level: 1, name: "通知" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "未読 1" })).toBeInTheDocument();
    expect(screen.getByText("2 件")).toBeInTheDocument();
  });

  it("does not repeat the banners on the notification list itself", () => {
    window.history.replaceState(null, "", "/notifications");
    const { source, emit } = manualSource();
    renderCenter(source);
    emit({ key: "gpu", severity: "critical", title: "GPUが応答しません", at });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    window.history.replaceState(null, "", "/");
  });

  it("keeps the newest event's content when an older one arrives late", () => {
    const { source, emit } = manualSource();
    renderCenter(source);
    emit({ key: "backup", id: "2", severity: "critical", title: "6 時間成功していません", at });
    emit({
      key: "backup",
      id: "1",
      severity: "warning",
      title: "一時的に失敗しました",
      at: "2026-09-28T01:00:00Z",
    });
    expect(screen.getByRole("alert")).toHaveTextContent("6 時間成功していません");
  });

  it("remembers every id of an entry for dedup", () => {
    let items = [] as ReturnType<typeof mergeNotification>;
    for (let i = 0; i < 80; i++) {
      items = mergeNotification(items, { key: "k", id: `e${i}`, severity: "info", title: "n", at });
    }
    expect(mergeNotification(items, { key: "k", id: "e0", severity: "info", title: "n", at })).toBe(
      items,
    );
  });

  it("keeps the read / dismissed state and the order when an older event arrives late", async () => {
    const { source, emit } = manualSource();
    renderCenter(source);
    emit({ key: "backup", id: "2", severity: "critical", title: "6 時間成功していません", at });
    emit({
      key: "repo",
      severity: "info",
      title: "解析が完了しました",
      at: "2026-09-28T03:00:00Z",
    });
    const user = userEvent.setup();
    await user.click(
      within(screen.getByRole("alert")).getByRole("button", { name: "バナーを閉じる" }),
    );
    await user.click(screen.getByRole("button", { name: "通知 1 件未読" }));
    const panel = screen.getByRole("complementary", { name: "通知" });
    await user.click(within(panel).getByRole("button", { name: "すべて既読" }));
    emit({
      key: "backup",
      id: "1",
      severity: "critical",
      title: "古い失敗",
      at: "2026-09-28T01:00:00Z",
    });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "通知" })).toBeInTheDocument();
    const titles = within(panel)
      .getAllByRole("listitem")
      .map((item) => item.querySelector(".notification-title")?.textContent);
    expect(titles).toEqual(["解析が完了しました", "6 時間成功していません"]);
    expect(within(panel).getByText("2 件をまとめて表示")).toBeInTheDocument();
  });
});
