import { describe, expect, it } from "vitest";
import { designCandidates, heldCandidate, memoryCandidate } from "../test/preferenceFake";
import {
  askState,
  badgeCount,
  candidateId,
  candidatePath,
  heldCandidates,
  memoryCandidates,
  namesFrom,
  needsAcknowledgement,
  preferencePath,
  promptCandidate,
} from "./model";

describe("preference rules of the screens", () => {
  it("asks about the strongest ready candidate, skipping the ones put off", () => {
    const all = designCandidates();
    // Ready: pytest (4, standing) and review (3, standing); the held ones are not ready.
    expect(promptCandidate(all, new Set())?.title).toBe("pytest は -x を付けて実行する");
    expect(promptCandidate(all, new Set(["m-pytest"]))?.title).toBe(
      "レビューは Codex と Claude の両方",
    );
    expect(promptCandidate(all, new Set(["m-pytest", "m-review"]))).toBeNull();
  });

  it("puts standing words before more observations, then the newest", () => {
    const many = memoryCandidate({
      memory_id: "m-many",
      evidence: { ...memoryCandidate({ memory_id: "x" }).evidence, frequency: 9 },
    });
    const standing = memoryCandidate({
      memory_id: "m-standing",
      evidence: {
        ...memoryCandidate({ memory_id: "x" }).evidence,
        frequency: 1,
        language_strength: "standing",
      },
    });
    expect(promptCandidate([many, standing], new Set())?.memory_id).toBe("m-standing");
    const older = memoryCandidate({ memory_id: "m-old", observed_at: "2026-10-01T00:00:00Z" });
    const newer = memoryCandidate({ memory_id: "m-new", observed_at: "2026-10-08T00:00:00Z" });
    expect(promptCandidate([older, newer], new Set())?.memory_id).toBe("m-new");
  });

  it("counts the ready candidates and every held item for the Memory badge", () => {
    expect(badgeCount(designCandidates())).toBe(5);
    expect(badgeCount([])).toBe(0);
  });

  it("orders 推定の候補 by readiness and 保留中 newest first", () => {
    const all = designCandidates();
    expect(memoryCandidates(all).map((item) => [item.title, askState(item)])).toEqual([
      ["pytest は -x を付けて実行する", "ready"],
      ["レビューは Codex と Claude の両方", "ready"],
      ["コミット前に ruff format を実行する", "notYet"],
      ["lint を飛ばしてテストだけ回す", "once"],
      ["Lint は旧設定を使う", "conflicting"],
    ]);
    expect(heldCandidates(all).map((item) => item.key)).toEqual([
      "merge.after_ci",
      "ui.language",
      "pr.language",
    ]);
  });

  it("needs the acknowledgement for a high-risk or high-risk-held candidate only", () => {
    const [pytest] = designCandidates();
    expect(pytest && needsAcknowledgement(pytest)).toBe(false);
    expect(needsAcknowledgement(heldCandidate({ entry_id: "e" }))).toBe(true);
    expect(
      needsAcknowledgement(heldCandidate({ entry_id: "e", held_reason: "held_confirmed" })),
    ).toBe(false);
  });

  it("maps candidates to paths and back", () => {
    const held = heldCandidate({ entry_id: "e-1", item_index: 2 });
    expect(candidateId(held)).toBe("e-1.2");
    expect(candidatePath(held)).toBe("/memory/held/e-1.2");
    expect(candidatePath(memoryCandidate({ memory_id: "m 1" }))).toBe("/memory/candidates/m%201");
    expect(preferencePath("/memory/candidates")).toEqual({ view: "candidates", id: null });
    expect(preferencePath("/memory/candidates/m%201")).toEqual({ view: "candidates", id: "m 1" });
    expect(preferencePath("/memory/held/e-1.2")).toEqual({ view: "held", id: "e-1.2" });
    expect(preferencePath("/memory/m-1")).toBeNull();
    expect(preferencePath("/memory")).toBeNull();
  });

  it("names projects and repositories from the scope tree", () => {
    const names = namesFrom({
      user: 0,
      shared: 0,
      projects: [
        {
          project_id: "p",
          name: "ExampleProject",
          count: 1,
          project_count: 1,
          repos: [{ repo_id: "r", name: "backend", count: 1 }],
        },
      ],
    });
    expect(names.project("p")).toBe("ExampleProject");
    expect(names.repo("r")).toBe("backend");
    expect(names.repo("other")).toBeNull();
    expect(namesFrom(null).project("p")).toBeNull();
  });
});
