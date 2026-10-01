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
}

const NotificationContext = createContext<NotificationValue | null>(null);
const MAX_ITEMS = 100;
// The ids of the entries the MAX_ITEMS limit pushed out, still deduplicated (a
// replay of their events is not new). Bounded too: the oldest are forgotten.
const MAX_EVICTED_IDS = 10_000;

interface NotificationState {
  items: NotificationItem[];
  /** Ids of entries dropped by the MAX_ITEMS limit (not of resolved ones). */
  evicted: ReadonlySet<string>;
}

const EMPTY: NotificationState = { items: [], evicted: new Set() };

/**
 * `mergeNotification` that also deduplicates the events of entries the 100-entry
 * limit pushed out (Codex review #173): the list is capped, the ids seen are not
 * forgotten with it. A resolved entry's ids are forgotten (its condition may
 * come back).
 */
function pushNotification(
  state: NotificationState,
  incoming: IncomingNotification,
): NotificationState {
  if (incoming.id !== undefined && state.evicted.has(incoming.id)) return state;
  const items = mergeNotification(state.items, incoming);
  if (items === state.items) return state;
  const kept = new Set(items.map((item) => item.key));
  const dropped = state.items.filter((item) => !kept.has(item.key)).flatMap((item) => item.ids);
  if (dropped.length === 0) return { ...state, items };
  const evicted = [...state.evicted, ...dropped];
  return { items, evicted: new Set(evicted.slice(-MAX_EVICTED_IDS)) };
}

function earlier(a: string, b: string): string {
  return Date.parse(b) < Date.parse(a) ? b : a;
}

export function mergeNotification(
  items: NotificationItem[],
  incoming: IncomingNotification,
): NotificationItem[] {
  const existing = items.find((item) => item.key === incoming.key);
  // Dedup: the same event again changes nothing (it stays read if it was read).
  if (existing && incoming.id !== undefined && existing.ids.includes(incoming.id)) return items;
  const ids = incoming.id === undefined ? [] : [incoming.id];
  if (!existing) {
    const created = { ...incoming, count: 1, firstAt: incoming.at, ids, read: false };
    return [{ ...created, dismissed: false }, ...items].slice(0, MAX_ITEMS);
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
    read: false,
    dismissed: false,
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
  const [state, setState] = useState<NotificationState>(EMPTY);
  const items = state.items;
  // Every change but a push and a clear is to the list only.
  const setItems = useCallback((change: (current: NotificationItem[]) => NotificationItem[]) => {
    setState((current) => {
      const next = change(current.items);
      return next === current.items ? current : { ...current, items: next };
    });
  }, []);
  const push = useCallback((incoming: IncomingNotification) => {
    setState((current) => pushNotification(current, incoming));
  }, []);
  const resolve = useCallback(
    (key: string) => {
      setItems((current) =>
        current.some((item) => item.key === key)
          ? current.filter((item) => item.key !== key)
          : current,
      );
    },
    [setItems],
  );
  const resolveMatching = useCallback(
    (test: (key: string) => boolean) => {
      setItems((current) =>
        current.some((item) => test(item.key))
          ? current.filter((item) => !test(item.key))
          : current,
      );
    },
    [setItems],
  );
  const markRead = useCallback(
    (key: string) => {
      setItems((current) =>
        current.map((item) => (item.key === key && !item.read ? { ...item, read: true } : item)),
      );
    },
    [setItems],
  );
  const markAllRead = useCallback(() => {
    setItems((current) => current.map((item) => ({ ...item, read: true })));
  }, [setItems]);
  const dismiss = useCallback(
    (key: string) => {
      setItems((current) =>
        current.map((item) => (item.key === key ? { ...item, dismissed: true, read: true } : item)),
      );
    },
    [setItems],
  );
  const clear = useCallback(() => {
    setState((current) =>
      current.items.length === 0 && current.evicted.size === 0 ? current : EMPTY,
    );
  }, []);
  useEffect(() => source?.subscribe(push, resolve), [source, push, resolve]);
  const value = useMemo(
    () => ({
      items,
      unread: items.filter((item) => !item.read).length,
      push,
      resolve,
      resolveMatching,
      markRead,
      markAllRead,
      dismiss,
      clear,
    }),
    [items, push, resolve, resolveMatching, markRead, markAllRead, dismiss, clear],
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
