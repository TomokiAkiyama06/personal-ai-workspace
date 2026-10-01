import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import { apiError, mockApi, reply } from "../test/helpers";
import { version } from "../test/memoryFake";
import { apiMemorySource } from "./apiSource";

afterEach(() => {
  vi.unstubAllGlobals();
});

/** The URL (path and query, below /api/v1) of each request. */
function urls(fetchMock: ReturnType<typeof mockApi>["fetchMock"]): string[] {
  return fetchMock.mock.calls.map(([input]) => {
    const url = new URL(String(input), "http://localhost");
    return `${url.pathname.replace(/^\/api\/v1/, "")}${url.search}`;
  });
}

describe("apiMemorySource (/api/v1/memory)", () => {
  it("reads the scope tree as the Backend answers it", async () => {
    const tree = { user: 2, projects: [], shared: 1 };
    mockApi({ "GET /memory/scopes": reply(200, tree) });
    await expect(apiMemorySource.scopes()).resolves.toEqual(tree);
  });

  it("lists a scope with its ids and the search, and unwraps the memories", async () => {
    const memory = version({ memory_id: "m-1" });
    const { fetchMock } = mockApi({
      "GET /memory/memories": reply(200, { memories: [memory], truncated: false }),
    });
    await expect(apiMemorySource.list({ kind: "user" }, "")).resolves.toEqual([memory]);
    await apiMemorySource.list({ kind: "project", project_id: "p-1" }, "deploy day");
    await apiMemorySource.list({ kind: "repo", project_id: "p-1", repo_id: "r/1" }, "  ");
    await apiMemorySource.list({ kind: "shared" }, "50%");
    expect(urls(fetchMock)).toEqual([
      "/memory/memories?scope=user",
      "/memory/memories?scope=project&project_id=p-1&q=deploy+day",
      "/memory/memories?scope=repo&project_id=p-1&repo_id=r%2F1",
      "/memory/memories?scope=shared&q=50%25",
    ]);
  });

  it("reads the history and the sources of a version", async () => {
    const history = { versions: [], relations: [], related: [], can_write: false };
    const source = {
      source_type: "task",
      conversation_id: null,
      message_id: null,
      source_ref: "task-7",
      source_deleted_at: null,
      created_at: "2026-09-24T12:00:00Z",
    };
    const { fetchMock } = mockApi({
      "GET /memory/memories/m%201/history": reply(200, history),
      "GET /memory/memories/m%201/versions/3/sources": reply(200, { sources: [source] }),
    });
    await expect(apiMemorySource.history("m 1")).resolves.toEqual(history);
    await expect(apiMemorySource.sources("m 1", 3)).resolves.toEqual([source]);
    expect(urls(fetchMock)).toEqual([
      "/memory/memories/m%201/history",
      "/memory/memories/m%201/versions/3/sources",
    ]);
  });

  it("edits and restores with the expected version", async () => {
    const written = version({ memory_id: "m-1", version_number: 4 });
    const { calls } = mockApi({
      "POST /memory/memories/m-1/edit": reply(200, written),
      "POST /memory/memories/m-1/restore": reply(200, written),
    });
    await expect(
      apiMemorySource.edit("m-1", 3, { content: "new", reason: "why" }),
    ).resolves.toEqual(written);
    await apiMemorySource.restore("m-1", 3, 1);
    expect(calls.map((call) => [call.method, call.path, call.body])).toEqual([
      ["POST", "/memory/memories/m-1/edit", { expected_version: 3, content: "new", reason: "why" }],
      ["POST", "/memory/memories/m-1/restore", { expected_version: 3, source_version: 1 }],
    ]);
  });

  it("passes the Backend's error codes on", async () => {
    mockApi({
      "POST /memory/memories/m-1/edit": apiError(409, "memory_version_conflict"),
      "GET /memory/memories/m-2/history": apiError(404, "memory_not_found"),
    });
    await expect(apiMemorySource.edit("m-1", 1, { title: "t" })).rejects.toMatchObject({
      status: 409,
      code: "memory_version_conflict",
    });
    const missing = apiMemorySource.history("m-2");
    await expect(missing).rejects.toBeInstanceOf(ApiError);
    await expect(missing).rejects.toMatchObject({ code: "memory_not_found" });
  });
});
