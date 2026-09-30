// Notification Center (the PAW-060 design: NotificationCenter / MobileNotifications).
// Desktop and tablet: the bell opens a non-modal dropdown. Phone (< 768px): the
// bell and the 通知 bottom tab open the full-screen list at /notifications.
import { useCallback, useId, useRef, useState } from "react";
import { useI18n } from "../i18n";
import { Link, useRouter } from "../router";
import { PHONE_QUERY, useDismiss, useMediaQuery } from "../shell/common";
import { Icon } from "../shell/icons";
import {
  matchesFilter,
  NOTIFICATION_FILTERS,
  type NotificationFilter,
  type NotificationItem,
  useNotifications,
} from "./store";

function NotificationEntry({ item }: { item: NotificationItem }) {
  const { t, formatTime } = useI18n();
  return (
    <li className={`notification severity-${item.severity} ${item.read ? "read" : "unread"}`}>
      <span className="notification-dot" aria-hidden="true" />
      <div className="notification-body">
        <div className="notification-meta">
          <span className="severity-label">
            {t(`notifications.severity.${item.severity}`)}
            {item.read && ` · ${t("notifications.read")}`}
          </span>
          {item.count > 1 ? (
            <span className="chip-count">{t("notifications.grouped", { count: item.count })}</span>
          ) : (
            item.source && <span className="notification-source">{item.source}</span>
          )}
          <time dateTime={item.at} className="notification-time">
            {formatTime(item.at)}
          </time>
        </div>
        <p className="notification-title">{item.title}</p>
        {item.body && <p className="notification-text">{item.body}</p>}
      </div>
    </li>
  );
}

/** Header (未読 N / すべて既読), the filter chips and the list. */
export function NotificationList({ headingLevel = 2 }: { headingLevel?: 1 | 2 }) {
  const { t } = useI18n();
  const { items, unread, markAllRead } = useNotifications();
  const [filter, setFilter] = useState<NotificationFilter>("all");
  const shown = items.filter((item) => matchesFilter(item, filter));
  const Heading = headingLevel === 1 ? "h1" : "h2";
  return (
    <>
      <div className="notifications-head">
        <Heading>{t("notifications.title")}</Heading>
        {unread > 0 && (
          <span className="count-chip">{t("notifications.unread", { count: unread })}</span>
        )}
        <button
          type="button"
          className="text-button accent"
          onClick={markAllRead}
          disabled={unread === 0}
        >
          {t("notifications.markAllRead")}
        </button>
      </div>
      <fieldset className="filter-chips">
        <legend className="visually-hidden">{t("notifications.filter")}</legend>
        {NOTIFICATION_FILTERS.map((value) => (
          <button
            key={value}
            type="button"
            className="chip"
            aria-pressed={filter === value}
            onClick={() => setFilter(value)}
          >
            {t(`notifications.filter.${value}`)}
          </button>
        ))}
      </fieldset>
      {shown.length === 0 ? (
        <p className="notifications-empty">
          {items.length === 0 ? t("notifications.empty") : t("notifications.emptyFiltered")}
        </p>
      ) : (
        <ul className="notification-list">
          {shown.map((item) => (
            <NotificationEntry key={item.key} item={item} />
          ))}
        </ul>
      )}
    </>
  );
}

function BellIcon({ unread }: { unread: number }) {
  return (
    <>
      <Icon name="bell" size={19} />
      {unread > 0 && (
        <span className="badge" aria-hidden="true">
          {unread > 99 ? "99+" : unread}
        </span>
      )}
    </>
  );
}

/** The bell in the header and its dropdown panel. */
export function NotificationBell() {
  const { t } = useI18n();
  const { navigate } = useRouter();
  const { unread } = useNotifications();
  const phone = useMediaQuery(PHONE_QUERY);
  const [open, setOpen] = useState(false);
  const panelId = useId();
  const container = useRef<HTMLDivElement>(null);
  const close = useCallback(() => setOpen(false), []);
  useDismiss(open, container, close);

  const label =
    unread > 0 ? t("notifications.bellUnread", { count: unread }) : t("notifications.bell");
  if (phone) {
    return (
      <button
        type="button"
        className="icon-button bell"
        aria-label={label}
        onClick={() => navigate("/notifications")}
      >
        <BellIcon unread={unread} />
      </button>
    );
  }
  return (
    <div className="notification-center" ref={container}>
      <button
        type="button"
        className="icon-button bell"
        aria-label={label}
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((value) => !value)}
      >
        <BellIcon unread={unread} />
      </button>
      {open && (
        <aside
          id={panelId}
          className="popover notification-panel"
          aria-label={t("notifications.title")}
        >
          <NotificationList />
          <div className="popover-footer">
            <Link to="/notifications" onClick={close}>
              {t("notifications.seeAll")}
            </Link>
            <Link to="/settings/notifications" onClick={close}>
              {t("notifications.rules")}
            </Link>
          </div>
        </aside>
      )}
    </div>
  );
}

/** The full-screen list (phone) and the "すべての通知を見る" page. */
export function NotificationsPage() {
  return (
    <div className="page notifications-page">
      <NotificationList headingLevel={1} />
    </div>
  );
}

/** ERROR / CRITICAL notifications that are not dismissed, as non-modal banners. */
export function NotificationBanners() {
  const { t } = useI18n();
  const { items, dismiss } = useNotifications();
  const shown = items.filter(
    (item) => !item.dismissed && (item.severity === "error" || item.severity === "critical"),
  );
  if (shown.length === 0) return null;
  return (
    <div className="banners">
      {shown.map((item) => (
        <div
          key={item.key}
          className={`banner banner-${item.severity}`}
          role={item.severity === "critical" ? "alert" : "status"}
        >
          <span className="severity-pill">{t(`notifications.severity.${item.severity}`)}</span>
          <span className="banner-text">{item.title}</span>
          {item.body && <span className="banner-detail">{item.body}</span>}
          <button
            type="button"
            className="banner-close"
            aria-label={t("notifications.dismiss")}
            onClick={() => dismiss(item.key)}
          >
            ×
          </button>
        </div>
      ))}
    </div>
  );
}
