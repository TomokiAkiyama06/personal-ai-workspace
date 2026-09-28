import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { apiError, mockApi, renderApp, reply, session } from "../test/helpers";

afterEach(() => {
  vi.unstubAllGlobals();
});

const otherSession = {
  ...session().session,
  id: "s-2",
  device_name: "Phone",
  current: false,
};

const pairing = {
  pairing_id: "p-1",
  token: "tok_123",
  link_path: "/pair#tok_123",
  expires_at: "2026-09-28T02:10:00Z",
  approval_required: true,
};

const waiting = {
  pairing_id: "p-1",
  device_name: "New phone",
  claimed_at: "2026-09-28T02:01:00Z",
  expires_at: "2026-09-28T02:11:00Z",
};

describe("Devices", () => {
  it("lists the signed-in devices and signs one out", async () => {
    const { calls } = mockApi({
      "GET /auth/session": reply(200, session()),
      "GET /auth/sessions": [
        reply(200, { sessions: [session().session, otherSession] }),
        reply(200, { sessions: [session().session] }),
      ],
      "GET /auth/pairing/pending": reply(200, { pending: [] }),
      "DELETE /auth/sessions/s-2": reply(204),
    });
    renderApp("/settings/devices");
    const user = userEvent.setup();
    const list = await screen.findByRole("region", { name: "ログイン中の端末" });
    expect(await within(list).findByText("Phone")).toBeInTheDocument();
    expect(within(list).getByText("この端末")).toBeInTheDocument();
    await user.click(within(list).getByRole("button", { name: "ログアウトさせる" }));
    await waitFor(() => expect(within(list).queryByText("Phone")).not.toBeInTheDocument());
    expect(
      calls.some((call) => call.method === "DELETE" && call.path === "/auth/sessions/s-2"),
    ).toBe(true);
  });

  it("signs every other device out", async () => {
    mockApi({
      "GET /auth/session": reply(200, session()),
      "GET /auth/sessions": reply(200, { sessions: [session().session, otherSession] }),
      "GET /auth/pairing/pending": reply(200, { pending: [] }),
      "POST /auth/sessions/revoke-others": reply(200, { revoked: 1 }),
    });
    renderApp("/settings/devices");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "他のすべての端末からログアウト" }));
    expect(await screen.findByRole("status")).toHaveTextContent(
      "1 台の端末をログアウトさせました。",
    );
  });

  it("shows a one-time QR code and link for a new device, and can cancel it", async () => {
    const { calls } = mockApi({
      "GET /auth/session": reply(200, session()),
      "GET /auth/sessions": reply(200, { sessions: [session().session] }),
      "GET /auth/pairing/pending": reply(200, { pending: [] }),
      "POST /auth/pairing": reply(201, pairing),
      "DELETE /auth/pairing": reply(200, { revoked: 1 }),
    });
    renderApp("/settings/devices");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "新規端末を追加" }));
    expect(
      await screen.findByRole("img", { name: "新しい端末で読み取る QR コード" }),
    ).toBeVisible();
    expect(screen.getByLabelText("リンク")).toHaveValue(`${window.location.origin}/pair#tok_123`);
    expect(screen.getByText(/この端末での承認が必要です/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "QR コードを無効にする" }));
    expect(await screen.findByRole("button", { name: "新規端末を追加" })).toBeInTheDocument();
    expect(calls.some((call) => call.method === "DELETE" && call.path === "/auth/pairing")).toBe(
      true,
    );
  });

  it("approves a waiting device with its confirmation code after a step-up", async () => {
    const { calls } = mockApi({
      "GET /auth/session": reply(200, session({ enrolled: false })),
      "GET /auth/sessions": reply(200, { sessions: [session().session] }),
      "GET /auth/pairing/pending": [
        reply(200, { pending: [waiting] }),
        reply(200, { pending: [] }),
      ],
      "POST /auth/pairing/p-1/approve": [apiError(403, "step_up_required"), reply(204)],
      "POST /auth/step-up": reply(200, session()),
    });
    renderApp("/settings/devices");
    const user = userEvent.setup();
    const pending = await screen.findByRole("region", { name: "承認待ちの端末" });
    expect(await within(pending).findByText(/New phone/)).toBeInTheDocument();
    await user.type(
      within(pending).getByLabelText("新しい端末に表示されている確認コード"),
      "AB12-CD34",
    );
    await user.click(within(pending).getByRole("button", { name: "承認" }));

    // The Backend asked for a step-up: the prompt is inline, not a modal.
    const prompt = await screen.findByRole("region", { name: "本人確認" });
    await user.type(within(prompt).getByLabelText("パスワード"), "correct horse");
    await user.click(within(prompt).getByRole("button", { name: "パスワードで確認" }));

    expect(await screen.findByText("端末を承認しました。")).toBeInTheDocument();
    const approvals = calls.filter((call) => call.path === "/auth/pairing/p-1/approve");
    expect(approvals).toHaveLength(2);
    expect(approvals[1]?.body).toEqual({ confirmation_code: "AB12-CD34" });
    expect(calls.find((call) => call.path === "/auth/step-up")?.body).toEqual({
      method: "password",
      password: "correct horse",
    });
  });

  it("shows a wrong confirmation code as such", async () => {
    mockApi({
      "GET /auth/session": reply(200, session()),
      "GET /auth/sessions": reply(200, { sessions: [session().session] }),
      "GET /auth/pairing/pending": reply(200, { pending: [waiting] }),
      "POST /auth/pairing/p-1/approve": apiError(403, "confirmation_code_mismatch"),
    });
    renderApp("/settings/devices");
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("新しい端末に表示されている確認コード"), "WRONG");
    await user.click(screen.getByRole("button", { name: "承認" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("確認コードが一致しません");
  });

  it("rejects a waiting device", async () => {
    const { calls } = mockApi({
      "GET /auth/session": reply(200, session()),
      "GET /auth/sessions": reply(200, { sessions: [session().session] }),
      "GET /auth/pairing/pending": [
        reply(200, { pending: [waiting] }),
        reply(200, { pending: [] }),
      ],
      "POST /auth/pairing/p-1/reject": reply(204),
    });
    renderApp("/settings/devices");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "拒否" }));
    expect(await screen.findByText("端末を拒否しました。")).toBeInTheDocument();
    expect(calls.some((call) => call.path === "/auth/pairing/p-1/reject")).toBe(true);
  });
});
