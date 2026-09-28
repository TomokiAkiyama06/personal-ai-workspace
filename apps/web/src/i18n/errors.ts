import { ApiError } from "../api/client";
import { ja, type MessageKey } from "./ja";

type Translate = (key: MessageKey, params?: Record<string, string | number>) => string;

function isMessageKey(key: string): key is MessageKey {
  return key in ja;
}

/** A user-facing message for any error thrown by an API call or a passkey ceremony. */
export function errorMessage(t: Translate, error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === "rate_limited" && error.retryAfterSeconds !== null) {
      return t("error.rate_limited_seconds", { seconds: error.retryAfterSeconds });
    }
    const key = `error.${error.code}`;
    if (isMessageKey(key)) return t(key);
    return t("error.unknown", { code: error.code });
  }
  return t("error.unknown", { code: "client" });
}
