import { afterEach, describe, expect, it, vi } from "vitest";
import { apiError, mockApi, reply } from "../test/helpers";
import { heldCandidate, memoryCandidate } from "../test/preferenceFake";
import { apiPreferenceSource } from "./api";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("apiPreferenceSource", () => {
  const memory = memoryCandidate({ memory_id: "m-1", version_number: 3 });
  const held = heldCandidate({ entry_id: "e-1", item_index: 2 });

  it("reads the candidates", async () => {
    mockApi({ "GET /memory/preferences/candidates": reply(200, { candidates: [memory] }) });
    await expect(apiPreferenceSource.candidates()).resolves.toEqual([memory]);
  });

  it("answers a memory candidate at its version and a held one by its item", async () => {
    const { calls } = mockApi({
      "POST /memory/preferences/memories/m-1/confirm": reply(200, { memory_id: "m-1" }),
      "POST /memory/preferences/memories/m-1/reject": reply(200, { version: null }),
      "POST /memory/preferences/memories/m-1/interpret": reply(200, { content: "x" }),
      "POST /memory/preferences/held/e-1/2/confirm": reply(200, { memory_id: "m-2" }),
      "POST /memory/preferences/held/e-1/2/reject": reply(200, { version: null }),
      "POST /memory/preferences/held/e-1/2/interpret": reply(200, { content: "x" }),
    });
    const button = {
      scope: "repo" as const,
      project_id: "p",
      repo_id: "r",
      acknowledge_high_risk: false,
    };
    await apiPreferenceSource.confirm(memory, button);
    await apiPreferenceSource.reject(memory);
    await apiPreferenceSource.interpret(memory, "今後は");
    await apiPreferenceSource.confirm(held, { ...button, acknowledge_high_risk: true });
    await apiPreferenceSource.reject(held);
    await apiPreferenceSource.interpret(held, "今後は");
    expect(calls.map((call) => [call.path, call.body])).toEqual([
      ["/memory/preferences/memories/m-1/confirm", { expected_version: 3, ...button }],
      ["/memory/preferences/memories/m-1/reject", { expected_version: 3 }],
      ["/memory/preferences/memories/m-1/interpret", { expected_version: 3, text: "今後は" }],
      ["/memory/preferences/held/e-1/2/confirm", { ...button, acknowledge_high_risk: true }],
      ["/memory/preferences/held/e-1/2/reject", {}],
      ["/memory/preferences/held/e-1/2/interpret", { text: "今後は" }],
    ]);
  });

  it("passes the Backend's error codes on", async () => {
    mockApi({
      "POST /memory/preferences/held/e-1/2/confirm": apiError(
        409,
        "preference_high_risk_unacknowledged",
      ),
    });
    await expect(
      apiPreferenceSource.confirm(held, {
        scope: "user",
        project_id: null,
        repo_id: null,
        acknowledge_high_risk: false,
      }),
    ).rejects.toMatchObject({ code: "preference_high_risk_unacknowledged" });
  });
});
