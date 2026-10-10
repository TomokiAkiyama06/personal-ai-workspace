// [その他…]: the person's own words → the Backend's structured preview → save
// (issue #38, the P1008PrefOther / P1008MobilePrefOther boards; Decision 0081
// point 11). `interpret` writes nothing; the person may correct the preview, and
// the Backend validates it again and re-measures the risk when saving. A
// high-risk preview (or "必須") needs the explicit acknowledgement.
import { type ReactNode, useId, useState } from "react";
import { isApiError } from "../api/client";
import { useI18n } from "../i18n";
import { needsAcknowledgement, optionOf } from "./model";
import { Acknowledgement, AnswerError, type Answered, Chip, Note, Pill, PrefIcon } from "./parts";
import type { PreferenceState } from "./store";
import type {
  InterpretedScope,
  PreferenceCandidate,
  PreferencePreview,
  StructuredPreference,
} from "./types";

const SCOPES: readonly InterpretedScope[] = ["project_group", "repo", "project", "user"];

/** yyyy-mm-dd of an ISO time in the browser's time zone ("" for none). */
function dateValue(iso: string | null): string {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

/** The end of the chosen day in the browser's time zone, as ISO (null for none). */
function expiresFrom(value: string): string | null {
  if (!value) return null;
  const date = new Date(`${value}T23:59:59`);
  return Number.isNaN(date.getTime()) ? null : date.toISOString();
}

export function OtherForm({
  candidate,
  state,
  page,
  onBack,
  onAnswered,
}: {
  candidate: PreferenceCandidate;
  state: PreferenceState;
  /** The phone's full-screen page (P1008MobilePrefOther) instead of the card's body. */
  page: boolean;
  onBack: () => void;
  onAnswered: (answer: Answered) => void;
}) {
  const { t } = useI18n();
  const ids = useId();
  const [text, setText] = useState("");
  const [preview, setPreview] = useState<PreferencePreview | null>(null);
  const [draft, setDraft] = useState<StructuredPreference | null>(null);
  const [adding, setAdding] = useState<string | null>(null);
  const [checked, setChecked] = useState(false);
  const [forced, setForced] = useState(false);
  const [busy, setBusy] = useState<"interpret" | "save" | null>(null);
  const [failure, setFailure] = useState<unknown>(null);

  const repo = optionOf(candidate, "repo");
  const project = optionOf(candidate, "project");
  const names = state.names;
  const repoId = draft?.repo_id ?? repo?.repo_id ?? null;
  const projectId = draft?.project_id ?? project?.project_id ?? repo?.project_id ?? null;
  const repoName = names.repo(repoId) ?? t("pref.scope.unknownRepo");
  const projectName = names.project(projectId) ?? t("pref.scope.unknownProject");

  const highRisk =
    forced ||
    needsAcknowledgement(candidate) ||
    (preview !== null && (preview.requires_acknowledgement || preview.risk_level === "high")) ||
    draft?.strength === "required";

  const interpret = () => {
    if (!text.trim() || busy) return;
    setBusy("interpret");
    setFailure(null);
    state.source
      .interpret(candidate, text.trim())
      .then((found) => {
        setPreview(found);
        setDraft({ ...found.preference, exceptions: [...new Set(found.preference.exceptions)] });
        setBusy(null);
      })
      .catch((caught: unknown) => {
        setFailure(caught);
        setBusy(null);
      });
  };

  // An exception is kept once (the list is keyed by its text).
  const addException = (value: string) => {
    if (draft && value && !draft.exceptions.includes(value)) {
      setDraft({ ...draft, exceptions: [...draft.exceptions, value] });
    }
  };

  const setScope = (scope: InterpretedScope) => {
    if (!draft) return;
    if (scope === "repo") {
      setDraft({ ...draft, scope, repo_id: repoId, project_id: repo?.project_id ?? projectId });
    } else if (scope === "project") {
      setDraft({ ...draft, scope, repo_id: null, project_id: projectId });
    } else {
      setDraft({ ...draft, scope, repo_id: null, project_id: null });
    }
  };

  const save = () => {
    if (!draft || busy || (highRisk && !checked)) return;
    setBusy("save");
    setFailure(null);
    state.source
      .confirm(candidate, {
        preference: {
          ...draft,
          apply_to: draft.apply_to?.trim() ? draft.apply_to.trim() : null,
          rule: draft.rule.trim(),
        },
        acknowledge_high_risk: highRisk && checked,
      })
      .then((version) => {
        onAnswered({ kind: "saved", candidate, version });
        void state.reload();
      })
      .catch((caught: unknown) => {
        if (isApiError(caught, "preference_high_risk_unacknowledged")) setForced(true);
        setFailure(caught);
        setBusy(null);
      });
  };

  // Only the scopes the preview can name: a repository or a project the candidate
  // was seen in (the Backend validates the rest).
  const scopes = SCOPES.filter(
    (scope) => (scope !== "repo" || repoId !== null) && (scope !== "project" || projectId !== null),
  );
  const scopeLabel = (scope: InterpretedScope) =>
    scope === "repo"
      ? t("pref.other.scope.repo", { name: repoName })
      : scope === "project"
        ? t("pref.other.scope.project", { name: projectName })
        : t(`pref.other.scope.${scope}`);

  const row = (label: ReactNode, control: ReactNode) => (
    <div className="pref-field">
      {label}
      {control}
    </div>
  );

  const previewBox = draft && preview && (
    <div className="pref-preview">
      <div className="pref-preview-head">
        <span>{page ? t("pref.other.previewShort") : t("pref.other.preview")}</span>
        {!page &&
          (highRisk ? (
            <Chip tone="error">{t("pref.other.riskHigh")}</Chip>
          ) : (
            <Chip tone="ok">{t("pref.other.riskLow")}</Chip>
          ))}
        <Chip>
          {preview.interpreted_by === "model" ? t("pref.other.byModel") : t("pref.other.byRules")}
        </Chip>
      </div>
      <div className="pref-preview-body">
        {row(
          <label htmlFor={`${ids}-scope`}>{t("pref.other.scope")}</label>,
          <select
            id={`${ids}-scope`}
            value={draft.scope}
            onChange={(event) => setScope(event.target.value as InterpretedScope)}
          >
            {scopes.map((scope) => (
              <option key={scope} value={scope}>
                {scopeLabel(scope)}
              </option>
            ))}
          </select>,
        )}
        {(draft.scope === "project_group" || draft.apply_to) &&
          row(
            <label htmlFor={`${ids}-apply`}>{t("pref.other.applyTo")}</label>,
            <input
              id={`${ids}-apply`}
              value={draft.apply_to ?? ""}
              onChange={(event) => setDraft({ ...draft, apply_to: event.target.value })}
            />,
          )}
        {row(
          <label htmlFor={`${ids}-rule`}>{t("pref.other.rule")}</label>,
          <input
            id={`${ids}-rule`}
            value={draft.rule}
            required
            onChange={(event) => setDraft({ ...draft, rule: event.target.value })}
          />,
        )}
        {(!page || draft.exceptions.length > 0) &&
          row(
            <span>{t("pref.other.exceptions")}</span>,
            <div className="pref-exceptions">
              {draft.exceptions.map((item) => (
                <span key={item} className="pref-exception">
                  {item}
                  <button
                    type="button"
                    className="icon-button"
                    aria-label={t("pref.other.removeException", { text: item })}
                    onClick={() =>
                      setDraft({
                        ...draft,
                        exceptions: draft.exceptions.filter((other) => other !== item),
                      })
                    }
                  >
                    <PrefIcon name="x" size={11} />
                  </button>
                </span>
              ))}
              {adding === null ? (
                <button type="button" className="pref-add" onClick={() => setAdding("")}>
                  {t("pref.other.addException")}
                </button>
              ) : (
                <input
                  className="pref-add-input"
                  aria-label={t("pref.other.newException")}
                  // biome-ignore lint/a11y/noAutofocus: the person just asked to type one
                  autoFocus
                  value={adding}
                  onChange={(event) => setAdding(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") {
                      event.preventDefault();
                      const value = adding.trim();
                      addException(value);
                      setAdding(null);
                    } else if (event.key === "Escape") setAdding(null);
                  }}
                  onBlur={() => {
                    const value = adding.trim();
                    addException(value);
                    setAdding(null);
                  }}
                />
              )}
            </div>,
          )}
        {row(
          <span>{t("pref.other.strength")}</span>,
          <fieldset className="pref-segments">
            <legend className="visually-hidden">{t("pref.other.strength")}</legend>
            {(["default", "required"] as const).map((strength) => (
              <button
                key={strength}
                type="button"
                aria-pressed={draft.strength === strength}
                onClick={() => setDraft({ ...draft, strength })}
              >
                {t(`pref.other.strength.${strength}`)}
              </button>
            ))}
          </fieldset>,
        )}
        {draft.strength === "required" && page && (
          <p className="pref-small-note">{t("pref.other.requiredNote")}</p>
        )}
        {!page &&
          row(
            <label htmlFor={`${ids}-expires`}>{t("pref.other.expires")}</label>,
            <div className="pref-expires">
              <input
                id={`${ids}-expires`}
                type="date"
                value={dateValue(draft.expires_at)}
                onChange={(event) =>
                  setDraft({ ...draft, expires_at: expiresFrom(event.target.value) })
                }
              />
              <span>{t("pref.other.noExpiry")}</span>
            </div>,
          )}
      </div>
    </div>
  );

  const notes = draft && (
    <>
      {draft.scope === "project_group" && (
        <Note>
          {t("pref.other.groupNote", {
            target: draft.apply_to?.trim() || t("pref.other.scope.project_group"),
          })}
        </Note>
      )}
      {draft.strength === "required" && !page && <Note>{t("pref.other.requiredNote")}</Note>}
      {highRisk && <Note tone="danger">{t("pref.other.riskNote")}</Note>}
      {highRisk && <Acknowledgement checked={checked} onChange={setChecked} boxes={false} />}
    </>
  );

  const textArea = (
    <div className="pref-other-text">
      <label htmlFor={`${ids}-text`} className="pref-label">
        {t("pref.other.textLabel")}
      </label>
      <div className="pref-other-input">
        <textarea
          id={`${ids}-text`}
          rows={2}
          value={text}
          onChange={(event) => setText(event.target.value)}
        />
        <button
          type="button"
          className="secondary small-button"
          disabled={!text.trim() || busy !== null}
          aria-busy={busy === "interpret"}
          onClick={interpret}
        >
          {busy === "interpret"
            ? t("pref.other.interpreting")
            : preview
              ? t("pref.other.reinterpret")
              : t("pref.other.interpret")}
        </button>
      </div>
    </div>
  );

  const error = failure !== null && (
    <AnswerError
      error={failure}
      candidate={candidate}
      onReload={() => {
        setFailure(null);
        void state.reload();
      }}
    />
  );

  const saveDisabled = !draft?.rule.trim() || busy !== null || (highRisk && !checked);
  const saveLabel =
    busy === "save"
      ? t("pref.action.saving")
      : highRisk
        ? t("pref.action.confirm")
        : t("pref.other.save");

  if (page) {
    return (
      <div className="pref-page" role="dialog" aria-modal="true" aria-labelledby={`${ids}-title`}>
        <header className="pref-page-head">
          <button
            type="button"
            className="icon-button"
            aria-label={t("pref.other.back")}
            onClick={onBack}
          >
            <PrefIcon name="back" size={20} />
          </button>
          <h1 id={`${ids}-title`}>{t("pref.other.titleShort")}</h1>
          {highRisk && <Pill tone="error">{t("pref.pill.highRisk")}</Pill>}
        </header>
        <div className="pref-page-body">
          {textArea}
          {previewBox}
          {notes}
          {error}
        </div>
        <div className="pref-page-foot">
          <button type="button" className="wide" disabled={saveDisabled} onClick={save}>
            {saveLabel}
          </button>
          <span>{highRisk ? t("pref.risk.hint") : t("pref.other.revalidate")}</span>
        </div>
      </div>
    );
  }

  return (
    <div className="pref-other">
      {textArea}
      {previewBox}
      {notes}
      {error}
      <div className="pref-actions">
        <button type="button" className="small-button" disabled={saveDisabled} onClick={save}>
          {saveLabel}
        </button>
        <button type="button" className="secondary small-button pref-ghost" onClick={onBack}>
          {t("pref.other.back")}
        </button>
        <span className="pref-hint">
          {highRisk ? t("pref.risk.hint") : t("pref.other.revalidate")}
        </span>
      </div>
    </div>
  );
}
