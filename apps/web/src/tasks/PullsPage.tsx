// プルリクエスト (the design's PullRequest board; docs/UI_DESIGN.md §12): the pull
// requests the agents delivered, what they still need before Merge Ready, and the
// human's merge authority. The Backend has no merge API, so the merge button is
// shown disabled and the pull request is merged on GitHub.
import { useCallback, useEffect, useState } from "react";
import { useI18n } from "../i18n";
import { errorMessage } from "../i18n/errors";
import { Link, useRouter } from "../router";
import { Icon } from "../shell/icons";
import type { PullRequestRecord } from "./model";
import { decodeSegment, REFRESH_MS, shortId } from "./model";
import { CheckMark } from "./parts";
import { type TaskSource, useTaskSource } from "./source";
import { TASKS_PATH, Unavailable } from "./TasksPage";
import "./tasks.css";

export const PULLS_PATH = "/pulls";

type Filter = "all" | "ready" | "review" | "draft";
const FILTERS: readonly Filter[] = ["all", "ready", "review", "draft"];

function matches(pr: PullRequestRecord, filter: Filter): boolean {
  switch (filter) {
    case "all":
      return true;
    case "ready":
      return pr.mergeReady;
    case "review":
      return pr.state === "open" && !pr.mergeReady;
    case "draft":
      return pr.state === "draft";
  }
}

function selectedPullId(path: string): string | null {
  if (!path.startsWith(`${PULLS_PATH}/`)) return null;
  const id = path.slice(PULLS_PATH.length + 1);
  return id ? (decodeSegment(id) ?? id) : null;
}

/** A GitHub link is followed only if it is an https URL (the Backend records one). */
function safeUrl(url: string): string | null {
  try {
    return new URL(url).protocol === "https:" ? url : null;
  } catch {
    return null;
  }
}

export function PullsPage() {
  const source = useTaskSource();
  if (!source) return <Unavailable title="pulls.title" />;
  return <PullsView source={source} />;
}

type Load =
  | { status: "loading" }
  | { status: "error"; error: unknown }
  | {
      status: "ready";
      data: readonly PullRequestRecord[];
    };

function PullsView({ source }: { source: TaskSource }) {
  const { t } = useI18n();
  const { path } = useRouter();
  const selectedId = selectedPullId(path);
  const [load, setLoad] = useState<Load>({ status: "loading" });
  const [filter, setFilter] = useState<Filter>("all");
  const reload = useCallback(() => {
    source
      .listPullRequests()
      .then((data) => setLoad({ status: "ready", data }))
      .catch((error: unknown) => setLoad({ status: "error", error }));
  }, [source]);
  useEffect(reload, [reload]);

  const all = load.status === "ready" ? load.data : [];
  // Review, evaluation and Merge Ready change and new PRs appear while the list
  // is shown; a failed background read keeps what is shown.
  const loaded = load.status === "ready";
  useEffect(() => {
    if (!loaded) return;
    const timer = window.setInterval(() => {
      source
        .listPullRequests()
        .then((data) => setLoad({ status: "ready", data }))
        .catch(() => {});
    }, REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [loaded, source]);
  // A selected record the bounded list does not hold (an older one a task links
  // to) is read by its id; a refused or missing one is not shown.
  const listed = !loaded || !selectedId || all.some((pr) => pr.id === selectedId);
  const [single, setSingle] = useState<PullRequestRecord | null>(null);
  useEffect(() => {
    if (listed || !selectedId) return;
    let current = true;
    source
      .getPullRequest(selectedId)
      .then((pr) => {
        if (current) setSingle(pr);
      })
      .catch(() => {
        if (current) setSingle(null);
      });
    return () => {
      current = false;
    };
  }, [listed, selectedId, source]);
  const visible = all.filter((pr) => matches(pr, filter));
  const shown =
    all.find((pr) => pr.id === (selectedId ?? visible[0]?.id)) ??
    (single !== null && single.id === selectedId ? single : null);

  return (
    <div className={selectedId ? "tasks-screen has-selection" : "tasks-screen"}>
      <div className="screen-head">
        <h1>{t("pulls.title")}</h1>
        <fieldset className="filter-chips">
          <legend className="visually-hidden">{t("pulls.filter.label")}</legend>
          {FILTERS.map((option) => {
            const count = all.filter((pr) => matches(pr, option)).length;
            return (
              <button
                key={option}
                type="button"
                className="chip"
                aria-pressed={filter === option}
                onClick={() => setFilter(option)}
              >
                {option === "all" ? t("tasks.filter.all") : t(`pulls.filter.${option}`)}
                {option !== "all" && count > 0 && ` ${count}`}
              </button>
            );
          })}
        </fieldset>
      </div>
      <div className="tasks-body">
        <nav className="task-list-pane" aria-label={t("pulls.list")}>
          {load.status === "loading" && (
            <p className="muted small" role="status">
              {t("pulls.loading")}
            </p>
          )}
          {load.status === "error" && (
            <div className="stack">
              <p className="form-error" role="alert">
                {errorMessage(t, load.error)}
              </p>
              <button type="button" className="secondary small-button" onClick={reload}>
                {t("app.retry")}
              </button>
            </div>
          )}
          {load.status === "ready" && all.length === 0 && (
            <p className="muted small">{t("pulls.empty")}</p>
          )}
          <ul className="task-cards">
            {visible.map((pr) => (
              <li key={pr.id}>
                <Link
                  to={`${PULLS_PATH}/${encodeURIComponent(pr.id)}`}
                  className="task-card"
                  aria-current={pr.id === shown?.id ? "page" : undefined}
                >
                  <span className="task-card-line">
                    <span className="task-card-title ellipsis">{pr.title}</span>
                    <span className="mono muted task-id">#{pr.number}</span>
                  </span>
                  <span className="task-card-line">
                    <PrStatus pr={pr} tag />
                    <span className="mono muted ellipsis">{pr.repository}</span>
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        </nav>
        <div className="task-detail-pane">
          {shown ? (
            <PullDetail pr={shown} />
          ) : (
            load.status === "ready" &&
            all.length > 0 && <p className="muted small">{t("pulls.select")}</p>
          )}
        </div>
      </div>
    </div>
  );
}

function PrStatus({ pr, tag = false }: { pr: PullRequestRecord; tag?: boolean }) {
  const { t } = useI18n();
  const [tone, label] = pr.mergeReady
    ? (["ok", t("pulls.mergeReady")] as const)
    : pr.state === "open"
      ? (["info", t("pulls.filter.review")] as const)
      : pr.state === "draft"
        ? (["neutral", t("pulls.filter.draft")] as const)
        : pr.state === "merged"
          ? (["ok", t("tasks.pr.merged")] as const)
          : (["muted", t("tasks.pr.closed")] as const);
  if (tag) return <span className={`state-tag tone-${tone}`}>{label}</span>;
  return (
    <span className={`state-pill tone-${tone}`}>
      <span className="state-dot" aria-hidden="true" />
      {label}
    </span>
  );
}

type ConditionState = "done" | "failed" | "waiting";

function Condition({
  label,
  state,
  value,
}: {
  label: string;
  state: ConditionState;
  value: string;
}) {
  return (
    <li className={`condition condition-${state}`}>
      <span className="condition-icon" aria-hidden="true">
        {state === "done" && <CheckMark />}
      </span>
      <span className="condition-label">{label}</span>
      <span className="mono condition-value">{value}</span>
    </li>
  );
}

function PullDetail({ pr }: { pr: PullRequestRecord }) {
  const { t } = useI18n();
  const url = safeUrl(pr.url);
  const merged = pr.state === "merged";
  return (
    <div className="pull-detail">
      <Link to={PULLS_PATH} className="back-row phone-only">
        <Icon name="back" size={18} />
        {t("pulls.back")}
      </Link>
      <div className="pull-main">
        <div className="task-title-block">
          <div className="task-title-line">
            <h2>{pr.title}</h2>
            <span className="mono muted">#{pr.number}</span>
          </div>
          <div className="task-title-line">
            <PrStatus pr={pr} />
            <span className="mono muted">
              {pr.branch} → {pr.base}
            </span>
            <span className="mono muted">
              {pr.repository} ·{" "}
              <Link to={`${TASKS_PATH}/${encodeURIComponent(pr.taskId)}`}>
                {t("pulls.task", { task: shortId(pr.taskId) })}
              </Link>
            </span>
          </div>
        </div>
        <section className="task-card-panel flush" aria-labelledby="conditions-title">
          <div className="panel-head padded">
            <h3 id="conditions-title">{t("pulls.conditions")}</h3>
            <span className="small muted">{t("pulls.conditionsHint")}</span>
          </div>
          <ul className="plain-list">
            <Condition
              label={t("pulls.condition.pr")}
              state={pr.state === "closed" ? "failed" : pr.state === "draft" ? "waiting" : "done"}
              value={t(`tasks.pr.${pr.state}`)}
            />
            <Condition
              label={t("pulls.condition.tests")}
              state={
                pr.evaluation === "passed"
                  ? "done"
                  : pr.evaluation === "failed"
                    ? "failed"
                    : "waiting"
              }
              value={t(`tasks.evaluation.${pr.evaluation}`)}
            />
            <Condition
              label={t("pulls.condition.review")}
              state={
                pr.review === "approved"
                  ? "done"
                  : pr.review === "changes_requested"
                    ? "failed"
                    : "waiting"
              }
              value={t(`tasks.review.${pr.review}`)}
            />
            <Condition
              label={t("pulls.condition.human")}
              state={merged ? "done" : "waiting"}
              value={merged ? t("pulls.condition.done") : t("pulls.condition.notDone")}
            />
          </ul>
        </section>
      </div>
      <aside className="pull-side">
        <section className="task-card-panel merge-panel" aria-labelledby="merge-title">
          <div className="stack-xs">
            <h3 id="merge-title">{t("pulls.merge")}</h3>
            <p className="small muted">{t("pulls.mergeBody")}</p>
          </div>
          <button type="button" className="wide" disabled aria-describedby="merge-unavailable">
            {t("pulls.mergeButton")}
          </button>
          <p id="merge-unavailable" className="small muted">
            {t("pulls.mergeUnavailable")}
          </p>
          <div className="actions">
            {url && (
              <a className="button-link" href={url} target="_blank" rel="noopener noreferrer">
                {t("pulls.openGitHub")}
              </a>
            )}
            <Link
              className="button-link quiet"
              to={`${TASKS_PATH}/${encodeURIComponent(pr.taskId)}`}
            >
              {t("pulls.openTask")}
            </Link>
          </div>
        </section>
        <div className="authority-note">
          <Icon name="key" size={16} />
          <p>{t("pulls.authority")}</p>
        </div>
      </aside>
    </div>
  );
}
