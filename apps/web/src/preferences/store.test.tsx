import { act, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { designCandidates } from "../test/preferenceFake";
import { type PreferenceSource, PreferenceSourceProvider } from "./source";
import { PreferenceProvider, usePreferences } from "./store";
import type { PreferenceCandidate } from "./types";

/** A source whose reads answer when the test says so. */
function deferredSource() {
  const reads: Array<(found: PreferenceCandidate[]) => void> = [];
  const source: PreferenceSource = {
    candidates: () => new Promise((resolve) => reads.push(resolve)),
    confirm: () => Promise.reject(new Error("unused")),
    reject: () => Promise.reject(new Error("unused")),
    interpret: () => Promise.reject(new Error("unused")),
  };
  return { source, reads };
}

function Probe() {
  const state = usePreferences();
  return (
    <>
      <span data-testid="count">{state?.candidates?.length ?? "none"}</span>
      <button type="button" onClick={() => void state?.reload()}>
        reload
      </button>
    </>
  );
}

describe("the preference candidates store", () => {
  it("keeps the latest read when an older one answers last", async () => {
    const { source, reads } = deferredSource();
    render(
      <PreferenceSourceProvider source={source}>
        <PreferenceProvider>
          <Probe />
        </PreferenceProvider>
      </PreferenceSourceProvider>,
    );
    expect(reads).toHaveLength(1);
    act(() => screen.getByRole("button", { name: "reload" }).click());
    expect(reads).toHaveLength(2);
    const all = designCandidates();
    await act(async () => reads[1]?.(all.slice(1)));
    expect(screen.getByTestId("count")).toHaveTextContent(String(all.length - 1));
    // The older read (before the answer) still lists the answered candidate.
    await act(async () => reads[0]?.(all));
    expect(screen.getByTestId("count")).toHaveTextContent(String(all.length - 1));
  });
});
