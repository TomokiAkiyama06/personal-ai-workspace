import { type DependencyList, useCallback, useEffect, useRef, useState } from "react";

export interface Loaded<T> {
  data: T | null;
  error: unknown;
  loading: boolean;
  reload: () => void;
}

/**
 * Run `load` when `deps` change (and on `reload`). A reload keeps the last
 * answer on screen; new deps drop it. An answer that arrives after the deps
 * moved on is dropped.
 */
export function useLoad<T>(load: () => Promise<T>, deps: DependencyList): Loaded<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [round, setRound] = useState(0);
  const reload = useCallback(() => setRound((value) => value + 1), []);
  const lastRound = useRef(round);
  useEffect(() => {
    let cancelled = false;
    // New deps ask for other data: drop the old answer (a reload keeps it on screen).
    if (lastRound.current === round) setData(null);
    lastRound.current = round;
    setLoading(true);
    setError(null);
    load().then(
      (value) => {
        if (cancelled) return;
        setData(value);
        setLoading(false);
      },
      (caught: unknown) => {
        if (cancelled) return;
        setError(caught);
        setLoading(false);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [...deps, round]);
  return { data, error, loading, reload };
}
