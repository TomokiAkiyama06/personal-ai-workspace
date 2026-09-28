import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { describeDevice } from "../auth/device";
import { apiError, mockApi, renderApp, reply, session } from "../test/helpers";

afterEach(() => {
  vi.unstubAllGlobals();
});

async function fillAndSubmit() {
  const user = userEvent.setup();
  await user.type(await screen.findByLabelText("ユーザー名"), "tomoki");
  await user.type(screen.getByLabelText("パスワード"), "correct horse");
  await user.click(screen.getByLabelText("この端末を信頼する"));
  await user.click(screen.getByRole("button", { name: "サインイン" }));
}

describe("LoginPage", () => {
  it("has the design's copy and no device-name field", async () => {
    mockApi({ "GET /auth/session": apiError(401, "unauthorized") });
    renderApp("/");
    expect(await screen.findByRole("heading", { name: "サインイン" })).toBeInTheDocument();
    expect(
      screen.getByText("この Workspace は招待された利用者のみが使用できます。"),
    ).toBeInTheDocument();
    expect(screen.getByText("開発エージェントを", { exact: false })).toBeInTheDocument();
    expect(screen.getByText("システム状態")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "この端末を追加する" })).toHaveAttribute(
      "href",
      "/pair",
    );
    expect(
      screen.getByText(/既存の信頼済み端末で発行した QR コードかリンクが必要です/),
    ).toBeVisible();
    expect(screen.queryByLabelText(/端末の名前/)).not.toBeInTheDocument();
    // The Backend has no passkey-first sign-in (Decision 0025): no such button.
    expect(screen.queryByRole("button", { name: /Passkey でサインイン/ })).not.toBeInTheDocument();
  });

  it("shows the Backend's readiness in the brand panel", async () => {
    mockApi({
      "GET /auth/session": apiError(401, "unauthorized"),
      "GET /health/ready": reply(200, { status: "ok", checks: { database: "ok" } }),
    });
    renderApp("/");
    expect(await screen.findByText("稼働中")).toBeInTheDocument();
  });

  it("switches the theme with 表示 ダーク / ライト", async () => {
    mockApi({ "GET /auth/session": apiError(401, "unauthorized") });
    renderApp("/");
    const user = userEvent.setup();
    const group = await screen.findByRole("group", { name: "テーマを選ぶ" });
    expect(within(group).getByRole("button", { name: "ダーク" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await user.click(within(group).getByRole("button", { name: "ライト" }));
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(within(group).getByRole("button", { name: "ライト" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("shows and hides the password", async () => {
    mockApi({ "GET /auth/session": apiError(401, "unauthorized") });
    renderApp("/");
    const user = userEvent.setup();
    const password = await screen.findByLabelText("パスワード");
    expect(password).toHaveAttribute("type", "password");
    await user.click(screen.getByRole("button", { name: "パスワードを表示" }));
    expect(password).toHaveAttribute("type", "text");
    await user.click(screen.getByRole("button", { name: "パスワードを隠す" }));
    expect(password).toHaveAttribute("type", "password");
  });

  it("explains the password reset", async () => {
    mockApi({ "GET /auth/session": apiError(401, "unauthorized") });
    renderApp("/");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "パスワードをお忘れですか" }));
    expect(screen.getByText(/Owner \/ Admin に依頼してください/)).toBeInTheDocument();
  });

  it("signs in (trusting the device) and opens the shell", async () => {
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
      device_name: describeDevice() || null,
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
      "ユーザー名またはパスワードが正しくありません。",
    );
    expect(screen.getByLabelText("パスワード")).toHaveValue("correct horse");
  });

  it("shows the wait of a rate-limited sign-in", async () => {
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
    expect(await screen.findByRole("heading", { name: "Passkey が必要です" })).toBeInTheDocument();
  });

  it("reads the full state when the sign-in answer had none", async () => {
    mockApi({
      "GET /auth/session": [apiError(401, "unauthorized"), reply(200, session({ role: "admin" }))],
      "POST /auth/login": reply(200, session({ auth: null })),
    });
    renderApp("/");
    await fillAndSubmit();
    expect(await screen.findByRole("link", { name: /管理/ })).toBeInTheDocument();
  });
});

describe("describeDevice", () => {
  it("names the browser and the system", () => {
    expect(
      describeDevice(
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36",
      ),
    ).toBe("Chrome · macOS");
    expect(
      describeDevice("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko Firefox/140.0"),
    ).toBe("Firefox · Windows");
    expect(describeDevice("curl/8")).toBe("");
  });
});
