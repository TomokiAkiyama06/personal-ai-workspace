import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { SessionProvider } from "./auth/session";
import { apiHealthSource } from "./health/api";
import { HealthSourceProvider } from "./health/model";
import { I18nProvider } from "./i18n";
import { apiMemorySource } from "./memory/apiSource";
import { MemorySourceProvider } from "./memory/source";
import { NotificationProvider } from "./notifications/store";
import { RouterProvider } from "./router";
import { apiTaskSource } from "./tasks/apiSource";
import { TaskSourceProvider } from "./tasks/source";
import { ThemeProvider } from "./theme";
import { apiUsageSource } from "./usage/api";
import { UsageSourceProvider } from "./usage/model";
// IBM Plex Sans JP / IBM Plex Mono (the design's fonts), bundled into the build's
// assets so the page's CSP can stay font-src 'self' (no font CDN).
import "@fontsource/ibm-plex-sans-jp/400.css";
import "@fontsource/ibm-plex-sans-jp/500.css";
import "@fontsource/ibm-plex-sans-jp/600.css";
import "@fontsource/ibm-plex-mono/400.css";
import "@fontsource/ibm-plex-mono/500.css";
import "./styles.css";

const root = document.getElementById("root");
if (root) {
  createRoot(root).render(
    <StrictMode>
      <I18nProvider>
        <ThemeProvider>
          <RouterProvider>
            <SessionProvider>
              <NotificationProvider>
                <MemorySourceProvider source={apiMemorySource}>
                  <UsageSourceProvider source={apiUsageSource}>
                    <TaskSourceProvider source={apiTaskSource}>
                      <HealthSourceProvider source={apiHealthSource}>
                        <App />
                      </HealthSourceProvider>
                    </TaskSourceProvider>
                  </UsageSourceProvider>
                </MemorySourceProvider>
              </NotificationProvider>
            </SessionProvider>
          </RouterProvider>
        </ThemeProvider>
      </I18nProvider>
    </StrictMode>,
  );
}
