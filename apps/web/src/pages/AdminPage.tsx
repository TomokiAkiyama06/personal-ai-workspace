// 管理 (the design's Admin / Usage boards): the title with the role badge and
// the admin tabs, then the chosen tab. This issue (PAW-064) fills 使用状況; the
// other tabs belong to later issues and are placeholders. The navigation shows 管理
// to an Owner / Admin only; the Backend enforces every permission.
import { useEffect } from "react";
import { showsAdmin, useSignedIn } from "../auth/session";
import { type MessageKey, useI18n } from "../i18n";
import { Link, useRouter } from "../router";
import { RoleBadge } from "../shell/common";
import { UsageView } from "../usage/UsageView";

export const ADMIN_TABS: readonly { path: string; label: MessageKey }[] = [
  { path: "/admin", label: "admin.tab.overview" },
  { path: "/admin/users", label: "admin.tab.users" },
  { path: "/admin/usage", label: "admin.tab.usage" },
  { path: "/admin/monitoring", label: "admin.tab.monitoring" },
  { path: "/admin/quotas", label: "admin.tab.quotas" },
  { path: "/admin/models", label: "admin.tab.models" },
  { path: "/admin/backup", label: "admin.tab.backup" },
  { path: "/admin/audit", label: "admin.tab.audit" },
];

function AdminPlaceholder({ label }: { label: MessageKey }) {
  const { t } = useI18n();
  return (
    <div className="admin-placeholder">
      <h1>{t(label)}</h1>
      <p className="muted">{t("placeholder.body")}</p>
    </div>
  );
}

export function AdminPage() {
  const { t } = useI18n();
  const { path, navigate } = useRouter();
  const { user } = useSignedIn();
  const admin = showsAdmin(user.system_role);
  // A Member who opens 管理 directly (a link, a typed URL) is not shown it (Roles:
  // 管理メニューは表示されません); their own usage is 設定 › 自分の使用状況.
  useEffect(() => {
    if (!admin) navigate(path === "/admin/usage" ? "/settings/usage" : "/", { replace: true });
  }, [admin, path, navigate]);
  if (!admin) return null;
  const tab = ADMIN_TABS.find((entry) => entry.path === path) ?? ADMIN_TABS[0];
  return (
    <div className="admin">
      <div className="admin-head">
        <span className="admin-title">{t("admin.title")}</span>
        <RoleBadge role={user.system_role} />
        <nav className="admin-tabs" aria-label={t("admin.tabs")}>
          {ADMIN_TABS.map((entry) => (
            <Link
              key={entry.path}
              to={entry.path}
              aria-current={entry.path === tab?.path ? "page" : undefined}
            >
              {t(entry.label)}
            </Link>
          ))}
        </nav>
      </div>
      {tab?.path === "/admin/usage" ? (
        <UsageView title="usage.title" scopes={["self", "workspace"]} />
      ) : (
        <AdminPlaceholder label={tab?.label ?? "admin.tab.overview"} />
      )}
    </div>
  );
}
