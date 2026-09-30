import { render } from "@testing-library/react";
import type { ReactNode } from "react";
import { vi } from "vitest";
import { App } from "../App";
import type { SessionResponse } from "../api/auth";
import { SessionProvider } from "../auth/session";
import { I18nProvider, type Locale } from "../i18n";
import { NotificationProvider, type NotificationSource } from "../notifications/store";
import { RouterProvider } from "../router";
import { ThemeProvider } from "../theme";

export interface Reply {
  status: number;
  body?: unknown;
  headers?: Record<string, string>;
}

export type Route = Reply | ((body: unknown) => Reply);

export interface Call {
  method: string;
  path: string;
  body: unknown;
  init: RequestInit;
}

export function reply(status: number, body?: unknown, headers?: Record<string, string>): Reply {
  return { status, body, headers };
}

export function apiError(status: number, code: string, headers?: Record<string, string>): Reply {
  return reply(status, { error: { code, message: code, request_id: "req-1" } }, headers);
}

/** Replace `fetch` with a table of `"METHOD /path"` (below /api/v1) answers. */
export function mockApi(routes: Record<string, Route | Route[]>) {
  const calls: Call[] = [];
  const queues = new Map<string, Route[]>();
  for (const [key, value] of Object.entries(routes)) {
    queues.set(key, Array.isArray(value) ? [...value] : [value]);
  }
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = new URL(String(input), "http://localhost");
    const method = (init.method ?? "GET").toUpperCase();
    const path = url.pathname.replace(/^\/api\/v1/, "");
    const body = typeof init.body === "string" ? JSON.parse(init.body) : undefined;
    calls.push({ method, path, body, init });
    const queue = queues.get(`${method} ${path}`);
    if (!queue || queue.length === 0) {
      return new Response(JSON.stringify({ error: { code: "not_found", message: "x" } }), {
        status: 404,
        headers: { "Content-Type": "application/json" },
      });
    }
    // The last answer repeats.
    const route = queue.length > 1 ? (queue.shift() as Route) : (queue[0] as Route);
    const answer = typeof route === "function" ? route(body) : route;
    const headers = { "Content-Type": "application/json", ...answer.headers };
    return new Response(
      answer.status === 204 || answer.body === undefined ? null : JSON.stringify(answer.body),
      { status: answer.status, headers },
    );
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls, fetchMock };
}

export function session(
  overrides: {
    role?: string;
    gate?: string;
    enrolled?: boolean;
    available?: boolean;
    auth?: null;
  } = {},
): SessionResponse {
  return {
    user: { id: "u-1", login_name: "tomoki", system_role: overrides.role ?? "user" },
    session: {
      id: "s-1",
      created_at: "2026-09-28T01:00:00Z",
      last_used_at: "2026-09-28T02:00:00Z",
      expires_at: "2026-10-28T02:00:00Z",
      absolute_expires_at: "2026-12-27T01:00:00Z",
      remember_me: false,
      device_name: "Laptop",
      current: true,
    },
    auth:
      overrides.auth === null
        ? null
        : {
            method: "password",
            passkey: {
              requirement: "optional",
              enrolled: overrides.enrolled ?? false,
              enrollment_required: overrides.gate === "enrollment_required",
              recommended: true,
              available: overrides.available ?? true,
              gate: overrides.gate ?? "open",
              next: null,
            },
            step_up: {
              method: null,
              verified_at: null,
              valid_until: null,
              window_minutes: 10,
              satisfied: false,
            },
          },
  };
}

export function Providers({
  children,
  locale,
  source,
}: {
  children: ReactNode;
  locale?: Locale;
  source?: NotificationSource;
}) {
  return (
    <I18nProvider initialLocale={locale}>
      <ThemeProvider>
        <RouterProvider>
          <SessionProvider>
            <NotificationProvider source={source}>{children}</NotificationProvider>
          </SessionProvider>
        </RouterProvider>
      </ThemeProvider>
    </I18nProvider>
  );
}

export function renderApp(
  path = "/",
  options: { locale?: Locale; source?: NotificationSource } = {},
) {
  window.history.replaceState(null, "", path);
  return render(
    <Providers locale={options.locale} source={options.source}>
      <App />
    </Providers>,
  );
}
