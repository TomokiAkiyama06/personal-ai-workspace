// メモリ › 推定の候補 / 保留中 (issue #38, the P1008PrefMemory / PrefHeld boards;
// on a phone P1008MobilePrefList). The candidates with their evidence and the
// same answers as the chat's card; answering here means the chat does not ask
// any more. Only the person sees their candidates (the Backend reads their own
// rows only).
import { type ReactNode, useId, useState } from "react";
import { useI18n } from "../i18n";
import { LoadError, SourcesTab } from "../memory/MemoryDetail";
import { useMemorySource } from "../memory/source";
import { Link, useRouter } from "../router";
import { PHONE_QUERY, useMediaQuery } from "../shell/common";
import {
  askState,
  CANDIDATES_PATH,
  candidateId,
  candidatePath,
  candidateTitle,
  HELD_PATH,
  heldCandidates,
  MIN_REPEATS,
  memoryCandidates,
  needsAcknowledgement,
  type PreferenceView,
  titleIsContent,
} from "./model";
import { OtherForm } from "./other";
import {
  AnswerControls,
  type Answered,
  AnsweredLine,
  Chip,
  EvidenceChips,
  KindPill,
  Note,
  optionLabel,
  Pill,
  type Tone,
} from "./parts";
import { type PreferenceState, usePreferences } from "./store";
import type { HeldReason, PreferenceCandidate } from "./types";
import "./preferences.css";

const HELD_TONE: Record<HeldReason, Tone> = {
  held_high_risk: "error",
  held_confirmed: "warning",
  held_widened: "info",
};

function counts(state: PreferenceState) {
  const all = state.candidates ?? [];
  return {
    candidates: all.filter((item) => item.kind === "memory").length,
    held: all.filter((item) => item.kind === "held").length,
  };
}

/** 確認: 推定の候補 N / 保留中 N, above the scope tree. */
export function PreferenceSection({ view }: { view: PreferenceView | null }) {
  const { t } = useI18n();
  const state = usePreferences();
  if (!state) return null;
  const count = counts(state);
  const row = (to: string, label: string, value: number, current: boolean) => (
    <Link to={to} className="scope-row pref-scope-row" aria-current={current ? "page" : undefined}>
      <span className="scope-name ellipsis">{label}</span>
      <span className="scope-count mono pref-count">{value}</span>
    </Link>
  );
  return (
    <div className="pref-section">
      <span className="section-label pref-section-label">{t("pref.memory.section")}</span>
      {row(CANDIDATES_PATH, t("pref.memory.candidates"), count.candidates, view === "candidates")}
      {row(HELD_PATH, t("pref.memory.held"), count.held, view === "held")}
      <hr />
    </div>
  );
}

/** At the foot of the scope tree while 推定の候補 / 保留中 is shown. */
export function PreferencePrivacy() {
  const { t } = useI18n();
  return <p className="pref-privacy">{t("pref.memory.private")}</p>;
}

/** The toolbar's 推定の候補 N / 保留中 N chips (and the phone's segments). */
export function PreferenceChips({ view }: { view: PreferenceView | null }) {
  const { t } = useI18n();
  const state = usePreferences();
  if (!state) return null;
  const count = counts(state);
  return (
    <>
      <Link
        to={CANDIDATES_PATH}
        className="chip pref-toolbar-chip"
        aria-current={view === "candidates" ? "page" : undefined}
      >
        {t("pref.memory.candidatesCount", { count: count.candidates })}
      </Link>
      <Link
        to={HELD_PATH}
        className="chip pref-toolbar-chip held"
        aria-current={view === "held" ? "page" : undefined}
      >
        {t("pref.memory.heldCount", { count: count.held })}
      </Link>
    </>
  );
}

export function PreferenceSegments({ view }: { view: PreferenceView | null }) {
  const { t } = useI18n();
  const state = usePreferences();
  if (!state) return null;
  const count = counts(state);
  return (
    <nav className="pref-segments-bar" aria-label={t("pref.memory.views")}>
      <Link to="/memory" aria-current={view === null ? "page" : undefined}>
        {t("pref.memory.all")}
      </Link>
      <Link to={CANDIDATES_PATH} aria-current={view === "candidates" ? "page" : undefined}>
        {t("pref.memory.candidatesCount", { count: count.candidates })}
      </Link>
      <Link to={HELD_PATH} className="held" aria-current={view === "held" ? "page" : undefined}>
        {t("pref.memory.heldShort", { count: count.held })}
      </Link>
    </nav>
  );
}

/** 同期済み HH:MM (the last read of the candidates). */
export function PreferenceSynced() {
  const { t, formatTime } = useI18n();
  const state = usePreferences();
  if (!state?.syncedAt) return null;
  return (
    <span className="pref-synced mono">
      <span aria-hidden="true" />
      {t("pref.memory.synced", { time: formatTime(state.syncedAt) })}
    </span>
  );
}

function ListCard({ candidate, selected }: { candidate: PreferenceCandidate; selected: boolean }) {
  const { t } = useI18n();
  const facts = candidate.evidence;
  let chips: ReactNode;
  let meta: string;
  if (candidate.kind === "held" && candidate.held_reason) {
    const reason = candidate.held_reason;
    chips = (
      <>
        <Chip tone={HELD_TONE[reason]}>{t(`pref.held.kind.${reason}`)}</Chip>
        {reason === "held_high_risk" && <Chip tone="warning">{t("pref.pill.held")}</Chip>}
      </>
    );
    meta = t(`pref.meta.held.${reason}`, { count: facts.frequency });
  } else {
    const ask = askState(candidate);
    const tone: Tone = ask === "ready" ? "info" : ask === "conflicting" ? "error" : "neutral";
    chips = (
      <>
        <Chip tone="warning">{t("pref.pill.inferred")}</Chip>
        <Chip tone={tone}>{t(`pref.ask.${ask}`)}</Chip>
      </>
    );
    const recommended =
      candidate.options.find((option) => option.recommended) ?? candidate.options[0];
    meta =
      ask === "ready"
        ? t("pref.meta.ready", {
            count: facts.frequency,
            scope: recommended ? optionLabel(recommended, t) : "—",
          })
        : ask === "notYet"
          ? t("pref.meta.notYet", {
              count: facts.frequency,
              left: Math.max(1, MIN_REPEATS - facts.frequency),
            })
          : t(`pref.meta.${ask}`);
  }
  return (
    <Link
      to={candidatePath(candidate)}
      className="pref-list-card"
      aria-current={selected ? "page" : undefined}
    >
      <span className="pref-list-main">
        <span className="pref-list-title">{candidateTitle(candidate)}</span>
        <span className="pref-chips">{chips}</span>
        <span className="mono pref-list-meta">{meta}</span>
      </span>
      <span className="pref-answer-button" aria-hidden="true">
        {t("pref.memory.answer")}
      </span>
    </Link>
  );
}

function HeldKinds() {
  const { t } = useI18n();
  return (
    <div className="pref-kinds">
      <span className="strong">{t("pref.held.kinds")}</span>
      {(["held_high_risk", "held_confirmed", "held_widened"] as const).map((reason) => (
        <span key={reason}>
          <span className={`tone-${HELD_TONE[reason]}`}>{t(`pref.held.kind.${reason}`)}</span>
          {t(`pref.held.kind.${reason}Text`)}
        </span>
      ))}
      <span>{t("pref.held.kind.retired")}</span>
    </div>
  );
}

type Tab = "evidence" | "body" | "history" | "sources";
const TABS: readonly Tab[] = ["evidence", "body", "history", "sources"];

function EvidenceCard({
  label,
  value,
  text,
  tone,
  mark,
}: {
  label: string;
  value: string;
  text: string;
  tone?: Tone;
  mark?: "ask" | "stop";
}) {
  const { t } = useI18n();
  return (
    <div className="pref-evidence">
      <div className="pref-evidence-head">
        <span>{label}</span>
        {mark && (
          <span className={mark === "ask" ? "tone-ok" : "tone-warning"}>
            {t(mark === "ask" ? "pref.evidence.ask" : "pref.evidence.stop")}
          </span>
        )}
      </div>
      <span className={tone ? `pref-evidence-value tone-${tone}` : "pref-evidence-value"}>
        {value}
      </span>
      <span className="pref-evidence-text">{text}</span>
    </div>
  );
}

function EvidenceGrid({ candidate }: { candidate: PreferenceCandidate }) {
  const { t, formatDate } = useI18n();
  const facts = candidate.evidence;
  return (
    <div className="pref-evidence-grid">
      <EvidenceCard
        label={t("pref.evidence.frequency")}
        value={t("pref.evidence.frequencyValue", { count: facts.frequency })}
        text={t("pref.evidence.frequencyText", { min: MIN_REPEATS })}
        mark={facts.frequency >= MIN_REPEATS && candidate.ready ? "ask" : undefined}
      />
      <EvidenceCard
        label={t("pref.evidence.scope")}
        value={t("pref.evidence.scopeValue", {
          projects: facts.project_count,
          repos: facts.repo_count,
        })}
        text={t("pref.evidence.scopeText", {
          outside: facts.outside_projects,
          why: t(`pref.evidence.why.${candidate.recommendation.scope}`),
        })}
      />
      <EvidenceCard
        label={t("pref.evidence.language")}
        value={t(`pref.evidence.language.${facts.language_strength}`)}
        text={t("pref.evidence.languageText")}
        tone={facts.language_strength === "standing" ? "info" : undefined}
        mark={
          facts.language_strength === "standing"
            ? "ask"
            : facts.language_strength === "once"
              ? "stop"
              : undefined
        }
      />
      <EvidenceCard
        label={t("pref.evidence.consistency")}
        value={t(`pref.evidence.consistency.${facts.consistency}`)}
        text={t(`pref.evidence.consistencyText.${facts.consistency}`)}
        tone={facts.consistency === "conflicting" ? "error" : undefined}
        mark={facts.consistency === "conflicting" ? "stop" : undefined}
      />
      <EvidenceCard
        label={t("pref.evidence.risk")}
        value={t(`pref.evidence.risk.${facts.risk_level}`)}
        text={t(`pref.evidence.riskText.${facts.risk_level}`)}
        tone={facts.risk_level === "high" ? "error" : "ok"}
      />
      <EvidenceCard
        label={t("pref.evidence.last")}
        value={
          facts.last_observed_at ? formatDate(facts.last_observed_at) : t("pref.evidence.none")
        }
        text={t("pref.evidence.lastText")}
      />
    </div>
  );
}

function Answer({
  candidate,
  state,
  hint,
  onAnswered,
}: {
  candidate: PreferenceCandidate;
  state: PreferenceState;
  hint: string;
  onAnswered: (answer: Answered) => void;
}) {
  const phone = useMediaQuery(PHONE_QUERY);
  const [other, setOther] = useState(false);
  const [answer, setAnswer] = useState<Answered | null>(null);
  const done = (value: Answered) => {
    setAnswer(value);
    onAnswered(value);
  };
  if (answer) return <AnsweredLine answer={answer} state={state} />;
  if (other) {
    return (
      <OtherForm
        candidate={candidate}
        state={state}
        page={phone}
        onBack={() => setOther(false)}
        onAnswered={done}
      />
    );
  }
  return (
    <AnswerControls
      candidate={candidate}
      state={state}
      hint={hint}
      onAnswered={done}
      onOther={() => setOther(true)}
    />
  );
}

function CandidateDetail({
  candidate,
  state,
  onAnswered,
}: {
  candidate: PreferenceCandidate;
  state: PreferenceState;
  onAnswered: (answer: Answered) => void;
}) {
  const { t } = useI18n();
  const memory = useMemorySource();
  const tabsId = useId();
  const [tab, setTab] = useState<Tab>("evidence");
  const memoryId = candidate.memory_id;
  return (
    <div className="pref-detail">
      <Link to={CANDIDATES_PATH} className="memory-back">
        <svg width="16" height="16" viewBox="0 0 24 24" aria-hidden="true">
          <path d="M14.5 6 8.5 12l6 6" fill="none" stroke="currentColor" strokeWidth="2" />
        </svg>
        {t("pref.memory.candidates")}
      </Link>
      <div className="pref-detail-head">
        <h2>{candidateTitle(candidate)}</h2>
        <KindPill candidate={candidate} />
        <Pill tone="neutral">{t("pref.detail.scopeUser")}</Pill>
        <span className="mono muted">
          {t("pref.detail.unconfirmed", { number: candidate.version_number ?? 1 })}
        </span>
        {memoryId && (
          <Link to={`/memory/${encodeURIComponent(memoryId)}`} className="push-right">
            {t("pref.detail.openHistory")}
          </Link>
        )}
      </div>
      <p className="pref-content">{candidate.content}</p>
      <div className="memory-tabs" role="tablist" aria-label={t("pref.detail.tabs")}>
        {TABS.map((name) => (
          <button
            key={name}
            type="button"
            role="tab"
            id={`${tabsId}-${name}`}
            aria-selected={tab === name}
            aria-controls={`${tabsId}-panel`}
            tabIndex={tab === name ? 0 : -1}
            onClick={() => setTab(name)}
            onKeyDown={(event) => {
              const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
              if (!step) return;
              const next = TABS[(TABS.indexOf(name) + step + TABS.length) % TABS.length] as Tab;
              setTab(next);
              document.getElementById(`${tabsId}-${next}`)?.focus();
            }}
          >
            {t(`pref.detail.tab.${name}`)}
          </button>
        ))}
      </div>
      <div
        className="pref-tab-panel"
        role="tabpanel"
        id={`${tabsId}-panel`}
        aria-labelledby={`${tabsId}-${tab}`}
      >
        {tab === "evidence" && <EvidenceGrid candidate={candidate} />}
        {tab === "body" && (
          <div className="stack">
            <p className="memory-body-text">{candidate.content}</p>
            <EvidenceChips candidate={candidate} names={state.names} />
          </div>
        )}
        {tab === "history" && (
          <div className="stack">
            <p className="small muted">{t("pref.detail.historyText")}</p>
            {memoryId && (
              <Link to={`/memory/${encodeURIComponent(memoryId)}`}>
                {t("pref.detail.openHistory")}
              </Link>
            )}
          </div>
        )}
        {tab === "sources" && memory && memoryId && candidate.version_number !== null && (
          <SourcesTab
            source={memory}
            memoryId={memoryId}
            versionNumber={candidate.version_number}
          />
        )}
      </div>
      <Answer
        candidate={candidate}
        state={state}
        hint={t("pref.detail.answerHint")}
        onAnswered={onAnswered}
      />
      <Note>{t("pref.detail.rejectNote")}</Note>
    </div>
  );
}

function HeldDetail({
  candidate,
  state,
  onAnswered,
}: {
  candidate: PreferenceCandidate;
  state: PreferenceState;
  onAnswered: (answer: Answered) => void;
}) {
  const { t, formatDate } = useI18n();
  const reason = candidate.held_reason ?? "held_high_risk";
  const high = needsAcknowledgement(candidate);
  return (
    <div className="pref-detail">
      <Link to={HELD_PATH} className="memory-back">
        <svg width="16" height="16" viewBox="0 0 24 24" aria-hidden="true">
          <path d="M14.5 6 8.5 12l6 6" fill="none" stroke="currentColor" strokeWidth="2" />
        </svg>
        {t("pref.memory.held")}
      </Link>
      <div className="pref-detail-head">
        <span className={`pref-tag tone-${HELD_TONE[reason]}`}>{t(`pref.held.tag.${reason}`)}</span>
        <h2>{candidateTitle(candidate)}</h2>
      </div>
      {!titleIsContent(candidate) && <p className="pref-content">{candidate.content}</p>}
      <div className="pref-chips">
        <EvidenceChips candidate={{ ...candidate, held_reason: null }} names={state.names} />
        <span className="mono pref-list-meta">
          {t("pref.held.lastObserved", {
            time: formatDate(candidate.evidence.last_observed_at ?? candidate.observed_at),
          })}
        </span>
      </div>
      <Note tone={high ? "danger" : "info"}>{t(`pref.held.note.${reason}`)}</Note>
      {candidate.options.length === 0 && <Note>{t("pref.held.retired")}</Note>}
      <Answer
        candidate={candidate}
        state={state}
        hint={t("pref.held.rejectHint")}
        onAnswered={onAnswered}
      />
    </div>
  );
}

/** The list and the detail of 推定の候補 / 保留中 (the Memory screen's two right panes). */
export function PreferencePanes({
  view,
  id,
  query,
}: {
  view: PreferenceView;
  id: string | null;
  query: string;
}) {
  const { t } = useI18n();
  const state = usePreferences();
  const { navigate } = useRouter();
  // The last answer: the candidate leaves the list, its line stays in the pane.
  const [done, setDone] = useState<Answered | null>(null);
  if (!state) return null;
  const all = state.candidates ?? [];
  const listed = (view === "candidates" ? memoryCandidates(all) : heldCandidates(all)).filter(
    (item) => {
      const needle = query.trim().toLowerCase();
      return (
        !needle ||
        item.title.toLowerCase().includes(needle) ||
        item.content.toLowerCase().includes(needle)
      );
    },
  );
  const selected = id ? (all.find((item) => candidateId(item) === id) ?? null) : null;
  const empty = view === "candidates" ? t("pref.memory.empty") : t("pref.memory.heldEmpty");
  return (
    <>
      <div className="memory-list-pane pref-list-pane">
        <p className="pref-list-hint">
          <span className="pref-hint-desktop">
            {view === "candidates" ? t("pref.memory.readyOrder") : t("pref.memory.newest")}
          </span>
          <span className="pref-hint-phone">
            {view === "candidates" ? t("pref.memory.readyOrderPhone") : t("pref.memory.newest")}
          </span>
        </p>
        {state.candidates === null ? (
          state.error ? (
            <LoadError error={state.error} onRetry={() => void state.reload()} />
          ) : (
            <p className="muted small" role="status">
              {t("app.loading")}
            </p>
          )
        ) : listed.length > 0 ? (
          <ul className="pref-list" aria-label={t("pref.memory.listLabel")}>
            {listed.map((item) => (
              <li key={candidateId(item)}>
                <ListCard candidate={item} selected={candidateId(item) === id} />
              </li>
            ))}
          </ul>
        ) : (
          <p className="memory-empty muted small">{empty}</p>
        )}
        {view === "held" && <HeldKinds />}
      </div>
      <section className="memory-detail-pane" aria-label={t("memory.detail.label")}>
        {selected ? (
          selected.kind === "memory" ? (
            <CandidateDetail key={id} candidate={selected} state={state} onAnswered={setDone} />
          ) : (
            <HeldDetail key={id} candidate={selected} state={state} onAnswered={setDone} />
          )
        ) : done && id === candidateId(done.candidate) ? (
          <AnsweredLine answer={done} state={state} />
        ) : id && state.candidates !== null ? (
          <div className="stack">
            <p className="memory-empty muted small">{t("pref.memory.notFound")}</p>
            <button
              type="button"
              className="secondary small-button"
              onClick={() => navigate(view === "candidates" ? CANDIDATES_PATH : HELD_PATH)}
            >
              {view === "candidates" ? t("pref.memory.candidates") : t("pref.memory.held")}
            </button>
          </div>
        ) : (
          <p className="memory-empty muted small">{t("pref.memory.none")}</p>
        )}
      </section>
    </>
  );
}
