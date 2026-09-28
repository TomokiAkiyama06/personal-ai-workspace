import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { I18nProvider } from "../i18n";
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
      <NotificationProvider source={source}>
        <NotificationBell />
        <NotificationBanners />
      </NotificationProvider>
    </I18nProvider>,
  );
}

const at = "2026-09-28T02:00:00Z";

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
    emit({ key: "repo", severity: "info", title: "Repo解析が完了しました", at });
    emit({ key: "backup", severity: "warning", title: "Memory Backupが失敗しました", at });
    emit({ key: "backup", severity: "warning", title: "Memory Backupが失敗しました", at });
    const user = userEvent.setup();
    const bell = screen.getByRole("button", { name: "通知（未読 2 件）" });
    await user.click(bell);
    const panel = screen.getByRole("region", { name: "通知" });
    expect(within(panel).getAllByRole("listitem")).toHaveLength(2);
    expect(within(panel).getByText(/2 件/)).toBeInTheDocument();
    expect(within(panel).getByText("警告")).toBeInTheDocument();
    await user.click(within(panel).getByRole("button", { name: "すべて既読にする" }));
    expect(screen.getByRole("button", { name: "通知" })).toBeInTheDocument();
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
    await user.click(within(screen.getByRole("alert")).getByRole("button", { name: "閉じる" }));
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
