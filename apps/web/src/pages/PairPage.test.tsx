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
  fireEvent.click(screen.getByRole("button", { name: "続ける" }));
}

const pending = {
  status: "pending_approval",
  session: null,
  claim: "claim_1",
  confirmation_code: "AB12-CD34",
  expires_at: "2026-09-28T02:10:00Z",
};

async function advancePoll() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(COMPLETE_POLL_MS);
  });
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

  it("suggests a device name from the browser", async () => {
    vi.spyOn(navigator, "userAgent", "get").mockReturnValue(
      "Mozilla/5.0 (iPhone; CPU iPhone OS 19_1 like Mac OS X) AppleWebKit/605.1.15 Version/19.1 Mobile Safari/604.1",
    );
    mockApi({ "GET /auth/session": apiError(401, "unauthorized") });
    renderApp("/pair#tok_123");
    expect(await screen.findByLabelText("この端末の名前")).toHaveValue("Safari · iPhone");
  });

  it("shows the confirmation code and waits for the approval", async () => {
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
    expect(await screen.findByText("信頼済み端末での承認を待っています。")).toBeInTheDocument();
    expect(screen.getByRole("status", { name: "確認コード" })).toHaveTextContent("AB12-CD34");

    await advancePoll();
    // Still waiting: the code stays on screen.
    expect(screen.getByText("AB12-CD34")).toBeInTheDocument();
    await advancePoll();
    expect(await screen.findByRole("link", { name: /管理/ })).toBeInTheDocument();
    const completes = calls.filter((call) => call.path === "/auth/pairing/complete");
    expect(completes[0]?.body).toEqual({ claim: "claim_1" });
  });

  // The Backend answers a refused, revoked, expired, used or locked pairing with
  // 400 `invalid_token` (auth/onboarding/pairing.py `_finish`), not 404.
  it("stops polling and says so when the pairing was refused or expired (invalid_token)", async () => {
    const { calls } = mockApi({
      "GET /auth/session": apiError(401, "unauthorized"),
      "POST /auth/pairing/claim": reply(202, pending),
      "POST /auth/pairing/complete": apiError(400, "invalid_token"),
    });
    renderApp("/pair#tok_123");
    await claim();
    await screen.findByText("AB12-CD34");
    await advancePoll();
    expect(
      await screen.findByText(/リンクの期限が切れたか、拒否または取り消されました/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    const completes = () => calls.filter((call) => call.path === "/auth/pairing/complete");
    expect(completes()).toHaveLength(1);
    await advancePoll();
    await advancePoll();
    // No more calls: a refused call does not give its rate-limit attempt back.
    expect(completes()).toHaveLength(1);
  });

  it("also ends on a 404 not_found", async () => {
    mockApi({
      "GET /auth/session": apiError(401, "unauthorized"),
      "POST /auth/pairing/claim": reply(202, pending),
      "POST /auth/pairing/complete": apiError(404, "not_found"),
    });
    renderApp("/pair#tok_123");
    await claim();
    await screen.findByText("AB12-CD34");
    await advancePoll();
    expect(await screen.findByText(/拒否または取り消されました/)).toBeInTheDocument();
  });

  it("keeps polling after a passing error, waiting as long as a rate limit says", async () => {
    const { calls } = mockApi({
      "GET /auth/session": apiError(401, "unauthorized"),
      "POST /auth/pairing/claim": reply(202, pending),
      "POST /auth/pairing/complete": [
        apiError(429, "rate_limited", { "Retry-After": "9" }),
        reply(202, { ...pending, claim: null, confirmation_code: null }),
      ],
    });
    renderApp("/pair#tok_123");
    await claim();
    await screen.findByText("AB12-CD34");
    await advancePoll();
    const completes = () => calls.filter((call) => call.path === "/auth/pairing/complete");
    expect(completes()).toHaveLength(1);
    await advancePoll();
    await advancePoll();
    expect(completes()).toHaveLength(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    expect(completes()).toHaveLength(2);
  });

  it("explains a link without a token", async () => {
    mockApi({ "GET /auth/session": apiError(401, "unauthorized") });
    renderApp("/pair");
    expect(await screen.findByText(/リンクが正しくありません/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "サインイン画面へ" })).toBeInTheDocument();
  });

  it("shows a used or expired link as such (invalid_token on the claim)", async () => {
    mockApi({
      "GET /auth/session": apiError(401, "unauthorized"),
      "POST /auth/pairing/claim": apiError(400, "invalid_token"),
    });
    renderApp("/pair#tok_used");
    await claim();
    expect(await screen.findByText(/リンクの期限が切れたか/)).toBeInTheDocument();
    expect(screen.queryByText(/invalid_token/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "続ける" })).not.toBeInTheDocument();
  });
});
