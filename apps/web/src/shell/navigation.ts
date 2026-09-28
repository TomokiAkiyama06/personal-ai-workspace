import type { MessageKey } from "../i18n";

export interface NavItem {
  path: string;
  label: MessageKey;
  /** Shown to an Owner / Admin only (display only: the Backend enforces the role). */
  adminOnly?: boolean;
}

// docs/UI_DESIGN.md §3 (V1 candidates). Low-frequency entries sit below the divider.
export const PRIMARY_NAV: readonly NavItem[] = [
  { path: "/", label: "nav.chat" },
  { path: "/projects", label: "nav.projects" },
  { path: "/agents", label: "nav.agents" },
  { path: "/memory", label: "nav.memory" },
  { path: "/pulls", label: "nav.pulls" },
  { path: "/usage", label: "nav.usage" },
];

export const SECONDARY_NAV: readonly NavItem[] = [
  { path: "/admin", label: "nav.admin", adminOnly: true },
  { path: "/settings", label: "nav.settings" },
];

/** Whether `path` is the entry's page or one below it. */
export function isActive(item: NavItem, path: string): boolean {
  if (item.path === "/") return path === "/";
  return path === item.path || path.startsWith(`${item.path}/`);
}
