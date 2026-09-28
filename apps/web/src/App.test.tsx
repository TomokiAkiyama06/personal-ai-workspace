import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { apiError, mockApi, renderApp, reply, session } from "./test/helpers";

afterEach(() => {
  vi.unstubAllGlobals();
});

const waiting = {
  pairing_id: "p-1",
  device_name: "New phone",
  claimed_at: "2026-09-28T02:01:00Z",
  expires_at: "2026-09-28T02:11:00Z",
};

describe("App", () => {
  it("shows the sign-in page (in Japanese) when there is no session", async () => {
    mockApi({ "GET /auth/session": apiError(401, "unauthorized") });
    renderApp("/");
    expect(await screen.findByRole("heading", { name: "サインイン" })).toBeInTheDocument();
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

  it("shows a Member the design's main menu, without 管理 and 使用状況", async () => {
    mockApi({ "GET /auth/session": reply(200, session()) });
    renderApp("/projects");
    const nav = await screen.findByRole("navigation", { name: "メインナビゲーション" });
    const names = within(nav)
      .getAllByRole("link")
      .filter((link) => !link.closest(".drawer-account"))
      .map((link) => link.textContent);
    expect(names).toEqual([
      expect.stringContaining("新しいチャット"),
      expect.stringContaining("チャット"),
      expect.stringContaining("プロジェクト"),
      expect.stringContaining("エージェント / タスク"),
      expect.stringContaining("メモリ"),
      expect.stringContaining("プルリクエスト"),
      expect.stringContaining("設定"),
    ]);
    // The drawer's account link comes first on a phone; it is not a menu entry.
    expect(within(nav).getByRole("link", { name: "プロフィール" })).toBeInTheDocument();
    expect(within(nav).getByRole("link", { name: "プロジェクト" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(within(nav).queryByRole("link", { name: /管理/ })).not.toBeInTheDocument();
    expect(within(nav).queryByRole("link", { name: /使用状況/ })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "プロジェクト" })).toBeInTheDocument();
  });

  it("shows 管理 with the role badge to an Admin and an Owner", async () => {
    for (const [role, badge] of [
      ["admin", "Admin"],
      ["owner", "Owner"],
    ]) {
      mockApi({ "GET /auth/session": reply(200, session({ role })) });
      const view = renderApp("/");
      const nav = await screen.findByRole("navigation", { name: "メインナビゲーション" });
      const admin = within(nav).getByRole("link", { name: /管理/ });
      expect(admin).toHaveTextContent(badge as string);
      view.unmount();
    }
  });

  it("opens 新しいチャット as a later-issue placeholder", async () => {
    mockApi({ "GET /auth/session": reply(200, session()) });
    renderApp("/");
    const user = userEvent.setup();
    const nav = await screen.findByRole("navigation", { name: "メインナビゲーション" });
    await user.click(within(nav).getByRole("link", { name: "新しいチャット" }));
    expect(window.location.pathname).toBe("/chat/new");
    expect(screen.getByRole("heading", { name: "新しいチャット" })).toBeInTheDocument();
    expect(screen.getByText("この画面は後続の Issue で実装します。")).toBeInTheDocument();
  });

  it("navigates without reloading and toggles the phone drawer", async () => {
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
    const nav = screen.getByRole("navigation", { name: "メインナビゲーション" });
    await user.click(within(nav).getByRole("link", { name: "メモリ" }));
    expect(window.location.pathname).toBe("/memory");
    expect(screen.getByRole("heading", { name: "メモリ" })).toBeInTheDocument();
    // Choosing an entry closes the drawer.
    expect(screen.getByRole("button", { name: "メニューを開く" })).toBeInTheDocument();
  });

  it("has the phone bottom tabs: チャット / タスク / メモリ / 通知 / 設定", async () => {
    mockApi({ "GET /auth/session": reply(200, session()) });
    renderApp("/");
    const user = userEvent.setup();
    const tabs = await screen.findByRole("navigation", { name: "主要画面" });
    expect(
      within(tabs)
        .getAllByRole("link")
        .map((link) => link.textContent),
    ).toEqual(["チャット", "タスク", "メモリ", "通知", "設定"]);
    await user.click(within(tabs).getByRole("link", { name: "通知" }));
    expect(window.location.pathname).toBe("/notifications");
    expect(screen.getByRole("heading", { level: 1, name: "通知" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "未読" })).toBeInTheDocument();
  });

  it("sends a restricted session to the passkey gate", async () => {
    mockApi({ "GET /auth/session": reply(200, session({ gate: "enrollment_required" })) });
    renderApp("/");
    expect(await screen.findByRole("heading", { name: "Passkey が必要です" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "登録する" })).toBeInTheDocument();
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
  });

  it("asks for a confirmation when a passkey is registered but not used", async () => {
    mockApi({
      "GET /auth/session": reply(200, session({ gate: "assertion_required", enrolled: true })),
    });
    renderApp("/");
    expect(await screen.findByRole("button", { name: "Passkey で確認" })).toBeInTheDocument();
  });

  it("shows the design's user menu and signs out from it", async () => {
    const { calls } = mockApi({
      "GET /auth/session": reply(200, session()),
      "GET /auth/pairing/pending": reply(200, { pending: [waiting] }),
      "POST /auth/logout": reply(204),
    });
    renderApp("/");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "アカウントメニュー" }));
    const menu = document.querySelector(".user-popover") as HTMLElement;
    expect(within(menu).getByText("Member")).toBeInTheDocument();
    expect(within(menu).getByText(`tomoki @ ${window.location.host}`)).toBeInTheDocument();
    for (const name of [
      "プロフィール",
      "設定",
      "使用状況",
      "キーボードショートカット",
      "ヘルプとドキュメント",
    ]) {
      expect(within(menu).getByRole("link", { name: new RegExp(name) })).toBeInTheDocument();
    }
    expect(
      await within(menu).findByRole("link", { name: /端末とセッション.*承認待ち 1/ }),
    ).toHaveAttribute("href", "/settings/devices");
    // A Member's usage is 設定 › 自分の使用状況.
    expect(within(menu).getByRole("link", { name: /使用状況/ })).toHaveAttribute(
      "href",
      "/settings/usage",
    );
    expect(within(menu).getByText(/v0\.1\.0/)).toBeInTheDocument();
    await user.click(within(menu).getByRole("button", { name: "サインアウト" }));
    expect(await screen.findByRole("heading", { name: "サインイン" })).toBeInTheDocument();
    expect(calls.some((call) => call.method === "POST" && call.path === "/auth/logout")).toBe(true);
  });

  it("switches the theme from the user menu and remembers it", async () => {
    mockApi({ "GET /auth/session": reply(200, session()) });
    renderApp("/");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "アカウントメニュー" }));
    const group = screen.getByRole("group", { name: "テーマを選ぶ" });
    expect(within(group).getByRole("button", { name: "システム" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(document.documentElement.dataset.theme).toBeUndefined();
    await user.click(within(group).getByRole("button", { name: "ライト" }));
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(window.localStorage.getItem("paw.theme")).toBe("light");
    await user.click(within(group).getByRole("button", { name: "システム" }));
    expect(document.documentElement.dataset.theme).toBeUndefined();
  });

  it("toggles light / dark from the header", async () => {
    mockApi({ "GET /auth/session": reply(200, session()) });
    renderApp("/");
    const user = userEvent.setup();
    // Without prefers-color-scheme (jsdom) "system" is the design's default, dark.
    await user.click(await screen.findByRole("button", { name: "ライトテーマに切り替える" }));
    expect(document.documentElement.dataset.theme).toBe("light");
    await user.click(screen.getByRole("button", { name: "ダークテーマに切り替える" }));
    expect(document.documentElement.dataset.theme).toBe("dark");
  });

  it("keeps working when the browser storage is blocked", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    mockApi({ "GET /auth/session": reply(200, session()) });
    renderApp("/");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "ライトテーマに切り替える" }));
    expect(document.documentElement.dataset.theme).toBe("light");
  });

  it("can still render the English catalog (no switch on screen in V1)", async () => {
    mockApi({ "GET /auth/session": apiError(401, "unauthorized") });
    renderApp("/", { locale: "en" });
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    await waitFor(() => expect(document.documentElement.lang).toBe("en"));
  });
});

describe("signing out", () => {
  it("stays signed in and says so when the server could not be told", async () => {
    mockApi({
      "GET /auth/session": reply(200, session()),
      "POST /auth/logout": apiError(503, "service_unavailable"),
    });
    renderApp("/");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "アカウントメニュー" }));
    await user.click(screen.getByRole("button", { name: "サインアウト" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("一時的に利用できません");
    expect(screen.getByRole("navigation", { name: "メインナビゲーション" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "サインイン" })).not.toBeInTheDocument();
  });

  it("treats a session the server already ended as signed out", async () => {
    mockApi({
      "GET /auth/session": reply(200, session()),
      "POST /auth/logout": apiError(401, "unauthorized"),
    });
    renderApp("/");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "アカウントメニュー" }));
    await user.click(screen.getByRole("button", { name: "サインアウト" }));
    expect(await screen.findByRole("heading", { name: "サインイン" })).toBeInTheDocument();
  });
});

describe("the startup session probe", () => {
  it("does not undo a pairing that completed before its late 401", async () => {
    mockApi({
      "POST /auth/pairing/claim": reply(200, {
        status: "completed",
        session: session(),
        claim: null,
        confirmation_code: null,
        expires_at: null,
      }),
    });
    const tableFetch = globalThis.fetch;
    let answerProbe: (response: Response) => void = () => {};
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        if (String(input).endsWith("/auth/session")) {
          return new Promise<Response>((resolve) => {
            answerProbe = resolve;
          });
        }
        return tableFetch(input, init);
      }),
    );
    renderApp("/pair#tok_123");
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("この端末の名前"), "Phone");
    await user.click(screen.getByRole("button", { name: "続ける" }));
    expect(await screen.findByRole("navigation", { name: "メインナビゲーション" })).toBeVisible();
    answerProbe(
      new Response(JSON.stringify({ error: { code: "unauthorized", message: "x" } }), {
        status: 401,
        headers: { "Content-Type": "application/json" },
      }),
    );
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.getByRole("navigation", { name: "メインナビゲーション" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "サインイン" })).not.toBeInTheDocument();
  });
});

describe("a 401 of an earlier session", () => {
  it("does not end the session signed in meanwhile", async () => {
    mockApi({
      "GET /auth/session": [reply(200, session()), apiError(401, "unauthorized")],
      "POST /auth/logout": reply(204),
      "POST /auth/login": reply(200, session()),
    });
    const tableFetch = globalThis.fetch;
    let answerPending: (response: Response) => void = () => {};
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        if (String(input).endsWith("/auth/pairing/pending")) {
          return new Promise<Response>((resolve) => {
            answerPending = resolve;
          });
        }
        return tableFetch(input, init);
      }),
    );
    renderApp("/");
    const user = userEvent.setup();
    // Opening the menu starts GET /auth/pairing/pending under the first session.
    await user.click(await screen.findByRole("button", { name: "アカウントメニュー" }));
    await user.click(screen.getByRole("button", { name: "サインアウト" }));
    await user.type(await screen.findByLabelText("ユーザー名"), "tomoki");
    await user.type(screen.getByLabelText("パスワード"), "correct horse");
    await user.click(screen.getByRole("button", { name: "サインイン" }));
    expect(await screen.findByRole("navigation", { name: "メインナビゲーション" })).toBeVisible();
    answerPending(
      new Response(JSON.stringify({ error: { code: "unauthorized", message: "x" } }), {
        status: 401,
        headers: { "Content-Type": "application/json" },
      }),
    );
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.getByRole("navigation", { name: "メインナビゲーション" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "サインイン" })).not.toBeInTheDocument();
  });
});

describe("an ended session", () => {
  it("returns to the sign-in page with a message when any request finds it ended", async () => {
    mockApi({
      "GET /auth/session": reply(200, session()),
      "GET /auth/sessions": apiError(401, "unauthorized"),
    });
    renderApp("/settings/devices");
    expect(await screen.findByRole("heading", { name: "サインイン" })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("サインインの有効期限が切れました");
  });

  it("does not treat a wrong password as an ended session", async () => {
    mockApi({
      "GET /auth/session": reply(200, session()),
      "GET /auth/sessions": reply(200, { sessions: [session().session] }),
      "GET /auth/pairing/pending": reply(200, { pending: [] }),
      "GET /auth/passkeys": reply(200, { passkeys: [] }),
      "POST /auth/pairing": apiError(401, "invalid_credentials"),
    });
    renderApp("/settings/devices");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "新しい端末を追加" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("ユーザー名またはパスワード");
    expect(screen.getByRole("navigation", { name: "設定メニュー" })).toBeInTheDocument();
  });
});
