// Notification Center state (docs/UI_DESIGN.md §10, docs/NOTIFICATION_POLICY.md):
// normal notifications gather under the bell, the same kind of notification is
// grouped, and ERROR / CRITICAL ones also show as a non-modal banner.
//
// The Backend has no notification API yet: this is the shell. A source (the
// future event stream) is plugged in through `NotificationSource`; without one the
// center is simply empty.
import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
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
  at: string;
}

export interface NotificationItem extends IncomingNotification {
  count: number;
  read: boolean;
  dismissed: boolean;
}

export interface NotificationSource {
  subscribe(onNotification: (notification: IncomingNotification) => void): () => void;
}

interface NotificationValue {
  items: NotificationItem[];
  unread: number;
  push: (notification: IncomingNotification) => void;
  markAllRead: () => void;
  dismiss: (key: string) => void;
}

const NotificationContext = createContext<NotificationValue | null>(null);
const MAX_ITEMS = 100;

export function mergeNotification(
  items: NotificationItem[],
  incoming: IncomingNotification,
): NotificationItem[] {
  const existing = items.find((item) => item.key === incoming.key);
  const merged: NotificationItem = existing
    ? { ...incoming, count: existing.count + 1, read: false, dismissed: false }
    : { ...incoming, count: 1, read: false, dismissed: false };
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
  const markAllRead = useCallback(() => {
    setItems((current) => current.map((item) => ({ ...item, read: true })));
  }, []);
  const dismiss = useCallback((key: string) => {
    setItems((current) =>
      current.map((item) => (item.key === key ? { ...item, dismissed: true, read: true } : item)),
    );
  }, []);
  useEffect(() => source?.subscribe(push), [source, push]);
  const value = useMemo(
    () => ({
      items,
      unread: items.filter((item) => !item.read).length,
      push,
      markAllRead,
      dismiss,
    }),
    [items, push, markAllRead, dismiss],
  );
  return <NotificationContext.Provider value={value}>{children}</NotificationContext.Provider>;
}

export function useNotifications(): NotificationValue {
  const value = useContext(NotificationContext);
  if (!value) throw new Error("useNotifications outside NotificationProvider");
  return value;
}
