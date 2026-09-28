import { type FormEvent, useState } from "react";
import { authApi, DEVICE_NAME_MAX, LOGIN_NAME_MAX } from "../api/auth";
import { useSession } from "../auth/session";
import { useI18n } from "../i18n";
import { errorMessage } from "../i18n/errors";

export function LoginPage() {
  const { t } = useI18n();
  const { state, accept } = useSession();
  const [loginName, setLoginName] = useState("");
  const [password, setPassword] = useState("");
  const [deviceName, setDeviceName] = useState("");
  const [rememberMe, setRememberMe] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(
    state.status === "signed_out" && state.reason === "expired" ? t("error.unauthorized") : null,
  );

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const session = await authApi.login({
        login_name: loginName,
        password,
        remember_me: rememberMe,
        device_name: deviceName.trim() || null,
      });
      setPassword("");
      accept(session);
    } catch (caught) {
      setError(errorMessage(t, caught));
      setBusy(false);
    }
  };

  return (
    <main className="auth-page">
      <form className="panel auth-card" onSubmit={submit} aria-labelledby="login-title">
        <p className="brand">{t("app.name")}</p>
        <h1 id="login-title">{t("login.title")}</h1>
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        <label>
          {t("login.loginName")}
          <input
            name="username"
            autoComplete="username"
            value={loginName}
            onChange={(event) => setLoginName(event.target.value)}
            required
            maxLength={LOGIN_NAME_MAX}
          />
        </label>
        <label>
          {t("login.password")}
          <input
            name="password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            required
          />
        </label>
        <label>
          {t("login.deviceName")}
          <input
            name="device"
            value={deviceName}
            onChange={(event) => setDeviceName(event.target.value)}
            maxLength={DEVICE_NAME_MAX}
          />
        </label>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={rememberMe}
            onChange={(event) => setRememberMe(event.target.checked)}
          />
          {t("login.rememberMe")}
        </label>
        <button type="submit" disabled={busy}>
          {busy ? t("login.submitting") : t("login.submit")}
        </button>
        <p className="muted small">{t("login.pairHint")}</p>
      </form>
    </main>
  );
}
