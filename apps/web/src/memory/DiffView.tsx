import { useMemo } from "react";
import { useI18n } from "../i18n";
import { type DiffLine, diffLines, sideBySide } from "./diff";

function Cell({ line }: { line: DiffLine | null }) {
  if (!line) return <span className="memory-diff-cell empty" />;
  if (line.kind === "removed") {
    return (
      <del className="memory-diff-cell removed">
        <span aria-hidden="true">− </span>
        {line.text || " "}
      </del>
    );
  }
  if (line.kind === "added") {
    return (
      <ins className="memory-diff-cell added">
        <span aria-hidden="true">+ </span>
        {line.text || " "}
      </ins>
    );
  }
  return <span className="memory-diff-cell">{line.text || " "}</span>;
}

/** Two texts side by side, the removed lines facing the lines added in their place. */
export function DiffView({
  before,
  after,
  beforeLabel,
  afterLabel,
}: {
  before: string;
  after: string;
  beforeLabel: string;
  afterLabel: string;
}) {
  const { t } = useI18n();
  const rows = useMemo(() => {
    const diff = diffLines(before, after);
    return diff ? sideBySide(diff) : null;
  }, [before, after]);
  if (!rows) return <p className="muted small">{t("memory.diff.tooLarge")}</p>;
  const changed = rows.some((row) => row.left?.kind !== "same" || row.right?.kind !== "same");
  return (
    <div className="memory-diff">
      <div className="memory-diff-head">
        <span>{beforeLabel}</span>
        <span>{afterLabel}</span>
      </div>
      {changed ? (
        <div className="memory-diff-rows">
          {rows.map((row, index) => (
            <div className="memory-diff-row" key={index}>
              <Cell line={row.left} />
              <Cell line={row.right} />
            </div>
          ))}
        </div>
      ) : (
        <p className="muted small memory-diff-same">{t("memory.diff.same")}</p>
      )}
    </div>
  );
}
