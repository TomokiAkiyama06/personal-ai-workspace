import { describe, expect, it } from "vitest";
import { ApiError } from "../api/client";
import { en } from "./en";
import { errorMessage } from "./errors";
import { DEFAULT_LOCALE, formatDateTime, translate } from "./index";
import { ja } from "./ja";

describe("i18n", () => {
  it("defaults to Japanese", () => {
    expect(DEFAULT_LOCALE).toBe("ja");
    expect(translate("ja", "login.title")).toBe("ログイン");
  });

  it("has the same keys and placeholders in every catalog", () => {
    expect(Object.keys(en).sort()).toEqual(Object.keys(ja).sort());
    const placeholders = (text: string) => (text.match(/\{\w+\}/g) ?? []).sort();
    for (const key of Object.keys(ja) as (keyof typeof ja)[]) {
      expect(placeholders(en[key]), key).toEqual(placeholders(ja[key]));
    }
  });

  it("replaces placeholders and leaves unknown ones", () => {
    expect(translate("ja", "devices.revokedOthers", { count: 3 })).toBe(
      "3 台の端末をログアウトさせました。",
    );
    expect(translate("en", "devices.revokedOthers", {})).toContain("{count}");
  });

  it("formats dates in the locale and keeps an invalid value", () => {
    expect(formatDateTime("ja", "2026-09-28T01:00:00Z")).toMatch(/2026/);
    expect(formatDateTime("ja", "not a date")).toBe("not a date");
  });
});

describe("errorMessage", () => {
  const t = (key: keyof typeof ja, params?: Record<string, string | number>) =>
    translate("ja", key, params);

  it("maps a Backend error code to its message", () => {
    expect(errorMessage(t, new ApiError(401, "invalid_credentials", "x"))).toBe(
      ja["error.invalid_credentials"],
    );
  });

  it("says how long to wait when the server does", () => {
    const error = new ApiError(429, "rate_limited", "x", { retryAfterSeconds: 30 });
    expect(errorMessage(t, error)).toContain("30 秒");
  });

  it("names an unknown code instead of hiding it", () => {
    expect(errorMessage(t, new ApiError(409, "something_new", "x"))).toContain("something_new");
    expect(errorMessage(t, new Error("boom"))).toContain("client");
  });
});
