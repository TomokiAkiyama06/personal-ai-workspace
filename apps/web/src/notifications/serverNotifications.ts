// The account's stored notifications (GET /api/v1/notifications, issue #188) in
// the Notification Center, kept current by the authenticated event stream
// (/api/v1/events/stream): a `notification.changed` event carries no content, it
// only says "read the list again". The list is also read on every (re)connect of
// the stream, when the tab becomes visible again, and as a slow fallback poll.
//
// Marking an entry read (or closing its banner) and すべて既読 are sent to the
// Backend (POST /notifications/read), so the state follows the account across
// devices. A notification is codes and numbers; it is worded here (the i18n
// catalog). Its actions only NAVIGATE (NOTIFICATION_POLICY §6).
import { useEffect } from "react";
import { EVENT_STREAM_URL, notificationsApi, type StoredNotification } from "../api/notifications";
import { componentName, reasonText, severityLabel, statusText } from "../health/text";
import { type MessageKey, useI18n } from "../i18n";
import { type IncomingNotification, useNotifications } from "./store";

export const SERVER_KEY_PREFIX = "server:";
/** The fallback poll while the stream is not connected (and the tab is visible). */
export const SERVER_POLL_MS = 60_000;
/** How long to wait before opening the stream again once it was closed. */
export const STREAM_RETRY_MS = 30_000;

type Translate = (key: MessageKey, params?: Record<string, string | number>) => string;

function text(value: unknown): string {
  return typeof value === "string" ? value : value == null ? "-" : String(value);
}

/** A stored notification as the Notification Center shows it. */
export function toIncoming(item: StoredNotification, t: Translate): IncomingNotification {
  const base = {
    key: `${SERVER_KEY_PREFIX}${item.key}`,
    id: item.id,
    severity: item.severity,
    category: item.category,
    at: item.created_at,
    read: item.read,
    remote: true,
  } as const;
  if (item.kind === "system_health.component_changed") {
    const name = componentName(t, text(item.params.component));
    const reasons = Array.isArray(item.params.reasons) ? item.params.reasons : [];
    return {
      ...base,
      title: t(
        item.severity === "info"
          ? "notifications.health.recovered"
          : "notifications.health.problem",
        { component: name },
      ),
      body: t("notifications.health.body", {
        status: statusText(t, text(item.params.status)),
        previous: severityLabel(t, text(item.params.previous_severity)),
      }),
      detail:
        reasons.length > 0 ? reasons.map((reason) => reasonText(t, reason)).join(" · ") : undefined,
      source: t("notifications.health.source"),
      // 管理 › サーバー監視 (PAW-067) opens the details of the abnormal component.
      actions: [{ label: t("notifications.health.open"), to: "/admin/monitoring", primary: true }],
    };
  }
  // A kind this version does not know yet: still listed, with its code.
  return {
    ...base,
    title: t("notifications.generic.title", { kind: item.kind }),
    source: t("notifications.generic.source"),
  };
}

/** Mounted once for every screen of a signed-in session past the Passkey gate. */
export function ServerNotifications(): null {
  useServerNotifications();
  return null;
}

export function useServerNotifications(): void {
  const { t } = useI18n();
  const { push, resolveMatching, setRemote, setRemoteUnread } = useNotifications();

  useEffect(() => {
    setRemote({
      markRead: async (ids) => (await notificationsApi.markRead(ids)).unread,
      markAllRead: async () => (await notificationsApi.markAllRead()).unread,
    });
    return () => setRemote(null);
  }, [setRemote]);

  useEffect(() => {
    let cancelled = false;
    let inFlight = false;
    let again = false;
    let stream: EventSource | null = null;
    let retry: number | undefined;

    const load = async () => {
      if (inFlight) {
        again = true; // a change arrived while reading: read once more after
        return;
      }
      inFlight = true;
      try {
        const { notifications, unread } = await notificationsApi.list();
        if (cancelled) return;
        setRemoteUnread(unread);
        // Oldest first, so that the entries count and order as they arrived.
        for (const item of [...notifications].reverse()) push(toIncoming(item, t));
        const current = new Set(notifications.map((item) => `${SERVER_KEY_PREFIX}${item.key}`));
        // Dismissed, resolved or no longer this account's (a role change).
        resolveMatching((key) => key.startsWith(SERVER_KEY_PREFIX) && !current.has(key));
      } catch {
        // Only a hint: a failed read keeps what is shown until the next one.
      } finally {
        inFlight = false;
        if (again && !cancelled) {
          again = false;
          void load();
        }
      }
    };

    const connect = () => {
      if (cancelled || typeof EventSource === "undefined") return;
      const source = new EventSource(EVENT_STREAM_URL);
      stream = source;
      source.addEventListener("open", () => void load());
      source.addEventListener("notification.changed", () => void load());
      source.addEventListener("error", () => {
        // The browser reconnects on its own unless the answer was an error (the
        // session ended: 401); then try again later.
        if (source.readyState === EventSource.CLOSED && !cancelled) {
          stream = null;
          retry = window.setTimeout(connect, STREAM_RETRY_MS);
        }
      });
    };

    void load();
    connect();
    const timer = window.setInterval(() => {
      if (document.visibilityState !== "hidden" && stream?.readyState !== EventSource?.OPEN)
        void load();
    }, SERVER_POLL_MS);
    const onVisible = () => {
      if (document.visibilityState === "visible") void load();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      cancelled = true;
      stream?.close();
      window.clearTimeout(retry);
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [t, push, resolveMatching, setRemoteUnread]);
}
