// Inline "confirm it is you" for the changes the Backend guards with a recent
// step-up (registering / removing a passkey, approving a device, ...). The action
// is tried first; only a `step_up_required` / `step_up_method_insufficient` answer
// shows the prompt, and the action is retried after the step-up (asking again for
// a passkey if a password step-up was not enough). Not a modal (docs/UI_DESIGN.md:
// normal work is not blocked by dialogs); the copy is the design's PasskeyStates D.
import { type FormEvent, type ReactNode, useCallback, useId, useRef, useState } from "react";
import { authApi } from "../api/auth";
import { isApiError } from "../api/client";
import { useI18n } from "../i18n";
import { errorMessage } from "../i18n/errors";
import { useSession, useSignedIn } from "./session";
import { getPasskey, webauthnSupported } from "./webauthn";

export class StepUpCancelledError extends Error {
  constructor() {
    super("Step-up cancelled");
    this.name = "StepUpCancelledError";
  }
}

interface Pending {
  /** A new prompt is a new panel (fresh busy / error state), even right after another. */
  id: number;
  passkeyOnly: boolean;
  resolve: (done: boolean) => void;
}

export interface StepUpOptions {
  /** The action is known to need a passkey step-up (offer no password form). */
  passkeyOnly?: boolean;
}

export function useStepUp(): {
  run: <T>(action: () => Promise<T>, options?: StepUpOptions) => Promise<T>;
  prompt: ReactNode;
} {
  const [pending, setPending] = useState<Pending | null>(null);
  const prompts = useRef(0);
  const run = useCallback(
    async <T,>(action: () => Promise<T>, options: StepUpOptions = {}): Promise<T> => {
      let passkeyOnly = options.passkeyOnly ?? false;
      // A password step-up can satisfy `step_up_required` and still not be enough
      // for an action that needs a passkey one (the Backend answers
      // `step_up_required` first when there is no step-up at all). The retry then
      // says `step_up_method_insufficient`: ask again, for a passkey only.
      for (let attempt = 0; ; attempt++) {
        try {
          return await action();
        } catch (error) {
          if (!isApiError(error, "step_up_required", "step_up_method_insufficient")) throw error;
          if (attempt >= 2 || (attempt > 0 && passkeyOnly)) throw error;
          passkeyOnly = passkeyOnly || error.code === "step_up_method_insufficient";
          const only = passkeyOnly;
          const done = await new Promise<boolean>((resolve) =>
            setPending({ id: ++prompts.current, passkeyOnly: only, resolve }),
          );
          setPending(null);
          if (!done) throw new StepUpCancelledError();
        }
      }
    },
    [],
  );
  const prompt = pending ? (
    <StepUpPanel key={pending.id} passkeyOnly={pending.passkeyOnly} onDone={pending.resolve} />
  ) : null;
  return { run, prompt };
}

function StepUpPanel({
  passkeyOnly,
  onDone,
}: {
  passkeyOnly: boolean;
  onDone: (done: boolean) => void;
}) {
  const { t } = useI18n();
  const { accept } = useSession();
  const data = useSignedIn();
  const titleId = useId();
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [verifying, setVerifying] = useState(false);
  const passkey = data.auth?.passkey;
  const canUsePasskey = Boolean(passkey?.enrolled && passkey.available && webauthnSupported());

  const withPasskey = async () => {
    setBusy(true);
    setVerifying(true);
    setError(null);
    try {
      const { options } = await authApi.passkeyAuthenticateBegin();
      const credential = await getPasskey(options);
      accept(await authApi.passkeyAuthenticateFinish(credential));
      onDone(true);
    } catch (caught) {
      setError(errorMessage(t, caught));
      setBusy(false);
    } finally {
      setVerifying(false);
    }
  };

  const withPassword = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      accept(await authApi.stepUpWithPassword(password));
      setPassword("");
      onDone(true);
    } catch (caught) {
      setError(errorMessage(t, caught));
      setBusy(false);
    }
  };

  return (
    <section className="card step-up" aria-labelledby={titleId}>
      <h3 id={titleId}>{t("stepUp.title")}</h3>
      <p className="muted">{passkeyOnly ? t("stepUp.passkeyOnly") : t("stepUp.body")}</p>
      {verifying && (
        <p className="notice" role="status">
          <strong>{t("passkey.verifying")}</strong> {t("passkey.verifyingBody")}
        </p>
      )}
      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
      <div className="actions">
        {(canUsePasskey || passkeyOnly) && (
          <button type="button" onClick={withPasskey} disabled={busy}>
            {t("stepUp.withPasskey")}
          </button>
        )}
      </div>
      {!passkeyOnly && (
        <form onSubmit={withPassword} className="inline-form">
          <label>
            {t("stepUp.password")}
            <input
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              required
            />
          </label>
          <button type="submit" className="secondary" disabled={busy || password === ""}>
            {t("stepUp.withPassword")}
          </button>
        </form>
      )}
      <div className="actions">
        <button type="button" className="secondary" onClick={() => onDone(false)} disabled={busy}>
          {t("stepUp.cancel")}
        </button>
      </div>
    </section>
  );
}
