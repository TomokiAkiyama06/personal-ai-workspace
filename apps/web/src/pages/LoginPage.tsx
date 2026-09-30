// サインイン (the design's Login / LoginLight / MobileLogin). The Backend signs in
// with a user name and password only (a passkey is a gate or step-up after it,
// Decision 0025), so the design's "Passkey でサインイン" is not shown (Decision 0044,
// design deviations).
import { type FormEvent, useId, useState } from "react";
import { authApi, DEVICE_NAME_MAX, LOGIN_NAME_MAX } from "../api/auth";
import { describeDevice } from "../auth/device";
import { useSession } from "../auth/session";
import { useI18n } from "../i18n";
import { errorMessage } from "../i18n/errors";
import { Link } from "../router";
import { Icon } from "../shell/icons";
import { AuthLayout } from "./AuthLayout";

export function LoginPage() {
  const { t } = useI18n();
  const { state, accept } = useSession();
  const titleId = useId();
  const [loginName, setLoginName] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [trust, setTrust] = useState(false);
  const [forgot, setForgot] = useState(false);
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
        remember_me: trust,
        device_name: describeDevice().slice(0, DEVICE_NAME_MAX) || null,
      });
      setPassword("");
      accept(session);
    } catch (caught) {
      setError(errorMessage(t, caught));
      setBusy(false);
    }
  };

  return (
    <AuthLayout>
      <form className="stack-lg" onSubmit={submit} aria-labelledby={titleId}>
        <div className="stack-xs">
          <h1 id={titleId}>{t("login.title")}</h1>
          <p className="muted">{t("login.subtitle")}</p>
        </div>
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        <label className="field">
          <span>{t("login.loginName")}</span>
          <input
            name="username"
            autoComplete="username"
            value={loginName}
            onChange={(event) => setLoginName(event.target.value)}
            required
            maxLength={LOGIN_NAME_MAX}
          />
        </label>
        <div className="field">
          <label htmlFor={`${titleId}-password`}>{t("login.password")}</label>
          <div className="input-with-button">
            <input
              id={`${titleId}-password`}
              name="password"
              type={showPassword ? "text" : "password"}
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              required
            />
            <button
              type="button"
              className="icon-button"
              aria-label={showPassword ? t("login.hidePassword") : t("login.showPassword")}
              aria-pressed={showPassword}
              onClick={() => setShowPassword((value) => !value)}
            >
              <Icon name="eye" size={17} />
            </button>
          </div>
        </div>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={trust}
            onChange={(event) => setTrust(event.target.checked)}
          />
          {t("login.trust")}
        </label>
        <button type="submit" className="wide" disabled={busy}>
          {busy ? t("login.submitting") : t("login.submit")}
        </button>
        <div className="split-links">
          <button
            type="button"
            className="link-button"
            aria-expanded={forgot}
            onClick={() => setForgot((value) => !value)}
          >
            {t("login.forgot")}
          </button>
          <Link to="/pair">{t("login.addDevice")}</Link>
        </div>
        {forgot && <p className="muted small">{t("login.forgotBody")}</p>}
        <div className="info-box">
          <Icon name="info" size={16} />
          <p>{t("login.addDeviceHint")}</p>
        </div>
      </form>
    </AuthLayout>
  );
}
