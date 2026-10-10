// The pieces the chat card, the phone sheet and the Memory screen share (issue
// #38, the P1008Pref* boards): icons, evidence chips, the scope buttons, the
// high-risk acknowledgement and the answer row. Labels are the catalog's
// Japanese only; the API's enum names never reach the screen.
import { type ReactNode, useState } from "react";
import { isApiError } from "../api/client";
import { type MessageKey, useI18n } from "../i18n";
import { errorMessage } from "../i18n/errors";
import type { MemoryVersion } from "../memory/types";
import { Link } from "../router";
import { defaultOption, type Names, needsAcknowledgement } from "./model";
import type { PreferenceState } from "./store";
import type { PreferenceCandidate, ScopeOption } from "./types";

type T = (key: MessageKey, params?: Record<string, string | number>) => string;

const ICONS = {
  layers: (
    <>
      <path d="M12 3.5 3 8l9 4.5L21 8Z" />
      <path d="M3 12.6 12 17.1 21 12.6" />
      <path d="M3 16.9 12 21.4 21 16.9" />
    </>
  ),
  warn: (
    <>
      <path d="M12 3.5 21 19.5H3Z" />
      <path d="M12 10v4" />
      <path d="M12 17v.4" />
    </>
  ),
  info: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 11v5" />
      <path d="M12 7.6v.5" />
    </>
  ),
  check: <path d="m5 12.5 4.5 4.5L19 7" />,
  lock: (
    <>
      <rect x="5" y="10.5" width="14" height="9" rx="2" />
      <path d="M8.5 10.5V7.5a3.5 3.5 0 0 1 7 0v3" />
    </>
  ),
  x: (
    <>
      <path d="M6.5 6.5l11 11" />
      <path d="M17.5 6.5l-11 11" />
    </>
  ),
  pen: (
    <>
      <path d="M4 20h4L19 9l-4-4L4 16Z" />
      <path d="m13.5 6.5 4 4" />
    </>
  ),
  back: <path d="M14.5 6 8.5 12l6 6" />,
} as const;

export type PrefIconName = keyof typeof ICONS | "star";

/** The boards' line icons (decorative). */
export function PrefIcon({ name, size = 14 }: { name: PrefIconName; size?: number }) {
  if (name === "star") {
    return (
      <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
        <path d="m12 3.5 2.6 5.4 5.9.8-4.3 4.1 1 5.8L12 16.9l-5.2 2.7 1-5.8-4.3-4.1 5.9-.8Z" />
      </svg>
    );
  }
  return (
    <svg
      className="pref-icon"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {ICONS[name]}
    </svg>
  );
}

export type Tone = "neutral" | "strong" | "info" | "ok" | "warning" | "error";

export function Chip({ tone = "neutral", children }: { tone?: Tone; children: ReactNode }) {
  return <span className={`pref-chip tone-${tone}`}>{children}</span>;
}

export function Pill({ tone, children }: { tone: Tone; children: ReactNode }) {
  return <span className={`pref-pill tone-${tone}`}>{children}</span>;
}

/** 推定 / 高リスク, the card's and the detail's status pill. */
export function KindPill({ candidate }: { candidate: PreferenceCandidate }) {
  const { t } = useI18n();
  return needsAcknowledgement(candidate) ? (
    <Pill tone="error">{t("pref.pill.highRisk")}</Pill>
  ) : (
    <Pill tone="warning">{t("pref.pill.inferred")}</Pill>
  );
}

/** Where the evidence was seen, in one chip's words (null: nothing to say). */
export function spreadLabel(candidate: PreferenceCandidate, names: Names, t: T): string | null {
  const facts = candidate.evidence;
  const repoId =
    candidate.recommendation.repo_id ??
    candidate.options.find((option) => option.repo_id)?.repo_id ??
    null;
  const projectId =
    candidate.recommendation.project_id ??
    candidate.options.find((option) => option.project_id)?.project_id ??
    null;
  const project = names.project(projectId) ?? t("pref.scope.unknownProject");
  if (facts.project_count === 0) return facts.outside_projects > 0 ? t("pref.chip.outside") : null;
  if (facts.project_count > 1 || facts.outside_projects > 0) {
    return t("pref.chip.projects", { count: facts.project_count });
  }
  if (facts.repo_count === 1) {
    return t("pref.chip.onlyRepo", { repo: names.repo(repoId) ?? t("pref.scope.unknownRepo") });
  }
  if (facts.repo_count > 1)
    return t("pref.chip.projectRepos", { project, count: facts.repo_count });
  return t("pref.chip.onlyProject", { project });
}

/** The evidence chips of the card (観測 4 回 · backend だけで観測 · …). */
export function EvidenceChips({
  candidate,
  names,
  consistency = true,
}: {
  candidate: PreferenceCandidate;
  names: Names;
  consistency?: boolean;
}) {
  const { t } = useI18n();
  const facts = candidate.evidence;
  const spread = spreadLabel(candidate, names, t);
  return (
    <div className="pref-chips">
      <Chip tone="strong">{t("pref.chip.frequency", { count: facts.frequency })}</Chip>
      {spread && <Chip>{spread}</Chip>}
      {facts.language_strength === "standing" && <Chip tone="info">{t("pref.chip.standing")}</Chip>}
      {facts.language_strength === "once" && <Chip>{t("pref.chip.once")}</Chip>}
      {consistency && (
        <Chip tone={facts.consistency === "conflicting" ? "error" : "neutral"}>
          {t(`pref.chip.${facts.consistency}`)}
        </Chip>
      )}
      {facts.risk_level === "high" ? (
        <Chip tone="error">{t("pref.chip.riskHigh")}</Chip>
      ) : (
        <Chip tone="ok">{t("pref.chip.riskLow")}</Chip>
      )}
      {candidate.held_reason && (
        <Chip tone="warning">{t(`pref.chip.held.${candidate.held_reason}`)}</Chip>
      )}
    </div>
  );
}

/** "Seen 4 times · last 14:12". */
export function observedMeta(candidate: PreferenceCandidate, t: T, time: (iso: string) => string) {
  return t("pref.card.meta", {
    count: candidate.evidence.frequency,
    time: time(candidate.evidence.last_observed_at ?? candidate.observed_at),
  });
}

export function optionLabel(option: ScopeOption, t: T): string {
  return t(`pref.scope.${option.scope}`);
}

export function optionTarget(option: ScopeOption, names: Names, t: T): string {
  switch (option.scope) {
    case "repo":
      return names.repo(option.repo_id) ?? t("pref.scope.unknownRepo");
    case "project":
      return names.project(option.project_id) ?? t("pref.scope.unknownProject");
    case "user":
      return t("pref.scope.userTarget");
  }
}

/** Where a confirmation wrote: the repository, the project or 自分のメモリ. */
export function writtenWhere(version: MemoryVersion, names: Names, t: T): string {
  if (version.scope === "repo") return names.repo(version.repo_id) ?? t("pref.scope.unknownRepo");
  if (version.scope === "project") {
    return names.project(version.project_id) ?? t("pref.scope.unknownProject");
  }
  return t("pref.scope.userTarget");
}

export function Note({
  tone = "info",
  icon,
  children,
}: {
  tone?: "info" | "danger" | "lock";
  icon?: PrefIconName;
  children: ReactNode;
}) {
  return (
    <div className={`pref-note ${tone}`}>
      <PrefIcon name={icon ?? (tone === "danger" ? "warn" : tone === "lock" ? "lock" : "info")} />
      <p>{children}</p>
    </div>
  );
}

/** 保存すると / 変わらないこと and the checkbox (Decision 0081 point 10). */
export function Acknowledgement({
  checked,
  onChange,
  boxes = true,
}: {
  checked: boolean;
  onChange: (value: boolean) => void;
  boxes?: boolean;
}) {
  const { t } = useI18n();
  return (
    <>
      {boxes && (
        <div className="pref-risk-boxes">
          <div>
            <span className="tone-ok">{t("pref.risk.saves")}</span>
            <p>{t("pref.risk.savesText")}</p>
          </div>
          <div>
            <span>{t("pref.risk.keeps")}</span>
            <p>{t("pref.risk.keepsText")}</p>
          </div>
        </div>
      )}
      <label className="pref-ack">
        <input
          type="checkbox"
          checked={checked}
          onChange={(event) => onChange(event.target.checked)}
        />
        <span>{t("pref.risk.acknowledge")}</span>
      </label>
    </>
  );
}

/** A failure of an answer, in the flow's words (the flows board's table C). */
export function AnswerError({
  error,
  candidate,
  onReload,
}: {
  error: unknown;
  candidate: PreferenceCandidate;
  onReload: () => void;
}) {
  const { t } = useI18n();
  const changed = isApiError(error, "preference_candidate_changed", "memory_version_conflict");
  const gone = isApiError(error, "memory_not_found");
  let message: string;
  if (changed) message = t("pref.error.changed");
  else if (gone) message = t("pref.error.notFound");
  else if (isApiError(error, "preference_high_risk_unacknowledged")) {
    message = t("pref.error.highRisk");
  } else if (isApiError(error, "forbidden")) message = t("pref.error.forbidden");
  else if (isApiError(error, "validation_error") && candidate.held_reason === "held_widened") {
    message = t("pref.error.widened");
  } else message = errorMessage(t, error);
  return (
    <div className="pref-error" role="alert">
      <p>{message}</p>
      {(changed || gone) && (
        <button type="button" className="secondary small-button" onClick={onReload}>
          {t("pref.action.reload")}
        </button>
      )}
    </div>
  );
}

export interface Answered {
  kind: "saved" | "rejected";
  candidate: PreferenceCandidate;
  version: MemoryVersion | null;
}

/**
 * どこで使いますか, the scope buttons and [保存しない] [その他…]. An ordinary
 * candidate is saved by its button; a high-risk one (or one the Backend
 * answered 409 `preference_high_risk_unacknowledged`) selects a button, needs
 * the checkbox, then [確認して保存].
 */
export function AnswerControls({
  candidate,
  state,
  hint,
  onAnswered,
  onOther,
  showBoxes = true,
}: {
  candidate: PreferenceCandidate;
  state: PreferenceState;
  /** The row's note on the right (the board's grey text). */
  hint?: string;
  onAnswered: (answer: Answered) => void;
  onOther: () => void;
  showBoxes?: boolean;
}) {
  const { t } = useI18n();
  const [forced, setForced] = useState(false);
  const ack = forced || needsAcknowledgement(candidate);
  const [selected, setSelected] = useState<ScopeOption | null>(() => defaultOption(candidate));
  const [checked, setChecked] = useState(false);
  const [busy, setBusy] = useState<"confirm" | "reject" | null>(null);
  const [failure, setFailure] = useState<unknown>(null);

  const finish = (answer: Answered) => {
    onAnswered(answer);
    void state.reload();
  };
  const confirm = (option: ScopeOption | null) => {
    if (!option || busy) return;
    setBusy("confirm");
    setFailure(null);
    state.source
      .confirm(candidate, {
        scope: option.scope,
        project_id: option.project_id,
        repo_id: option.repo_id,
        acknowledge_high_risk: ack && checked,
      })
      .then((version) => finish({ kind: "saved", candidate, version }))
      .catch((caught: unknown) => {
        if (isApiError(caught, "preference_high_risk_unacknowledged")) {
          setForced(true);
          setSelected(option);
        }
        setFailure(caught);
        setBusy(null);
      });
  };
  const reject = () => {
    if (busy) return;
    setBusy("reject");
    setFailure(null);
    state.source
      .reject(candidate)
      .then(() => finish({ kind: "rejected", candidate, version: null }))
      .catch((caught: unknown) => {
        setFailure(caught);
        setBusy(null);
      });
  };

  const offered = candidate.options;
  return (
    <>
      {ack && offered.length > 0 && (
        <Acknowledgement checked={checked} onChange={setChecked} boxes={showBoxes} />
      )}
      {offered.length > 0 && (
        <>
          <span className="pref-label">{t("pref.scope.question")}</span>
          <div className="pref-scopes">
            {offered.map((option) => (
              <button
                key={`${option.scope}:${option.project_id ?? ""}:${option.repo_id ?? ""}`}
                type="button"
                className={option.recommended ? "pref-scope recommended" : "pref-scope"}
                aria-pressed={ack ? selected === option : undefined}
                disabled={busy !== null}
                onClick={() => (ack ? setSelected(option) : confirm(option))}
              >
                <span className="pref-scope-label">
                  {optionLabel(option, t)}
                  {option.recommended && (
                    <span className="pref-recommended">
                      <PrefIcon name="star" size={9} />
                      {t("pref.scope.recommended")}
                    </span>
                  )}
                </span>
                <span className="pref-scope-target mono">
                  {/* The Backend offers the repository seen most when several were. */}
                  {option.scope === "repo" &&
                  !option.recommended &&
                  candidate.evidence.repo_count > 1
                    ? t("pref.scope.mostObserved", {
                        name: optionTarget(option, state.names, t),
                      })
                    : optionTarget(option, state.names, t)}
                </span>
              </button>
            ))}
          </div>
        </>
      )}
      {failure !== null && (
        <AnswerError
          error={failure}
          candidate={candidate}
          onReload={() => {
            setFailure(null);
            void state.reload();
          }}
        />
      )}
      <div className="pref-actions">
        {ack && offered.length > 0 && (
          <button
            type="button"
            className="small-button"
            disabled={!checked || !selected || busy !== null}
            aria-busy={busy === "confirm"}
            onClick={() => confirm(selected)}
          >
            {busy === "confirm" ? t("pref.action.saving") : t("pref.action.confirm")}
          </button>
        )}
        <button
          type="button"
          className="secondary small-button pref-ghost"
          disabled={busy !== null}
          aria-busy={busy === "reject"}
          onClick={reject}
        >
          {t("pref.action.reject")}
        </button>
        {offered.length > 0 && (
          <button
            type="button"
            className="secondary small-button pref-ghost"
            disabled={busy !== null}
            onClick={onOther}
          >
            <PrefIcon name="pen" size={13} />
            {t("pref.action.other")}
          </button>
        )}
        <span className="pref-hint">{ack && offered.length > 0 ? t("pref.risk.hint") : hint}</span>
      </div>
    </>
  );
}

/** The one line a card folds into once answered. */
export function AnsweredLine({ answer, state }: { answer: Answered; state: PreferenceState }) {
  const { t } = useI18n();
  const version = answer.version;
  return (
    <div className="pref-answered" role="status">
      <span className="pref-answered-mark">
        <PrefIcon name={answer.kind === "saved" ? "check" : "x"} size={10} />
      </span>
      <span className="muted">
        {answer.kind === "saved" ? t("pref.card.saved") : t("pref.card.rejected")}
      </span>
      <span className="pref-answered-title ellipsis">{answer.candidate.title}</span>
      {version ? (
        <>
          <span className="mono muted">
            {writtenWhere(version, state.names, t)} ·{" "}
            {t("pref.card.version", { number: version.version_number })}
          </span>
          <Link to={`/memory/${encodeURIComponent(version.memory_id)}`}>
            {t("pref.card.history")}
          </Link>
        </>
      ) : (
        <span className="mono muted">{t("pref.card.rejectedHint")}</span>
      )}
    </div>
  );
}
