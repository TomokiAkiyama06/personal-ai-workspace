import { type FormEvent, useCallback, useEffect, useState } from "react";
import { authApi, type PairingIssued, type PendingPairing, type SessionInfo } from "../api/auth";
import { StepUpCancelledError, useStepUp } from "../auth/StepUp";
import { useSession } from "../auth/session";
import { useI18n } from "../i18n";
import { errorMessage } from "../i18n/errors";
import { QrCode } from "../shell/QrCode";

// How often the list of new devices waiting for an approval is re-read while a
// pairing that needs one is shown.
export const PENDING_POLL_MS = 5000;

function PendingApproval({
  item,
  onApprove,
  onReject,
  busy,
}: {
  item: PendingPairing;
  onApprove: (code: string) => void;
  onReject: () => void;
  busy: boolean;
}) {
  const { t, formatDate } = useI18n();
  const [code, setCode] = useState("");
  const submit = (event: FormEvent) => {
    event.preventDefault();
    onApprove(code);
  };
  return (
    <li>
      <p className="item-title">
        {t("devices.pendingItem", {
          name: item.device_name ?? t("devices.unnamed"),
          date: formatDate(item.claimed_at),
        })}
      </p>
      <form onSubmit={submit} className="inline-form">
        <label>
          {t("devices.code")}
          <input
            value={code}
            onChange={(event) => setCode(event.target.value)}
            autoComplete="off"
            autoCapitalize="characters"
            spellCheck={false}
            maxLength={32}
            required
          />
        </label>
        <button type="submit" disabled={busy || code.trim() === ""}>
          {t("devices.approve")}
        </button>
        <button type="button" className="secondary" onClick={onReject} disabled={busy}>
          {t("devices.reject")}
        </button>
      </form>
    </li>
  );
}

export function DevicesSection() {
  const { t, formatDate } = useI18n();
  const { signOut } = useSession();
  const { run, prompt } = useStepUp();
  const [sessions, setSessions] = useState<SessionInfo[] | null>(null);
  const [pairing, setPairing] = useState<PairingIssued | null>(null);
  const [pending, setPending] = useState<PendingPairing[]>([]);
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const loadSessions = useCallback(async () => {
    try {
      setSessions((await authApi.sessions()).sessions);
    } catch (caught) {
      setError(errorMessage(t, caught));
    }
  }, [t]);

  const loadPending = useCallback(async () => {
    try {
      setPending((await authApi.pendingPairings()).pending);
    } catch (caught) {
      setError(errorMessage(t, caught));
    }
  }, [t]);

  useEffect(() => {
    void loadSessions();
    void loadPending();
  }, [loadSessions, loadPending]);

  useEffect(() => {
    if (!pairing?.approval_required) return;
    const timer = window.setInterval(() => void loadPending(), PENDING_POLL_MS);
    return () => window.clearInterval(timer);
  }, [pairing, loadPending]);

  const act = async (action: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await action();
    } catch (caught) {
      if (!(caught instanceof StepUpCancelledError)) setError(errorMessage(t, caught));
    } finally {
      setBusy(false);
    }
  };

  const link = pairing ? new URL(pairing.link_path, window.location.origin).toString() : "";

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(link);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };

  return (
    <>
      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
      {notice && (
        <p className="notice" role="status">
          {notice}
        </p>
      )}
      {prompt}
      <section className="panel" aria-labelledby="pairing-title">
        <h2 id="pairing-title">{t("devices.add")}</h2>
        {pairing ? (
          <div className="pairing">
            <QrCode value={link} label={t("devices.qrLabel")} />
            <div className="stack">
              <p>{t("devices.addBody")}</p>
              <label>
                {t("devices.link")}
                <input readOnly value={link} onFocus={(event) => event.target.select()} />
              </label>
              <div className="actions">
                <button type="button" className="secondary" onClick={() => void copy()}>
                  {copied ? t("devices.copied") : t("devices.copy")}
                </button>
                <button
                  type="button"
                  className="secondary"
                  disabled={busy}
                  onClick={() =>
                    void act(async () => {
                      await authApi.revokePairing();
                      setPairing(null);
                      await loadPending();
                    })
                  }
                >
                  {t("devices.cancelPairing")}
                </button>
              </div>
              <p className="muted small">
                {t("devices.pairingExpires", { date: formatDate(pairing.expires_at) })}
              </p>
              {pairing.approval_required && (
                <p className="muted small">{t("devices.approvalRequired")}</p>
              )}
            </div>
          </div>
        ) : (
          <button
            type="button"
            disabled={busy}
            onClick={() =>
              void act(async () => {
                setCopied(false);
                setPairing(await authApi.issuePairing());
              })
            }
          >
            {t("devices.add")}
          </button>
        )}
      </section>

      <section className="panel" aria-labelledby="pending-title">
        <h2 id="pending-title">{t("devices.pending")}</h2>
        {pending.length === 0 ? (
          <p className="muted">{t("devices.pendingEmpty")}</p>
        ) : (
          <ul className="item-list">
            {pending.map((item) => (
              <PendingApproval
                key={item.pairing_id}
                item={item}
                busy={busy}
                onApprove={(code) =>
                  void act(async () => {
                    await run(() => authApi.approvePairing(item.pairing_id, code));
                    setNotice(t("devices.approved"));
                    setPairing(null);
                    await Promise.all([loadPending(), loadSessions()]);
                  })
                }
                onReject={() =>
                  void act(async () => {
                    await authApi.rejectPairing(item.pairing_id);
                    setNotice(t("devices.rejected"));
                    await loadPending();
                  })
                }
              />
            ))}
          </ul>
        )}
      </section>

      <section className="panel" aria-labelledby="sessions-title">
        <h2 id="sessions-title">{t("devices.sessions")}</h2>
        {sessions === null ? (
          <p className="muted">{t("app.loading")}</p>
        ) : (
          <ul className="item-list">
            {sessions.map((session) => (
              <li key={session.id}>
                <div>
                  <p className="item-title">
                    {session.device_name ?? t("devices.unnamed")}
                    {session.current && <span className="tag">{t("devices.current")}</span>}
                  </p>
                  <p className="muted small">
                    {t("devices.lastActive", { date: formatDate(session.last_used_at) })} ·{" "}
                    {t("devices.expires", { date: formatDate(session.expires_at) })}
                  </p>
                </div>
                <button
                  type="button"
                  className="secondary"
                  disabled={busy}
                  onClick={() =>
                    session.current
                      ? void signOut()
                      : void act(async () => {
                          await authApi.revokeSession(session.id);
                          await loadSessions();
                        })
                  }
                >
                  {session.current ? t("user.logout") : t("devices.revoke")}
                </button>
              </li>
            ))}
          </ul>
        )}
        {sessions?.some((session) => !session.current) && (
          <button
            type="button"
            className="danger"
            disabled={busy}
            onClick={() =>
              void act(async () => {
                const { revoked } = await authApi.revokeOtherSessions();
                setNotice(t("devices.revokedOthers", { count: revoked }));
                await loadSessions();
              })
            }
          >
            {t("devices.revokeOthers")}
          </button>
        )}
      </section>
    </>
  );
}
