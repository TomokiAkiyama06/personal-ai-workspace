// A line diff for comparing two versions' text (the History Graph's "Diff",
// UI_DESIGN.md §7, and the optimistic lock conflict). Plain LCS on lines: memory
// text is short, and a diff that would be too large to compute is refused.

export type DiffKind = "same" | "removed" | "added";

export interface DiffLine {
  kind: DiffKind;
  text: string;
}

export interface DiffRow {
  /** The row's position, as a stable React key. */
  id: number;
  left: DiffLine | null;
  right: DiffLine | null;
}

/** Above this many line pairs the diff is not computed (null). */
export const MAX_DIFF_CELLS = 4_000_000;

function lines(text: string): string[] {
  return text === "" ? [] : text.replace(/\r\n?/g, "\n").split("\n");
}

/** The lines of `before` and `after` in order: kept, removed and added. */
export function diffLines(before: string, after: string): DiffLine[] | null {
  const a = lines(before);
  const b = lines(after);
  if (a.length * b.length > MAX_DIFF_CELLS) return null;
  const width = b.length + 1;
  // lcs[i * width + j]: the longest common subsequence of a[i..] and b[j..].
  const lcs = new Uint32Array((a.length + 1) * width);
  for (let i = a.length - 1; i >= 0; i--) {
    for (let j = b.length - 1; j >= 0; j--) {
      lcs[i * width + j] =
        a[i] === b[j]
          ? (lcs[(i + 1) * width + j + 1] ?? 0) + 1
          : Math.max(lcs[(i + 1) * width + j] ?? 0, lcs[i * width + j + 1] ?? 0);
    }
  }
  const out: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < a.length && j < b.length) {
    const left = a[i] as string;
    const right = b[j] as string;
    if (left === right) {
      out.push({ kind: "same", text: left });
      i++;
      j++;
    } else if ((lcs[(i + 1) * width + j] ?? 0) >= (lcs[i * width + j + 1] ?? 0)) {
      out.push({ kind: "removed", text: left });
      i++;
    } else {
      out.push({ kind: "added", text: right });
      j++;
    }
  }
  for (; i < a.length; i++) out.push({ kind: "removed", text: a[i] as string });
  for (; j < b.length; j++) out.push({ kind: "added", text: b[j] as string });
  return out;
}

/** The diff as two columns: a removed line faces the line added in its place. */
export function sideBySide(diff: readonly DiffLine[]): DiffRow[] {
  const rows: DiffRow[] = [];
  let removed: DiffLine[] = [];
  let added: DiffLine[] = [];
  const flush = () => {
    const count = Math.max(removed.length, added.length);
    for (let k = 0; k < count; k++) {
      rows.push({ id: rows.length, left: removed[k] ?? null, right: added[k] ?? null });
    }
    removed = [];
    added = [];
  };
  for (const line of diff) {
    if (line.kind === "removed") removed.push(line);
    else if (line.kind === "added") added.push(line);
    else {
      flush();
      rows.push({ id: rows.length, left: line, right: line });
    }
  }
  flush();
  return rows;
}
