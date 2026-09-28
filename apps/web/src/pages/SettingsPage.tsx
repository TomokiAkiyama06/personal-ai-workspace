import { useSignedIn } from "../auth/session";
import { LOCALES, useI18n } from "../i18n";
import { Link, useRouter } from "../router";
import { DevicesSection } from "./DevicesSection";
import { SecuritySection } from "./SecuritySection";

const TABS = [
  { path: "/settings", label: "settings.general" },
  { path: "/settings/security", label: "settings.security" },
  { path: "/settings/devices", label: "settings.devices" },
] as const;

function GeneralSection() {
  const { t, locale, setLocale } = useI18n();
  const { user } = useSignedIn();
  return (
    <>
      <section className="panel">
        <h2>{t("settings.account")}</h2>
        <dl className="facts">
          <dt>{t("settings.loginName")}</dt>
          <dd>{user.login_name}</dd>
          <dt>{t("settings.role")}</dt>
          <dd>{user.system_role}</dd>
        </dl>
      </section>
      <section className="panel">
        <h2>
          <label htmlFor="locale-select">{t("settings.language")}</label>
        </h2>
        <select
          id="locale-select"
          value={locale}
          onChange={(event) => {
            const next = LOCALES.find((item) => item.code === event.target.value);
            if (next) setLocale(next.code);
          }}
        >
          {LOCALES.map((item) => (
            <option key={item.code} value={item.code}>
              {item.label}
            </option>
          ))}
        </select>
      </section>
    </>
  );
}

export function SettingsPage() {
  const { t } = useI18n();
  const { path } = useRouter();
  return (
    <div className="page">
      <h1>{t("settings.title")}</h1>
      <nav className="tabs" aria-label={t("settings.tabs")}>
        {TABS.map((tab) => (
          <Link key={tab.path} to={tab.path} aria-current={path === tab.path ? "page" : undefined}>
            {t(tab.label)}
          </Link>
        ))}
      </nav>
      {path === "/settings/security" ? (
        <SecuritySection />
      ) : path === "/settings/devices" ? (
        <DevicesSection />
      ) : (
        <GeneralSection />
      )}
    </div>
  );
}
