import { type FormEvent, useState } from "react";
import { authApi, PASSKEY_NAME_MAX } from "../api/auth";
import { useSession, useSignedIn } from "../auth/session";
import { createPasskey, getPasskey } from "../auth/webauthn";
import { useI18n } from "../i18n";
import { errorMessage } from "../i18n/errors";
import { AuthLayout } from "./AuthLayout";

/** A session the passkey policy restricts: register or use a passkey first (Decision 0025). */
export function PasskeyGatePage() {
  const { t } = useI18n();
  const { accept, refresh, signOut } = useSession();
  const data = useSignedIn();
  const passkey = data.auth?.passkey;
  const enrolling = passkey?.gate === "enrollment_required";
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const register = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const { options } = await authApi.passkeyEnrollBegin();
      const credential = await createPasskey(options);
      const result = await authApi.passkeyEnrollFinish(credential, name.trim() || null);
      // Registering lifts the gate and rotates the session (the new state is here).
      if (result.session) accept(result.session);
      else await refresh();
    } catch (caught) {
      setError(errorMessage(t, caught));
      setBusy(false);
    }
  };

  const authenticate = async () => {
    setBusy(true);
    setError(null);
    try {
      const { options } = await authApi.passkeyAuthenticateBegin();
      accept(await authApi.passkeyAuthenticateFinish(await getPasskey(options)));
    } catch (caught) {
      setError(errorMessage(t, caught));
      setBusy(false);
    }
  };

  return (
    <AuthLayout>
      <section className="stack-lg" aria-labelledby="gate-title">
        <h1 id="gate-title">{t("passkeyGate.title")}</h1>
        {passkey && !passkey.available ? (
          <p>{t("passkeyGate.unavailable")}</p>
        ) : (
          <p>{enrolling ? t("passkeyGate.enrollBody") : t("passkeyGate.assertBody")}</p>
        )}
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        {passkey?.available !== false &&
          (enrolling ? (
            <form onSubmit={register} className="stack">
              <label className="field">
                <span>{t("passkey.name")}</span>
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
          ) : (
            <button type="button" className="wide" onClick={authenticate} disabled={busy}>
              {t("passkeyGate.authenticate")}
            </button>
          ))}
        {busy && (
          <p className="notice" role="status">
            <strong>{t("passkey.verifying")}</strong> {t("passkey.verifyingBody")}
          </p>
        )}
        <button type="button" className="secondary" onClick={() => void signOut()}>
          {t("user.signOut")}
        </button>
      </section>
    </AuthLayout>
  );
}
