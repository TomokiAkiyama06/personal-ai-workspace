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
  type SetStateAction,
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
// The ids of the entries the MAX_ITEMS limit pushed out, still deduplicated (a
// replay of their events is not new). Bounded too: the oldest are forgotten.
const MAX_EVICTED_IDS = 10_000;

interface NotificationState {
  items: NotificationItem[];
  /**
   * The events of entries dropped by the MAX_ITEMS limit (not of resolved ones),
   * as `evictedEvent(key, id)`: an id is only unique within its key.
   */
  evicted: ReadonlySet<string>;
}

const EMPTY: NotificationState = { items: [], evicted: new Set() };

function evictedEvent(key: string, id: string): string {
  return JSON.stringify([key, id]);
}

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
  if (incoming.id !== undefined && state.evicted.has(evictedEvent(incoming.key, incoming.id))) {
    return state;
  }
  const items = mergeNotification(state.items, incoming);
  if (items === state.items) return state;
  const kept = new Set(items.map((item) => item.key));
  const dropped = state.items
    .filter((item) => !kept.has(item.key))
    .flatMap((item) => item.ids.map((id) => evictedEvent(item.key, id)));
  if (dropped.length === 0) return { ...state, items };
  const evicted = [...state.evicted, ...dropped];
  return { items, evicted: new Set(evicted.slice(-MAX_EVICTED_IDS)) };
}

/**
 * The condition of the keys passing `test` is over: their entries go away and
 * their ids are forgotten, also those of entries the limit already pushed out
 * (Codex review #194), so the condition coming back is a new notification.
 */
function resolveKeys(state: NotificationState, test: (key: string) => boolean): NotificationState {
  const items = state.items.some((item) => test(item.key))
    ? state.items.filter((item) => !test(item.key))
    : state.items;
  const remembered = [...state.evicted].filter((event) => {
    const [key] = JSON.parse(event) as [string, string];
    return !test(key);
  });
  const evicted = remembered.length === state.evicted.size ? state.evicted : new Set(remembered);
  return items === state.items && evicted === state.evicted ? state : { items, evicted };
}

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
  const [state, setState] = useState<NotificationState>(EMPTY);
  const items = state.items;
  // Every change but a push, a resolve and a clear is to the list only.
  const setItems = useCallback((change: (current: NotificationItem[]) => NotificationItem[]) => {
    setState((current) => {
      const next = change(current.items);
      return next === current.items ? current : { ...current, items: next };
    });
  }, []);
  // The current items for the handlers below, which send requests (never from
  // inside a state update, which React may run twice).
  const latest = useRef(items);
  latest.current = items;
  const remote = useRef<NotificationRemote | null>(null);
  const setRemote = useCallback((value: NotificationRemote | null) => {
    remote.current = value;
  }, []);
  const [remoteUnread, setRemoteUnreadState] = useState<number | null>(null);
  // Every change of the total (a list, an optimistic read, an account clear)
  // starts a new generation; a write's answer sets the total only while its
  // generation is the newest, so an older answer arriving late never overwrites
  // a newer total.
  const unreadGeneration = useRef(0);
  // The generation of the last account clear.
  const cleared = useRef(0);
  const changeRemoteUnread = useCallback((total: SetStateAction<number | null>) => {
    unreadGeneration.current += 1;
    setRemoteUnreadState(total);
    return unreadGeneration.current;
  }, []);
  const setRemoteUnread = useCallback(
    (total: number | null) => {
      changeRemoteUnread(total);
    },
    [changeRemoteUnread],
  );
  // A write's outcome: the Backend's total after it, or (it failed) the entries
  // are unread again, as on the Backend, and the total is the one before the
  // optimistic change, until the next list. A newer total stays.
  const settle = useCallback(
    (write: Promise<number>, generation: number, keys: Set<string>, before: number | null) => {
      const current = () => unreadGeneration.current === generation;
      write.then(
        (total) => {
          if (current()) setRemoteUnreadState(total);
        },
        () => {
          if (generation <= cleared.current) return;
          setItems((items) =>
            items.map((item) => (keys.has(item.key) ? { ...item, read: false } : item)),
          );
          if (current()) setRemoteUnreadState(before);
        },
      );
    },
    [setItems],
  );
  const sendRead = useCallback(
    (key: string) => {
      const item = latest.current.find((entry) => entry.key === key);
      const target = remote.current;
      if (!item?.remote || item.read || item.ids.length === 0 || !target) return;
      const before = remoteUnread;
      const generation = changeRemoteUnread((total) =>
        total === null ? null : Math.max(0, total - item.ids.length),
      );
      settle(target.markRead(item.ids), generation, new Set([key]), before);
    },
    [remoteUnread, changeRemoteUnread, settle],
  );
  const push = useCallback((incoming: IncomingNotification) => {
    setState((current) => pushNotification(current, incoming));
  }, []);
  const resolve = useCallback((key: string) => {
    setState((current) => resolveKeys(current, (candidate) => candidate === key));
  }, []);
  const resolveMatching = useCallback((test: (key: string) => boolean) => {
    setState((current) => resolveKeys(current, test));
  }, []);
  const markRead = useCallback(
    (key: string) => {
      sendRead(key);
      setItems((current) =>
        current.map((item) => (item.key === key && !item.read ? { ...item, read: true } : item)),
      );
    },
    [sendRead, setItems],
  );
  const markAllRead = useCallback(() => {
    const target = remote.current;
    if (target && (remoteUnread ?? 0) + latest.current.filter((i) => i.remote && !i.read).length) {
      const keys = new Set(
        latest.current.filter((item) => item.remote && !item.read).map((item) => item.key),
      );
      const before = remoteUnread;
      settle(target.markAllRead(), changeRemoteUnread(0), keys, before);
    }
    setItems((current) => current.map((item) => ({ ...item, read: true })));
  }, [remoteUnread, changeRemoteUnread, settle, setItems]);
  const dismiss = useCallback(
    (key: string) => {
      // Closing the banner reads the entry (it stays in the list).
      sendRead(key);
      setItems((current) =>
        current.map((item) => (item.key === key ? { ...item, dismissed: true, read: true } : item)),
      );
    },
    [sendRead, setItems],
  );
  const clear = useCallback(() => {
    setState((current) =>
      current.items.length === 0 && current.evicted.size === 0 ? current : EMPTY,
    );
    // Writes sent for the previous account change nothing when they answer.
    cleared.current = changeRemoteUnread(null);
  }, [changeRemoteUnread]);
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
      setRemoteUnread,
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
