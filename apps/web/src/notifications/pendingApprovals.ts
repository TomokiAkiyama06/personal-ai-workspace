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
  const { push, resolveMatching } = useNotifications();

  useEffect(() => {
    let cancelled = false;
    let inFlight = false;

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
        // From the store, not from this mount: the device may have been approved
        // while this was unmounted (設定 is a screen outside the shell).
        resolveMatching((key) => key.startsWith(KEY_PREFIX) && !current.has(key));
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
  }, [t, push, resolveMatching]);
}
