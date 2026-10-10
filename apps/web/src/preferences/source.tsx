// Where the Inferred Preference screens read and write (issue #38).
//
// Like the Memory screen's MemorySource, the screens take their data through
// this interface: main.tsx plugs in `apiPreferenceSource` (api.ts, the Backend's
// /api/v1/memory/preferences routes, Decision 0081); the tests plug in a fake.
// Without a source nothing is asked in the chat and the Memory screen has no
// 推定の候補 / 保留中.
//
// The Backend decides everything: whether to ask (`ready`), the recommended
// scope, the buttons, whether a preference is high risk. A failure is the
// Backend's ApiError (409 `preference_candidate_changed`, 409
// `preference_high_risk_unacknowledged`, 403 `forbidden`, ...).
import { createContext, type ReactNode, useContext } from "react";
import type { MemoryVersion } from "../memory/types";
import type { Confirmation, PreferenceCandidate, PreferencePreview } from "./types";

export interface PreferenceSource {
  /** The person's candidates with their evidence, newest observation first. */
  candidates(): Promise<PreferenceCandidate[]>;
  /** Confirm at a button's scope, or as the その他 preview says. */
  confirm(candidate: PreferenceCandidate, confirmation: Confirmation): Promise<MemoryVersion>;
  /** [保存しない]. */
  reject(candidate: PreferenceCandidate): Promise<void>;
  /** The structured preview of a free-text answer; writes nothing. */
  interpret(candidate: PreferenceCandidate, text: string): Promise<PreferencePreview>;
}

const PreferenceSourceContext = createContext<PreferenceSource | null>(null);

export function PreferenceSourceProvider({
  source,
  children,
}: {
  source: PreferenceSource | null;
  children: ReactNode;
}) {
  return (
    <PreferenceSourceContext.Provider value={source}>{children}</PreferenceSourceContext.Provider>
  );
}

/** The plugged-in source, or null. */
export function usePreferenceSource(): PreferenceSource | null {
  return useContext(PreferenceSourceContext);
}
