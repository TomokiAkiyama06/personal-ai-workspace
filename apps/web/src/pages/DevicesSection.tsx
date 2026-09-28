// 設定 › 端末とセッション (the design's SettingsDevices, DeviceAdd step 1 and 3):
// 新しい端末を追加 (a one-time QR code / link), the trusted devices (the signed-in
// sessions and the devices waiting for this account's approval), the passkeys and
// 他のすべての端末からサインアウト.
import { type FormEvent, useCallback, useEffect, useId, useRef, useState } from "react";
import { authApi, type PairingIssued, type PendingPairing, type SessionInfo } from "../api/auth";
import { StepUpCancelledError, useStepUp } from "../auth/StepUp";
import { useSession } from "../auth/session";
import { useI18n } from "../i18n";
import { errorMessage } from "../i18n/errors";
import { Icon } from "../shell/icons";
import { QrCode } from "../shell/QrCode";
import { PasskeysSection } from "./PasskeysSection";

// How often the list of new devices waiting for an approval is re-read while a
// pairing that needs one is shown.
export const PENDING_POLL_MS = 5000;

/** Seconds left until `iso`, ticking every second while mounted. */
function useSecondsLeft(iso: string | null): number | null {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!iso) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [iso]);
  if (!iso) return null;
  const end = new Date(iso).getTime();
  if (Number.isNaN(end)) return null;
  return Math.max(0, Math.floor((end - now) / 1000));
}

function mmss(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

function PairingCard({
  pairing,
  busy,
  onCancel,
}: {
  pairing: PairingIssued;
  busy: boolean;
  onCancel: () => void;
}) {
  const { t } = useI18n();
  const titleId = useId();
  const [copied, setCopied] = useState(false);
  const left = useSecondsLeft(pairing.expires_at);
  const link = new URL(pairing.link_path, window.location.origin).toString();
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(link);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };
  return (
    <section className="card pairing-card" aria-labelledby={titleId}>
      <div className="card-head">
        <h2 id={titleId}>{t("devices.addTitle")}</h2>
      </div>
      <div className="card-row pairing">
        <QrCode value={link} label={t("devices.qrLabel")} />
        <div className="stack">
          <p className="muted">{t("devices.addBody")}</p>
          <label className="field">
            <span>{t("devices.link")}</span>
            <input readOnly value={link} onFocus={(event) => event.target.select()} />
          </label>
          <p className="mono countdown" aria-live="off">
            {left === 0
              ? t("devices.pairingExpired")
              : left !== null && t("devices.expiresIn", { time: mmss(left) })}
          </p>
          {pairing.approval_required && (
            <p className="muted small">{t("devices.approvalRequired")}</p>
          )}
          <div className="actions">
            <button type="button" className="secondary" onClick={() => void copy()}>
              {copied ? t("devices.copied") : t("devices.copy")}
            </button>
            <button type="button" className="danger" disabled={busy} onClick={onCancel}>
              {t("devices.cancelPairing")}
            </button>
          </div>
        </div>
      </div>
    </section>
  );
}

function PendingRow({
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
  const { t, formatDate, formatTime } = useI18n();
  const formId = useId();
  const [code, setCode] = useState("");
  const submit = (event: FormEvent) => {
    event.preventDefault();
    onApprove(code);
  };
  // One grid row like the others (端末 / 最終アクティブ / 有効期限 / buttons), and
  // below it the confirmation code the Backend requires (Decision 0033).
  return (
    <li className="table-row device-row pending-row">
      <span className="cell-main">
        <span className="device-icon warning">
          <Icon name="phone" />
        </span>
        <span>{item.device_name ?? t("devices.unnamed")}</span>
        <span className="status-chip warning">{t("devices.pendingTag")}</span>
      </span>
      <span className="mono muted">
        {t("devices.requestedAt", { time: formatTime(item.claimed_at) })}
      </span>
      <span className="mono muted">{formatDate(item.expires_at)}</span>
      <span className="cell-actions">
        <button
          type="submit"
          form={formId}
          className="small-button"
          disabled={busy || code.trim() === ""}
        >
          {t("devices.approve")}
        </button>
        <button type="button" className="danger small-button" onClick={onReject} disabled={busy}>
          {t("devices.reject")}
        </button>
      </span>
      <form id={formId} onSubmit={submit} className="approve-code">
        <label className="field compact">
          <span>{t("devices.code")}</span>
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
      </form>
    </li>
  );
}

export function DevicesSection() {
  const { t, formatDate } = useI18n();
  const { signOut } = useSession();
  const { run, prompt } = useStepUp();
  const trustedId = useId();
  const othersId = useId();
  const [sessions, setSessions] = useState<SessionInfo[] | null>(null);
  const [pairing, setPairing] = useState<PairingIssued | null>(null);
  const [pending, setPending] = useState<PendingPairing[]>([]);
  // A new device appears only when its own pairing completes: right after its
  // claim when no approval is needed, or after its own poll once approved. The
  // list is re-read until a session not known before shows up or the pairing
  // expires; on the no-approval branch the spent QR code / link then goes.
  const [awaiting, setAwaiting] = useState<{
    until: number;
    known: Set<string>;
    dropsPairing: boolean;
  } | null>(null);
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

  // Only the newest read of the waiting devices counts: a poll that was in
  // flight during an approval / refusal must not bring the decided one back.
  const pendingReads = useRef(0);
  const loadPending = useCallback(async () => {
    const mine = ++pendingReads.current;
    try {
      const { pending: next } = await authApi.pendingPairings();
      if (mine === pendingReads.current) setPending(next);
    } catch (caught) {
      if (mine === pendingReads.current) setError(errorMessage(t, caught));
    }
  }, [t]);

  useEffect(() => {
    void loadSessions();
    void loadPending();
  }, [loadSessions, loadPending]);

  useEffect(() => {
    if (!awaiting) return;
    if (sessions?.some((session) => !awaiting.known.has(session.id))) {
      setAwaiting(null);
      if (awaiting.dropsPairing) {
        setPairing(null);
        setNotice(t("devices.paired"));
      }
      return;
    }
    if (Date.now() >= awaiting.until) {
      setAwaiting(null);
      return;
    }
    const timer = window.setTimeout(() => void loadSessions(), PENDING_POLL_MS);
    return () => window.clearTimeout(timer);
  }, [awaiting, sessions, loadSessions, t]);

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

  const watchForNewSession = (expiresAt: string, dropsPairing: boolean) => {
    const until = new Date(expiresAt).getTime();
    setAwaiting({
      until: Number.isNaN(until) ? Date.now() : until,
      known: new Set((sessions ?? []).map((session) => session.id)),
      dropsPairing,
    });
  };

  const others = sessions?.filter((session) => !session.current).length ?? 0;
  return (
    <div className="settings-content">
      <div className="page-head">
        <div>
          <h1>{t("settings.devices")}</h1>
          <p className="muted">{t("devices.body")}</p>
        </div>
        {!pairing && (
          <button
            type="button"
            className="push-right"
            disabled={busy}
            onClick={() =>
              void act(async () => {
                const issued = await authApi.issuePairing();
                setPairing(issued);
                // Without an approval the claim itself signs the new device in.
                if (!issued.approval_required) watchForNewSession(issued.expires_at, true);
              })
            }
          >
            <Icon name="plus" size={15} />
            {t("devices.add")}
          </button>
        )}
      </div>
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
      {pairing && (
        <PairingCard
          pairing={pairing}
          busy={busy}
          onCancel={() =>
            void act(async () => {
              await authApi.revokePairing();
              setPairing(null);
              setAwaiting(null);
              await loadPending();
            })
          }
        />
      )}

      <section className="card" aria-labelledby={trustedId}>
        <div className="card-head">
          <h2 id={trustedId}>{t("devices.trusted")}</h2>
          {sessions && <span className="count-chip">{sessions.length + pending.length}</span>}
        </div>
        <div className="table-head device-row" aria-hidden="true">
          <span>{t("devices.col.device")}</span>
          <span>{t("devices.col.lastActive")}</span>
          <span>{t("devices.col.expires")}</span>
          <span />
        </div>
        {sessions === null ? (
          <p className="card-row muted">{t("app.loading")}</p>
        ) : (
          <ul className="table-list">
            {sessions.map((session) => (
              <li
                key={session.id}
                className={
                  session.current ? "table-row device-row current" : "table-row device-row"
                }
              >
                <span className="cell-main">
                  <span className="device-icon">
                    <Icon name="laptop" />
                  </span>
                  <span className={session.current ? "strong" : undefined}>
                    {session.device_name ?? t("devices.unnamed")}
                  </span>
                  {session.current && (
                    <span className="status-chip ok">{t("devices.current")}</span>
                  )}
                </span>
                <span className="mono muted">
                  {session.current ? t("devices.now") : formatDate(session.last_used_at)}
                </span>
                <span className="mono muted">{formatDate(session.expires_at)}</span>
                <span className="cell-actions">
                  <button
                    type="button"
                    className="secondary small-button"
                    disabled={busy}
                    onClick={() =>
                      session.current
                        ? void act(signOut)
                        : void act(async () => {
                            await authApi.revokeSession(session.id);
                            await loadSessions();
                          })
                    }
                  >
                    {t("devices.signOut")}
                  </button>
                </span>
              </li>
            ))}
            {pending.map((item) => (
              <PendingRow
                key={item.pairing_id}
                item={item}
                busy={busy}
                onApprove={(code) =>
                  void act(async () => {
                    // Approving a device always needs a passkey step-up (Decision 0033).
                    await run(() => authApi.approvePairing(item.pairing_id, code), {
                      passkeyOnly: true,
                    });
                    setNotice(t("devices.approved"));
                    setPairing(null);
                    watchForNewSession(item.expires_at, false);
                    await Promise.all([loadPending(), loadSessions()]);
                  })
                }
                onReject={() =>
                  void act(async () => {
                    await authApi.rejectPairing(item.pairing_id);
                    setNotice(t("devices.rejected"));
                    // The refused claim ends that pairing: its QR code / link is dead.
                    setPairing(null);
                    await loadPending();
                  })
                }
              />
            ))}
          </ul>
        )}
      </section>

      <PasskeysSection onSessionsChanged={() => void loadSessions()} />

      {others > 0 && (
        <section className="card danger-zone" aria-labelledby={othersId}>
          <div className="danger-zone-text">
            <h2 id={othersId}>{t("devices.revokeOthers")}</h2>
            <p className="muted small">{t("devices.revokeOthersBody", { count: others })}</p>
          </div>
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
            {t("devices.revokeOthersRun")}
          </button>
        </section>
      )}
    </div>
  );
}
