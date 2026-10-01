// Where the Memory screen reads and writes (PAW-063).
//
// Like the Notification Center (Decision 0044, 11), the screen takes its data
// through this interface and shows the "not available yet" state while no source
// is plugged in. In production main.tsx plugs in `apiMemorySource` (apiSource.ts,
// the Backend's /api/v1/memory routes, issue #186); the tests plug in a fake.
//
// Every call is answered by the Backend's rules, never the screen's: a failure is
// an `ApiError` with the Backend's code (`memory_version_conflict` when the
// expected version is not the current one, `memory_not_found`, ...).
import { createContext, type ReactNode, useContext } from "react";
import type {
  MemoryEdit,
  MemoryHistory,
  MemorySourceRecord,
  MemoryVersion,
  ScopeKey,
  ScopeTree,
} from "./types";

export interface MemorySource {
  /** The Scope pane: the scopes the reader may see, with counts. */
  scopes(): Promise<ScopeTree>;
  /** The current version of each memory of `scope`; `query` searches text and sources. */
  list(scope: ScopeKey, query: string): Promise<MemoryVersion[]>;
  /** Every readable version of a memory and its relations. */
  history(memoryId: string): Promise<MemoryHistory>;
  /** The sources of one version. */
  sources(memoryId: string, versionNumber: number): Promise<MemorySourceRecord[]>;
  /**
   * A manual edit: a new `confirmed` version after `expectedVersion`
   * (`edit_memory`, optimistic lock). The previous version stays in the history.
   */
  edit(memoryId: string, expectedVersion: number, changes: MemoryEdit): Promise<MemoryVersion>;
  /**
   * A new active version with the content of `sourceVersion` (`restore_version`).
   * The old version itself is never made active again.
   */
  restore(memoryId: string, expectedVersion: number, sourceVersion: number): Promise<MemoryVersion>;
}

const MemorySourceContext = createContext<MemorySource | null>(null);

export function MemorySourceProvider({
  source,
  children,
}: {
  source: MemorySource | null;
  children: ReactNode;
}) {
  return <MemorySourceContext.Provider value={source}>{children}</MemorySourceContext.Provider>;
}

/** The plugged-in source, or null (no Memory API yet). */
export function useMemorySource(): MemorySource | null {
  return useContext(MemorySourceContext);
}
