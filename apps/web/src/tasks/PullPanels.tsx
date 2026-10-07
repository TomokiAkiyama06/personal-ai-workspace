// The PR screen's panels that come from the Backend's records of the delivered
// pull request (the PullRequest board; issue #185 item 6, Decision 0078):
// 変更されたファイル, レビューの要点 and 監査. Each is read once for the pull
// request shown; a panel whose read failed says so and the others stay.
import { useEffect, useState } from "react";
import { useI18n } from "../i18n";
import { errorMessage } from "../i18n/errors";
import { Link } from "../router";
import type { AuditRow, ChangedFiles, ReviewSummary } from "./model";

type Read<T> =
  | { status: "loading" }
  | { status: "error"; error: unknown }
  | { status: "ready"; data: T };

/** Read `load()` for `id` (again when it changes); a stale answer is dropped. */
export function useRead<T>(load: () => Promise<T>, id: string): Read<T> {
  const [read, setRead] = useState<{ id: string; value: Read<T> }>({
    id,
    value: { status: "loading" },
  });
  // biome-ignore lint/correctness/useExhaustiveDependencies: read again only for another id
  useEffect(() => {
    let current = true;
    setRead({ id, value: { status: "loading" } });
    load()
      .then((data) => {
        if (current) setRead({ id, value: { status: "ready", data } });
      })
      .catch((error: unknown) => {
        if (current) setRead({ id, value: { status: "error", error } });
      });
    return () => {
      current = false;
    };
  }, [id]);
  return read.id === id ? read.value : { status: "loading" };
}

export const PULLS_BASE = "/pulls";

export function filesPath(id: string): string {
  return `${PULLS_BASE}/${encodeURIComponent(id)}/files`;
}

export function diffPath(id: string, index: number): string {
  return `${filesPath(id)}/${index}`;
}

export function LineCounts({ additions, deletions }: { additions: number; deletions: number }) {
  return (
    <>
      <span className="mono line-add">+{additions}</span>
      <span className="mono line-delete">−{deletions}</span>
    </>
  );
}

export function ChangedFilesPanel({
  prId,
  changes,
}: {
  prId: string;
  changes: Read<ChangedFiles>;
}) {
  const { t } = useI18n();
  const data = changes.status === "ready" && changes.data.recorded ? changes.data : null;
  const firstDiff = data?.files.find((file) => file.hasPatch);
  return (
    <section className="task-card-panel flush" aria-labelledby="files-title">
      <div className="panel-head padded files-head">
        <h3 id="files-title">{t("pulls.files.title")}</h3>
        {data && (
          <>
            <span className="mono muted small">
              {t("pulls.filesCount", { count: data.files.length })}
            </span>
            <LineCounts additions={data.additions} deletions={data.deletions} />
            {firstDiff && (
              <Link className="push-right files-open" to={diffPath(prId, firstDiff.index)}>
                {t("pulls.files.open")}
              </Link>
            )}
          </>
        )}
      </div>
      {changes.status === "loading" && (
        <p className="panel-note muted small" role="status">
          {t("pulls.files.loading")}
        </p>
      )}
      {changes.status === "error" && (
        <p className="panel-note form-error" role="alert">
          {errorMessage(t, changes.error)}
        </p>
      )}
      {changes.status === "ready" && !changes.data.recorded && (
        <p className="panel-note muted small">{t("pulls.files.notRecorded")}</p>
      )}
      {data && (
        <ul className="plain-list">
          {data.files.map((file) => (
            <li key={file.index} className="file-row">
              <span className="file-path">
                {file.hasPatch ? (
                  <Link to={diffPath(prId, file.index)} className="mono">
                    {file.path}
                  </Link>
                ) : (
                  <span className="mono">{file.path}</span>
                )}
                {file.previousPath && (
                  <span className="mono muted small">
                    {t("pulls.files.renamedFrom", { path: file.previousPath })}
                  </span>
                )}
              </span>
              <LineCounts additions={file.additions} deletions={file.deletions} />
            </li>
          ))}
        </ul>
      )}
      {data?.truncated && <p className="panel-note muted small">{t("pulls.files.truncated")}</p>}
    </section>
  );
}

/** The avatar's two letters: "Codex" → "Co". */
function initials(name: string): string {
  const letters = name.replace(/[^\p{L}\p{N}]/gu, "");
  return letters.slice(0, 1).toUpperCase() + letters.slice(1, 2).toLowerCase();
}

export function ReviewPanel({ review }: { review: Read<ReviewSummary> }) {
  const { t, formatTime } = useI18n();
  return (
    <section className="task-card-panel review-panel" aria-labelledby="review-title">
      <h3 id="review-title">{t("pulls.review.title")}</h3>
      {review.status === "error" && (
        <p className="form-error" role="alert">
          {errorMessage(t, review.error)}
        </p>
      )}
      {review.status === "ready" && review.data.reviewers.length === 0 && (
        <p className="small muted">{t("pulls.review.none")}</p>
      )}
      {review.status === "ready" &&
        review.data.reviewers.map((reviewer) => {
          const name = reviewer.agent ?? reviewer.title;
          return (
            <div key={reviewer.key} className="reviewer">
              <span className="reviewer-avatar" aria-hidden="true">
                {initials(name)}
              </span>
              <div className="stack-xs">
                <span className="reviewer-line">
                  {t("pulls.review.line", {
                    reviewer: name,
                    state: t(`tasks.node.${reviewer.state}`),
                  })}
                </span>
                <span className="small muted">
                  {[
                    reviewer.agent ? reviewer.title : null,
                    reviewer.model,
                    reviewer.finishedAt ? formatTime(reviewer.finishedAt) : null,
                  ]
                    .filter(Boolean)
                    .join(" · ")}
                </span>
              </div>
            </div>
          );
        })}
    </section>
  );
}

function clock(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

export function AuditPanel({ audit }: { audit: Read<readonly AuditRow[]> }) {
  const { t } = useI18n();
  return (
    <section className="task-card-panel audit-panel" aria-labelledby="audit-title">
      <span id="audit-title" className="section-label">
        {t("pulls.audit.title")}
      </span>
      {audit.status === "error" && (
        <p className="form-error" role="alert">
          {errorMessage(t, audit.error)}
        </p>
      )}
      {audit.status === "ready" && audit.data.length === 0 && (
        <p className="small muted">{t("pulls.audit.empty")}</p>
      )}
      {audit.status === "ready" && audit.data.length > 0 && (
        <ul className="plain-list audit-rows">
          {audit.data.map((row, index) => (
            <li
              // Rows have no id: the Backend answers a closed projection.
              // biome-ignore lint/suspicious/noArrayIndexKey: the list is replaced as a whole
              key={index}
              className={`mono audit-row decision-${row.decision}`}
            >
              <time dateTime={row.occurredAt}>{clock(row.occurredAt)}</time> {row.action}{" "}
              {t(`pulls.audit.${row.decision}`)} <span className="muted">{row.reason}</span>
              <span className="visually-hidden"> · {t(`pulls.audit.actor.${row.actor}`)}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
