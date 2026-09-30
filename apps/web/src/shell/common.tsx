// Small pieces shared by the shell, the settings screen and the sign-in page.
import { type RefObject, useEffect, useState } from "react";
import { useI18n } from "../i18n";
import { type ThemePreference, useTheme } from "../theme";
import { Icon } from "./icons";

/** "Owner" / "Admin" / "Member" (the design's names); an unknown role as it is. */
export function useRoleLabel(): (role: string) => string {
  const { t } = useI18n();
  return (role) =>
    role === "owner" || role === "admin" || role === "user" ? t(`user.role.${role}`) : role;
}

export function Avatar({ name, size = "small" }: { name: string; size?: "small" | "large" }) {
  return (
    <span className={`avatar avatar-${size}`} aria-hidden="true">
      {name.slice(0, 1).toUpperCase()}
    </span>
  );
}

export function RoleBadge({ role }: { role: string }) {
  const roleLabel = useRoleLabel();
  return <span className="role-badge">{roleLabel(role)}</span>;
}

/** "workspace.local · v0.1.0" */
export function useFooterText(): string {
  const { t } = useI18n();
  return t("app.footer", { host: window.location.host, version: __APP_VERSION__ });
}

/** Whether a media query matches (false where matchMedia does not exist, e.g. jsdom). */
export function useMediaQuery(query: string): boolean {
  const get = () => typeof window.matchMedia === "function" && window.matchMedia(query).matches;
  const [matches, setMatches] = useState(get);
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const list = window.matchMedia(query);
    const onChange = () => setMatches(list.matches);
    onChange();
    list.addEventListener?.("change", onChange);
    return () => list.removeEventListener?.("change", onChange);
  }, [query]);
  return matches;
}

export const PHONE_QUERY = "(max-width: 767px)";

/** Close a popover on Escape or a pointer press outside `container`. */
export function useDismiss(
  open: boolean,
  container: RefObject<HTMLElement | null>,
  close: () => void,
): void {
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") close();
    };
    const onPointer = (event: PointerEvent) => {
      if (container.current && !container.current.contains(event.target as Node)) close();
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", onPointer);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("pointerdown", onPointer);
    };
  }, [open, container, close]);
}

/** The segmented theme control (UserMenu: システム / ライト / ダーク). */
export function ThemeSegments({
  options = ["system", "light", "dark"],
  withIcons = false,
}: {
  options?: readonly ThemePreference[];
  withIcons?: boolean;
}) {
  const { t } = useI18n();
  const { preference, resolved, setPreference } = useTheme();
  // Without a "system" option (the sign-in page), the resolved theme is pressed.
  const current = options.includes(preference) ? preference : resolved;
  return (
    <fieldset className="segments">
      <legend className="visually-hidden">{t("theme.group")}</legend>
      {options.map((option) => (
        <button
          key={option}
          type="button"
          aria-pressed={current === option}
          onClick={() => setPreference(option)}
        >
          {withIcons && option !== "system" && (
            <Icon name={option === "dark" ? "moon" : "sun"} size={14} />
          )}
          {t(`theme.${option}`)}
        </button>
      ))}
    </fieldset>
  );
}
