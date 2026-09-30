// New devices waiting for this account's approval, as notifications
// (GET /api/v1/auth/pairing/pending, the only "needs a human" item the Backend
// exposes today). One entry per pairing; it goes away when the pairing is no
// longer pending (approved / rejected on another screen or device, or expired).
//
// The notification only links to 設定 › 端末とセッション: approving needs the
// confirmation code and a Passkey Step-up there (NOTIFICATION_POLICY §6), so
// this never approves or rejects anything itself.
import { useEffect } from "react";
import { authApi } from "../api/auth";
import { useI18n } from "../i18n";
import { useNotifications } from "./store";

export const PENDING_APPROVAL_POLL_MS = 30_000;
const KEY_PREFIX = "pairing:";

export function usePendingApprovalNotifications(): void {
  const { t } = useI18n();
  const { push, resolve } = useNotifications();

  useEffect(() => {
    let cancelled = false;
    let inFlight = false;
    const shown = new Set<string>();

    const load = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const { pending } = await authApi.pendingPairings();
        if (cancelled) return;
        const current = new Set<string>();
        for (const item of pending) {
          const key = `${KEY_PREFIX}${item.pairing_id}`;
          current.add(key);
          push({
            key,
            id: item.pairing_id,
            severity: "warning",
            title: t("notifications.pairing.title"),
            body: t("notifications.pairing.body", {
              device: item.device_name ?? t("notifications.pairing.unnamed"),
            }),
            source: t("notifications.pairing.source"),
            category: "system",
            at: item.claimed_at,
            actions: [
              { label: t("notifications.pairing.review"), to: "/settings/devices", primary: true },
            ],
          });
        }
        for (const key of shown) {
          if (!current.has(key)) resolve(key);
        }
        shown.clear();
        for (const key of current) shown.add(key);
      } catch {
        // Only a hint: a failed read keeps what is shown until the next one.
      } finally {
        inFlight = false;
      }
    };

    void load();
    const timer = window.setInterval(() => {
      if (document.visibilityState !== "hidden") void load();
    }, PENDING_APPROVAL_POLL_MS);
    const onVisible = () => {
      if (document.visibilityState === "visible") void load();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [t, push, resolve]);
}
