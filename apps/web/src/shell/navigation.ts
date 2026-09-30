import type { MessageKey } from "../i18n";
import type { IconName } from "./icons";

export interface NavItem {
  path: string;
  label: MessageKey;
  /** The short label of the tablet icon rail (768–1279px). */
  short: MessageKey;
  icon: IconName;
  /** Shown to an Owner / Admin only (display only: the Backend enforces the role). */
  adminOnly?: boolean;
}

// The PAW-060 design canvas (Roles / ShellDark): the main menu, a divider, then
// 管理 (Owner / Admin only, with the role badge) and 設定. 使用状況 is not in the
// main menu: it is under 管理 (Admin / Owner) and 設定 › 自分の使用状況.
export const NEW_CHAT_PATH = "/chat/new";

export const PRIMARY_NAV: readonly NavItem[] = [
  { path: "/", label: "nav.chat", short: "nav.short.chat", icon: "chat" },
  { path: "/projects", label: "nav.projects", short: "nav.short.projects", icon: "projects" },
  { path: "/agents", label: "nav.agents", short: "nav.short.agents", icon: "agents" },
  { path: "/memory", label: "nav.memory", short: "nav.short.memory", icon: "memory" },
  { path: "/pulls", label: "nav.pulls", short: "nav.short.pulls", icon: "pulls" },
];

export const SECONDARY_NAV: readonly NavItem[] = [
  { path: "/admin", label: "nav.admin", short: "nav.short.admin", icon: "admin", adminOnly: true },
  { path: "/settings", label: "nav.settings", short: "nav.short.settings", icon: "settings" },
];

/** The phone bottom tabs (MobileShell): チャット / タスク / メモリ / 通知 / 設定. */
export const BOTTOM_TABS: readonly NavItem[] = [
  { path: "/", label: "nav.chat", short: "nav.chat", icon: "chat" },
  { path: "/agents", label: "nav.short.agents", short: "nav.short.agents", icon: "agents" },
  { path: "/memory", label: "nav.memory", short: "nav.memory", icon: "memory" },
  { path: "/notifications", label: "nav.notifications", short: "nav.notifications", icon: "bell" },
  { path: "/settings", label: "nav.settings", short: "nav.settings", icon: "settings" },
];

/** Whether `path` is the entry's page or one below it. */
export function isActive(item: { path: string }, path: string): boolean {
  if (item.path === "/") return path === "/";
  return path === item.path || path.startsWith(`${item.path}/`);
}
