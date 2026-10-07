// The compact health state of the Global Header (ShellDark: "● GPU 38% · Queue 2";
// docs/UI_DESIGN.md "通常時はHeader等にcompactなhealth stateのみ表示する").
//
// Owner / Admin (admin.system_health.view): GPU utilization and the queue while
// everything is normal, the worst component when not ("Backup 異常"), and the
// chip opens 管理 › サーバー監視. Everyone else (system_health.summary.read): the
// overall severity and whether Codex / Claude are available, nothing to open
// (Decision 0059 §3). Without a source (tests) or before the first answer
// nothing is shown; a failed refresh keeps the last state (Decision 0080).
import { useEffect, useState } from "react";
import { showsAdmin, useSignedIn } from "../auth/session";
import { useI18n } from "../i18n";
import { Link } from "../router";
import {
  abnormal,
  componentOf,
  type HealthReport,
  type HealthSummary,
  numberOf,
  queueOf,
  type Severity,
  useHealthSource,
} from "./model";
import { componentName, severityName } from "./text";
import "./health.css";

/** How often the chip reads the state again (and when the tab becomes visible). */
export const CHIP_REFRESH_SECONDS = 30;

type State =
  | { kind: "none" }
  | { kind: "admin"; report: HealthReport }
  | { kind: "user"; summary: HealthSummary };

function useChipState(admin: boolean): State {
  const source = useHealthSource();
  const [state, setState] = useState<State>({ kind: "none" });
  useEffect(() => {
    setState({ kind: "none" });
    if (!source) return;
    let cancelled = false;
    // One read at a time, so an older answer never replaces a newer one.
    let reading = false;
    const read = () => {
      if (reading) return;
      if (typeof document !== "undefined" && document.visibilityState === "hidden") return;
      reading = true;
      const request: Promise<State> = admin
        ? source.report().then((report) => ({ kind: "admin", report }))
        : source.summary().then((summary) => ({ kind: "user", summary }));
      request.then(
        (next) => {
          reading = false;
          if (!cancelled) setState(next);
        },
        () => {
          // Only a hint: the last state stays (or nothing is shown).
          reading = false;
        },
      );
    };
    read();
    const timer = window.setInterval(read, CHIP_REFRESH_SECONDS * 1000);
    document.addEventListener("visibilitychange", read);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", read);
    };
  }, [source, admin]);
  return state;
}

export function HealthChip() {
  const { t } = useI18n();
  const { user } = useSignedIn();
  const admin = showsAdmin(user.system_role);
  const state = useChipState(admin);
  if (state.kind === "none") return null;

  let severity: Severity;
  let text: string;
  if (state.kind === "admin") {
    const report = state.report;
    severity = report.severity;
    const problems = abnormal(report);
    const worst = problems[0];
    if (worst) {
      const component = componentName(t, worst.component);
      text =
        problems.length > 1
          ? t("health.chip.problemMore", { component, more: problems.length - 1 })
          : t("health.chip.problem", { component });
    } else {
      const compute = componentOf(report, "compute");
      const gpu = compute ? numberOf(compute.metrics, "utilization_percent") : null;
      const queue = queueOf(componentOf(report, "task_queue"));
      const queued = queue ? queue.running + queue.queued : 0;
      text =
        gpu === null
          ? t("health.chip.queueOnly", { queue: queued })
          : t("health.chip.admin", { gpu: `${Math.round(gpu)}%`, queue: queued });
    }
  } else {
    severity = state.summary.severity;
    const down = Object.entries(state.summary.connections)
      .filter(([, available]) => available !== "available")
      .map(([kind]) =>
        kind === "codex" || kind === "claude" ? t(`health.connection.${kind}`) : kind,
      );
    text =
      down.length === 0
        ? t("health.chip.available")
        : t("health.chip.unavailable", { kinds: down.join(" · ") });
  }

  const label = t("health.chip.label", { state: `${severityName(t, severity)} · ${text}` });
  const content = (
    <>
      <span className={`health-dot-mark sev-${severity}`} aria-hidden="true" />
      <span className="health-chip-text">{text}</span>
    </>
  );
  return admin ? (
    <Link to="/admin/monitoring" className={`header-health sev-${severity}`} aria-label={label}>
      {content}
    </Link>
  ) : (
    <span className={`header-health sev-${severity}`} role="status" aria-label={label}>
      {content}
    </span>
  );
}
