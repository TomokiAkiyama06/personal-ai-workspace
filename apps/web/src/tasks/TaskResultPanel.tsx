// The 結果 panel of the task detail (issue #290, Decision 0098, 5): a read-only
// task that completed shows each node's title, role and summary in the plan's
// order, as plain text (no Markdown, no links; line breaks kept). The Design
// canvas (PAW-060) has no such panel, so it is built only from the existing
// task-card-panel, panel heading, tag (badge) and text tokens.
import { useCallback, useEffect, useState } from "react";
import { ApiError, isApiError } from "../api/client";
import { useI18n } from "../i18n";
import { errorMessage } from "../i18n/errors";
import type { NodeRole, TaskResult, TaskResultState } from "./model";
import type { TaskSource } from "./source";

const NODE_ROLES: readonly string[] = [
  "planner",
  "worker",
  "researcher",
  "reviewer",
] satisfies readonly NodeRole[];

function isNodeRole(role: string): role is NodeRole {
  return NODE_ROLES.includes(role);
}

type ResultLoad =
  | { status: "loading" }
  | { status: "ready"; data: TaskResult }
  // 404 (task_result_not_found, or the task is gone): nothing is shown.
  | { status: "gone" }
  // 403 task_result_forbidden: the repositories' ACL changed since the detail.
  | { status: "hidden" }
  | { status: "error"; error: unknown };

/**
 * `available`: the result, read once when the panel is shown and not again while
 * the task stays the same (the detail's polls do not read it). `hidden`: one line
 * that a repository the person may not read keeps it from being shown (no name,
 * no count). `none`: nothing.
 */
export function TaskResultPanel({
  source,
  taskId,
  state,
}: {
  source: TaskSource;
  taskId: string;
  state: TaskResultState;
}) {
  if (state === "none") return null;
  if (state === "hidden") return <HiddenResult />;
  return <ResultBody key={taskId} source={source} taskId={taskId} />;
}

function HiddenResult() {
  const { t } = useI18n();
  return (
    <section className="task-card-panel result-panel" aria-labelledby="result-title">
      <div className="panel-head">
        <h3 id="result-title">{t("tasks.result.title")}</h3>
      </div>
      <p className="small muted">{t("tasks.result.hidden")}</p>
    </section>
  );
}

function ResultBody({ source, taskId }: { source: TaskSource; taskId: string }) {
  const { t, formatTime } = useI18n();
  const [load, setLoad] = useState<ResultLoad>({ status: "loading" });
  const [attempt, setAttempt] = useState(0);

  // biome-ignore lint/correctness/useExhaustiveDependencies: read again only on 再試行
  useEffect(() => {
    let current = true;
    setLoad({ status: "loading" });
    source
      .getTaskResult(taskId)
      .then((data) => {
        if (current) setLoad({ status: "ready", data });
      })
      .catch((error: unknown) => {
        if (!current) return;
        if (error instanceof ApiError && error.status === 404) setLoad({ status: "gone" });
        else if (isApiError(error, "task_result_forbidden")) setLoad({ status: "hidden" });
        else setLoad({ status: "error", error });
      });
    return () => {
      current = false;
    };
  }, [source, taskId, attempt]);
  const retry = useCallback(() => setAttempt((value) => value + 1), []);

  if (load.status === "gone") return null;
  if (load.status === "hidden") return <HiddenResult />;

  return (
    <section className="task-card-panel result-panel" aria-labelledby="result-title">
      <div className="panel-head">
        <h3 id="result-title">{t("tasks.result.title")}</h3>
        {load.status === "ready" && (
          <span className="small muted">
            {t("tasks.result.completedAt", { time: formatTime(load.data.completedAt) })}
          </span>
        )}
      </div>
      {load.status === "loading" && (
        <p className="small muted" role="status">
          {t("tasks.result.loading")}
        </p>
      )}
      {load.status === "error" && (
        <div className="result-error">
          <p className="form-error" role="alert">
            {errorMessage(t, load.error)}
          </p>
          <button type="button" className="secondary small-button" onClick={retry}>
            {t("app.retry")}
          </button>
        </div>
      )}
      {load.status === "ready" &&
        (load.data.nodes.length === 0 ? (
          <p className="small muted">{t("tasks.result.empty")}</p>
        ) : (
          <ol className="plain-list result-list">
            {load.data.nodes.map((entry, index) => (
              // Keys are unique in a plan; the index keeps a damaged result shown.
              // biome-ignore lint/suspicious/noArrayIndexKey: the list never reorders
              <li key={`${index}-${entry.key}`} className="result-node">
                <div className="result-node-head">
                  <span className="strong">{entry.title}</span>
                  <span className="tag">
                    {isNodeRole(entry.role) ? t(`tasks.role.${entry.role}`) : entry.role}
                  </span>
                </div>
                <p className="result-summary">{entry.summary}</p>
              </li>
            ))}
          </ol>
        ))}
    </section>
  );
}
