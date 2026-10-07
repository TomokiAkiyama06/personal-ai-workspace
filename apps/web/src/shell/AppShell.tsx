// The Application Shell of the PAW-060 design canvas (ShellDark / ShellLight,
// Tablet, MobileShell / MobileMenu, UserMenu). One navigation, three layouts:
//   >= 1280px  full sidebar (252px) and the header search
//   768-1279px icon rail (78px) with short labels
//   < 768px    drawer (320px) behind the menu button, plus bottom tabs
// The role only chooses what to SHOW; the Backend decides every permission.
import { type ReactNode, useCallback, useEffect, useId, useRef, useState } from "react";
import { authApi } from "../api/auth";
import { showsAdmin, useSession, useSignedIn } from "../auth/session";
import { HealthChip } from "../health/HealthChip";
import { useI18n } from "../i18n";
import { errorMessage } from "../i18n/errors";
import {
  MarkAllReadButton,
  NotificationBanners,
  NotificationBell,
} from "../notifications/NotificationCenter";
import { usePendingApprovalNotifications } from "../notifications/pendingApprovals";
import { useNotifications } from "../notifications/store";
import { Link, useRouter } from "../router";
import { useTheme } from "../theme";
import {
  Avatar,
  PHONE_QUERY,
  RoleBadge,
  ThemeSegments,
  useDismiss,
  useFooterText,
  useMediaQuery,
  useRoleLabel,
} from "./common";
import { Icon, type IconName } from "./icons";
import {
  BOTTOM_TABS,
  isActive,
  type NavItem,
  NEW_CHAT_PATH,
  PRIMARY_NAV,
  SECONDARY_NAV,
} from "./navigation";

function NavLink({ item, onNavigate }: { item: NavItem; onNavigate: () => void }) {
  const { t } = useI18n();
  const { path } = useRouter();
  const { user } = useSignedIn();
  return (
    <Link
      to={item.path}
      className="nav-link"
      aria-current={isActive(item, path) ? "page" : undefined}
      onClick={onNavigate}
    >
      <Icon name={item.icon} />
      <span className="nav-label">{t(item.label)}</span>
      <span className="nav-short" aria-hidden="true">
        {t(item.short)}
      </span>
      {item.adminOnly && <RoleBadge role={user.system_role} />}
    </Link>
  );
}

function DrawerAccount({ onNavigate }: { onNavigate: () => void }) {
  const { t } = useI18n();
  const { user } = useSignedIn();
  const roleLabel = useRoleLabel();
  return (
    <div className="drawer-account">
      <Avatar name={user.login_name} size="large" />
      <span className="drawer-account-text">
        <strong>{user.login_name}</strong>
        <span className="mono muted">
          {roleLabel(user.system_role)} · {window.location.host}
        </span>
      </span>
      <Link
        to="/settings/profile"
        className="icon-button"
        aria-label={t("user.profile")}
        onClick={onNavigate}
      >
        <Icon name="chevronRight" size={18} />
      </Link>
    </div>
  );
}

function MenuLink({
  to,
  icon,
  label,
  extra,
  onNavigate,
}: {
  to: string;
  icon: IconName;
  label: string;
  extra?: ReactNode;
  onNavigate: () => void;
}) {
  return (
    <Link to={to} className="menu-item" onClick={onNavigate}>
      <Icon name={icon} size={16} />
      <span className="menu-item-label">{label}</span>
      {extra}
    </Link>
  );
}

function UserMenu() {
  const { t } = useI18n();
  const { signOut } = useSession();
  const { user } = useSignedIn();
  const footer = useFooterText();
  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState(0);
  const [signOutError, setSignOutError] = useState<string | null>(null);
  const menuId = useId();
  const container = useRef<HTMLDivElement>(null);
  const close = useCallback(() => setOpen(false), []);
  useDismiss(open, container, close);

  // 端末とセッション shows the devices waiting for this account's approval.
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    authApi
      .pendingPairings()
      .then((result) => {
        if (!cancelled) setPending(result.pending.length);
      })
      .catch(() => {
        // Only a hint: without it the entry simply has no badge.
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  const usagePath = showsAdmin(user.system_role) ? "/admin/usage" : "/settings/usage";
  return (
    <div className="user-menu" ref={container}>
      <button
        type="button"
        className="user-button"
        aria-label={t("user.menu")}
        aria-expanded={open}
        aria-controls={menuId}
        onClick={() => setOpen((value) => !value)}
      >
        <Avatar name={user.login_name} />
        <span className="user-name">{user.login_name}</span>
        <Icon name="chevronDown" size={13} />
      </button>
      {open && (
        <div id={menuId} className="popover user-popover">
          <div className="user-popover-head">
            <Avatar name={user.login_name} size="large" />
            <span className="user-popover-id">
              <span className="user-popover-name">
                <strong>{user.login_name}</strong>
                <RoleBadge role={user.system_role} />
              </span>
              <span className="mono muted ellipsis">
                {t("user.account", { name: user.login_name, host: window.location.host })}
              </span>
            </span>
          </div>
          <div className="menu-group">
            <MenuLink
              to="/settings/profile"
              icon="profile"
              label={t("user.profile")}
              onNavigate={close}
            />
            <MenuLink
              to="/settings"
              icon="settings"
              label={t("user.settings")}
              onNavigate={close}
            />
            <MenuLink
              to="/settings/devices"
              icon="devices"
              label={t("user.devices")}
              onNavigate={close}
              extra={
                pending > 0 ? (
                  <span className="status-chip warning">
                    {t("user.pending", { count: pending })}
                  </span>
                ) : null
              }
            />
            <MenuLink to={usagePath} icon="usage" label={t("user.usage")} onNavigate={close} />
            <MenuLink
              to="/help/shortcuts"
              icon="keyboard"
              label={t("user.shortcuts")}
              onNavigate={close}
              extra={<span className="mono muted">⌘ /</span>}
            />
            <MenuLink to="/help" icon="help" label={t("user.help")} onNavigate={close} />
          </div>
          <div className="menu-theme">
            <span>{t("user.theme")}</span>
            <ThemeSegments />
          </div>
          <div className="menu-group">
            {signOutError && (
              <p className="form-error menu-error" role="alert">
                {signOutError}
              </p>
            )}
            <button
              type="button"
              className="menu-item danger"
              onClick={() => {
                setSignOutError(null);
                signOut().catch((caught: unknown) => setSignOutError(errorMessage(t, caught)));
              }}
            >
              <Icon name="signOut" size={16} />
              <span className="menu-item-label">{t("user.signOut")}</span>
            </button>
          </div>
          <div className="popover-footer mono muted">{footer}</div>
        </div>
      )}
    </div>
  );
}

function ThemeToggle() {
  const { t } = useI18n();
  const { resolved, setPreference } = useTheme();
  const toLight = resolved === "dark";
  return (
    <button
      type="button"
      className="icon-button bordered theme-toggle"
      aria-label={toLight ? t("header.toLight") : t("header.toDark")}
      onClick={() => setPreference(toLight ? "light" : "dark")}
    >
      <Icon name={toLight ? "moon" : "sun"} size={17} />
    </button>
  );
}

function BottomTabs() {
  const { t } = useI18n();
  const { path } = useRouter();
  const { unread } = useNotifications();
  return (
    <nav className="bottom-tabs" aria-label={t("nav.bottom")}>
      {BOTTOM_TABS.map((item) => (
        <Link
          key={item.path}
          to={item.path}
          aria-current={isActive(item, path) ? "page" : undefined}
        >
          <Icon name={item.icon} size={20} />
          {t(item.label)}
          {item.path === "/notifications" && unread > 0 && (
            <span className="tab-dot" aria-hidden="true" />
          )}
        </Link>
      ))}
    </nav>
  );
}

/** The label of the screen at `path`, for the phone header. */
function useScreenTitle(): string {
  const { t } = useI18n();
  const { path } = useRouter();
  if (path === "/notifications") return t("nav.notifications");
  const item = [...PRIMARY_NAV, ...SECONDARY_NAV].find((entry) => isActive(entry, path));
  return item ? t(item.label) : t("app.name");
}

/** Header, global navigation and main area. */
export function AppShell({ children }: { children: ReactNode }) {
  const { t } = useI18n();
  const { path } = useRouter();
  const { user } = useSignedIn();
  const [navOpen, setNavOpen] = useState(false);
  const navId = useId();
  const title = useScreenTitle();
  const secondary = SECONDARY_NAV.filter((item) => !item.adminOnly || showsAdmin(user.system_role));
  const close = useCallback(() => setNavOpen(false), []);
  // MobileNotifications: the full-screen list has 戻る and すべて既読 in the header.
  const phoneNotifications = useMediaQuery(PHONE_QUERY) && path === "/notifications";
  usePendingApprovalNotifications();

  useEffect(() => {
    if (!navOpen) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") close();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [navOpen, close]);

  return (
    <div className={navOpen ? "shell nav-open" : "shell"}>
      <header className="app-header">
        {phoneNotifications ? (
          <Link to="/" className="icon-button nav-toggle" aria-label={t("notifications.back")}>
            <Icon name="back" size={20} />
          </Link>
        ) : (
          <button
            type="button"
            className="icon-button nav-toggle"
            aria-label={navOpen ? t("nav.close") : t("nav.open")}
            aria-expanded={navOpen}
            aria-controls={navId}
            onClick={() => setNavOpen((value) => !value)}
          >
            <Icon name="menu" size={20} />
          </button>
        )}
        <Link to="/" className="brand">
          <Icon name="logo" size={22} />
          <span className="brand-name">{t("app.name")}</span>
        </Link>
        <span className="phone-title">{title}</span>
        <div className="header-search" title={t("header.searchLater")}>
          <Icon name="search" size={15} />
          <label htmlFor="global-search" className="visually-hidden">
            {t("header.search")}
          </label>
          <input
            id="global-search"
            type="search"
            placeholder={t("header.searchPlaceholder")}
            disabled
          />
          <kbd>⌘K</kbd>
        </div>
        <div className="header-actions">
          <HealthChip />
          <ThemeToggle />
          {phoneNotifications ? <MarkAllReadButton /> : <NotificationBell />}
          <UserMenu />
        </div>
      </header>
      <div className="shell-body">
        <nav id={navId} className="app-nav" aria-label={t("nav.label")}>
          <DrawerAccount onNavigate={close} />
          <Link
            to={NEW_CHAT_PATH}
            className="new-chat"
            aria-current={path === NEW_CHAT_PATH ? "page" : undefined}
            onClick={close}
          >
            <Icon name="plus" size={16} />
            <span className="nav-label">{t("nav.newChat")}</span>
          </Link>
          {PRIMARY_NAV.map((item) => (
            <NavLink key={item.path} item={item} onNavigate={close} />
          ))}
          <hr />
          {secondary.map((item) => (
            <NavLink key={item.path} item={item} onNavigate={close} />
          ))}
        </nav>
        {navOpen && <div className="nav-scrim" aria-hidden="true" onClick={close} />}
        <main className="app-main">
          <NotificationBanners />
          {children}
        </main>
      </div>
      <BottomTabs />
    </div>
  );
}
