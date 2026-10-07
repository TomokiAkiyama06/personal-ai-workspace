// 差分 (the design's MobileDiff board; issue #185 item 6, Decision 0078): one
// changed file of a delivered pull request with its unified diff, the previous /
// next file and the file list. The diff is what the Backend recorded when the
// pull request was delivered (bounded, credentials redacted); it is shown as
// text only.
import { useI18n } from "../i18n";
import { errorMessage } from "../i18n/errors";
import { Link } from "../router";
import { Icon } from "../shell/icons";
import { diffLines, type PullRequestRecord, shortId } from "./model";
import { ChangedFilesPanel, diffPath, LineCounts, PULLS_BASE, useRead } from "./PullPanels";
import type { TaskSource } from "./source";

function pullPath(id: string): string {
  return `${PULLS_BASE}/${encodeURIComponent(id)}`;
}

export function DiffView({
  source,
  pr,
  index,
}: {
  source: TaskSource;
  pr: PullRequestRecord;
  index: number;
}) {
  const { t } = useI18n();
  const read = useRead(() => source.getFileDiff(pr.id, index), `${pr.id}/${index}`);
  const file = read.status === "ready" ? read.data : null;
  const lines = file?.patch ? diffLines(file.patch) : [];
  const hunks = lines.filter((line) => line.kind === "hunk").length;
  return (
    <article className="diff-view" aria-labelledby="diff-title">
      <div className="diff-head">
        <Link to={pullPath(pr.id)} className="icon-button" aria-label={t("diff.back")}>
          <Icon name="back" size={18} />
        </Link>
        <span className="diff-title-block">
          <h2 id="diff-title" className="mono ellipsis">
            {file?.path ?? ""}
          </h2>
          {file && (
            <span className="mono muted small">
              {t("diff.position", {
                index: file.index + 1,
                count: file.count,
                task: shortId(pr.taskId),
              })}
            </span>
          )}
        </span>
      </div>
      {read.status === "loading" && (
        <p className="muted small" role="status">
          {t("diff.loading")}
        </p>
      )}
      {read.status === "error" && (
        <p className="form-error" role="alert">
          {errorMessage(t, read.error)}
        </p>
      )}
      {file && (
        <>
          <div className="diff-stats">
            <LineCounts additions={file.additions} deletions={file.deletions} />
            {hunks > 0 && (
              <span className="mono muted small">{t("diff.hunks", { count: hunks })}</span>
            )}
          </div>
          {file.patch === null ? (
            <p className="muted small diff-note">{t("diff.noPatch")}</p>
          ) : (
            // Each line is its own element so the colours follow its kind; the
            // markers (+ / -) stay in the text for those who do not see colours.
            <pre className="diff-lines">
              {lines.map((line, number) => (
                <span
                  // biome-ignore lint/suspicious/noArrayIndexKey: lines are positional
                  key={number}
                  className={`diff-line diff-${line.kind}`}
                >
                  {line.text || " "}
                </span>
              ))}
            </pre>
          )}
          {file.patchTruncated && <p className="muted small diff-note">{t("diff.truncated")}</p>}
          <nav className="diff-nav" aria-label={t("diff.list")}>
            {file.index > 0 ? (
              <Link
                to={diffPath(pr.id, file.index - 1)}
                className="button-link square"
                aria-label={t("diff.previous")}
              >
                <Icon name="back" size={18} />
              </Link>
            ) : (
              <span className="button-link square disabled" aria-hidden="true">
                <Icon name="back" size={18} />
              </span>
            )}
            <Link to={`${pullPath(pr.id)}/files`} className="button-link strong">
              {t("diff.list")}
            </Link>
            {file.index + 1 < file.count ? (
              <Link
                to={diffPath(pr.id, file.index + 1)}
                className="button-link square"
                aria-label={t("diff.next")}
              >
                <Icon name="chevronRight" size={18} />
              </Link>
            ) : (
              <span className="button-link square disabled" aria-hidden="true">
                <Icon name="chevronRight" size={18} />
              </span>
            )}
          </nav>
        </>
      )}
    </article>
  );
}

/** The changed files on their own screen (the diff's ファイル一覧). */
export function FilesView({ source, pr }: { source: TaskSource; pr: PullRequestRecord }) {
  const { t } = useI18n();
  const changes = useRead(() => source.getChangedFiles(pr.id), pr.id);
  return (
    <article className="diff-view" aria-labelledby="files-view-title">
      <div className="diff-head">
        <Link to={pullPath(pr.id)} className="icon-button" aria-label={t("diff.back")}>
          <Icon name="back" size={18} />
        </Link>
        <span className="diff-title-block">
          <h2 id="files-view-title">{pr.title}</h2>
          <span className="mono muted small">
            #{pr.number} · {pr.repository}
          </span>
        </span>
      </div>
      <ChangedFilesPanel prId={pr.id} changes={changes} />
    </article>
  );
}
