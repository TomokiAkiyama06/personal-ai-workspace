import { useEffect, useId, useRef, useState } from "react";
import { useI18n } from "../i18n";
import { type NotificationItem, useNotifications } from "./store";

function SeverityLabel({ item }: { item: NotificationItem }) {
  const { t } = useI18n();
  return (
    <span className={`severity severity-${item.severity}`}>
      {t(`notifications.severity.${item.severity}`)}
    </span>
  );
}

/** The bell in the header and its panel (a non-modal popover). */
export function NotificationBell() {
  const { t, formatDate } = useI18n();
  const { items, unread, markAllRead } = useNotifications();
  const [open, setOpen] = useState(false);
  const panelId = useId();
  const container = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    const onPointer = (event: PointerEvent) => {
      if (container.current && !container.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", onPointer);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("pointerdown", onPointer);
    };
  }, [open]);

  const label =
    unread > 0 ? t("notifications.bellUnread", { count: unread }) : t("notifications.bell");
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
        <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" focusable="false">
          <path
            fill="currentColor"
            d="M12 22a2.5 2.5 0 0 0 2.45-2h-4.9A2.5 2.5 0 0 0 12 22Zm7-6V11a7 7 0 0 0-5.5-6.84V3a1.5 1.5 0 0 0-3 0v1.16A7 7 0 0 0 5 11v5l-2 2v1h18v-1l-2-2Z"
          />
        </svg>
        {unread > 0 && (
          <span className="badge" aria-hidden="true">
            {unread > 99 ? "99+" : unread}
          </span>
        )}
      </button>
      {open && (
        <section id={panelId} className="notification-panel" aria-label={t("notifications.title")}>
          <header>
            <h2>{t("notifications.title")}</h2>
            <button
              type="button"
              className="link-button"
              onClick={markAllRead}
              disabled={unread === 0}
            >
              {t("notifications.markAllRead")}
            </button>
          </header>
          {items.length === 0 ? (
            <p className="muted">{t("notifications.empty")}</p>
          ) : (
            <ul>
              {items.map((item) => (
                <li key={item.key} className={item.read ? "read" : "unread"}>
                  <SeverityLabel item={item} />
                  <div>
                    <p className="notification-title">
                      {item.title}
                      {item.count > 1 && (
                        <span className="muted">
                          {" "}
                          · {t("notifications.repeated", { count: item.count })}
                        </span>
                      )}
                    </p>
                    {item.body && <p className="muted">{item.body}</p>}
                    <time dateTime={item.at} className="muted">
                      {formatDate(item.at)}
                    </time>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>
      )}
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
          <SeverityLabel item={item} />
          <span className="banner-text">{item.title}</span>
          <button type="button" className="link-button" onClick={() => dismiss(item.key)}>
            {t("notifications.dismiss")}
          </button>
        </div>
      ))}
    </div>
  );
}
