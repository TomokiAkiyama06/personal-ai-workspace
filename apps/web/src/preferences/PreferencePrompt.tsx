// The chat's confirmation card (issue #38, the P1008PrefCard / PrefHighRisk /
// PrefOther boards; on a phone P1008MobilePrefCard's bottom sheet, like
// MobileApproval). Under an assistant reply it asks about at most one
// candidate: the strongest ready one not put off in this conversation (the
// human's choice; the rest wait in Memory › 推定の候補). × (あとで) calls no API
// and only hides the card in this conversation. Once answered, the card folds
// into one line (保存しました … 履歴).
import { useEffect, useRef, useState } from "react";
import { useI18n } from "../i18n";
import { PHONE_QUERY, useMediaQuery } from "../shell/common";
import { candidateId, candidateTitle, needsAcknowledgement, titleIsContent } from "./model";
import { OtherForm } from "./other";
import {
  AnswerControls,
  type Answered,
  AnsweredLine,
  EvidenceChips,
  KindPill,
  Note,
  observedMeta,
  PrefIcon,
} from "./parts";
import { type PreferenceState, usePreferences } from "./store";
import type { PreferenceCandidate } from "./types";
import "./preferences.css";

function CardBody({
  candidate,
  state,
  phone,
  onAnswered,
  onOther,
}: {
  candidate: PreferenceCandidate;
  state: PreferenceState;
  phone: boolean;
  onAnswered: (answer: Answered) => void;
  onOther: () => void;
}) {
  const { t } = useI18n();
  const ack = needsAcknowledgement(candidate);
  return (
    <>
      <div className="pref-card-text">
        {phone ? <h2>{candidateTitle(candidate)}</h2> : <h3>{candidateTitle(candidate)}</h3>}
        {!titleIsContent(candidate) && <p>{candidate.content}</p>}
      </div>
      <EvidenceChips candidate={candidate} names={state.names} consistency={!phone} />
      {ack && <Note tone="danger">{t("pref.risk.notice")}</Note>}
      <AnswerControls
        candidate={candidate}
        state={state}
        hint={phone ? undefined : t("pref.card.laterHint")}
        onAnswered={onAnswered}
        onOther={onOther}
      />
    </>
  );
}

/** The card under one assistant reply (`reply`) of `conversation`. */
export function PreferencePrompt({ conversation, reply }: { conversation: string; reply: string }) {
  const state = usePreferences();
  if (!state) return null;
  return <Prompt state={state} conversation={conversation} reply={reply} />;
}

function Prompt({
  state,
  conversation,
  reply,
}: {
  state: PreferenceState;
  conversation: string;
  reply: string;
}) {
  const { t, formatTime } = useI18n();
  const phone = useMediaQuery(PHONE_QUERY);
  const id = state.promptFor(conversation, reply);
  const [answer, setAnswer] = useState<Answered | null>(null);
  const [later, setLater] = useState(false);
  const [other, setOther] = useState(false);
  const candidate = state.candidates?.find((item) => candidateId(item) === id) ?? null;
  const heading = useRef<HTMLSpanElement>(null);
  const open = phone && candidate !== null && answer === null && !later;
  useEffect(() => {
    if (open) heading.current?.focus();
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      // Escape is × (あとで): put off in this conversation, no API call.
      if (event.key === "Escape" && !other && id !== null) {
        state.dismiss(conversation, id);
        setLater(true);
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, other, id, state, conversation]);

  if (answer) return <AnsweredLine answer={answer} state={state} />;
  // Answered elsewhere (the Memory screen) or gone: nothing to ask.
  if (!candidate || later) return null;

  const putOff = () => {
    state.dismiss(conversation, candidateId(candidate));
    setLater(true);
  };
  const ack = needsAcknowledgement(candidate);
  const close = (
    <button
      type="button"
      className="icon-button pref-close"
      aria-label={t("pref.card.later")}
      onClick={putOff}
    >
      <PrefIcon name="x" size={phone ? 16 : 14} />
    </button>
  );

  if (phone) {
    if (other) {
      return (
        <OtherForm
          candidate={candidate}
          state={state}
          page
          onBack={() => setOther(false)}
          onAnswered={setAnswer}
        />
      );
    }
    return (
      <div className="pref-sheet-layer">
        <button
          type="button"
          className="pref-sheet-backdrop"
          aria-label={t("pref.card.later")}
          tabIndex={-1}
          onClick={putOff}
        />
        <div
          role="dialog"
          aria-modal="true"
          aria-label={t("pref.card.label")}
          className="pref-sheet"
        >
          <span className="pref-sheet-grip" aria-hidden="true" />
          <div className="pref-sheet-head">
            <PrefIcon name="layers" size={15} />
            <span className="pref-card-question" ref={heading} tabIndex={-1}>
              {t("pref.card.question")}
            </span>
            <KindPill candidate={candidate} />
            {close}
          </div>
          <span className="pref-sheet-meta mono">{observedMeta(candidate, t, formatTime)}</span>
          <CardBody
            candidate={candidate}
            state={state}
            phone
            onAnswered={setAnswer}
            onOther={() => setOther(true)}
          />
          {!ack && <p className="pref-sheet-foot">{t("pref.card.footer")}</p>}
        </div>
      </div>
    );
  }

  return (
    <section
      aria-label={t("pref.card.label")}
      className={other ? "pref-card other" : ack ? "pref-card risk" : "pref-card"}
    >
      <div className="pref-card-head">
        <PrefIcon name="layers" size={15} />
        <span className="pref-card-question">
          {other ? t("pref.other.title") : t("pref.card.question")}
        </span>
        <KindPill candidate={candidate} />
        <span className="pref-card-meta mono ellipsis">
          {other ? candidate.title : observedMeta(candidate, t, formatTime)}
        </span>
        {close}
      </div>
      <div className="pref-card-body">
        {other ? (
          <OtherForm
            candidate={candidate}
            state={state}
            page={false}
            onBack={() => setOther(false)}
            onAnswered={setAnswer}
          />
        ) : (
          <CardBody
            candidate={candidate}
            state={state}
            phone={false}
            onAnswered={setAnswer}
            onOther={() => setOther(true)}
          />
        )}
      </div>
      {!other && !ack && (
        <div className="pref-card-foot">
          <PrefIcon name="lock" size={13} />
          <span>{t("pref.card.footer")}</span>
        </div>
      )}
    </section>
  );
}
