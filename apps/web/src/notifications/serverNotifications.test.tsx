import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { StoredNotification } from "../api/notifications";
import { translate } from "../i18n";
import { mockApi, renderApp, reply, session } from "../test/helpers";
import { toIncoming } from "./serverNotifications";

// A minimal EventSource: the test opens it and dispatches the named events.
class FakeEventSource extends EventTarget {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;
  static instances: FakeEventSource[] = [];
  readyState = FakeEventSource.CONNECTING;
  closed = false;
  constructor(readonly url: string) {
    super();
    FakeEventSource.instances.push(this);
  }
  emit(type: string) {
    if (type === "open") this.readyState = FakeEventSource.OPEN;
    this.dispatchEvent(new Event(type));
  }
  close() {
    this.closed = true;
    this.readyState = FakeEventSource.CLOSED;
  }
}

beforeEach(() => {
  FakeEventSource.instances = [];
  vi.stubGlobal("EventSource", FakeEventSource);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function stored(overrides: Partial<StoredNotification> = {}): StoredNotification {
  return {
    id: "n-1",
    key: "system_health:recovery_backup",
    kind: "system_health.component_changed",
    severity: "error",
    category: "system",
    project_id: null,
    params: {
      component: "recovery_backup",
      status: "failing",
      previous_severity: "warning",
      reasons: ["consecutive_failures"],
    },
    created_at: "2026-10-01T02:00:00Z",
    read: false,
    ...overrides,
  };
}

const t = (key: Parameters<typeof translate>[1], params?: Record<string, string | number>) =>
  translate("ja", key, params);

describe("stored notifications: wording", () => {
  it("words a System Health change from its codes", () => {
    const item = toIncoming(stored(), t);
    expect(item).toMatchObject({
      key: "server:system_health:recovery_backup",
      id: "n-1",
      severity: "error",
      title: "Backup に異常があります",
      body: "状態 failing · 以前の Severity warning",
      detail: "consecutive_failures",
      source: "System Health",
      remote: true,
      read: false,
    });
    expect(item.actions).toEqual([{ label: "詳細を見る", to: "/admin", primary: true }]);
    expect(toIncoming(stored({ severity: "info" }), t).title).toBe("Backup は正常に戻りました");
  });

  it("lists a kind it does not know with its code", () => {
    const item = toIncoming(stored({ kind: "task.needs_human", params: {} }), t);
    expect(item.title).toBe("通知（task.needs_human）");
  });
});

describe("stored notifications in the Notification Center", () => {
  it("shows the account's notifications and marks them read on the Backend", async () => {
    const { calls } = mockApi({
      "GET /auth/session": reply(200, session()),
      "GET /notifications": reply(200, { notifications: [stored()], unread: 1 }),
      "POST /notifications/read": reply(200, { updated: 1, unread: 0 }),
    });
    renderApp("/");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "通知 1 件未読" }));
    const panel = screen.getByRole("complementary", { name: "通知" });
    expect(within(panel).getByText("Backup に異常があります")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "すべて既読" }));
    await waitFor(() =>
      expect(calls.filter((call) => call.method === "POST")).toEqual([
        expect.objectContaining({ path: "/notifications/read", body: { all: true } }),
      ]),
    );
  });

  it("closing the banner reads that notification on the Backend", async () => {
    const { calls } = mockApi({
      "GET /auth/session": reply(200, session()),
      "GET /notifications": reply(200, { notifications: [stored()], unread: 1 }),
      "POST /notifications/read": reply(200, { updated: 1, unread: 0 }),
    });
    renderApp("/");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "バナーを閉じる" }));
    await waitFor(() =>
      expect(calls.filter((call) => call.method === "POST")).toEqual([
        expect.objectContaining({ path: "/notifications/read", body: { ids: ["n-1"] } }),
      ]),
    );
  });

  it("a notification read on another device is read here, without a banner", async () => {
    mockApi({
      "GET /auth/session": reply(200, session()),
      "GET /notifications": reply(200, {
        notifications: [stored({ read: true })],
        unread: 0,
      }),
    });
    renderApp("/");
    expect(await screen.findByRole("button", { name: "通知" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "バナーを閉じる" })).not.toBeInTheDocument();
  });

  it("reads the list again when the stream says it changed, and drops what went", async () => {
    mockApi({
      "GET /auth/session": reply(200, session()),
      "GET /notifications": [
        reply(200, { notifications: [], unread: 0 }),
        reply(200, { notifications: [stored({ severity: "warning" })], unread: 1 }),
        reply(200, { notifications: [], unread: 0 }),
      ],
    });
    renderApp("/");
    expect(await screen.findByRole("button", { name: "通知" })).toBeInTheDocument();
    const [stream] = FakeEventSource.instances;
    expect(stream?.url).toBe("/api/v1/events/stream");
    await act(async () => stream?.emit("notification.changed"));
    expect(await screen.findByRole("button", { name: "通知 1 件未読" })).toBeInTheDocument();
    // Dismissed on another device (or resolved): gone from the next list.
    await act(async () => stream?.emit("notification.changed"));
    await waitFor(() => expect(screen.getByRole("button", { name: "通知" })).toBeInTheDocument());
  });

  it("keeps what is shown when a read fails, and closes the stream on sign-out", async () => {
    mockApi({
      "GET /auth/session": reply(200, session()),
      "GET /notifications": [
        reply(200, { notifications: [stored({ severity: "warning" })], unread: 1 }),
        reply(503, { error: { code: "service_unavailable", message: "x" } }),
      ],
    });
    const { unmount } = renderApp("/");
    expect(await screen.findByRole("button", { name: "通知 1 件未読" })).toBeInTheDocument();
    await act(async () => FakeEventSource.instances[0]?.emit("notification.changed"));
    expect(screen.getByRole("button", { name: "通知 1 件未読" })).toBeInTheDocument();
    unmount();
    expect(FakeEventSource.instances[0]?.closed).toBe(true);
  });

  it("counts the Backend's unread total, not only the entries loaded", async () => {
    mockApi({
      "GET /auth/session": reply(200, session()),
      "GET /notifications": reply(200, { notifications: [stored()], unread: 150 }),
    });
    renderApp("/");
    expect(await screen.findByRole("button", { name: "通知 150 件未読" })).toBeInTheDocument();
  });

  it("a read the Backend refused is unread again", async () => {
    const { calls } = mockApi({
      "GET /auth/session": reply(200, session()),
      "GET /notifications": reply(200, { notifications: [stored()], unread: 1 }),
      "POST /notifications/read": reply(503, {
        error: { code: "service_unavailable", message: "x" },
      }),
    });
    renderApp("/");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "バナーを閉じる" }));
    await waitFor(() => expect(calls.some((call) => call.method === "POST")).toBe(true));
    expect(await screen.findByRole("button", { name: "通知 1 件未読" })).toBeInTheDocument();
  });

  it("stays connected on the settings screens", async () => {
    mockApi({
      "GET /auth/session": reply(200, session()),
      "GET /notifications": [
        reply(200, { notifications: [], unread: 0 }),
        reply(200, { notifications: [stored()], unread: 1 }),
      ],
      "GET /auth/sessions": reply(200, { sessions: [session().session] }),
      "GET /auth/passkeys": reply(200, { passkeys: [] }),
    });
    renderApp("/settings/devices");
    expect(await screen.findByRole("navigation", { name: "設定メニュー" })).toBeInTheDocument();
    const [stream] = FakeEventSource.instances;
    await act(async () => stream?.emit("notification.changed"));
    expect(await screen.findByRole("button", { name: "バナーを閉じる" })).toBeInTheDocument();
  });
});
