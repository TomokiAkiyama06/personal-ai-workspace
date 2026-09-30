import { useSession } from "./auth/session";
import { type MessageKey, useI18n } from "./i18n";
import { NotificationsPage } from "./notifications/NotificationCenter";
import { useNotificationOwner } from "./notifications/store";
import { LoginPage } from "./pages/LoginPage";
import { PairPage } from "./pages/PairPage";
import { PasskeyGatePage } from "./pages/PasskeyGatePage";
import { PlaceholderPage } from "./pages/PlaceholderPage";
import { SettingsPage } from "./pages/SettingsPage";
import { useRouter } from "./router";
import { AppShell } from "./shell/AppShell";
import { isActive, NEW_CHAT_PATH, PRIMARY_NAV, SECONDARY_NAV } from "./shell/navigation";

// Screens of later issues that are reached from the user menu, not the navigation.
const OTHER_SCREENS: readonly { path: string; label: MessageKey }[] = [
  { path: NEW_CHAT_PATH, label: "nav.newChat" },
  { path: "/admin/usage", label: "screen.adminUsage" },
  { path: "/help/shortcuts", label: "screen.shortcuts" },
  { path: "/help", label: "screen.help" },
];

function SignedInPage() {
  const { path } = useRouter();
  if (path === "/notifications") return <NotificationsPage />;
  const other = OTHER_SCREENS.find((entry) => entry.path === path);
  if (other) return <PlaceholderPage screen={other.label} />;
  const item = [...PRIMARY_NAV, ...SECONDARY_NAV].find((entry) => isActive(entry, path));
  return <PlaceholderPage screen={item?.label ?? "nav.chat"} />;
}

export function App() {
  const { t } = useI18n();
  const { path } = useRouter();
  const { state, refresh } = useSession();
  useNotificationOwner(state.status === "signed_in" ? state.data.user.id : null);

  // A device opened a pairing QR code / link. It normally has no session yet; if
  // it has one, completing the pairing replaces it (the Backend's replace_token).
  if (path === "/pair") return <PairPage />;

  switch (state.status) {
    case "loading":
      return (
        <main className="center-page">
          <p className="muted" role="status">
            {t("app.loading")}
          </p>
        </main>
      );
    case "error":
      return (
        <main className="center-page">
          <section className="card center-card">
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
      // Settings is a screen of its own (the design's 「ワークスペースへ戻る」 header).
      if (path === "/settings" || path.startsWith("/settings/")) return <SettingsPage />;
      return (
        <AppShell>
          <SignedInPage />
        </AppShell>
      );
  }
}
