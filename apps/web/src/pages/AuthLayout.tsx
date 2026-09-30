// The frame of the pages shown before the shell (the design's Login / LoginLight /
// MobileLogin): the brand panel with the system state on the left (wide screens
// only), and the theme switch (表示 ダーク / ライト) above the card on the right.
import { type ReactNode, useEffect, useState } from "react";
import { apiRequest } from "../api/client";
import { useI18n } from "../i18n";
import { ThemeSegments, useFooterText } from "../shell/common";
import { Icon } from "../shell/icons";

type Health = "checking" | "ok" | "down";

/** GET /api/v1/health/ready: the only system state the Backend shows before sign-in. */
function useBackendHealth(): Health {
  const [health, setHealth] = useState<Health>("checking");
  useEffect(() => {
    let cancelled = false;
    apiRequest<{ status: string }>("GET", "/health/ready")
      .then((body) => {
        if (!cancelled) setHealth(body.status === "ok" ? "ok" : "down");
      })
      .catch(() => {
        if (!cancelled) setHealth("down");
      });
    return () => {
      cancelled = true;
    };
  }, []);
  return health;
}

function BrandPanel() {
  const { t } = useI18n();
  const health = useBackendHealth();
  const footer = useFooterText();
  const value =
    health === "ok" ? t("brand.ok") : health === "down" ? t("brand.down") : t("brand.checking");
  return (
    <aside className="brand-panel">
      <div className="stack-lg">
        <div className="brand">
          <Icon name="logo" size={28} />
          <span className="brand-name">{t("app.name")}</span>
        </div>
        <p className="brand-headline">
          {t("brand.headline1")}
          <br />
          {t("brand.headline2")}
        </p>
        <p className="brand-body">{t("brand.body")}</p>
      </div>
      <div className="brand-status">
        <span className="section-label">{t("brand.status")}</span>
        <div className="status-line">
          <span className={`status-dot ${health}`} aria-hidden="true" />
          <span className="status-name">{t("brand.backend")}</span>
          <span className={`mono status-value ${health}`}>{value}</span>
        </div>
        <div className="brand-footer mono">{footer}</div>
      </div>
    </aside>
  );
}

export function AuthLayout({ children }: { children: ReactNode }) {
  const { t } = useI18n();
  const footer = useFooterText();
  return (
    <div className="auth-screen">
      <BrandPanel />
      <div className="auth-side">
        <div className="auth-topbar">
          <span className="brand auth-mobile-brand">
            <Icon name="logo" size={22} />
            <span className="brand-name">{t("app.name")}</span>
          </span>
          <span className="muted small push-right theme-label">{t("login.theme")}</span>
          <ThemeSegments options={["dark", "light"]} withIcons />
        </div>
        <main className="auth-center">
          <div className="auth-card">{children}</div>
        </main>
        <p className="auth-mobile-footer mono muted">{footer}</p>
      </div>
    </div>
  );
}
