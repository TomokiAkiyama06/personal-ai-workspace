import { describe, expect, it } from "vitest";
import { designMemories, version } from "../test/memoryFake";
import { diffLines, MAX_DIFF_CELLS, sideBySide } from "./diff";
import { layoutHistory } from "./graph";
import { freshnessTag, isStale, needsReview } from "./labels";

describe("layoutHistory", () => {
  it("puts the memory's versions on lane 0, newest first, and related memories beside", () => {
    const data = designMemories();
    const own = data.versions.filter((entry) => entry.memory_id === data.mergeId);
    const related = data.versions.filter((entry) => entry.memory_id === "m-observation");
    const layout = layoutHistory({ versions: own, relations: data.relations, related });

    expect(layout.nodes.map((node) => [node.version.version_number, node.lane, node.own])).toEqual([
      [3, 0, true],
      [2, 0, true],
      [1, 1, false],
      [1, 0, true],
    ]);
    expect(layout.nodes.map((node) => node.current)).toEqual([true, false, false, false]);
    expect(layout.lanes).toBe(2);
    expect(layout.spine).toEqual({ top: 0, bottom: 3 });
    expect(layout.edges.map((edge) => [edge.from.row, edge.to.row, edge.relation])).toEqual([
      [1, 3, "supersedes"],
      [1, 2, "extends"],
      [0, 1, "supersedes"],
      [0, 1, "confirmed_from"],
    ]);
  });

  it("leaves out an edge whose far end the reader cannot see", () => {
    const v1 = version({ memory_id: "m", version_number: 1 });
    const layout = layoutHistory({
      versions: [v1],
      relations: [
        {
          from_version_id: v1.version_id,
          to_version_id: "hidden",
          relation: "extends",
          reason: null,
        },
      ],
      related: [],
    });
    expect(layout.edges).toEqual([]);
    expect(layout.spine).toEqual({ top: 0, bottom: 0 });
  });
});

describe("diff", () => {
  it("keeps, removes and adds lines, and pairs replacements side by side", () => {
    const diff = diffLines("a\nb\nc", "a\nB\nc\nd");
    expect(diff).toEqual([
      { kind: "same", text: "a" },
      { kind: "removed", text: "b" },
      { kind: "added", text: "B" },
      { kind: "same", text: "c" },
      { kind: "added", text: "d" },
    ]);
    expect(sideBySide(diff ?? [])).toEqual([
      { id: 0, left: { kind: "same", text: "a" }, right: { kind: "same", text: "a" } },
      { id: 1, left: { kind: "removed", text: "b" }, right: { kind: "added", text: "B" } },
      { id: 2, left: { kind: "same", text: "c" }, right: { kind: "same", text: "c" } },
      { id: 3, left: null, right: { kind: "added", text: "d" } },
    ]);
  });

  it("refuses a diff that is too large to compute", () => {
    const side = Math.ceil(Math.sqrt(MAX_DIFF_CELLS)) + 1;
    const text = Array.from({ length: side }, (_, index) => String(index)).join("\n");
    expect(diffLines(text, `${text}\nx`)).toBeNull();
  });
});

describe("labels", () => {
  const now = Date.parse("2026-09-30T00:00:00Z");

  it("treats a revalidate memory past its time as stale, even before the job marks it", () => {
    const due = version({
      memory_id: "m",
      freshness_policy: "revalidate",
      verified_at: "2026-09-01T00:00:00Z",
      revalidate_after: 86_400,
    });
    expect(isStale(due, now)).toBe(true);
    expect(freshnessTag(due, now).label).toBe("memory.fresh.stale");
    expect(needsReview(due, now)).toBe(true);
  });

  it("does not ask to review a memory that is no longer active", () => {
    const old = version({ memory_id: "m", status: "superseded", confirmation_state: "inferred" });
    expect(needsReview(old, now)).toBe(false);
    expect(freshnessTag(old, now).label).toBe("memory.fresh.old");
  });
});
