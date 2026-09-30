import { type FormEvent, useEffect, useId, useState } from "react";
import { authApi, DEVICE_NAME_MAX } from "../api/auth";
import { isApiError } from "../api/client";
import { describeDevice } from "../auth/device";
import { useSession } from "../auth/session";
import { useI18n } from "../i18n";
import { errorMessage } from "../i18n/errors";
import { Link, useRouter } from "../router";
import { AuthLayout } from "./AuthLayout";

// How often a new device that waits for its approval asks whether it came. A
// correct claim gives its rate-limit attempt back (Decision 0033), so polling
// does not lock the device out.
export const COMPLETE_POLL_MS = 3000;

/**
 * The Backend's answers that end a pairing for good: 400 `invalid_token` (the
 * pairing was refused, revoked, expired, used or locked: paw_backend
 * auth/onboarding/pairing.py `_finish`) and 404 `not_found`. Polling after them
 * would only burn rate-limit attempts, which a refused call does not give back.
 */
function isTerminal(error: unknown): boolean {
  return isApiError(error, "invalid_token", "not_found");
}

/** The pairing token of `/pair#<token>` (a fragment never reaches the server's logs). */
function tokenFromLocation(): string | null {
  const raw = window.location.hash.replace(/^#/, "");
  if (!raw) return null;
  try {
    return decodeURIComponent(raw);
  } catch {
    return null;
  }
}

type Stage =
  | { name: "form" }
  | { name: "waiting"; claim: string; code: string | null; expiresAt: string | null }
  | { name: "ended" };

/** The new device's side of a pairing (a QR code / link from a trusted device). */
export function PairPage() {
  const { t, formatDate } = useI18n();
  const { accept } = useSession();
  const { navigate } = useRouter();
  const [token] = useState(tokenFromLocation);
  const [deviceName, setDeviceName] = useState(describeDevice);
  const [rememberMe, setRememberMe] = useState(false);
  const titleId = useId();
  const [stage, setStage] = useState<Stage>({ name: "form" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const claim = stage.name === "waiting" ? stage.claim : null;

  // Once read, the one-time token leaves the address bar and the history entry.
  useEffect(() => {
    if (window.location.hash) window.history.replaceState(null, "", window.location.pathname);
  }, []);

  useEffect(() => {
    if (claim === null) return;
    let cancelled = false;
    let timer: number | undefined;
    const poll = async (delay: number) => {
      timer = window.setTimeout(async () => {
        try {
          const progress = await authApi.completePairing(claim);
          if (cancelled) return;
          if (progress.status === "completed" && progress.session) {
            accept(progress.session);
            navigate("/", { replace: true });
            return;
          }
          void poll(COMPLETE_POLL_MS);
        } catch (caught) {
          if (cancelled) return;
          if (isApiError(caught, "rate_limited")) {
            void poll(Math.max(COMPLETE_POLL_MS, (caught.retryAfterSeconds ?? 0) * 1000));
          } else if (isTerminal(caught)) {
            setError(null);
            setStage({ name: "ended" });
          } else {
            setError(errorMessage(t, caught));
            void poll(COMPLETE_POLL_MS);
          }
        }
      }, delay);
    };
    void poll(COMPLETE_POLL_MS);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [claim, accept, navigate, t]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!token) return;
    setBusy(true);
    setError(null);
    try {
      const progress = await authApi.claimPairing({
        token,
        device_name: deviceName.trim(),
        remember_me: rememberMe,
      });
      if (progress.status === "completed" && progress.session) {
        accept(progress.session);
        navigate("/", { replace: true });
        return;
      }
      if (progress.claim) {
        setStage({
          name: "waiting",
          claim: progress.claim,
          code: progress.confirmation_code,
          expiresAt: progress.expires_at,
        });
      }
    } catch (caught) {
      if (isTerminal(caught)) setStage({ name: "ended" });
      else setError(errorMessage(t, caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <AuthLayout>
      <section className="stack-lg" aria-labelledby={titleId}>
        <div className="stack-xs">
          <h1 id={titleId}>{t("pair.title")}</h1>
          {token && stage.name === "form" && <p className="muted">{t("pair.intro")}</p>}
        </div>
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        {!token || stage.name === "ended" ? (
          <>
            <p role={token ? "status" : undefined}>
              {token ? t("pair.expired") : t("pair.noToken")}
            </p>
            <Link to="/">{t("pair.toSignIn")}</Link>
          </>
        ) : stage.name === "waiting" ? (
          <div className="stack" aria-live="polite">
            <p>{t("pair.waiting")}</p>
            {stage.code && (
              <div className="confirmation-code">
                <span className="section-label">{t("pair.codeLabel")}</span>
                <output aria-label={t("pair.codeLabel")}>{stage.code}</output>
                <span className="muted small">{t("pair.codeHint")}</span>
              </div>
            )}
            {stage.expiresAt && (
              <p className="muted small">
                {t("pair.expires", { date: formatDate(stage.expiresAt) })}
              </p>
            )}
          </div>
        ) : (
          <form onSubmit={submit} className="stack-lg">
            <label className="field">
              <span>{t("pair.deviceName")}</span>
              <input
                value={deviceName}
                onChange={(event) => setDeviceName(event.target.value)}
                maxLength={DEVICE_NAME_MAX}
                required
              />
            </label>
            <label className="checkbox">
              <input
                type="checkbox"
                checked={rememberMe}
                onChange={(event) => setRememberMe(event.target.checked)}
              />
              {t("pair.trust")}
            </label>
            <button type="submit" className="wide" disabled={busy || deviceName.trim() === ""}>
              {t("pair.submit")}
            </button>
          </form>
        )}
      </section>
    </AuthLayout>
  );
}
