import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { apiError, mockApi, renderApp, reply, session } from "../test/helpers";

afterEach(() => {
  vi.unstubAllGlobals();
  Reflect.deleteProperty(navigator, "credentials");
});

const passkey = {
  id: "pk-1",
  name: "MacBook",
  created_at: "2026-09-01T00:00:00Z",
  last_used_at: null,
  backup_eligible: true,
  backed_up: true,
};

const devicesPage = {
  "GET /auth/sessions": reply(200, { sessions: [session().session] }),
  "GET /auth/pairing/pending": reply(200, { pending: [] }),
};

/** A browser whose passkey prompt answers at once. */
function fakeWebAuthn() {
  vi.stubGlobal("PublicKeyCredential", function PublicKeyCredential() {});
  const credential = { toJSON: () => ({ id: "cred" }) };
  Object.defineProperty(navigator, "credentials", {
    configurable: true,
    value: { create: vi.fn(async () => credential), get: vi.fn(async () => credential) },
  });
}

function passkeyCard() {
  return screen.getByRole("region", { name: "Passkey" });
}

async function confirmRemoval(user: ReturnType<typeof userEvent.setup>) {
  const card = await screen.findByRole("region", { name: "Passkey" });
  await user.click(await within(card).findByRole("button", { name: "削除" }));
  expect(within(card).getByText("Passkey「MacBook」を削除しますか？")).toBeInTheDocument();
  const row = within(card).getByText("MacBook").closest("li") as HTMLElement;
  await user.click(within(row).getByRole("button", { name: "削除" }));
}

describe("Passkeys (設定 › 端末とセッション)", () => {
  it("lists the passkeys", async () => {
    mockApi({
      ...devicesPage,
      "GET /auth/session": reply(200, session({ enrolled: true })),
      "GET /auth/passkeys": reply(200, { passkeys: [passkey] }),
    });
    renderApp("/settings/devices");
    expect(await screen.findByText("MacBook")).toBeInTheDocument();
    const card = passkeyCard();
    expect(within(card).getByText("同期")).toBeInTheDocument();
    expect(within(card).getByText("未使用")).toBeInTheDocument();
    expect(within(card).getByRole("button", { name: "この端末に追加" })).toBeInTheDocument();
  });

  it("removes a passkey after an inline confirmation", async () => {
    const { calls } = mockApi({
      ...devicesPage,
      "GET /auth/session": reply(200, session({ enrolled: true })),
      "GET /auth/passkeys": [reply(200, { passkeys: [passkey] }), reply(200, { passkeys: [] })],
      "DELETE /auth/passkeys/pk-1": reply(200, {
        revoked: true,
        sessions_ended: 0,
        signed_out: false,
      }),
    });
    renderApp("/settings/devices");
    const user = userEvent.setup();
    await confirmRemoval(user);
    expect(await screen.findByText("Passkey を削除しました。")).toBeInTheDocument();
    expect(await screen.findByText("登録済みの Passkey はありません。")).toBeInTheDocument();
    expect(
      calls.some((call) => call.method === "DELETE" && call.path === "/auth/passkeys/pk-1"),
    ).toBe(true);
  });

  it("re-reads the session and the devices when removing a passkey ended other sessions", async () => {
    const other = { ...session().session, id: "s-2", device_name: "Phone", current: false };
    const { calls } = mockApi({
      "GET /auth/session": [
        reply(200, session({ enrolled: true })),
        reply(200, session({ enrolled: false })),
      ],
      "GET /auth/sessions": [
        reply(200, { sessions: [session().session, other] }),
        reply(200, { sessions: [session().session] }),
      ],
      "GET /auth/pairing/pending": reply(200, { pending: [] }),
      "GET /auth/passkeys": [reply(200, { passkeys: [passkey] }), reply(200, { passkeys: [] })],
      "DELETE /auth/passkeys/pk-1": reply(200, {
        revoked: true,
        sessions_ended: 1,
        signed_out: false,
      }),
    });
    renderApp("/settings/devices");
    const user = userEvent.setup();
    expect(await screen.findByText("Phone")).toBeInTheDocument();
    await confirmRemoval(user);
    expect(await screen.findByText("Passkey を削除しました。")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("Phone")).not.toBeInTheDocument());
    expect(calls.filter((call) => call.path === "/auth/session")).toHaveLength(2);
  });

  it("returns to the sign-in page when removing the passkey ended this session", async () => {
    mockApi({
      ...devicesPage,
      "GET /auth/session": reply(200, session({ enrolled: true })),
      "GET /auth/passkeys": reply(200, { passkeys: [passkey] }),
      "DELETE /auth/passkeys/pk-1": reply(200, {
        revoked: true,
        sessions_ended: 1,
        signed_out: true,
      }),
    });
    renderApp("/settings/devices");
    const user = userEvent.setup();
    await confirmRemoval(user);
    expect(await screen.findByRole("heading", { name: "サインイン" })).toBeInTheDocument();
  });

  it("asks for a passkey step-up when a password one is not enough, and can be cancelled", async () => {
    mockApi({
      ...devicesPage,
      "GET /auth/session": reply(200, session({ enrolled: true })),
      "GET /auth/passkeys": reply(200, { passkeys: [passkey] }),
      "DELETE /auth/passkeys/pk-1": apiError(403, "step_up_method_insufficient"),
    });
    renderApp("/settings/devices");
    const user = userEvent.setup();
    await confirmRemoval(user);
    const prompt = await screen.findByRole("region", { name: "本人確認が必要な操作です" });
    expect(within(prompt).getByRole("button", { name: "Passkey で確認" })).toBeInTheDocument();
    expect(within(prompt).queryByLabelText("パスワード")).not.toBeInTheDocument();
    await user.click(within(prompt).getByRole("button", { name: "キャンセル" }));
    expect(
      screen.queryByRole("region", { name: "本人確認が必要な操作です" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  // The Backend answers `step_up_required` when there is no step-up at all, even
  // for an action that needs a passkey one; after a password step-up the retry
  // says `step_up_method_insufficient`. The prompt must come back, passkey-only.
  it("asks again for a passkey when the password step-up was not enough", async () => {
    fakeWebAuthn();
    const { calls } = mockApi({
      ...devicesPage,
      "GET /auth/session": reply(200, session({ enrolled: true })),
      "GET /auth/passkeys": [reply(200, { passkeys: [passkey] }), reply(200, { passkeys: [] })],
      "DELETE /auth/passkeys/pk-1": [
        apiError(403, "step_up_required"),
        apiError(403, "step_up_method_insufficient"),
        reply(200, { revoked: true, sessions_ended: 0, signed_out: false }),
      ],
      "POST /auth/step-up": reply(200, session({ enrolled: true })),
      "POST /auth/passkeys/authenticate/begin": reply(200, { options: { challenge: "AQ" } }),
      "POST /auth/passkeys/authenticate/finish": reply(200, session({ enrolled: true })),
    });
    renderApp("/settings/devices");
    const user = userEvent.setup();
    await confirmRemoval(user);
    let prompt = await screen.findByRole("region", { name: "本人確認が必要な操作です" });
    await user.type(within(prompt).getByLabelText("パスワード"), "correct horse");
    await user.click(within(prompt).getByRole("button", { name: "パスワードで確認" }));

    prompt = await screen.findByRole("region", { name: "本人確認が必要な操作です" });
    expect(await within(prompt).findByText(/Passkey での本人確認が必要です/)).toBeInTheDocument();
    expect(within(prompt).queryByLabelText("パスワード")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    await user.click(within(prompt).getByRole("button", { name: "Passkey で確認" }));

    expect(await screen.findByText("Passkey を削除しました。")).toBeInTheDocument();
    expect(calls.filter((call) => call.path === "/auth/passkeys/pk-1")).toHaveLength(3);
  });

  it("registers a passkey on this device from the enrolment panel", async () => {
    fakeWebAuthn();
    const { calls } = mockApi({
      ...devicesPage,
      // The finish answer has no replacement session: the state is read again.
      "GET /auth/session": [reply(200, session()), reply(200, session({ enrolled: true }))],
      "GET /auth/passkeys": [reply(200, { passkeys: [] }), reply(200, { passkeys: [passkey] })],
      "POST /auth/passkeys/enroll/begin": reply(200, {
        options: {
          challenge: "AQ",
          rp: { name: "PAW" },
          user: { id: "AQ", name: "t", displayName: "t" },
          pubKeyCredParams: [],
        },
      }),
      "POST /auth/passkeys/enroll/finish": reply(200, { passkey, session: null }),
    });
    renderApp("/settings/devices");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "この端末に追加" }));
    expect(
      screen.getByRole("heading", { name: "この端末に Passkey を登録しますか" }),
    ).toBeVisible();
    await user.type(screen.getByLabelText("端末名"), "MacBook");
    await user.click(screen.getByRole("button", { name: "登録する" }));
    expect(await screen.findByText("Passkey を登録しました。")).toBeInTheDocument();
    expect(screen.queryByText("Passkey の登録をおすすめします。")).not.toBeInTheDocument();
    expect(calls.filter((call) => call.path === "/auth/session")).toHaveLength(2);
    expect(calls.find((call) => call.path === "/auth/passkeys/enroll/finish")?.body).toEqual({
      credential: { id: "cred" },
      name: "MacBook",
    });
  });

  it("ignores a read of the passkeys that was in flight while one was registered", async () => {
    fakeWebAuthn();
    mockApi({
      ...devicesPage,
      "GET /auth/session": [reply(200, session()), reply(200, session({ enrolled: true }))],
      "POST /auth/passkeys/enroll/begin": reply(200, {
        options: {
          challenge: "AQ",
          rp: { name: "PAW" },
          user: { id: "AQ", name: "t", displayName: "t" },
          pubKeyCredParams: [],
        },
      }),
      "POST /auth/passkeys/enroll/finish": reply(200, { passkey, session: null }),
    });
    const tableFetch = globalThis.fetch;
    let reads = 0;
    let answerFirst: (response: Response) => void = () => {};
    const json = (body: unknown) =>
      new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        if (String(input).endsWith("/auth/passkeys") && (init?.method ?? "GET") === "GET") {
          reads += 1;
          if (reads === 1) {
            return new Promise<Response>((resolve) => {
              answerFirst = resolve;
            });
          }
          return Promise.resolve(json({ passkeys: [passkey] }));
        }
        return tableFetch(input, init);
      }),
    );
    renderApp("/settings/devices");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "この端末に追加" }));
    await user.type(screen.getByLabelText("端末名"), "MacBook");
    await user.click(screen.getByRole("button", { name: "登録する" }));
    expect(await screen.findByText("Passkey を登録しました。")).toBeInTheDocument();
    await waitFor(() => expect(within(passkeyCard()).getByText("MacBook")).toBeInTheDocument());
    // The first read, started before the registration, answers last.
    answerFirst(json({ passkeys: [] }));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(within(passkeyCard()).getByText("MacBook")).toBeInTheDocument();
  });

  it("reports a browser without passkey support when registering", async () => {
    vi.stubGlobal("PublicKeyCredential", undefined);
    mockApi({
      ...devicesPage,
      "GET /auth/session": reply(200, session()),
      "GET /auth/passkeys": reply(200, { passkeys: [] }),
      "POST /auth/passkeys/enroll/begin": reply(200, { options: { challenge: "AQ" } }),
    });
    renderApp("/settings/devices");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "この端末に追加" }));
    await user.click(screen.getByRole("button", { name: "登録する" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "このブラウザは Passkey に対応していません。",
    );
  });
});
