import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { apiError, mockApi, renderApp, reply, session } from "../test/helpers";

afterEach(() => {
  vi.unstubAllGlobals();
});

async function fillAndSubmit() {
  const user = userEvent.setup();
  await user.type(await screen.findByLabelText("ログイン名"), "tomoki");
  await user.type(screen.getByLabelText("パスワード"), "correct horse");
  await user.type(screen.getByLabelText("この端末の名前（任意）"), "Laptop");
  await user.click(screen.getByLabelText("ログインしたままにする"));
  await user.click(screen.getByRole("button", { name: "ログイン" }));
}

describe("LoginPage", () => {
  it("signs in and opens the shell", async () => {
    const { calls } = mockApi({
      "GET /auth/session": apiError(401, "unauthorized"),
      "POST /auth/login": reply(200, session()),
    });
    renderApp("/");
    await fillAndSubmit();
    expect(await screen.findByRole("navigation", { name: "メインナビゲーション" })).toBeVisible();
    const login = calls.find((call) => call.path === "/auth/login");
    expect(login?.body).toEqual({
      login_name: "tomoki",
      password: "correct horse",
      remember_me: true,
      device_name: "Laptop",
    });
  });

  it("says the credentials are wrong without saying which", async () => {
    mockApi({
      "GET /auth/session": apiError(401, "unauthorized"),
      "POST /auth/login": apiError(401, "invalid_credentials"),
    });
    renderApp("/");
    await fillAndSubmit();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "ログイン名またはパスワードが正しくありません。",
    );
    expect(screen.getByLabelText("パスワード")).toHaveValue("correct horse");
  });

  it("shows the wait of a rate-limited login", async () => {
    mockApi({
      "GET /auth/session": apiError(401, "unauthorized"),
      "POST /auth/login": apiError(429, "rate_limited", { "Retry-After": "30" }),
    });
    renderApp("/");
    await fillAndSubmit();
    expect(await screen.findByRole("alert")).toHaveTextContent("30 秒");
  });

  it("sends a restricted sign-in to the passkey gate", async () => {
    mockApi({
      "GET /auth/session": apiError(401, "unauthorized"),
      "POST /auth/login": reply(200, session({ gate: "enrollment_required" })),
    });
    renderApp("/");
    await fillAndSubmit();
    expect(await screen.findByRole("heading", { name: "パスキーが必要です" })).toBeInTheDocument();
  });

  it("reads the full state when the login answer had none", async () => {
    mockApi({
      "GET /auth/session": [apiError(401, "unauthorized"), reply(200, session({ role: "admin" }))],
      "POST /auth/login": reply(200, session({ auth: null })),
    });
    renderApp("/");
    await fillAndSubmit();
    expect(await screen.findByRole("link", { name: "管理" })).toBeInTheDocument();
  });
});
