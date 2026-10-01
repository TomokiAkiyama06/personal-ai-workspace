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
// The Backend's stored notifications (GET /api/v1/notifications, issue #188) are
// pushed by serverNotifications.ts with their read state (`read`, `remote`); marking
// such an entry read here also marks it on the Backend (`NotificationRemote`), so
// the state follows the account across devices. Watchers of other APIs
// (pendingApprovals.ts) push tab-local notifications through `useNotifications`;
// a source can also be plugged in through `NotificationSource`.
import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
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
  /** The Backend's read state of this event (a stored notification). */
  read?: boolean;
  /** A stored notification: reading it is sent to the Backend. */
  remote?: boolean;
}

/**
 * Sends the read state of stored notifications to the Backend. Each resolves
 * with the account's unread total after the change; a rejection puts the entry
 * back to unread (the Backend still has it unread).
 */
export interface NotificationRemote {
  markRead: (ids: string[]) => Promise<number>;
  markAllRead: () => Promise<number>;
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
  /** Every id already counted, for dedup. */
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
  /** Resolve every entry whose key passes `test`. */
  resolveMatching: (test: (key: string) => boolean) => void;
  markRead: (key: string) => void;
  markAllRead: () => void;
  dismiss: (key: string) => void;
  /** Forget everything (another account signed in on this browser). */
  clear: () => void;
  /** Where marking stored notifications read is sent (`null`: nowhere). */
  setRemote: (remote: NotificationRemote | null) => void;
  /**
   * The Backend's unread total of the stored notifications (the list holds the
   * newest only): the badge counts it instead of the stored entries shown.
   */
  setRemoteUnread: (total: number | null) => void;
}

const NotificationContext = createContext<NotificationValue | null>(null);
const MAX_ITEMS = 100;

function earlier(a: string, b: string): string {
  return Date.parse(b) < Date.parse(a) ? b : a;
}

export function mergeNotification(
  items: NotificationItem[],
  incoming: IncomingNotification,
): NotificationItem[] {
  const existing = items.find((item) => item.key === incoming.key);
  // A stored notification that is already read (on this or another device) is
  // read here and raises no banner.
  const read = incoming.read === true;
  // Dedup: the same event again changes nothing (it stays read if it was read),
  // except that the Backend now says the entry's latest event was read.
  if (existing && incoming.id !== undefined && existing.ids.includes(incoming.id)) {
    if (!read || existing.read || existing.ids[0] !== incoming.id) return items;
    return items.map((item) =>
      item === existing ? { ...existing, read: true, dismissed: true } : item,
    );
  }
  const ids = incoming.id === undefined ? [] : [incoming.id];
  if (!existing) {
    const created = { ...incoming, count: 1, firstAt: incoming.at, ids, read };
    return [{ ...created, dismissed: read }, ...items].slice(0, MAX_ITEMS);
  }
  const newer = Date.parse(incoming.at) >= Date.parse(existing.at);
  if (!newer) {
    // A late, older event (a replay) is only counted: the newer content (title,
    // severity, actions), the read / dismissed state and the position stay.
    return items.map((item) =>
      item === existing
        ? {
            ...existing,
            count: existing.count + 1,
            firstAt: earlier(existing.firstAt, incoming.at),
            ids: [...ids, ...existing.ids],
          }
        : item,
    );
  }
  const merged: NotificationItem = {
    ...incoming,
    count: existing.count + 1,
    firstAt: earlier(existing.firstAt, incoming.at),
    ids: [...ids, ...existing.ids],
    read,
    dismissed: read,
  };
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
  // The current items for the handlers below, which send requests (never from
  // inside a state update, which React may run twice).
  const latest = useRef(items);
  latest.current = items;
  const remote = useRef<NotificationRemote | null>(null);
  const setRemote = useCallback((value: NotificationRemote | null) => {
    remote.current = value;
  }, []);
  const [remoteUnread, setRemoteUnread] = useState<number | null>(null);
  // A failed write: the entries are unread again (as on the Backend), and the
  // total is the one before the optimistic change, until the next list.
  const restore = useCallback((keys: Set<string>, total: number | null) => {
    setItems((current) =>
      current.map((item) => (keys.has(item.key) ? { ...item, read: false } : item)),
    );
    setRemoteUnread(total);
  }, []);
  const sendRead = useCallback(
    (key: string) => {
      const item = latest.current.find((entry) => entry.key === key);
      const target = remote.current;
      if (!item?.remote || item.read || item.ids.length === 0 || !target) return;
      const before = remoteUnread;
      setRemoteUnread((total) => (total === null ? null : Math.max(0, total - item.ids.length)));
      target.markRead(item.ids).then(setRemoteUnread, () => restore(new Set([key]), before));
    },
    [remoteUnread, restore],
  );
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
  const resolveMatching = useCallback((test: (key: string) => boolean) => {
    setItems((current) =>
      current.some((item) => test(item.key)) ? current.filter((item) => !test(item.key)) : current,
    );
  }, []);
  const markRead = useCallback(
    (key: string) => {
      sendRead(key);
      setItems((current) =>
        current.map((item) => (item.key === key && !item.read ? { ...item, read: true } : item)),
      );
    },
    [sendRead],
  );
  const markAllRead = useCallback(() => {
    const target = remote.current;
    if (target && (remoteUnread ?? 0) + latest.current.filter((i) => i.remote && !i.read).length) {
      const keys = new Set(
        latest.current.filter((item) => item.remote && !item.read).map((item) => item.key),
      );
      const before = remoteUnread;
      setRemoteUnread(0);
      target.markAllRead().then(setRemoteUnread, () => restore(keys, before));
    }
    setItems((current) => current.map((item) => ({ ...item, read: true })));
  }, [remoteUnread, restore]);
  const dismiss = useCallback(
    (key: string) => {
      // Closing the banner reads the entry (it stays in the list).
      sendRead(key);
      setItems((current) =>
        current.map((item) => (item.key === key ? { ...item, dismissed: true, read: true } : item)),
      );
    },
    [sendRead],
  );
  const clear = useCallback(() => {
    setItems((current) => (current.length === 0 ? current : []));
    setRemoteUnread(null);
  }, []);
  useEffect(() => source?.subscribe(push, resolve), [source, push, resolve]);
  const value = useMemo(
    () => ({
      items,
      unread:
        items.filter((item) => !item.read && !item.remote).length +
        (remoteUnread ?? items.filter((item) => !item.read && item.remote).length),
      push,
      resolve,
      resolveMatching,
      markRead,
      markAllRead,
      dismiss,
      clear,
      setRemote,
      setRemoteUnread,
    }),
    [
      items,
      remoteUnread,
      push,
      resolve,
      resolveMatching,
      markRead,
      markAllRead,
      dismiss,
      clear,
      setRemote,
    ],
  );
  return <NotificationContext.Provider value={value}>{children}</NotificationContext.Provider>;
}

/**
 * Notifications belong to the signed-in account: they are forgotten when it signs
 * out or another account signs in, so one account never sees another's.
 * `userId` is the account, `null` when signed out, `undefined` when unknown.
 */
export function useNotificationOwner(userId: string | null | undefined): void {
  const { clear } = useNotifications();
  const owner = useRef(userId);
  // A layout effect: cleared before the browser paints the new account's shell,
  // so it never shows the previous account's notifications, even for a frame.
  useLayoutEffect(() => {
    // `undefined`: not known right now (loading, a transient error).
    if (userId === undefined || owner.current === userId) return;
    owner.current = userId;
    clear();
  }, [userId, clear]);
}

export function useNotifications(): NotificationValue {
  const value = useContext(NotificationContext);
  if (!value) throw new Error("useNotifications outside NotificationProvider");
  return value;
}
