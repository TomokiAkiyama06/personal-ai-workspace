// トークンでパスワードを設定 (the design's PTokenRedeem, page 追加案（09-28）).
// `/setup#<token>` receives the one-time token of `owner-setup` / `owner-recover`,
// or the password reset token an Owner / Admin issued with a Passkey reset (#108),
// and spends it with the new password on POST /auth/token/redeem (issue #262).
// Nobody is signed in by it: the next step is the sign-in, and a role that needs a
// Passkey then meets the Passkey gate (PasskeyGatePage) right after it.
//
// The design's third kind (招待を受け取る) is another endpoint
// (/auth/invitations/redeem) and not part of this page.
import { type FormEvent, type ReactNode, useEffect, useId, useState } from "react";
import {
  authApi,
  PASSWORD_MAX_LENGTH,
  PASSWORD_MIN_DISTINCT,
  PASSWORD_MIN_LENGTH,
  type RedeemResult,
  TOKEN_MAX,
} from "../api/auth";
import { isApiError } from "../api/client";
import { useSession } from "../auth/session";
import { type MessageKey, useI18n } from "../i18n";
import { errorMessage } from "../i18n/errors";
import { Link, useRouter } from "../router";
import { useFooterText } from "../shell/common";
import { Icon } from "../shell/icons";
import { AuthLayout } from "./AuthLayout";

export const SETUP_PATH = "/setup";

/** The token of `/setup#<token>` (a fragment never reaches the server's logs). */
function tokenFromLocation(): string | null {
  const raw = window.location.hash.replace(/^#/, "");
  if (!raw) return null;
  try {
    return decodeURIComponent(raw).trim() || null;
  } catch {
    return null;
  }
}

/** `pawst1.<id>.••••`: enough to recognise the token, never its secret. */
function maskToken(token: string): string {
  const [prefix, id] = token.split(".");
  return id ? `${prefix}.${id.slice(0, 8)}…••••••••` : "••••••••••••";
}

// Why the Backend refused a new password (paw_backend/auth/errors.py
// PasswordProblem). The API sends it only inside the fixed message of 422
// `password_policy`: "The password does not meet the policy (<problem>)".
const POLICY_PROBLEMS = [
  "too_short",
  "too_long",
  "invalid_character",
  "too_common",
  "contains_login_name",
  "same_as_current",
] as const;
type PolicyProblem = (typeof POLICY_PROBLEMS)[number] | "unknown";

export function passwordProblem(error: unknown): PolicyProblem | null {
  if (!isApiError(error, "password_policy")) return null;
  const found = /\(([a-z_]+)\)\s*$/.exec(error.message)?.[1];
  return (POLICY_PROBLEMS as readonly string[]).includes(found ?? "")
    ? (found as PolicyProblem)
    : "unknown";
}

/** The rules the page can check before sending (the same as the Backend's). */
export function passwordChecks(password: string, confirm: string) {
  const characters = Array.from(password.normalize("NFKC"));
  const distinct = new Set(characters.map((character) => character.toLowerCase())).size;
  return {
    count: characters.length,
    length: characters.length >= PASSWORD_MIN_LENGTH && characters.length <= PASSWORD_MAX_LENGTH,
    distinct: distinct >= PASSWORD_MIN_DISTINCT,
    match: password !== "" && password === confirm,
  };
}

type Kind = "owner" | "reset";

type Failure =
  | { kind: "token" }
  | { kind: "rate"; until: number | null }
  | { kind: "policy"; problem: PolicyProblem }
  | { kind: "other"; message: string };

/** Whole seconds until `until` (a time in ms), counting down once a second. */
function useSecondsLeft(until: number | null): number {
  const [now, setNow] = useState(() => Date.now());
  const [counting, setCounting] = useState(until);
  if (counting !== until) {
    // A new wait starts from the current time, not from the last tick.
    setCounting(until);
    setNow(Date.now());
  }
  useEffect(() => {
    if (until === null) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [until]);
  return until === null ? 0 : Math.max(0, Math.ceil((until - now) / 1000));
}

function formatWait(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  return `${String(minutes).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}

function Check({ state, label }: { state: "ok" | "ng" | "idle"; label: string }) {
  const { t } = useI18n();
  return (
    <li className={`redeem-check ${state}`}>
      <span className="redeem-check-mark" aria-hidden="true">
        {state === "ok" && <Icon name="check" size={10} />}
        {state === "ng" && <Icon name="cross" size={9} />}
      </span>
      <span>
        {label}
        <span className="visually-hidden">（{t(`redeem.check.${state}`)}）</span>
      </span>
    </li>
  );
}

function ResultCard({
  tone,
  icon,
  title,
  code,
  role,
  children,
}: {
  tone: "ok" | "error" | "warning";
  icon: "check" | "cross" | "clock";
  title: string;
  code?: string;
  role: "status" | "alert";
  children: ReactNode;
}) {
  return (
    <div className={`redeem-result ${tone}`} role={role}>
      <div className="redeem-result-head">
        <span className="redeem-result-mark" aria-hidden="true">
          <Icon name={icon} size={12} />
        </span>
        <h2>{title}</h2>
        {code && <span className="mono redeem-result-code">{code}</span>}
      </div>
      {children}
    </div>
  );
}

/** The left panel: what the token is, and what to do when the password is lost. */
function RedeemAside() {
  const { t } = useI18n();
  const footer = useFooterText();
  const rows: { role: string; text: MessageKey; owner?: boolean }[] = [
    { role: "User", text: "redeem.forgot.user" },
    { role: "Admin", text: "redeem.forgot.admin" },
    { role: "Owner", text: "redeem.forgot.owner", owner: true },
  ];
  return (
    <>
      <div className="stack-lg">
        <div className="brand">
          <Icon name="logo" size={28} />
          <span className="brand-name">{t("app.name")}</span>
        </div>
        <p className="brand-headline redeem-headline">
          {t("redeem.headline1")}
          <br />
          {t("redeem.headline2")}
        </p>
        <p className="brand-body">{t("redeem.asideBody")}</p>
      </div>
      <section className="redeem-forgot" aria-labelledby="redeem-forgot-title">
        <div className="redeem-forgot-head">
          <Icon name="info" size={16} />
          <h2 id="redeem-forgot-title">{t("redeem.forgot.title")}</h2>
        </div>
        <p className="redeem-forgot-body">{t("redeem.forgot.body")}</p>
        <div className="stack">
          {rows.map((row) => (
            <div key={row.role} className="redeem-forgot-row">
              <span className={`redeem-role${row.owner ? " owner" : ""}`}>{row.role}</span>
              <span className="redeem-forgot-text">
                {t(row.text)}
                {row.owner && (
                  <code className="redeem-command">
                    sudo python -m paw_backend.cli owner-recover --confirm-owner-recovery
                  </code>
                )}
              </span>
            </div>
          ))}
        </div>
        <p className="muted small">{t("redeem.forgot.lock")}</p>
      </section>
      <div className="brand-footer redeem-aside-footer">
        <span className="mono">{footer}</span>
        <Link to="/">{t("redeem.backToSignIn")}</Link>
      </div>
    </>
  );
}

/** Set a password with a one-time token (Owner setup / recovery, password reset). */
export function TokenRedeemPage() {
  const { t } = useI18n();
  const { navigate } = useRouter();
  const { refresh } = useSession();
  const id = useId();
  const [linkToken, setLinkToken] = useState(tokenFromLocation);
  const [typedToken, setTypedToken] = useState("");
  const [kind, setKind] = useState<Kind>("owner");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [show, setShow] = useState(false);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<Failure | null>(null);
  const [result, setResult] = useState<RedeemResult | null>(null);

  // Once read, the one-time token leaves the address bar and the history entry.
  useEffect(() => {
    if (window.location.hash) window.history.replaceState(null, "", window.location.pathname);
  }, []);

  const waitSeconds = useSecondsLeft(failure?.kind === "rate" ? failure.until : null);

  const token = (linkToken ?? typedToken).trim();
  const checks = passwordChecks(password, confirm);
  const ready = token !== "" && checks.length && checks.distinct && checks.match;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!ready || busy || waitSeconds > 0) return;
    setBusy(true);
    setFailure(null);
    try {
      const answer = await authApi.redeemToken({ token, new_password: password });
      setPassword("");
      setConfirm("");
      setLinkToken(null);
      setTypedToken("");
      setResult(answer);
    } catch (caught) {
      const problem = passwordProblem(caught);
      if (problem) setFailure({ kind: "policy", problem });
      else if (isApiError(caught, "invalid_token")) setFailure({ kind: "token" });
      else if (isApiError(caught, "rate_limited")) {
        const seconds = caught.retryAfterSeconds;
        setFailure({ kind: "rate", until: seconds === null ? null : Date.now() + seconds * 1000 });
      } else setFailure({ kind: "other", message: errorMessage(t, caught) });
    } finally {
      setBusy(false);
    }
  };

  const toSignIn = () => {
    // A recovery ends every session, this browser's too: ask again before going on.
    void refresh().finally(() => navigate("/", { replace: true }));
  };

  const owner = result ? result.purpose !== "password_reset" : kind === "owner";
  const state = (filled: boolean, ok: boolean) => (!filled ? "idle" : ok ? "ok" : "ng");

  return (
    <AuthLayout aside={<RedeemAside />}>
      {result ? (
        <ResultCard
          tone="ok"
          icon="check"
          role="status"
          title={t(owner ? "redeem.success.owner" : "redeem.success.reset")}
          code={t("redeem.success.code")}
        >
          <p className="redeem-result-body">{t("redeem.success.body")}</p>
          {result.passkey_required && (
            <div className="redeem-note">
              <Icon name="key" size={16} />
              <p>{t("redeem.success.passkey")}</p>
            </div>
          )}
          <button type="button" className="redeem-proceed" onClick={toSignIn}>
            {t("redeem.toSignIn")}
          </button>
        </ResultCard>
      ) : (
        <form className="stack" onSubmit={submit} aria-labelledby={`${id}-title`} noValidate>
          <fieldset className="stack-xs redeem-kind">
            <legend className="section-label">{t("redeem.kind")}</legend>
            <div className="segments">
              {(["owner", "reset"] as const).map((option) => (
                <button
                  key={option}
                  type="button"
                  aria-pressed={kind === option}
                  onClick={() => setKind(option)}
                >
                  {t(`redeem.kind.${option}`)}
                </button>
              ))}
            </div>
          </fieldset>
          <div className="stack-xs">
            <div className="redeem-title">
              <h1 id={`${id}-title`}>{t(`redeem.heading.${kind}`)}</h1>
              <span className="mono redeem-ttl">{t(`redeem.ttl.${kind}`)}</span>
            </div>
            <p className="muted redeem-intro">{t(`redeem.intro.${kind}`)}</p>
          </div>

          {failure?.kind === "token" && (
            <ResultCard
              tone="error"
              icon="cross"
              role="alert"
              title={t("redeem.invalid.title")}
              code="invalid_token"
            >
              <p className="redeem-result-body">{t("redeem.invalid.body")}</p>
            </ResultCard>
          )}
          {failure?.kind === "rate" && (
            <ResultCard
              tone="warning"
              icon="clock"
              role="alert"
              title={t("redeem.rate.title")}
              code="429 · Retry-After"
            >
              <div className="redeem-wait">
                <p className="redeem-result-body">{t("redeem.rate.body")}</p>
                {failure.until !== null && (
                  <span className="redeem-wait-time">
                    {waitSeconds > 0 ? (
                      <>
                        <span className="mono">{formatWait(waitSeconds)}</span>
                        <span className="muted small">{t("redeem.rate.after")}</span>
                      </>
                    ) : (
                      <span className="muted small">{t("redeem.rate.ready")}</span>
                    )}
                  </span>
                )}
              </div>
            </ResultCard>
          )}
          {failure?.kind === "policy" && (
            <p className="form-error" role="alert">
              <strong>{t("redeem.policy.title")}</strong> {t(`redeem.policy.${failure.problem}`)}
            </p>
          )}
          {failure?.kind === "other" && (
            <p className="form-error" role="alert">
              {failure.message}
            </p>
          )}

          <div className="field">
            <label htmlFor={`${id}-token`}>{t("redeem.token")}</label>
            {linkToken ? (
              <div className="input-with-button redeem-token-filled">
                <input
                  id={`${id}-token`}
                  className="mono"
                  value={maskToken(linkToken)}
                  readOnly
                  aria-describedby={`${id}-token-hint`}
                />
                <span className="redeem-filled-chip">
                  <Icon name="check" size={11} />
                  {t("redeem.fromLink")}
                </span>
              </div>
            ) : (
              <input
                id={`${id}-token`}
                className="mono"
                value={typedToken}
                onChange={(event) => setTypedToken(event.target.value)}
                placeholder="pawst1.xxxxxxxx.xxxxxxxx"
                autoComplete="off"
                spellCheck={false}
                maxLength={TOKEN_MAX}
                aria-describedby={`${id}-token-hint`}
              />
            )}
            <p id={`${id}-token-hint`} className="redeem-hint">
              {t(`redeem.tokenHint.${kind}`)}
            </p>
            {linkToken && failure?.kind === "token" && (
              <button
                type="button"
                className="link-button redeem-retype"
                onClick={() => {
                  setLinkToken(null);
                  setFailure(null);
                }}
              >
                {t("redeem.retype")}
              </button>
            )}
          </div>

          <div className="field">
            <label htmlFor={`${id}-password`}>{t("redeem.password")}</label>
            <div className="input-with-button">
              <input
                id={`${id}-password`}
                type={show ? "text" : "password"}
                autoComplete="new-password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                aria-describedby={`${id}-checks`}
              />
              <button
                type="button"
                className="icon-button"
                aria-label={show ? t("login.hidePassword") : t("login.showPassword")}
                aria-pressed={show}
                onClick={() => setShow((value) => !value)}
              >
                <Icon name="eye" size={17} />
              </button>
            </div>
          </div>
          <label className="field">
            <span>{t("redeem.confirm")}</span>
            <input
              type={show ? "text" : "password"}
              autoComplete="new-password"
              value={confirm}
              onChange={(event) => setConfirm(event.target.value)}
            />
          </label>

          <ul id={`${id}-checks`} className="redeem-checks" aria-live="polite">
            <Check
              state={state(password !== "", checks.length)}
              label={t("redeem.check.length", { count: checks.count })}
            />
            <Check
              state={state(password !== "", checks.distinct)}
              label={t("redeem.check.distinct")}
            />
            <Check state={state(confirm !== "", checks.match)} label={t("redeem.check.match")} />
            <li className="redeem-checks-note">{t("redeem.check.server")}</li>
          </ul>

          {kind === "owner" && (
            <div className="redeem-warning">
              <Icon name="warning" size={16} />
              <p>{t("redeem.recoveryWarning")}</p>
            </div>
          )}

          <button type="submit" className="wide" disabled={!ready || busy || waitSeconds > 0}>
            {busy ? t("redeem.submitting") : t("redeem.submit")}
          </button>
          {/* The side panel (with its link) is hidden below 1280px. */}
          <Link to="/" className="redeem-back-narrow">
            {t("redeem.backToSignIn")}
          </Link>
        </form>
      )}
    </AuthLayout>
  );
}
