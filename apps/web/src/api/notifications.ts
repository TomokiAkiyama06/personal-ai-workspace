// /api/v1/notifications (issue #188): the signed-in user's stored notifications.
// The Backend decides what the user receives (their own, and their role's
// audiences); a notification holds codes and numbers, worded by the Web App.
import { apiRequest } from "./client";

export type NotificationParam = string | number | boolean | null | string[];

export interface StoredNotification {
  id: string;
  /** Notifications with the same key are one entry of the Notification Center. */
  key: string;
  kind: string;
  severity: "info" | "warning" | "error" | "critical";
  category: "task" | "system";
  project_id: string | null;
  params: Record<string, NotificationParam>;
  created_at: string;
  read: boolean;
}

export interface NotificationList {
  notifications: StoredNotification[];
  unread: number;
}

export const notificationsApi = {
  list: () => apiRequest<NotificationList>("GET", "/notifications"),
  markRead: (ids: string[]) =>
    apiRequest<{ updated: number; unread: number }>("POST", "/notifications/read", { ids }),
  markAllRead: () =>
    apiRequest<{ updated: number; unread: number }>("POST", "/notifications/read", {
      all: true,
    }),
  dismiss: (id: string) =>
    apiRequest<void>("POST", `/notifications/${encodeURIComponent(id)}/dismiss`),
};

/** The authenticated event stream (Server-Sent Events, same origin, the cookie). */
export const EVENT_STREAM_URL = "/api/v1/events/stream";
