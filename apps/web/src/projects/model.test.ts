import { describe, expect, it } from "vitest";
import { SUMMARIES } from "../test/projectsFixture";
import { effectivePermissions, lifecycleActions, memberAccess, sortProjects } from "./model";

const backend = { name: "backend", acl: null };
const iosReadOnly = { name: "ios", acl: ["read" as const] };
const closed = { name: "secret", acl: [] };

describe("effectivePermissions", () => {
  it("narrows the role by the override and never widens it", () => {
    expect(effectivePermissions("manager", backend)).toEqual(["read", "write", "agent"]);
    expect(effectivePermissions("contributor", iosReadOnly)).toEqual(["read"]);
    expect(effectivePermissions("viewer", { acl: ["read", "write"] })).toEqual(["read"]);
    expect(effectivePermissions("manager", closed)).toEqual([]);
  });
});

describe("memberAccess", () => {
  it("is all repositories (or read-only for a Viewer) without an override", () => {
    expect(memberAccess("manager", [backend])).toEqual({ kind: "all" });
    expect(memberAccess("viewer", [backend, iosReadOnly])).toEqual({ kind: "read_only" });
    expect(memberAccess("contributor", [])).toEqual({ kind: "all" });
  });

  it("names the repositories an override narrows or closes", () => {
    expect(memberAccess("contributor", [backend, iosReadOnly, closed])).toEqual({
      kind: "partial",
      full: ["backend"],
      limited: ["ios"],
      excluded: ["secret"],
    });
    expect(memberAccess("viewer", [backend, closed])).toEqual({
      kind: "partial",
      full: ["backend"],
      limited: [],
      excluded: ["secret"],
    });
  });

  it("is none when every repository is closed", () => {
    expect(memberAccess("manager", [closed])).toEqual({ kind: "none" });
  });
});

describe("lifecycleActions", () => {
  it("follows the lifecycle of REQUIREMENTS.md", () => {
    expect(lifecycleActions("active")).toEqual(["archive", "begin_deletion"]);
    expect(lifecycleActions("archived")).toEqual(["unarchive", "begin_deletion"]);
    expect(lifecycleActions("pending_deletion")).toEqual(["restore"]);
  });
});

describe("sortProjects", () => {
  it("puts Active first, then Archived, then Pending deletion", () => {
    const shuffled = [...SUMMARIES].reverse();
    expect(sortProjects(shuffled).map((project) => project.name)).toEqual([
      "ExampleProject",
      "InternalTools",
      "ResearchNotes",
      "LegacyAPI",
    ]);
  });
});
