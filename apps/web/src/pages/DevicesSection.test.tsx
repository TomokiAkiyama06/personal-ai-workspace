import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { apiError, mockApi, renderApp, reply, session } from "../test/helpers";

afterEach(() => {
  vi.useRealTimers();
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
  expires_at: new Date(Date.now() + 4 * 60_000 + 12_000).toISOString(),
  approval_required: true,
};

const waiting = {
  pairing_id: "p-1",
  device_name: "New phone",
  claimed_at: "2026-09-28T02:01:00Z",
  expires_at: "2026-09-28T02:11:00Z",
};

const base = {
  "GET /auth/session": reply(200, session()),
  "GET /auth/passkeys": reply(200, { passkeys: [] }),
};

// Issuing waits for the first read of the device list (its sessions are the baseline).
async function addDevice(user: ReturnType<typeof userEvent.setup>) {
  const button = await screen.findByRole("button", { name: "新しい端末を追加" });
  await waitFor(() => expect(button).toBeEnabled());
  await user.click(button);
}

function trusted() {
  return screen.getByRole("region", { name: "信頼済み端末" });
}

describe("設定 › 端末とセッション", () => {
  it("is the design's settings screen with the account group", async () => {
    mockApi({
      ...base,
      "GET /auth/sessions": reply(200, { sessions: [session().session] }),
      "GET /auth/pairing/pending": reply(200, { pending: [] }),
    });
    renderApp("/settings/devices");
    expect(
      await screen.findByRole("heading", { level: 1, name: "端末とセッション" }),
    ).toBeVisible();
    expect(screen.getByRole("link", { name: "ワークスペースへ戻る" })).toHaveAttribute("href", "/");
    const menu = screen.getByRole("navigation", { name: "設定メニュー" });
    expect(within(menu).getByRole("link", { name: "端末とセッション" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    // A Member sees only the アカウント group.
    expect(within(menu).queryByText("ワークスペース")).not.toBeInTheDocument();
  });

  it("lists the signed-in devices and signs one out", async () => {
    const { calls } = mockApi({
      ...base,
      "GET /auth/sessions": [
        reply(200, { sessions: [session().session, otherSession] }),
        reply(200, { sessions: [session().session] }),
      ],
      "GET /auth/pairing/pending": reply(200, { pending: [] }),
      "DELETE /auth/sessions/s-2": reply(204),
    });
    renderApp("/settings/devices");
    const user = userEvent.setup();
    await screen.findByText("Phone");
    const list = trusted();
    expect(within(list).getByText("この端末")).toBeInTheDocument();
    const row = within(list).getByText("Phone").closest("li") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: "サインアウト" }));
    await waitFor(() => expect(within(list).queryByText("Phone")).not.toBeInTheDocument());
    expect(
      calls.some((call) => call.method === "DELETE" && call.path === "/auth/sessions/s-2"),
    ).toBe(true);
  });

  it("signs every other device out", async () => {
    mockApi({
      ...base,
      "GET /auth/sessions": reply(200, { sessions: [session().session, otherSession] }),
      "GET /auth/pairing/pending": reply(200, { pending: [] }),
      "POST /auth/sessions/revoke-others": reply(200, { revoked: 1 }),
    });
    renderApp("/settings/devices");
    const user = userEvent.setup();
    const zone = await screen.findByRole("region", { name: "他のすべての端末からサインアウト" });
    expect(within(zone).getByText("この端末以外の 1 件のセッションを失効させます。")).toBeVisible();
    await user.click(within(zone).getByRole("button", { name: "サインアウトを実行" }));
    expect(await screen.findByRole("status")).toHaveTextContent(
      "1 件のセッションをサインアウトさせました。",
    );
  });

  it("shows a one-time QR code and link with a countdown, and can revoke it", async () => {
    const { calls } = mockApi({
      ...base,
      "GET /auth/sessions": reply(200, { sessions: [session().session] }),
      "GET /auth/pairing/pending": reply(200, { pending: [] }),
      "POST /auth/pairing": reply(201, pairing),
      "DELETE /auth/pairing": reply(200, { revoked: 1 }),
    });
    renderApp("/settings/devices");
    const user = userEvent.setup();
    await addDevice(user);
    expect(
      await screen.findByRole("img", { name: "新しい端末で読み取る QR コード" }),
    ).toBeVisible();
    expect(screen.getByLabelText("読み取れない場合のリンク")).toHaveValue(
      `${window.location.origin}/pair#tok_123`,
    );
    expect(screen.getByText(/残り 0[34]:\d\d で失効/)).toBeInTheDocument();
    expect(screen.getByText(/この一覧での承認が必要です/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "発行を取り消す" }));
    expect(await screen.findByRole("button", { name: "新しい端末を追加" })).toBeInTheDocument();
    expect(calls.some((call) => call.method === "DELETE" && call.path === "/auth/pairing")).toBe(
      true,
    );
  });

  it("says when the QR code expired", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mockApi({
      ...base,
      "GET /auth/sessions": reply(200, { sessions: [session().session] }),
      "GET /auth/pairing/pending": reply(200, { pending: [] }),
      "POST /auth/pairing": reply(201, {
        ...pairing,
        approval_required: false,
        expires_at: new Date(Date.now() + 2000).toISOString(),
      }),
    });
    renderApp("/settings/devices");
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    await addDevice(user);
    await screen.findByText(/残り 00:0\d で失効/);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    expect(screen.getByText("期限が切れました。もう一度発行してください。")).toBeInTheDocument();
  });

  it("asks for a passkey (not a password) before approving a waiting device", async () => {
    const { calls } = mockApi({
      ...base,
      "GET /auth/session": reply(200, session({ enrolled: true })),
      "GET /auth/sessions": reply(200, { sessions: [session().session] }),
      "GET /auth/pairing/pending": reply(200, { pending: [waiting] }),
      "POST /auth/pairing/p-1/approve": apiError(403, "step_up_required"),
    });
    renderApp("/settings/devices");
    const user = userEvent.setup();
    await screen.findByText("New phone");
    const row = within(trusted()).getByText("New phone").closest("li") as HTMLElement;
    expect(within(row).getByText("承認待ち")).toBeInTheDocument();
    await user.type(
      within(row).getByLabelText("新しい端末に表示されている確認コード"),
      "AB12-CD34",
    );
    await user.click(within(row).getByRole("button", { name: "承認" }));

    // The Backend asked for a step-up: the prompt is inline, not a modal, and
    // offers only a passkey (approving always needs one; Decision 0033).
    const prompt = await screen.findByRole("region", { name: "本人確認が必要な操作です" });
    expect(within(prompt).getByRole("button", { name: "Passkey で確認" })).toBeInTheDocument();
    expect(within(prompt).queryByLabelText("パスワード")).not.toBeInTheDocument();
    expect(calls.filter((call) => call.path === "/auth/pairing/p-1/approve")[0]?.body).toEqual({
      confirmation_code: "AB12-CD34",
    });
  });

  it("approves a waiting device when the step-up is still valid", async () => {
    mockApi({
      ...base,
      "GET /auth/sessions": reply(200, { sessions: [session().session] }),
      "GET /auth/pairing/pending": [
        reply(200, { pending: [waiting] }),
        reply(200, { pending: [] }),
      ],
      "POST /auth/pairing/p-1/approve": reply(204),
    });
    renderApp("/settings/devices");
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("新しい端末に表示されている確認コード"), "AB12");
    await user.click(screen.getByRole("button", { name: "承認" }));
    expect(await screen.findByText("端末を承認しました。")).toBeInTheDocument();
  });

  it("keeps reading the devices until the approved one has signed in", async () => {
    const soon = { ...waiting, expires_at: new Date(Date.now() + 60_000).toISOString() };
    const newDevice = { ...otherSession, id: "s-3", device_name: "Approved phone" };
    mockApi({
      ...base,
      "GET /auth/sessions": [
        reply(200, { sessions: [session().session] }),
        reply(200, { sessions: [session().session] }),
        reply(200, { sessions: [session().session, newDevice] }),
      ],
      "GET /auth/pairing/pending": [reply(200, { pending: [soon] }), reply(200, { pending: [] })],
      "POST /auth/pairing/p-1/approve": reply(204),
    });
    renderApp("/settings/devices");
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("新しい端末に表示されている確認コード"), "AB12");
    await user.click(screen.getByRole("button", { name: "承認" }));
    expect(await screen.findByText("端末を承認しました。")).toBeInTheDocument();
    expect(screen.queryByText("Approved phone")).not.toBeInTheDocument();
    // The new device completes its pairing a little later; the list follows.
    expect(await screen.findByText("Approved phone", {}, { timeout: 7000 })).toBeInTheDocument();
  }, 10000);

  it("drops a QR code that needs no approval once the new device has signed in", async () => {
    const newDevice = { ...otherSession, id: "s-3", device_name: "Paired phone" };
    const { calls } = mockApi({
      ...base,
      "GET /auth/sessions": [
        reply(200, { sessions: [session().session] }),
        reply(200, { sessions: [session().session, newDevice] }),
      ],
      "GET /auth/pairing/pending": reply(200, { pending: [] }),
      "POST /auth/pairing": reply(201, { ...pairing, approval_required: false }),
    });
    renderApp("/settings/devices");
    const user = userEvent.setup();
    await addDevice(user);
    expect(
      await screen.findByRole("img", { name: "新しい端末で読み取る QR コード" }),
    ).toBeVisible();
    // The claim created the session at once (no approval): the spent QR code goes.
    expect(await screen.findByText("Paired phone", {}, { timeout: 12000 })).toBeInTheDocument();
    expect(await screen.findByRole("status")).toHaveTextContent("新しい端末がサインインしました。");
    expect(
      screen.queryByRole("img", { name: "新しい端末で読み取る QR コード" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "新しい端末を追加" })).toBeInTheDocument();
    expect(calls.some((call) => call.path === "/auth/pairing")).toBe(true);
  }, 15000);

  it("waits for the device list before a new device can be added", async () => {
    mockApi({
      ...base,
      "GET /auth/pairing/pending": reply(200, { pending: [] }),
    });
    const tableFetch = globalThis.fetch;
    let answerSessions: (response: Response) => void = () => {};
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        if (String(input).endsWith("/auth/sessions")) {
          return new Promise<Response>((resolve) => {
            answerSessions = resolve;
          });
        }
        return tableFetch(input, init);
      }),
    );
    renderApp("/settings/devices");
    // Without the known sessions, the current one would later look like the new device.
    expect(await screen.findByRole("button", { name: "新しい端末を追加" })).toBeDisabled();
    answerSessions(
      new Response(JSON.stringify({ sessions: [session().session] }), {
        headers: { "Content-Type": "application/json" },
      }),
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "新しい端末を追加" })).toBeEnabled(),
    );
  });

  it("ignores a read of the devices that was in flight while one was signed out", async () => {
    mockApi({
      ...base,
      "GET /auth/pairing/pending": reply(200, { pending: [] }),
      "POST /auth/pairing": reply(201, { ...pairing, approval_required: false }),
      "DELETE /auth/sessions/s-2": reply(204),
    });
    const tableFetch = globalThis.fetch;
    let reads = 0;
    let answerPoll: (response: Response) => void = () => {};
    const json = (body: unknown) =>
      new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        if (String(input).endsWith("/auth/sessions") && (init?.method ?? "GET") === "GET") {
          reads += 1;
          if (reads === 1)
            return Promise.resolve(json({ sessions: [session().session, otherSession] }));
          if (reads === 2) {
            return new Promise<Response>((resolve) => {
              answerPoll = resolve;
            });
          }
          return Promise.resolve(json({ sessions: [session().session] }));
        }
        return tableFetch(input, init);
      }),
    );
    renderApp("/settings/devices");
    const user = userEvent.setup();
    await addDevice(user);
    // The watcher's five-second read starts and is left hanging.
    await waitFor(() => expect(reads).toBe(2), { timeout: 7000 });
    const row = within(trusted()).getByText("Phone").closest("li") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: "サインアウト" }));
    await waitFor(() => expect(within(trusted()).queryByText("Phone")).not.toBeInTheDocument());
    answerPoll(json({ sessions: [session().session, otherSession] }));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(within(trusted()).queryByText("Phone")).not.toBeInTheDocument();
  }, 10000);

  it("keeps waiting for the approved device when the step-up rotated this session", async () => {
    const soon = { ...waiting, expires_at: new Date(Date.now() + 60_000).toISOString() };
    const rotated = { ...session().session, id: "s-rotated" };
    const newDevice = { ...otherSession, id: "s-3", device_name: "Approved phone" };
    mockApi({
      ...base,
      "GET /auth/sessions": [
        reply(200, { sessions: [session().session] }),
        // Right after the approval: only this session, under its new id.
        reply(200, { sessions: [rotated] }),
        reply(200, { sessions: [rotated, newDevice] }),
      ],
      "GET /auth/pairing/pending": [reply(200, { pending: [soon] }), reply(200, { pending: [] })],
      "POST /auth/pairing/p-1/approve": reply(204),
    });
    renderApp("/settings/devices");
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("新しい端末に表示されている確認コード"), "AB12");
    await user.click(screen.getByRole("button", { name: "承認" }));
    expect(await screen.findByText("端末を承認しました。")).toBeInTheDocument();
    expect(await screen.findByText("Approved phone", {}, { timeout: 7000 })).toBeInTheDocument();
  }, 10000);

  it("keeps waiting for the new device after a failed read of the list", async () => {
    const newDevice = { ...otherSession, id: "s-3", device_name: "Paired phone" };
    mockApi({
      ...base,
      "GET /auth/sessions": [
        reply(200, { sessions: [session().session] }),
        apiError(503, "service_unavailable"),
        reply(200, { sessions: [session().session, newDevice] }),
      ],
      "GET /auth/pairing/pending": reply(200, { pending: [] }),
      "POST /auth/pairing": reply(201, { ...pairing, approval_required: false }),
    });
    renderApp("/settings/devices");
    const user = userEvent.setup();
    await addDevice(user);
    expect(await screen.findByText("Paired phone", {}, { timeout: 12000 })).toBeInTheDocument();
    expect(await screen.findByRole("status")).toHaveTextContent("新しい端末がサインインしました。");
  }, 15000);

  it("shows a device waiting for approval that another browser issued the code for", async () => {
    mockApi({
      ...base,
      "GET /auth/sessions": reply(200, { sessions: [session().session] }),
      // Nothing waits when the page opens; the claim comes in a little later.
      "GET /auth/pairing/pending": [
        reply(200, { pending: [] }),
        reply(200, { pending: [waiting] }),
      ],
    });
    renderApp("/settings/devices");
    await screen.findByRole("button", { name: "新しい端末を追加" });
    expect(screen.queryByText("New phone")).not.toBeInTheDocument();
    expect(await screen.findByText("New phone", {}, { timeout: 7000 })).toBeInTheDocument();
  }, 10000);

  it("drops the QR code once another device has decided its claim, and shows the new device", async () => {
    const newDevice = { ...otherSession, id: "s-3", device_name: "Approved phone" };
    mockApi({
      ...base,
      "GET /auth/sessions": [
        reply(200, { sessions: [session().session] }),
        reply(200, { sessions: [session().session, newDevice] }),
      ],
      // Claimed, then approved on another signed-in device of this account.
      "GET /auth/pairing/pending": [
        reply(200, { pending: [] }),
        reply(200, { pending: [waiting] }),
        reply(200, { pending: [] }),
      ],
      "POST /auth/pairing": reply(201, pairing),
    });
    renderApp("/settings/devices");
    const user = userEvent.setup();
    await addDevice(user);
    expect(
      await screen.findByRole("img", { name: "新しい端末で読み取る QR コード" }),
    ).toBeVisible();
    expect(await screen.findByText("New phone", {}, { timeout: 7000 })).toBeInTheDocument();
    await waitFor(
      () =>
        expect(
          screen.queryByRole("img", { name: "新しい端末で読み取る QR コード" }),
        ).not.toBeInTheDocument(),
      { timeout: 7000 },
    );
    expect(await screen.findByText("Approved phone", {}, { timeout: 7000 })).toBeInTheDocument();
    expect(await screen.findByRole("status")).toHaveTextContent("新しい端末がサインインしました。");
  }, 25000);

  it("shows a wrong confirmation code as such", async () => {
    mockApi({
      ...base,
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

  it("drops the issued QR code after refusing the device that claimed it", async () => {
    mockApi({
      ...base,
      "GET /auth/sessions": reply(200, { sessions: [session().session] }),
      "GET /auth/pairing/pending": [
        reply(200, { pending: [] }),
        reply(200, { pending: [waiting] }),
        reply(200, { pending: [] }),
      ],
      "POST /auth/pairing": reply(201, pairing),
      "POST /auth/pairing/p-1/reject": reply(204),
    });
    renderApp("/settings/devices");
    const user = userEvent.setup();
    await addDevice(user);
    expect(
      await screen.findByRole("img", { name: "新しい端末で読み取る QR コード" }),
    ).toBeVisible();
    await waitFor(() => expect(screen.getByText("New phone")).toBeInTheDocument(), {
      timeout: 7000,
    });
    await user.click(screen.getByRole("button", { name: "拒否" }));
    expect(await screen.findByText("端末を拒否しました。")).toBeInTheDocument();
    expect(
      screen.queryByRole("img", { name: "新しい端末で読み取る QR コード" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "新しい端末を追加" })).toBeInTheDocument();
  }, 10000);

  it("ignores a poll that was in flight while the device was refused", async () => {
    const { calls } = mockApi({
      ...base,
      "GET /auth/sessions": reply(200, { sessions: [session().session] }),
      "POST /auth/pairing": reply(201, pairing),
      "POST /auth/pairing/p-1/reject": reply(204),
    });
    const tableFetch = globalThis.fetch;
    let reads = 0;
    let answerPoll: (response: Response) => void = () => {};
    const json = (body: unknown) =>
      new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        if (String(input).endsWith("/auth/pairing/pending")) {
          reads += 1;
          if (reads === 1) return Promise.resolve(json({ pending: [waiting] }));
          if (reads === 2) {
            return new Promise<Response>((resolve) => {
              answerPoll = resolve;
            });
          }
          return Promise.resolve(json({ pending: [] }));
        }
        return tableFetch(input, init);
      }),
    );
    renderApp("/settings/devices");
    const user = userEvent.setup();
    await addDevice(user);
    // The five-second poll starts and is left hanging.
    await waitFor(() => expect(reads).toBe(2), { timeout: 7000 });
    await user.click(screen.getByRole("button", { name: "拒否" }));
    expect(await screen.findByText("端末を拒否しました。")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("New phone")).not.toBeInTheDocument());
    answerPoll(json({ pending: [waiting] }));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.queryByText("New phone")).not.toBeInTheDocument();
    expect(calls.some((call) => call.path === "/auth/pairing/p-1/reject")).toBe(true);
  }, 10000);

  it("rejects a waiting device", async () => {
    const { calls } = mockApi({
      ...base,
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

describe("the settings menu per role", () => {
  it("shows the ワークスペース group to an Admin, without 権限制御", async () => {
    mockApi({ "GET /auth/session": reply(200, session({ role: "admin" })) });
    renderApp("/settings");
    const menu = await screen.findByRole("navigation", { name: "設定メニュー" });
    for (const name of [
      "メンバー",
      "サインインと Passkey",
      "GitHub App と Repo",
      "Codex / Claude 接続",
      "バックアップと復旧",
    ]) {
      expect(within(menu).getByRole("link", { name })).toBeInTheDocument();
    }
    expect(within(menu).queryByRole("link", { name: "権限制御" })).not.toBeInTheDocument();
    // /settings opens プロフィール, with the translated role.
    expect(screen.getByRole("heading", { level: 1, name: "プロフィール" })).toBeInTheDocument();
    expect(within(screen.getByRole("main")).getByText("Admin")).toBeInTheDocument();
  });

  it("shows 権限制御 to the Owner", async () => {
    mockApi({ "GET /auth/session": reply(200, session({ role: "owner" })) });
    renderApp("/settings/permissions");
    const menu = await screen.findByRole("navigation", { name: "設定メニュー" });
    expect(within(menu).getByRole("link", { name: "権限制御" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.getByText("この画面は後続の Issue で実装します。")).toBeInTheDocument();
  });

  it("labels a user as Member", async () => {
    mockApi({ "GET /auth/session": reply(200, session()) });
    renderApp("/settings/profile");
    await screen.findByRole("heading", { level: 1, name: "プロフィール" });
    expect(within(screen.getByRole("main")).getByText("Member")).toBeInTheDocument();
  });

  it("shows the language as fixed Japanese and lets the theme be chosen (言語と外観)", async () => {
    mockApi({ "GET /auth/session": reply(200, session()) });
    renderApp("/settings/appearance");
    const user = userEvent.setup();
    expect(await screen.findByText("日本語")).toBeInTheDocument();
    expect(screen.getByText("固定")).toBeInTheDocument();
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    expect(screen.queryByText("English")).not.toBeInTheDocument();
    expect(screen.getByText(/端末から自動検出/)).toBeInTheDocument();
    await user.click(screen.getByRole("radio", { name: "ライト" }));
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(screen.getByRole("radio", { name: "システムに合わせる" })).not.toBeChecked();
  });
});
