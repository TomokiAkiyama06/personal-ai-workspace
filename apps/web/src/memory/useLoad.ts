import { type DependencyList, useCallback, useEffect, useState } from "react";

export interface Loaded<T> {
  data: T | null;
  error: unknown;
  loading: boolean;
  reload: () => void;
}

/**
 * Run `load` when `deps` change (and on `reload`), keeping the last answer.
 * An answer that arrives after the deps moved on is dropped.
 */
export function useLoad<T>(load: () => Promise<T>, deps: DependencyList): Loaded<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [round, setRound] = useState(0);
  const reload = useCallback(() => setRound((value) => value + 1), []);
  useEffect(() => {
    let cancelled = false;
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
