import { type ReactNode, useEffect, useId, useRef, useState } from "react";
import { showsAdmin, useSession, useSignedIn } from "../auth/session";
import { useI18n } from "../i18n";
import { NotificationBanners, NotificationBell } from "../notifications/NotificationCenter";
import { Link, useRouter } from "../router";
import { isActive, type NavItem, PRIMARY_NAV, SECONDARY_NAV } from "./navigation";

function NavList({ items, onNavigate }: { items: readonly NavItem[]; onNavigate: () => void }) {
  const { t } = useI18n();
  const { path } = useRouter();
  return (
    <ul>
      {items.map((item) => (
        <li key={item.path}>
          <Link
            to={item.path}
            aria-current={isActive(item, path) ? "page" : undefined}
            onClick={onNavigate}
          >
            {t(item.label)}
          </Link>
        </li>
      ))}
    </ul>
  );
}

function UserMenu() {
  const { t } = useI18n();
  const { signOut } = useSession();
  const { user } = useSignedIn();
  const [open, setOpen] = useState(false);
  const menuId = useId();
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
  const role = user.system_role;
  const roleLabel =
    role === "owner" || role === "admin" || role === "user" ? t(`user.role.${role}`) : role;
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
        <span className="avatar" aria-hidden="true">
          {user.login_name.slice(0, 1).toUpperCase()}
        </span>
        <span className="user-name">{user.login_name}</span>
      </button>
      {open && (
        <div id={menuId} className="user-popover">
          <p>
            <strong>{user.login_name}</strong>
            <br />
            <span className="muted">{roleLabel}</span>
          </p>
          <Link to="/settings" onClick={() => setOpen(false)}>
            {t("user.settings")}
          </Link>
          <button type="button" className="secondary" onClick={() => void signOut()}>
            {t("user.logout")}
          </button>
        </div>
      )}
    </div>
  );
}

/** Header, global navigation and main area (docs/UI_DESIGN.md §2, §17). */
export function AppShell({ children }: { children: ReactNode }) {
  const { t } = useI18n();
  const { user } = useSignedIn();
  const [navOpen, setNavOpen] = useState(false);
  const navId = useId();
  const secondary = SECONDARY_NAV.filter((item) => !item.adminOnly || showsAdmin(user.system_role));
  const close = () => setNavOpen(false);
  return (
    <div className={navOpen ? "shell nav-open" : "shell"}>
      <header className="app-header">
        <button
          type="button"
          className="icon-button nav-toggle"
          aria-label={navOpen ? t("nav.close") : t("nav.open")}
          aria-expanded={navOpen}
          aria-controls={navId}
          onClick={() => setNavOpen((value) => !value)}
        >
          <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" focusable="false">
            <path fill="currentColor" d="M3 6h18v2H3zm0 5h18v2H3zm0 5h18v2H3z" />
          </svg>
        </button>
        <Link to="/" className="brand">
          {t("app.name")}
        </Link>
        <div className="header-actions">
          <NotificationBell />
          <UserMenu />
        </div>
      </header>
      <nav id={navId} className="app-nav" aria-label={t("nav.label")}>
        <NavList items={PRIMARY_NAV} onNavigate={close} />
        <hr />
        <NavList items={secondary} onNavigate={close} />
      </nav>
      {navOpen && <div className="nav-scrim" aria-hidden="true" onClick={close} />}
      <main className="app-main">
        <NotificationBanners />
        {children}
      </main>
    </div>
  );
}
