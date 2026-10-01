import { screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { isApiError } from "../api/client";
import { apiError, mockApi, renderApp, reply, session } from "../test/helpers";
import { exampleDetail, SUMMARIES } from "../test/projectsFixture";
import { projectsApi } from "./api";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("projectsApi", () => {
  it("reads the list and a project's detail", async () => {
    const detail = exampleDetail();
    const { calls } = mockApi({
      "GET /projects": reply(200, { projects: SUMMARIES, truncated: false }),
      "GET /projects/p-example": reply(200, detail),
    });
    await expect(projectsApi.list()).resolves.toEqual(SUMMARIES);
    await expect(projectsApi.detail("p-example")).resolves.toEqual(detail);
    expect(calls.map((call) => `${call.method} ${call.path}`)).toEqual([
      "GET /projects",
      "GET /projects/p-example",
    ]);
  });

  it("creates a project and returns its id", async () => {
    const { calls } = mockApi({
      "POST /projects": reply(201, { id: "p-new", name: "New", status: "active" }),
    });
    await expect(projectsApi.create({ name: "New", description: null })).resolves.toEqual({
      id: "p-new",
    });
    expect(calls[0]?.body).toEqual({ name: "New", description: null });
  });

  it("sends each lifecycle operation to its route, the deletion with the name", async () => {
    const { calls } = mockApi({
      "POST /projects/p-1/archive": reply(200, {}),
      "POST /projects/p-1/unarchive": reply(200, {}),
      "POST /projects/p-1/begin-deletion": reply(200, {}),
      "POST /projects/p-1/restore": reply(200, {}),
    });
    await projectsApi.lifecycle("p-1", "archive");
    await projectsApi.lifecycle("p-1", "unarchive");
    await projectsApi.lifecycle("p-1", "begin_deletion", "Project One");
    await projectsApi.lifecycle("p-1", "restore");
    expect(calls.map((call) => [call.path, call.body])).toEqual([
      ["/projects/p-1/archive", undefined],
      ["/projects/p-1/unarchive", undefined],
      ["/projects/p-1/begin-deletion", { confirm_name: "Project One" }],
      ["/projects/p-1/restore", undefined],
    ]);
  });

  it("registers a repository and changes a role", async () => {
    const { calls } = mockApi({
      "POST /projects/p-1/repositories": reply(201, {}),
      "PUT /projects/p-1/members/u-2/role": reply(200, { user_id: "u-2", role: "viewer" }),
    });
    await projectsApi.registerRepository("p-1", {
      source: "github_clone",
      url: "owner/repo",
      branch: "dev",
    });
    await projectsApi.changeRole("p-1", "u-2", "viewer");
    expect(calls.map((call) => [call.method, call.path, call.body])).toEqual([
      [
        "POST",
        "/projects/p-1/repositories",
        { source: "github_clone", url: "owner/repo", branch: "dev" },
      ],
      ["PUT", "/projects/p-1/members/u-2/role", { role: "viewer" }],
    ]);
  });

  it("escapes the ids in the path", async () => {
    const { calls } = mockApi({});
    await projectsApi.detail("a/b").catch(() => undefined);
    expect(calls[0]?.init).toBeDefined();
    expect(calls[0]?.path).toBe("/projects/a%2Fb");
  });

  it("passes the Backend's refusal on", async () => {
    mockApi({ "POST /projects/p-1/begin-deletion": apiError(422, "confirmation_mismatch") });
    const error = await projectsApi.lifecycle("p-1", "begin_deletion", "x").catch((e) => e);
    expect(isApiError(error, "confirmation_mismatch")).toBe(true);
  });
});

describe("the production app", () => {
  it("reads the screen from the Backend's routes", async () => {
    mockApi({
      "GET /auth/session": reply(200, session()),
      "GET /projects": reply(200, { projects: SUMMARIES, truncated: false }),
      "GET /projects/p-example": reply(200, exampleDetail()),
    });
    renderApp("/projects/p-example");
    expect(await screen.findByRole("heading", { name: "ExampleProject" })).toBeInTheDocument();
    expect(screen.getByText("InternalTools")).toBeInTheDocument();
    expect(screen.queryByText("プロジェクトはまだ表示できません")).not.toBeInTheDocument();
  });
});
