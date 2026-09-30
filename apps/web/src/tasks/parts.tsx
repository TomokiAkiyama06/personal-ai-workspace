// Small pieces shared by the task and pull request screens.
import { useEffect, useState } from "react";
import { useI18n } from "../i18n";
import type { NodeState, TaskState, WaitReason } from "./model";

export type Tone = "info" | "warning" | "ok" | "error" | "neutral" | "muted";

/** The colour of a task state (the design: running blue, approval amber, done green). */
export function taskTone(state: TaskState, waitReason: WaitReason | null): Tone {
  switch (state) {
    case "running":
    case "evaluating":
      return "info";
    case "waiting":
      return waitReason === "resource" ? "neutral" : "warning";
    case "completed":
      return "ok";
    case "failed":
      return "error";
    case "cancelled":
      return "muted";
    case "queued":
    case "paused":
      return "neutral";
  }
}

export function nodeTone(state: NodeState): Tone {
  switch (state) {
    case "running":
      return "info";
    case "succeeded":
      return "ok";
    case "failed":
    case "blocked":
      return "error";
    case "cancelled":
      return "muted";
    case "pending":
    case "ready":
      return "neutral";
  }
}

/** "実行中", or the wait reason of a waiting task ("承認待ち"). */
export function useTaskStateLabel(): (state: TaskState, waitReason: WaitReason | null) => string {
  const { t } = useI18n();
  return (state, waitReason) =>
    state === "waiting" && waitReason ? t(`tasks.wait.${waitReason}`) : t(`tasks.state.${state}`);
}

/** The state as a pill with a dot (never colour alone: the label is the text). */
export function StatePill({
  state,
  waitReason,
}: {
  state: TaskState;
  waitReason: WaitReason | null;
}) {
  const label = useTaskStateLabel();
  return (
    <span className={`state-pill tone-${taskTone(state, waitReason)}`}>
      <span className="state-dot" aria-hidden="true" />
      {label(state, waitReason)}
    </span>
  );
}

/** The current time, updated every `interval` ms while `active` (running timers). */
export function useNow(active: boolean, interval = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), interval);
    return () => window.clearInterval(timer);
  }, [active, interval]);
  return now;
}

/** The design's check mark (a done step or condition). */
export function CheckMark() {
  return (
    <svg
      width="11"
      height="11"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="3.4"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <path d="m5 12.5 4.5 4.5L19 7" />
    </svg>
  );
}
