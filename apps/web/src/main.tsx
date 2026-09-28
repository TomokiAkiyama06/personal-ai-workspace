import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { SessionProvider } from "./auth/session";
import { I18nProvider } from "./i18n";
import { NotificationProvider } from "./notifications/store";
import { RouterProvider } from "./router";
import "./styles.css";

const root = document.getElementById("root");
if (root) {
  createRoot(root).render(
    <StrictMode>
      <I18nProvider>
        <RouterProvider>
          <SessionProvider>
            <NotificationProvider>
              <App />
            </NotificationProvider>
          </SessionProvider>
        </RouterProvider>
      </I18nProvider>
    </StrictMode>,
  );
}
