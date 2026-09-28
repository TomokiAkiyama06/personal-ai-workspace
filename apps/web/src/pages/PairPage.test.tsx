import { act, fireEvent, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { apiError, mockApi, renderApp, reply, session } from "../test/helpers";
import { COMPLETE_POLL_MS } from "./PairPage";

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

async function claim(deviceName = "New phone") {
  fireEvent.change(await screen.findByLabelText("この端末の名前"), {
    target: { value: deviceName },
  });
  fireEvent.click(screen.getByRole("button", { name: "この端末を追加" }));
}

describe("PairPage (the new device)", () => {
  it("takes the token from the link and removes it from the address bar", async () => {
    const { calls } = mockApi({
      "GET /auth/session": apiError(401, "unauthorized"),
      "POST /auth/pairing/claim": reply(200, {
        status: "completed",
        session: session(),
        claim: null,
        confirmation_code: null,
        expires_at: null,
      }),
    });
    renderApp("/pair#tok_123");
    expect(await screen.findByRole("heading", { name: "この端末を追加" })).toBeInTheDocument();
    expect(window.location.hash).toBe("");
    await claim();
    expect(await screen.findByRole("navigation", { name: "メインナビゲーション" })).toBeVisible();
    expect(window.location.pathname).toBe("/");
    expect(calls.find((call) => call.path === "/auth/pairing/claim")?.body).toEqual({
      token: "tok_123",
      device_name: "New phone",
      remember_me: false,
    });
  });

  it("shows the confirmation code and waits for the approval", async () => {
    const pending = {
      status: "pending_approval",
      session: null,
      claim: "claim_1",
      confirmation_code: "AB12-CD34",
      expires_at: "2026-09-28T02:10:00Z",
    };
    const { calls } = mockApi({
      "GET /auth/session": apiError(401, "unauthorized"),
      "POST /auth/pairing/claim": reply(202, pending),
      "POST /auth/pairing/complete": [
        reply(202, { ...pending, claim: null, confirmation_code: null }),
        reply(200, { ...pending, status: "completed", session: session({ role: "admin" }) }),
      ],
    });
    renderApp("/pair#tok_123");
    await claim();
    expect(
      await screen.findByText("ログイン済みの端末での承認を待っています。"),
    ).toBeInTheDocument();
    expect(screen.getByRole("status", { name: "確認コード" })).toHaveTextContent("AB12-CD34");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(COMPLETE_POLL_MS);
    });
    // Still waiting: the code stays on screen.
    expect(screen.getByText("AB12-CD34")).toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(COMPLETE_POLL_MS);
    });
    expect(await screen.findByRole("link", { name: "管理" })).toBeInTheDocument();
    const completes = calls.filter((call) => call.path === "/auth/pairing/complete");
    expect(completes[0]?.body).toEqual({ claim: "claim_1" });
  });

  it("says so when the approval expired or was refused", async () => {
    mockApi({
      "GET /auth/session": apiError(401, "unauthorized"),
      "POST /auth/pairing/claim": reply(202, {
        status: "pending_approval",
        session: null,
        claim: "claim_1",
        confirmation_code: "AB12-CD34",
        expires_at: "2026-09-28T02:10:00Z",
      }),
      "POST /auth/pairing/complete": apiError(404, "not_found"),
    });
    renderApp("/pair#tok_123");
    await claim();
    await screen.findByText("AB12-CD34");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(COMPLETE_POLL_MS);
    });
    expect(await screen.findByText(/承認の期限が切れたか、拒否されました/)).toBeInTheDocument();
  });

  it("explains a link without a token", async () => {
    mockApi({ "GET /auth/session": apiError(401, "unauthorized") });
    renderApp("/pair");
    expect(await screen.findByText(/リンクが正しくありません/)).toBeInTheDocument();
  });

  it("shows a used or expired token as such", async () => {
    mockApi({
      "GET /auth/session": apiError(401, "unauthorized"),
      "POST /auth/pairing/claim": apiError(404, "not_found"),
    });
    renderApp("/pair#tok_used");
    await claim();
    expect(await screen.findByRole("alert")).toHaveTextContent("期限切れか、すでに使われた");
  });
});
