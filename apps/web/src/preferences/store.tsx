// The person's preference candidates, shared by the chat's confirmation card, the
// Memory screen's 推定の候補 / 保留中 and the Memory badge of the navigation
// (issue #38). One read of GET /memory/preferences/candidates serves all three;
// it is read again after every answer, when the tab comes back, and every
// minute while the tab is shown (candidates come from the journal worker, which
// has no push yet). A failed background read keeps what is shown.
//
// Also kept here, in this tab only: the candidates the person put off with ×
// ("あとで": no API call, only this conversation; the human's choice) and which
// candidate each assistant reply asked about (at most one card per reply).
import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useMemorySource } from "../memory/source";
import type { ScopeTree } from "../memory/types";
import { badgeCount, candidateId, type Names, namesFrom, promptCandidate } from "./model";
import { type PreferenceSource, usePreferenceSource } from "./source";
import type { PreferenceCandidate } from "./types";

export const REFRESH_MS = 60_000;

export interface PreferenceState {
  source: PreferenceSource;
  /** Null until the first answer. */
  candidates: PreferenceCandidate[] | null;
  /** The last read's failure (the last answer stays shown). */
  error: unknown;
  /** When the candidates were last read (ISO). */
  syncedAt: string | null;
  /** Read again; true when the latest read succeeded and was applied. */
  reload: () => Promise<boolean>;
  names: Names;
  /** The Memory badge: ready candidates and held items. */
  badge: number;
  /**
   * The candidate a reply asks about. The first call for a reply picks the
   * strongest ready candidate not put off in the conversation; later calls keep
   * it (also once answered), so a reply never shows a second card.
   */
  promptFor: (conversation: string, reply: string) => string | null;
  /** × (あとで): hide the card in this conversation only. */
  dismiss: (conversation: string, id: string) => void;
}

const PreferenceContext = createContext<PreferenceState | null>(null);

function Store({ source, children }: { source: PreferenceSource; children: ReactNode }) {
  const memory = useMemorySource();
  const [candidates, setCandidates] = useState<PreferenceCandidate[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [syncedAt, setSyncedAt] = useState<string | null>(null);
  const [tree, setTree] = useState<ScopeTree | null>(null);
  const [dismissed, setDismissed] = useState<ReadonlyMap<string, ReadonlySet<string>>>(
    () => new Map(),
  );
  const prompts = useRef(new Map<string, string | null>());

  // Reads may overlap (the minute's refresh and the one after an answer): only
  // the latest one started is applied, so an older answer never brings back a
  // candidate that was just answered. A superseded read answers with the
  // outcome of the read that replaced it.
  const generation = useRef(0);
  const latest = useRef<Promise<boolean>>(Promise.resolve(true));
  const reload = useCallback(() => {
    generation.current += 1;
    const mine = generation.current;
    const read: Promise<boolean> = source.candidates().then(
      (found) => {
        if (mine !== generation.current) return latest.current;
        setCandidates(found);
        setError(null);
        setSyncedAt(new Date().toISOString());
        return true;
      },
      (caught: unknown) => {
        if (mine !== generation.current) return latest.current;
        setError(caught);
        return false;
      },
    );
    latest.current = read;
    return read;
  }, [source]);

  useEffect(() => {
    void reload();
    const timer = window.setInterval(() => {
      if (document.visibilityState !== "hidden") void reload();
    }, REFRESH_MS);
    const onVisible = () => {
      if (document.visibilityState === "visible") void reload();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [reload]);

  // Project and repository names for the buttons (the Memory scope tree); without
  // it the buttons say "Project" / "Repo".
  useEffect(() => {
    if (!memory) return;
    let current = true;
    memory.scopes().then(
      (found) => {
        if (current) setTree(found);
      },
      () => {},
    );
    return () => {
      current = false;
    };
  }, [memory]);

  const promptFor = useCallback(
    (conversation: string, reply: string) => {
      const key = `${conversation}\u0000${reply}`;
      if (prompts.current.has(key)) return prompts.current.get(key) ?? null;
      if (candidates === null) return null;
      const chosen = promptCandidate(candidates, dismissed.get(conversation) ?? new Set());
      const id = chosen ? candidateId(chosen) : null;
      prompts.current.set(key, id);
      return id;
    },
    [candidates, dismissed],
  );

  const dismiss = useCallback((conversation: string, id: string) => {
    setDismissed((current) => {
      const next = new Map(current);
      next.set(conversation, new Set(current.get(conversation)).add(id));
      return next;
    });
  }, []);

  const names = useMemo(() => namesFrom(tree), [tree]);
  const value = useMemo<PreferenceState>(
    () => ({
      source,
      candidates,
      error,
      syncedAt,
      reload,
      names,
      badge: candidates ? badgeCount(candidates) : 0,
      promptFor,
      dismiss,
    }),
    [source, candidates, error, syncedAt, reload, names, promptFor, dismiss],
  );
  return <PreferenceContext.Provider value={value}>{children}</PreferenceContext.Provider>;
}

/** Mounted for a signed-in person (keyed by the account, so another one starts empty). */
export function PreferenceProvider({ children }: { children: ReactNode }) {
  const source = usePreferenceSource();
  if (!source) return <>{children}</>;
  return <Store source={source}>{children}</Store>;
}

/** The shared state, or null while no source is plugged in. */
export function usePreferences(): PreferenceState | null {
  return useContext(PreferenceContext);
}
