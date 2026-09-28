// The only way the Web App talks to the Backend: same-origin JSON requests under
// /api/v1 with the session cookie (HttpOnly; never readable here). The Backend
// decides every permission; an error is shown, never worked around.

export const API_BASE = "/api/v1";

/** A failed request: the Backend's stable `error.code` (or a local one). */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly retryAfterSeconds: number | null;
  readonly requestId: string | null;

  constructor(
    status: number,
    code: string,
    message: string,
    options: { retryAfterSeconds?: number | null; requestId?: string | null } = {},
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.retryAfterSeconds = options.retryAfterSeconds ?? null;
    this.requestId = options.requestId ?? null;
  }
}

export function isApiError(value: unknown, ...codes: string[]): value is ApiError {
  return value instanceof ApiError && (codes.length === 0 || codes.includes(value.code));
}

const FALLBACK_CODES: Record<number, string> = {
  400: "bad_request",
  401: "unauthorized",
  403: "forbidden",
  404: "not_found",
  409: "conflict",
  422: "validation_error",
  429: "rate_limited",
  503: "service_unavailable",
};

function retryAfter(response: Response): number | null {
  const value = response.headers.get("Retry-After");
  if (value === null || !/^\d+$/.test(value)) return null;
  return Number(value);
}

async function errorFrom(response: Response): Promise<ApiError> {
  let code = FALLBACK_CODES[response.status] ?? "http_error";
  let message = response.statusText || code;
  let requestId: string | null = null;
  try {
    const body: unknown = await response.json();
    const error = (body as { error?: { code?: unknown; message?: unknown; request_id?: unknown } })
      ?.error;
    if (typeof error?.code === "string") code = error.code;
    if (typeof error?.message === "string") message = error.message;
    if (typeof error?.request_id === "string") requestId = error.request_id;
  } catch {
    // Not JSON (a proxy's error page): the status decides.
  }
  return new ApiError(response.status, code, message, {
    retryAfterSeconds: retryAfter(response),
    requestId,
  });
}

/**
 * Dispatched on `window` when a request is answered 401 `unauthorized`: the
 * session ended (expired, signed out elsewhere, revoked). A wrong password is
 * `invalid_credentials`, not this.
 */
export const SESSION_ENDED_EVENT = "paw:session-ended";

export type Method = "GET" | "POST" | "PUT" | "DELETE";

/** Send one request; resolve with the JSON body (`undefined` for 204). */
export async function apiRequest<T>(method: Method, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const init: RequestInit = {
    method,
    headers,
    credentials: "same-origin",
    cache: "no-store",
    redirect: "error",
  };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, init);
  } catch {
    throw new ApiError(0, "network_error", "Network error");
  }
  if (!response.ok) {
    const error = await errorFrom(response);
    if (error.status === 401 && error.code === "unauthorized") {
      window.dispatchEvent(new Event(SESSION_ENDED_EVENT));
    }
    throw error;
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}
