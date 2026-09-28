import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { apiError, mockApi, renderApp, reply, session } from "./test/helpers";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("App", () => {
  it("shows the login page (in Japanese) when there is no session", async () => {
    mockApi({ "GET /auth/session": apiError(401, "unauthorized") });
    renderApp("/");
    expect(await screen.findByRole("heading", { name: "ログイン" })).toBeInTheDocument();
    expect(document.documentElement.lang).toBe("ja");
  });

  it("offers a retry when the server cannot be reached", async () => {
    mockApi({
      "GET /auth/session": [apiError(503, "service_unavailable"), reply(200, session())],
    });
    renderApp("/");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "再試行" }));
    expect(await screen.findByRole("navigation", { name: "メインナビゲーション" })).toBeVisible();
  });

  it("renders the shell with the global navigation for a signed-in user", async () => {
    mockApi({ "GET /auth/session": reply(200, session()) });
    renderApp("/projects");
    const nav = await screen.findByRole("navigation", { name: "メインナビゲーション" });
    for (const name of [
      "チャット",
      "プロジェクト",
      "メモリ",
      "プルリクエスト",
      "使用状況",
      "設定",
    ]) {
      expect(within(nav).getByRole("link", { name })).toBeInTheDocument();
    }
    expect(within(nav).getByRole("link", { name: "プロジェクト" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    // Admin is shown to an Owner / Admin only (the Backend enforces it anyway).
    expect(within(nav).queryByRole("link", { name: "管理" })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "プロジェクト" })).toBeInTheDocument();
  });

  it("shows the Admin entry to an Admin and an Owner", async () => {
    for (const role of ["admin", "owner"]) {
      mockApi({ "GET /auth/session": reply(200, session({ role })) });
      const view = renderApp("/");
      const nav = await screen.findByRole("navigation", { name: "メインナビゲーション" });
      expect(within(nav).getByRole("link", { name: "管理" })).toBeInTheDocument();
      view.unmount();
    }
  });

  it("navigates without reloading and toggles the narrow-screen menu", async () => {
    mockApi({ "GET /auth/session": reply(200, session()) });
    renderApp("/");
    const user = userEvent.setup();
    const toggle = await screen.findByRole("button", { name: "メニューを開く" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await user.click(toggle);
    expect(screen.getByRole("button", { name: "メニューを閉じる" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    await user.click(screen.getByRole("link", { name: "メモリ" }));
    expect(window.location.pathname).toBe("/memory");
    expect(screen.getByRole("heading", { name: "メモリ" })).toBeInTheDocument();
    // Choosing an entry closes the drawer.
    expect(screen.getByRole("button", { name: "メニューを開く" })).toBeInTheDocument();
  });

  it("sends a restricted session to the passkey gate", async () => {
    mockApi({ "GET /auth/session": reply(200, session({ gate: "enrollment_required" })) });
    renderApp("/");
    expect(await screen.findByRole("heading", { name: "パスキーが必要です" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "パスキーを登録" })).toBeInTheDocument();
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
  });

  it("asks for an authentication when a passkey is registered but not used", async () => {
    mockApi({
      "GET /auth/session": reply(200, session({ gate: "assertion_required", enrolled: true })),
    });
    renderApp("/");
    expect(await screen.findByRole("button", { name: "パスキーで認証" })).toBeInTheDocument();
  });

  it("signs out from the user menu", async () => {
    const { calls } = mockApi({
      "GET /auth/session": reply(200, session()),
      "POST /auth/logout": reply(204),
    });
    renderApp("/");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "ユーザーメニュー" }));
    await user.click(screen.getByRole("button", { name: "ログアウト" }));
    expect(await screen.findByRole("heading", { name: "ログイン" })).toBeInTheDocument();
    expect(calls.some((call) => call.method === "POST" && call.path === "/auth/logout")).toBe(true);
  });

  it("can be shown in English", async () => {
    mockApi({ "GET /auth/session": apiError(401, "unauthorized") });
    renderApp("/", { locale: "en" });
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    await waitFor(() => expect(document.documentElement.lang).toBe("en"));
  });
});

describe("an ended session", () => {
  it("returns to the login page with a message when any request finds it ended", async () => {
    mockApi({
      "GET /auth/session": reply(200, session()),
      "GET /auth/passkeys": apiError(401, "unauthorized"),
    });
    renderApp("/settings/security");
    expect(await screen.findByRole("heading", { name: "ログイン" })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("ログインの有効期限が切れました");
  });

  it("does not treat a wrong password as an ended session", async () => {
    mockApi({
      "GET /auth/session": reply(200, session()),
      "GET /auth/sessions": reply(200, { sessions: [session().session] }),
      "GET /auth/pairing/pending": reply(200, { pending: [] }),
      "POST /auth/pairing": apiError(401, "invalid_credentials"),
    });
    renderApp("/settings/devices");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "新規端末を追加" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("ログイン名またはパスワード");
    expect(screen.getByRole("navigation", { name: "メインナビゲーション" })).toBeInTheDocument();
  });
});
