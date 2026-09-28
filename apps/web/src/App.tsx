import { useSession } from "./auth/session";
import { useI18n } from "./i18n";
import { LoginPage } from "./pages/LoginPage";
import { PairPage } from "./pages/PairPage";
import { PasskeyGatePage } from "./pages/PasskeyGatePage";
import { PlaceholderPage } from "./pages/PlaceholderPage";
import { SettingsPage } from "./pages/SettingsPage";
import { useRouter } from "./router";
import { AppShell } from "./shell/AppShell";
import { isActive, PRIMARY_NAV, SECONDARY_NAV } from "./shell/navigation";

function SignedInPage() {
  const { path } = useRouter();
  if (path === "/settings" || path.startsWith("/settings/")) return <SettingsPage />;
  const item = [...PRIMARY_NAV, ...SECONDARY_NAV].find((entry) => isActive(entry, path));
  return <PlaceholderPage screen={item?.label ?? "nav.chat"} />;
}

export function App() {
  const { t } = useI18n();
  const { path } = useRouter();
  const { state, refresh } = useSession();

  // A device opened a pairing QR code / link. It normally has no session yet; if
  // it has one, completing the pairing replaces it (the Backend's replace_token).
  if (path === "/pair") return <PairPage />;

  switch (state.status) {
    case "loading":
      return (
        <main className="auth-page">
          <p className="muted" role="status">
            {t("app.loading")}
          </p>
        </main>
      );
    case "error":
      return (
        <main className="auth-page">
          <section className="panel auth-card">
            <p role="alert">{t("app.loadFailed")}</p>
            <button type="button" onClick={() => void refresh()}>
              {t("app.retry")}
            </button>
          </section>
        </main>
      );
    case "signed_out":
      return <LoginPage />;
    case "signed_in":
      if (state.data.auth && state.data.auth.passkey.gate !== "open") return <PasskeyGatePage />;
      return (
        <AppShell>
          <SignedInPage />
        </AppShell>
      );
  }
}
