import { afterEach, describe, expect, it, vi } from "vitest";
import { apiError, mockApi, reply } from "../test/helpers";
import { authApi } from "./auth";
import { type ApiError, apiRequest, isApiError } from "./client";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("apiRequest", () => {
  it("sends same-origin JSON with the cookie and never follows redirects", async () => {
    const { calls } = mockApi({ "POST /auth/login": reply(200, { ok: true }) });
    await apiRequest("POST", "/auth/login", { login_name: "a" });
    const call = calls[0];
    if (!call) throw new Error("no request");
    expect(call.init.credentials).toBe("same-origin");
    expect(call.init.redirect).toBe("error");
    expect(call.init.cache).toBe("no-store");
    expect((call.init.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
    expect(call.body).toEqual({ login_name: "a" });
  });

  it("resolves 204 without a body", async () => {
    mockApi({ "POST /auth/logout": reply(204) });
    await expect(authApi.logout()).resolves.toBeUndefined();
  });

  it("turns the Backend's error body into an ApiError", async () => {
    mockApi({ "POST /auth/login": apiError(429, "rate_limited", { "Retry-After": "45" }) });
    const error = await authApi.login({ login_name: "a", password: "b", remember_me: false }).then(
      () => null,
      (caught: unknown) => caught,
    );
    expect(isApiError(error, "rate_limited")).toBe(true);
    expect((error as ApiError).status).toBe(429);
    expect((error as ApiError).retryAfterSeconds).toBe(45);
    expect((error as ApiError).requestId).toBe("req-1");
  });

  it("falls back to the status when the body is not the Backend's", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("<html>bad gateway</html>", { status: 503 })),
    );
    await expect(apiRequest("GET", "/auth/session")).rejects.toMatchObject({
      code: "service_unavailable",
      status: 503,
    });
  });

  it("reports a network failure as network_error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );
    await expect(apiRequest("GET", "/auth/session")).rejects.toMatchObject({
      code: "network_error",
      status: 0,
    });
  });

  it("encodes path parameters", async () => {
    const { calls } = mockApi({});
    await authApi.revokeSession("a/b").catch(() => undefined);
    expect(calls[0]?.path).toBe("/auth/sessions/a%2Fb");
  });
});
