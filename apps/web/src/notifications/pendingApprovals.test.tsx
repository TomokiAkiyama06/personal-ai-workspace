import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { apiError, mockApi, renderApp, reply, session } from "../test/helpers";

afterEach(() => {
  vi.unstubAllGlobals();
});

const waiting = {
  pairing_id: "p-1",
  device_name: "New phone",
  claimed_at: "2026-09-28T02:01:00Z",
  expires_at: "2026-09-28T02:11:00Z",
};

function becomeVisible() {
  return act(async () => {
    document.dispatchEvent(new Event("visibilitychange"));
  });
}

describe("pending device approvals as notifications", () => {
  it("shows a device waiting for approval and links to the step-up screen", async () => {
    const { calls } = mockApi({
      "GET /auth/session": reply(200, session()),
      "GET /auth/pairing/pending": reply(200, { pending: [waiting] }),
      "GET /auth/sessions": reply(200, { sessions: [session().session] }),
      "GET /auth/passkeys": reply(200, { passkeys: [] }),
    });
    renderApp("/");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "通知 1 件未読" }));
    const panel = screen.getByRole("complementary", { name: "通知" });
    expect(within(panel).getByText("新しい端末が承認を待っています")).toBeInTheDocument();
    expect(
      within(panel).getByText(/New phone がこのアカウントへの追加を求めています/),
    ).toBeVisible();
    expect(within(panel).getByText("WARNING")).toBeInTheDocument();
    expect(within(panel).getByText("端末とセッション")).toBeInTheDocument();
    // The notification only opens 端末とセッション: approving there needs the
    // confirmation code and a Passkey Step-up. It never approves by itself.
    await user.click(within(panel).getByRole("link", { name: "確認する" }));
    expect(window.location.pathname).toBe("/settings/devices");
    expect(await screen.findByRole("navigation", { name: "設定メニュー" })).toBeInTheDocument();
    expect(calls.filter((call) => call.method !== "GET")).toEqual([]);
  });

  it("does not count the same pending device again on the next read", async () => {
    mockApi({
      "GET /auth/session": reply(200, session()),
      "GET /auth/pairing/pending": reply(200, { pending: [waiting] }),
    });
    renderApp("/");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "通知 1 件未読" }));
    await user.click(screen.getByRole("button", { name: "すべて既読" }));
    await becomeVisible();
    await becomeVisible();
    expect(screen.getByRole("button", { name: "通知" })).toBeInTheDocument();
    const panel = screen.getByRole("complementary", { name: "通知" });
    expect(within(panel).getAllByRole("listitem")).toHaveLength(1);
    expect(within(panel).queryByText(/件をまとめて表示/)).not.toBeInTheDocument();
  });

  it("removes the notification once the device is no longer pending", async () => {
    mockApi({
      "GET /auth/session": reply(200, session()),
      "GET /auth/pairing/pending": [
        reply(200, { pending: [waiting] }),
        reply(200, { pending: [] }),
      ],
    });
    renderApp("/");
    expect(await screen.findByRole("button", { name: "通知 1 件未読" })).toBeInTheDocument();
    await becomeVisible();
    await waitFor(() => expect(screen.getByRole("button", { name: "通知" })).toBeInTheDocument());
  });

  it("keeps what is shown when a read fails", async () => {
    mockApi({
      "GET /auth/session": reply(200, session()),
      "GET /auth/pairing/pending": [
        reply(200, { pending: [waiting] }),
        apiError(503, "service_unavailable"),
      ],
    });
    renderApp("/");
    expect(await screen.findByRole("button", { name: "通知 1 件未読" })).toBeInTheDocument();
    await becomeVisible();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.getByRole("button", { name: "通知 1 件未読" })).toBeInTheDocument();
  });

  it("forgets the notifications when the account signs out", async () => {
    mockApi({
      "GET /auth/session": reply(200, session()),
      "GET /auth/pairing/pending": [
        reply(200, { pending: [waiting] }),
        reply(200, { pending: [] }),
      ],
      "POST /auth/logout": reply(204),
      "POST /auth/login": reply(200, session()),
    });
    renderApp("/");
    const user = userEvent.setup();
    expect(await screen.findByRole("button", { name: "通知 1 件未読" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "アカウントメニュー" }));
    await user.click(screen.getByRole("button", { name: "サインアウト" }));
    await user.type(await screen.findByLabelText("ユーザー名"), "other");
    await user.type(screen.getByLabelText("パスワード"), "correct horse");
    await user.click(screen.getByRole("button", { name: "サインイン" }));
    expect(await screen.findByRole("navigation", { name: "メインナビゲーション" })).toBeVisible();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.getByRole("button", { name: "通知" })).toBeInTheDocument();
  });
});
