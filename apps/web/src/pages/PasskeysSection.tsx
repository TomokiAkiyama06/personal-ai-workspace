// The Passkey card of 設定 › 端末とセッション (the design's SettingsDevices) and
// its registration panel (PasskeyStates C).
import { type FormEvent, useCallback, useEffect, useId, useRef, useState } from "react";
import { authApi, PASSKEY_NAME_MAX, type Passkey } from "../api/auth";
import { StepUpCancelledError, useStepUp } from "../auth/StepUp";
import { useSession, useSignedIn } from "../auth/session";
import { createPasskey } from "../auth/webauthn";
import { useI18n } from "../i18n";
import { errorMessage } from "../i18n/errors";

export function PasskeysSection({ onSessionsChanged }: { onSessionsChanged?: () => void }) {
  const { t, formatDate } = useI18n();
  const { accept, expired, refresh } = useSession();
  const data = useSignedIn();
  const { run, prompt } = useStepUp();
  const titleId = useId();
  const [passkeys, setPasskeys] = useState<Passkey[] | null>(null);
  const [enrolling, setEnrolling] = useState(false);
  const [name, setName] = useState("");
  const [confirming, setConfirming] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [verifying, setVerifying] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const requirement = data.auth?.passkey;

  // Only the newest read counts: an earlier one answering after a registration
  // or removal must not bring the old list back.
  const reads = useRef(0);
  const load = useCallback(async () => {
    const mine = ++reads.current;
    try {
      const { passkeys: next } = await authApi.passkeys();
      if (mine === reads.current) setPasskeys(next);
    } catch (caught) {
      if (mine === reads.current) setError(errorMessage(t, caught));
    }
  }, [t]);

  useEffect(() => {
    void load();
  }, [load]);

  const fail = (caught: unknown) => {
    if (!(caught instanceof StepUpCancelledError)) setError(errorMessage(t, caught));
  };

  const register = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      // The step-up is checked when the ceremony begins, so only `begin` is retried.
      const { options } = await run(() => authApi.passkeyEnrollBegin());
      setVerifying(true);
      const credential = await createPasskey(options);
      const result = await authApi.passkeyEnrollFinish(credential, name.trim() || null);
      // No replacement session (no rotation was needed): read the new passkey
      // state, so the Step-up offers the passkey and the recommendation goes.
      if (result.session) accept(result.session);
      else await refresh();
      setName("");
      setEnrolling(false);
      setNotice(t("passkey.registered"));
      await load();
    } catch (caught) {
      fail(caught);
    } finally {
      setVerifying(false);
      setBusy(false);
    }
  };

  const revoke = async (passkey: Passkey) => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const result = await run(() => authApi.revokePasskey(passkey.id));
      setConfirming(null);
      if (result.signed_out) {
        expired();
        return;
      }
      setNotice(t("passkeys.revoked"));
      // Removing a passkey can change this session's passkey state (the last
      // one under an optional policy) and end other sessions (sessions_ended).
      await Promise.all([load(), refresh()]);
      if (result.sessions_ended > 0) onSessionsChanged?.();
    } catch (caught) {
      fail(caught);
    } finally {
      setBusy(false);
    }
  };

  const canAdd = requirement?.available !== false;
  return (
    <section className="card" aria-labelledby={titleId}>
      <div className="card-head">
        <h2 id={titleId}>{t("passkeys.title")}</h2>
        {requirement?.requirement === "required" && (
          <span className="muted small">{t("passkeys.required")}</span>
        )}
        {requirement?.recommended && !requirement.enrolled && (
          <span className="muted small">{t("passkeys.recommended")}</span>
        )}
        {canAdd && !enrolling && (
          <button
            type="button"
            className="secondary small-button push-right"
            onClick={() => {
              setEnrolling(true);
              setNotice(null);
            }}
            disabled={busy}
          >
            {t("passkeys.add")}
          </button>
        )}
      </div>
      {requirement && !requirement.available && (
        <p className="card-row muted">{t("error.passkey_unavailable")}</p>
      )}
      {(error || notice || prompt || verifying) && (
        <div className="card-row stack">
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
          {verifying && (
            <p className="notice" role="status">
              <strong>{t("passkey.verifying")}</strong> {t("passkey.verifyingBody")}
            </p>
          )}
          {prompt}
        </div>
      )}
      {enrolling && (
        <form className="card-row enroll-panel" onSubmit={register}>
          <h3>{t("passkey.enrollTitle")}</h3>
          <p className="muted">{t("passkey.enrollBody")}</p>
          <label className="field">
            <span>{t("passkey.name")}</span>
            <input
              value={name}
              onChange={(event) => setName(event.target.value)}
              maxLength={PASSKEY_NAME_MAX}
            />
          </label>
          <div className="actions">
            <button type="submit" disabled={busy}>
              {busy ? t("passkey.registering") : t("passkey.register")}
            </button>
            <button
              type="button"
              className="text-button"
              onClick={() => setEnrolling(false)}
              disabled={busy}
            >
              {t("passkey.later")}
            </button>
          </div>
        </form>
      )}
      {passkeys === null ? (
        <p className="card-row muted">{t("app.loading")}</p>
      ) : passkeys.length === 0 ? (
        <p className="card-row muted">{t("passkeys.empty")}</p>
      ) : (
        <ul className="table-list">
          {passkeys.map((passkey) => (
            <li key={passkey.id} className="table-row passkey-row">
              <span className="cell-main">{passkey.name}</span>
              <span className="mono muted">
                {t("passkeys.addedAt", { date: formatDate(passkey.created_at) })}
              </span>
              <span className="mono muted">
                {passkey.last_used_at
                  ? t("passkeys.lastUsedAt", { date: formatDate(passkey.last_used_at) })
                  : t("passkeys.neverUsed")}
              </span>
              <span className="muted small">
                {passkey.backup_eligible ? t("passkeys.synced") : t("passkeys.deviceBound")}
              </span>
              <span className="cell-actions">
                {confirming === passkey.id ? (
                  <>
                    <button
                      type="button"
                      className="danger small-button"
                      onClick={() => void revoke(passkey)}
                      disabled={busy}
                    >
                      {t("passkeys.revoke")}
                    </button>
                    <button
                      type="button"
                      className="text-button small-button"
                      onClick={() => setConfirming(null)}
                    >
                      {t("common.cancel")}
                    </button>
                  </>
                ) : (
                  <button
                    type="button"
                    className="danger small-button"
                    onClick={() => setConfirming(passkey.id)}
                    disabled={busy}
                  >
                    {t("passkeys.revoke")}
                  </button>
                )}
              </span>
              {confirming === passkey.id && (
                <span className="row-note">
                  {t("passkeys.revokeConfirm", { name: passkey.name })}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
