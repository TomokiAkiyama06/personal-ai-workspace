import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { apiError, mockApi, renderApp, reply, session } from "../test/helpers";

afterEach(() => {
  vi.unstubAllGlobals();
});

const passkey = {
  id: "pk-1",
  name: "MacBook",
  created_at: "2026-09-01T00:00:00Z",
  last_used_at: null,
  backup_eligible: true,
  backed_up: true,
};

describe("Passkeys", () => {
  it("lists the passkeys", async () => {
    mockApi({
      "GET /auth/session": reply(200, session({ enrolled: true })),
      "GET /auth/passkeys": reply(200, { passkeys: [passkey] }),
    });
    renderApp("/settings/security");
    expect(await screen.findByText("MacBook")).toBeInTheDocument();
    expect(screen.getByText("同期パスキー")).toBeInTheDocument();
    expect(screen.getByText(/未使用/)).toBeInTheDocument();
  });

  it("removes a passkey after an inline confirmation", async () => {
    const { calls } = mockApi({
      "GET /auth/session": reply(200, session({ enrolled: true })),
      "GET /auth/passkeys": [reply(200, { passkeys: [passkey] }), reply(200, { passkeys: [] })],
      "DELETE /auth/passkeys/pk-1": reply(200, {
        revoked: true,
        sessions_ended: 0,
        signed_out: false,
      }),
    });
    renderApp("/settings/security");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "削除" }));
    expect(screen.getByText("パスキー「MacBook」を削除しますか？")).toBeInTheDocument();
    const item = screen.getByText("MacBook").closest("li") as HTMLElement;
    await user.click(within(item).getByRole("button", { name: "削除" }));
    expect(await screen.findByText("パスキーを削除しました。")).toBeInTheDocument();
    expect(await screen.findByText("登録済みのパスキーはありません。")).toBeInTheDocument();
    expect(
      calls.some((call) => call.method === "DELETE" && call.path === "/auth/passkeys/pk-1"),
    ).toBe(true);
  });

  it("returns to the login page when removing the passkey ended this session", async () => {
    mockApi({
      "GET /auth/session": reply(200, session({ enrolled: true })),
      "GET /auth/passkeys": reply(200, { passkeys: [passkey] }),
      "DELETE /auth/passkeys/pk-1": reply(200, {
        revoked: true,
        sessions_ended: 1,
        signed_out: true,
      }),
    });
    renderApp("/settings/security");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "削除" }));
    const item = screen.getByText("MacBook").closest("li") as HTMLElement;
    await user.click(within(item).getByRole("button", { name: "削除" }));
    expect(await screen.findByRole("heading", { name: "ログイン" })).toBeInTheDocument();
  });

  it("asks for a passkey step-up when a password one is not enough, and can be cancelled", async () => {
    mockApi({
      "GET /auth/session": reply(200, session({ enrolled: true })),
      "GET /auth/passkeys": reply(200, { passkeys: [passkey] }),
      "DELETE /auth/passkeys/pk-1": apiError(403, "step_up_method_insufficient"),
    });
    renderApp("/settings/security");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "削除" }));
    const item = screen.getByText("MacBook").closest("li") as HTMLElement;
    await user.click(within(item).getByRole("button", { name: "削除" }));
    const prompt = await screen.findByRole("region", { name: "本人確認" });
    expect(within(prompt).getByRole("button", { name: "パスキーで確認" })).toBeInTheDocument();
    expect(within(prompt).queryByLabelText("パスワード")).not.toBeInTheDocument();
    await user.click(within(prompt).getByRole("button", { name: "キャンセル" }));
    expect(screen.queryByRole("region", { name: "本人確認" })).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("reports a browser without passkey support when registering", async () => {
    vi.stubGlobal("PublicKeyCredential", undefined);
    mockApi({
      "GET /auth/session": reply(200, session()),
      "GET /auth/passkeys": reply(200, { passkeys: [] }),
      "POST /auth/passkeys/enroll/begin": reply(200, { options: { challenge: "AQ" } }),
    });
    renderApp("/settings/security");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "パスキーを登録" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "このブラウザはパスキーに対応していません。",
    );
  });
});
