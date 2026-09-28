import { type FormEvent, useCallback, useEffect, useState } from "react";
import { authApi, PASSKEY_NAME_MAX, type Passkey } from "../api/auth";
import { StepUpCancelledError, useStepUp } from "../auth/StepUp";
import { useSession, useSignedIn } from "../auth/session";
import { createPasskey } from "../auth/webauthn";
import { useI18n } from "../i18n";
import { errorMessage } from "../i18n/errors";

export function SecuritySection() {
  const { t, formatDate } = useI18n();
  const { accept, expired } = useSession();
  const data = useSignedIn();
  const { run, prompt } = useStepUp();
  const [passkeys, setPasskeys] = useState<Passkey[] | null>(null);
  const [name, setName] = useState("");
  const [confirming, setConfirming] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const requirement = data.auth?.passkey;

  const load = useCallback(async () => {
    try {
      setPasskeys((await authApi.passkeys()).passkeys);
    } catch (caught) {
      setError(errorMessage(t, caught));
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
      const credential = await createPasskey(options);
      const result = await authApi.passkeyEnrollFinish(credential, name.trim() || null);
      if (result.session) accept(result.session);
      setName("");
      setNotice(t("passkey.registered"));
      await load();
    } catch (caught) {
      fail(caught);
    } finally {
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
      setNotice(t("security.revoked"));
      await load();
    } catch (caught) {
      fail(caught);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="panel" aria-labelledby="passkeys-title">
      <h2 id="passkeys-title">{t("security.title")}</h2>
      {requirement?.requirement === "required" && (
        <p className="muted">{t("security.requirement.required")}</p>
      )}
      {requirement?.recommended && !requirement.enrolled && (
        <p className="muted">{t("security.requirement.recommended")}</p>
      )}
      {requirement && !requirement.available && (
        <p className="muted">{t("error.passkey_unavailable")}</p>
      )}
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
      {passkeys === null ? (
        <p className="muted">{t("app.loading")}</p>
      ) : passkeys.length === 0 ? (
        <p className="muted">{t("security.empty")}</p>
      ) : (
        <ul className="item-list">
          {passkeys.map((passkey) => (
            <li key={passkey.id}>
              <div>
                <p className="item-title">
                  {passkey.name}
                  {passkey.backup_eligible && <span className="tag">{t("security.synced")}</span>}
                </p>
                <p className="muted small">
                  {t("security.createdAt", { date: formatDate(passkey.created_at) })} ·{" "}
                  {passkey.last_used_at
                    ? t("security.lastUsedAt", { date: formatDate(passkey.last_used_at) })
                    : t("security.neverUsed")}
                </p>
              </div>
              {confirming === passkey.id ? (
                <div className="actions">
                  <span>{t("security.revokeConfirm", { name: passkey.name })}</span>
                  <button
                    type="button"
                    className="danger"
                    onClick={() => void revoke(passkey)}
                    disabled={busy}
                  >
                    {t("security.revoke")}
                  </button>
                  <button type="button" className="secondary" onClick={() => setConfirming(null)}>
                    {t("common.cancel")}
                  </button>
                </div>
              ) : (
                <button
                  type="button"
                  className="secondary"
                  onClick={() => setConfirming(passkey.id)}
                  disabled={busy}
                >
                  {t("security.revoke")}
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
      {requirement?.available !== false && (
        <form onSubmit={register} className="inline-form">
          <label>
            {t("passkey.name")}
            <input
              value={name}
              onChange={(event) => setName(event.target.value)}
              maxLength={PASSKEY_NAME_MAX}
            />
          </label>
          <button type="submit" disabled={busy}>
            {busy ? t("passkey.registering") : t("passkey.register")}
          </button>
        </form>
      )}
    </section>
  );
}
