import { act, fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import { apiError, mockApi, renderApp, reply, session } from "../test/helpers";
import { passwordChecks, passwordProblem } from "./TokenRedeemPage";

const TOKEN = `pawst1.${"a".repeat(32)}.${"S".repeat(43)}`;
const GOOD = "tsuki-no-hikari-42";

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function policy(problem: string) {
  return reply(422, {
    error: {
      code: "password_policy",
      message: `The password does not meet the policy (${problem})`,
      request_id: "req-1",
    },
  });
}

async function fillPasswords(password = GOOD, confirm = password) {
  fireEvent.change(await screen.findByLabelText("新しいパスワード"), {
    target: { value: password },
  });
  fireEvent.change(screen.getByLabelText("新しいパスワード（確認）"), {
    target: { value: confirm },
  });
}

function submitButton() {
  return screen.getByRole("button", { name: "パスワードを設定する" });
}

describe("TokenRedeemPage (/setup)", () => {
  it("takes the token from the link, removes it from the address bar and never shows it", async () => {
    const { calls } = mockApi({
      "GET /auth/session": apiError(401, "unauthorized"),
      "POST /auth/token/redeem": reply(200, {
        purpose: "setup",
        passkey_required: false,
        next: "login",
      }),
    });
    renderApp(`/setup#${TOKEN}`);
    expect(
      await screen.findByRole("heading", { name: "Owner の初期設定 / 復旧" }),
    ).toBeInTheDocument();
    expect(window.location.hash).toBe("");
    expect(window.location.pathname).toBe("/setup");
    expect(screen.getByText("リンクから入力済み")).toBeInTheDocument();
    expect(screen.getByLabelText("トークン")).toHaveValue(`pawst1.aaaaaaaa…••••••••`);
    expect(document.body.textContent).not.toContain("S".repeat(43));

    await fillPasswords();
    fireEvent.click(submitButton());
    expect(
      await screen.findByRole("heading", { name: "Owner のパスワードを設定しました" }),
    ).toBeInTheDocument();
    expect(calls.find((call) => call.path === "/auth/token/redeem")?.body).toEqual({
      token: TOKEN,
      new_password: GOOD,
    });
    expect(screen.queryByText(/Passkey が必須です/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "サインインへ進む" }));
    expect(await screen.findByRole("heading", { name: "サインイン" })).toBeInTheDocument();
    expect(window.location.pathname).toBe("/");
  });

  it("shows the rules up front and enables the button only when they are met", async () => {
    mockApi({ "GET /auth/session": apiError(401, "unauthorized") });
    renderApp(`/setup#${TOKEN}`);
    expect(await screen.findByText("10〜256 文字（現在 0 文字）")).toBeInTheDocument();
    expect(screen.getByText(/4 種類以上の文字/)).toBeInTheDocument();
    expect(screen.getByText(/よく使われるパスワードやユーザー名/)).toBeInTheDocument();
    expect(submitButton()).toBeDisabled();

    await fillPasswords("short");
    expect(screen.getByText("10〜256 文字（現在 5 文字）").closest("li")).toHaveClass("ng");
    expect(submitButton()).toBeDisabled();

    // Advice only: the Backend's casefold decides (see the test below).
    await fillPasswords("aaaaaaaaaaaa");
    expect(screen.getByText(/4 種類以上の文字/).closest("li")).toHaveClass("ng");
    expect(submitButton()).toBeEnabled();

    await fillPasswords(GOOD, `${GOOD}x`);
    expect(screen.getByText("確認用と一致している").closest("li")).toHaveClass("ng");
    expect(submitButton()).toBeDisabled();

    await fillPasswords(GOOD);
    expect(screen.getByText("確認用と一致している").closest("li")).toHaveClass("ok");
    expect(submitButton()).toBeEnabled();
  });

  it("accepts a pasted token when there is no link, and trims it", async () => {
    const { calls } = mockApi({
      "GET /auth/session": apiError(401, "unauthorized"),
      "POST /auth/token/redeem": reply(200, {
        purpose: "password_reset",
        passkey_required: true,
        next: "login",
      }),
    });
    renderApp("/setup");
    fireEvent.click(await screen.findByRole("button", { name: "パスワードの再設定" }));
    expect(screen.getByRole("heading", { name: "パスワードの再設定" })).toBeInTheDocument();
    expect(screen.queryByText(/復旧用トークンの場合/)).not.toBeInTheDocument();
    await fillPasswords();
    expect(submitButton()).toBeDisabled();
    fireEvent.change(screen.getByLabelText("トークン"), { target: { value: ` ${TOKEN}\n` } });
    fireEvent.click(submitButton());

    expect(
      await screen.findByRole("heading", { name: "パスワードを設定しました" }),
    ).toBeInTheDocument();
    // passkey_required: the sign-in leads to the Passkey registration (the gate).
    expect(screen.getByText(/Passkey が必須です/)).toBeInTheDocument();
    expect(calls.find((call) => call.path === "/auth/token/redeem")?.body).toEqual({
      token: TOKEN,
      new_password: GOOD,
    });
  });

  it("goes on to the Passkey gate after the sign-in when the account needs one", async () => {
    mockApi({
      "GET /auth/session": [
        apiError(401, "unauthorized"),
        apiError(401, "unauthorized"),
        reply(200, session({ role: "owner", gate: "enrollment_required" })),
      ],
      "POST /auth/token/redeem": reply(200, {
        purpose: "setup",
        passkey_required: true,
        next: "login",
      }),
      "POST /auth/login": reply(200, session({ role: "owner", gate: "enrollment_required" })),
    });
    renderApp(`/setup#${TOKEN}`);
    await fillPasswords();
    fireEvent.click(submitButton());
    fireEvent.click(await screen.findByRole("button", { name: "サインインへ進む" }));
    fireEvent.change(await screen.findByLabelText("ユーザー名"), {
      target: { value: "tomoki" },
    });
    fireEvent.change(screen.getByLabelText("パスワード"), { target: { value: GOOD } });
    fireEvent.click(screen.getByRole("button", { name: "サインイン" }));
    expect(await screen.findByRole("heading", { name: "Passkey が必要です" })).toBeInTheDocument();
  });

  it.each([
    ["too_short", "10 文字以上にしてください。"],
    ["too_common", "よく使われるパスワードか"],
    ["contains_login_name", "ユーザー名を含むパスワードは使えません。"],
  ])("says at once why the server refused the password (%s)", async (problem, text) => {
    mockApi({
      "GET /auth/session": apiError(401, "unauthorized"),
      "POST /auth/token/redeem": policy(problem),
    });
    renderApp(`/setup#${TOKEN}`);
    await fillPasswords();
    fireEvent.click(submitButton());
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("このパスワードは使えません。");
    expect(alert).toHaveTextContent(text);
    // The form stays: the password can be changed and sent again.
    expect(screen.getByLabelText("新しいパスワード")).toHaveValue(GOOD);
  });

  it("explains a refused token and lets another one be entered", async () => {
    const { calls } = mockApi({
      "GET /auth/session": apiError(401, "unauthorized"),
      "POST /auth/token/redeem": apiError(400, "invalid_token"),
    });
    renderApp(`/setup#${TOKEN}`);
    await fillPasswords();
    fireEvent.click(submitButton());
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "トークンが無効か、期限切れか、使用済みです",
    );
    fireEvent.click(screen.getByRole("button", { name: "別のトークンを入力する" }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByLabelText("トークン")).toHaveValue("");
    expect(submitButton()).toBeDisabled();
    expect(calls.filter((call) => call.path === "/auth/token/redeem")).toHaveLength(1);
  });

  it("counts down the wait of a rate limit before allowing another try", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mockApi({
      "GET /auth/session": apiError(401, "unauthorized"),
      "POST /auth/token/redeem": apiError(429, "rate_limited", { "Retry-After": "272" }),
    });
    renderApp(`/setup#${TOKEN}`);
    await fillPasswords();
    fireEvent.click(submitButton());
    expect(await screen.findByRole("alert")).toHaveTextContent("試行が多すぎます");
    // 272 s; real time still passes under shouldAdvanceTime.
    expect(screen.getByText(/^04:(32|31|30)$/)).toBeInTheDocument();
    expect(submitButton()).toBeDisabled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(273_000);
    });
    expect(screen.getByText("再試行できます")).toBeInTheDocument();
    expect(submitButton()).toBeEnabled();
  });

  it("signs out another account's session before going to the sign-in", async () => {
    // A password reset link opened in a browser signed in as someone else: the
    // Backend ends only the token owner's sessions, so this one is still valid.
    const { calls } = mockApi({
      "GET /auth/session": reply(200, session({ role: "admin" })),
      "POST /auth/token/redeem": reply(200, {
        purpose: "password_reset",
        passkey_required: false,
        next: "login",
      }),
      "POST /auth/logout": reply(204),
    });
    renderApp(`/setup#${TOKEN}`);
    await fillPasswords();
    fireEvent.click(submitButton());
    fireEvent.click(await screen.findByRole("button", { name: "サインインへ進む" }));
    expect(await screen.findByRole("heading", { name: "サインイン" })).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "メインナビゲーション" })).toBeNull();
    expect(calls.filter((call) => call.path === "/auth/logout")).toHaveLength(1);
    expect(window.location.pathname).toBe("/");
  });

  it("stays on the page when the other account's sign-out fails, and lets it be retried", async () => {
    const { calls } = mockApi({
      "GET /auth/session": reply(200, session({ role: "admin" })),
      "POST /auth/token/redeem": reply(200, {
        purpose: "password_reset",
        passkey_required: false,
        next: "login",
      }),
      "POST /auth/logout": [apiError(503, "service_unavailable"), reply(204)],
    });
    renderApp(`/setup#${TOKEN}`);
    await fillPasswords();
    fireEvent.click(submitButton());
    fireEvent.click(await screen.findByRole("button", { name: "サインインへ進む" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("パスワードは設定しました");
    expect(alert).toHaveTextContent("サインアウト");
    expect(window.location.pathname).toBe("/setup");
    expect(screen.queryByRole("navigation", { name: "メインナビゲーション" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "もう一度サインアウトする" }));
    expect(await screen.findByRole("heading", { name: "サインイン" })).toBeInTheDocument();
    expect(window.location.pathname).toBe("/");
    expect(calls.filter((call) => call.path === "/auth/logout")).toHaveLength(2);
  });

  it("only advises on the different characters: the server decides (casefold)", async () => {
    // JS lower-casing sees 2 different characters here, Python's casefold 4.
    mockApi({ "GET /auth/session": apiError(401, "unauthorized") });
    renderApp(`/setup#${TOKEN}`);
    await fillPasswords("ΐΐΐΐΐΐΐΐaa");
    expect(screen.getByText(/4 種類以上の文字/).closest("li")).toHaveClass("ng");
    expect(submitButton()).toBeEnabled();
  });

  it("is reachable while signed in (a recovery from a still-open session)", async () => {
    mockApi({ "GET /auth/session": reply(200, session({ role: "owner" })) });
    renderApp(`/setup#${TOKEN}`);
    expect(
      await screen.findByRole("heading", { name: "Owner の初期設定 / 復旧" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "メインナビゲーション" })).toBeNull();
  });
});

describe("password rules", () => {
  it("counts characters after NFKC and needs four different ones", () => {
    expect(passwordChecks("ａｂｃｄｅｆｇｈｉｊ", "ａｂｃｄｅｆｇｈｉｊ")).toEqual({
      count: 10,
      length: true,
      distinct: true,
      match: true,
    });
    expect(passwordChecks("AbAbAbAbAbAb", "").distinct).toBe(false);
    // Lower-casing İ gives two code points (i + U+0307), as the Backend's
    // casefold does: four different characters, which the Backend accepts.
    expect(passwordChecks("İİİİİİİİab", "").distinct).toBe(true);
    expect(passwordChecks("x".repeat(257), "").length).toBe(false);
  });

  it("reads the reason from the fixed message of 422 password_policy", () => {
    const refused = (message: string) => new ApiError(422, "password_policy", message);
    expect(passwordProblem(refused("The password does not meet the policy (too_common)"))).toBe(
      "too_common",
    );
    expect(passwordProblem(refused("The password does not meet the policy (new_rule)"))).toBe(
      "unknown",
    );
    expect(passwordProblem(new ApiError(400, "invalid_token", "x"))).toBeNull();
  });
});
