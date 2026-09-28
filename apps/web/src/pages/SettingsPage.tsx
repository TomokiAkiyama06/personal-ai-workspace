// The settings screen (the design's SettingsDevices / SettingsLanguage / Roles):
// its own header with 「ワークスペースへ戻る」, a sidebar grouped アカウント and
// (Owner / Admin) ワークスペース, and the chosen page. Entries of later issues are
// placeholders. The role only chooses what to SHOW; the Backend enforces it.
import type { ReactNode } from "react";
import { showsAdmin, useSignedIn } from "../auth/session";
import { type MessageKey, useI18n } from "../i18n";
import { NotificationBanners } from "../notifications/NotificationCenter";
import { Link, useRouter } from "../router";
import { Avatar, RoleBadge, useRoleLabel } from "../shell/common";
import { Icon } from "../shell/icons";
import { THEME_PREFERENCES, useTheme } from "../theme";
import { DevicesSection } from "./DevicesSection";

type Audience = "everyone" | "admin" | "owner";

interface SettingsEntry {
  path: string;
  label: MessageKey;
  audience: Audience;
}

export const SETTINGS_ACCOUNT: readonly SettingsEntry[] = [
  { path: "/settings/profile", label: "settings.profile", audience: "everyone" },
  { path: "/settings/devices", label: "settings.devices", audience: "everyone" },
  { path: "/settings/appearance", label: "settings.appearance", audience: "everyone" },
  { path: "/settings/agents", label: "settings.agents", audience: "everyone" },
  { path: "/settings/prompts", label: "settings.prompts", audience: "everyone" },
  { path: "/settings/github", label: "settings.github", audience: "everyone" },
  { path: "/settings/notifications", label: "settings.notifications", audience: "everyone" },
  { path: "/settings/usage", label: "settings.usage", audience: "everyone" },
];

export const SETTINGS_WORKSPACE: readonly SettingsEntry[] = [
  { path: "/settings/members", label: "settings.members", audience: "admin" },
  { path: "/settings/permissions", label: "settings.permissions", audience: "owner" },
  { path: "/settings/auth-policy", label: "settings.authPolicy", audience: "admin" },
  { path: "/settings/github-app", label: "settings.githubApp", audience: "admin" },
  { path: "/settings/connections", label: "settings.connections", audience: "admin" },
  { path: "/settings/backup", label: "settings.backup", audience: "admin" },
];

function shows(entry: SettingsEntry, role: string): boolean {
  if (entry.audience === "everyone") return true;
  if (entry.audience === "owner") return role === "owner";
  return showsAdmin(role);
}

function ProfileSection() {
  const { t } = useI18n();
  const { user } = useSignedIn();
  const roleLabel = useRoleLabel();
  return (
    <div className="settings-content">
      <div className="page-head">
        <div>
          <h1>{t("settings.profile")}</h1>
          <p className="muted">{t("profile.body")}</p>
        </div>
      </div>
      <section className="card">
        <dl className="facts">
          <dt>{t("profile.loginName")}</dt>
          <dd>{user.login_name}</dd>
          <dt>{t("profile.role")}</dt>
          <dd>{roleLabel(user.system_role)}</dd>
        </dl>
      </section>
    </div>
  );
}

function AppearanceSection() {
  const { t, formatDate } = useI18n();
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  return (
    <div className="settings-content">
      <div className="page-head">
        <div>
          <h1>{t("settings.appearance")}</h1>
          <p className="muted">{t("appearance.body")}</p>
        </div>
      </div>
      <section className="card">
        <div className="card-head">
          <h2>{t("appearance.language")}</h2>
        </div>
        <div className="card-row setting-row">
          <div>
            <p className="strong">{t("appearance.languageValue")}</p>
            <p className="muted small">{t("appearance.languageNote")}</p>
          </div>
          <span className="role-badge">{t("appearance.languageFixed")}</span>
        </div>
        <div className="card-row setting-row">
          <div>
            <p className="strong">{t("appearance.timeZone")}</p>
            <p className="muted small">{t("appearance.timeZoneValue", { zone })}</p>
          </div>
        </div>
        <div className="card-row setting-row">
          <div>
            <p className="strong">{t("appearance.dateTime")}</p>
            <p className="mono small">{formatDate(new Date().toISOString())}</p>
            <p className="muted small">{t("appearance.dateTimeNote")}</p>
          </div>
        </div>
      </section>
      <section className="card">
        <div className="card-head">
          <h2>{t("appearance.theme")}</h2>
        </div>
        <div className="card-row">
          <ThemeRadios />
        </div>
      </section>
    </div>
  );
}

/** The design's radio list (システムに合わせる / ライト / ダーク); same choice as the user menu. */
function ThemeRadios() {
  const { t } = useI18n();
  const { preference, setPreference } = useTheme();
  return (
    <fieldset className="radio-list">
      <legend className="visually-hidden">{t("appearance.theme")}</legend>
      {THEME_PREFERENCES.map((option) => (
        <label key={option} className="radio-option">
          <input
            type="radio"
            name="theme"
            value={option}
            checked={preference === option}
            onChange={() => setPreference(option)}
          />
          {option === "system" ? t("theme.systemLong") : t(`theme.${option}`)}
        </label>
      ))}
    </fieldset>
  );
}

function SettingsPlaceholder({ label }: { label: MessageKey }) {
  const { t } = useI18n();
  return (
    <div className="settings-content">
      <div className="page-head">
        <div>
          <h1>{t(label)}</h1>
          <p className="muted">{t("placeholder.body")}</p>
        </div>
      </div>
    </div>
  );
}

function SettingsNavGroup({
  title,
  entries,
  current,
}: {
  title: MessageKey;
  entries: readonly SettingsEntry[];
  current: string;
}) {
  const { t } = useI18n();
  return (
    <div className="settings-group">
      <span className="section-label">{t(title)}</span>
      {entries.map((entry) => (
        <Link
          key={entry.path}
          to={entry.path}
          aria-current={current === entry.path ? "page" : undefined}
        >
          {t(entry.label)}
        </Link>
      ))}
    </div>
  );
}

export function SettingsPage() {
  const { t } = useI18n();
  const { path } = useRouter();
  const { user } = useSignedIn();
  const role = user.system_role;
  const account = SETTINGS_ACCOUNT.filter((entry) => shows(entry, role));
  const workspace = SETTINGS_WORKSPACE.filter((entry) => shows(entry, role));
  // /settings opens the first page (プロフィール).
  const current = path === "/settings" ? "/settings/profile" : path;
  const entry = [...account, ...workspace].find((item) => item.path === current);

  let content: ReactNode;
  if (current === "/settings/profile") content = <ProfileSection />;
  else if (current === "/settings/devices") content = <DevicesSection />;
  else if (current === "/settings/appearance") content = <AppearanceSection />;
  else content = <SettingsPlaceholder label={entry?.label ?? "settings.title"} />;

  return (
    <div className="settings-screen">
      <header className="settings-header">
        <Link to="/" className="back-link">
          <Icon name="back" size={15} />
          {t("settings.back")}
        </Link>
        <span className="divider-v" aria-hidden="true" />
        <span className="settings-title">{t("settings.title")}</span>
        <span className="settings-user">
          <Avatar name={user.login_name} />
          <span className="user-name">{user.login_name}</span>
          <RoleBadge role={role} />
        </span>
      </header>
      <div className="settings-body">
        <nav className="settings-nav" aria-label={t("settings.menu")}>
          <SettingsNavGroup title="settings.group.account" entries={account} current={current} />
          {workspace.length > 0 && (
            <>
              <hr />
              <SettingsNavGroup
                title="settings.group.workspace"
                entries={workspace}
                current={current}
              />
            </>
          )}
        </nav>
        <main className="settings-main">
          <NotificationBanners />
          {content}
        </main>
      </div>
    </div>
  );
}
