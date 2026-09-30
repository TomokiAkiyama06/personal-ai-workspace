// Notification Center state (docs/UI_DESIGN.md §10, docs/NOTIFICATION_POLICY.md):
// normal notifications gather under the bell, the same kind of notification is
// grouped, and ERROR / CRITICAL ones also show as a non-modal banner.
//
// Noise reduction (NOTIFICATION_POLICY §4): an event that arrives again with the
// same `id` (a re-poll, a reconnect) is not counted twice (dedup); different
// events with the same `key` become one entry with a count and the first / latest
// time (aggregation); the newest one sets the severity, so a repeated failure can
// escalate the entry. A source resolves a key when its condition is over.
//
// The Backend has no notification API yet (no store of notifications, read state
// or authorized event stream): notifications live in this browser tab only. A
// source (the future event stream) is plugged in through `NotificationSource`;
// watchers of existing APIs (pendingApprovals.ts) push through `useNotifications`.
import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

export type Severity = "info" | "warning" | "error" | "critical";
export const SEVERITIES: readonly Severity[] = ["info", "warning", "error", "critical"];

export interface IncomingNotification {
  /** Notifications with the same key are one entry with a count. */
  key: string;
  severity: Severity;
  title: string;
  body?: string;
  /** Where it comes from, shown next to the severity ("Backup", "Project · Task #203"). */
  source?: string;
  /** "task" notifications are listed by the タスク filter. */
  category?: "task" | "system";
  at: string;
  /** The event's identity: the same id again is a duplicate, not a new event. */
  id?: string;
  /** A short monospaced line under the message ("最終成功 07:30 · 連続失敗 12 回"). */
  detail?: string;
  /**
   * Where to act on it (NOTIFICATION_POLICY §6). Actions only NAVIGATE: the
   * operation itself (approve, retry, ...) runs on the target screen, under that
   * screen's Permission / Step-up policy. A notification never sends a
   * state-changing request by itself.
   */
  actions?: readonly NotificationAction[];
}

export interface NotificationAction {
  label: string;
  /** An in-app path. */
  to: string;
  primary?: boolean;
}

export type NotificationFilter = "all" | "unread" | "important" | "task";
export const NOTIFICATION_FILTERS: readonly NotificationFilter[] = [
  "all",
  "unread",
  "important",
  "task",
];

/** The design's filters: 未読, 重要 (ERROR / CRITICAL) and タスク. */
export function matchesFilter(item: NotificationItem, filter: NotificationFilter): boolean {
  switch (filter) {
    case "all":
      return true;
    case "unread":
      return !item.read;
    case "important":
      return item.severity === "error" || item.severity === "critical";
    case "task":
      return item.category === "task";
  }
}

export interface NotificationItem extends IncomingNotification {
  count: number;
  /** When the first event of the entry arrived (`at` is the latest). */
  firstAt: string;
  /** The ids already counted (the latest ones), for dedup. */
  ids: string[];
  read: boolean;
  dismissed: boolean;
}

export interface NotificationSource {
  subscribe(
    onNotification: (notification: IncomingNotification) => void,
    onResolve?: (key: string) => void,
  ): () => void;
}

interface NotificationValue {
  items: NotificationItem[];
  unread: number;
  push: (notification: IncomingNotification) => void;
  /** The condition is over (approved elsewhere, recovered): the entry goes away. */
  resolve: (key: string) => void;
  markRead: (key: string) => void;
  markAllRead: () => void;
  dismiss: (key: string) => void;
  /** Forget everything (another account signed in on this browser). */
  clear: () => void;
}

const NotificationContext = createContext<NotificationValue | null>(null);
const MAX_ITEMS = 100;
const MAX_IDS = 50;

function earlier(a: string, b: string): string {
  return Date.parse(b) < Date.parse(a) ? b : a;
}

function later(a: string, b: string): string {
  return Date.parse(b) > Date.parse(a) ? b : a;
}

export function mergeNotification(
  items: NotificationItem[],
  incoming: IncomingNotification,
): NotificationItem[] {
  const existing = items.find((item) => item.key === incoming.key);
  // Dedup: the same event again changes nothing (it stays read if it was read).
  if (existing && incoming.id !== undefined && existing.ids.includes(incoming.id)) return items;
  const ids = incoming.id === undefined ? [] : [incoming.id];
  const merged: NotificationItem = existing
    ? {
        ...incoming,
        count: existing.count + 1,
        at: later(existing.at, incoming.at),
        firstAt: earlier(existing.firstAt, incoming.at),
        ids: [...ids, ...existing.ids].slice(0, MAX_IDS),
        read: false,
        dismissed: false,
      }
    : { ...incoming, count: 1, firstAt: incoming.at, ids, read: false, dismissed: false };
  const rest = items.filter((item) => item.key !== incoming.key);
  return [merged, ...rest].slice(0, MAX_ITEMS);
}

export function NotificationProvider({
  source,
  children,
}: {
  source?: NotificationSource;
  children: ReactNode;
}) {
  const [items, setItems] = useState<NotificationItem[]>([]);
  const push = useCallback((incoming: IncomingNotification) => {
    setItems((current) => mergeNotification(current, incoming));
  }, []);
  const resolve = useCallback((key: string) => {
    setItems((current) =>
      current.some((item) => item.key === key)
        ? current.filter((item) => item.key !== key)
        : current,
    );
  }, []);
  const markRead = useCallback((key: string) => {
    setItems((current) =>
      current.map((item) => (item.key === key && !item.read ? { ...item, read: true } : item)),
    );
  }, []);
  const markAllRead = useCallback(() => {
    setItems((current) => current.map((item) => ({ ...item, read: true })));
  }, []);
  const dismiss = useCallback((key: string) => {
    setItems((current) =>
      current.map((item) => (item.key === key ? { ...item, dismissed: true, read: true } : item)),
    );
  }, []);
  const clear = useCallback(() => {
    setItems((current) => (current.length === 0 ? current : []));
  }, []);
  useEffect(() => source?.subscribe(push, resolve), [source, push, resolve]);
  const value = useMemo(
    () => ({
      items,
      unread: items.filter((item) => !item.read).length,
      push,
      resolve,
      markRead,
      markAllRead,
      dismiss,
      clear,
    }),
    [items, push, resolve, markRead, markAllRead, dismiss, clear],
  );
  return <NotificationContext.Provider value={value}>{children}</NotificationContext.Provider>;
}

/**
 * Notifications belong to the signed-in account: they are forgotten when it signs
 * out or another account signs in, so one account never sees another's.
 */
export function useNotificationOwner(userId: string | null): void {
  const { clear } = useNotifications();
  const owner = useRef(userId);
  useEffect(() => {
    if (owner.current === userId) return;
    owner.current = userId;
    clear();
  }, [userId, clear]);
}

export function useNotifications(): NotificationValue {
  const value = useContext(NotificationContext);
  if (!value) throw new Error("useNotifications outside NotificationProvider");
  return value;
}
